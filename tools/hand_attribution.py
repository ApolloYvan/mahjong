import collections
import glob
import json
import sys

ROOM = sys.argv[1] if len(sys.argv) > 1 else "a_e2d63304bd5b"

hands = collections.defaultdict(list)
hus = collections.defaultdict(list)
for path in glob.glob("logs/*.jsonl"):
    with open(path, encoding="utf-8", errors="ignore") as source:
        for line in source:
            if ROOM not in line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") or {}
            gid = str(payload.get("game_id") or "")
            if not gid.startswith(ROOM) or item.get("kind") != "decision":
                continue
            wall = payload.get("wall_remaining")
            scores = payload.get("scores")
            dealer = payload.get("dealer")
            if wall is None or scores is None or dealer is None:
                continue
            hands[gid].append((wall, dealer, scores, payload.get("seat")))
            if (payload.get("decision") or {}).get("action") == "hu":
                hus[gid].append(dealer)

stats = {"hands": 0, "my_win_hands": 0, "opp_wins": 0, "dealer_streak_paid": 0, "my_dealer_hands": 0,
         "my_dealer_earn": 0, "non_dealer_earn": 0}
for gid in sorted(hands):
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
    batch = gid.split("_r1_")[1] if "_r1_" in gid else gid
    detail = []
    for index, seg in enumerate(segments):
        dealer = seg[0][1]
        start_scores = seg[0][2]
        if index + 1 < len(segments):
            end_scores = segments[index + 1][0][2]
        else:
            end_scores = seg[-1][2]
        deltas = [end_scores[i] - start_scores[i] for i in range(4)]
        winner = deltas.index(max(deltas)) if max(deltas) > 0 else None
        stats["hands"] += 1
        if winner == my_seat:
            stats["my_win_hands"] += 1
            if my_seat == dealer:
                stats["my_dealer_hands"] += 1
                stats["my_dealer_earn"] += max(deltas)
            else:
                stats["non_dealer_earn"] += max(deltas)
        elif max(deltas) > 0:
            stats["opp_wins"] += 1
            if index > 0 and segments[index - 1][0][1] == dealer and detail and "D" in detail[-1]:
                pass
        detail.append(f"{winner if winner is not None else '?'}{'D' if winner == dealer else ''}{max(deltas):+d}" if max(deltas) > 0 else "draw")
    print(batch, "my_seat:", my_seat, " ".join(detail))

print(json.dumps(stats, ensure_ascii=False))
