import collections
import glob
import json
import sys

ROOM = sys.argv[1] if len(sys.argv) > 1 else "a_d5fc6d693eab"

games = {}
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
            kind = item.get("kind")
            gid = str(payload.get("game_id") or "")
            if not gid.startswith(ROOM):
                continue
            if kind == "decision":
                entry = games.setdefault(gid, {"seat": payload.get("seat"), "hu": 0, "actions": collections.Counter()})
                decision = payload.get("decision") or {}
                entry["actions"][decision.get("action")] += 1
                if decision.get("action") == "hu":
                    entry["hu"] += 1
            elif kind == "result":
                snapshot = (payload.get("state") or {}).get("snapshot") or {}
                entry = games.setdefault(gid, {"seat": snapshot.get("seat"), "hu": 0, "actions": collections.Counter()})
                entry["final_scores"] = snapshot.get("scores")
                entry["my_seat"] = snapshot.get("seat")

total = 0
wins = 0
for gid in sorted(games):
    entry = games[gid]
    seat = entry.get("my_seat", entry.get("seat"))
    scores = entry.get("final_scores")
    if scores and seat is not None and 0 <= seat < len(scores):
        my = scores[seat]
        total += my
        tag = "WIN" if my > 0 else ("draw" if my == 0 else "lose")
        if my > 0:
            wins += 1
        print(f"{gid.split('_r1_')[1] if '_r1_' in gid else gid} seat={seat} {tag} my={my} scores={scores} hu={entry['hu']} actions={dict(entry['actions'])}")
    else:
        print(gid, "no final", entry.get("hu"))
print("games:", len(games), "wins:", wins, "net:", total)
