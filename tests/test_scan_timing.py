"""Small checks for opt-in timing; scan results must remain identical."""
import json
import os
import sys
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sage.models import VolumeInfo
from sage.platform.windows import mft
from sage.services.scanner import scan_ntfs
from sage.services.scan_timing import write_scan_timings
from test_mft_parser import make_mft_record, pack_file_name, pack_nonresident_data


class ScanTimingTests(unittest.TestCase):
    def test_timing_option_survives_administrator_restart(self):
        from sage import __main__ as entry
        from sage.platform.windows import admin
        import ctypes
        shell = Mock()
        shell.ShellExecuteW.return_value = 33
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(sys, 'argv', [r'E:\Niket\Projects\SAGE\run_sage.py','--scan-timings','--mft-parser=c']), \
             patch.object(entry, '_log'), patch.object(entry,'is_admin',return_value=False), \
             patch.object(ctypes,'WinDLL',return_value=shell):
            os.environ.pop('SAGE_SCAN_TIMINGS',None)
            self.assertEqual(entry.main(),0)
            self.assertEqual(os.environ['SAGE_SCAN_TIMINGS'],'1')
            self.assertEqual(os.environ['SAGE_MFT_PARSER'],'c')
            arguments = shell.ShellExecuteW.call_args.args[3]
            self.assertIn('--scan-timings',arguments)
            self.assertIn('--mft-parser=c',arguments)

    def replay(self):
        root = (1 << 48) | 5
        chunks = [bytes(1024)] * 7
        chunks[5] = make_mft_record(pack_file_name('.', root), flags=3)
        chunks[6] = make_mft_record(pack_file_name('test.bin', root), pack_nonresident_data(100,4096))
        data = b''.join(chunks)
        geometry = SimpleNamespace(BytesPerSector=512, BytesPerCluster=1024,
            BytesPerFileRecordSegment=1024, MftValidDataLength=len(data), MftStartLcn=1)
        kernel = Mock()
        kernel.CreateFileW.return_value = 1
        with patch.dict(os.environ, {'SAGE_MFT_PARSER':'python'}), \
             patch('sage.services.scanner.get_volume_info', return_value=VolumeInfo('C:', '', 'NTFS')), \
             patch.object(mft, '_kernel32', return_value=kernel), \
             patch.object(mft, '_volume_data', return_value=geometry), \
             patch.object(mft, '_mft_data_runs', return_value=[(1,7)]), \
             patch.object(mft, 'apply_mft_fixup', return_value=True), \
             patch.object(mft, '_read_at', side_effect=[bytes(1024),data]):
            # A real callback object avoids Mock synthesizing optional attributes.
            class Reporter:
                def __call__(self, count): pass
            progress = Reporter()
            return scan_ntfs('C:', progress), progress

    def test_opt_in_preserves_data_and_records_stages(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA':folder}):
            with patch.dict(os.environ, {'SAGE_SCAN_TIMINGS':'0'}):
                expected, _ = self.replay()
                self.assertFalse((Path(folder)/'SAGE'/'scan-timing-last.json').exists())
            with patch.dict(os.environ, {'SAGE_SCAN_TIMINGS':'1'}):
                actual, progress = self.replay()
                report = json.loads((Path(folder)/'SAGE'/'scan-timing-last.json').read_text())
            self.assertEqual(actual.records, expected.records)
            self.assertEqual(actual.children, expected.children)
            self.assertEqual(actual.stats, expected.stats)
            self.assertEqual(actual.directory_sizes, expected.directory_sizes)
            self.assertEqual(report['backend'], 'python_mft')
            self.assertEqual(report['status'], 'scanner_complete')
            for key in ('mft_read_seconds','record_parsing_seconds','links_and_extensions_seconds',
                        'enumeration_seconds','hierarchy_and_counts_seconds','folder_sizes_seconds','scanner_total_seconds'):
                self.assertGreaterEqual(report[key], 0)
            self.assertEqual(progress.scan_timings['records'], len(actual.records))
            self.assertNotIn('test.bin', json.dumps(report))

    def test_logging_failure_is_nonfatal(self):
        with patch.dict(os.environ, {'SAGE_SCAN_TIMINGS':'1'}), \
             patch.object(Path, 'mkdir', side_effect=PermissionError('denied')):
            write_scan_timings({'status':'complete'})

    def test_failed_scan_replaces_old_success(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {'LOCALAPPDATA':folder,'SAGE_SCAN_TIMINGS':'1'}), \
             patch('sage.services.scanner.get_volume_info', return_value=VolumeInfo('C:', '', 'NTFS')), \
             patch('sage.services.scanner.enumerate_mft', side_effect=OSError('read failure')):
            write_scan_timings({'status':'complete'})
            with self.assertRaises(OSError):
                scan_ntfs('C:')
            report = json.loads((Path(folder)/'SAGE'/'scan-timing-last.json').read_text())
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['error_type'], 'OSError')

    def test_ui_timing_preserves_order_and_ignores_old_scan(self):
        from sage.ui import application
        result, _ = self.replay()
        noop = lambda *args, **kwargs: None
        order, callbacks = [], []
        report = {'backend':'python_mft'}
        app = SimpleNamespace(_generation=1, _scan_timings=(1,report),
            _scan_started_at=time.monotonic(), _assistant_busy=False,
            _assistant=SimpleNamespace(set_result=noop,scan_summary=lambda:'summary'),
            _changes_banner=SimpleNamespace(grid_remove=noop),
            tree_panel=SimpleNamespace(changes=None,show_result=lambda value:order.append('tree')),
            assistant_panel=SimpleNamespace(set_selected_item=noop,write=noop),
            _update_drive_info=noop,progress_bar=SimpleNamespace(stop=noop),
            scan_button=SimpleNamespace(configure=noop),
            _size_finished=lambda value:order.append('completion'),after_idle=callbacks.append)
        with patch.object(application,'ConversationAssistant'), patch.object(application,'write_scan_timings') as write:
            application.SageApplication._scan_finished(app,result)
            self.assertEqual(order,['tree','completion'])
            callbacks[0]()
            self.assertEqual(report['status'],'complete')
            self.assertGreaterEqual(report['scan_to_first_idle_seconds'],report['tree_insert_seconds'])
            write.reset_mock()
            app._generation=2
            callbacks[0]()
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
