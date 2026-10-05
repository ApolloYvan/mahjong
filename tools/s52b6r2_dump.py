import json
import os

our = 'u_a3a5624dce45'
path = os.path.join('portal_events', 'a_a0cccfb47f5d', 'b6.json')
data = json.load(open(path, encoding='utf-8'))
seats = data.get('seats') or []
my_seat = next((i for i, s in enumerate(seats) if s.get('user_id') == our), None)
print('seats:', [(i, s.get('name')) for i, s in enumerate(seats)], '我方座位:', my_seat)

events = [e for b in data['blocks'] for e in (b.get('events') or [])]
hands, cur = [], []
for e in events:
    cur.append(e)
    if e['type'] == 'round_ended':
        hands.append(cur)
        cur = []

r2 = hands[1] if len(hands) > 1 else []
end = next((e for e in reversed(r2) if e['type'] == 'round_ended'), None)
if end:
    print('局终:', json.dumps(end.get('data'), ensure_ascii=False)[:200])

print(f'\n=== 第2局 {len(r2)} 事件: 我方牌流 ===')
five_t = 0
for e in r2:
    if e.get('seat') != my_seat:
        if e['type'] in ('chi', 'peng', 'gang'):
            print(f"  seq {e['seq']:>4} (对手 {t if (t:=e['type']) else ''} {e.get('tile')})")
        continue
    t = e['type']
    if t == 'tile_drawn':
        if e.get('tile') == '5t':
            five_t += 1
        print(f"  seq {e['seq']:>4} 摸 {e.get('tile')}")
    elif t == 'tile_discarded':
        if e.get('tile') == '5t':
            five_t -= 1
        print(f"  seq {e['seq']:>4} 打 {e.get('tile')}   [手里5t余 {five_t}]")
    elif t in ('chi', 'peng', 'gang'):
        print(f"  seq {e['seq']:>4} {t} {e.get('tile')}  data={json.dumps(e.get('data'), ensure_ascii=False)[:80]}")
