"""Authorised, explicit review commands and review-round context."""
import json
from pathlib import Path
import re


def permitted(comment, allowed):
    user = comment.get('user', {})
    return user.get('type') != 'Bot' and user.get('login', '').casefold() in {name.casefold() for name in allowed}


def is_command(comment, command, allowed):
    if not command:
        return permitted(comment, allowed) and bool((comment.get('body') or '').strip())
    return permitted(comment, allowed) and bool(re.fullmatch(
        re.escape(command) + r'(?:\s+[\s\S]*)?', (comment.get('body') or '').strip()))


def comment_snapshot(comment):
    return {'id': comment['id'], 'author': comment['user']['login'],
            'body': comment.get('body', ''), 'url': comment.get('html_url', ''),
            'created_at': comment.get('created_at', '')}


def collect_context(store, github, job, allowed):
    trigger = json.loads(job['context'])['trigger'] if job.get('context') else None
    comments = [comment_snapshot(c) for c in github.comments(job['repo'], job['number']) if permitted(c, allowed)]
    context = {'request': trigger, 'comments': comments, 'previous_review': None}
    previous = store.previous_review(job['repo'], job['number'], job['id'])
    if previous and previous.get('artifacts'):
        directory = Path(previous['artifacts'])
        result = json.loads((directory / 'result.json').read_text())
        findings = []
        for reviewer in result['reviewers']:
            for index, finding in enumerate(reviewer['findings']):
                findings.append(dict(finding, id=f"{reviewer['name']}:{index}", reviewer=reviewer['name']))
        context['previous_review'] = {'run': previous['id'], 'head': previous['head'],
                                      'result': result, 'findings': findings,
                                      'report': (directory / 'verified-report.md').read_text()}
    return context
