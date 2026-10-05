import json
import os
from collections import Counter

our = 'u_a3a5624dce45'
rooms = {'s45': 'a_6c868628359f', 's46': 'a_5a06a8f48d67',
         's47': 'a_ae8e2507e0ec', 's48': 'a_2e31e10fe10a'}

tag_count = {'我方': Counter(), '对手': Counter()}
hu_count = {'我方': 0, '对手': 0}
f2_count = {'我方': 0, '对手': 0}
dealer_hu = {'我方': 0, '对手': 0}
dealer_fan = {'我方': 0, '对手': 0}

for name, room in rooms.items():
    for board in range(10):
        path = os.path.join('portal_events', room, f'b{board}.json')
        data = json.load(open(path, encoding='utf-8'))
        seats = data.get('seats') or []
        my_seat = next((i for i, s in enumerate(seats) if s.get('user_id') == our), None)
        for block in data['blocks']:
            for e in block.get('events') or []:
                if e['type'] != 'round_ended':
                    continue
                d = e.get('data') or {}
                who = '我方' if e.get('seat') == my_seat else '对手'
                hu_count[who] += 1
                fan = d.get('fan') or 1
                if fan >= 2:
                    f2_count[who] += 1
                if d.get('dealer') == e.get('seat'):
                    dealer_hu[who] += 1
                    dealer_fan[who] += fan
                for tag in (d.get('detail') or []):
                    tag_count[who][tag] += 1

print('=== 胡/番结构对比 (4 场 320 局) ===')
for who in ('我方', '对手'):
    n = hu_count[who]
    print(f"{who}: 胡 {n}, f2+ {f2_count[who]} ({f2_count[who] * 100 / max(n, 1):.1f}%), "
          f"庄家胡 {dealer_hu[who]} (庄胡均番 {dealer_fan[who] / max(dealer_hu[who], 1):.2f})")
    for tag, c in tag_count[who].most_common():
        print(f"   {tag}: {c} ({c * 100 / max(n, 1):.1f}%/胡)")
