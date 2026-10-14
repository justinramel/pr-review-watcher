from pathlib import Path
import tempfile
import unittest

from pr_review_watcher.state import Store
from pr_review_watcher.welcome import WelcomeOutbox, welcome_message


class WelcomeTests(unittest.TestCase):
    def test_welcome_includes_configured_repositories_and_clarification_example(self):
        text = welcome_message({'slack': {'owner_email': 'reviewer@example.com'},
                                'github': {'owner': 'DEFRA', 'repositories': ['one', 'two']}})
        self.assertIn('DEFRA/one', text)
        self.assertIn('DEFRA/two', text)
        self.assertIn('reviewer@example.com', text)
        self.assertIn('This PR covers only the config change.', text)
        self.assertNotIn('{repositories}', text)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state.db')
        self.addCleanup(self.store.close)
        self.outbox = WelcomeOutbox(self.store.db)
        self.users = {'justin': {'slack_email': 'justin@example.com'},
                      'donatas': {'enabled': False, 'slack_email': 'donatas@example.com'}}

    def test_baseline_existing_users_then_welcome_newly_enabled_user_once(self):
        self.assertEqual(self.outbox.sync(self.users, 'Welcome', 'https://github.com/pulls/inbox'), 0)
        self.assertEqual(self.outbox.pending(), [])
        self.users['donatas']['enabled'] = True
        self.assertEqual(self.outbox.sync(self.users, 'Welcome', 'https://github.com/pulls/inbox'), 1)
        row = self.outbox.pending()[0]
        self.assertEqual(row['recipient'], 'donatas@example.com')
        self.outbox.accepted(row['id'])
        # New object shares persisted state, as after a service restart.
        again = WelcomeOutbox(self.store.db)
        self.assertEqual(again.sync(self.users, 'Changed wording', 'https://github.com/pulls/inbox'), 0)
        self.assertEqual(again.pending(), [])

    def test_disabling_user_cancels_pending_welcome_and_reenabling_resumes_it(self):
        self.outbox.sync(self.users, 'Welcome', 'url')
        self.users['donatas']['enabled'] = True
        self.outbox.sync(self.users, 'Welcome', 'url')
        self.users['donatas']['enabled'] = False
        self.outbox.sync(self.users, 'Welcome', 'url')
        self.assertEqual(self.outbox.pending(), [])
        self.users['donatas']['enabled'] = True
        self.outbox.sync(self.users, 'Welcome', 'url')
        self.assertEqual(len(self.outbox.pending()), 1)

    def test_failures_retry_later_and_email_change_uses_new_recipient(self):
        self.outbox.sync(self.users, 'Welcome', 'url')
        self.users['donatas']['enabled'] = True
        self.outbox.sync(self.users, 'Welcome', 'url')
        row = self.outbox.pending()[0]
        self.outbox.failed(row['id'])
        self.assertEqual(self.outbox.pending(), [])
        self.assertEqual(self.outbox.summary()[-1]['attempts'], 1)
        self.users['donatas']['slack_email'] = 'new@example.com'
        self.outbox.sync(self.users, 'Custom message', 'url')
        pending = self.outbox.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['recipient'], 'new@example.com')
        self.assertEqual(pending[0]['report'], 'Custom message')

    def test_disabled_welcome_option_suppresses_new_user_messages(self):
        self.outbox.sync(self.users, 'Welcome', 'url')
        self.users['donatas']['enabled'] = True
        self.outbox.sync(self.users, 'Welcome', 'url', enabled=False)
        self.assertEqual(self.outbox.pending(), [])
