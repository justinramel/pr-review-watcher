"""CLI and user-service entry point."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

from .config import DEFAULT_CONFIG, load_config, enabled_user
from .github import GitHub
from .service import Service, exclusive_lock
from .slack import webhook_url
from .state import Store
from .welcome import WelcomeOutbox


def configure_logging(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger = logging.getLogger('watcher')
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(root / 'watcher.log', maxBytes=5_000_000, backupCount=5)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    for destination in (handler, logging.StreamHandler()):
        destination.setFormatter(formatter)
        logger.addHandler(destination)


def doctor(cfg):
    checks = []
    for command in ('gh', 'node', cfg['agent']['command'][0]):
        checks.append((f'executable {command}', shutil.which(command) is not None))
    skill = Path(cfg['agent']['skill'])
    checks.append(('review skill', skill.is_file() and (skill.parent / 'scripts/review-tools.mjs').is_file()))
    try:
        webhook_url(cfg['slack']['webhook_file'])
        checks.append(('private Slack webhook', True))
    except Exception:
        checks.append(('private Slack webhook', False))
    gh = GitHub()
    try:
        identity = gh.api('user')['login']
        checks.append((f"GitHub identity matches {cfg['github']['reviewer']}", identity.casefold() == cfg['github']['reviewer'].casefold()))
    except Exception:
        checks.append(('GitHub authentication', False))
    for name in cfg['github']['repositories']:
        repo = f"{cfg['github']['owner']}/{name}"
        try:
            value = gh.api(f'repos/{repo}')
            perms = value.get('permissions', {})
            checks.append((repo, perms.get('push') or perms.get('triage') or perms.get('admin', False)))
        except Exception:
            checks.append((repo, False))
    for name, ok in checks:
        print(f"{'OK' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in checks) else 1


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Local PR review watcher')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('doctor', 'scan', 'once', 'run', 'status'):
        sub.add_parser(command)
    logs = sub.add_parser('logs')
    logs.add_argument('--follow', '-f', action='store_true')
    enqueue = sub.add_parser('enqueue')
    enqueue.add_argument('url')
    retry = sub.add_parser('retry')
    retry.add_argument('run', type=int)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    root = Path(cfg['paths']['state'])
    if args.command == 'doctor':
        return doctor(cfg)
    if args.command == 'logs':
        path = root / 'watcher.log'
        if not path.exists():
            print('No logs yet')
            return 0
        return subprocess.call(['tail', '-n', '80', *(['-f'] if args.follow else []), str(path)])
    configure_logging(root)
    if args.command == 'status':
        store = Store(root / 'state.db')
        try:
            print(json.dumps({'runs': store.jobs(), 'deliveries': store.delivery_summary(),
                              'welcomes': WelcomeOutbox(store.db).summary()}, indent=2))
        finally:
            store.close()
        return 0
    with exclusive_lock(root):
        store = Store(root / 'state.db')
        try:
            service = Service(cfg, store)
            if args.command == 'enqueue':
                match = re.fullmatch(r'https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)/?', args.url)
                if not match:
                    raise ValueError('Expected a GitHub pull request URL')
                owner, name, number = match.groups()
                if owner.casefold() != cfg['github']['owner'].casefold() or name not in cfg['github']['repositories']:
                    raise ValueError('Repository is not configured')
                repo = f"{cfg['github']['owner']}/{name}"
                pr = service.github.pull(repo, int(number))
                if not enabled_user(cfg, pr['user']['login']) or pr['draft'] or pr['state'] != 'open':
                    raise ValueError('PR must be ready, open and authored by an enabled mapped user')
                print('Queued run', store.enqueue(repo, pr, 'manual-' + uuid.uuid4().hex))
                return 0
            if args.command == 'retry':
                job = next((j for j in store.jobs() if j['id'] == args.run), None)
                if not job or job['status'] != 'failed':
                    raise ValueError('Only failed reviews can be retried')
                store.update_job(args.run, 'queued')
                print('Queued retry', args.run)
                return 0
            if args.command == 'scan':
                counts = service.scan()
                print(json.dumps(counts))
                return 1 if counts['errors'] else 0
            service.recover()
            def stop(*_):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop)
            while True:
                # Re-read config each cycle; paths require a process restart.
                updated = load_config(args.config)
                if updated['paths']['state'] != cfg['paths']['state']:
                    raise ValueError('State directory changed; restart the service')
                cfg = service.cfg = updated
                service.scan()
                service.deliver()
                service.work()
                service.deliver()
                if args.command == 'once':
                    return 0
                time.sleep(cfg['watch']['poll_interval_seconds'])
        except KeyboardInterrupt:
            logging.getLogger('watcher').info('watcher_stopped')
            return 0
        finally:
            store.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # CLI errors must never print an exception containing a secret URL.
        print(f'Error: {type(error).__name__}. Check configuration and local logs.', file=sys.stderr)
        sys.exit(1)
