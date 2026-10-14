"""Immutable code cache with fresh PR metadata and requirement snapshots."""
import base64
import hashlib
import json
from pathlib import Path
import re
import sys

from .review import run_command
from .config import BUNDLED_SKILLS


def cached_code(root, job, fetch):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(json.dumps([job['repo'], job['base'], job['head'], 1]).encode()).hexdigest()
    path = root / f'{key}.json'
    if path.exists():
        return json.loads(path.read_text()), True
    value = fetch()
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)
    return value, False


def architecture_context(github, job):
    tree = github.api(f"repos/{job['repo']}/git/trees/{job['head']}?recursive=1")
    if tree.get('truncated'):
        raise ValueError('Architecture tree is incomplete')
    paths = [entry for entry in tree['tree'] if entry['type'] == 'blob' and
             (Path(entry['path']).name == 'CONTEXT.md' or
              re.search(r'(^|/)(adr|adrs|architecture)(/|\.)', entry['path'], re.I)) and entry['path'].endswith('.md')]
    if len(paths) > 40:
        raise ValueError('Architecture context exceeds 40 documents; narrow the configured source policy')
    sources = []
    for entry in paths:
        blob = github.api(f"repos/{job['repo']}/git/blobs/{entry['sha']}")
        sources.append({'path': f"https://github.com/{job['repo']}/blob/{job['head']}/{entry['path']}",
                        'content': base64.b64decode(blob['content']).decode()})
    return sources


def prepare_evidence(cfg, job, directory, github):
    current = github.pull(job['repo'], job['number'])
    if current['head']['sha'] != job['head'] or current['base']['sha'] != job['base']:
        raise ValueError('PR changed before evidence collection')
    tool = Path(cfg['agent']['skill']).parent / 'scripts/review-tools.mjs'
    path = directory / 'evidence.json'

    def fetch():
        run_command(['node', str(tool), 'collect', '--pr', current['html_url'], '--out', str(path)],
                    directory, directory / 'collect.log', 600)
        evidence = json.loads(path.read_text())
        if (evidence['pullRequest']['headSha'] != job['head'] or evidence['pullRequest']['baseSha'] != job['base']
                or evidence.get('standardsDiscoveryTruncated') or not evidence['diff']['files']):
            raise ValueError('Incomplete or changed evidence')
        if evidence['commits'] and evidence['commits'][-1]['sha'] != job['head']:
            raise ValueError('Commit list does not end at pinned head')
        diff_path = directory / 'pinned.diff'
        run_command(['gh', 'api', f"repos/{job['repo']}/compare/{job['base']}...{job['head']}",
                     '-H', 'Accept: application/vnd.github.diff'], directory, diff_path, 120)
        script = '''import {readFileSync,writeFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const [tool,file,diff,owner,repository,headSha]=process.argv.slice(2);
const {parseDiff}=await import(pathToFileURL(tool));
const e=JSON.parse(readFileSync(file,'utf8')); const raw=readFileSync(diff,'utf8');
const files=parseDiff(raw,{owner,repository,headSha});
if(files.length!==e.diff.files.length)throw new Error('Pinned diff count mismatch');
e.diff={raw,files};writeFileSync(file,JSON.stringify(e));'''
        owner, repo = job['repo'].split('/')
        run_command(['node', '--input-type=module', '-e', script, 'pin-evidence', str(tool), str(path),
                     str(diff_path), owner, repo, job['head']], directory, directory / 'pin.log', 120)
        evidence = json.loads(path.read_text())
        code = {key: evidence[key] for key in ('diff', 'commits', 'standards', 'standardsDiscoveryTruncated')}
        code['architectureContext'] = architecture_context(github, job)
        return code

    code, hit = cached_code(Path(cfg['paths']['state']) / 'cache/code', job, fetch)
    evidence = dict(code, source='github-pr', checks=github.checks(job['repo'], job['head']),
                    pullRequest={'url': current['html_url'], 'title': current['title'], 'body': current.get('body') or '',
                                 'baseRef': current['base']['ref'], 'headRef': current['head']['ref'],
                                 'baseSha': job['base'], 'headSha': job['head']})
    path.write_text(json.dumps(evidence, indent=2))
    (directory / 'cache.json').write_text(json.dumps({'code_cache_hit': hit}))
    return evidence


def adf_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(filter(None, (adf_text(item) for item in value)))
    if not isinstance(value, dict):
        return ''
    if value.get('type') == 'text':
        return value.get('text', '')
    return adf_text(value.get('content', []))


def refresh_sources(cfg, job, directory, evidence, github):
    """Requirements are always fetched, even when the code cache hits."""
    plan = {'architectureContext': evidence.get('architectureContext', [])}
    pull = evidence['pullRequest']
    text = '\n'.join([pull['title'], pull.get('headRef', ''), pull.get('body', ''),
                      *[commit['message'] for commit in evidence.get('commits', [])]])
    key = re.search(r'\b[A-Z][A-Z0-9]+-\d+\b', text)
    if key:
        command = cfg.get('sources', {}).get('jira_command',
            [sys.executable, str(BUNDLED_SKILLS / 'jira-read-ticket/scripts/read_jira_ticket.py')])
        jira_dir = directory / 'jira'
        run_command([*command, key[0], '--output-dir', str(jira_dir), '--max-chars', '0'],
                    directory, directory / 'jira-fetch.log', 120)
        snapshot = json.loads((jira_dir / key[0] / 'issue-rest.json').read_text())
        fields = snapshot['fields']
        content = [f"{key[0]}: {fields['summary']}", f"Updated: {fields.get('updated', 'not exposed')}",
                   adf_text(fields.get('description'))]
        for field, name in snapshot.get('names', {}).items():
            if field != 'description' and re.search(r'acceptance|requirements|specification', name, re.I):
                value = adf_text(fields.get(field))
                if value:
                    content += [name, value]
        plan['specification'] = {'path': f'https://eaflood.atlassian.net/browse/{key[0]}', 'content': '\n\n'.join(content)}
    else:
        issue = re.search(r'https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)|(?:closes|fixes|resolves)\s+#(\d+)', text, re.I)
        if issue:
            repo, number = (issue[1], issue[2]) if issue[1] else (job['repo'], issue[3])
            source = github.api(f'repos/{repo}/issues/{number}')
            plan['specification'] = {'path': source['html_url'], 'content': source['title'] + '\n\n' + (source.get('body') or '')}
    (directory / 'resolution-plan.json').write_text(json.dumps(plan, indent=2))
    return plan
