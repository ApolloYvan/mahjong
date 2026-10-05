"""全历史番值分布：我方 vs 对手的胡牌番值结构与胡率（跨会话聚合）。
复用 hand_attribution 的分段差分法：decision 快照分数差分 → 赢家 earning 反推 fan。
10/20/40/80=闲 f1/2/4/8；24/48/96=庄 f1/2/4。"""
import collections
import glob
import json
from collections import Counter, defaultdict

hands = collections.defaultdict(list)
FAN = {10: 1, 20: 2, 40: 4, 80: 8, 24: 1, 48: 2, 96: 4}

for path in glob.glob("logs/*.jsonl"):
    with open(path, encoding="utf-8", errors="ignore") as source:
        for line in source:
            if '"kind": "decision"' not in line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") or {}
            gid = str(payload.get("game_id") or "")
            if not (gid.startswith("a_") or gid.startswith("t_65d538e905c5")):
                continue
            wall = payload.get("wall_remaining")
            scores = payload.get("scores")
            dealer = payload.get("dealer")
            if wall is None or scores is None or dealer is None:
                continue
            hands[gid].append((wall, dealer, scores, payload.get("seat")))

rooms = defaultdict(lambda: {"mine": Counter(), "opp": Counter(), "hands": 0, "my_hu": 0, "opp_hu": 0})
for gid in sorted(hands):
    room = gid[:14]
    seq = hands[gid]
    segments = []
    cur = []
    prev_wall = None
    for wall, dealer, scores, seat in seq:
        if prev_wall is not None and wall > prev_wall + 2:
            segments.append(cur)
            cur = []
        cur.append((wall, dealer, scores, seat))
        prev_wall = wall
    if cur:
        segments.append(cur)
    segments = [seg for seg in segments if len(seg) >= 3]
    my_seat = segments[0][0][3] if segments else None
    info = rooms[room]
    for index, seg in enumerate(segments):
        dealer = seg[0][1]
        start_scores = seg[0][2]
        end_scores = segments[index + 1][0][2] if index + 1 < len(segments) else seg[-1][2]
        deltas = [end_scores[i] - start_scores[i] for i in range(4)]
        top = max(deltas)
        info["hands"] += 1
        if top <= 0:
            continue
        winner = deltas.index(top)
        fan = FAN.get(top, 0)
        if not fan:
            continue
        if winner == my_seat:
            info["mine"][fan] += 1
            info["my_hu"] += 1
        else:
            info["opp"][fan] += 1
            info["opp_hu"] += 1

total_mine = Counter()
total_opp = Counter()
hands_total = my_hu = opp_hu = 0
for room in sorted(rooms):
    info = rooms[room]
    hands_total += info["hands"]
    total_mine.update(info["mine"])
    total_opp.update(info["opp"])
    my_hu += info["my_hu"]
    opp_hu += info["opp_hu"]
    print(f"{room} hands={info['hands']:3d} my_hu={info['my_hu']:2d} opp_hu={info['opp_hu']:2d} "
          f"my={dict(sorted(info['mine'].items()))} opp={dict(sorted(info['opp'].items()))}")

print(f"\nTOTAL hands={hands_total} my_hu={my_hu} ({my_hu / max(1, hands_total):.1%}) "
      f"opp_hu={opp_hu} ({opp_hu / max(1, hands_total):.1%})")
print("my fan dist:", dict(sorted(total_mine.items())),
      "avg:", round(sum(f * n for f, n in total_mine.items()) / max(1, my_hu), 2),
      "f2+:", sum(n for f, n in total_mine.items() if f >= 2))
print("opp fan dist:", dict(sorted(total_opp.items())),
      "avg:", round(sum(f * n for f, n in total_opp.items()) / max(1, opp_hu), 2),
      "f2+:", sum(n for f, n in total_opp.items() if f >= 2))
