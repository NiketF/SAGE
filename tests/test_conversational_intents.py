"""Unit tests for greeting, off-topic, non-existent file handling, and suggestion intent filtering."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage.models import FileRecord, VolumeInfo
from sage.services.conversation import DriveAssistant
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services.local_ai import OllamaClient
from sage.services.local_database import DriveIndex


def make_scan(names, drive='D:'):
    records = {5: FileRecord(5, 5, '.', 16)}
    paths = {'': 5}
    for name, size in names.items():
        parts = name.rstrip('/').split('/')
        for n, part in enumerate(parts):
            path = '/'.join(parts[:n+1])
            if path in paths:
                continue
            identity = len(paths) + 5
            folder = n < len(parts) - 1 or name.endswith('/')
            paths[path] = identity
            records[identity] = FileRecord(
                identity, paths['/'.join(parts[:n])], part, 16 if folder else 0,
                size, size, size_ready=not folder, modified=100
            )
    result = build_scan_result(VolumeInfo(drive, 'Synthetic', 'NTFS'), records)
    calculate_directory_sizes(result)
    return result


class ConversationalIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scan_result = make_scan({
            'Games/Kingdom Come/game.exe': 50000000,
            'Games/photo.jpg': 100000,
            'Documents/report.docx': 20000,
        }, drive='D:')
        self.transport_calls = []

        def transport(payload):
            self.transport_calls.append(payload)
            return {'message': {'content': json.dumps({
                'purpose': 'Game files for Kingdom Come.',
                'answer': 'This application can be uninstalled safely if no longer played.'
            })}}

        self.client = OllamaClient(transport=transport)
        self.index = DriveIndex(str(Path(self.temp.name) / 'index.sqlite3'))
        self.assistant = DriveAssistant(self.scan_result, self.client, self.index)

    def test_greetings_reply_directly_without_folder_dumps_or_suggestions(self):
        greetings = ['hi', 'hii', 'hello SAGE', 'good morning', 'hey there']
        for g in greetings:
            with self.subTest(greeting=g):
                reply = self.assistant.answer(g, (r'D:\Games',))
                self.assertIn('Hello! I am SAGE', reply)
                self.assertIn('drive D:', reply)
                # Must not recite folder data
                self.assertNotIn('game.exe', reply)
                self.assertNotIn('photo.jpg', reply)
                # Model was not called for pure greeting
                self.assertEqual(len(self.transport_calls), 0)
                # No uninstall suggestions attached
                self.assertEqual(self.assistant.last_suggestions, [])

    def test_off_topic_and_vulgar_queries_are_politely_declined(self):
        off_topic_queries = [
            'boobs or ass?',
            'tell me a joke',
            'what is the capital of France',
            'write a poem about flowers',
        ]
        for query in off_topic_queries:
            with self.subTest(query=query):
                self.transport_calls.clear()
                reply = self.assistant.answer(query, (r'D:\Games',))
                self.assertIn('storage and drive analysis assistant', reply)
                self.assertNotIn('game.exe', reply)
                self.assertNotIn('Games folder contains', reply)
                self.assertEqual(len(self.transport_calls), 0)
                self.assertEqual(self.assistant.last_suggestions, [])

    def test_non_existent_explicit_path_cleanly_reported(self):
        reply = self.assistant.answer(r'Can I delete D:\DoesNotExist\file.txt?', (r'D:\Games',))
        self.assertIn(r'`D:\DoesNotExist\file.txt` does not exist on this drive.', reply)
        # Must not fall back to describing D:\Games
        self.assertNotIn('Kingdom Come', reply)
        self.assertEqual(self.assistant.last_suggestions, [])

    def test_non_existent_named_file_cleanly_reported(self):
        reply = self.assistant.answer('Can I delete fake_document.pdf?', (r'D:\Games',))
        self.assertIn('`fake_document.pdf` does not exist on this drive.', reply)
        self.assertNotIn('Kingdom Come', reply)
        self.assertEqual(self.assistant.last_suggestions, [])

    def test_existing_named_file_in_selected_folder_is_inspected(self):
        reply = self.assistant.answer('Can I delete photo.jpg?', (r'D:\Games',))
        self.assertNotIn('does not exist', reply)
        self.assertEqual(self.assistant.focus, [r'D:\Games\photo.jpg'])

    def test_suggestions_not_attached_on_pure_fact_queries(self):
        # User asking about size or content without cleanup intent
        with patch.object(self.assistant, '_detect_installed_apps', return_value=[{'dummy': 1}]):
            self.assistant.answer('How big is this folder?', (r'D:\Games',))
            self.assertEqual(self.assistant.last_suggestions, [])

    def test_suggestions_attached_on_cleanup_queries(self):
        with patch.object(self.assistant, '_detect_installed_apps', return_value=['suggestion']):
            self.assistant.answer('what can i delete here?', (r'D:\Games',))
            self.assertEqual(self.assistant.last_suggestions, ['suggestion'])

    def test_asking_about_outside_folder_overrides_selection(self):
        c_scan = make_scan({
            'Fraps/fraps.exe': 1000,
            'Games/game.exe': 2000,
            'Program Files/App/app.exe': 3000,
            'ProgramData/Data/cache.bin': 4000,
        }, drive='C:')
        assistant = DriveAssistant(c_scan, self.client, self.index)

        # 1. Active selection is ProgramData, but user asks about Program Files
        assistant.answer('can i delete program files?', (r'C:\ProgramData',))
        self.assertEqual(assistant.focus, [r'C:\Program Files'])

        # 2. Active selection is Program Files, user asks about Fraps
        reply = assistant.answer('what is inside Fraps?', (r'C:\Program Files',))
        self.assertEqual(assistant.focus, [r'C:\Fraps'])
        self.assertNotIn('storage and drive analysis assistant', reply)

        # 3. Active selection is Fraps, user asks about Games
        reply = assistant.answer('what is inside games can i delete it?', (r'C:\Fraps',))
        self.assertEqual(assistant.focus, [r'C:\Games'])
        self.assertNotIn('Program Files', reply)

    def test_outside_folder_query_does_not_suggest_apps_from_previous_selection(self):
        c_scan = make_scan({
            'PerfLogs/log.txt': 100,
            'Program Files (x86)/App/app.exe': 50000000,
        }, drive='C:')
        assistant = DriveAssistant(c_scan, self.client, self.index)

        def mock_detect(targets):
            if any('Program Files' in t for t in targets):
                return ['app_in_program_files']
            return []

        with patch.object(assistant, '_detect_installed_apps', side_effect=mock_detect):
            assistant.answer('can i delete perflogs', (r'C:\Program Files (x86)',))
            self.assertEqual(assistant.focus, [r'C:\PerfLogs'])
            self.assertEqual(assistant.last_suggestions, [])

    def test_size_number_tokens_do_not_match_git_object_folders(self):
        """'50 gb' must not pick up .git/objects/50 as the target."""
        git_scan = make_scan({
            '.git/objects/50/abcdef': 100,
            'Documents/report.docx': 5000,
        }, drive='D:')
        assistant = DriveAssistant(git_scan, self.client, self.index)
        # The query contains "50" from "50 gb" - must not resolve to .git/objects/50
        assistant.answer('i need around 50 gb space free here what should i delete?', ())
        focused = assistant.focus
        for p in focused:
            self.assertNotIn('.git', p.casefold(),
                             msg=f"Git internal folder should not be targeted: {p}")

    def test_number_only_phrase_never_resolves_as_folder(self):
        """A query like 'free 100 mb' must not try to target a folder named '100'."""
        num_scan = make_scan({
            '100/some_data.bin': 500,
            'Downloads/video.mp4': 200,
        }, drive='D:')
        assistant = DriveAssistant(num_scan, self.client, self.index)
        assistant.answer('i need to free 100 mb of space', ())
        # Should not have resolved the folder named '100'
        for p in assistant.focus:
            self.assertFalse(
                p.endswith('\\100') or '\\100\\' in p,
                msg=f"Pure number folder should not be a target: {p}"
            )

    def test_cleanup_query_injects_folder_evidence_in_context(self):
        """When user asks what to delete, context should include largest_items_in_folder."""
        large_scan = make_scan({
            'Games/Kingdom Come/cache/tmp.bin': 80_000_000,
            'Games/Kingdom Come/game.exe': 50_000_000,
        }, drive='D:')
        assistant = DriveAssistant(large_scan, self.client, self.index)
        assistant.answer('what can i delete here?', (r'D:\Games\Kingdom Come',))
        ctx = assistant.last_context
        # Context should have largest_items_in_folder to help the model make recommendations
        self.assertIn('largest_items_in_folder', ctx,
                      msg="cleanup query should populate largest_items_in_folder in context")
        # The largest items should reflect actual children
        paths_in_ctx = [item['path'] for item in ctx['largest_items_in_folder']]
        self.assertTrue(any('cache' in p.lower() or 'game' in p.lower() for p in paths_in_ctx),
                        msg=f"Expected game/cache sub-items in context, got: {paths_in_ctx}")


    def test_sensitive_hate_query_handled_with_utmost_care_without_folder_match(self):
        android_scan = make_scan({
            'AppData/Local/Android/Sdk/sources/android-36.1/android/app/people/PeopleSpaceTile.java': 24000,
            'AppData/Local/Android/Sdk/sources/android-36.1/android/app/people/ConversationStatus.java': 12000,
        }, drive='C:')
        assistant = DriveAssistant(android_scan, self.client, self.index)
        reply = assistant.answer('black people are bad', ())
        self.assertEqual(assistant.focus, [])
        self.assertEqual(len(self.transport_calls), 0)
        self.assertIn('storage and drive analysis assistant', reply)
        self.assertIn('do not support discrimination', reply.lower())
        self.assertNotIn('PeopleSpaceTile', reply)
        self.assertNotIn('android\\app\\people', reply.lower())

    def test_same_basename_folders_resolve_to_shallowest_root_folder(self):
        recovery_scan = make_scan({
            'Recovery/Customizations/file.bin': 50000000,
            'Windows/Panther/Rollback/EFI/Microsoft/Recovery/BCD': 50000,
        }, drive='C:')
        assistant = DriveAssistant(recovery_scan, self.client, self.index)
        assistant.answer('what does this recovery actually do?', ())
        self.assertEqual(assistant.focus, [r'C:\Recovery'])

    def test_what_is_oem_folder_is_recognized_and_inspected(self):
        oem_scan = make_scan({
            'OEM/recovery.img': 280000000,
            'Program Files/app.exe': 10000000,
        }, drive='C:')
        assistant = DriveAssistant(oem_scan, self.client, self.index)
        reply = assistant.answer('what is OEM', ())
        self.assertEqual(assistant.focus, [r'C:\OEM'])
        self.assertNotIn('storage and drive analysis assistant', reply)


    def test_conversational_followup_question_and_positive_affirmation_flow(self):
        c_scan = make_scan({
            'Recovery/Customizations/file.bin': 50000000,
            'Downloads/setup.exe': 100000000,
            'Temp/cache.tmp': 20000000,
        }, drive='C:')
        def unavailable(_): raise RuntimeError('Model unavailable')
        client = OllamaClient(transport=unavailable)
        assistant = DriveAssistant(c_scan, client, self.index)

        # 1. Informational query about C:\Recovery
        reply1 = assistant.answer('what is recovery?', ())
        self.assertIn('Recovery', reply1)
        self.assertIn('Protected', reply1)
        # SAGE should ask a follow-up question
        self.assertIn('?', reply1)
        # SAGE should NOT dump cleanup candidates proactively
        self.assertNotIn('setup.exe', reply1)

        # 2. User responds affirmatively ("yes")
        reply2 = assistant.answer('yes', ())
        # Should now provide suggestions because user confirmed
        self.assertIn(r'Oops, `C:\Recovery` is protected and has nothing safe to delete directly', reply2)
        self.assertIn('suggestions to free up space', reply2.lower())

    def test_folder_with_nothing_to_delete_gives_alternative_suggestions_on_yes(self):
        c_scan = make_scan({
            'MyFolder/important.docx': 1000000,
            'Downloads/big_installer.exe': 500000000,
        }, drive='C:')
        def unavailable(_): raise RuntimeError('Model unavailable')
        client = OllamaClient(transport=unavailable)
        assistant = DriveAssistant(c_scan, client, self.index)

        # 1. Ask what is in MyFolder
        reply1 = assistant.answer('what is in MyFolder?', ())
        self.assertIn('important.docx', reply1)
        self.assertIn('?', reply1)

        # 2. User confirms with positive affirmation ("sure")
        reply2 = assistant.answer('sure', ())
        self.assertIn(r'Oops, `C:\MyFolder` has nothing safe to delete directly', reply2)
        self.assertIn('suggestions to free up space', reply2.lower())
        self.assertIn('No file was changed', reply2)

if __name__ == '__main__':
    unittest.main()
