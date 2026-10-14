"""GitHub CLI adapter. Never interpolate PR data into shell commands."""
import json
import subprocess


class GitHub:
    def api(self, endpoint, *, paginate=False, payload=None):
        args = ['gh', 'api', endpoint]
        if paginate:
            args += ['--paginate', '--slurp']
        if payload is not None:
            args += ['--method', 'POST', '--input', '-']
        result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            # Don't leak credential or response content into logs.
            raise RuntimeError(f'GitHub request failed (exit {result.returncode})')
        value = json.loads(result.stdout)
        return [item for page in value for item in page] if paginate else value

    def pulls(self, repo):
        return self.api(f'repos/{repo}/pulls?state=open&per_page=100', paginate=True)

    def pull(self, repo, number):
        return self.api(f'repos/{repo}/pulls/{number}')

    def timeline(self, repo, number):
        return self.api(f'repos/{repo}/issues/{number}/timeline?per_page=100', paginate=True)

    def comments(self, repo, number):
        return self.api(f'repos/{repo}/issues/{number}/comments?per_page=100', paginate=True)

    def checks(self, repo, head):
        try:
            runs = []
            page = 1
            while True:
                value = self.api(f'repos/{repo}/commits/{head}/check-runs?per_page=100&page={page}')
                batch = value.get('check_runs', [])
                runs += [{'name': r['name'], 'status': r['status'], 'conclusion': r.get('conclusion'),
                          'url': r.get('html_url')} for r in batch]
                if len(batch) < 100:
                    break
                page += 1
            statuses = self.api(f'repos/{repo}/commits/{head}/status').get('statuses', [])
            runs += [{'name': s['context'], 'status': 'in_progress' if s['state'] == 'pending' else 'completed',
                      'conclusion': None if s['state'] == 'pending' else 'success' if s['state'] == 'success' else 'failure',
                      'url': s.get('target_url')} for s in statuses]
            state = ('missing' if not runs else 'pending' if any(r['status'] != 'completed' for r in runs)
                     else 'success' if all(r['conclusion'] in ('success', 'neutral', 'skipped') for r in runs) else 'failure')
            return {'state': state, 'runs': runs}
        except Exception:
            return {'state': 'missing', 'runs': []}

    def assign(self, repo, number, reviewer):
        existing = self.api(f'repos/{repo}/pulls/{number}/requested_reviewers')
        if reviewer.casefold() not in {u['login'].casefold() for u in existing['users']}:
            self.api(f'repos/{repo}/pulls/{number}/requested_reviewers', payload={'reviewers': [reviewer]})
