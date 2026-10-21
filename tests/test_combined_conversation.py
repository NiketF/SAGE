"""Small contracts for combined context, tree access, startup and optional actions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,MagicMock,patch

from test_drive_conversation import scan,tool
from sage.services.conversation import DriveAssistant
from sage.services.local_ai import OllamaClient
from sage.services.local_database import DriveIndex
from sage.services.retrieval import ScanEvidence
from sage.services.tool_registry import ToolRegistry


class CombinedConversationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.index=DriveIndex(str(Path(temp.name)/'index.sqlite3'))
        self.result=scan({'Courses/lesson.mp4':30,'Runner/models/model.bin':100,'Other/Runner/file.txt':1})

    def assistant(self,respond,result=None):
        return DriveAssistant(result or self.result,OllamaClient(transport=respond),self.index)

    def test_named_root_tree_is_loaded_before_generation_and_becomes_followup(self):
        received=[]
        def respond(payload):
            received.append(json.loads(json.dumps(payload)))
            return {'message':{'content':json.dumps({'subject':r'E:\Runner','purpose':'This folder contains model data.',
                 'answer':'Review whether the models are still needed before removing them.'})}}
        a=self.assistant(respond);a.focus=[r'E:\Courses']
        reply=a.answer('Can I delete Runner?',(r'E:\Courses',))
        context=json.loads(received[0]['messages'][-1]['content'].split('Drive evidence: ',1)[1])
        hinted=context['named_candidates']['inspected_items'][0]
        self.assertEqual(hinted['path'],r'E:\Runner');self.assertIn('tree_evidence',hinted)
        self.assertTrue(hinted['tree_access']['all_retained_descendants_retrievable'])
        self.assertEqual(a.focus,[r'E:\Runner']);self.assertEqual(a.last_timings['model_calls'],1)
        self.assertNotIn('No file was changed',reply)
        a.client.transport=lambda _: {'message':{'content':'{"subject":"E:\\\\Runner","fact":"size"}'}}
        self.assertIn('100 B',a.answer('How big is it?',(r'E:\Courses',)))

    def test_subject_is_required_but_three_response_forms_remain(self):
        a=self.assistant(lambda _:None)
        forms=a.response_format['oneOf']
        self.assertEqual([set(form['required'])-{'intent','subject'} for form in forms],[{'fact'},{'purpose','answer'},{'calls'}])
        self.assertTrue(all('subject' in form['required'] for form in forms))

    def test_greetings_remain_model_written_without_whole_drive_lookup(self):
        a=self.assistant(lambda _: {'message':{'content':'{"subject":null,"purpose":"Hello!","answer":"How can I help?"}'}})
        with (patch.object(a.evidence,'resolve_name',side_effect=AssertionError('No subject lookup')),
              patch.object(a.evidence,'mentioned',side_effect=AssertionError('No full metadata walk'))):
            reply=a.answer('Hello')
        self.assertEqual(reply,'Hello! How can I help?');self.assertEqual(a.last_timings['model_calls'],1)
        self.assertEqual(a.last_suggestions,[])

    def test_deep_target_is_inspected_before_native_answer_is_displayed(self):
        a=self.assistant(lambda _:None);a.client.transport=None;a.focus=[r'E:\Courses'];seen=[];shown=[]
        def chat(messages,on_token,**kwargs):
            seen.append(messages)
            raw=json.dumps({'intent':'assessment','subject':r'E:\Other\Runner','purpose':'Premature statement.' if len(seen)==1 else 'This folder holds a text file.',
                            'answer':'Review the file before removal.'})
            for char in raw:on_token(char)
            return {'content':raw}
        with patch.object(a.client,'ready',return_value=True),patch.object(a.client,'chat',side_effect=chat):
            reply=a.answer('Assess the nested runner.',on_token=shown.append)
        self.assertEqual(len(seen),2);self.assertNotIn('Premature',''.join(shown))
        self.assertEqual(''.join(shown),reply);self.assertEqual(a.focus,[r'E:\Other\Runner'])

    def test_every_retained_child_is_accessible_with_pages(self):
        result=scan({f'Folder/{n:02}.txt':n+1 for n in range(37)})
        registry=ToolRegistry(ScanEvidence(result),None);offset=0;paths=[]
        while True:
            page=registry.call('list_children',{'path':r'E:\Folder','offset':offset,'limit':8})
            paths.extend(row['path'] for row in page['items'])
            if page['next_offset'] is None:break
            offset=page['next_offset']
        self.assertEqual(len(paths),37);self.assertEqual(len(set(paths)),37)
        self.assertEqual(paths,sorted(paths))

    def test_search_reaches_beyond_old_bound_and_sql_pages_agree(self):
        result=scan({**{f'Bulk/{n:05}.txt':1 for n in range(20002)},'Late/needle.txt':99})
        registry=ToolRegistry(ScanEvidence(result),None)
        self.assertEqual(registry.call('search_files',{'query':'needle.txt'})['items'][0]['path'],r'E:\Late\needle.txt')
        # Small scan checks page parity without building an unnecessary large DB.
        ordered=scan({'Zebra/a.txt':1,'alpha/B.txt':1,'Mixed/z.txt':1,'Mixed/C.txt':1})
        evidence=ScanEvidence(ordered);fallback=ToolRegistry(evidence,None)
        self.index.update_scan(ordered,index_content=False);indexed=ToolRegistry(evidence,self.index)
        args={'scope':'E:\\','kind':'file','sort':'name','limit':1,'offset':1}
        self.assertEqual([row['path'] for row in fallback.call('search_files',args)['items']],
                         [row['path'] for row in indexed.call('search_files',args)['items']])

    def test_partial_reply_gets_one_complete_replacement_within_budget(self):
        calls=[]
        def respond(payload):
            calls.append(payload)
            if len(calls)==1:return {'message':{'content':'{"subject":null,"purpose":"Incomplete'},'done_reason':'length'}
            return {'message':{'content':'{"subject":null,"purpose":"RAM holds working data.","answer":"Its contents are temporary."}'},'done_reason':'stop'}
        a=self.assistant(respond)
        reply=a.answer('Explain RAM')
        self.assertEqual(len(calls),2);self.assertEqual(a.last_timings['model_calls'],2)
        self.assertNotIn('Incomplete',reply);self.assertIn('temporary',reply)

    def test_uninstall_actions_require_registered_location_and_model_choice(self):
        result=scan({'Apps/Editor/app.exe':10,'Apps/Inferred/app.exe':20,'Windows/Service/app.exe':30},drive='C:')
        apps=[{'name':'Editor','location':r'C:\Apps\Editor','registered_location':r'C:\Apps\Editor','uninstall_string':r'"C:\Apps\Editor\uninstall.exe"'},
              {'name':'Inferred','location':r'C:\Apps\Inferred','uninstall_string':r'C:\unknown.exe'},
              {'name':'Service','location':r'C:\Windows\Service','registered_location':r'C:\Windows\Service','uninstall_string':r'C:\service.exe'}]
        def respond(_):return {'message':{'content':json.dumps({'subject':r'C:\Apps','purpose':'This contains application folders.',
             'answer':r'Consider uninstalling `C:\Apps\Editor` if you no longer need it. Preserve needed data first.',
             'uninstall_paths':[r'C:\Apps\Editor',r'C:\Apps\Inferred']})}}
        a=self.assistant(respond,result)
        with patch('sage.services.profile.installed_apps',return_value=apps),patch.dict('os.environ',{'SystemRoot':r'C:\Windows'}):
            before=list(a.cleanup_suggestions());reply=a.answer('What can I remove in Apps?')
            self.assertEqual([item.path for item in a.last_suggestions],[r'C:\Apps\Editor'])
            self.assertEqual(a.cleanup_suggestions(),before)
            self.assertNotIn('uninstall.exe',json.dumps(a.last_context))
            self.assertEqual(a.uninstall_suggestions([r'C:\Apps\Editor'])[0].path,r'C:\Apps\Editor')

    def test_runtime_is_reused_and_missing_server_is_started_once(self):
        with patch('sage.services.local_ai.socket.create_connection') as connect,patch('sage.services.local_ai.subprocess.Popen') as launch:
            OllamaClient._ensure_server();launch.assert_not_called()
        process=Mock();process.poll.return_value=None
        with patch.object(OllamaClient,'_server_process',None),patch('sage.services.local_ai.socket.create_connection',side_effect=[OSError('offline'),MagicMock(),MagicMock()]),\
             patch('sage.services.local_ai.shutil.which',return_value=r'E:\Runtime\ollama.exe'),patch('sage.services.local_ai.subprocess.Popen',return_value=process) as launch:
            OllamaClient._ensure_server();OllamaClient._ensure_server()
            launch.assert_called_once();self.assertEqual(launch.call_args.args[0],[r'E:\Runtime\ollama.exe','serve'])
            self.assertNotIn('shell',launch.call_args.kwargs)

    def test_fact_queries_persist_fresh_values(self):
        a=self.assistant(lambda _: {'message':{'content':'{"subject":"E:\\\\Courses","fact":"files"}'}})
        self.assertIn('1 files',a.answer('How many files are in Courses?'))
        self.assertTrue(Path(self.index.db_path).with_name('index-facts.sqlite3').exists())


if __name__=='__main__':unittest.main()
