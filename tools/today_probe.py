import io
import json
import os
import sys
from collections import Counter

path = sys.argv[1] if len(sys.argv) > 1 else 'logs/2026-09-20.jsonl'
if not os.path.exists(path):
    print('no log:', path)
    raise SystemExit(0)
kinds = Counter()
rooms = set()
for line in io.open(path, encoding='utf-8', errors='replace'):
    try:
        rec = json.loads(line)
    except Exception:
        continue
    kinds[rec.get('kind')] += 1
    payload = rec.get('payload') or {}
    game_id = payload.get('game_id')
    if game_id:
        rooms.add(game_id[:12])
print(path, 'kinds', dict(kinds))
print('recent rooms:', sorted(rooms)[-3:])
