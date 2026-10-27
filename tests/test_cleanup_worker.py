"""Cleanup calculations run without Tk; stale results cannot create buttons."""
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sage.services.cleanup import find_cleanup_suggestions
from sage.ui.application import SageApplication
from test_cleanup import cleanup_scan

class CleanupWorkerTests(unittest.TestCase):
    def test_scan_completion_does_not_calculate_cleanup_on_ui_thread(self):
        result=cleanup_scan();started=threading.Event();release=threading.Event()
        def calculate():
            self.assertIsNot(threading.current_thread(),threading.main_thread())
            started.set();release.wait(2)
            return find_cleanup_suggestions(result)
        conversation=Mock();conversation.cleanup_suggestions.side_effect=calculate
        app=SimpleNamespace(_result=result,_generation=1,_conversation=conversation,_assistant=Mock(),
            _events=queue.SimpleQueue(),tree_panel=Mock(),progress_bar=Mock(),progress_phase=Mock(),
            progress_detail=Mock(),assistant_panel=Mock(),status_var=Mock(),_update_drive_info=lambda:None,
            _mft_elapsed=1.0,_size_started_at=0.0)
        app._cleanup_worker=lambda *args:SageApplication._cleanup_worker(app,*args)
        SageApplication._size_finished(app,result)
        self.assertTrue(started.wait(2))
        release.set()
        self.assertEqual(app._events.get(timeout=2)[0],'cleanup_done')

    def test_worker_returns_existing_suggestions_without_ui_calls(self):
        result=cleanup_scan()
        expected=find_cleanup_suggestions(result)
        worker_threads=[]
        def suggestions():
            worker_threads.append(threading.current_thread().name)
            return find_cleanup_suggestions(result)
        conversation=SimpleNamespace(cleanup_suggestions=suggestions)
        app=SimpleNamespace(_events=queue.SimpleQueue())
        worker=threading.Thread(target=SageApplication._cleanup_worker,
            args=(app,3,result,conversation),name='test-cleanup')
        worker.start();worker.join(5)
        self.assertFalse(worker.is_alive())
        event=app._events.get_nowait()
        self.assertEqual(event[:2],('cleanup_done',3))
        self.assertIs(event[2],result)
        self.assertIs(event[3],conversation)
        self.assertEqual(event[4],expected)
        self.assertEqual(worker_threads,['test-cleanup'])

    def test_completion_keeps_buttons_and_rejects_stale_result(self):
        result=cleanup_scan();suggestions=find_cleanup_suggestions(result)
        conversation=Mock()
        panel=Mock();delete=Mock()
        app=SimpleNamespace(_result=result,_conversation=conversation,assistant_panel=panel,_delete_suggestion=delete)
        SageApplication._cleanup_finished(app,object(),conversation,suggestions,1.0)
        SageApplication._cleanup_finished(app,result,object(),suggestions,1.0)
        panel.add_cleanup_actions.assert_not_called()
        SageApplication._cleanup_finished(app,result,conversation,suggestions,1.0)
        panel.add_cleanup_actions.assert_called_once_with(suggestions,delete)
        conversation.remember_suggestions.assert_called_once_with(suggestions)

    def test_completion_waits_for_streaming_answer(self):
        result=cleanup_scan();conversation=Mock();panel=Mock();scheduled=[]
        app=SimpleNamespace(_result=result,_conversation=conversation,assistant_panel=panel,
            _assistant_busy=True,after=lambda delay,callback:scheduled.append(callback))
        SageApplication._cleanup_finished(app,result,conversation,[],1.0)
        self.assertEqual(len(scheduled),1)
        panel.write.assert_not_called()

    def test_worker_reports_failure_without_ui_access(self):
        conversation=Mock();conversation.cleanup_suggestions.side_effect=ValueError('failed')
        app=SimpleNamespace(_events=queue.SimpleQueue());result=object()
        SageApplication._cleanup_worker(app,4,result,conversation)
        event=app._events.get_nowait()
        self.assertEqual(event[:2],('cleanup_error',4))
        self.assertIsInstance(event[4],ValueError)

if __name__=='__main__':unittest.main()
