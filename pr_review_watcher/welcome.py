"""Persistent welcome outbox, independent of PR review jobs."""
from pathlib import Path
import time

DEFAULT_MESSAGE = Path(__file__).with_name('welcome.txt').read_text().strip()
DEFAULT_URL = 'https://github.com/pulls/inbox'


def welcome_message(cfg):
    template = cfg['slack'].get('welcome_message', DEFAULT_MESSAGE)
    repos = '\n'.join(f"• {cfg['github']['owner']}/{name}" for name in cfg['github']['repositories'])
    return template.replace('{owner_email}', cfg['slack']['owner_email']).replace('{repositories}', repos)


class WelcomeOutbox:
    def __init__(self, db):
        self.db = db
        self.db.execute('''CREATE TABLE IF NOT EXISTS welcomes (
            id INTEGER PRIMARY KEY, login TEXT, recipient TEXT, report TEXT, url TEXT,
            status TEXT, attempts INTEGER DEFAULT 0, next_attempt REAL DEFAULT 0,
            UNIQUE(login,recipient))''')

    def sync(self, users, report, url, *, enabled=True):
        active = {(login.casefold(), user['slack_email'].casefold())
                  for login, user in users.items() if user.get('enabled', True)}
        queued = 0
        with self.db:
            first = not self.db.execute("SELECT 1 FROM features WHERE name='welcome_users'").fetchone()
            self.db.execute("INSERT OR IGNORE INTO features VALUES ('welcome_users',?)", (time.time(),))
            for row in self.db.execute("SELECT id,login,recipient FROM welcomes WHERE status='pending'").fetchall():
                if not enabled or (row['login'], row['recipient']) not in active:
                    self.db.execute("UPDATE welcomes SET status='cancelled' WHERE id=?", (row['id'],))
            for login, recipient in active:
                previous = self.db.execute('SELECT id,status FROM welcomes WHERE login=? AND recipient=?',
                                           (login, recipient)).fetchone()
                if previous and previous['status'] != 'cancelled':
                    continue
                if not first and not enabled:
                    continue
                status = 'baseline' if first else 'pending'
                if previous:
                    self.db.execute('UPDATE welcomes SET status=?,report=?,url=?,next_attempt=0 WHERE id=?',
                                    (status, report, url, previous['id']))
                else:
                    self.db.execute('INSERT INTO welcomes (login,recipient,report,url,status) VALUES (?,?,?,?,?)',
                                    (login, recipient, report, url, status))
                queued += status == 'pending'
        return queued

    def pending(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM welcomes WHERE status='pending' AND next_attempt<=? ORDER BY id", (time.time(),))]

    def accepted(self, identifier):
        with self.db:
            self.db.execute("UPDATE welcomes SET status='accepted' WHERE id=?", (identifier,))

    def failed(self, identifier):
        with self.db:
            self.db.execute('''UPDATE welcomes SET attempts=attempts+1,
                next_attempt=? + min(3600,60 * (1 << min(attempts,6))) WHERE id=?''',
                (time.time(), identifier))

    def summary(self):
        return [dict(r) for r in self.db.execute(
            'SELECT id,login,recipient,status,attempts FROM welcomes ORDER BY id')]
