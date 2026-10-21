"""Regression checks for runtime queueing, intent and evidence validation."""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_drive_conversation import scan
from sage.services.conversation import DriveAssistant
from sage.services.local_ai import OllamaClient
from sage.services.local_database import DriveIndex


class ResponseRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.index=DriveIndex(str(Path(temp.name)/'index.sqlite3'))
        self.result=scan({'ProgramData/App/Cache/data.bin':20,'ProgramData/App/settings.json':1},drive='C:')

    def assistant(self,respond):
        return DriveAssistant(self.result,OllamaClient(transport=respond),self.index)

    def test_empty_purpose_is_valid_and_greeting_has_no_eager_drive_analytics(self):
        a=self.assistant(lambda _:None);a.client.transport=None
        def chat(messages,on_token,**kwargs):
            evidence=json.loads(messages[-1]['content'].split('Drive evidence: ',1)[1])
            self.assertNotIn('drive_overview',evidence);self.assertNotIn('largest_direct_items',evidence)
            raw='{"intent":"conversation","subject":null,"purpose":"","answer":"Hello!"}'
            on_token(raw);return {'content':raw}
        with patch.object(a.client,'ready',side_effect=AssertionError('Do not probe residence for each question')),patch.object(a.client,'chat',side_effect=chat):
            self.assertEqual(a.answer('hey'),'Hello!')

    def test_question_waits_for_warmup_then_runs_without_resubmission(self):
        a=self.assistant(lambda _:None);a.client.transport=None
        loading=threading.Event();loading.set();statuses=[]
        with patch.object(a.client,'loading',loading),patch.object(a.client,'wait_for_warm',return_value=True) as wait,\
             patch.object(a.client,'chat',return_value={'content':'{"intent":"conversation","subject":null,"purpose":"","answer":"Hello!"}'}) as chat:
            self.assertEqual(a.answer('hello',on_status=statuses.append),'Hello!')
            wait.assert_called_once();chat.assert_called_once()
        self.assertTrue(any('queued' in message for message in statuses))

    def test_normalized_named_folder_is_inspected_before_first_generation(self):
        seen=[]
        def respond(payload):
            seen.append(payload)
            evidence=json.loads(payload['messages'][-1]['content'].split('Drive evidence: ',1)[1])
            self.assertEqual(evidence['named_candidates']['inspected_items'][0]['path'],r'C:\ProgramData')
            return {'message':{'content':json.dumps({'intent':'assessment','subject':r'C:\ProgramData','purpose':'This holds shared application data.',
                'answer':r'Review `C:\ProgramData\App\Cache` in the application before removal; regeneration is not verified.'})}}
        a=self.assistant(respond)
        reply=a.answer('what can i delete in program data')
        self.assertIn(r'C:\ProgramData\App\Cache',reply)
        self.assertEqual(a.last_timings['status'],'model_answer');self.assertEqual(len(seen),1)

    def test_assessment_cannot_end_as_a_reported_size(self):
        calls=[]
        def respond(payload):
            calls.append(payload)
            if len(calls)==1:return {'message':{'content':json.dumps({'intent':'assessment','subject':r'C:\ProgramData',
                'calls':[{'name':'report_fact','args':{'field':'size','path':r'C:\ProgramData','final':True}}]})}}
            return {'message':{'content':json.dumps({'intent':'assessment','subject':r'C:\ProgramData',
                'purpose':'This holds shared application data.','answer':'Review specific contents instead of deleting the container.'})}}
        a=self.assistant(respond);reply=a.answer('What can I delete in ProgramData?')
        self.assertIn('specific contents',reply);self.assertEqual(len(calls),2)
        self.assertEqual(a.last_timings['status'],'model_answer')

    def test_failure_does_not_disable_following_question_for_twenty_seconds(self):
        calls=[]
        def respond(_):
            calls.append(1)
            if len(calls)==1:raise RuntimeError('Temporary failure')
            return {'message':{'content':'{"intent":"conversation","subject":null,"purpose":"","answer":"Ready to help."}'}}
        a=self.assistant(respond);a.answer('hello')
        self.assertEqual(a.answer('try again'),'Ready to help.');self.assertEqual(len(calls),2)

    def test_hardware_placement_has_one_startup_fallback_and_shared_options(self):
        payloads=[]
        def open_request(request,**kwargs):
            payload=json.loads(request.data);payloads.append(payload)
            if request.full_url.endswith('/show'):
                return io.BytesIO(json.dumps({'model_info':{'general.architecture':'example','example.block_count':4}}).encode())
            if payload['options']['num_gpu']==6:raise OSError('Preferred placement unavailable')
            return io.BytesIO(b'{"done":true}')
        with patch.object(OllamaClient,'_gpu_layers',{}),patch.object(OllamaClient,'_ensure_server'),\
             patch('sage.services.local_ai.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=open_request
            client=OllamaClient();client.warm([{'role':'system','content':'Test instructions'}])
            self.assertEqual([row['options']['num_gpu'] for row in payloads if 'options' in row],[6,-1])
            self.assertEqual(client._runtime_options(),{'num_ctx':8192,'num_batch':128,'num_gpu':-1})
            self.assertFalse(client.loading.is_set());self.assertTrue(client.wait_for_warm(0))

    def test_cancelled_question_does_not_wait_for_model_startup(self):
        client=OllamaClient();client.cancel()
        pending=threading.Event()
        with patch.object(client,'_warm_complete',pending):
            with self.assertRaisesRegex(RuntimeError,'cancelled'):client.wait_for_warm(60)

    def test_complete_json_at_token_limit_still_passes_full_validation(self):
        raw=json.dumps({'intent':'assessment','subject':r'C:\ProgramData','purpose':'Shared application data.',
                        'answer':'Keep the container; review specific applications before cleanup.'})
        a=self.assistant(lambda _: {'message':{'content':raw},'done_reason':'length'})
        reply=a.answer('What can I remove in ProgramData?')
        self.assertIn('review specific applications',reply)
        self.assertEqual(a.last_timings['status'],'model_answer')
        self.assertEqual(a.last_timings['model_calls'],1)
        unsafe=json.dumps({'intent':'assessment','subject':r'C:\ProgramData','purpose':'',
                           'answer':r'Delete `C:\Invented\file.exe`.'})
        a=self.assistant(lambda _: {'message':{'content':unsafe},'done_reason':'length'})
        a.answer('What can I remove in ProgramData?')
        self.assertEqual(a.last_timings['status'],'fallback')
        self.assertEqual(a.last_suggestions,[])

    def test_malformed_stream_is_replaced_once_without_mixing_visible_answers(self):
        a=self.assistant(lambda _:None);a.client.transport=None
        visible=[];resets=[];calls=[]
        broken='{"intent":"assessment","subject":"C:\\\\ProgramData","purpose":"Shared application data.","answer":"Old recommendation. '
        complete=json.dumps({'intent':'assessment','subject':r'C:\ProgramData','purpose':'Shared application data.',
                             'answer':'Review the specific contents before cleanup.'})
        def reset():resets.append(1);visible.clear()
        def chat(messages,on_token,**kwargs):
            calls.append(1)
            raw=broken if len(calls)==1 else complete
            on_token(raw)
            return {'content':raw}
        with patch.object(a.client,'chat',side_effect=chat):
            reply=a.answer('Can I remove ProgramData?',on_token=visible.append,on_reset=reset)
        self.assertEqual(len(calls),2);self.assertEqual(resets,[1])
        self.assertEqual(''.join(visible),reply)
        self.assertNotIn('Old recommendation',reply)
        self.assertEqual(a.last_timings['status'],'model_answer')
        self.assertEqual(a.history[-1]['content'],reply)

    def test_early_end_gets_one_replacement_within_same_deadline(self):
        a=self.assistant(lambda _:None);a.client.transport=None;calls=[]
        def chat(messages,on_token,**kwargs):
            calls.append(kwargs['timeout_seconds'])
            if len(calls)==1:raise RuntimeError('Local Ollama ended its response before completion.')
            return {'content':'{"intent":"conversation","subject":null,"purpose":"","answer":"Hello!"}'}
        with patch.object(a.client,'chat',side_effect=chat):
            self.assertEqual(a.answer('hello'),'Hello!')
        self.assertEqual(len(calls),2);self.assertLessEqual(calls[1],calls[0])

    def test_repeated_incomplete_format_has_bounded_repair(self):
        a=self.assistant(lambda _:None);a.client.transport=None
        with patch.object(a.client,'chat',return_value={'content':'{"intent":"conversation"'}) as chat:
            reply=a.answer('hello')
        self.assertEqual(chat.call_count,2)
        self.assertIn('response format',reply)
        self.assertNotIn('Please send the question again',reply)

    def test_active_response_can_finish_after_thirty_seconds_without_a_new_target(self):
        # Simulate elapsed time, not a thirty-second test or a faster model.
        clock=[100.0]
        class Response(io.BytesIO):
            def __iter__(self):
                clock[0]=131.0
                yield b'{"message":{"content":"complete"},"done":true}\n'
        with patch('sage.services.local_ai.time.monotonic',side_effect=lambda:clock[0]), \
             patch('sage.services.local_ai.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=Response()
            reply=OllamaClient._request({'stream':True},timeout_seconds=90)
        self.assertEqual(reply['message']['content'],'complete')
        self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'],30)

    def test_compact_context_keeps_measurements_tree_and_actual_guards(self):
        from sage.services.conversation import compact
        value={'path':r'C:\Program Files\Editor','kind':'folder','allocated_display':'1.0 GB',
               'owner_verified':False,'dependencies_verified':False,
               'cleanup_risk':{'risk':'protected','guidance':'Use the application uninstaller.'},
               'tree_evidence':{'partial':True,'files_observed':10,'coverage':'10 of 20 retained entries.'}}
        encoded=compact(value)
        self.assertEqual(encoded['allocated_display'],'1.0 GB')
        self.assertEqual(encoded['tree_evidence'],value['tree_evidence'])
        self.assertEqual(encoded['direct_action_guidance'],'Use the application uninstaller.')
        self.assertNotIn('cleanup_risk',encoded)
        # Positive verification evidence must never be removed as a default.
        value['owner_verified']=True
        self.assertTrue(compact(value)['owner_verified'])

    def test_verified_descendant_and_trailing_separator_need_no_second_generation(self):
        from sage.services.conversation import paths_verified
        a=self.assistant(lambda _:None)
        known={r'c:\programdata'}
        valid='Review `C:\\ProgramData\\App\\Cache\\` with its owning application.'
        self.assertTrue(paths_verified(valid,known,a.evidence.inspect))
        self.assertFalse(paths_verified(r'Remove `C:\ProgramData\App\Cache.bak`.',known,a.evidence.inspect))
        self.assertTrue(paths_verified(r'Review **C:\ProgramData\App\Cache**.',known,a.evidence.inspect))
        self.assertFalse(paths_verified(r'Remove **C:\ProgramData\App\Cache.bak**.',known,a.evidence.inspect))

    def test_conversation_keeps_same_total_deadline_after_thirty_seconds(self):
        a=self.assistant(lambda _:None);a.client.transport=None
        clock=[100.0]
        def chat(messages,on_token,**kwargs):
            self.assertGreater(kwargs['timeout_seconds'],30)
            clock[0]=131.0
            return {'content':'{"intent":"conversation","subject":null,"purpose":"","answer":"Hello!"}'}
        with patch('sage.services.conversation.time.monotonic',side_effect=lambda:clock[0]), \
             patch.object(a.client,'chat',side_effect=chat):
            self.assertEqual(a.answer('hello'),'Hello!')
        self.assertEqual(a.last_timings['total_seconds'],31)

    def test_loaded_short_subject_does_not_trigger_another_lookup_or_generation(self):
        result=scan({'Program Files/Editor/app.exe':100},drive='C:')
        a=DriveAssistant(result,OllamaClient(),self.index);visible=[]
        raw=json.dumps({'intent':'assessment','subject':'program files',
                        'purpose':'This folder stores installed applications.',
                        'answer':'The scanned folder includes Editor.'})
        def chat(messages,on_token,**kwargs):
            on_token(raw)
            return {'content':raw}
        with patch.object(a.client,'chat',side_effect=chat) as model, \
             patch.object(a.tools,'resolve_subject',side_effect=AssertionError('The matching tree is already inspected')):
            reply=a.answer('what is program files',on_token=visible.append)
        self.assertIn('stores installed applications',reply)
        self.assertEqual(''.join(visible),reply)
        self.assertEqual(model.call_count,1)
        self.assertEqual(a.focus,[r'C:\Program Files'])

    def test_failed_assistant_text_is_not_replayed_and_failure_report_has_no_question(self):
        seen=[]
        def respond(payload):
            seen.append(payload)
            if len(seen)==1:raise RuntimeError('Temporary runtime failure')
            return {'message':{'content':json.dumps({'intent':'conversation','subject':None,
                'purpose':'','answer':'Program Files stores installed applications.'})}}
        a=self.assistant(respond)
        failure=a.answer('Explain this private request sentinel')
        report=json.loads(Path(self.index.db_path).with_name('assistant-last-error.json').read_text(encoding='utf-8'))
        self.assertEqual(report['error'],'Temporary runtime failure')
        self.assertNotIn('private request sentinel',json.dumps(report))
        self.assertNotIn(failure,[turn['content'] for turn in a.history])
        self.assertIn('private request sentinel',a.history[-1]['content'])
        self.assertIn('stores installed applications',a.answer('what is program files'))
        self.assertNotIn(failure,[turn['content'] for turn in seen[-1]['messages']])


if __name__=='__main__':unittest.main()
