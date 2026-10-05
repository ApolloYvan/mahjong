"""翻倍漏斗：七对潜手 / 爆头全听态（含副露手）的出现与最终结局。用法: python tools/double_funnel.py <room>"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.joker_ev import hand_all_wait
from mj.shanten import pair_shanten, shanten
from mj.tiles import TILE_INDEX, to_counts

ROOM = sys.argv[1] if len(sys.argv) > 1 else "a_85e87d4858a1"

games = defaultdict(list)
hu_detail = defaultdict(list)
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
            if not gid.startswith(ROOM):
                continue
            kind = item.get("kind")
            dec = payload.get("decision") or {}
            if kind == "decision" and dec.get("action") and payload.get("wall_remaining") is not None:
                games[gid].append(payload)
            elif kind == "hu_detail":
                hu_detail[gid].append(payload)


def segments_of(seq):
    segments, cur, prev_wall = [], [], None
    for payload in seq:
        wall = payload.get("wall_remaining")
        if prev_wall is not None and wall > prev_wall + 2:
            segments.append(cur)
            cur = []
        cur.append(payload)
        prev_wall = wall
    if cur:
        segments.append(cur)
    return [seg for seg in segments if len(seg) >= 3]


funnel = Counter()
for gid in sorted(games):
    segs = segments_of(games[gid])
    round_fan = {}
    for row in hu_detail.get(gid, []):
        round_fan[row.get("round_no")] = (row.get("fan"), tuple(row.get("detail") or []))
    for index, seg in enumerate(segs):
        round_no = seg[0].get("round_no")
        fan, detail = round_fan.get(round_no, (0, ()))
        won = fan > 0
        f2win = fan >= 2
        seven_win = "七对子" in detail or "豪华七对" in detail
        baotou_win = "爆头" in detail
        reached_pair1 = reached_pair2 = reached_allwait = False
        for payload in seg:
            dec = payload.get("decision") or {}
            if dec.get("action") != "discard":
                continue
            hand = payload.get("hand") or []
            seat = payload.get("seat", 0)
            melds = payload.get("melds")
            meld_groups = len((melds[seat] or [])) if isinstance(melds, list) and seat < len(melds) else 0
            tile = dec.get("tile")
            if not hand or not tile or tile not in hand:
                continue
            left = list(hand)
            left.remove(tile)
            counts = to_counts(left)
            pair = pair_shanten(counts)
            std = shanten(counts, meld_groups)
            if meld_groups == 0:
                if pair <= 1:
                    reached_pair1 = True
                if pair <= 2:
                    reached_pair2 = True
            if counts[TILE_INDEX["白"]]:
                if hand_all_wait(counts, meld_groups):
                    reached_allwait = True
        if reached_pair2:
            funnel["pair2_total"] += 1
            funnel["pair2_won_any" if won else "pair2_lost"] += 1
            funnel["pair2_f2" if f2win else "pair2_flat"] += 1 if won else 0
            funnel["pair2_seven" if seven_win else "pair2_noseven"] += 1 if won else 0
        if reached_pair1:
            funnel["pair1_total"] += 1
            funnel["pair1_won_any" if won else "pair1_lost"] += 1
        if reached_allwait:
            funnel["allwait_total"] += 1
            funnel["allwait_" + ("baotou_win" if baotou_win else ("lost" if not won else "other_win"))] += 1

print(json.dumps(funnel, ensure_ascii=False, indent=1, sort_keys=True))
print("\n胡明细:")
for gid in sorted(hu_detail):
    for row in hu_detail[gid]:
        print(f"  {gid.split('_r1_')[-1]} r{row.get('round_no')} fan={row.get('fan')} "
              f"detail={row.get('detail')}")
