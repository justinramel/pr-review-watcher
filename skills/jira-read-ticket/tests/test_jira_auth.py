import argparse
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

reader = load('jira_reader_tests', ROOT / 'jira-read-ticket/scripts/read_jira_ticket.py')

class JiraAuthenticationTests(unittest.TestCase):
    def test_malformed_cookie_values_never_appear_in_subprocess_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie = Path(tmp) / 'cookies.txt'
            marker = 'PRIVATE-COOKIE-VALUE-DO-NOT-PRINT'
            # This invalid boolean field triggers cookiejar's warning traceback,
            # which embeds cookie values on some supported Python versions.
            cookie.write_text('# Netscape HTTP Cookie File\n'
                              f'.example.com\tFALSE\t/\tFALSE\t0\tsession\t{marker}\n')
            cookie.chmod(0o600)
            result = subprocess.run([sys.executable, str(ROOT / 'jira-read-ticket/scripts/read_jira_ticket.py'),
                                     '--cookies', str(cookie), '--check-auth'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(marker, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertIn('Could not load cookies file', result.stderr)

    def reader_run(self, identity_status=200, issue_status=200, shell=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = argparse.Namespace(ticket='FGP-974', base_url='https://jira.example',
            cookies='/unused', output_dir=tmp.name, max_chars=50000, check_auth=False)
        calls = []
        def fetch(opener, url, accept):
            calls.append(url)
            if '/myself' in url:
                body = b'{"accountId":"test-account"}' if identity_status == 200 else b'{}'
                return reader.FetchResult(identity_status, url, 'application/json', body)
            if '/issue/' in url:
                if shell:
                    return reader.FetchResult(503, url, 'application/json', b'{}')
                body = b'{"key":"FGP-974","fields":{"summary":"Run the business"}}' if issue_status == 200 else b'{"errorMessages":["Issue does not exist or you do not have permission to see it."]}'
                return reader.FetchResult(issue_status, url, 'application/json', body)
            return reader.FetchResult(200, url, 'text/html', b'<html><title>Jira</title></html>')
        with patch.object(reader, 'parse_args', return_value=args), patch.object(reader, 'make_opener', return_value=object()), patch.object(reader, 'fetch', side_effect=fetch), contextlib.redirect_stdout(io.StringIO()):
            try:
                return reader.main(), '', calls
            except SystemExit as exc:
                return 1, str(exc), calls

    def test_invalid_cookies_identified_before_ticket_lookup(self):
        code, message, calls = self.reader_run(identity_status=401, issue_status=404)
        self.assertNotEqual(code, 0)
        self.assertIn('authentication', message.lower())
        self.assertIn('cookie', message.lower())
        self.assertFalse(any('/issue/' in url for url in calls))

    def test_blank_jira_shell_does_not_count_as_ticket_read(self):
        code, _, _ = self.reader_run(issue_status=404, shell=True)
        self.assertNotEqual(code, 0)

    def test_authenticated_missing_ticket_is_not_expired_login(self):
        code, message, _ = self.reader_run(issue_status=404)
        self.assertNotEqual(code, 0)
        self.assertIn('permission', message.lower())
        self.assertNotIn('expired', message.lower())

    def test_valid_ticket_read_succeeds(self):
        code, _, _ = self.reader_run()
        self.assertEqual(code, 0)

if __name__ == '__main__':
    unittest.main()
