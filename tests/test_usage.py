import json
import tempfile
import unittest
from pathlib import Path
from pr_review_watcher.usage import collect_usage


class UsageTests(unittest.TestCase):
    def test_copied_sessions_and_artifacts_do_not_duplicate_provider_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            message={'role':'assistant','provider':'test','model':'unchanged','responseId':'response-1',
                     'usage':{'input':10,'cacheRead':100,'output':20,'reasoning':5,'totalTokens':130}}
            record=json.dumps({'type':'message','id':'original','message':message})+'\n'
            (p/'parent.jsonl').write_text(record)
            (p/'fork.jsonl').write_text(record+'{incomplete')
            usage=collect_usage(p)
            self.assertEqual(usage['responses'],1)
            self.assertEqual(usage['totalTokens'],130)
            self.assertEqual(usage['reasoning'],5)
