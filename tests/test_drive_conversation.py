"""Contracts for the active conversational UI path, not scenario-specific replies."""
import json
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from sage.models import FileRecord, VolumeInfo
from sage.services.conversation import DriveAssistant,answer_text,paths_verified
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services.local_ai import OllamaClient
from sage.services.local_database import DriveIndex
from sage.services.retrieval import ScanEvidence
from sage.services.tool_registry import ToolRegistry


def scan(names, drive='E:'):
    records={5:FileRecord(5,5,'.',16)}
    paths={'':5}
    for name,size in names.items():
        parts=name.rstrip('/').split('/')
        for n,part in enumerate(parts):
            path='/'.join(parts[:n+1])
            if path in paths: continue
            identity=len(paths)+5
            folder=n<len(parts)-1 or name.endswith('/')
            paths[path]=identity
            records[identity]=FileRecord(identity,paths['/'.join(parts[:n])],part,16 if folder else 0,
                                         size,size,size_ready=not folder,modified=100)
    result=build_scan_result(VolumeInfo(drive,'Synthetic','NTFS'),records)
    calculate_directory_sizes(result)
    return result


def tool(name,args):
    return {'message':{'content':'','tool_calls':[{'function':{'name':name,'arguments':args}}]}}


class IndexTests(unittest.TestCase):
    def test_removed_paths_and_changed_content_are_not_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            with patch.object(ScanEvidence,'_read_excerpt',return_value={'status':'text','text':'old project setting'}):
                index.update_scan(scan({'old.txt':10,'current.txt':10}))
            self.assertTrue(index.search_content('setting'))
            result=scan({'current.txt':20})
            result.records[6].modified=200
            index.update_scan(result,index_content=False)
            self.assertFalse(index.search_names('old'))
            self.assertFalse(index.search_content('setting'))

    def test_scope_has_folder_boundaries_and_literal_wildcards(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            index.update_scan(scan({'Code/a.txt':10,'Code_old/b.txt':20,'A_B/c.txt':30,'AXB/d.txt':40}),index_content=False)
            self.assertEqual([r['name'] for r in index.search_metadata(scope=r'E:\Code')],['a.txt'])
            self.assertEqual([r['name'] for r in index.search_metadata(scope=r'E:\A_B')],['c.txt'])

    def test_partial_sizes_stay_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            result=scan({'file.txt':10});result.records[6].size_ready=False
            index.update_scan(result,index_content=False)
            self.assertIsNone(index.search_names('file')[0]['allocated_bytes'])

    def test_unicode_names_and_scope_follow_case_insensitive_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            index.update_scan(scan({'Über/Résumé.txt':10}),index_content=False)
            rows=index.search_metadata(scope='E:\\ÜBER',query='RÉSUMÉ')
            self.assertEqual(len(rows),1)

    def test_repeated_scan_reuses_unchanged_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            result=scan({'file.txt':10})
            with patch.object(ScanEvidence,'_read_excerpt',return_value={'status':'text','text':'configuration'}) as read:
                index.update_scan(result);index.update_scan(result)
                self.assertEqual(read.call_count,1)

    def test_content_filters_include_exclude_and_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            result=scan({'Public/a.txt':10,'Private/b.txt':10,'Public/secrets/c.json':10,'Public/tool.exe':10})
            with patch.object(ScanEvidence,'_read_excerpt',return_value={'status':'text','text':'sample'}) as read:
                index.update_scan(result,include_paths=(r'E:\Public',),exclude_paths=(r'E:\Public\secrets',))
                self.assertEqual(read.call_count,1)
            with patch.object(ScanEvidence,'_read_excerpt',side_effect=AssertionError('No reads after deadline')):
                index.update_scan(result,content_seconds=0)

    def test_cancelled_metadata_transaction_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            result=scan({'a.txt':10})
            with self.assertRaises(InterruptedError): index.update_scan(result,cancelled=lambda:True)
            self.assertFalse(index.search_names('a.txt'))


class ToolTests(unittest.TestCase):
    def test_tree_inspection_covers_nested_mixed_contents_without_opening_files(self):
        result=scan({'Work/raw/video.mp4':400,'Work/code/src/start.py':30,
                     'Work/code/settings.json':20,'Work/empty/':0,'Other/unrelated.exe':500})
        evidence=ScanEvidence(result)
        with patch.object(evidence,'_read_excerpt',side_effect=AssertionError('Metadata only')):
            tree=evidence.tree_summary(r'E:\Work')
        self.assertFalse(tree['partial'])
        self.assertEqual(tree['files_observed'],3)
        self.assertEqual(tree['subfolders_observed'],4)
        self.assertEqual({row['extension'] for row in tree['file_types']},{'.mp4','.py','.json'})
        self.assertNotIn('unrelated.exe',json.dumps(tree))
        self.assertEqual(tree['scanned_totals']['files'],3)

    def test_tree_limits_never_present_a_sample_as_a_full_inspection(self):
        evidence=ScanEvidence(scan({f'Wide/f{i}.bin':10 for i in range(30)}))
        limited=evidence.tree_summary(r'E:\Wide',max_records=5)
        self.assertTrue(limited['partial'])
        self.assertLessEqual(limited['records_checked'],5)
        self.assertEqual(limited['scanned_totals']['files'],30)
        timed=evidence.tree_summary(r'E:\Wide',seconds=0)
        self.assertTrue(timed['partial'])
        self.assertEqual(timed['records_checked'],0)

    def test_tree_does_not_follow_links_or_loop_on_repeated_records(self):
        result=scan({'Work/Link/target.txt':10,'Work/normal.txt':10})
        link=next(r for r in result.records.values() if r.name=='Link')
        link.attributes |= 0x400
        result.children[link.parent_frn].append(link.parent_frn)
        tree=ScanEvidence(result).tree_summary(r'E:\Work')
        self.assertTrue(tree['partial'])
        self.assertEqual(tree['unfollowed_links'],1)
        self.assertEqual(tree['missing_or_repeated_records'],1)
        self.assertNotIn('target.txt',json.dumps(tree))

    def test_file_inspection_retains_type_attributes_and_unknown_measurements(self):
        result=scan({'Work/item.dat':10})
        record=next(r for r in result.records.values() if r.name=='item.dat')
        record.attributes |= 1|2|0x4000
        record.size_ready=False
        tree=ScanEvidence(result).tree_summary(r'E:\Work\item.dat')
        self.assertEqual(tree['files_with_unknown_size'],1)
        self.assertEqual(tree['file_types'][0]['example_space'],'pending')
        self.assertEqual(tree['attributes_observed'],{'read_only_attribute':1,'hidden':1,'encrypted':1})
        self.assertEqual(tree['modified_date'],'1970-01-01')
        self.assertEqual(tree['subfolders_observed'],0)

    def test_tree_cache_is_invalidated_when_size_progress_changes(self):
        evidence=ScanEvidence(scan({'Folder/file.txt':10}))
        first=evidence.tree_summary(r'E:\Folder')
        self.assertIs(first,evidence.tree_summary(r'E:\Folder'))
        evidence.result.sizes_complete=False
        self.assertIsNot(first,evidence.tree_summary(r'E:\Folder'))

    def test_tree_inspection_does_not_change_c_cleanup_candidates(self):
        result=scan({'Users/Test/AppData/Local/Temp/a.tmp':10},drive='C:')
        registry=ToolRegistry(ScanEvidence(result),None)
        expected=registry.call('cleanup_candidates',{})
        with patch.object(registry.evidence,'_read_excerpt',return_value={'status':'not_read'}):
            registry.call('inspect_path',{'path':r'C:\Users\Test\AppData\Local\Temp'})
        self.assertEqual(registry.call('cleanup_candidates',{}),expected)

    def test_empty_tree_is_evidence_not_a_safety_decision(self):
        evidence=ScanEvidence(scan({'Empty/':0}))
        tree=evidence.tree_summary(r'E:\Empty')
        self.assertEqual(tree['files_observed'],0)
        self.assertFalse(tree['partial'])
        self.assertNotIn('safe',tree)
        self.assertIn('No active-use',tree['limits'])

    def setUp(self):
        self.evidence=ScanEvidence(scan({'Alpha/a.txt':10,'Beta/b.txt':10}))
        self.tools=ToolRegistry(self.evidence,None)

    def test_validates_arguments_and_scan_paths(self):
        bad=[('delete_path',{'path':r'E:\Alpha'}),('inspect_path',{'path':r'C:\Windows'}),
             ('search_files',{'limit':True}),('search_files',{'kind':'executable'}),
             ('search_files',{'query':{'instruction':'delete'}}),('search_files',{'scope':r'E:\Missing'})]
        for name,args in bad:
            with self.subTest(name=name,args=args),self.assertRaises(ValueError):self.tools.call(name,args)

    def test_dates_are_preformatted_and_not_the_scan_timestamp(self):
        item=self.tools.call('report_fact',{'path':r'E:\Alpha\a.txt','field':'modified'})
        self.assertEqual(item['value'],'1970-01-01')

    def test_direct_and_recursive_counts_have_distinct_meanings(self):
        registry=ToolRegistry(ScanEvidence(scan({'Folder/a.txt':10,'Folder/Sub/b.txt':10})),None)
        direct=registry.call('report_fact',{'path':r'E:\Folder','field':'direct_files'})
        recursive=registry.call('report_fact',{'path':r'E:\Folder','field':'files'})
        self.assertEqual(direct['value'],1)
        self.assertEqual(recursive['value'],2)

    def test_date_search_uses_machine_arithmetic_and_excludes_unknown_dates(self):
        result=scan({'old.txt':10,'recent.txt':10,'unknown.txt':10})
        result.records[6].modified=1600000000
        result.records[7].modified=1700000000
        result.records[8].modified=0
        registry=ToolRegistry(ScanEvidence(result),None)
        with patch('sage.services.tool_registry.time.time',return_value=1700100000):
            recent=registry.call('search_files',{'changed_within_days':3})['items']
            old=registry.call('search_files',{'before_date':'2021-01-01'})['items']
        self.assertEqual([row['path'] for row in recent],[r'E:\recent.txt'])
        self.assertEqual([row['path'] for row in old],[r'E:\old.txt'])

    def test_search_before_index_ready_uses_real_metadata(self):
        rows=self.tools.call('search_files',{'extension':'.txt','kind':'file','minimum_bytes':5})['items']
        self.assertEqual({row['path'] for row in rows},{r'E:\Alpha\a.txt',r'E:\Beta\b.txt'})

    def test_selected_files_get_parent_context_without_reading_sibling_contents(self):
        evidence=ScanEvidence(scan({'Game/package.json':10,'Game/assets/icon.png':10}))
        registry=ToolRegistry(evidence,None)
        with patch.object(evidence,'_read_excerpt',return_value={'status':'not_read'}):
            item=registry.call('inspect_path',{'path':r'E:\Game\assets\icon.png'})
        project=next(row for row in item['parent_context'] if row['path']==r'E:\Game')
        self.assertIn('package.json',project['files'])
        self.assertIn('assets',project['folders'])

    def test_duplicate_groups_are_explicitly_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            index.update_scan(self.evidence.result,index_content=False)
            self.tools.index=index
            reply=self.tools.call('find_duplicates',{})
            self.assertFalse(reply['content_verified'])
            self.assertEqual(len(reply['groups']),1)

    def test_related_name_search_returns_candidates_not_false_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            evidence=ScanEvidence(scan({'Project/config.json':10}))
            index.update_scan(evidence.result,index_content=False)
            reply=ToolRegistry(evidence,index).call('search_files',{'query':'configuration'})
            self.assertIn('candidates',reply['match_type'])
            self.assertEqual(reply['items'][0]['path'],r'E:\Project\config.json')

    def test_content_search_can_read_relevant_text_beyond_startup_index_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=DriveIndex(str(Path(tmp)/'index.sqlite3'))
            result=scan({'Project/main.py':6000,'Private/passwords.txt':100})
            index.update_scan(result,index_content=False,exclude_paths=(r'E:\Private',))
            registry=ToolRegistry(ScanEvidence(result),index)
            def excerpt(record,**kwargs):
                self.assertEqual(record.name,'main.py')
                return {'status':'text','text':'x'*4500+'\nsettings = load("config.json")'}
            with patch.object(ScanEvidence,'_read_excerpt',side_effect=excerpt):
                answer=registry.call('find_references',{'query':'config.json','scope':r'E:\Project'})
            self.assertEqual(answer['items'][0]['path'],r'E:\Project\main.py')
            self.assertIn('config.json',answer['items'][0]['snippet'])
            self.assertEqual(answer['items'][0]['source'],'current_text_excerpt')


class ConversationTests(unittest.TestCase):
    def test_path_checks_accept_matching_quotes_without_accepting_new_suffixes(self):
        known={r'e:\toolbox'}
        for text in ['References point to "E:\\Toolbox".', "References point to 'E:\\Toolbox'."]:
            self.assertTrue(paths_verified(text,known))
        for text in ['References point to "E:\\Toolbox.bak".', "References point to E:\\Toolbox'."]:
            self.assertFalse(paths_verified(text,known))

    def test_all_change_wordings_receive_inspected_evidence_before_model_answer(self):
        questions=['Can I delete this?','Would getting rid of this cause problems?',
                   'Should I update this?','Can I edit its settings?', 'Can I replace this?',
                   'What if I move it to another drive?', 'Can I rename it?',
                   'Would unpacking it affect my work?','Can I change this?']
        for question in questions:
            with self.subTest(question=question):
                received=[]
                def transport(payload):
                    received.append(payload)
                    return {'message':{'content':json.dumps({'purpose':'This folder contains a video.',
                        'answer':'Review its use before changes. No file was changed.'})}}
                a=self.make_assistant(transport)
                reply=a.answer(question,(r'E:\Media',))
                context=json.loads(received[0]['messages'][-1]['content'].split('Drive evidence: ',1)[1])
                tree=context['selected_items'][0]['tree_evidence']
                self.assertEqual(tree['file_types'][0]['extension'],'.mp4')
                self.assertFalse(tree['partial'])
                self.assertEqual(a.last_timings['model_calls'],1)
                self.assertNotIn('No file was changed',reply)

    def test_search_discovery_is_inspected_before_the_model_gives_advice(self):
        received=[]
        def transport(payload):
            received.append(payload)
            return tool('search_files',{'query':'config.json'}) if len(received)==1 else {
                'message':{'content':json.dumps({'purpose':'This JSON file may store settings.',
                    'answer':'Check its reader before changing it.'})}}
        a=self.make_assistant(transport)
        a.answer('Would changing the project configuration affect anything?')
        result=json.loads(received[-1]['messages'][-1]['content'].split(': ',1)[1])
        item=result['inspected_items'][0]
        self.assertEqual(item['path'],r'E:\Project\config.json')
        self.assertEqual(item['tree_evidence']['file_types'][0]['extension'],'.json')
        self.assertEqual(a.last_timings['model_calls'],2)

    def test_paths_from_nested_tree_evidence_can_be_used_in_the_response(self):
        a=self.make_assistant(lambda _: {'message':{'content':json.dumps({
            'purpose':'The folder contains footage.',
            'answer':'Review `E:\\Media\\Sub\\Nested\\clip.mp4` before changes.'})}},
            names={'Media/Sub/Nested/clip.mp4':1000})
        reply=a.answer('What can I change here?',(r'E:\Media',))
        self.assertIn(r'E:\Media\Sub\Nested\clip.mp4',reply)
        self.assertEqual(a.last_timings['status'],'model_answer')

    def make_assistant(self, transport, names=None):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        return DriveAssistant(scan(names or {'Project/config.json':100,'Media/clip.mp4':1000}),
            OllamaClient(transport=transport),DriveIndex(str(Path(temp.name)/'index.sqlite3')))

    def test_model_gets_equivalent_wordings_without_keyword_gate(self):
        wordings=['How big is this?','What is its size?','How much space does this use?','Tell me its storage footprint.']
        for question in wordings:
            seen=[]
            def transport(payload):
                seen.append(json.loads(json.dumps(payload)))
                return tool('report_fact',{'field':'size','final':True})
            a=self.make_assistant(transport)
            reply=a.answer(question,(r'E:\Media\clip.mp4',))
            self.assertIn('1000 B',reply)
            self.assertIn(question,seen[0]['messages'][-1]['content'])
            self.assertEqual(a.last_timings['model_calls'],1)
            self.assertEqual(a.last_timings['status'],'verified_fact')

    def test_short_followup_preserves_user_needs_and_focus(self):
        calls=[]
        def transport(payload):
            calls.append(json.dumps(payload))
            return {'message':{'content':'Keep the files you still need for your work.'}}
        a=self.make_assistant(transport)
        a.answer('I use this for editing. Should I keep it?',(r'E:\Media',))
        a.answer('editing')
        self.assertIn('I use this for editing',calls[-1])
        self.assertEqual(a.focus,[r'E:\Media'])
        self.assertEqual(a.last_timings['status'],'model_answer')

    def test_model_can_choose_a_lookup_and_use_its_results(self):
        calls=[]
        def transport(payload):
            calls.append(payload)
            return tool('search_files',{'query':'config.json'}) if len(calls)==1 else {
                'message':{'content':'`E:\\Project\\config.json` may store project settings. Its reader has not been verified. No file was changed.'}}
        a=self.make_assistant(transport)
        reply=a.answer('Where is my project configuration?')
        self.assertIn('config.json',reply)
        self.assertEqual(a.last_timings['tool_calls'],1)
        self.assertEqual(calls[-1]['messages'][-1]['role'],'user')
        self.assertIn('Read-only result',calls[-1]['messages'][-1]['content'])

    def test_whole_drive_counts_are_not_treated_as_item_lists(self):
        calls=[]
        def transport(payload):
            calls.append(payload)
            return tool('drive_overview',{}) if len(calls)==1 else {'message':{'content':'This drive contains three files.'}}
        a=self.make_assistant(transport)
        self.assertIn('three files',a.answer('Give me a drive overview.'))
        self.assertEqual(a.last_timings['status'],'model_answer')

    def test_explicit_path_overrides_previous_focus_and_selection(self):
        a=self.make_assistant(lambda _: {'message':{'content':'I will explain the selected item.'}})
        a.answer('Explain this.',(r'E:\Media',))
        a.answer(r'Explain E:\Project\config.json',(r'E:\Media',))
        self.assertEqual(a.focus,[r'E:\Project\config.json'])
        reply=a.answer(r'Explain E:\Missing.txt')
        self.assertEqual(a.last_context['unresolved_paths'],[r'E:\Missing.txt'])
        self.assertEqual(a.tools.focus,[])

    def test_missing_path_with_spaces_is_not_shortened_to_existing_parent(self):
        a=self.make_assistant(lambda _: {'message':{'content':'{"fact":"size"}'}})
        a.answer(r'Explain E:\Project New folder?')
        self.assertEqual(a.unresolved_paths,[r'E:\Project New folder'])
        self.assertEqual(a.tools.focus,[])

    def test_unchanged_tree_selection_does_not_override_conversation_focus(self):
        a=self.make_assistant(lambda _: {'message':{'content':'I can explain that item.'}})
        a.answer('Explain this.',(r'E:\Media',))
        a.answer(r'Explain E:\Project\config.json',(r'E:\Media',))
        a.client.transport=lambda _: {'message':{'content':'{"fact":"size"}'}}
        reply=a.answer('How big is it?',(r'E:\Media',))
        self.assertIn(r'E:\Project\config.json',reply)
        self.assertIn('100 B',reply)

    def test_multiple_path_references_keep_an_unscanned_destination_separate(self):
        a=self.make_assistant(lambda _: {'message':{'content':json.dumps({
            'purpose':'This video file contains footage.',
            'answer':'Moving it may affect links. The requested `D:\\Archive` is not in this scan. No file was changed.'})}})
        reply=a.answer(r'What happens if I move E:\Media\clip.mp4 to D:\Archive?')
        self.assertIn(r'D:\Archive',reply)
        self.assertEqual(a.last_context['selected_items'][0]['path'],r'E:\Media\clip.mp4')
        self.assertEqual(a.last_context['unresolved_paths'],[r'D:\Archive'])
        with self.assertRaises(ValueError):a.tools.call('inspect_path',{'path':r'D:\Archive'})

    def test_unknown_paths_are_not_displayed_even_without_backticks(self):
        a=self.make_assistant(lambda _: {'message':{'content':'Delete E:\\Invented\\data.txt to save space.'}})
        reply=a.answer('What can I remove?',(r'E:\Media',))
        self.assertNotIn('Invented',reply)
        self.assertEqual(a.last_timings['status'],'fallback')

    def test_investigation_is_bounded_and_tools_cannot_change_files(self):
        a=self.make_assistant(lambda _: tool('delete_path',{'path':r'E:\Media'}))
        reply=a.answer('Delete these files.',(r'E:\Media',))
        self.assertLessEqual(a.last_timings['model_calls'],3)
        self.assertNotIn('No file was changed',reply)
        self.assertEqual(a.last_suggestions,[])

    def test_unavailable_model_has_useful_fallback(self):
        def unavailable(_):raise RuntimeError('Model unavailable')
        a=self.make_assistant(unavailable)
        reply=a.answer('Help me decide.',(r'E:\Media',))
        self.assertNotIn('clip.mp4',reply)
        self.assertNotIn('No file was changed',reply)
        a.answer('Try again.',(r'E:\Media',))
        self.assertEqual(a.last_timings['model_calls'],1)

    def test_fallback_uses_short_simple_sentences_as_a_style_proxy(self):
        def unavailable(_):raise RuntimeError('Unavailable')
        a=self.make_assistant(unavailable)
        reply=a.answer('Can I remove this?',(r'E:\Media',))
        sentences=re.split(r'(?<=[.!?])\s+',re.sub(r'`[^`]*`','PATH',reply))
        self.assertGreaterEqual(sum(len(sentence.split())<=20 for sentence in sentences)/len(sentences),.8)

    def test_cold_model_loads_in_background_without_queuing_question(self):
        a=self.make_assistant(lambda _: {'message':{'content':'unused'}})
        a.client.transport=None;a.client.loading.set()
        with patch.object(a.client,'wait_for_warm',return_value=False),patch.object(a.client,'chat',side_effect=AssertionError('Startup did not finish')):
            started=time.monotonic()
            reply=a.answer('Help me decide.',(r'E:\Media',))
            self.assertLess(time.monotonic()-started,.2)
        self.assertNotIn('clip.mp4',reply)
        self.assertEqual(a.last_timings['status'],'loading_fallback')
        a.client.loading.clear()

    def test_fact_lookup_does_not_end_an_advice_request(self):
        calls=[]
        def transport(payload):
            calls.append(payload)
            return tool('report_fact',{'field':'size','final':False}) if len(calls)==1 else {
                'message':{'content':'Review the video before removal. Keep it if you still need it. No file was changed.'}}
        a=self.make_assistant(transport)
        reply=a.answer('Should I remove this to save space?',(r'E:\Media',))
        self.assertIn('Review the video',reply)
        self.assertEqual(a.last_timings['model_calls'],2)

    def test_compact_json_fact_and_lookup_protocol(self):
        a=self.make_assistant(lambda _: {'message':{'content':'{"fact":"size"}'}})
        self.assertIn('1000 B',a.answer('What is its storage footprint?',(r'E:\Media',)))
        self.assertEqual(a.last_timings['model_calls'],1)
        calls=[]
        def transport(payload):
            calls.append(payload)
            data={'calls':[{'name':'search_files','args':{'query':'config.json'}}]} if len(calls)==1 else {'answer':'`E:\\Project\\config.json` is a configuration candidate.'}
            return {'message':{'content':json.dumps(data)}}
        a=self.make_assistant(transport)
        self.assertIn('configuration candidate',a.answer('Where are my settings?'))
        self.assertEqual(a.last_timings['tool_calls'],1)

    def test_model_written_explanation_is_placed_before_assessment(self):
        a=self.make_assistant(lambda _: {'message':{'content':json.dumps({
            'answer':'Removing it may reset settings. Check references first.',
            'purpose':'This JSON file likely stores project settings.'})}})
        reply=a.answer('Can I delete it?',(r'E:\Project\config.json',))
        self.assertTrue(reply.startswith('This JSON file'))
        self.assertLess(reply.index('stores project settings'),reply.index('Removing it'))

    def test_stream_does_not_show_assessment_before_description(self):
        a=self.make_assistant(lambda _: {'message':{'content':'unused'}})
        a.client.transport=None
        tokens=[]
        def chat(messages,on_token,**kwargs):
            first='{"intent":"assessment","subject":null,"answer":"Removing it may reset settings.",'
            on_token(first)
            self.assertEqual(tokens,[])
            last='"purpose":"This JSON file stores settings."}'
            on_token(last)
            return {'content':first+last}
        with patch.object(a.client,'ready',return_value=True),patch.object(a.client,'chat',side_effect=chat):
            reply=a.answer('Could I remove this?',(r'E:\Project\config.json',),on_token=tokens.append)
        self.assertTrue(reply.startswith('This JSON file'))
        self.assertEqual(''.join(tokens),reply)

    def test_invalid_json_protocol_never_becomes_user_text(self):
        for raw in ['{"calls":[]}','{"fact":"size","fact":"modified"}',
                    '{"answer":"Delete it","command":"erase"}','{"calls":[{"name":"delete","args":{},"force":true}]}']:
            with self.subTest(raw=raw):
                a=self.make_assistant(lambda _,raw=raw:{'message':{'content':raw}})
                reply=a.answer('Help me decide.',(r'E:\Media',))
                self.assertEqual(a.last_timings['status'],'fallback')
                self.assertNotIn('"calls"',reply)

    def test_json_stream_decodes_escaped_paths_and_rejects_path_suffixes(self):
        self.assertEqual(answer_text('{"answer":"Path E:\\\\Media is'), 'Path E:\\Media is')
        known={r'e:\project\config.json'}
        self.assertTrue(paths_verified(r'`E:\Project\config.json` is data.',known))
        self.assertFalse(paths_verified(r'`E:\Project\config.json.bak` is data.',known))
        self.assertFalse(paths_verified(r'E:\Project\config.json.bak is data.',known))

    def test_data_size_and_disk_allocation_are_not_confused(self):
        a=self.make_assistant(lambda _: {'message':{'content':'{"fact":"logical_size"}'}})
        entry=a.evidence.inspect(r'E:\Media\clip.mp4')
        a.result.records[entry['frn']].allocated_size=4096
        reply=a.answer('How much room does this take?',(entry['path'],))
        self.assertIn('1000 B of data',reply)
        self.assertIn('4.0 KB of disk space',reply)

    def test_model_cannot_claim_that_it_changed_files(self):
        a=self.make_assistant(lambda _: {'message':{'content':'I deleted the files for you.'}})
        self.assertNotIn('I deleted',a.answer('Please remove this.',(r'E:\Media',)))
        self.assertEqual(a.last_suggestions,[])

    def test_streamed_paragraphs_and_final_tail_are_delivered_once(self):
        text='The file is project data.\n\nIts reader has not been verified.'
        a=self.make_assistant(lambda _: {'message':{'content':text}})
        tokens=[]
        reply=a.answer('Explain this.',(r'E:\Project\config.json',),on_token=tokens.append)
        self.assertEqual(''.join(tokens),reply)

    def test_partial_generation_auto_continues_and_has_action_confirmation(self):
        a=self.make_assistant(lambda _: {'message':{'content':'This file may store project settings. Removing it could'},'done_reason':'length'})
        reply=a.answer('What happens if I remove it?',(r'E:\Project\config.json',))
        self.assertNotIn('partial explanation',reply)
        self.assertNotIn('Removing it could',reply)
        self.assertNotIn('No file was changed',reply)

    def test_c_drive_policy_is_separate_without_write_tools(self):
        a=self.make_assistant(lambda _: {'message':{'content':'This is an explanation.'}})
        self.assertEqual(a.action_policy['tier'],'default_read_only')
        c=DriveAssistant(scan({'Windows/data.txt':10},drive='C:'),index=a.index)
        self.assertEqual(c.action_policy['tier'],'c_strict_read_only')
        self.assertTrue(c.action_policy['read_only'])
        self.assertFalse(any('delete' in definition['function']['name'] for definition in c.tools.definitions()))

    def test_initialization_does_not_index_and_worker_can_cancel(self):
        a=self.make_assistant(lambda _: {'message':{'content':'Hello'}})
        started=threading.Event();release=threading.Event();threads=[]
        def indexing(*args,**kwargs):
            threads.append(threading.current_thread().name);started.set();release.wait(1)
            self.assertTrue(kwargs['cancelled']())
        with patch.object(a.index,'update_scan',side_effect=indexing):
            worker=a.start_indexing();self.assertTrue(started.wait(1))
            a.cancel();release.set();worker.join(2)
        self.assertEqual(threads,['sage-drive-index'])
        self.assertIsNone(a.tools.index)

    def test_scan_tree_is_displayed_without_waiting_for_index(self):
        from sage.ui.application import SageApplication
        order=[]
        noop=lambda *args,**kwargs:None
        app=SimpleNamespace(_scan_started_at=time.monotonic(),_assistant=SimpleNamespace(set_result=noop,scan_summary=lambda:'summary'),
            _assistant_busy=False,tree_panel=SimpleNamespace(show_result=lambda result:order.append('tree')),
            assistant_panel=SimpleNamespace(set_selected_item=noop,write=noop),_update_drive_info=noop,
            progress_bar=SimpleNamespace(stop=noop),scan_button=SimpleNamespace(configure=noop),_size_finished=noop)
        with patch.object(DriveIndex,'update_scan',side_effect=AssertionError('Must not index on UI thread')):
            SageApplication._scan_finished(app,scan({'file.txt':10}))
        self.assertEqual(order,['tree'])


class DeadlineTests(unittest.TestCase):
    def test_slow_stream_is_disconnected_at_total_deadline(self):
        disconnected=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.end_headers()
                try:
                    for _ in range(50):
                        self.wfile.write(b'{"message":{"content":"x"},"done":false}\n');self.wfile.flush();time.sleep(.02)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):disconnected.set()
        server=HTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);worker.start()
        try:
            with patch('sage.services.local_ai.OLLAMA_CHAT_URL',f'http://127.0.0.1:{server.server_port}/api/chat'):
                start=time.monotonic()
                with self.assertRaises(RuntimeError):OllamaClient().chat([],timeout_seconds=.12)
                self.assertLess(time.monotonic()-start,.6)
            self.assertTrue(disconnected.wait(.5))
        finally:server.shutdown();server.server_close();worker.join(1)


if __name__=='__main__':unittest.main()
