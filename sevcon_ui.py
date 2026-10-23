"""Sevcon Gen4 CANopen diagnostic GUI for Ewert CANdapter.
Python 3.10+, pyserial. Not a motor commissioning tool.
"""
import csv, datetime, queue, threading, time, tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
from serial.tools import list_ports
from eds_support import load_eds, decode as eds_decode, encode as eds_encode

RATES = {"10000":0,"20000":1,"50000":2,"100000":3,"125000":4,"250000":5,"500000":6,"800000":7,"1000000":8}
# Unit scaling verified for Gen4 battery voltage protection objects.
# Other CANopen objects stay raw until their firmware-specific type is known.
VOLTAGE_OBJECTS = {
    (0x2C00, 0): "Nominal battery voltage",
    (0x2C01, 1): "Overvoltage cutback start",
    (0x2C01, 2): "Overvoltage shutdown",
    (0x2C02, 1): "Undervoltage cutback start",
    (0x2C02, 2): "Undervoltage shutdown",
}
VOLT_STEP = 0.0625
OBJECTS = [("CANopen error register",0x1001,0),("Error history count",0x1003,0),
    ("Identity vendor",0x1018,1),("Identity product",0x1018,2),
    ("Identity revision",0x1018,3),("Active fault count (Sevcon)",0x5300,1)] + [
    (name, index, sub) for (index, sub), name in VOLTAGE_OBJECTS.items()]

def decode_value(index, sub, raw):
    number = int.from_bytes(raw, "little", signed=False)
    if (index, sub) in VOLTAGE_OBJECTS:
        return f"{number * VOLT_STEP:.4f} V"
    if (index, sub) == (0x1001, 0):
        flags = ["generic", "current", "voltage", "temperature", "communications", "device profile"]
        bits = [0, 1, 2, 3, 4, 7]
        active = [label for bit, label in zip(bits, flags) if number & (1 << bit)]
        return f"0x{number:02X} (" + (", ".join(active) if active else "no flags") + ")"
    return str(number) + " (raw)"

