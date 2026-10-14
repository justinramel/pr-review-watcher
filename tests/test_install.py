import importlib.util
from pathlib import Path
import tempfile
import os
import tomllib
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('installer', Path(__file__).resolve().parents[1] / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def configure(self, home, *, cookie=None):
        replies = ['my-login', 'me@example.com', 'example-org', 'repo', 'pi',
                   cookie or str(home / '.agents/secrets/eaflood.atlassian.net_cookies.txt'), 'y']
        identity = installer.subprocess.CompletedProcess([], 0, 'my-login\n', '')
        with patch.object(installer, 'ask', side_effect=replies), \
             patch.object(installer.subprocess, 'run', return_value=identity), \
             patch.object(installer.shutil, 'which', return_value='/bin/pi'), \
             patch.object(installer.getpass, 'getpass', return_value=''), \
             patch.dict(os.environ, {'HOME': str(home)}):
            result = installer.configure(home)
        config = tomllib.loads((home / '.config/pr-review-watcher/config.toml').read_text())
        return config, result

    def test_plain_install_then_configure_watches_only_authenticated_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.object(installer.subprocess, 'run'):
                installer.install(home)
            cfg, _ = self.configure(home)
            self.assertEqual(cfg['users'], {'my-login': {'enabled': True, 'slack_email': 'me@example.com'}})

    def test_relative_cookie_path_is_saved_as_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cookie = home / 'cookies.txt'
            cookie.write_text('# Netscape HTTP Cookie File\n')
            relative = os.path.relpath(cookie)
            cfg, result = self.configure(home, cookie=relative)
            self.assertEqual(result, str(cookie))
            self.assertEqual(cfg['sources']['jira_command'][-1], str(cookie))

    def test_configure_keeps_existing_mappings_secret_and_custom_jira_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home / '.config/pr-review-watcher'
            folder.mkdir(parents=True)
            cfg = installer.new_config('my-login', 'me@example.com', 'example-org', ['repo'], ['pi'])
            cfg['users']['colleague'] = {'enabled': True, 'slack_email': 'colleague@example.com'}
            cfg['sources'] = {'jira_command': ['/custom/reader', '--site', 'custom']}
            (folder / 'config.toml').write_text(installer.dump_toml(cfg))
            (folder / 'slack-webhook-url').write_text('https://hooks.slack.com/triggers/existing/private\n')
            updated, _ = self.configure(home, cookie=str(home / 'other-cookies.txt'))
            self.assertEqual(updated['users'], cfg['users'])
            self.assertEqual(updated['sources'], cfg['sources'])
            self.assertEqual((folder / 'slack-webhook-url').read_text(), 'https://hooks.slack.com/triggers/existing/private\n')

    def test_install_preserves_live_config_and_secret_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / '.config/pr-review-watcher'
            config.mkdir(parents=True)
            (config / 'config.toml').write_text('existing config')
            (config / 'slack-webhook-url').write_text('existing secret')
            with patch.object(installer.subprocess, 'run') as run:
                installer.install(home, Path(__file__).resolve().parents[1])
            self.assertEqual((config / 'config.toml').read_text(), 'existing config')
            self.assertEqual((config / 'slack-webhook-url').read_text(), 'existing secret')
            self.assertEqual([c.args[0] for c in run.call_args_list], [['systemctl', '--user', 'daemon-reload']])
            self.assertTrue((home / '.local/bin/pr-review-watcher').is_symlink())

    def test_unit_can_be_updated_and_keeps_previous_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = Path(__file__).resolve().parents[1]
            with patch.object(installer.subprocess, 'run'):
                installer.install(home, source)
                unit = home / '.config/systemd/user/pr-review-watcher.service'
                old = unit.read_text()
                unit.write_text(old.replace('RestartSec=30', 'RestartSec=60'))
                installer.install(home, source)
            self.assertEqual(unit.read_text(), old)
            self.assertIn('RestartSec=60', unit.with_suffix('.service.bak').read_text())

    def test_toml_roundtrip_preserves_custom_settings(self):
        import tomllib
        value = {'github': {'repositories': ['repo'], 'reviewer': 'test'},
                 'users': {'a-user': {'enabled': False, 'slack_email': 'a@example.com'}},
                 'sources': {'jira_command': ['/path with space/reader', '--option']}}
        self.assertEqual(tomllib.loads(installer.dump_toml(value)), value)

    def test_new_setup_uses_only_self_for_testing(self):
        cfg = installer.new_config('my-login', 'me@example.com', 'DEFRA', ['repo'], ['pi'])
        self.assertEqual(cfg['users'], {'my-login': {'enabled': True, 'slack_email': 'me@example.com'}})
        self.assertEqual(cfg['watch']['review_command'], '')
        self.assertNotIn('skill', cfg['agent'])

    def test_guided_setup_saves_private_self_only_configuration_and_webhook(self):
        import tomllib
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            replies = ['my-login', 'me@example.com', 'DEFRA', 'repo', 'pi',
                       str(home / '.agents/secrets/eaflood.atlassian.net_cookies.txt'), 'y']
            identity = installer.subprocess.CompletedProcess([], 0, 'my-login\n', '')
            with patch.object(installer, 'ask', side_effect=replies), \
                 patch.object(installer.subprocess, 'run', return_value=identity), \
                 patch.object(installer.shutil, 'which', return_value='/bin/pi'), \
                 patch.object(installer.getpass, 'getpass', return_value='https://hooks.slack.com/triggers/test/private'), \
                 patch.dict(installer.os.environ, {'HOME': str(home)}):
                installer.configure(home)
            folder = home / '.config/pr-review-watcher'
            cfg = tomllib.loads((folder / 'config.toml').read_text())
            self.assertEqual(list(cfg['users']), ['my-login'])
            for name in ('config.toml', 'slack-webhook-url'):
                self.assertEqual((folder / name).stat().st_mode & 0o777, 0o600)
            self.assertNotIn('hooks.slack.com', (folder / 'config.toml').read_text())
