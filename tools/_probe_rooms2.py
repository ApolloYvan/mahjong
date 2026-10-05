import io
from collections import defaultdict

rooms = defaultdict(lambda: [0, ''])
for line in io.open('logs/2026-09-18.jsonl', encoding='utf-8', errors='ignore'):
    if '"kind": "decision"' not in line:
        continue
    i = line.find('game_id')
    if i < 0:
        continue
    gid = line[i + 11:].split('"')[0]
    if not gid.startswith(('a_', 't_')):
        continue
    room = gid.split('_r1_')[0]
    if '_r1_' not in gid:
        room = gid.rsplit('_b', 1)[0] if '_b' in gid else gid
    rooms[room][0] += 1
    rooms[room][1] = max(rooms[room][1], line[8:28])

for room, (n, last) in sorted(rooms.items(), key=lambda kv: kv[1][1]):
    print(f'{room} decisions={n} last={last}')
