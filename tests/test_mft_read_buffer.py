"""Verify writable raw-read buffers, alignment, short reads and buffer lifetime."""
import ctypes
from ctypes import wintypes
from types import SimpleNamespace
import unittest

from sage.platform.windows.mft import _read_at

class ReadBufferTests(unittest.TestCase):
    def kernel(self,source):
        state={'offset':0,'reads':0}
        def seek(handle,offset,out,method):state['offset']=offset;return 1
        def read(handle,buffer,size,count,overlapped):
            data=source[state['offset']:state['offset']+size]
            ctypes.memmove(buffer,data,len(data))
            ctypes.cast(count,ctypes.POINTER(wintypes.DWORD)).contents.value=len(data)
            state['reads']+=1
            return 1
        return SimpleNamespace(SetFilePointerEx=seek,ReadFile=read),state

    def test_aligned_read_is_writable_and_releases_export(self):
        data=bytes(range(256))*4
        kernel,state=self.kernel(data)
        result=_read_at(kernel,1,0,1024,512)
        self.assertIsInstance(result,bytearray)
        self.assertEqual(result,data)
        result.extend(b'x')
        self.assertEqual(state['reads'],1)

    def test_unaligned_and_short_reads_match_requested_bytes(self):
        data=bytes(range(256))*4
        for offset,size in ((7,500),(510,30),(1000,100),(1024,100),(1200,100)):
            kernel,state=self.kernel(data)
            result=_read_at(kernel,1,offset,size,512)
            self.assertEqual(result,data[offset:offset+size])
            self.assertEqual(state['offset'],offset-offset%512)

    def test_zero_size_does_not_read(self):
        kernel,state=self.kernel(b'abc')
        self.assertEqual(_read_at(kernel,1,0,0,512),b'')
        self.assertEqual(state['reads'],0)

if __name__=='__main__':unittest.main()
