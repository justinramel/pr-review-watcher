"""Transactional queue, transition history and delivery outbox."""
import sqlite3
import json
import time
from datetime import datetime
from pathlib import Path

from .comments import is_command, comment_snapshot


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS observed (
                repo TEXT, number INTEGER, PRIMARY KEY(repo,number));
            CREATE TABLE IF NOT EXISTS scopes (
                repo TEXT, author TEXT, since REAL, PRIMARY KEY(repo,author));
            CREATE TABLE IF NOT EXISTS features (name TEXT PRIMARY KEY, since REAL);
            CREATE TABLE IF NOT EXISTS seen_comments (
                repo TEXT, number INTEGER, comment TEXT, PRIMARY KEY(repo,number,comment));
            CREATE TABLE IF NOT EXISTS events (
                repo TEXT, number INTEGER, event TEXT, PRIMARY KEY(repo,number,event));
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, repo TEXT, number INTEGER, event TEXT,
                head TEXT, base TEXT, author TEXT, url TEXT, title TEXT,
                status TEXT DEFAULT 'queued', verdict TEXT, error TEXT, artifacts TEXT,
                created REAL, updated REAL, UNIQUE(repo,number,event));
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY, job INTEGER, recipient TEXT, report TEXT,
                url TEXT, part INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                next_attempt REAL DEFAULT 0, UNIQUE(job,recipient,part));
        ''')
        if 'context' not in {row['name'] for row in self.db.execute('PRAGMA table_info(jobs)')}:
            with self.db:
                self.db.execute('ALTER TABLE jobs ADD COLUMN context TEXT')
        if 'next_attempt' not in {row['name'] for row in self.db.execute('PRAGMA table_info(jobs)')}:
            with self.db:
                self.db.execute('ALTER TABLE jobs ADD COLUMN next_attempt REAL DEFAULT 0')

    def close(self):
        self.db.close()

    def scope_since(self, repo, author):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO scopes VALUES (?,?,?)', (repo, author.casefold(), time.time()))
        return self.db.execute('SELECT since FROM scopes WHERE repo=? AND author=?',
                               (repo, author.casefold())).fetchone()['since']

    def feature_since(self, name):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO features VALUES (?,?)', (name, time.time()))
        return self.db.execute('SELECT since FROM features WHERE name=?', (name,)).fetchone()['since']

    def observe_comments(self, repo, pr, comments, *, allowed, command, since):
        with self.db:
            commands = []
            for comment in sorted(comments, key=lambda c: int(c['id'])):
                inserted = self.db.execute('INSERT OR IGNORE INTO seen_comments VALUES (?,?,?)',
                                           (repo, pr['number'], str(comment['id']))).rowcount
                created = datetime.fromisoformat(comment['created_at'].replace('Z', '+00:00')).timestamp()
                is_author = comment.get('user', {}).get('login', '').casefold() == pr['user']['login'].casefold()
                if inserted and is_author and created >= since and is_command(comment, command, allowed):
                    commands.append(comment)
            if commands and not pr['draft'] and pr['state'] == 'open':
                latest = commands[-1]
                return self._enqueue(repo, pr, f"comment:{latest['id']}",
                                     {'trigger': comment_snapshot(latest)})
        return None

    def observe(self, repo, pr, events, since=None):
        number = pr['number']
        with self.db:
            known = self.db.execute('SELECT 1 FROM observed WHERE repo=? AND number=?',
                                    (repo, number)).fetchone()
            self.db.execute('INSERT OR IGNORE INTO observed VALUES (?,?)', (repo, number))
            unseen = []
            for event in events:
                if event['event'] != 'ready_for_review':
                    continue
                cursor = self.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)',
                                         (repo, number, str(event['id'])))
                happened = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00')).timestamp() if event.get('created_at') else 0
                if cursor.rowcount and (known or (since is not None and happened >= since)):
                    unseen.append(str(event['id']))
            if unseen and not pr['draft'] and pr['state'] == 'open':
                return self._enqueue(repo, pr, unseen[-1])
        return None

    def _enqueue(self, repo, pr, event, context=None):
        now = time.time()
        inserted = self.db.execute('''INSERT OR IGNORE INTO jobs
            (repo,number,event,head,base,author,url,title,created,updated,context)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (repo, pr['number'], event, pr['head']['sha'], pr['base']['sha'],
             pr['user']['login'], pr['html_url'], pr['title'], now, now,
             json.dumps(context) if context else None)).rowcount
        if inserted:
            self.db.execute("UPDATE jobs SET status='superseded',updated=? WHERE repo=? AND number=? AND event!=? AND status IN ('queued','waiting_checks')",
                            (now, repo, pr['number'], event))
        return self.db.execute('SELECT id FROM jobs WHERE repo=? AND number=? AND event=?',
                               (repo, pr['number'], event)).fetchone()['id']

    def enqueue(self, repo, pr, event):
        with self.db:
            return self._enqueue(repo, pr, event)

    def jobs(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM jobs ORDER BY id')]

    def previous_review(self, repo, number, current_id):
        row = self.db.execute("SELECT * FROM jobs WHERE repo=? AND number=? AND id!=? AND status='complete' ORDER BY updated DESC LIMIT 1",
                              (repo, number, current_id)).fetchone()
        return dict(row) if row else None

    def queued(self):
        row = self.db.execute("SELECT * FROM jobs WHERE status IN ('queued','waiting_checks') AND next_attempt<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
        return dict(row) if row else None

    def defer(self, job, status, error, delay=30):
        with self.db:
            self.db.execute('UPDATE jobs SET status=?,error=?,updated=?,next_attempt=? WHERE id=?',
                            (status, error, time.time(), time.time() + delay, job))

    def update_job(self, job, status, error=None):
        with self.db:
            self.db.execute('UPDATE jobs SET status=?,error=?,updated=? WHERE id=?',
                            (status, error, time.time(), job))

    def pin_job(self, job, pr, artifacts):
        with self.db:
            self.db.execute('UPDATE jobs SET head=?,base=?,artifacts=?,title=? WHERE id=?',
                            (pr['head']['sha'], pr['base']['sha'], str(artifacts), pr['title'], job))

    def recover(self, owner_email=None):
        with self.db:
            interrupted = [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE status='running'")]
            self.db.execute("UPDATE jobs SET status='failed',error='interrupted; use retry',updated=? WHERE status='running'",
                            (time.time(),))
            if owner_email:
                for job in interrupted:
                    self.db.execute('INSERT OR IGNORE INTO deliveries (job,recipient,report,url,part) VALUES (?,?,?,?,-1)',
                                    (job['id'], owner_email,
                                     f"Review interrupted: {job['repo']} #{job['number']}. Check local run {job['id']} and retry it manually.", job['url']))

    def complete(self, job, verdict, deliveries):
        with self.db:
            self.db.execute("UPDATE jobs SET status='complete',verdict=?,updated=? WHERE id=?",
                            (verdict, time.time(), job))
            for recipient, report, url in deliveries:
                chunks = [report[i:i+2300] for i in range(0, len(report), 2300)]
                for part, chunk in enumerate(chunks):
                    if len(chunks) > 1:
                        chunk = f'Part {part+1}/{len(chunks)}\n' + chunk
                    self.db.execute('INSERT OR IGNORE INTO deliveries (job,recipient,report,url,part) VALUES (?,?,?,?,?)',
                                    (job, recipient, chunk, url, part))

    def pending_deliveries(self):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM deliveries WHERE status='pending' AND next_attempt<=? ORDER BY id", (time.time(),))]

    def delivered(self, delivery):
        with self.db:
            self.db.execute("UPDATE deliveries SET status='accepted' WHERE id=?", (delivery,))

    def cancel_delivery(self, delivery):
        with self.db:
            self.db.execute("UPDATE deliveries SET status='cancelled' WHERE id=?", (delivery,))

    def delivery_failed(self, delivery):
        with self.db:
            self.db.execute('''UPDATE deliveries SET attempts=attempts+1,
                next_attempt=? + min(3600,60 * (1 << min(attempts,6))) WHERE id=?''',
                (time.time(), delivery))

    def notify_failure(self, job, recipient, message):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO deliveries (job,recipient,report,url,part) VALUES (?,?,?,?,-1)',
                            (job['id'], recipient, message, job['url']))

    def delivery_summary(self):
        return [dict(row) for row in self.db.execute(
            'SELECT id,job,recipient,status,attempts FROM deliveries ORDER BY id')]
