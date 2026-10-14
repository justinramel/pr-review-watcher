import tempfile
import unittest
from pathlib import Path

from pr_review_watcher.state import Store
from pr_review_watcher.review import validate_result, public_report, deliveries_for, render_brief, render_result
from pr_review_watcher.config import load_config
from pr_review_watcher.slack import send
from unittest.mock import patch
import json
import urllib.error


def pr(draft=False, sha='a' * 40, author='donatasbacius'):
    return dict(number=7, draft=draft, state='open', user={'login': author},
                head={'sha': sha}, base={'sha': 'b' * 40},
                html_url='https://github.com/DEFRA/example/pull/7', title='A change')


def event(n):
    return {'id': n, 'event': 'ready_for_review'}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_first_scan_baselines_existing_pr(self):
        self.store.observe('DEFRA/example', pr(), [event(1)])
        self.assertEqual(self.store.jobs(), [])

    def test_new_pr_first_seen_after_watch_started_catches_ready_event(self):
        self.store.observe('DEFRA/example', pr(), [dict(event(1), created_at='2026-10-14T12:30:00Z')], since=1)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_ready_transition_is_durable_and_deduplicated(self):
        self.store.observe('DEFRA/example', pr(True), [])
        self.store.observe('DEFRA/example', pr(), [event(1)])
        self.store.close()
        self.store = Store(self.path)
        self.store.observe('DEFRA/example', pr(), [event(1)])
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.jobs()[0]['head'], 'a' * 40)

    def test_repeated_ready_events_coalesce_but_later_transition_requeues(self):
        self.store.observe('DEFRA/example', pr(True), [])
        self.store.observe('DEFRA/example', pr(), [event(1), event(2)])
        self.assertEqual(len(self.store.jobs()), 1)
        self.store.observe('DEFRA/example', pr(), [event(1), event(2), event(3)])
        self.assertEqual(len(self.store.jobs()), 2)

    def test_draft_transition_not_reviewed_later_without_another_ready_event(self):
        self.store.observe('DEFRA/example', pr(True), [])
        self.store.observe('DEFRA/example', pr(True), [event(1)])
        self.store.observe('DEFRA/example', pr(), [event(1)])
        self.assertEqual(self.store.jobs(), [])

    def test_interrupted_review_is_not_automatically_repeated(self):
        job = self.store.enqueue('DEFRA/example', pr(), 'manual')
        self.store.update_job(job, 'running')
        self.store.recover()
        self.assertEqual(self.store.jobs()[0]['status'], 'failed')

    def test_deliveries_are_saved_with_review_completion(self):
        job = self.store.enqueue('DEFRA/example', pr(), 'manual')
        self.store.complete(job, 'GREEN', [('me@example.com', 'report', 'https://github.com')])
        self.assertEqual(self.store.jobs()[0]['status'], 'complete')
        self.assertEqual(len(self.store.pending_deliveries()), 1)
        self.store.delivered(self.store.pending_deliveries()[0]['id'])
        self.assertEqual(self.store.pending_deliveries(), [])

    def test_long_reports_have_individually_tracked_parts(self):
        job = self.store.enqueue('DEFRA/example', pr(), 'manual')
        self.store.complete(job, 'GREEN', [('me@example.com', 'a' * 6000, 'https://github.com')])
        parts = self.store.pending_deliveries()
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p['report']) < 2800 for p in parts))
        self.store.delivered(parts[0]['id'])
        self.assertEqual(len(self.store.pending_deliveries()), len(parts) - 1)


def valid_result():
    return {'valid': True, 'mergeStatus': 'GREEN', 'mergeReady': True,
            'reviewers': [{'name': n, 'files': ['app.py'], 'verdict': 'approve',
                           'runtime': {'model': 'not exposed', 'effort': 'not exposed'}, 'findings': []}
                          for n in ['Standards', 'Spec', 'Architecture & DDD']]}


