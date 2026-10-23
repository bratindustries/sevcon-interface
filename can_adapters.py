"""USB CAN adapter backends for the Sevcon CANopen GUI.

Each driver exposes send(can_id, data), frames (timestamp, id, bytes),
transaction_lock, active, failure, and close(). No CANopen logic lives here.
GVRET follows SavvyCAN's classic CAN serial binary protocol.
"""
import queue
import threading
import time
import serial

RATES = {"10000":0,"20000":1,"50000":2,"100000":3,"125000":4,"250000":5,"500000":6,"800000":7,"1000000":8}


class GVRETParser:
    """Incremental GVRET device-to-PC parser for classic CAN plus control replies."""
    LENGTHS = {1:4, 6:10, 7:6, 9:2, 12:1, 13:15}

    def __init__(self, on_frame, on_validation, on_bus_info):
        self.buf=bytearray()
        self.on_frame=on_frame
        self.on_validation=on_validation
        self.on_bus_info=on_bus_info

    def feed(self,data):
        self.buf.extend(data)
        while True:
            if not self.buf:return
            start=self.buf.find(b'\xf1')
            if start<0:self.buf.clear();return
            if start:self.buf[:start]=b''
            if len(self.buf)<2:return
            command=self.buf[1]
            if command==0:
                if len(self.buf)<11:return
                size=self.buf[10]&15
                if size>8:
                    del self.buf[0];continue
                total=11+size
                if len(self.buf)<total:return
                packet=bytes(self.buf[:total]);del self.buf[:total]
                raw_id=int.from_bytes(packet[6:10],'little')
                # Only classic standard CAN frames can carry CANopen SDO.
                bus=packet[10]>>4
                if raw_id&0x80000000 or raw_id>0x7FF:continue
                self.on_frame(bus,raw_id,packet[11:total])
            elif command in self.LENGTHS:
                total=2+self.LENGTHS[command]
                if len(self.buf)<total:return
                payload=bytes(self.buf[2:total]);del self.buf[:total]
                if command==9 and payload==b'\xde\xad':self.on_validation()
                if command==6:self.on_bus_info(payload)
            else:
                # Unknown command cannot be decoded safely; resync.
                del self.buf[0]


class GVRETAdapter:
    """SavvyCAN/GVRET compatible serial adapter, CAN0 or CAN1."""
    def __init__(self,port,rate,eventq,bus=0):
        if bus not in (0,1):raise ValueError('GVRET supports CAN0 or CAN1 here')
        if int(rate)<=0:raise ValueError('Invalid CAN bitrate')
        self.q=eventq
        self.bus=bus
        self.rate=int(rate)
        self.frames=queue.Queue(maxsize=256)
        self.transaction_lock=threading.RLock()
        self.write_lock=threading.Lock()
        self.active=True
        self.failure=None
        self.validated=threading.Event()
        self.current_buses=None
        self.ser=serial.Serial(port,1000000,timeout=.05,rtscts=False,dsrdtr=False)
        self.worker=threading.Thread(target=self._reader,daemon=True)
        self.worker.start()
        try:
            # Enter binary mode, request validation and current CAN configuration.
            self._write(b'\xe7\xe7\xf1\x09\xf1\x06')
            if not self.validated.wait(3):
                raise TimeoutError('GVRET did not respond to binary validation (F1 09)')
            # Update only the chosen bus; preserve other channel settings.
            setting=(self.rate|0x80000000|0x40000000).to_bytes(4,'little')
            unchanged=bytes(4)
            config=(setting+unchanged) if bus==0 else (unchanged+setting)
            self._write(b'\xf1\x05'+config+b'\x00')
        except Exception:
            self.close()
            raise

    def _write(self,payload):
        if not self.active or self.failure:raise ConnectionError('GVRET disconnected: '+str(self.failure))
        with self.write_lock:self.ser.write(payload)

    def _rx_frame(self,bus,cid,data):
        if bus!=self.bus:return
        ts=time.monotonic()
        if 0x580<=cid<=0x5FF:
            try:self.frames.put_nowait((ts,cid,data))
            except queue.Full:self.q.put(('error','GVRET SDO receive queue overflow'))
        self.q.put(('frame',cid,data))

    def _reader(self):
        parser=GVRETParser(self._rx_frame,self.validated.set,self._bus_info)
        while self.active:
            try:
                chunk=self.ser.read(64)
                if chunk:parser.feed(chunk)
            except Exception as exc:
                if self.active:
                    self.failure=str(exc);self.q.put(('error','GVRET: '+str(exc)))
                break

    def _bus_info(self,payload):
        self.current_buses=payload

    def send(self,cid,data):
        if not 0<=cid<=0x7FF or len(data)>8:raise ValueError('GVRET requires classic 11-bit CAN, DLC 0..8')
        packet=b'\xf1\x00'+cid.to_bytes(4,'little')+bytes((self.bus,len(data)))+data+b'\x00'
        self._write(packet)

    def close(self):
        self.active=False
        try:self.ser.close()
        except Exception:pass


class EwertCANdapter:
    def __init__(self, port, rate, eventq):
        self.ser=serial.Serial(port,9600,timeout=.08)
        self.q=eventq; self.lock=threading.Lock(); self.transaction_lock=threading.RLock(); self.acks=queue.Queue(); self.frames=queue.Queue(maxsize=256); self.active=True;self.failure=None
        self.worker=threading.Thread(target=self._reader,daemon=True); self.worker.start()
        try:
            self.command(f'S{RATES[rate]}')
            self.command('O')
        except Exception:
            self.close();raise
    def _reader(self):
        buf=bytearray()
        while self.active:
            try:
                b=self.ser.read(1)
                if not b: continue
                if b in (b'\x06',b'\x15') and not buf:
                    self.acks.put(b==b'\x06');continue
                if b in (b'\r',b'\n'):
                    if buf:
                        line=bytes(buf).decode('ascii','replace');buf.clear()
                        if len(line)>=5 and line[0] in ('T','t'):
                            try:
                                cid=int(line[1:4],16);n=int(line[4],16)
                                data=bytes.fromhex(line[5:5+n*2])
                                if len(data)==n:
                                    if 0x580 <= cid <= 0x5FF:
                                    try:self.frames.put_nowait((time.monotonic(),cid,data))
                                    except queue.Full:self.q.put(('error','SDO receive queue overflow'))
                                self.q.put(('frame',cid,data))
                            except ValueError:pass
                    continue
                if len(buf)<256:buf.extend(b)
                else:buf.clear()
            except Exception as e:
                if self.active:
                    self.failure=str(e);self.q.put(('error',str(e)))
                break
    def command(self, value):
        if not self.active or self.failure:raise ConnectionError('CANdapter unavailable: '+str(self.failure))
        with self.lock:
            while not self.acks.empty():self.acks.get_nowait()
            self.ser.write((value+'\n').encode('ascii'))
            try:success=self.acks.get(timeout=1.5)
            except queue.Empty:raise TimeoutError('CANdapter did not acknowledge command')
            if not success:raise RuntimeError('CANdapter rejected command: '+value[:1])
    def send(self, cid, data):
        self.command(f'T{cid:03X}{len(data):X}{data.hex().upper()}')
    def close(self):
        self.active=False
        try:self.ser.write(b'C\n')
        except Exception:pass
        try:self.ser.close()
        except Exception:pass


ADAPTERS = {'Ewert CANdapter': EwertCANdapter, 'GVRET': GVRETAdapter}
