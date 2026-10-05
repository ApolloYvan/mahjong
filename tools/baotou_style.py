import json
import os
from collections import Counter

our = 'u_a3a5624dce45'
rooms = {'s45': 'a_6c868628359f', 's46': 'a_5a06a8f48d67',
         's47': 'a_ae8e2507e0ec', 's48': 'a_2e31e10fe10a'}

style = Counter()
cais = Counter()
cais_win = Counter()
rows = Counter()

for name, room in rooms.items():
    for board in range(10):
        path = os.path.join('portal_events', room, f'b{board}.json')
        data = json.load(open(path, encoding='utf-8'))
        seats = data.get('seats') or []
        my_seat = next((i for i, s in enumerate(seats) if s.get('user_id') == our), None)
        events = [e for b in data['blocks'] for e in (b.get('events') or [])]
        hands, cur = [], []
        for e in events:
            cur.append(e)
            if e['type'] == 'round_ended':
                hands.append(cur)
                cur = []
        for hand in hands:
            end = next((e for e in reversed(hand) if e['type'] == 'round_ended'), None)
            if not end:
                continue
            d = end.get('data') or {}
            winner = end.get('seat')
            who = '我方' if winner == my_seat else '对手'
            detail = d.get('detail') or []
            if '爆头' not in detail:
                continue
            declared = any(e.get('seat') == winner and e.get('tile') == '白'
                           and e['type'] == 'tile_discarded' for e in hand)
            style[(who, '宣言式' if declared else '持白式')] += 1
            if '财飘' in detail:
                cais[(who, declared)] += 1
            rows[(who, '宣言式' if declared else '持白式')] += 1

print('=== 爆头赢的打法来源 (4 场 320 局) ===')
for key in sorted(style):
    print(f"  {key[0]} {key[1]}: {style[key]}")
print('\n=== 财飘标记分布 ===')
for key in sorted(cais):
    print(f"  {key[0]} 宣言式={key[1]}: {cais[key]}")
