import tkinter as tk
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sage.ui.assistant_panel import AssistantPanel
from sage.ui.markdown import markdown_spans
from sage.ui.application import SageApplication


class MarkdownTests(unittest.TestCase):
    def test_bold_code_bullets_and_windows_paths(self):
        spans = markdown_spans("**Total Size:** **354 MB**\n* Folder: `F:\\Git`\n")
        text = "".join(value for value, _tags in spans)
        self.assertEqual(text, "Total Size: 354 MB\n• Folder: F:\\Git\n")
        self.assertIn(("354 MB", ("bold",)), spans)
        self.assertIn(("F:\\Git", ("code",)), spans)

    def test_partial_bold_does_not_expose_delimiters(self):
        self.assertEqual(markdown_spans("**Total", partial=True), [("Total", ("bold",))])
        self.assertEqual(markdown_spans("**Total"), [("Total", ("bold",))])


class AssistantPanelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.panel = AssistantPanel(self.root, lambda _question: None)

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def test_stream_formatting_preserves_other_scan_messages(self):
        self.panel.begin_reply()
        self.assertEqual(str(self.panel.ask_button["state"]), "disabled")
        self.panel.set_status("Writing the answer…")
        self.panel.append_reply("**Total")
        self.panel.after_cancel(self.panel._render_job)
        self.panel._render_reply()
        self.panel.write("SAGE", "Size analysis complete.")
        self.panel.append_reply(" Size:** **354 MB** in `F:\\Git`.")
        self.panel.finish_reply("**Total Size:** **354 MB** in `F:\\Git`.")
        text = self.panel.transcript.get("1.0", "end-1c")
        self.assertIn("Total Size: 354 MB in F:\\Git.", text)
        self.assertIn("Size analysis complete.", text)
        self.assertNotIn("**", text)
        self.assertTrue(self.panel.transcript.tag_ranges("bold"))
        self.assertEqual(str(self.panel.ask_button["state"]), "normal")
        self.assertFalse(self.panel._busy)


class AssistantEventTests(unittest.TestCase):
    def test_old_answer_cannot_replace_current_request(self):
        events = queue.SimpleQueue()
        events.put(("assistant_token", 3, 10, "old text"))
        events.put(("assistant_answer", 3, 10, "old answer"))
        events.put(("assistant_token", 3, 11, "current text"))
        events.put(("assistant_answer", 3, 11, "current answer"))
        panel = Mock()
        app = SimpleNamespace(_events=events, _generation=3, _assistant_request_id=11, _assistant_busy=True, assistant_panel=panel, winfo_exists=lambda: False)
        SageApplication._drain_events(app)
        panel.append_reply.assert_called_once_with("current text")
        panel.finish_reply.assert_called_once_with("current answer")
        self.assertFalse(app._assistant_busy)


if __name__ == "__main__":
    unittest.main()
