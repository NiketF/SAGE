"""Regression cases found in the repository review; no real drive deletions."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sage.models import FileRecord, VolumeInfo
from sage.services.hierarchy import build_scan_result
from sage.services.retrieval import ScanEvidence
from sage.services.conversation import DriveAssistant, compact
from sage.services.local_ai import OllamaClient
from sage.services.local_database import DriveIndex
from sage.services.tool_registry import ToolRegistry
from sage.services.cleanup import find_cleanup_suggestions, cleanup_report
from sage.services.known_folders import describe
from sage.services.scan_changes import ScanChanges
from sage.platform.windows.file_operations import delete_permanently, _cleanup_path
from sage.ui.application import SageApplication
from test_conversational_intents import make_scan


def prose(answer='Hello.'):
    return {'message': {'content': json.dumps({'intent': 'conversation', 'subject': None,
                                              'purpose': '', 'answer': answer})}}


class FileSafetyTests(unittest.TestCase):
    def junction(self, link, target):
        result = subprocess.run(['cmd.exe','/c','mklink','/J',str(link),str(target)],capture_output=True)
        if result.returncode:
            self.skipTest('Cannot create an isolated test junction on this machine.')

    def test_contents_only_rejects_junction_and_preserves_destination(self):
        with tempfile.TemporaryDirectory(prefix='sage-fix-check-') as folder:
            base=Path(folder);target=base/'target';target.mkdir()
            marker=target/'keep.txt';marker.write_text('fixture')
            link=base/'link';self.junction(link,target)
            try:
                with self.assertRaises(OSError):delete_permanently(str(link),contents_only=True)
                self.assertEqual(marker.read_text(),'fixture')
            finally:os.rmdir(link)

    def test_linked_ancestor_is_rejected_but_link_itself_can_be_removed(self):
        with tempfile.TemporaryDirectory(prefix='sage-fix-check-') as folder:
            base=Path(folder);target=base/'target';target.mkdir()
            marker=target/'keep.txt';marker.write_text('fixture')
            link=base/'link';self.junction(link,target)
            try:
                with self.assertRaises(OSError):delete_permanently(str(link/'keep.txt'))
                self.assertTrue(marker.exists())
                self.assertEqual(delete_permanently(str(link)),0)
                self.assertTrue(marker.exists())
            finally:
                if link.exists():os.rmdir(link)

    def test_validated_directory_cannot_be_swapped_during_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='sage-fix-check-') as folder:
            base=Path(folder);target=base/'target';target.mkdir()
            with _cleanup_path(str(target),True):
                with self.assertRaises(OSError):os.rename(target,base/'replacement')
            # Handles are released on exit, including failure paths.
            os.rename(target,base/'replacement')

    def test_orphan_and_its_descendants_never_alias_a_real_root_path(self):
        records={5:FileRecord(5,5,'.',16),6:FileRecord(6,5,'same',16),
                 7:FileRecord(7,999,'same',16),8:FileRecord(8,7,'child.txt',0)}
        result=build_scan_result(VolumeInfo('C:','','NTFS'),records)
        self.assertEqual(result.path_for(6),'C:\\same')
        self.assertIsNone(result.path_for(7));self.assertIsNone(result.path_for(8))
        self.assertEqual(len(result.records),4)
        self.assertEqual(result.root_stats,(3,1,2))
        self.assertEqual(ScanEvidence(result).inspect('C:\\same')['frn'],6)
        app=SimpleNamespace(_result=result,tree_panel=SimpleNamespace(result=result,
            selected_frns=lambda:[7,8],changes=ScanChanges()))
        self.assertEqual(SageApplication._selected_paths(app),[])


class EvidenceFixTests(unittest.TestCase):
    def test_scope_is_applied_before_global_limit(self):
        files={f'Users/Ada/Downloads/setup-{i}.exe':100*1024**2 for i in range(12)}
        files['Users/Ada/AppData/Local/pip/cache/package.whl']=30*1024**2
        result=make_scan(files,drive='C:');registry=ToolRegistry(ScanEvidence(result),None)
        scope='C:\\Users\\Ada\\AppData\\Local\\pip\\cache'
        self.assertEqual(len(registry.cleanup_suggestions()),12)
        with patch.object(registry.evidence,'_installed_applications',return_value=[]):
            answer=registry.call('cleanup_candidates',{'scope':scope})
        self.assertEqual([item['path'] for item in answer['items']],[scope])
        self.assertEqual(len(registry.cleanup_suggestions()),12)

    def test_compacted_pages_do_not_skip_children(self):
        registry=ToolRegistry(ScanEvidence(make_scan({f'Folder/{i:02}.txt':i+1 for i in range(37)},drive='C:')),None)
        offset=0;paths=[]
        while True:
            page=compact(registry.call('list_children',{'path':'C:\\Folder','limit':20,'offset':offset}))
            paths.extend(item['path'] for item in page['items'])
            if 'next_offset' not in page:break
            offset=page['next_offset']
        self.assertEqual(len(paths),37);self.assertEqual(len(set(paths)),37)

    def test_compaction_preserves_verified_long_paths(self):
        path='C:\\'+'\\'.join(['long-folder-name']*100)+'\\file.txt'
        self.assertEqual(compact({'path':path})['path'],path)

    def test_summary_and_name_search_treat_wildcards_literally(self):
        with tempfile.TemporaryDirectory() as folder:
            index=DriveIndex(str(Path(folder)/'index.sqlite3'))
            index.update_scan(make_scan({'Build_1/a.txt':20,'BuildA1/b.txt':30,
                                        'Build%2/c.txt':40,'BuildX2/d.txt':50},drive='C:'),index_content=False)
            self.assertEqual(index.summarize_folder('c:\\BUILD_1')['total_allocated'],20)
            self.assertEqual(index.summarize_folder('C:\\Build%2')['total_allocated'],40)
            self.assertEqual({row['name'] for row in index.search_names('Build_1')},{'Build_1','a.txt'})

    def test_cleanup_wording_does_not_claim_restoration_or_recoverable_space(self):
        result=make_scan({'Users/Ada/Downloads/my-only-build.exe':30*1024**2},drive='C:')
        suggestions=find_cleanup_suggestions(result)
        self.assertEqual(len(suggestions),1)
        self.assertIn('only copy',suggestions[0].reason)
        report=cleanup_report(result,suggestions)
        self.assertIn('Actual space recovered may differ',report)
        self.assertNotIn('You can free',report)
        self.assertNotIn('remaining space is in installed',cleanup_report(result,[]))

    def test_location_advice_is_explicitly_unverified(self):
        item=describe('C:\\NVIDIA\\personal-project\\notes.txt')
        self.assertIn('not a verified assessment',item['basis'])
        self.assertNotIn('Safe to delete',item['advice'])


class ConversationFixTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.result=make_scan({'Folder/file.txt':100},drive='C:')
        self.index=DriveIndex(str(Path(self.temp.name)/'index.sqlite3'))

    def test_cancelled_answer_does_not_cancel_index_or_next_question(self):
        calls=[]
        def respond(payload):
            calls.append(payload)
            if len(calls)==1:assistant.cancel_reply()
            return prose()
        assistant=DriveAssistant(self.result,OllamaClient(transport=respond),self.index)
        first=assistant.answer('Hello')
        self.assertIn('Files changed',first)
        self.assertEqual(assistant.history,[])
        self.assertFalse(assistant._index_cancel.is_set())
        self.assertEqual(assistant.answer('Hello again'),'Hello.')
        self.assertEqual(len(calls),2)
        self.assertEqual(assistant.last_timings['status'],'model_answer')

    def test_ui_waits_for_old_worker_before_allowing_next_answer(self):
        assistant=DriveAssistant(self.result,OllamaClient(transport=lambda _:prose()),self.index)
        app=SimpleNamespace(tree_panel=SimpleNamespace(changes=ScanChanges(),refresh_changes=Mock()),
            assistant_panel=Mock(),_changes_var=Mock(),_changes_banner=Mock(),status_var=Mock(),
            _assistant_busy=True,_assistant_request_id=1,_conversation=assistant)
        SageApplication._publish_changes(app)
        self.assertTrue(app._assistant_busy)
        self.assertEqual(app._assistant_request_id,1)
        self.assertTrue(assistant._reply_cancelled.is_set())
        app.assistant_panel.finish_reply.assert_not_called()

    def test_inflight_worker_stops_before_a_new_answer_uses_the_session(self):
        started=threading.Event();release=threading.Event();calls=[];answers=[]
        def respond(payload):
            calls.append(payload)
            if len(calls)==1:
                started.set();release.wait(3)
            return prose()
        assistant=DriveAssistant(self.result,OllamaClient(transport=respond),self.index)
        worker=threading.Thread(target=lambda:answers.append(assistant.answer('Hello')))
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            assistant.cancel_reply()
        finally:
            release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIn('Files changed',answers[0])
        self.assertEqual(assistant.history,[])
        self.assertEqual(assistant.answer('Hello again'),'Hello.')

    def test_stale_selection_does_not_answer_for_the_model(self):
        calls=[]
        assistant=DriveAssistant(self.result,OllamaClient(transport=lambda payload:(calls.append(payload) or prose())),self.index)
        self.assertEqual(assistant.answer('Hello',('C:\\Missing',)),'Hello.')
        self.assertEqual(len(calls),1)
        self.assertEqual(assistant.last_suggestions,[])

    def test_injected_and_real_transport_reject_the_same_invalid_replies(self):
        for raw in ('Hello.',json.dumps({'subject':None,'purpose':'','answer':'Hello.'})):
            response={'message':{'content':raw}}
            injected=DriveAssistant(None,OllamaClient(transport=lambda _,r=response:r),self.index)
            real=DriveAssistant(None,OllamaClient(),self.index)
            with patch.object(OllamaClient,'_request',return_value=response):
                actual=real.answer('Hello')
            self.assertEqual(injected.answer('Hello'),actual)
            self.assertEqual(real.last_timings['status'],'fallback')
            self.assertEqual(injected.last_timings['status'],'fallback')

    def test_injected_and_real_transport_accept_same_valid_response(self):
        response=prose()
        injected=DriveAssistant(None,OllamaClient(transport=lambda _:response),self.index)
        real=DriveAssistant(None,OllamaClient(),self.index)
        with patch.object(OllamaClient,'_request',return_value=response):
            actual=real.answer('Hello')
        self.assertEqual(actual,'Hello.');self.assertEqual(injected.answer('Hello'),actual)


if __name__=='__main__':unittest.main()
