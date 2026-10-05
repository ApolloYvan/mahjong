"""从决策日志提取策略回归样本。"""
import glob
import json
import os

from .ev import choose_route_discard
from .strategy import choose_discard


def build_samples(pattern="logs/*.jsonl", output="models/decision_samples.json"):
    samples = []
    for path in glob.glob(pattern):
        with open(path, encoding="utf-8") as source:
            for line in source:
                record = json.loads(line)
                if record.get("kind") != "decision":
                    continue
                payload = record["payload"]
                hand = payload.get("hand") or []
                decision = payload.get("decision") or {}
                if decision.get("action") != "discard" or not hand:
                    continue
                base = choose_discard(hand)
                seat = payload.get("seat", -1)
                melds = payload.get("melds") or []
                if isinstance(melds, dict):
                    melds = melds.get(str(seat), melds.get(seat, []))
                meld_groups = len(melds)
                god = payload.get("god") or {}
                route = choose_route_discard(
                    hand,
                    meld_groups=meld_groups,
                    chain_count=god.get("chain_count", 0),
                    piao=god.get("piao", payload.get("piao", 0)),
                    rules=payload.get("rules") or {},
                )
                samples.append({
                    "game_id": payload.get("game_id"),
                    "round_no": payload.get("round_no"),
                    "seq": payload.get("seq"),
                    "actual": decision.get("tile"),
                    "baseline": base,
                    "route": route,
                    "baseline_match": decision.get("tile") == base,
                    "route_match": decision.get("tile") == route,
                })
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(samples, target, ensure_ascii=False, indent=2)
    return samples
