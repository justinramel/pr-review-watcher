import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pr_review_watcher.state import Store
from pr_review_watcher.service import Service
from pr_review_watcher.review import validate_followup
from test_watcher import pr, valid_result
from test_service import FakeGitHub


def comment(number, body='/review', author='donatasbacius', created='2026-10-14T13:00:00Z'):
    return {'id': number, 'body': body, 'user': {'login': author, 'type': 'User'},
            'html_url': f'https://github.com/DEFRA/example/pull/7#issuecomment-{number}', 'created_at': created}


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state.db')
        self.addCleanup(self.store.close)

    def observe(self, comments, current=None, since=0):
        return self.store.observe_comments('DEFRA/example', current or pr(), comments,
            allowed={'donatasbacius', 'justin'}, command='/review', since=since)

    def test_new_command_queues_without_any_new_commit(self):
        job = self.observe([comment(1, '/review This is config only. UI is delivered separately.')])
        self.assertIsNotNone(job)
        self.assertEqual(self.store.jobs()[0]['head'], 'a' * 40)
        context = json.loads(self.store.jobs()[0]['context'])
        self.assertIn('config only', context['trigger']['body'])

    def test_any_comment_mode_only_accepts_the_pr_author(self):
        self.store.observe_comments('DEFRA/example', pr(), [comment(1, 'Ticket updated', 'justin')],
            allowed={'donatasbacius', 'justin'}, command='', since=0)
        self.assertEqual(self.store.jobs(), [])
        job = self.store.observe_comments('DEFRA/example', pr(), [comment(2, 'Ticket updated')],
            allowed={'donatasbacius', 'justin'}, command='', since=0)
        self.assertIsNotNone(job)

    def test_ordinary_embedded_and_unauthorised_commands_do_not_trigger(self):
        self.observe([comment(1, 'Thanks'), comment(2, 'Please type /review'),
                      comment(3, '/review', 'stranger'), comment(4, '/reviewer'),
                      dict(comment(5), user={'login': 'justin', 'type': 'Bot'})])
        self.assertEqual(self.store.jobs(), [])

    def test_old_comments_are_baselined_and_edits_do_not_trigger(self):
        self.observe([comment(1)], since=9_999_999_999)
        self.observe([comment(1)])
        self.observe([comment(2, 'Ordinary comment')])
        self.observe([comment(2, '/review')])
        self.assertEqual(self.store.jobs(), [])

    def test_multiple_commands_coalesce_into_one_queued_run(self):
        self.observe([comment(1), comment(2, '/review Use the scope above')])
        self.observe([comment(1), comment(2), comment(3)])
        queued = [job for job in self.store.jobs() if job['status'] == 'queued']
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]['event'], 'comment:3')

    def test_command_during_active_review_queues_one_followup(self):
        first = self.observe([comment(1)])
        self.store.update_job(first, 'running')
        self.observe([comment(1), comment(2)])
        self.assertEqual([j['status'] for j in self.store.jobs()], ['running', 'queued'])

    def test_repeated_polling_does_not_repeat_command(self):
        self.observe([comment(1)])
        self.observe([comment(1)])
        self.assertEqual(len(self.store.jobs()), 1)

    def test_draft_comment_is_consumed_without_starting_review(self):
        self.observe([comment(1)], current=pr(True))
        self.observe([comment(1)])
        self.assertEqual(self.store.jobs(), [])

    def test_followup_must_account_for_each_previous_finding(self):
        context = {'previous_review': {'findings': [{'id': 'Spec:0', 'summary': 'Missing UI'}]}}
        valid = [{'id': 'Spec:0', 'status': 'accepted', 'reason': 'UI is outside this PR scope'}]
        self.assertEqual(validate_followup(context, valid), valid)
        for invalid in ([], valid + valid, [{'id': 'Spec:0', 'status': 'ignore', 'reason': 'Because'}]):
            with self.assertRaises(ValueError):
                validate_followup(context, invalid)

    def test_green_cannot_have_still_open_previous_feedback(self):
        context = {'previous_review': {'findings': [{'id': 'Spec:0', 'summary': 'Missing UI'}]}}
        feedback = [{'id': 'Spec:0', 'status': 'still-open', 'reason': 'Requirement remains unmet'}]
        with self.assertRaises(ValueError):
            validate_followup(context, feedback, result=valid_result())


class CommentServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state.db')
        self.addCleanup(self.store.close)
        self.cfg = {'github': {'owner': 'DEFRA', 'repositories': ['example'], 'reviewer': 'justin'},
                    'users': {'donatasbacius': {'slack_email': 'donatas@example.com'}},
                    'slack': {'owner_email': 'justin@example.com'},
                    'watch': {'review_command': '/review'}, 'paths': {'state': str(self.root)}}
        self.gh = FakeGitHub()
        self.gh.messages = []
        self.gh.comments = lambda repo, number: self.gh.messages
        self.service = Service(self.cfg, self.store, github=self.gh)

    def test_scan_detects_new_command_but_not_historical_comment(self):
        self.gh.messages = [comment(1, created='2000-01-01T00:00:00Z')]
        self.service.scan()
        self.assertEqual(self.store.jobs(), [])
        self.gh.messages += [comment(2, '/review Config only', created='2099-01-01T00:00:00Z')]
        self.service.scan()
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.jobs()[0]['event'], 'comment:2')

    def test_context_and_previous_findings_reach_review_runner(self):
        old = self.store.enqueue('DEFRA/example', pr(), 'previous')
        artifacts = self.root / 'previous'
        artifacts.mkdir()
        (artifacts / 'result.json').write_text(json.dumps(valid_result()))
        (artifacts / 'verified-report.md').write_text('Earlier findings')
        self.store.pin_job(old, pr(), artifacts)
        self.store.complete(old, 'RED', [])
        self.store.observe_comments('DEFRA/example', pr(), [comment(5, '/review Config only')],
                                   allowed={'donatasbacius'}, command='/review', since=0)
        self.gh.messages = [comment(5, '/review Config only'), comment(6, '/review malicious', 'stranger')]
        with patch('pr_review_watcher.service.run_review', return_value=(valid_result(), 'Fine')) as runner:
            self.service.work()
        context = runner.call_args.args[1]['review_context']
        self.assertIn('Config only', context['request']['body'])
        self.assertEqual(len(context['comments']), 1)
        self.assertEqual(context['previous_review']['head'], 'a' * 40)
        self.assertEqual(context['previous_review']['report'], 'Earlier findings')
