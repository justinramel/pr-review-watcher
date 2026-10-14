"""Provider-reported usage, deduplicated across copied/forked session transcripts."""
import hashlib
import json
from pathlib import Path

FIELDS = ('input', 'cacheRead', 'cacheWrite', 'output', 'reasoning', 'totalTokens')


def transcript_lines(path):
    with path.open() as stream:
        yield from stream


def collect_usage(directory):
    totals = {key: 0 for key in FIELDS}
    seen = set()
    models = set()
    missing_ids = 0
    for path in sorted(Path(directory).rglob('*.jsonl')):
        for line in transcript_lines(path):
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # A running process may be partway through the final line.
            message = entry.get('message', {})
            usage = message.get('usage')
            if message.get('role') != 'assistant' or not isinstance(usage, dict):
                continue
            response = message.get('responseId')
            if response:
                key = (message.get('provider'), response)
            else:
                fingerprint = [message.get('provider'), message.get('model'), message.get('timestamp'),
                               message.get('content'), usage]
                key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            missing_ids += not bool(response)
            models.add(message.get('model', 'not exposed'))
            for field in FIELDS:
                value = usage.get(field, 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[field] += value
    return dict(totals, responses=len(seen), models=sorted(models), missing_response_ids=missing_ids)


def usage_log(usage):
    return (f"input_tokens={usage['input']} cached_input_tokens={usage['cacheRead']} "
            f"cache_write_tokens={usage['cacheWrite']} output_tokens={usage['output']} "
            f"reasoning_tokens={usage['reasoning']} total_tokens={usage['totalTokens']} "
            f"responses={usage['responses']}")
