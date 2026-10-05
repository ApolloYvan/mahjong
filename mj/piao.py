"""按庄闲、动作链、座位分层估计财飘成功率。"""
import glob
import json
import os
from collections import defaultdict


def _rate(successes, attempts, prior=0.25):
    return (successes + prior) / (attempts + 1)


def _key(payload):
    god = payload.get("god") or {}
    is_dealer = payload.get("dealer") == payload.get("seat")
    return (
        "dealer" if is_dealer else "idle",
        str(payload.get("seat", -1)),
        str(god.get("chain_count", payload.get("chain_count", 0))),
    )


def estimate(pattern="logs/*.jsonl", output="models/piao_rate.json", prior=0.25):
    """piao_attempt 记录为真实弃白续飘；同局同座随后 hu 提交 = 成功。"""
    groups = defaultdict(lambda: {"attempts": 0, "successes": 0})
    pending = defaultdict(list)
    hu_seats = defaultdict(set)
    game_done = set()
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8", errors="ignore") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") or {}
                kind = record.get("kind")
                game_id = payload.get("game_id") or (payload.get("state") or {}).get("game_id")
                if kind == "piao_attempt":
                    seat = payload.get("seat", -1)
                    key = _key(payload)
                    groups[key]["attempts"] += 1
                    pending[game_id].append({"seat": seat, "key": key})
                elif kind == "decision" and (payload.get("decision") or {}).get("action") == "hu":
                    hu_seats[game_id].add(payload.get("seat", -1))
                elif kind == "result" and game_id:
                    game_done.add(game_id)
    for game_id in list(pending):
        if game_id in game_done:
            for item in pending.pop(game_id):
                if item["seat"] in hu_seats.get(game_id, set()):
                    groups[item["key"]]["successes"] += 1
    attempts = sum(value["attempts"] for value in groups.values())
    successes = sum(value["successes"] for value in groups.values())
    result = {"attempts": attempts, "successes": successes, "rate": _rate(successes, attempts, prior), "groups": {"|".join(key): {**value, "rate": _rate(value["successes"], value["attempts"], prior)} for key, value in groups.items()}}
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(result, target, ensure_ascii=False, indent=2)
    return result


def rate_for(snapshot, path="models/piao_rate.json"):
    try:
        with open(path, encoding="utf-8") as source:
            result = json.load(source)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0.25
    group = (result.get("groups") or {}).get("|".join(_key(snapshot)))
    return (group or {}).get("rate", result.get("rate", 0.25))
