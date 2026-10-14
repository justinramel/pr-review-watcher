import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pr_review_watcher.runtime import run_fixed_review
from pr_review_watcher.evidence import cached_code

SKILL = Path(__file__).resolve().parents[1] / 'skills/pr-review/SKILL.md'


class RuntimeTests(unittest.TestCase):
    def test_fixed_workers_use_fresh_sessions_and_real_aggregation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fake=root/'worker.py'
            fake.write_text('''import os,json,sys
from pathlib import Path
assert '--no-extensions' in sys.argv and '--no-skills' in sys.argv and '--no-mcp' in sys.argv
assert '--system-prompt' in sys.argv and '--continue' not in sys.argv
axis=os.environ['REVIEW_AXIS_NAME']
Path(os.environ['REVIEW_OUTPUT_PATH']).write_text(json.dumps({'name':axis,'files':['app.py'],'verdict':'approve','runtime':{'model':'not exposed','effort':'not exposed'},'findings':[]}))
''')
            cfg={'agent':{'command':[sys.executable,str(fake)],'skill':str(SKILL),'timeout_seconds':30,'thinking':'high'},
                 'paths':{'state':str(root)},'slack':{'report_format':'brief'}}
            evidence={'source':'github-pr','pullRequest':{'url':'https://github.com/test/repo/pull/1','title':'A change',
                      'body':'Do the change','headSha':'a'*40,'baseSha':'b'*40},'commits':[], 'standards':[],
                      'diff':{'files':[{'path':'app.py','diff':'diff --git a/app.py b/app.py\n-old\n+new'}]},
                      'checks':{'state':'success','runs':[]}}
            with patch('pr_review_watcher.runtime.prepare_evidence',return_value=evidence), \
                 patch('pr_review_watcher.runtime.refresh_sources',return_value={}), \
                 patch('pr_review_watcher.runtime.GitHub.checks',return_value=evidence['checks']):
                result,report=run_fixed_review(cfg,{'id':1,'repo':'test/repo','number':1,'head':'a'*40,'base':'b'*40},root/'run')
            self.assertEqual(result['mergeStatus'],'GREEN')
            self.assertIn('No findings',report)
            self.assertNotIn('Review coverage',report)
            self.assertEqual(len(list((root/'run/workers').glob('*/result.json'))),3)
            self.assertTrue((root/'run/verified-report.md').exists())

    def test_immutable_cache_reused_but_base_change_invalidates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            job={'repo':'test/repo','head':'a'*40,'base':'b'*40}
            calls=[]
            def fetch():
                calls.append(1)
                return {'diff':{'raw':'exact pinned diff'},'standards':[]}
            one,hit=cached_code(Path(tmp),job,fetch)
            self.assertFalse(hit)
            two,hit=cached_code(Path(tmp),job,fetch)
            self.assertTrue(hit)
            self.assertEqual(one,two)
            self.assertEqual(len(calls),1)
            cached_code(Path(tmp),dict(job,base='c'*40),fetch)
            self.assertEqual(len(calls),2)
