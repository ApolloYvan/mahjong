import json
import os

our = 'u_a3a5624dce45'
room = 'a_a0cccfb47f5d'

bai_declare = 0
bai_draw = 0
hu_details = []
for board in range(10):
    path = os.path.join('portal_events', room, f'b{board}.json')
    data = json.load(open(path, encoding='utf-8'))
    seats = data.get('seats') or []
    my_seat = next((i for i, s in enumerate(seats) if s.get('user_id') == our), None)
    events = [e for b in data['blocks'] for e in (b.get('events') or [])]
    for e in events:
        if e.get('seat') != my_seat:
            continue
        if e.get('tile') == '白':
            if e['type'] == 'tile_discarded':
                bai_declare += ((e.get('data') or {}).get('catch_play') is True)
            elif e['type'] == 'tile_drawn':
                bai_draw += 1
        if e['type'] == 'round_ended':
            d = e.get('data') or {}
            hu_details.append((board, d.get('detail'), d.get('fan')))

print('我方摸到白:', bai_draw, ' 我方打白(宣言):', bai_declare)
print('我方胡牌明细:')
for board, detail, fan in hu_details:
    print(f'  b{board}: {detail} fan{fan}')
