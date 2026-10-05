import collections
import json
import sys

ROOM = sys.argv[1] if len(sys.argv) > 1 else "a_3346567c129b"

games = {}
with open("logs/2026-09-16.jsonl", encoding="utf-8", errors="ignore") as source:
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
        if kind == "decision":
            entry = games.setdefault(gid, {"hu": 0, "sprint_decisions": 0, "sprint_hu": 0, "actions": collections.Counter()})
            decision = payload.get("decision") or {}
            entry["actions"][decision.get("action")] += 1
            if decision.get("action") == "hu":
                entry["hu"] += 1
                if payload.get("opp_chain"):
                    entry["sprint_hu"] += 1
            if payload.get("opp_chain"):
                entry["sprint_decisions"] += 1
        elif kind == "result":
            snapshot = (payload.get("state") or {}).get("snapshot") or {}
            entry = games.setdefault(gid, {"hu": 0, "sprint_decisions": 0, "sprint_hu": 0, "actions": collections.Counter()})
            entry["final_scores"] = snapshot.get("scores")
            entry["my_seat"] = snapshot.get("seat")

total = wins = sprint_hands = sprint_win_hands = 0
for gid in sorted(games):
    entry = games[gid]
    seat = entry.get("my_seat")
    scores = entry.get("final_scores")
    line = f"{gid.split('_r1_')[1] if '_r1_' in gid else gid} sprint_decisions={entry['sprint_decisions']} hu={entry['hu']}"
    if scores and seat is not None and 0 <= seat < len(scores):
        my = scores[seat]
        total += my
        tag = "WIN" if my > 0 else ("draw" if my == 0 else "lose")
        if my > 0:
            wins += 1
        line += f" {tag} my={my} scores={scores}"
    if entry["sprint_decisions"]:
        sprint_hands += 1
        sprint_win_hands += entry["hu"]
        line += f" sprint_hu={entry['sprint_hu']}"
    print(line)
print("batches:", len(games), "wins:", wins, "net:", total, "sprint_batches:", sprint_hands, "sprint_batch_hu:", sprint_win_hands)
