"""Exercise the real subprocess boundary and the skill's actual aggregator offline."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from pr_review_watcher.review import run_review
from pr_review_watcher.review import run_command as real_run_command
from unittest.mock import patch
from test_watcher import valid_result


SKILL = Path(__file__).resolve().parents[1] / 'skills/pr-review/SKILL.md'


@unittest.skipUnless(SKILL.is_file() and shutil.which('node'), 'Bundled skill and Node needed')
class ProcessTests(unittest.TestCase):
    def run_fixture(self, checks='success', missing_axis=False, context=None, followup=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        skill = root / 'skill'
        (skill / 'scripts').mkdir(parents=True)
        (skill / 'SKILL.md').write_text('Fixture skill adapter')
        evidence = {'pullRequest': {'headSha': 'a' * 40, 'baseSha': 'b' * 40,
                    'title': 'Fixture change', 'body': 'Preserve behavior', 'url': 'https://github.com/test/repo/pull/7'},
                    'source': 'github-pr', 'commits': [], 'specCandidates': [], 'standards': [],
                    'checks': {'state': checks, 'runs': []},
                    'diff': {'raw': 'diff', 'files': [{'path': 'app.py', 'diff': 'a complete fixture diff'}]}}
        (root / 'fixture.json').write_text(json.dumps(evidence))
        tool = skill / 'scripts/review-tools.mjs'
        real_tool = (SKILL.parent / 'scripts/review-tools.mjs').as_uri()
        tool.write_text(f'''import {{ readFileSync, writeFileSync }} from 'node:fs';
import {{ aggregateReview, compilePanel }} from {json.dumps(real_tool)};
export {{parseDiff}} from {json.dumps(real_tool)};
if (process.argv[1].endsWith('review-tools.mjs')) {{
const args = process.argv.slice(2);
const arg = name => args[args.indexOf(name) + 1];
const evidence = JSON.parse(readFileSync({json.dumps(str(root / 'fixture.json'))}, 'utf8'));
let value;
if (args[0] === 'collect') value = evidence;
if (args[0] === 'compile-panel') value = await compilePanel({{evidence}});
if (args[0] === 'aggregate') value = aggregateReview(JSON.parse(readFileSync(arg('--input'), 'utf8')));
writeFileSync(arg('--out'), JSON.stringify(value));
}}
''')
        reviewers = valid_result()['reviewers']
        if missing_axis:
            reviewers.pop()
        data = dict(reviewers=reviewers, checks={'state': 'success', 'runs': []}, validation=[],
                    conflictingEvidence=False, securityOrDataLossRisk=False)
        fake = root / 'fake_pi.py'
        dispositions = [{'id': f['id'], 'status': 'accepted', 'reason': 'The clarified scope is config only'}
                        for f in ((context or {}).get('previous_review') or {}).get('findings', [])]
        fake.write_text(f'''import json, sys
from pathlib import Path
assert '--print' in sys.argv and '--skill' in sys.argv
Path('aggregation-input.json').write_text({json.dumps(json.dumps(data))})
Path('report.md').write_text('## Change summary\\nFixture review\\n## Review environment\\nPi fixture\\n## Verdict and merge status\\nGREEN')
{f"Path('followup.json').write_text({json.dumps(json.dumps(dispositions))})" if followup else ''}
''')
        cfg = {'agent': {'command': [sys.executable, str(fake)], 'skill': str(skill / 'SKILL.md'),
                         'timeout_seconds': 30, 'launcher': 'legacy'}}
        job = {'head': 'a' * 40, 'base': 'b' * 40, 'repo': 'test/repo', 'url': 'https://github.com/test/repo/pull/7'}
        if context:
            job['review_context'] = context
        def command(args, cwd, output, timeout, env=None):
            if args[0] == 'gh':
                Path(output).write_text('diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n')
            else:
                real_run_command(args, cwd, output, timeout, env)
        with patch('pr_review_watcher.review.run_command', side_effect=command):
            result = run_review(cfg, job, root / 'run')
        pinned = json.loads((root / 'run/evidence.json').read_text())
        self.assertIn('+new', pinned['diff']['raw'])
        if context:
            panel = json.loads((root / 'run/panel.json').read_text())
            for reviewer in panel['reviewers']:
                self.assertIn('Config only', reviewer['task'])
            self.assertEqual(json.loads((root / 'run/review-context.json').read_text()), context)
        return result

    def test_comment_scope_is_included_in_each_reviewer_brief(self):
        self.run_fixture(context={'request': {'body': '/review Config only'},
            'comments': [{'author': 'justin', 'body': '/review Config only'}], 'previous_review': None})

    def test_followup_report_records_accepted_scope_clarification(self):
        context = {'request': {'body': '/review Config only'}, 'comments': [],
                   'previous_review': {'findings': [{'id': 'Spec:0', 'reviewer': 'Spec', 'summary': 'Missing UI'}]}}
        _, report = self.run_fixture(context=context)
        self.assertIn('accepted: Missing UI', report)
        with self.assertRaises(FileNotFoundError):
            self.run_fixture(context=context, followup=False)

    def test_real_aggregator_and_subprocess_contract(self):
        result, report = self.run_fixture()
        self.assertEqual(result['mergeStatus'], 'GREEN')
        self.assertTrue(result['mergeReady'])
        self.assertIn('No findings', report)

    def test_missing_checks_cannot_be_overridden_by_pi(self):
        result, report = self.run_fixture(checks='missing')
        self.assertEqual(result['mergeStatus'], 'AMBER')
        self.assertFalse(result['mergeReady'])
        self.assertNotIn('GREEN', report)

    def test_incomplete_panel_fails_closed(self):
        with self.assertRaises(ValueError):
            self.run_fixture(missing_axis=True)
