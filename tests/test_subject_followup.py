"""Focused contracts for named subjects and additive uninstall advice."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_drive_conversation import scan,tool
from sage.services.conversation import DriveAssistant
from sage.services.local_ai import OllamaClient
from sage.services.local_database import DriveIndex
from sage.services.retrieval import ScanEvidence
from sage.services.tool_registry import ToolRegistry


class SubjectFollowupTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.index=DriveIndex(str(Path(temp.name)/'index.sqlite3'))
        self.result=scan({'Shared/Studio/settings.json':10,'Archive/Studio/notes.txt':20,'Selected/file.txt':1})

    def assistant(self,respond):
        return DriveAssistant(self.result,OllamaClient(transport=respond),self.index)

    def test_named_subject_is_inspected_before_answer_and_keeps_followup_target(self):
        seen=[]
        def respond(payload):
            seen.append(json.loads(json.dumps(payload)))
            if len(seen)==1:return {'message':{'content':json.dumps({'subject':'Studio','purpose':'WRONG selected folder description.','answer':'WRONG assessment.'})}}
            if len(seen)==2:return {'message':{'content':json.dumps({'subject':'Studio','purpose':'The folder holds application settings.','answer':'Review its use before removal.'})}}
            return {'message':{'content':'{"subject":"E:\\\\Shared\\\\Studio","fact":"size"}'}}
        a=self.assistant(respond);a.focus=[r'E:\Shared'];a._last_selection=(r'E:\Selected',)
        shown=[];reply=a.answer('Can I remove Studio?',(r'E:\Selected',),on_token=shown.append)
        self.assertNotIn('WRONG',''.join(shown));self.assertEqual(''.join(shown),reply)
        self.assertEqual(a.focus,[r'E:\Shared\Studio'])
        lookup=a.last_context['lookups'][0]['result']
        self.assertEqual(lookup['resolution'],'conversation scope')
        self.assertEqual(a.last_context['lookups'][1]['result']['path'],r'E:\Shared\Studio')
        self.assertIn('tree_evidence',a.last_context['lookups'][1]['result'])
        self.assertIn('10 B',a.answer('How big is it?',(r'E:\Selected',)))
        self.assertEqual(a.last_timings['model_calls'],1)

    def test_same_inspected_subject_needs_no_extra_model_round(self):
        a=self.assistant(lambda _: {'message':{'content':json.dumps({'subject':'Selected','purpose':'This is a folder.','answer':'It holds a text file.'})}})
        self.assertIn('text file',a.answer('What is Selected?',(r'E:\Selected',)))
        self.assertEqual(a.last_timings['model_calls'],1)

    def test_ambiguous_name_is_not_an_implicit_fact_about_selection(self):
        registry=ToolRegistry(ScanEvidence(self.result),self.index)
        rows=registry.resolve_subject('Studio')
        self.assertEqual(len(rows['items']),2)
        registry.subject_scope=[r'E:\Shared']
        self.assertEqual([row['path'] for row in registry.resolve_subject('Studio')['items']],[r'E:\Shared\Studio'])
        # An explicit scope is never widened to the conversation target.
        self.assertEqual(registry.call('search_files',{'query':'Studio','scope':r'E:\Selected'})['items'],[])

    def test_name_resolution_is_not_limited_to_indexing_first_twenty_thousand(self):
        result=scan({**{f'bulk/{i}.txt':1 for i in range(20005)},'Deep/Target/data.txt':2})
        registry=ToolRegistry(ScanEvidence(result),None)
        hit=registry.resolve_subject('Target')
        self.assertEqual(hit['items'][0]['path'],r'E:\Deep\Target')
        self.assertFalse(hit['partial'])

    def test_only_real_ambiguity_asks_for_a_path(self):
        calls=[]
        def respond(payload):
            calls.append(payload)
            if len(calls)==1:return {'message':{'content':'{"subject":"Studio","purpose":"Premature assessment.","answer":"Keep it."}'}}
            self.assertIn(r'E:\\Shared\\Studio',payload['messages'][-1]['content'])
            return {'message':{'content':json.dumps({'subject':None,'purpose':'There are two Studio folders.',
                'answer':r'Which one do you mean: `E:\Shared\Studio` or `E:\Archive\Studio`?'})}}
        a=self.assistant(respond);shown=[]
        reply=a.answer('Can I delete Studio?',(r'E:\Selected',),on_token=shown.append)
        self.assertNotIn('Premature',''.join(shown))
        self.assertIn('Which one',reply)
        self.assertEqual(a.last_timings['status'],'model_answer')
        self.assertEqual(len(calls),2)

    def test_greeting_needs_no_subject_lookup_or_extra_model_round(self):
        a=DriveAssistant(None,OllamaClient(transport=lambda _: {'message':{'content':'{"subject":null,"purpose":"Hello!","answer":""}'}}),self.index)
        with patch.object(a.tools,'call',side_effect=AssertionError('No lookup')):
            self.assertEqual(a.answer('Hello'),'Hello!')
        self.assertEqual(a.last_timings['model_calls'],1)

    def test_error_is_not_disguised_as_selected_folder_facts(self):
        def fail(_):raise RuntimeError('Model unavailable')
        a=self.assistant(fail)
        reply=a.answer('Tell me about Studio',(r'E:\Selected',))
        self.assertNotIn('file.txt',reply);self.assertNotIn('Space used',reply)
        self.assertIn('complete, verified answer',reply)

    def test_native_subject_stream_waits_for_target_and_preserves_valid_stream(self):
        a=self.assistant(lambda _:None);a.client.transport=None;shown=[];calls=[]
        def chat(messages,on_token,**kwargs):
            calls.append(messages)
            content=json.dumps({'subject':'Studio','purpose':'WRONG folder.' if len(calls)==1 else 'The folder holds settings.',
                                'answer':'Review the application before removal.'})
            for char in content:on_token(char)
            return {'content':content}
        a.focus=[r'E:\Shared']
        with patch.object(a.client,'ready',return_value=True),patch.object(a.client,'chat',side_effect=chat):
            reply=a.answer('Can I remove Studio?',on_token=shown.append)
        self.assertNotIn('WRONG',''.join(shown));self.assertEqual(''.join(shown),reply)
        self.assertGreater(len(shown),1)

    def test_optional_apps_are_separate_and_do_not_change_cleanup_allowlist(self):
        result=scan({'Program Files/Editor/bin.exe':10,'Windows/Utility/app.exe':10,
                     'Program Files/Driver/app.exe':10,'Program Files/Unknown/app.exe':10},drive='C:')
        evidence=ScanEvidence(result);registry=ToolRegistry(evidence,self.index)
        apps=[{'name':'Editor','location':r'C:\Program Files\Editor','uninstall_available':True},
              {'name':'Windows utility','location':r'C:\Windows\Utility','uninstall_available':True},
              {'name':'Driver','location':r'C:\Program Files\Driver','uninstall_available':True,'system_component':True},
              {'name':'Unknown','location':r'C:\Program Files\Unknown'}]
        before=list(registry.cleanup_suggestions())
        with patch.object(evidence,'_installed_applications',return_value=apps),patch.dict('os.environ',{'SystemRoot':r'C:\Windows','ProgramFiles':r'C:\Program Files'}):
            response=registry.call('cleanup_candidates',{})
        self.assertEqual(registry.cleanup_suggestions(),before)
        self.assertEqual(response['items'],[])
        self.assertEqual([row['path'] for row in response['optional_applications']],[r'C:\Program Files\Editor'])
        self.assertEqual(response['optional_applications'][0]['action'],'review_for_uninstall')
        self.assertIn('Do not delete',response['optional_applications'][0]['limits'])


if __name__=='__main__':unittest.main()
