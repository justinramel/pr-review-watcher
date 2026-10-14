"""Slack Workflow Builder transport with sanitised errors."""
import json
from pathlib import Path
import urllib.request
from urllib.parse import urlparse


def webhook_url(path):
    url = Path(path).read_text().strip()
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'hooks.slack.com' or not parsed.path.startswith('/triggers/') or parsed.username or parsed.password or parsed.port:
        raise ValueError('Expected a Slack workflow webhook URL')
    if Path(path).stat().st_mode & 0o077:
        raise ValueError('Webhook file must be private: chmod 600 the file')
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send(path, recipient, report, pr_url):
    url = webhook_url(path)
    data = json.dumps({'recipient': recipient, 'report': report, 'pr_url': pr_url}).encode()
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        # No redirects: keep the secret trigger request on Slack's host.
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            result = json.loads(response.read(4096))
            if response.status != 200 or result.get('ok') is not True:
                raise RuntimeError('Slack rejected workflow request')
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError('Slack transport failure') from None
