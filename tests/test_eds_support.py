import tempfile
import unittest
from pathlib import Path
from eds_support import load_eds, encode, decode

SAMPLE = '''[FileInfo]
FileName=sample.eds
[2000]
ParameterName=Motor threshold
ObjectType=0x7
DataType=0x0006
AccessType=rw
LowLimit=0
HighLimit=2000
DefaultValue=100
[2001]
ParameterName=Drive record
ObjectType=0x9
SubNumber=1
[2001sub0]
ParameterName=Number of entries
DataType=0x0005
AccessType=ro
[2001sub1]
ParameterName=Control mode
DataType=0x0002
AccessType=rw
LowLimit=-10
HighLimit=10
'''
class Tests(unittest.TestCase):
    def test_parse_encode_decode(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'test.eds';p.write_text(SAMPLE)
            a=load_eds(p)
        self.assertEqual([(x.index,x.subindex) for x in a],[(0x2000,0),(0x2001,0),(0x2001,1)])
        self.assertEqual(encode(a[0],'120'),b'\x78\x00')
        self.assertEqual(decode(a[0],b'\x78\x00'),'120')
        self.assertEqual(encode(a[2],'-2'),b'\xfe')
        with self.assertRaises(ValueError):encode(a[0],'2001')
        with self.assertRaises(ValueError):encode(a[2],'-11')
if __name__=='__main__':unittest.main()