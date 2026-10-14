"""Install local files, optionally guide configuration and start the service."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tomllib

SOURCE = Path(__file__).resolve().parent


def private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    temporary.replace(path)


def dump_toml(value):
    def scalar(item):
        if isinstance(item, bool):
            return str(item).lower()
        if isinstance(item, (str, int, float, list)):
            return json.dumps(item, ensure_ascii=False)
        raise ValueError('Unsupported configuration value')
    lines = []
    def table(items, prefix=()):
        if prefix:
            lines.extend(['', '[' + '.'.join(json.dumps(k) for k in prefix) + ']'])
        for key, item in items.items():
            if not isinstance(item, dict):
                lines.append(f'{json.dumps(key)} = {scalar(item)}')
        for key, item in items.items():
            if isinstance(item, dict):
                table(item, (*prefix, key))
    table(value)
    return '\n'.join(lines).lstrip() + '\n'


def new_config(login, email, owner, repos, command):
    return {'github': {'owner': owner, 'reviewer': login, 'repositories': repos},
            'watch': {'poll_interval_seconds': 60, 'review_command': '', 'wait_for_checks': True},
            'agent': {'command': command, 'launcher': 'fixed', 'thinking': 'high', 'timeout_seconds': 3600},
            'slack': {'owner_email': email, 'report_format': 'brief',
                      'webhook_file': '~/.config/pr-review-watcher/slack-webhook-url'},
            'users': {login: {'enabled': True, 'slack_email': email}},
            'paths': {'state': '~/.local/state/pr-review-watcher'}}


def ask(label, default=''):
    answer = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    return answer or default


def configure(home):
    from pr_review_watcher.config import load_config
    from pr_review_watcher.slack import webhook_url
    target = home / '.config/pr-review-watcher/config.toml'
    existing = tomllib.loads(target.read_text()) if target.exists() else None
    example = tomllib.loads((SOURCE / 'config.example.toml').read_text())
    print('\nConfigure this machine. Enter keeps the displayed value; Ctrl-C stops setup.')
    identity = subprocess.run(['gh', 'api', 'user', '--jq', '.login'], capture_output=True, text=True)
    if identity.returncode:
        raise ValueError('GitHub login is missing. Run gh auth login first, then rerun setup.')
    detected = identity.stdout.strip()
    login = ask('Your GitHub reviewer login', existing['github']['reviewer'] if existing else detected)
    email = ask('Your Slack account email (the address in this workspace)', existing['slack']['owner_email'] if existing else '')
    owner = ask('GitHub organisation', existing['github']['owner'] if existing else 'DEFRA')
    repos = ask('Repositories, comma separated (names without organisation)',
                ','.join((existing or example)['github']['repositories'])).split(',')
    repos = [r.strip() for r in repos if r.strip()]
    default_command = (existing or {}).get('agent', {}).get('command', ['pi'])
    if not existing and not shutil.which('pi') and (home / '.pi/agent/bin/pi').is_file():
        default_command = [str(home / '.pi/agent/bin/pi')]
    command = shlex.split(ask('Pi command or executable path (quote paths containing spaces)', shlex.join(default_command)))
    if not command or not shutil.which(command[0]):
        raise ValueError('Pi executable was not found. Install it or supply its full path.')
    if '@' not in email or login.casefold() != detected.casefold():
        raise ValueError('Use a Slack email and the GitHub login currently authenticated by gh.')
    if existing:
        cfg = existing
        cfg['github'].update(owner=owner, reviewer=login, repositories=repos)
        cfg['slack']['owner_email'] = email
        cfg['agent']['command'] = command
        print('Existing author mappings and review settings will be preserved.')
    else:
        cfg = new_config(login, email, owner, repos, command)
        print('New installation: only your own PRs will be watched initially.')
    print('Enabled authors: ' + ', '.join(k for k,v in cfg['users'].items() if v.get('enabled', True)))
    print('Add other authors later in config.toml under [users.GITHUB_LOGIN].')
    print('Their slack_email must belong to the same Slack workspace as the workflow.')
    reader = SOURCE / 'skills/jira-read-ticket/scripts/read_jira_ticket.py'
    jira_command = cfg.get('sources', {}).get('jira_command')
    bundled_reader = not jira_command or (len(jira_command) > 1 and Path(jira_command[1]).expanduser().resolve() == reader)
    cookie_default = os.environ.get('JIRA_COOKIES_FILE', str(home / '.agents/secrets/eaflood.atlassian.net_cookies.txt'))
    if bundled_reader and jira_command and '--cookies' in jira_command:
        cookie_default = jira_command[jira_command.index('--cookies') + 1]
    if not bundled_reader:
        print('Existing custom Jira command is preserved; the cookie setting below applies only to the bundled reader.')
    cookie = str(Path(ask('Jira cookie file (optional; Enter keeps the displayed path)',
                          cookie_default)).expanduser().resolve())
    if Path(cookie).is_file():
        print('Jira cookie file found; setup will check authentication later.')
    else:
        print('Jira cookie file is missing; ticket-backed reviews need it before they can run.')
    if bundled_reader:
        command = list(jira_command or [sys.executable, str(reader)])
        if '--cookies' in command:
            command[command.index('--cookies') + 1] = cookie
        else:
            command += ['--cookies', cookie]
        cfg.setdefault('sources', {})['jira_command'] = command
    draft = target.with_name('config.setup.toml')
    private_write(draft, dump_toml(cfg))
    try:
        load_config(draft)
    finally:
        draft.unlink(missing_ok=True)
    print(f'\nSave configuration to {target}')
    if ask('Save these settings? y/N', 'n').lower() not in ('y','yes'):
        raise SystemExit('Setup cancelled; configuration was not changed.')
    if target.exists():
        private_write(target.with_suffix('.toml.bak'), target.read_text())
    private_write(target, dump_toml(cfg))
    webhook = Path(cfg['slack'].get('webhook_file', '~/.config/pr-review-watcher/slack-webhook-url')).expanduser()
    if webhook.exists():
        print('Existing Slack webhook will be kept unless you enter a replacement.')
    while True:
        secret = getpass.getpass('Slack workflow webhook URL (hidden; Enter keeps existing or skips): ').strip()
        if not secret:
            break
        if not re.fullmatch(r'https://hooks\.slack\.com/triggers/[^\s]+', secret):
            print('Use the workflow web request URL starting https://hooks.slack.com/triggers/.')
            continue
        private_write(webhook, secret + '\n')
        break
    if webhook.exists():
        webhook.chmod(0o600)
        webhook_url(webhook)
    return cookie if bundled_reader else None


def quote_unit(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def install(home, source=SOURCE):
    config = home / '.config/pr-review-watcher'
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = config / 'config.toml'
    launcher = source / 'pr-review-watcher'
    launcher.chmod(0o755)
    bin_dir = home / '.local/bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / 'pr-review-watcher'
    if link.is_symlink() and link.resolve() == launcher:
        pass
    elif link.exists() or link.is_symlink():
        raise ValueError('Existing launcher points elsewhere; remove it only if replacing that installation.')
    else:
        link.symlink_to(launcher)
    units = home / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    path = ':'.join(dict.fromkeys([str(bin_dir), str(Path(sys.executable).parent), *os.environ.get('PATH', '').split(':')]))
    unit = f'''[Unit]
Description=Local PR review watcher
After=network-online.target

[Service]
Type=simple
WorkingDirectory={quote_unit(source)}
ExecStart={quote_unit(sys.executable)} -m pr_review_watcher run
Environment={quote_unit('PATH=' + path)}
UMask=0077
Restart=on-failure
RestartSec=30
TimeoutStopSec=15
KillMode=control-group

[Install]
WantedBy=default.target
'''
    unit_path = units / 'pr-review-watcher.service'
    if unit_path.exists() and unit_path.read_text() != unit:
        private_write(unit_path.with_suffix('.service.bak'), unit_path.read_text())
    private_write(unit_path, unit)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    print('Configuration:', target if target.exists() else 'not configured; run python3 install.py --configure')
    print('Installed service:', unit_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configure', action='store_true', help='Guide configuration and secret entry')
    parser.add_argument('--start', action='store_true', help='Run doctor, then enable and restart the service')
    args = parser.parse_args(argv)
    os.umask(0o077)
    if sys.version_info < (3,11) or sys.platform != 'linux':
        raise ValueError('The background service requires Linux and Python 3.11+.')
    cookie = configure(Path.home()) if args.configure else None
    install(Path.home())
    if cookie and Path(cookie).expanduser().is_file():
        check = subprocess.run([sys.executable, str(SOURCE / 'skills/jira-read-ticket/scripts/read_jira_ticket.py'), '--cookies', cookie, '--check-auth'], capture_output=True)
        print('Jira authentication: ' + ('OK' if check.returncode == 0 else 'FAILED; refresh your cookie export before ticket-backed reviews.'))
    if args.start:
        subprocess.run([sys.executable, '-m', 'pr_review_watcher', 'doctor'], cwd=SOURCE, check=True)
        subprocess.run(['systemctl', '--user', 'enable', 'pr-review-watcher.service'], check=True)
        subprocess.run(['systemctl', '--user', 'restart', 'pr-review-watcher.service'], check=True)
        print('Watcher enabled and running. View: pr-review-watcher logs --follow')
    else:
        print('Service was not started or restarted. Run: pr-review-watcher doctor')
        print('Start when ready: python install.py --start')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
