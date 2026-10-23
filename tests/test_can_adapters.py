"""Protocol-level tests for the shared GVRET backend (no hardware needed)."""
import unittest
from can_adapters import GVRETParser, GVRETAdapter

class TestGVRET(unittest.TestCase):
    def test_validation_and_frame(self):
        frames=[];valid=[];bus=[]
        parser=GVRETParser(lambda *x:frames.append(x),lambda:valid.append(True),
                            lambda b:bus.append(b))
        # Validator response has no payload, followed by a CAN0 8-byte SDO reply.
        frame=b'\xf1\x00'+bytes(4)+(0x581).to_bytes(4,'little')+b'\x08'+bytes.fromhex('4300180101020304')
        for piece in [b'\xe7\xf1',b'\x09',frame[:7],frame[7:]]:
            parser.feed(piece)
        self.assertEqual(valid,[True])
        self.assertEqual(frames,[(0,0x581,bytes.fromhex('4300180101020304'))])

    def test_filters_extended_frames(self):
        found=[]
        p=GVRETParser(lambda *f:found.append(f),lambda:None,lambda _:None)
        p.feed(b'\xf1\x00'+bytes(4)+(0x80000581).to_bytes(4,'little')+b'\x08'+bytes(8))
        self.assertEqual(found,[])

    def test_send_encoding(self):
        dev=GVRETAdapter.__new__(GVRETAdapter)
        dev.bus=1
        written=[]
        dev._write=written.append
        dev.send(0x601,bytes.fromhex('4018100100000000'))
        self.assertEqual(written,[b'\xf1\x00'+(0x601).to_bytes(4,'little')+
                                  b'\x01\x08'+bytes.fromhex('4018100100000000')+b'\x00'])

if __name__=='__main__':
    unittest.main()
