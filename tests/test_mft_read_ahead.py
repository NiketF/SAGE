"""Bounded read-ahead must preserve read ordering, cancellation and handle lifetime."""
import ctypes
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sage.platform.windows import mft
from test_mft_fast import replay, snapshot, ROOT
from test_mft_parser import make_mft_record, pack_file_name, pack_nonresident_data


class ReadAheadTests(unittest.TestCase):
    def test_fragmented_runs_preserve_records_split_across_run_boundaries(self):
        data=snapshot({5:make_mft_record(pack_file_name('.',ROOT),flags=3),
            **{i:make_mft_record(pack_file_name(f'file{i}',ROOT),pack_nonresident_data(i,4096))
               for i in range(6,13)}})
        runs=[(100,5),(200,7),(300,len(data)//512-12)]
        spans=[];logical=0
        for lcn,count in runs:
            spans.append((lcn*512,logical,count*512));logical+=count*512
        def read(_kernel,_handle,offset,size,_sector):
            for physical,logical,length in spans:
                if physical<=offset<physical+length:
                    at=logical+offset-physical
                    return bytearray(data[at:at+min(size,physical+length-offset)])
            raise AssertionError('Read outside mapped run')
        snapshots=[]
        for mode in ('off','on'):
            kernel=Mock();kernel.CreateFileW.return_value=1
            geometry=SimpleNamespace(BytesPerSector=512,BytesPerCluster=512,
                BytesPerFileRecordSegment=1024,MftValidDataLength=len(data),MftStartLcn=100)
            with patch.dict(os.environ,{'SAGE_MFT_READ_AHEAD':mode,'SAGE_MFT_PARSER':'python'}), \
                 patch.object(mft,'MFT_BUFFER_SIZE',1536),patch.object(mft,'_kernel32',return_value=kernel), \
                 patch.object(mft,'_volume_data',return_value=geometry), \
                 patch.object(mft,'_mft_data_runs',return_value=runs),patch.object(mft,'_read_at',side_effect=read):
                snapshots.append(mft.enumerate_mft('C:'))
            kernel.CloseHandle.assert_called_once_with(1)
        self.assertEqual(snapshots[0],snapshots[1])
        self.assertEqual(len(snapshots[1][0]),8)

    def test_results_and_partial_record_carry_match_sequential_both_parsers(self):
        data=snapshot({5:make_mft_record(pack_file_name('.',ROOT),flags=3),
            **{i:make_mft_record(pack_file_name(f'file{i}',ROOT),pack_nonresident_data(i,4096))
               for i in range(6,50)}})
        for backend in ('python','c'):
            if backend=='c':
                import importlib.util
                if not importlib.util.find_spec('sage.platform.windows._mft_fast'):continue
            with patch.dict(os.environ,{'SAGE_MFT_READ_AHEAD':'off'}):
                expected=replay(data,backend,1536,writable=True)
            with patch.dict(os.environ,{'SAGE_MFT_READ_AHEAD':'on'}):
                actual=replay(data,backend,1536,writable=True)
            for field in ('records','children','stats','directory_sizes','skipped_records'):
                self.assertEqual(getattr(actual,field),getattr(expected,field),(backend,field))

    def test_next_read_starts_before_consumer_and_only_one_is_pending(self):
        calls=[]
        second=threading.Event()
        def read(_kernel,_handle,offset,size,_sector):
            calls.append((offset,size,threading.current_thread().name))
            if len(calls)==2:second.set()
            return bytearray(b'x'*size)
        with patch.object(mft,'MFT_BUFFER_SIZE',512),patch.object(mft,'_read_at',side_effect=read):
            with ThreadPoolExecutor(max_workers=1) as reader:
                chunks=mft._read_mft_run(None,1,0,2048,512,None,None,reader)
                self.assertEqual(len(next(chunks)),512)
                self.assertTrue(second.wait(1))
                self.assertEqual(len(calls),2)
                self.assertEqual(sum(map(len,chunks)),1536)
        self.assertEqual([(offset,size) for offset,size,_ in calls],[(0,512),(512,512),(1024,512),(1536,512)])
        self.assertNotEqual(calls[0][2],calls[1][2])

    def test_short_reads_and_empty_end_keep_exact_offsets(self):
        for threaded in (False,True):
            offsets=[]
            def read(_kernel,_handle,offset,size,_sector):
                offsets.append(offset)
                return bytearray(b'x'*min(256,size)) if offset<768 else bytearray()
            with ThreadPoolExecutor(max_workers=1) as reader, \
                 patch.object(mft,'MFT_BUFFER_SIZE',512),patch.object(mft,'_read_at',side_effect=read):
                chunks=list(mft._read_mft_run(None,1,0,1024,512,None,None,reader if threaded else None))
            self.assertEqual(list(map(len,chunks)),[256,256,256])
            self.assertEqual(offsets,[0,256,512,768])

    def test_cancelled_scan_joins_reader_before_closing_volume(self):
        reading=threading.Event();finished=threading.Event()
        data=bytes(4096)
        kernel=Mock();kernel.CreateFileW.return_value=1
        geometry=SimpleNamespace(BytesPerSector=512,BytesPerCluster=1024,
            BytesPerFileRecordSegment=1024,MftValidDataLength=8192,MftStartLcn=1)
        calls=[]
        def read(*args):
            calls.append(args)
            if len(calls)==3:
                reading.set();time.sleep(.02);finished.set()
            return data if len(calls)>1 else bytes(1024)
        kernel.CloseHandle.side_effect=lambda _:self.assertTrue(finished.is_set())
        with patch.dict(os.environ,{'SAGE_MFT_READ_AHEAD':'on','SAGE_MFT_PARSER':'python'}), \
             patch.object(mft,'MFT_BUFFER_SIZE',4096),patch.object(mft,'_kernel32',return_value=kernel), \
             patch.object(mft,'_volume_data',return_value=geometry), \
             patch.object(mft,'_mft_data_runs',return_value=[(1,8)]),patch.object(mft,'_read_at',side_effect=read):
            def cancel():
                return reading.is_set()
            with self.assertRaises(InterruptedError):mft.enumerate_mft('C:',cancelled=cancel)
        kernel.CloseHandle.assert_called_once_with(1)

    def test_worker_read_failure_reaches_caller(self):
        with ThreadPoolExecutor(max_workers=1) as reader, \
             patch.object(mft,'MFT_BUFFER_SIZE',512), \
             patch.object(mft,'_read_at',side_effect=[bytearray(512),OSError('read failed')]):
            chunks=mft._read_mft_run(None,1,0,1024,512,None,None,reader)
            self.assertEqual(len(next(chunks)),512)
            with self.assertRaisesRegex(OSError,'read failed'):next(chunks)

    def test_option_survives_administrator_restart(self):
        from sage import __main__ as entry
        shell=Mock();shell.ShellExecuteW.return_value=33
        with patch.dict(os.environ,{},clear=False), \
             patch.object(sys,'argv',[r'E:\Niket\Projects\SAGE\run_sage.py','--mft-read-ahead=off']), \
             patch.object(entry,'_log'),patch.object(entry,'is_admin',return_value=False), \
             patch.object(ctypes,'WinDLL',return_value=shell):
            os.environ.pop('SAGE_MFT_READ_AHEAD',None)
            self.assertEqual(entry.main(),0)
            self.assertEqual(os.environ['SAGE_MFT_READ_AHEAD'],'off')
            self.assertIn('--mft-read-ahead=off',shell.ShellExecuteW.call_args.args[3])


if __name__=='__main__':unittest.main()
