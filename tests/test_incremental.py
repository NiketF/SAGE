"""Small additive contracts: original protocol, evidence, facts and completion."""
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

class IncrementalTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.index=DriveIndex(str(Path(temp.name)/'index.sqlite3'))
        self.result=scan({'Work/Training/lesson.mp4':40,'Work/Training/notes.txt':10,'Other/file.bin':100})

    def test_original_protocol_and_named_lookup_deliver_tree_then_preserve_focus(self):
        payloads=[]
        def respond(payload):
            payloads.append(json.loads(json.dumps(payload)))
            if len(payloads)==1:return tool('search_files',{'query':'Training','kind':'folder'})
            if len(payloads)==2:return {'message':{'content':json.dumps({'purpose':'This folder holds course videos and notes.','answer':'Review whether you still need them before removing the local copy.'})}}
            return {'message':{'content':'{"fact":"size"}'}}
        a=DriveAssistant(self.result,OllamaClient(transport=respond),self.index)
        self.assertEqual([set(form['required']) for form in a.response_format['oneOf']],[{'fact'},{'purpose','answer'},{'calls'}])
        a.answer('Assess the Training collection without a selection.')
        lookup=a.last_context['lookups'][0]['result']
        item=lookup['inspected_items'][0]
        self.assertEqual(item['path'],r'E:\Work\Training')
        self.assertIn('tree_evidence',item);self.assertIn('purpose_evidence',item);self.assertIn('parent_context',item)
        self.assertEqual(a.focus,[item['path']])
        self.assertIn('50 B',a.answer('How large is it?'))
        self.assertIn('Training',payloads[-1]['messages'][-1]['content'])

    def test_no_scan_conversation_uses_original_answer_form_without_a_classifier(self):
        for question,answer in [('Hello','Hello!'),('Explain RAM','RAM holds temporary working data.'),('Tell me a sports result','I help with computing and storage questions.')]:
            a=DriveAssistant(None,OllamaClient(transport=lambda _: {'message':{'content':json.dumps({'purpose':answer,'answer':''})}}),self.index)
            with patch.object(a.tools,'call',side_effect=AssertionError('No lookup needed')):
                self.assertEqual(a.answer(question),answer)
            self.assertEqual(a.last_timings['model_calls'],1)

    def test_headroom_and_footer_removal_keep_complete_answer_and_stream(self):
        seen=[]
        def respond(payload):
            seen.append(payload)
            return {'message':{'content':json.dumps({'purpose':'This folder holds training material.','answer':'Review the videos and notes before removal. No file was changed.'})},'done_reason':'stop'}
        a=DriveAssistant(self.result,OllamaClient(transport=respond),self.index);shown=[]
        reply=a.answer('Assess this.',(r'E:\Work\Training',),on_token=shown.append)
        self.assertEqual(seen[0]['options']['num_predict'],640)
        self.assertIn('Review the videos and notes',reply)
        self.assertNotIn('No file was changed',reply)
        self.assertEqual(''.join(shown),reply)

    def test_fact_storage_is_fresh_and_independent_of_index_writer(self):
        a=DriveAssistant(self.result,OllamaClient(transport=lambda _:None),self.index)
        a.tools.focus=[r'E:\Work\Training'];self.index.ensure_schema()
        with self.index._connect() as conn:
            conn.execute("INSERT INTO files(path,name,is_directory,indexed_at) VALUES ('E:\\new','new',0,1)")
            fact=a.tools.call('report_fact',{'field':'files'})
        self.assertEqual(fact['value'],2);self.assertEqual(fact['source'],'Retained scan')
        with patch('sage.platform.windows.drives.get_disk_space',side_effect=[(1000,600,400),(1000,700,300),OSError('Unavailable')]):
            first=a.tools.call('report_fact',{'field':'drive_capacity'})
            second=a.tools.call('report_fact',{'field':'drive_capacity'})
            self.assertIn('error',a.tools.call('report_fact',{'field':'drive_capacity'}))
        self.assertEqual(first['value']['free_bytes'],400);self.assertEqual(second['value']['free_bytes'],300)
        import sqlite3
        with sqlite3.connect(str(Path(self.index.db_path).with_name('index-facts.sqlite3'))) as conn:
            row=conn.execute("SELECT value_json,source FROM facts WHERE field='drive_capacity'").fetchone()
        self.assertEqual(json.loads(row[0])['free_bytes'],300)
        conn.close()
        self.assertEqual(row[1],'Windows volume measurement')

    def test_windows_evidence_is_additive_and_not_applied_to_other_drives(self):
        result=scan({'Program Files/App/tool.exe':1,'Personal/photo.jpg':4},drive='C:')
        registry=ToolRegistry(ScanEvidence(result),self.index)
        with patch.dict('os.environ',{'SystemDrive':'C:','ProgramFiles':r'C:\Program Files'}):
            before=registry.evidence.inspect(r'C:\Program Files\App')
            item=registry.call('inspect_path',{'path':r'C:\Program Files\App'})
        for key,value in before.items():self.assertEqual(item[key],value)
        self.assertEqual(item['windows_assessment']['role'],'Installed-application area')
        self.assertIn('not proof',item['windows_assessment']['limits'])
        other=ToolRegistry(ScanEvidence(self.result),self.index).call('inspect_path',{'path':r'E:\Work'})
        self.assertNotIn('windows_assessment',other)

if __name__=='__main__':unittest.main()
