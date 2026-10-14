import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pr_review_watcher.state import Store
from pr_review_watcher.service import Service, exclusive_lock
from test_watcher import pr, event, valid_result


class FakeGitHub:
    def __init__(self):
        self.current = pr()
        self.events = []
        self.assignments = []

    def pulls(self, repo):
        return [self.current, pr(author='someone-else')]

    def timeline(self, repo, number):
        return self.events

    def comments(self, repo, number):
        return []

    def pull(self, repo, number):
        return self.current

    def assign(self, repo, number, reviewer):
        self.assignments.append(reviewer)

    def checks(self, repo, head):
        return {'state': 'success', 'runs': []}


class ServiceTests(unittest.TestCase):
    def test_pending_review_deliveries_are_cancelled_when_author_or_repository_disabled(self):
        for change in ('author', 'repository'):
            with self.subTest(change=change):
                self.cfg['users']['donatasbacius']['enabled'] = True
                self.cfg['github']['repositories'] = ['example']
                run = self.store.enqueue('DEFRA/example', pr(), 'revoke-' + change)
                self.store.complete(run, 'RED', [('donatas@example.com', 'Private review', pr()['html_url'])])
                if change == 'author':
                    self.cfg['users']['donatasbacius']['enabled'] = False
                else:
                    self.cfg['github']['repositories'] = ['different']
                with patch('pr_review_watcher.service.send') as send, patch('pr_review_watcher.service.time.sleep'):
                    self.service.deliver()
                send.assert_not_called()
                self.assertEqual(self.store.delivery_summary()[-1]['status'], 'cancelled')

    def test_pending_author_and_owner_deliveries_do_not_use_replaced_email_addresses(self):
        run = self.store.enqueue('DEFRA/example', pr(), 'replace-addresses')
        self.store.complete(run, 'GREEN', [
            ('donatas@example.com', 'Author report', pr()['html_url']),
            ('justin@example.com', 'Approve manually', pr()['html_url'] + '/files')])
        self.cfg['users']['donatasbacius']['slack_email'] = 'new-author@example.com'
        self.cfg['slack']['owner_email'] = 'new-owner@example.com'
        with patch('pr_review_watcher.service.send') as send, patch('pr_review_watcher.service.time.sleep'):
            self.service.deliver()
        send.assert_not_called()
        self.assertEqual([d['status'] for d in self.store.delivery_summary()], ['cancelled', 'cancelled'])

    def test_pending_failure_notification_uses_owner_authorization(self):
        run = self.store.enqueue('DEFRA/example', pr(), 'failure-address')
        job = next(j for j in self.store.jobs() if j['id'] == run)
        self.store.notify_failure(job, 'justin@example.com', 'Review failed')
        self.cfg['slack']['owner_email'] = 'new-owner@example.com'
        with patch('pr_review_watcher.service.send') as send, patch('pr_review_watcher.service.time.sleep'):
            self.service.deliver()
        send.assert_not_called()
        self.assertEqual(self.store.delivery_summary()[0]['status'], 'cancelled')

    def test_new_enabled_user_gets_welcome_through_workflow_once(self):
        self.service.scan()  # Baseline existing users without messaging them.
        self.cfg['users']['new-reviewer'] = {'enabled': True, 'slack_email': 'new@example.com'}
        self.cfg['slack']['welcome_message'] = 'Hi! I review PRs for {owner_email}.'
        self.service.scan()
        with patch('pr_review_watcher.service.send') as send, patch('pr_review_watcher.service.time.sleep'):
            self.service.deliver()
            self.service.deliver()
        send.assert_called_once_with('unused', 'new@example.com',
                                     'Hi! I review PRs for justin@example.com.', 'https://github.com/pulls/inbox')
        self.assertEqual(self.service.welcomes.summary()[-1]['status'], 'accepted')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state.db')
        self.cfg = {'github': {'owner': 'DEFRA', 'repositories': ['example'], 'reviewer': 'justin'},
                    'users': {'donatasbacius': {'enabled': True, 'slack_email': 'donatas@example.com'}},
                    'slack': {'owner_email': 'justin@example.com', 'webhook_file': 'unused'},
                    'paths': {'state': str(self.root)}}
        self.gh = FakeGitHub()
        self.service = Service(self.cfg, self.store, github=self.gh)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_only_mapped_authors_are_queued_and_reviewed_once(self):
        self.service.scan()
        self.gh.events = [event(1)]
        self.service.scan()
        self.assertEqual(len(self.store.jobs()), 1)
        with patch('pr_review_watcher.service.run_review', return_value=(valid_result(), 'Fine')) as runner:
            self.service.work()
            self.service.work()
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(self.gh.assignments, ['justin'])
        self.assertEqual(len(self.store.pending_deliveries()), 2)

    def test_pending_ci_defers_review_without_spending_tokens(self):
        job = self.store.enqueue('DEFRA/example', pr(), 'manual')
        self.gh.checks = lambda repo, head: {'state': 'pending', 'runs': []}
        with patch('pr_review_watcher.service.run_review') as runner:
            self.service.work()
        runner.assert_not_called()
        self.assertEqual(self.store.jobs()[0]['status'], 'waiting_checks')
        self.assertEqual(self.gh.assignments, [])
        self.gh.checks = lambda repo, head: {'state': 'success', 'runs': []}
        self.store.defer(job, 'waiting_checks', 'ready for test', delay=0)
        with patch('pr_review_watcher.service.run_review', return_value=(valid_result(), 'Fine')) as runner:
            self.service.work()
        self.assertEqual(runner.call_count, 1)

    def test_new_request_supersedes_job_waiting_for_ci(self):
        job = self.store.enqueue('DEFRA/example', pr(), 'old')
        self.store.defer(job, 'waiting_checks', 'pending')
        new = self.store.enqueue('DEFRA/example', pr(), 'new')
        self.assertEqual([j['status'] for j in self.store.jobs()], ['superseded', 'queued'])

    def test_own_pr_runs_without_requesting_author_as_reviewer(self):
        self.cfg['github']['reviewer'] = 'donatasbacius'
        self.store.enqueue('DEFRA/example', pr(), 'manual')
        def reject_author(repo, number, reviewer):
            raise RuntimeError('Review cannot be requested from pull request author')
        self.gh.assign = reject_author
        with patch('pr_review_watcher.service.run_review', return_value=(valid_result(), 'Fine')) as runner:
            self.service.work()
        self.assertEqual(self.store.jobs()[0]['status'], 'complete')
        self.assertEqual(runner.call_count, 1)

    def test_closed_or_draft_pr_is_skipped_before_assignment(self):
        for field, value in [('draft', True), ('state', 'closed')]:
            with self.subTest(field=field):
                self.store.enqueue('DEFRA/example', pr(), field)
                self.gh.current = dict(pr(), **{field: value})
                self.service.work()
                self.assertEqual(self.gh.assignments, [])
        self.assertTrue(all(j['status'] == 'skipped' for j in self.store.jobs()))

    def test_failed_delivery_retries_without_repeating_review(self):
        job = self.store.enqueue('DEFRA/example', pr(), 'manual')
        self.store.complete(job, 'GREEN', [('donatas@example.com', 'Fine', pr()['html_url'])])
        with patch('pr_review_watcher.service.send', side_effect=RuntimeError('No connection')):
            self.service.deliver()
        self.assertEqual(self.store.jobs()[0]['status'], 'complete')
        summary = self.store.delivery_summary()[0]
        self.assertEqual(summary['status'], 'pending')
        self.assertEqual(summary['attempts'], 1)

    def test_failed_review_notifies_owner_only_and_remains_failed(self):
        self.store.enqueue('DEFRA/example', pr(), 'manual')
        with patch('pr_review_watcher.service.run_review', side_effect=ValueError('bad panel')):
            self.service.work()
        self.assertEqual(self.store.jobs()[0]['status'], 'failed')
        self.assertEqual([d['recipient'] for d in self.store.pending_deliveries()], ['justin@example.com'])

    def test_one_unavailable_repository_does_not_block_others(self):
        self.cfg['github']['repositories'] = ['broken', 'example']
        original = self.gh.pulls
        self.gh.pulls = lambda repo: (_ for _ in ()).throw(RuntimeError()) if repo.endswith('/broken') else original(repo)
        self.assertEqual(self.service.scan()['errors'], 1)
        self.gh.events = [event(1)]
        self.service.scan()
        self.assertEqual(len(self.store.jobs()), 1)

    def test_only_one_process_can_own_the_queue(self):
        with exclusive_lock(self.root):
            with self.assertRaises(RuntimeError):
                with exclusive_lock(self.root):
                    self.fail('second lock should fail')

    def test_ready_transition_during_scan_is_not_consumed_with_stale_draft(self):
        self.gh.current = pr(True)
        self.service.scan()
        def timeline(repo, number):
            self.gh.current = pr(False)
            return [event(1)]
        self.gh.timeline = timeline
        self.service.scan()
        self.assertEqual(len(self.store.jobs()), 1)

    def test_pending_green_is_cancelled_if_head_changes_before_delivery(self):
        job = self.store.enqueue('DEFRA/example', pr(), 'manual')
        self.store.complete(job, 'GREEN', [('justin@example.com', 'GREEN', pr()['html_url'] + '/files')])
        self.gh.current = pr(sha='c' * 40)
        with patch('pr_review_watcher.service.send') as sender:
            self.service.deliver()
        sender.assert_not_called()
        self.assertEqual(self.store.delivery_summary()[0]['status'], 'cancelled')

    def test_recovery_notifies_owner_about_interrupted_review(self):
        job = self.store.enqueue('DEFRA/example', pr(), 'manual')
        self.store.update_job(job, 'running')
        self.service.recover()
        self.assertEqual(self.store.jobs()[0]['status'], 'failed')
        self.assertEqual([d['recipient'] for d in self.store.pending_deliveries()], ['justin@example.com'])
