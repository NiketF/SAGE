import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sage.ui.application import SageApplication


class PropertiesRoutingTests(unittest.TestCase):
    def app(self):
        names = ('_on_tree_ask_sage', '_reveal_selection', '_copy_path', '_copy_to',
                 '_move_to', '_paste_selection', '_rename_selection', '_recycle_selection',
                 '_copy_selection', '_cut_selection', '_properties_selection')
        app = SimpleNamespace(**{name: Mock() for name in names})
        app._file_operation_running = True
        app.scan_button = Mock()
        app.drive_box = Mock()
        app.status_var = Mock()
        app._pending_size_completion = None
        app._selected_paths = lambda: ['C:\\sample.txt']
        return app

    def test_properties_is_dispatched_after_menu_helper_finishes(self):
        app = self.app()
        SageApplication._native_menu_finished(app, ['C:\\sample.txt'], {},
                                             {'action': 'properties'}, {}, {})
        self.assertFalse(app._file_operation_running)
        app._properties_selection.assert_called_once_with()

    def test_changed_selection_does_not_open_wrong_properties(self):
        app = self.app()
        app._selected_paths = lambda: ['C:\\other.txt']
        SageApplication._native_menu_finished(app, ['C:\\sample.txt'], {},
                                             {'action': 'properties'}, {}, {})
        app._properties_selection.assert_not_called()


class CompactMenuTests(unittest.TestCase):
    def test_right_click_uses_compact_menu_without_launching_native_helper(self):
        app = SimpleNamespace(tree_panel=SimpleNamespace(tree=Mock()),
            _selected_paths=lambda: ['C:\\sample.txt'], _file_operation_running=False,
            _show_sage_menu=Mock(), _show_native_menu=Mock())
        tree = app.tree_panel.tree
        tree.identify_row.return_value = 'frn:7'
        tree.selection.return_value = ('frn:7',)
        event = SimpleNamespace(y=20)
        SageApplication._show_tree_menu(app, event)
        app._show_sage_menu.assert_called_once_with(event, ['C:\\sample.txt'], True)
        app._show_native_menu.assert_not_called()

    def test_compact_menu_keeps_actions_and_native_menu_is_explicit(self):
        names = ('_on_tree_ask_sage', '_open_selection', '_reveal_selection', '_copy_path',
                 '_copy_selection', '_cut_selection', '_paste_selection', '_copy_to',
                 '_move_to', '_rename_selection', '_recycle_selection', '_properties_selection',
                 '_show_native_menu')
        app = SimpleNamespace(**{name: Mock() for name in names})
        app._tree_menu = Mock()
        app._clipboard_paths = []
        app._file_operation_running = False
        event = SimpleNamespace(x_root=10, y_root=20)
        paths = ['C:\\sample.txt']
        SageApplication._show_sage_menu(app, event, paths, True)
        commands = {call.kwargs['label']:call.kwargs for call in app._tree_menu.add_command.call_args_list}
        for label in ('Ask SAGE','Open','Copy','Cut','Paste','Rename…','Send to Recycle Bin…','Properties','More Windows options'):
            self.assertIn(label,commands)
        commands['Properties']['command']()
        app._properties_selection.assert_called_once()
        app._show_native_menu.assert_not_called()
        commands['More Windows options']['command']()
        app._show_native_menu.assert_called_once_with(paths,event)
        app._tree_menu.grab_release.assert_called_once()


if __name__ == '__main__':
    unittest.main()
