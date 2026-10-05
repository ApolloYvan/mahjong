import glob
import io
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

files = sorted(glob.glob('logs/*.jsonl'))
kinds = Counter()
fan_levels = Counter()
detail_terms = Counter()
total = 0
f2plus = 0

for path in files:
    for line in io.open(path, encoding='utf-8', errors='replace'):
        if '"hu_detail"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get('kind') != 'hu_detail':
            continue
        payload = rec.get('payload') or {}
        total += 1
        fan = payload.get('fan')
        fan_levels[repr(fan)] += 1
        if isinstance(fan, int) and fan >= 2:
            f2plus += 1
        detail = payload.get('detail')
        if isinstance(detail, str):
            detail_terms[detail] += 1
        elif isinstance(detail, (list, tuple)):
            for item in detail:
                detail_terms[str(item)] += 1
        elif isinstance(detail, dict):
            for key, value in detail.items():
                if value:
                    detail_terms[str(key)] += 1

print('hu_detail records:', total, 'f2+:', f2plus)
print('fan dist:', dict(fan_levels.most_common()))
print('detail terms:')
for term, count in detail_terms.most_common(30):
    print(' ', term, count)
