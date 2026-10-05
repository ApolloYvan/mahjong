import json
import os

our = 'u_a3a5624dce45'
rooms = {'s45': 'a_6c868628359f', 's46': 'a_5a06a8f48d67',
         's47': 'a_ae8e2507e0ec', 's48': 'a_2e31e10fe10a'}

for name, room in rooms.items():
    my_draws = opp_claims = opp_discards = my_discards = my_hu = opp_hu = 0
    hands = 0
    for board in range(10):
        path = os.path.join('portal_events', room, f'b{board}.json')
        data = json.load(open(path, encoding='utf-8'))
        seats = data.get('seats') or []
        my_seat = next((i for i, s in enumerate(seats) if s.get('user_id') == our), None)
        for block in data['blocks']:
            for e in block.get('events') or []:
                t = e['type']
                if t == 'round_ended':
                    hands += 1
                    d = e.get('data') or {}
                    if e.get('seat') == my_seat:
                        my_hu += 1
                    else:
                        opp_hu += 1
                elif e.get('seat') == my_seat:
                    if t == 'tile_drawn':
                        my_draws += 1
                    elif t == 'tile_discarded':
                        my_discards += 1
                else:
                    if t in ('chi', 'peng', 'gang'):
                        opp_claims += 1
                    elif t == 'tile_discarded':
                        opp_discards += 1
    print(f"{name}: 局数={hands} 我方摸牌={my_draws}({my_draws / max(hands, 1):.1f}/局) "
          f"对手吃碰杠={opp_claims}({opp_claims / max(hands, 1):.1f}/局) "
          f"我方胡={my_hu} 对手胡={opp_hu}")
