"""Polling coordinator. The queue and delivery outbox are independent."""
from contextlib import contextmanager
import fcntl
import logging
from pathlib import Path
import time
import uuid

from .config import enabled_user
from .github import GitHub
from .review import run_review, deliveries_for, public_report
from .slack import send
from .comments import collect_context
from .welcome import WelcomeOutbox, welcome_message, DEFAULT_URL

LOG = logging.getLogger('watcher')


@contextmanager
def exclusive_lock(root):
    Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)
    with (Path(root) / 'watcher.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Watcher is already running; stop its service first') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Service:
    def __init__(self, cfg, store, *, github=None):
        self.cfg, self.store = cfg, store
        self.github = github or GitHub()
        self.welcomes = WelcomeOutbox(store.db)

    def scan(self):
        slack = self.cfg['slack']
        message = welcome_message(self.cfg)
        welcomes = self.welcomes.sync(self.cfg['users'], public_report(message),
                                     DEFAULT_URL, enabled=slack.get('welcome_enabled', True))
        if welcomes:
            LOG.info('welcome_queued count=%s', welcomes)
        counts = {'repositories': 0, 'eligible_prs': 0, 'queued': 0, 'errors': 0}
        gh = self.cfg['github']
        command_since = self.store.feature_since('review_comments')
        allowed = {login for login, user in self.cfg['users'].items() if user.get('enabled', True)} | {gh['reviewer']}
        for name in gh['repositories']:
            repo = f"{gh['owner']}/{name}"
            try:
                since = {login.casefold(): self.store.scope_since(repo, login)
                         for login, user in self.cfg['users'].items() if user.get('enabled', True)}
                for pr in self.github.pulls(repo):
                    author = pr['user']['login']
                    if not enabled_user(self.cfg, author):
                        continue
                    counts['eligible_prs'] += 1
                    timeline = self.github.timeline(repo, pr['number'])
                    comments = self.github.comments(repo, pr['number'])
                    # Refresh after the timeline read: pulls() may predate a ready transition.
                    pr = self.github.pull(repo, pr['number'])
                    job = self.store.observe(repo, pr, timeline, since=since[author.casefold()])
                    comment_job = self.store.observe_comments(repo, pr, comments,
                        allowed=allowed, command=self.cfg.get('watch', {}).get('review_command', ''),
                        since=max(command_since, since[author.casefold()]))
                    job = comment_job or job
                    if job:
                        counts['queued'] += 1
                        LOG.info('review_queued run=%s repo=%s pr=%s head=%s trigger=%s', job, repo, pr['number'], pr['head']['sha'],
                                 'comment' if comment_job else 'ready')
                counts['repositories'] += 1
            except Exception as error:
                counts['errors'] += 1
                LOG.error('scan_failed repo=%s error_type=%s', repo, type(error).__name__)
        LOG.info('scan_complete %s', counts)
        return counts

    def recover(self):
        self.store.recover(self.cfg['slack']['owner_email'])

    def work(self):
        job = self.store.queued()
        if not job:
            return False
        run = job['id']
        phase = 'checking_pr'
        try:
            allowed_repos = {f"{self.cfg['github']['owner']}/{r}" for r in self.cfg['github']['repositories']}
            if job['repo'] not in allowed_repos or not enabled_user(self.cfg, job['author']):
                self.store.update_job(run, 'skipped', 'Repository or author disabled')
                return True
            current = self.github.pull(job['repo'], job['number'])
            if current['draft'] or current['state'] != 'open':
                self.store.update_job(run, 'skipped', 'PR is draft or closed')
                LOG.info('review_skipped run=%s reason=not_ready', run)
                return True
            phase = 'waiting_for_github_checks'
            if self.cfg.get('watch', {}).get('wait_for_checks', True):
                checks = self.github.checks(job['repo'], current['head']['sha'])
                if checks['state'] in ('pending', 'missing'):
                    self.store.defer(run, 'waiting_checks', f"GitHub checks: {checks['state']}",
                                     self.cfg.get('watch', {}).get('poll_interval_seconds', 60))
                    LOG.info('review_waiting_checks run=%s repo=%s pr=%s state=%s', run, job['repo'], job['number'], checks['state'])
                    return True
            # A queued job always starts with the latest commits, pinned before evidence collection.
            directory = Path(self.cfg['paths']['state']) / 'runs' / f"{run}-{uuid.uuid4().hex[:8]}"
            self.store.pin_job(run, current, directory)
            job.update(head=current['head']['sha'], base=current['base']['sha'], title=current['title'])
            phase = 'collecting_review_context'
            allowed = {login for login, user in self.cfg['users'].items() if user.get('enabled', True)} | {self.cfg['github']['reviewer']}
            job['review_context'] = collect_context(self.store, self.github, job, allowed)
            self.store.update_job(run, 'running')
            phase = 'assigning_reviewer'
            if current['user']['login'].casefold() == self.cfg['github']['reviewer'].casefold():
                LOG.info('reviewer_assignment_skipped run=%s reason=reviewer_is_author', run)
            else:
                self.github.assign(job['repo'], job['number'], self.cfg['github']['reviewer'])
            LOG.info('review_started run=%s repo=%s pr=%s head=%s artifacts=%s',
                     run, job['repo'], job['number'], job['head'], directory)
            phase = 'running_review'
            result, report = run_review(self.cfg, job, directory)
            phase = 'checking_review_freshness'
            latest = self.github.pull(job['repo'], job['number'])
            unchanged = (latest['head']['sha'] == job['head'] and latest['base']['sha'] == job['base']
                         and not latest['draft'] and latest['state'] == 'open')
            messages = deliveries_for(self.cfg, job, result, report, current=unchanged)
            self.store.complete(run, result['mergeStatus'], messages)
            LOG.info('review_complete run=%s verdict=%s current=%s', run, result['mergeStatus'], unchanged)
        except Exception as error:
            kind = type(error).__name__
            self.store.update_job(run, 'failed', f'{phase}: {kind}; inspect artifacts and logs; use retry')
            self.store.notify_failure(job, self.cfg['slack']['owner_email'],
                f"Review could not complete: {job['repo']} #{job['number']}. Check the local logs for run {run}. No approval was submitted.")
            LOG.error('review_failed run=%s repo=%s pr=%s phase=%s error_type=%s', run, job['repo'], job['number'], phase, kind)
        return True

    def deliver(self):
        for welcome in self.welcomes.pending():
            user = enabled_user(self.cfg, welcome['login'])
            if not self.cfg['slack'].get('welcome_enabled', True) or not user or user['slack_email'].casefold() != welcome['recipient']:
                continue
            try:
                send(self.cfg['slack']['webhook_file'], welcome['recipient'], welcome['report'], welcome['url'])
                self.welcomes.accepted(welcome['id'])
                LOG.info('welcome_accepted delivery=%s login=%s recipient=%s', welcome['id'], welcome['login'], welcome['recipient'])
            except Exception as error:
                self.welcomes.failed(welcome['id'])
                LOG.error('welcome_failed delivery=%s login=%s error_type=%s', welcome['id'], welcome['login'], type(error).__name__)
            time.sleep(1.1)
        jobs = {job['id']: job for job in self.store.jobs()}
        for delivery in self.store.pending_deliveries():
            try:
                job = jobs[delivery['job']]
                user = enabled_user(self.cfg, job['author'])
                allowed_repos = {f"{self.cfg['github']['owner']}/{name}" for name in self.cfg['github']['repositories']}
                owner_message = delivery['part'] == -1 or (job['verdict'] == 'GREEN' and delivery['url'].endswith('/files'))
                recipient = self.cfg['slack']['owner_email'] if owner_message else user['slack_email'] if user else None
                if (not user or job['repo'] not in allowed_repos or not recipient
                        or recipient.casefold() != delivery['recipient'].casefold()):
                    self.store.cancel_delivery(delivery['id'])
                    LOG.info('slack_cancelled_scope run=%s delivery=%s', job['id'], delivery['id'])
                    continue
                if delivery['url'].endswith('/files') and job['verdict'] == 'GREEN':
                    latest = self.github.pull(job['repo'], job['number'])
                    if (latest['head']['sha'] != job['head'] or latest['base']['sha'] != job['base']
                            or latest['draft'] or latest['state'] != 'open'):
                        self.store.cancel_delivery(delivery['id'])
                        LOG.info('slack_cancelled_stale_green run=%s delivery=%s', job['id'], delivery['id'])
                        continue
                send(self.cfg['slack']['webhook_file'], delivery['recipient'], delivery['report'], delivery['url'])
                self.store.delivered(delivery['id'])
                LOG.info('slack_accepted run=%s delivery=%s recipient=%s', delivery['job'], delivery['id'], delivery['recipient'])
            except Exception as error:
                self.store.delivery_failed(delivery['id'])
                LOG.error('slack_failed run=%s delivery=%s error_type=%s', delivery['job'], delivery['id'], type(error).__name__)
            # Workflow webhook limit: one request per second.
            time.sleep(1.1)
