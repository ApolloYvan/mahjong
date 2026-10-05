import collections
import json

with open("logs/2026-09-16.jsonl", encoding="utf-8", errors="ignore") as source:
    lines = source.readlines()

tail = lines[-600:]
games = set()
kinds = collections.Counter()
last = ""
room = set()
for line in tail:
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        continue
    payload = item.get("payload") or {}
    gid = str(payload.get("game_id", ""))
    if gid:
        games.add(gid)
        room.add(gid.split("_r")[0])
    kinds[item.get("kind")] += 1
    last = item.get("time", last)

print("rooms:", sorted(room))
print("recent games:", sorted(games)[-12:])
print("kinds:", dict(kinds))
print("last record:", last)
