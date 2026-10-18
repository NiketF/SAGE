import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sage.models import FileRecord, ScanResult, VolumeInfo
from sage.ui.application import SageApplication
from sage.ui.assistant_panel import AssistantPanel
from sage.ui.floating_button import FloatingAskButton
from sage.ui.tree_panel import TreePanel


class FloatingAskButtonTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def test_button_show_hide_lifecycle(self):
        clicked = []
        btn = FloatingAskButton(self.root, command=lambda: clicked.append(True), text="Ask SAGE")
        self.assertFalse(btn.is_visible())
        btn.show(20, 30)
        self.assertTrue(btn.is_visible())
        btn.invoke()
        self.assertFalse(btn.is_visible())
        self.assertEqual(clicked, [True])

    def test_button_hover_and_press_states(self):
        btn = FloatingAskButton(self.root, text="Ask SAGE")
        # Enter
        btn._on_enter(tk.Event())
        self.assertEqual(btn._state, "hover")
        # Press
        btn._on_press(tk.Event())
        # Leave
        btn._on_leave(tk.Event())
        self.assertEqual(btn._state, "normal")


class AssistantPanelHoverTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.asked = []
        self.panel = AssistantPanel(self.root, lambda question, quote: self.asked.append((question, quote)))
        self.panel.pack(fill="both", expand=True)
        self.root.update_idletasks()

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def test_selection_shows_hover_button_and_only_click_adds_context(self):
        self.panel.write("SAGE", "You can inspect C:\\Program Files safely.")
        start = self.panel.transcript.search("C:\\Program Files", "1.0")
        self.panel.transcript.tag_add("sel", start, f"{start}+16c")

        # Before triggering selection check: quote is empty
        self.assertEqual(self.panel._quote, "")

        # Trigger selection check
        self.panel._check_transcript_selection()
        self.root.update()

        # Hover button should be visible
        self.assertTrue(self.panel._hover_button.is_visible())
        # But quote context should STILL NOT be added!
        self.assertEqual(self.panel._quote, "")

        # When the user clicks / invokes the hover button:
        self.panel._hover_button.invoke()
        self.assertFalse(self.panel._hover_button.is_visible())
        self.assertEqual(self.panel._quote, "C:\\Program Files")
        self.assertIn("C:\\Program Files", self.panel.quote_text.get())

    def test_selected_item_context_has_clear_button(self):
        self.panel.set_selected_item("C:\\Program Files")
        self.assertEqual(self.panel.get_selected_item(), "C:\\Program Files")
        self.assertIn("C:\\Program Files", self.panel.selected_text.get())

        # Clicking clear removes it
        self.panel.clear_selected_item()
        self.assertIsNone(self.panel.get_selected_item())


class TreePanelHoverTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.root.geometry("600x400")

        records = {
            5: FileRecord(frn=5, parent_frn=5, name="C:", attributes=0x10),
            10: FileRecord(frn=10, parent_frn=5, name="Program Files", attributes=0x10),
            20: FileRecord(frn=20, parent_frn=10, name="app.exe", attributes=0),
        }
        self.result = ScanResult(
            volume=VolumeInfo("C:", "Acer", "NTFS"),
            records=records,
            children={5: [10], 10: [20]},
            root_frn=5,
        )
        self.asked_path = []
        self.panel = TreePanel(self.root, on_ask_sage=self.asked_path.append)
        self.panel.pack(fill="both", expand=True)
        self.panel.show_result(self.result)
        self.root.update_idletasks()

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def test_selecting_tree_item_shows_hover_button_and_only_click_adds_context(self):
        item_iid = "frn:10"
        self.panel.tree.selection_set(item_iid)
        self.panel.tree.focus(item_iid)

        # Before clicking Ask SAGE, on_ask_sage has NOT been called
        self.assertEqual(self.asked_path, [])

        self.panel._update_hover_button()
        self.root.update()

        # Hover button is shown
        self.assertTrue(self.panel._hover_button.is_visible())
        self.assertEqual(self.asked_path, [])

        # Invoking the hover button sends the folder path
        self.panel._hover_button.invoke()
        self.assertFalse(self.panel._hover_button.is_visible())
        self.assertEqual(self.asked_path, ["C:\\Program Files"])

    def test_clearing_or_sorting_hides_hover_button(self):
        item_iid = "frn:10"
        self.panel.tree.selection_set(item_iid)
        self.panel._update_hover_button()
        self.panel.clear()
        self.assertFalse(self.panel._hover_button.is_visible())


class ApplicationHoverIntegrationTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def test_application_tree_and_answer_context_flow(self):
        records = {
            5: FileRecord(frn=5, parent_frn=5, name="C:", attributes=0x10),
            10: FileRecord(frn=10, parent_frn=5, name="Program Files", attributes=0x10),
        }
        result = ScanResult(
            volume=VolumeInfo("C:", "Acer", "NTFS"),
            records=records,
            children={5: [10]},
            root_frn=5,
        )

        app = SageApplication.__new__(SageApplication)
        app._result = result
        app.assistant_panel = AssistantPanel(self.root, lambda q, quote: None)
        app.tree_panel = TreePanel(self.root, on_ask_sage=app._on_tree_ask_sage)
        app.tree_panel.show_result(result)

        # 1. User selects item in tree
        app.tree_panel.tree.selection_set("frn:10")
        app._on_tree_selected()
        # Item context is NOT set yet!
        self.assertIsNone(app.assistant_panel.get_selected_item())

        # 2. User clicks "Ask SAGE" hover button on the tree
        app._on_tree_ask_sage("C:\\Program Files")
        # Now context is set!
        self.assertEqual(app.assistant_panel.get_selected_item(), "C:\\Program Files")

        # 3. Context retrieved for answer
        active_item = app.assistant_panel.get_selected_item()
        selected_paths = (active_item,) if active_item else ()
        self.assertEqual(selected_paths, ("C:\\Program Files",))

        # 4. User clears the context
        app.assistant_panel.clear_selected_item()
        self.assertIsNone(app.assistant_panel.get_selected_item())
        active_item = app.assistant_panel.get_selected_item()
        selected_paths = (active_item,) if active_item else ()
        self.assertEqual(selected_paths, ())


if __name__ == "__main__":
    unittest.main()