class ReviewTests(unittest.TestCase):
    def test_green_owner_notification_includes_author_comments_and_links_once(self):
        comment = {'id': 10, 'author': 'donatasbacius', 'body': 'Config only; UI is a separate PR.',
                   'url': 'https://github.com/DEFRA/example/pull/7#issuecomment-10'}
        job = {'author': 'donatasbacius', 'url': pr()['html_url'], 'head': 'a' * 40,
               'repo': 'DEFRA/example', 'number': 7, 'title': 'A change',
               'review_context': {'request': comment, 'comments': [comment,
                   {'id': 11, 'author': 'other', 'body': 'Unrelated reviewer chat'}]}}
        cfg = {'users': {'donatasbacius': {'slack_email': 'donatas@example.com'}},
               'slack': {'owner_email': 'justin@example.com'}}
        messages = deliveries_for(cfg, job, valid_result(), 'Report', current=True)
        author, owner = messages
        self.assertNotIn('Author comments', author[1])
        self.assertEqual(owner[1].count(comment['body']), 1)
        self.assertIn(comment['url'], owner[1])
        self.assertNotIn('Unrelated reviewer chat', owner[1])
        stale = deliveries_for(cfg, job, valid_result(), 'Report', current=False)
        self.assertEqual(len(stale), 1)
        self.assertNotIn('Author comments', stale[0][1])

    def test_fix_brief_keeps_every_action_without_evidence_or_boilerplate(self):
        result = valid_result()
        result.update(mergeStatus='RED', mergeReady=False, decidingRule='A long generic explanation',
                      mergeReadyReason='Blocking findings remain')
        finding = {'severity': 'major', 'location': 'config/gas.json:2',
                   'summary': 'Missing administration role', 'evidence': 'LONG SUPPORTING EVIDENCE',
                   'fix': 'Set admin.role to ROLE_WOODLAND_ADMIN in gas.json and gas.next.json.'}
        result['reviewers'][1]['findings'] = [finding]
        evidence = {'checks': {'state': 'success', 'runs': [{'name': 'lint', 'conclusion': 'success'}]}}
        brief = render_brief(result, evidence)
        self.assertIn('RED', brief)
        self.assertIn('config/gas.json:2', brief)
        self.assertIn(finding['fix'], brief)
        self.assertNotIn('LONG SUPPORTING EVIDENCE', brief)
        self.assertNotIn('Review coverage', brief)
        self.assertNotIn('A long generic explanation', brief)
        self.assertIn('LONG SUPPORTING EVIDENCE', render_result(result, evidence))

    def test_fix_brief_preserves_accepted_scope_and_summarises_resolved_items(self):
        context = {'previous_review': {'findings': [
            {'id': 'Spec:0', 'summary': 'Missing UI'}, {'id': 'Spec:1', 'summary': 'Missing role'}]}}
        feedback = [{'id': 'Spec:0', 'status': 'accepted', 'reason': 'Configuration-only PR; UI is separate.'},
                    {'id': 'Spec:1', 'status': 'resolved', 'reason': 'Role configured correctly'}]
        brief = render_brief(valid_result(), {'checks': {'state': 'success'}}, context, feedback)
        self.assertIn('accepted: Missing UI', brief)
        self.assertIn('Configuration-only PR', brief)
        self.assertIn('1 resolved', brief)
        self.assertNotIn('Role configured correctly', brief)

    def test_missing_axis_or_invalid_result_cannot_be_green(self):
        result = valid_result()
        validate_result(result)
        result['reviewers'].pop()
        with self.assertRaises(ValueError):
            validate_result(result)
        result = valid_result()
        result['valid'] = False
        with self.assertRaises(ValueError):
            validate_result(result)

    def test_public_report_omits_environment_and_tool_names(self):
        report = '## Change summary\nFine\n## Review environment\nPi / Claude\n## Verdict\nGREEN\n'
        self.assertEqual(public_report(report), '## Change summary\nFine\n## Verdict\nGREEN')
        self.assertNotIn('Codex', public_report('Codex says check this'))

    def test_only_current_green_review_notifies_owner_with_approval_link(self):
        job = {'author': 'donatasbacius', 'url': pr()['html_url'], 'head': 'a' * 40,
               'repo': 'DEFRA/example', 'number': 7, 'title': 'A change'}
        cfg = {'users': {'donatasbacius': {'slack_email': 'donatas@example.com'}},
               'slack': {'owner_email': 'justin@example.com'}}
        messages = deliveries_for(cfg, job, valid_result(), 'Report', current=True)
        self.assertEqual([m[0] for m in messages], ['donatas@example.com', 'justin@example.com'])
        self.assertTrue(messages[1][2].endswith('/files'))
        stale = deliveries_for(cfg, job, valid_result(), 'Report', current=False)
        self.assertEqual([m[0] for m in stale], ['donatas@example.com'])
        self.assertIn('changed', stale[0][1])
        result = valid_result()
        result['mergeReady'] = False
        self.assertEqual(len(deliveries_for(cfg, job, result, 'Report', current=True)), 1)

    def test_author_is_owner_gets_one_green_report_with_files_link(self):
        job = {'author': 'justin', 'url': pr()['html_url'], 'head': 'a' * 40,
               'repo': 'DEFRA/example', 'number': 7, 'title': 'A change'}
        cfg = {'users': {'justin': {'slack_email': 'justin@example.com'}},
               'slack': {'owner_email': 'justin@example.com'}}
        messages = deliveries_for(cfg, job, valid_result(), 'Report', current=True)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0][2].endswith('/files'))
        self.assertNotIn('approve manually', messages[0][1])

    def test_config_requires_explicit_users_and_valid_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'config.toml'
            p.write_text('[github]\nowner="DEFRA"\nrepositories=["../bad"]\n')
            with self.assertRaises(ValueError):
                load_config(p)

    def test_slack_errors_do_not_expose_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'webhook'
            url = 'https://hooks.slack.com/triggers/SECRET'
            p.write_text(url)
            p.chmod(0o600)
            error = urllib.error.URLError(url)
            with patch('urllib.request.OpenerDirector.open', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, '^Slack transport failure$'):
                    send(p, 'me@example.com', 'Report', 'https://github.com')
