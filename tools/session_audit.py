import io
import json
from collections import Counter

rooms = {'s49': 'a_fce6b4833c1f', 's50': 'a_1fe688897daf',
         's51': None, 's52': 'a_a0cccfb47f5d', 's53': 'a_554fc9aeb394'}
# 找 s51 的房间: 16:4x-17:1x 之间出现的匹配房间号
seen_rooms = Counter()
per_room = {}
for line in io.open('logs/2026-09-20.jsonl', encoding='utf-8', errors='replace'):
    if '"hu_detail"' not in line and '"result"' not in line:
        continue
    try:
        rec = json.loads(line)
    except Exception:
        continue
    t = rec.get('time', '')
    p = rec.get('payload') or {}
    gid = p.get('game_id') or (p.get('state') or {}).get('game_id') or ''
    if not gid.startswith('a_'):
        continue
    room = gid[:14]
    seen_rooms[t[:13]] = room
    bucket = per_room.setdefault(room, Counter())
    if rec.get('kind') == 'hu_detail':
        bucket['hu'] += 1
        d = p.get('detail') or []
        for tag in d or ['平胡']:
            bucket[f'tag_{tag}'] += 1
        if (p.get('fan') or 1) >= 2:
            bucket['f2'] += 1

print('=== 房间活动时间线 (尾段) ===')
for t in sorted(seen_rooms)[-8:]:
    print(' ', t, seen_rooms[t])

print('\n=== 各房间 hu/fan 结构 ===')
for room, c in sorted(per_room.items()):
    print(f"{room}: hu={c['hu']} f2+={c['f2']} 爆头={c['tag_爆头']} 七对={c['tag_七对']} "
          f"财飘={c['tag_财飘']} 豪华={c['tag_豪华七对']} 链={c['tag_动作链x1'] + c['tag_动作链x2']}")