def volts_to_raw(index, sub, text):
    if (index, sub) not in VOLTAGE_OBJECTS:
        raise ValueError("No verified voltage scaling for this object")
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    try:
        volts = Decimal(text.strip())
        if not volts.is_finite() or volts < 0:
            raise ValueError("Voltage must be a nonnegative finite number")
        count = int((volts / Decimal('0.0625')).to_integral_value(rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise ValueError("Enter a voltage in volts (for example 90.0)")
    if count > 65535: raise ValueError("Voltage exceeds the 16-bit CANopen range")
    return count.to_bytes(2, "little")


class Adapter:
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

class GUI:
    def __init__(self, root):
        self.root=root;root.title('Sevcon Gen4 — CANdapter CANopen');root.geometry('1030x770')
        self.dev=None;self.busy=False;self.events=queue.Queue();self.samples=[];self.last_read=None;self.session=0;self.verified=False;self.connected_node=None;self.last_sdo_timeout=0
        self.eds_entries=[];self.eds_visible=[];self.eds_last=None
        top=ttk.LabelFrame(root,text='Connection (Ewert CANdapter only)');top.pack(fill='x',padx=10,pady=6)
        ttk.Label(top,text='COM port').pack(side='left',padx=5)
        self.port=tk.StringVar();self.ports=ttk.Combobox(top,textvariable=self.port,width=12);self.ports.pack(side='left')
        ttk.Button(top,text='Refresh',command=self.refresh_ports).pack(side='left',padx=4)
        ttk.Label(top,text='CAN bitrate').pack(side='left',padx=5)
        self.rate=tk.StringVar(value='1000000');self.rate_widget=ttk.Combobox(top,textvariable=self.rate,values=list(RATES),width=11,state='readonly');self.rate_widget.pack(side='left')
        ttk.Label(top,text='Node ID').pack(side='left',padx=5)
        self.node=tk.StringVar(value='1');self.node_widget=ttk.Entry(top,textvariable=self.node,width=5);self.node_widget.pack(side='left')
        ttk.Button(top,text='Connect',command=self.connect).pack(side='left',padx=6)
        ttk.Button(top,text='Disconnect',command=self.disconnect).pack(side='left')
        ttk.Button(top,text='Verify controller',command=self.verify_controller).pack(side='left',padx=4)
        self.status=tk.StringVar(value='Disconnected');ttk.Label(top,textvariable=self.status).pack(side='left',padx=8)
        tabs=ttk.Notebook(root);tabs.pack(fill='both',expand=True,padx=10,pady=5)
        diag=ttk.Frame(tabs);tabs.add(diag,text='Faults and diagnostics')
        actions=ttk.Frame(diag);actions.pack(fill='x',pady=7)
        ttk.Button(actions,text='Read all diagnostics',command=self.read_all).pack(side='left',padx=5)
        ttk.Button(actions,text='Read active faults',command=self.read_faults).pack(side='left',padx=5)
        ttk.Button(actions,text='Export readings CSV',command=self.export).pack(side='left',padx=5)
        self.tree=ttk.Treeview(diag,columns=('obj','raw','value','result'),show='headings',height=17)
        for col,w in [('obj',335),('raw',160),('value',240),('result',150)]:self.tree.heading(col,text=col.title());self.tree.column(col,width=w)
        self.tree.pack(fill='both',expand=True)
        ttk.Label(diag,text='Active Sevcon fault IDs and controller-provided descriptions are read via 0x5300 and 0x5610. Fault selectors change diagnostic selection only.').pack(anchor='w',padx=5)
        settings=ttk.Frame(tabs);tabs.add(settings,text='Settings')
        ttk.Label(settings,text='Select a battery setting. Values are displayed in volts, with the original CANopen bytes shown below.',wraplength=900).pack(anchor='w',padx=12,pady=10)
        selector=ttk.Frame(settings);selector.pack(fill='x',padx=12,pady=6)
        ttk.Label(selector,text='Setting').pack(side='left',padx=4)
        self.setting_name=tk.StringVar(value=list(VOLTAGE_OBJECTS.values())[0])
        self.setting_choices=ttk.Combobox(selector,textvariable=self.setting_name,
            values=list(VOLTAGE_OBJECTS.values())+['Custom CANopen object (raw)'],width=38,state='readonly')
        self.setting_choices.pack(side='left',padx=5)
        self.setting_choices.bind('<<ComboboxSelected>>',self.update_selected)
        ttk.Button(selector,text='Read setting',command=self.read_one).pack(side='left',padx=8)
        frm=ttk.Frame(settings);frm.pack(anchor='w',padx=12,pady=5)
        self.index=tk.StringVar(value='2C00');self.sub=tk.StringVar(value='00')
        for label,var,w in [('Index hex',self.index,10),('Subindex hex',self.sub,8)]:
            ttk.Label(frm,text=label).pack(side='left',padx=4);ttk.Entry(frm,textvariable=var,width=w).pack(side='left')
        self.val=tk.StringVar(value='Current value: not read')
        ttk.Label(settings,textvariable=self.val,font=('Consolas',12)).pack(anchor='w',padx=12,pady=9)
        ttk.Separator(settings).pack(fill='x',pady=10)
        self.new_label=tk.StringVar(value='New voltage (V):')
        ttk.Label(settings,textvariable=self.new_label).pack(anchor='w',padx=12)
        self.data=tk.StringVar()
        ttk.Entry(settings,textvariable=self.data,width=38).pack(anchor='w',padx=12,pady=5)
        ttk.Label(settings,text='A confirmation window shows the current and requested values before any write. The application verifies the result by reading it back.',wraplength=900).pack(anchor='w',padx=12,pady=8)
        ttk.Button(settings,text='Apply change…',command=self.write_one).pack(anchor='w',padx=12,pady=6)
        ttk.Button(settings,text='Save to Controller…',command=self.save_controller).pack(anchor='w',padx=12,pady=6)
        ttk.Label(settings,text='Saving is a separate operation using CANopen 1010:01. It may require Sevcon authorization and Pre-operational state. Acknowledgement is not a power-cycle persistence test.',wraplength=900).pack(anchor='w',padx=12,pady=8)
        self.update_selected()
        self.build_eds_tab(tabs)
        self.build_security_tab(tabs)
        logframe=ttk.LabelFrame(root,text='Communication log');logframe.pack(fill='both',expand=False,padx=10,pady=6)
        self.log=tk.Text(logframe,height=9,font=('Consolas',9));self.log.pack(fill='both',expand=True)
        self.refresh_ports();self.root.after(50,self.pump);root.protocol('WM_DELETE_WINDOW',self.quit)
    def build_eds_tab(self, tabs):
        page=ttk.Frame(tabs);tabs.add(page,text='EDS browser')
        header=ttk.Frame(page);header.pack(fill='x',padx=10,pady=8)
        ttk.Button(header,text='Open EDS…',command=self.open_eds).pack(side='left')
        self.eds_file=tk.StringVar(value='No EDS loaded')
        ttk.Label(header,textvariable=self.eds_file).pack(side='left',padx=10)
        filt=ttk.Frame(page);filt.pack(fill='x',padx=10,pady=4)
        ttk.Label(filt,text='Search index or name').pack(side='left')
        self.eds_query=tk.StringVar();self.eds_query.trace_add('write',lambda *_:self.filter_eds())
        ttk.Entry(filt,textvariable=self.eds_query,width=42).pack(side='left',padx=8)
        self.eds_tree=ttk.Treeview(page,columns=('index','name','type','access','default'),show='headings',height=15)
        for key,width in [('index',105),('name',390),('type',130),('access',80),('default',160)]:
            self.eds_tree.heading(key,text=key.title());self.eds_tree.column(key,width=width)
        self.eds_tree.pack(fill='both',expand=True,padx=10)
        self.eds_tree.bind('<<TreeviewSelect>>',self.eds_select)
        self.eds_details=tk.StringVar(value='Load an EDS to browse its object dictionary. EDS data is descriptive, not live controller data.')
        ttk.Label(page,textvariable=self.eds_details,wraplength=940).pack(anchor='w',padx=12,pady=6)
        ops=ttk.Frame(page);ops.pack(fill='x',padx=10,pady=4)
        ttk.Button(ops,text='Read selected',command=self.eds_read).pack(side='left',padx=3)
        self.eds_current=tk.StringVar(value='Value: not read')
        ttk.Label(ops,textvariable=self.eds_current).pack(side='left',padx=15)
        edit=ttk.Frame(page);edit.pack(fill='x',padx=10,pady=8)
        ttk.Label(edit,text='New typed value').pack(side='left')
        self.eds_new=tk.StringVar();ttk.Entry(edit,textvariable=self.eds_new,width=28).pack(side='left',padx=8)
        ttk.Button(edit,text='Apply change…',command=self.eds_write).pack(side='left',padx=4)
        ttk.Label(page,text='Writes require EDS write access, a matching live read, confirmation and read-back. No automatic save to controller memory.',wraplength=940).pack(anchor='w',padx=12,pady=4)

    def build_security_tab(self, tabs):
        page=ttk.Frame(tabs);tabs.add(page,text='Security')
        ttk.Label(page,text='Sevcon access diagnostics and optional credential submission. No automatic NMT state changes.',wraplength=940).pack(anchor='w',padx=12,pady=12)
        ttk.Button(page,text='Refresh security information',command=self.read_security).pack(anchor='w',padx=12,pady=5)
        self.security_values={}
        for label,key in [('Security access level (5000:01)','level'),
                          ('Operational state (5110:00)','state'),
                          ('Detailed SDO error reason (5310:00)','reason')]:
            row=ttk.Frame(page);row.pack(fill='x',padx=12,pady=8)
            ttk.Label(row,text=label,width=38).pack(side='left')
            variable=tk.StringVar(value='Not read')
            ttk.Label(row,textvariable=variable,wraplength=540).pack(side='left')
            self.security_values[key]=variable
        ttk.Label(page,text='Note: 5310:00 may be unsupported or firmware-specific. Failed reads are reported individually. Access levels do not guarantee permission to write a particular object.',wraplength=940).pack(anchor='w',padx=12,pady=14)
        auth=ttk.LabelFrame(page,text='Authorized login')
        auth.pack(fill='x',padx=12,pady=8)
        ttk.Label(auth,text='Requires a controller-specific pre-encoded response. Plain passwords are not converted by this tool.',wraplength=890).pack(anchor='w',padx=8,pady=5)
        line=ttk.Frame(auth);line.pack(anchor='w',padx=8,pady=5)
        ttk.Label(line,text='User ID (optional)').pack(side='left')
        self.auth_user=tk.StringVar()
        ttk.Entry(line,textvariable=self.auth_user,width=16).pack(side='left',padx=8)
        ttk.Label(line,text='Encoded response (hex)').pack(side='left')
        self.auth_token=tk.StringVar()
        ttk.Entry(line,textvariable=self.auth_token,show='*',width=21).pack(side='left',padx=8)
        ttk.Button(auth,text='Authorize…',command=self.authorize).pack(anchor='w',padx=8,pady=5)
        self.auth_result=tk.StringVar(value='Not attempted')
        ttk.Label(auth,textvariable=self.auth_result).pack(anchor='w',padx=8,pady=5)


    def authorize(self):
        """Submit an authorized, pre-encoded credential; never persist it."""
        if not self.dev or not self.verified:
            messagebox.showwarning('Not verified','Verify controller communication first.')
            return
        if self.busy:
            messagebox.showwarning('Busy','Finish the current CANopen request.')
            return
        try:
            token=self.auth_token.get().strip().replace(' ','')
            if token.lower().startswith('0x'):token=token[2:]
            if len(token) not in (2,4,8):
                raise ValueError('Encoded response must be 1, 2 or 4 hex bytes.')
            response=bytes.fromhex(token)
            user=self.auth_user.get().strip()
            user_id=None if not user else int(user,0) if user.lower().startswith('0x') else int(user)
            if user_id is not None and not 0<=user_id<=0xFFFFFFFF:
                raise ValueError('User ID outside 32-bit range')
        except ValueError as exc:
            messagebox.showerror('Invalid credentials',str(exc));return
        if not messagebox.askyesno('Authorize controller',
            'Send the supplied controller-specific encoded credential? '
            'Authentication can be rejected or logged by the controller.'):
            return
        self.auth_token.set('')
        self.auth_result.set('Authorization in progress…')
        def work():
            try:
                previous=int.from_bytes(self.sdo(0x5000,1),'little')
                if user_id is not None:
                    self.sdo(0x5000,3,user_id.to_bytes(4,'little'))
                self.sdo(0x5000,2,response)
                level=int.from_bytes(self.sdo(0x5000,1),'little')
                self.events.put(('ui',lambda v=level,p=previous:self._auth_complete(v,p)))
            except Exception as exc:
                self.events.put(('ui',lambda kind=type(exc).__name__:self._auth_error(kind)))
        self.job(work)

    def _auth_complete(self,level,previous):
        statement=f'Controller reports level {level} (previously {previous}).'
        self.auth_result.set(statement)
        self.security_values['level'].set(statement)
        self.info('Authorization transaction complete; credential withheld from log.')
        if level<=1:
            messagebox.showwarning('Access not elevated',statement)
        else:
            messagebox.showinfo('Access level read back',statement)

    def _auth_error(self,error_kind):
        self.auth_result.set('Authorization unverified: '+error_kind)
        self.info('Authorization failed or could not be verified ('+error_kind+'); credential withheld.')

    def read_security(self):
        probes=[('level',0x5000,1),('state',0x5110,0),('reason',0x5310,0)]
        for key,_,_ in probes:self.security_values[key].set('Reading…')
        def work():
            for key,index,sub in probes:
                try:
                    raw=self.sdo(index,sub)
                    value=int.from_bytes(raw,'little')
                    if key=='level':
                        label={1:'User',2:'Service Engineer',3:'Dealer',
                               4:'OEM Engineering',5:'Sevcon Engineering'}.get(value,'Unknown level')
                        shown=f'{value} — {label}  [raw: {raw.hex(" ").upper()}]'
                    elif key=='state':
                        label={0x05:'Operational',0x7F:'Pre-operational',
                               0x04:'Stopped'}.get(value,'Firmware-specific / unknown')
                        shown=f'0x{value:X} — {label}  [raw: {raw.hex(" ").upper()}]'
                    else:
                        # Do not claim a generic 5310 value is a particular
                        # security failure without firmware-specific mapping.
                        shown=f'{value} (0x{value:X}) — raw diagnostic reason'
                    self.events.put(('ui',lambda k=key,v=shown:self.security_values[k].set(v)))
                except Exception as exc:
                    self.events.put(('ui',lambda k=key,e=str(exc):self.security_values[k].set('Unavailable: '+e)))
        self.job(work)

    def open_eds(self):
        path=filedialog.askopenfilename(filetypes=[('CANopen EDS','*.eds'),('All files','*.*')])
        if not path:return
        try:entries=load_eds(path)
        except Exception as e:messagebox.showerror('EDS load failed',str(e));return
        self.eds_entries=entries;self.eds_file.set(path.split('/')[-1].split('\\')[-1]+f' — {len(entries)} entries')
        self.eds_last=None;self.eds_current.set('Value: not read');self.filter_eds()
        self.info(f'Loaded EDS: {path}; {len(entries)} entries (no controller values changed)')

    def filter_eds(self):
        if not hasattr(self,'eds_tree'):return
        needle=self.eds_query.get().strip().lower()
        self.eds_visible=[e for e in self.eds_entries if needle in f'{e.index:04x}:{e.subindex:02x} {e.name.lower()} {e.label.lower()}']
        self.eds_tree.delete(*self.eds_tree.get_children())
        for n,e in enumerate(self.eds_visible):
            self.eds_tree.insert('','end',iid=str(n),values=(f'{e.index:04X}:{e.subindex:02X}',e.name,e.label,e.access,e.default))
        self.eds_last=None;self.eds_current.set('Value: not read')

    def eds_selected(self):
        sel=self.eds_tree.selection()
        return self.eds_visible[int(sel[0])] if sel else None

    def eds_select(self,event=None):
        e=self.eds_selected();self.eds_last=None;self.eds_current.set('Value: not read')
        if e:self.eds_details.set(f'{e.name} — {e.index:04X}:{e.subindex:02X}  •  {e.label}  •  access={e.access}  •  min={e.low or "unspecified"}  •  max={e.high or "unspecified"}')

    def eds_read(self):
        e=self.eds_selected()
        if not e:return
        if not e.readable:messagebox.showwarning('Not readable','EDS marks this object as write-only.');return
        if not e.expedited:messagebox.showwarning('Unsupported type','This version supports expedited integer and REAL32 SDO values only.');return
        def work():
            b=self.sdo(e.index,e.subindex)
            value=eds_decode(e,b)
            self.events.put(('ui',lambda:self.eds_show(e,b,value)))
        self.job(work)

    def eds_show(self,e,b,value):
        if self.eds_selected()!=e:return
        self.eds_last=(e,b)
        self.eds_current.set(f'Current: {value}    [raw: {b.hex(" ").upper()}]')
        self._add(e.name,e.index,e.subindex,b.hex(' ').upper(),value,b)

    def eds_write(self):
        e=self.eds_selected()
        if not e:return
        if not e.writable:messagebox.showwarning('Read-only','EDS does not permit writes to this object.');return
        if not e.readable:messagebox.showwarning('Write-only object','Write-only EDS objects are not supported because read-back verification is required.');return
        if not e.expedited:messagebox.showwarning('Unsupported type','Cannot safely write this EDS data type.');return
        try:raw=eds_encode(e,self.eds_new.get())
        except Exception as ex:messagebox.showerror('Invalid value',str(ex));return
        if self.eds_last is None or self.eds_last[0]!=e:
            messagebox.showwarning('Read first','Read this EDS object from the controller before applying a change.');return
        old=self.eds_last[1]
        if len(old)!=len(raw):messagebox.showerror('Length mismatch','EDS type and controller reply length differ. No write sent.');return
        if not messagebox.askyesno('Confirm controller change',f'{e.name}  [{e.index:04X}:{e.subindex:02X}]\nOld: {eds_decode(e,old)}\nNew: {eds_decode(e,raw)}\n\nSend this change to the live controller?'):return
        def work():
            current=self.sdo(e.index,e.subindex)
            if current!=old:raise RuntimeError('Controller value changed since read; read again.')
            self.sdo(e.index,e.subindex,raw)
            actual=self.sdo(e.index,e.subindex)
            self.events.put(('ui',lambda:self.eds_show(e,actual,eds_decode(e,actual))))
            if actual!=raw:raise RuntimeError('Read-back mismatch. Desired value NOT verified.')
            self.events.put(('ui',lambda:messagebox.showinfo('Verified','Read-back matches the requested value. Persistence after power cycle is not confirmed.')))
        self.job(work)

    def refresh_ports(self):
        ports=[p.device for p in list_ports.comports()];self.ports['values']=ports
        if ports and self.port.get() not in ports:self.port.set(ports[0])
    def info(self,s):self.log.insert('end',f'{datetime.datetime.now():%H:%M:%S}  {s}\n');self.log.see('end')
    def node_id(self):
        n=int(self.node.get(),0)
        if not 1<=n<=127:raise ValueError('CANopen node ID must be 1..127')
        return n
    def connect(self):
        if self.busy:
            messagebox.showwarning('Busy','Finish the active CANopen operation first.');return
        if self.dev:self.disconnect()
        try:
            nid=self.node_id()
            if not self.port.get().strip():raise ValueError('Select a CANdapter serial port')
            self.dev=Adapter(self.port.get(),self.rate.get(),self.events)
            self.session+=1;self.connected_node=nid;self.verified=False
            self._connection_controls(True)
            self.status.set('CANdapter open — controller unverified')
            self.info('CANdapter serial link opened. Controller communication NOT yet verified.')
            self.verify_controller()
        except Exception as e:
            if self.dev:self.dev.close()
            self.dev=None
            self._connection_controls(False)
            self.status.set('Disconnected')
            messagebox.showerror('Connection failed',str(e))

    def _connection_controls(self,connected):
        self.ports.configure(state='disabled' if connected else 'normal')
        self.rate_widget.configure(state='disabled' if connected else 'readonly')
        self.node_widget.configure(state='disabled' if connected else 'normal')

    def verify_controller(self):
        if not self.dev:
            messagebox.showwarning('Not connected','Connect the CANdapter first.');return
        def work():
            # A fresh indexed SDO reply establishes bidirectional communication.
            response=self.sdo(0x1018,1)
            if not response:raise ValueError('Empty controller identity response')
            self.events.put(('ui',lambda value=int.from_bytes(response,'little'):
                self._verified(value)))
        self.job(work)

    def _verified(self,vendor_id):
        if not self.dev:return
        self.verified=True
        self.status.set(f'Controller verified — node {self.connected_node}')
        self.info(f'CANopen controller SDO reply confirmed: identity vendor 0x{vendor_id:X}')

    def disconnect(self):
        if self.busy:
            messagebox.showwarning('Busy','Wait until the active CANopen request finishes.');return
        if self.dev:self.dev.close();self.dev=None
        self.session+=1;self.verified=False;self.connected_node=None
        self._connection_controls(False)
        self.last_read=None;self.eds_last=None
        for field in getattr(self,'security_values',{}).values():field.set('Not read')
        if hasattr(self,'auth_token'):self.auth_token.set('')
        if hasattr(self,'auth_result'):self.auth_result.set('Not attempted')
        self.status.set('Disconnected')
        self.val.set('Current value: not read')
        self.eds_current.set('Value: not read')
        self.info('Disconnected; cached values cleared')

    def pump(self):
        while True:
            try:e=self.events.get_nowait()
            except queue.Empty:break
            if e[0]=='error':
                self.info('ADAPTER ERROR '+e[1]);self.verified=False
                self.status.set('CANdapter error — check connection');continue
            if e[0]=='frame':
                _,cid,data=e
                if data and self.connected_node and cid==0x580+self.connected_node:self.info(f'RX {cid:03X} {data.hex(" ").upper()}')
            elif e[0]=='ui':e[1]()
        self.root.after(50,self.pump)
    def job(self,fn):
        if not self.dev:messagebox.showwarning('Not connected','Connect the CANdapter first.');return
        if self.busy:messagebox.showwarning('Busy','Complete the current CANopen request first.');return
        self.busy=True
        def wrapper():
            try:fn()
            except Exception as e:self.events.put(('ui',lambda e=e:self._job_error(e)))
            finally:self.events.put(('ui',lambda:setattr(self,'busy',False)))
        threading.Thread(target=wrapper,daemon=True).start()
    def _job_error(self,e):
        self.info('ERROR: '+str(e))
        if isinstance(e,(TimeoutError,ConnectionError)):
            self.verified=False
            self.status.set('CANdapter open — controller not responding')

    def sdo(self,index,sub,raw=None):
        """One SDO transaction at a time; validate response and segment toggles."""
        dev=self.dev
        nid=self.connected_node
        if nid is None or dev is None:raise ConnectionError('CANdapter disconnected')
        if not 0<=index<=65535 or not 0<=sub<=255:raise ValueError('Invalid SDO address')
        if raw is not None and len(raw) not in (1,2,4):raise ValueError('Write must be 1, 2 or 4 bytes')
        with dev.transaction_lock:
            if dev is not self.dev or not dev.active:raise ConnectionError('CANdapter connection changed')
            cooldown=self.last_sdo_timeout+2.6-time.monotonic()
            if cooldown>0:time.sleep(cooldown)
            while True:
                try:dev.frames.get_nowait()
                except queue.Empty:break
            address=bytes((index&255,index>>8,sub))
            frame=(bytes((0x40,))+address+bytes(4)) if raw is None else (
                bytes((0x23+(4-len(raw))*4,))+address+raw.ljust(4,bytes(1)))
            def reply(check_address,sent_at):
                deadline=time.monotonic()+2.5
                while time.monotonic()<deadline:
                    if dev is not self.dev or not dev.active or dev.failure:
                        raise ConnectionError('CANdapter disconnected during SDO')
                    try:ts,cid,r=dev.frames.get(timeout=min(.1,max(.01,deadline-time.monotonic())))
                    except queue.Empty:continue
                    if ts<sent_at or cid!=0x580+nid or len(r)!=8:continue
                    if check_address and r[1:4]!=address:continue
                    if r[0]==0x80:
                        if r[1:4]!=address:continue
                        raise RuntimeError(f'SDO abort 0x{int.from_bytes(r[4:8],"little"):08X} at {index:04X}:{sub:02X}')
                    return r
                self.last_sdo_timeout=time.monotonic()
                raise TimeoutError(f'No SDO response to {index:04X}:{sub:02X}; check bitrate and node ID')
            sent=time.monotonic();dev.send(0x600+nid,frame)
            r=reply(True,sent)
            if raw is not None:
                if r[0]!=0x60:raise ValueError('Unexpected write acknowledgement '+r.hex())
                return raw
            if r[0]&0xE0!=0x40:raise ValueError('Unexpected SDO upload response '+r.hex())
            if r[0]&0x02:
                length=4-((r[0]>>2)&3) if r[0]&1 else 4
                return r[4:4+length]
            declared=int.from_bytes(r[4:8],'little') if r[0]&1 else None
            if declared is not None and declared>4096:raise ValueError('SDO response exceeds 4096 bytes')
            parts=bytearray();toggle=0
            for _ in range(586):
                sent=time.monotonic()
                dev.send(0x600+nid,bytes((0x60|(toggle<<4),))+bytes(7))
                segment=reply(False,sent)
                if segment[0]&0xE0!=0 or ((segment[0]>>4)&1)!=toggle:
                    raise ValueError('Invalid segmented SDO response or toggle')
                last=bool(segment[0]&1)
                unused=(segment[0]>>1)&7 if last else 0
                parts.extend(segment[1:8-unused])
                if len(parts)>4096:raise ValueError('Segmented SDO exceeds 4096 bytes')
                if last:
                    if declared is not None and len(parts)!=declared:
                        raise ValueError('Segmented SDO size mismatch')
                    return bytes(parts)
                toggle^=1
            raise TimeoutError('Segmented SDO did not terminate')

    def _fault_selector_write(self,index,sub,value,lengths=(1,2,4)):
        """Select a Sevcon diagnostic entry, trying permitted expedited widths."""
        errors=[]
        for length in lengths:
            try:
                return self.sdo(index,sub,value.to_bytes(length,'little'))
            except (RuntimeError, ValueError) as exc:
                errors.append(f'{length}-byte: {exc}')
                # Only retry after rejected write; do not hide transport timeouts.
        raise RuntimeError('Fault selection failed: '+'; '.join(errors))

    def _fault_text(self,fault_id):
        # The controller itself supplies the description through 5610:02.
        self._fault_selector_write(0x5610,1,fault_id)
        raw=self.sdo(0x5610,2)
        return raw.split(bytes(1),1)[0].decode('utf-8','replace').strip() or '(empty fault description)'

    def _show_fault(self,priority,fault_id,description,raw):
        self.tree.insert('','end',values=(
            f'Active fault #{priority+1} [5300:03]',
            raw.hex(' ').upper(),
            f'0x{fault_id:X}: {description}',
            'FAULT'))
        self.samples.append((datetime.datetime.now().isoformat(),
            f'Active fault #{priority+1}', '5300:03',
            raw.hex(' ').upper(), f'0x{fault_id:X}: {description}'))
    def display(self,name,i,s,b):
        raw=b.hex(' ').upper();value=decode_value(i,s,b)
        self.events.put(('ui',lambda name=name,i=i,s=s,raw=raw,value=value,b=b:self._add(name,i,s,raw,value,b)))
    def _add(self,name,i,s,raw,value,b):
        self.tree.insert('','end',values=(f'{name} [{i:04X}:{s:02X}]',raw,value,'OK'))
        self.samples.append((datetime.datetime.now().isoformat(),name,f'{i:04X}:{s:02X}',raw,value))
        self.last_read=(i,s,b)
        if self.selected()==(i,s):self.val.set(f'Current: {value}     [raw: {raw}]')
    def read_all(self):
        self.tree.delete(*self.tree.get_children());self.samples.clear()
        def work():
            for name,i,s in OBJECTS:
                try:self.display(name,i,s,self.sdo(i,s))
                except Exception as e:self.events.put(('ui',lambda name=name,i=i,s=s,e=e:self.tree.insert('','end',values=(f'{name} [{i:04X}:{s:02X}]','','',str(e)))))
        self.job(work)
    def read_faults(self):
        def work():
            for name,i,s in [('Error register',0x1001,0),('Error history count',0x1003,0)]:
                try:self.display(name,i,s,self.sdo(i,s))
                except Exception as e:self.events.put(('ui',lambda e=e:self.info(str(e))))
            count_raw=self.sdo(0x5300,1)
            self.display('Sevcon active fault count',0x5300,1,count_raw)
            count=int.from_bytes(count_raw,'little')
            if count>64:raise ValueError(f'Implausible active fault count: {count}')
            if count==0:
                self.events.put(('ui',lambda:self.info('No active Sevcon faults reported')))
                return
            for position in range(count):
                try:
                    self._fault_selector_write(0x5300,2,position)
                    raw=self.sdo(0x5300,3)
                    fault_id=int.from_bytes(raw,'little')
                    try:description=self._fault_text(fault_id)
                    except Exception as e:
                        description=f'Description unavailable ({e})'
                    self.events.put(('ui',lambda p=position,i=fault_id,t=description,b=raw:
                        self._show_fault(p,i,t,b)))
                except Exception as e:
                    self.events.put(('ui',lambda p=position,e=e:
                        self.info(f'Fault {p} read failed: {e}')))
        self.job(work)
    def selected(self):return int(self.index.get().strip().removeprefix('0x'),16),int(self.sub.get().strip().removeprefix('0x'),16)
    def update_selected(self, event=None):
        name=self.setting_name.get()
        for (i,s),label in VOLTAGE_OBJECTS.items():
            if label==name:
                self.index.set(f'{i:04X}');self.sub.set(f'{s:02X}');break
        is_voltage=name!='Custom CANopen object (raw)'
        self.new_label.set('New voltage (V):' if is_voltage else 'New raw bytes (hex, 1, 2 or 4 bytes):')
        self.last_read=None
        self.val.set('Current value: not read')
        self.data.set('')
    def read_one(self):
        try:i,s=self.selected()
        except ValueError:messagebox.showerror('Invalid value','Enter hexadecimal index/subindex');return
        self.job(lambda:self.display('Manual read',i,s,self.sdo(i,s)))
    def write_one(self):
        try:
            i,s=self.selected()
            if self.setting_name.get()=='Custom CANopen object (raw)':
                raw=bytes.fromhex(self.data.get())
                if len(raw) not in (1,2,4):raise ValueError('Provide 1, 2 or 4 bytes')
            else:
                if (i,s) not in VOLTAGE_OBJECTS or VOLTAGE_OBJECTS[i,s]!=self.setting_name.get():
                    raise ValueError('Selected object does not match named setting')
                raw=volts_to_raw(i,s,self.data.get())
        except Exception as e:messagebox.showerror('Invalid value',str(e));return
        if self.last_read is None or self.last_read[:2]!=(i,s):
            messagebox.showwarning('Read first','Read this exact object before writing');return
        old=self.last_read[2]
        if len(old)!=len(raw):
            messagebox.showerror('Type mismatch',f'Controller returned {len(old)} bytes; proposed value is {len(raw)} bytes. No write sent.');return
        if not messagebox.askyesno('Confirm CANopen write',
                f'Object {i:04X}:{s:02X}\n'
                f'Old: {decode_value(i,s,old)}  [{old.hex(" ").upper()}]\n'
                f'New: {decode_value(i,s,raw)}  [{raw.hex(" ").upper()}]\n\n'
                'Write this value to the live motor controller?'):return
        def work():
            # Repeat the read immediately before writing to detect external changes.
            current=self.sdo(i,s)
            if current!=old:raise RuntimeError('Value changed since the last read. Read again before writing.')
            self.sdo(i,s,raw)
            b=self.sdo(i,s)
            self.display('Read-back after write',i,s,b)
            if b!=raw:self.events.put(('ui',lambda:self.info('WARNING: read-back mismatch')))
            else:self.events.put(('ui',lambda:messagebox.showinfo('Verified','Controller read-back matches the requested value. Persistence after a restart is not confirmed.')))
        self.job(work)
    def save_controller(self):
        """Explicit CANopen 0x1010:01 store request; never called on parameter writes."""
        if not self.dev or not self.verified:
            messagebox.showwarning('Controller not verified',
                'Connect and verify controller communication before saving.')
            return
        if self.busy:
            messagebox.showwarning('Busy', 'Finish the current CANopen operation first.')
            return
        if not messagebox.askyesno(
            'Save configuration to controller',
            'Request permanent storage of the controller configuration?\\n\\n'
            'CANopen 0x1010:01 receives the four-byte ASCII signature "save".\\n'
            'This may require authorization and Pre-operational mode.\\n'
            'The application will NOT automatically change controller state.\\n\\n'
            'Proceed with the store request?'):
            return
        def work():
            # 0x1010:01 accepts 0x65766173 as a little-endian UNSIGNED32.
            # A positive SDO acknowledgement confirms receipt, not verified
            # nonvolatile retention. Verification needs a later power cycle.
            self.sdo(0x1010, 1, b'save')
            self.events.put(('ui', lambda:self._save_acknowledged()))
        self.job(work)

    def _save_acknowledged(self):
        self.info('Store parameters 1010:01 acknowledged. Persistence NOT tested.')
        messagebox.showinfo(
            'Save request acknowledged',
            'The controller acknowledged the CANopen save request.\\n\\n'
            'Nonvolatile retention is not yet verified. Power-cycle safely '
            'and read back the settings to confirm persistence.')

    def export(self):
        if not self.samples:messagebox.showinfo('Nothing to export','Read some values first');return
        path=filedialog.asksaveasfilename(defaultextension='.csv',filetypes=[('CSV','*.csv')])
        if path:
            with open(path,'w',newline='') as f:
                w=csv.writer(f);w.writerow(['timestamp','label','object','raw_hex','decoded_value']);w.writerows(self.samples)
            self.info('Exported '+path)
    def quit(self):
        if self.dev:
            self.dev.close();self.dev=None
        self.root.destroy()

if __name__=='__main__':
    root=tk.Tk();GUI(root);root.mainloop()