"""Snapshot changes survive deletion, lazy expansion and failed operations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import tkinter as tk

from sage.models import FileRecord, ScanResult, VolumeInfo
from sage.services.scan_changes import ScanChanges, observe_paths
from sage.services.tool_registry import ToolRegistry
from sage.services.retrieval import ScanEvidence
from sage.ui.application import SageApplication
from sage.ui.tree_panel import TreePanel


def scan():
    return ScanResult(VolumeInfo('C:', '', 'NTFS'),
        {5: FileRecord(5, 5, '', 16), 6: FileRecord(6, 5, 'Temp', 16),
         7: FileRecord(7, 6, 'child', 16), 8: FileRecord(8, 7, 'data.txt', 0),
         9: FileRecord(9, 5, 'keep.txt', 0)}, {5: [6, 9], 6: [7], 7: [8]}, 5,
         sizes_complete=True)


class SnapshotTests(unittest.TestCase):
    def test_worker_reports_partial_real_deletion_without_a_scan(self):
        with tempfile.TemporaryDirectory(prefix='sage-worker-check-') as directory:
            paths = [str(Path(directory)/name) for name in ('gone.txt', 'kept.txt')]
            for path in paths:
                Path(path).write_text('test')
            def operation():
                Path(paths[0]).unlink()
                raise OSError('simulated failure after first deletion')
            app = SimpleNamespace(_file_operation_running=False, _result=None, _generation=1,
                _size_cancel=threading.Event(), scan_button=Mock(), drive_box=Mock(),
                progress_bar=Mock(), progress_phase=Mock(), progress_detail=Mock(), status_var=Mock(),
                _events=queue.SimpleQueue(), winfo_id=lambda: 0)
            SageApplication._run_file_operation(app, 'permanent delete', paths, operation=operation)
            event = app._events.get(timeout=3)
            self.assertEqual(event[:4], ('operation_done', 1, 'permanent delete', False))
            self.assertIsInstance(event[-1], OSError)
            state = ScanChanges()
            state.record(event[5], event[6])
            self.assertEqual(state.files, 1)
            self.assertTrue(Path(paths[1]).exists())
            self.assertFalse(app._size_cancel.is_set())

    def test_actual_removal_repeated_count_and_unknown_access(self):
        with tempfile.TemporaryDirectory(prefix='sage-delete-check-') as directory:
            path = Path(directory)/'only-test-file.txt'
            path.write_text('test')
            before = observe_paths([str(path)])
            path.unlink()
            after = observe_paths([str(path)])
            state = ScanChanges()
            state.record(before, after)
            state.record(before, after)
            self.assertEqual((state.files, state.folders), (1, 0))
            state.record({'C:\\denied': (1, 2, True)}, {'C:\\denied': None})
            self.assertFalse(state.is_removed('C:\\denied'))
            with patch('sage.services.scan_changes.os.lstat', side_effect=PermissionError):
                self.assertEqual(observe_paths(['C:\\denied']), {'C:\\denied': None})

    def test_partial_folder_and_recreated_target_do_not_fake_success(self):
        state = ScanChanges()
        state.record({'C:\\Temp': (1, 20, True)}, {'C:\\Temp': (1, 20, True)})
        self.assertFalse(state.is_removed('C:\\Temp\\locked.txt'))
        self.assertTrue(state.affected('C:\\Temp\\locked.txt'))
        state.record({'C:\\Temp\\gone.txt': (1, 21, False)}, {'C:\\Temp\\gone.txt': (1, 99, False)})
        self.assertTrue(state.is_removed('c:/temp/gone.txt'))
        self.assertFalse(state.is_removed('C:\\Temperature\\keep.txt'))
        self.assertEqual(state.files, 1)

    def test_large_batch_has_bounded_ai_context(self):
        changes = ScanChanges()
        before = {f'C:\\Temp\\file-{index}': (1, index+1, False) for index in range(100)}
        changes.record(before, {path: False for path in before})
        context = changes.context()
        self.assertEqual(len(context['removed_paths']), 16)
        self.assertTrue(context['paths_partial'])
        self.assertEqual(changes.files, 100)
        self.assertTrue(changes.is_removed('C:\\Temp\\file-99'))

    def test_removed_folder_excludes_old_cleanup_and_current_facts(self):
        result = scan()
        tools = ToolRegistry(ScanEvidence(result), None)
        state = tools.scan_changes = ScanChanges()
        state.record({'C:\\Temp': (1, 20, True)}, {'C:\\Temp': False})
        tools._cleanup_cache = [SimpleNamespace(path='C:\\Temp'), SimpleNamespace(path='C:\\other')]
        self.assertEqual([s.path for s in tools.cleanup_suggestions()], ['C:\\other'])
        self.assertIn('error', tools.call('inspect_path', {'path': 'C:\\Temp\\child'}))
        self.assertIn('error', tools.call('report_fact', {'field': 'size', 'path': 'C:\\'}))
        overview = tools.call('drive_overview', {})
        self.assertEqual(overview['scan_change_notice']['removed_paths'], ['C:\\Temp'])
        self.assertEqual(len(result.records), 5)

    def test_completion_never_rescans_even_with_partial_error(self):
        changes = ScanChanges()
        app = SimpleNamespace(_file_operation_running=True, scan_button=Mock(), drive_box=Mock(),
            progress_bar=Mock(), progress_phase=Mock(), progress_detail=Mock(),
            tree_panel=SimpleNamespace(changes=changes), _pending_size_completion=None,
            assistant_panel=Mock(), _publish_changes=Mock(), _start_scan=Mock())
        with patch('sage.ui.application.messagebox.showerror'):
            SageApplication._operation_finished(app, 'recycle', False, True,
                {'C:\\gone': (1, 2, False), 'C:\\locked': (1, 3, False)},
                {'C:\\gone': False, 'C:\\locked': (1, 3, False)}, error=OSError('locked'))
        app._start_scan.assert_not_called()
        app._publish_changes.assert_called_once()
        self.assertFalse(app._file_operation_running)
        self.assertEqual(changes.files, 1)
        self.assertFalse(changes.is_removed('C:\\locked'))

    def test_contents_only_keeps_parent_and_counts_direct_removals(self):
        changes = ScanChanges()
        app = SimpleNamespace(_file_operation_running=True, scan_button=Mock(), drive_box=Mock(),
            progress_bar=Mock(), progress_phase=Mock(), progress_detail=Mock(),
            tree_panel=SimpleNamespace(changes=changes), _pending_size_completion=None,
            assistant_panel=Mock(), _publish_changes=Mock())
        SageApplication._operation_finished(app, 'permanent delete', True, True,
            {'C:\\Temp\\gone': (1, 2, False)}, {'C:\\Temp\\gone': False}, ['C:\\Temp'])
        self.assertFalse(changes.is_removed('C:\\Temp'))
        self.assertTrue(changes.affected('C:\\Temp'))
        self.assertTrue(changes.is_removed('C:\\Temp\\gone'))


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = TreePanel(self.root)

    def tearDown(self):
        self.panel.destroy()
        self.root.destroy()

    def test_lazy_children_remain_marked_after_sort_and_refresh(self):
        panel = self.panel
        result = scan()
        panel.show_result(result)
        panel.changes.record({'C:\\Temp': (1, 2, True)}, {'C:\\Temp': False})
        panel.refresh_changes()
        panel._insert_children(6)
        panel._insert_children(7)
        panel.apply_sort('allocated_desc')
        panel.refresh_loaded_rows()
        for frn in (6, 7, 8):
            self.assertIn('[Removed]', panel.tree.item(f'frn:{frn}', 'text'))
            self.assertIn('removed', panel.tree.item(f'frn:{frn}', 'tags'))
        self.assertNotIn('[Removed]', panel.tree.item('frn:9', 'text'))
        self.assertEqual(str(panel.tree['selectmode']), 'extended')
        self.assertIn('↕', panel.tree.heading('logical', 'text'))
        self.assertIn('↓', panel.tree.heading('allocated', 'text'))
        panel.show_result(result)
        self.assertFalse(panel.changes.stale)
        self.assertNotIn('[Removed]', panel.tree.item('frn:6', 'text'))


class ApplicationTests(unittest.TestCase):
    def test_real_ui_retains_tree_and_displays_manual_refresh_notice(self):
        with patch.object(SageApplication, '_load_volumes'), \
             patch.object(SageApplication, '_size_finished'):
            app = SageApplication()
            app.withdraw()
            try:
                result = scan()
                app._scan_finished(result)
                with patch.object(app, '_start_scan') as rescan:
                    app._operation_finished('recycle', True, True,
                        {'C:\\Temp': (1, 20, True)}, {'C:\\Temp': False})
                    rescan.assert_not_called()
                app.update_idletasks()
                self.assertIs(app._result, result)
                self.assertIs(app.tree_panel.result, result)
                self.assertTrue(app._changes_banner.grid_info())
                self.assertIn('1 folder(s)', app._changes_var.get())
                self.assertIn('Refresh', app._changes_var.get())
                self.assertEqual(str(app.scan_button['state']), 'normal')
                self.assertIs(app._conversation.tools.scan_changes, app.tree_panel.changes)
            finally:
                for job in app.tk.splitlist(app.tk.call('after', 'info')):
                    app.after_cancel(job)
                app.destroy()


@unittest.skipUnless(os.name == 'nt', 'Windows Shell required')
class NativeMenuTests(unittest.TestCase):
    def test_real_shell_menu_contains_sage_and_routable_delete(self):
        helper = Path(__import__('sage').__file__).parent/'platform/windows/shell_menu.py'
        with tempfile.TemporaryDirectory(prefix='sage-menu-check-') as directory:
            path = Path(directory)/'sample.txt'
            path.write_text('test')
            command = subprocess.run([sys.executable, '-B', str(helper), '--probe'],
                input=json.dumps({'paths': [str(path)], 'x': 10, 'y': 10}),
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result = json.loads(command.stdout)
            self.assertIn('Ask SAGE', result['labels'])
            for verb in ('delete', 'copy', 'cut', 'rename', 'properties'):
                self.assertIn(verb, result['verbs'])
            self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
