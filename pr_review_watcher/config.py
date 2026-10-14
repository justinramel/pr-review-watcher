"""User-editable TOML configuration; secrets remain in separate files."""
import re
import tomllib
from pathlib import Path

DEFAULT_CONFIG = Path.home() / '.config/pr-review-watcher/config.toml'
BUNDLED_SKILLS = Path(__file__).resolve().parents[1] / 'skills'


def load_config(path=DEFAULT_CONFIG):
    with Path(path).expanduser().open('rb') as stream:
        cfg = tomllib.load(stream)
    gh = cfg.get('github', {})
    names = [gh.get('owner', ''), *gh.get('repositories', []), gh.get('reviewer', '')]
    if not gh.get('repositories') or any(not isinstance(n, str) or not re.fullmatch(r'[\w.-]+', n) or n in ('.', '..') for n in names):
        raise ValueError('Configure a GitHub owner, reviewer and repository names')
    if not cfg.get('users') or not cfg.get('slack', {}).get('owner_email'):
        raise ValueError('Configure users and slack.owner_email')
    for login, user in cfg['users'].items():
        if not re.fullmatch(r'[\w-]+', login) or '@' not in user.get('slack_email', ''):
            raise ValueError('Each user needs a GitHub login and Slack email')
    agent = cfg.setdefault('agent', {})
    agent.setdefault('command', ['pi'])
    if not isinstance(agent['command'], list) or not agent['command'] or not all(isinstance(x, str) and x for x in agent['command']):
        raise ValueError('agent.command must be a nonempty array of arguments')
    agent.setdefault('skill', str(BUNDLED_SKILLS / 'pr-review/SKILL.md'))
    agent.setdefault('timeout_seconds', 3600)
    agent.setdefault('thinking', 'high')
    agent.setdefault('launcher', 'fixed')
    if agent['launcher'] not in ('fixed', 'legacy'):
        raise ValueError('agent.launcher must be fixed or legacy')
    if agent['thinking'] not in ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('Unsupported thinking level')
    watch = cfg.setdefault('watch', {})
    watch.setdefault('poll_interval_seconds', 60)
    watch.setdefault('review_command', '')
    watch.setdefault('wait_for_checks', True)
    if type(watch['wait_for_checks']) is not bool:
        raise ValueError('watch.wait_for_checks must be true or false')
    if not isinstance(watch['review_command'], str) or (watch['review_command'] and not re.fullmatch(r'/[A-Za-z][\w-]*', watch['review_command'])):
        raise ValueError('watch.review_command must be empty or a slash command such as /review')
    cfg['slack'].setdefault('report_format', 'brief')
    cfg['slack'].setdefault('welcome_enabled', True)
    if type(cfg['slack']['welcome_enabled']) is not bool:
        raise ValueError('slack.welcome_enabled must be true or false')
    if 'welcome_message' in cfg['slack'] and (not isinstance(cfg['slack']['welcome_message'], str)
                                            or not cfg['slack']['welcome_message'].strip()
                                            or len(cfg['slack']['welcome_message']) > 2300):
        raise ValueError('slack.welcome_message must be nonempty text up to 2300 characters')
    if cfg['slack']['report_format'] not in ('brief', 'full'):
        raise ValueError('slack.report_format must be brief or full')
    if watch['poll_interval_seconds'] < 10 or agent['timeout_seconds'] < 30:
        raise ValueError('Poll interval must be >=10s and review timeout >=30s')
    cfg.setdefault('paths', {}).setdefault('state', '~/.local/state/pr-review-watcher')
    cfg['slack'].setdefault('webhook_file', '~/.config/pr-review-watcher/slack-webhook-url')
    for group, key in [('paths', 'state'), ('slack', 'webhook_file'), ('agent', 'skill')]:
        cfg[group][key] = str(Path(cfg[group][key]).expanduser().resolve())
    return cfg


def enabled_user(cfg, login):
    return next((u for name, u in cfg['users'].items()
                 if name.casefold() == login.casefold() and u.get('enabled', True)), None)
