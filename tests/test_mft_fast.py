"""Compare both parsers on identical protected records, without drive access."""
import importlib.util
import os
import random
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sage.models import FileRecord, VolumeInfo
from sage.platform.windows import mft
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from test_mft_parser import (make_mft_record, pack_file_name, pack_nonresident_data,
    pack_resident_data, pack_sparse_nonresident_data, pack_compressed_nonresident_data, pack_resident_attr)
from test_scan_assessment import named_data

ROOT=(1<<48)|5

def protected(record):
    data=bytearray(record)
    struct.pack_into('<HH',data,4,0x30,3)
    data[0x30:0x32]=b'\xab\xcd'
    for i in (1,2):
        data[0x30+i*2:0x32+i*2]=data[i*512-2:i*512]
        data[i*512-2:i*512]=b'\xab\xcd'
    return bytes(data)

def snapshot(entries):
    records=[bytes(1024)]*(max(entries)+1)
    for i,record in entries.items(): records[i]=protected(record)
    return b''.join(records)

def replay(data,backend,chunk_size=4*1024*1024,progress=None,writable=False):
    geometry=SimpleNamespace(BytesPerSector=512,BytesPerCluster=1024,
        BytesPerFileRecordSegment=1024,MftValidDataLength=len(data),MftStartLcn=1)
    kernel=Mock();kernel.CreateFileW.return_value=1
    blocks=[data[i:i+chunk_size] for i in range(0,len(data),chunk_size)]
    if writable:
        blocks=[bytearray(block) for block in blocks]
    with patch.dict(os.environ,{'SAGE_MFT_PARSER':backend}), \
         patch.object(mft,'MFT_BUFFER_SIZE',chunk_size), \
         patch.object(mft,'_kernel32',return_value=kernel), \
         patch.object(mft,'_volume_data',return_value=geometry), \
         patch.object(mft,'_mft_data_runs',return_value=[(1,len(data)//1024)]), \
         patch.object(mft,'_read_at',side_effect=[bytes(1024),*blocks]):
        records,skipped=mft.enumerate_mft('C:',progress=progress)
    result=build_scan_result(VolumeInfo('C:','','NTFS'),records,skipped)
    calculate_directory_sizes(result)
    return result

@unittest.skipUnless(importlib.util.find_spec('sage.platform.windows._mft_fast'), 'Build the optional C parser first')
class CompiledParserTests(unittest.TestCase):
    def test_name_ranking_preserves_dos_only_and_namespace_order(self):
        entries = {5: make_mft_record(pack_file_name('.',ROOT),flags=3)}
        for i, namespaces in enumerate(((2,), (2,1), (1,2), (2,2), (0,2,1,3), (3,1,0,2)), 6):
            names=[pack_file_name(f'name{n}-{j}', ROOT+j, ns=n) for j,n in enumerate(namespaces)]
            entries[i]=make_mft_record(*names,pack_nonresident_data(1000,4096))
        invalid=bytearray(pack_file_name('ab',ROOT))
        invalid[0x18+0x42:0x18+0x44]=b'\x00\xd8'
        entries[20]=make_mft_record(bytes(invalid),pack_file_name('DOS~1',ROOT,ns=2))
        self.compare(snapshot(entries),4096)

    def test_repeated_metadata_remains_valid_across_batches(self):
        standard=bytearray(0x30)
        struct.pack_into('<Q',standard,8,116444736000000000+1700000000*10000000)
        entries={5:make_mft_record(pack_file_name('.',ROOT),flags=3)}
        for i in range(6,50):
            entries[i]=make_mft_record(pack_resident_attr(0x10,standard),
                pack_file_name(f'file{i}',ROOT),pack_nonresident_data(i,4096))
        self.compare(snapshot(entries),4096)

    def compare(self,data,chunk_size=4*1024*1024):
        expected=replay(data,'python',chunk_size)
        actual=replay(data,'c',chunk_size)
        for field in ('records','children','root_frn','stats','directory_sizes','skipped_records'):
            self.assertEqual(getattr(actual,field),getattr(expected,field),field)
        return actual

    def test_streams_extensions_links_names_and_attributes(self):
        standard=bytearray(0x30)
        struct.pack_into('<Q',standard,8,116444736000000000+1230000000)
        struct.pack_into('<I',standard,0x20,0x401)
        self.compare(snapshot({
            5:make_mft_record(pack_file_name('.',ROOT),flags=3),
            6:make_mft_record(pack_file_name('Docs',ROOT),named_data('$I30',8192,8192,0xa0),flags=3),
            7:make_mft_record(pack_file_name('one.bin',ROOT),pack_resident_attr(0x20,b''),pack_nonresident_data(100,4096)),
            8:make_mft_record(pack_file_name('two.bin',(1<<48)|6),named_data('extra',25,8192),base=(1<<48)|7),
            9:make_mft_record(pack_file_name('sparse',ROOT),pack_sparse_nonresident_data(512*1024**3,0)),
            10:make_mft_record(pack_file_name('compressed',ROOT),pack_compressed_nonresident_data(10000,4096,12288)),
            11:make_mft_record(pack_file_name('文書.txt',ROOT),pack_resident_data(12)),
            12:make_mft_record(pack_file_name('backed',ROOT),pack_resident_attr(0x10,standard),
                pack_resident_attr(0xc0,struct.pack('<I',0x80000017)),pack_nonresident_data(8000,0)),
            13:make_mft_record(pack_file_name('SHORT~1.TXT',ROOT,ns=2),pack_file_name('long name.txt',ROOT),
                pack_file_name('alias.txt',(1<<48)|6,ns=0),pack_nonresident_data(44,4096)),
            14:make_mft_record(pack_file_name('deleted',ROOT),flags=0),
            15:make_mft_record(pack_file_name('stale',ROOT),base=(3<<48)|7),
            16:make_mft_record(pack_file_name('junction',ROOT),pack_resident_attr(0x10,standard),
                pack_resident_attr(0xc0,struct.pack('<I',0xa0000003))),
        }))

    def test_batch_boundaries_and_unsigned_references(self):
        root=(65535<<48)|5
        data=snapshot({5:make_mft_record(pack_file_name('.',root),flags=3,sequence=65535),
            6:make_mft_record(pack_file_name('file',root),pack_nonresident_data(10,4096),sequence=65535)})
        self.compare(data,1536)

    def test_varied_tree_and_cross_block_extensions(self):
        rng=random.Random(72)
        entries={5:make_mft_record(pack_file_name('.',ROOT),flags=3)}
        for i in range(6,40):
            entries[i]=make_mft_record(pack_file_name(f'dir{i}',ROOT),flags=3)
        for i in range(40,500):
            names=[pack_file_name(f'file{i}',(1<<48)|rng.randrange(5,40))]
            if i%7==0:names.append(pack_file_name(f'alias{i}',ROOT))
            entries[i]=make_mft_record(*names,pack_nonresident_data(rng.randrange(100000),4096))
        self.compare(snapshot(entries),4096)

    def test_invalid_fixup_and_attribute_are_counted(self):
        data=bytearray(snapshot({5:make_mft_record(pack_file_name('.',ROOT),flags=3),
            6:make_mft_record(pack_file_name('bad',ROOT))}))
        data[6*1024+510]^=1
        self.compare(bytes(data))
        record=make_mft_record(pack_file_name('bad',ROOT))
        struct.pack_into('<I',record,0x3c,15)
        self.compare(snapshot({5:make_mft_record(pack_file_name('.',ROOT),flags=3),6:record}))

    def test_batch_validation_and_constructor_failure(self):
        from sage.platform.windows import _mft_fast
        with self.assertRaises(ValueError):
            _mft_fast.parse_batch(bytearray(1024),0,0,{},{},{},FileRecord,{})
        with self.assertRaises((BufferError,TypeError)):
            _mft_fast.parse_batch(bytes(1024),1024,0,{},{},{},FileRecord,{})
        block=bytearray(protected(make_mft_record(pack_file_name('file',ROOT))))
        def failed(*args):raise RuntimeError('constructor failed')
        with self.assertRaisesRegex(RuntimeError,'constructor failed'):
            _mft_fast.parse_batch(block,1024,0,{},{},{},failed,{})
        block.extend(b'x') # Buffer must be released even after an exception.

    def test_malformed_headers_do_not_crash(self):
        from sage.platform.windows import _mft_fast
        rng=random.Random(19)
        for _ in range(200):
            block=bytearray(protected(make_mft_record(pack_file_name('file',ROOT))))
            at=rng.randrange(0x38,300)
            block[at:at+4]=rng.randbytes(4)
            _mft_fast.parse_batch(block,1024,0,{},{},{},FileRecord,{})

    def test_lazy_details_preserve_duplicate_streams_and_names(self):
        self.compare(snapshot({
            5:make_mft_record(pack_file_name('.',ROOT),flags=3),
            6:make_mft_record(pack_file_name('same',ROOT),pack_file_name('same',ROOT,ns=3),
                pack_nonresident_data(100,4096),pack_nonresident_data(200,8192)),
            7:make_mft_record(pack_file_name('linked',ROOT),pack_file_name('other',ROOT),
                named_data('extra',10,4096),named_data('extra',20,8192),pack_nonresident_data(30,4096)),
            8:make_mft_record(pack_file_name('listed',ROOT),pack_resident_attr(0x20,b'')),
            9:make_mft_record(named_data('extra',2,4096),base=(1<<48)|8),
            10:make_mft_record(pack_file_name('DOS~1',ROOT,ns=2),pack_resident_attr(0x20,b''),pack_resident_data(12)),
        }))

    def test_lazy_stream_sum_keeps_python_integer_precision(self):
        huge=(1<<63)-1
        self.compare(snapshot({
            5:make_mft_record(pack_file_name('.',ROOT),flags=3),
            6:make_mft_record(pack_file_name('huge',ROOT),pack_nonresident_data(huge,huge),
                named_data('a',huge,huge),named_data('b',huge,huge)),
        }))

    def test_owned_read_buffers_with_partial_records(self):
        data=snapshot({5:make_mft_record(pack_file_name('.',ROOT),flags=3),
            6:make_mft_record(pack_file_name('data',ROOT),pack_nonresident_data(10,4096)),
            7:make_mft_record(pack_file_name('alias',ROOT),pack_nonresident_data(20,8192))})
        expected=replay(data,'python',1536)
        for backend in ('python','c'):
            actual=replay(data,backend,1536,writable=True)
            for field in ('records','children','stats','directory_sizes','skipped_records'):
                self.assertEqual(getattr(actual,field),getattr(expected,field))

if __name__=='__main__':unittest.main()
