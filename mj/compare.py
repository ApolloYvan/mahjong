"""离线比较两套弃牌策略：向听/进张基线 vs 路线 EV。"""
import glob
import json
import os
from collections import Counter

from .ev import choose_route_discard
from .strategy import choose_discard


def compare(pattern="models/events/*.json", output="models/strategy_compare.json"):
    rows = []
    for path in glob.glob(pattern):
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        for block in data.get("blocks", []):
            for event in block.get("events", []):
                if event.get("type") != "tile_discarded":
                    continue
                hand = event.get("data", {}).get("hand") or event.get("hand")
                if not hand or event.get("tile") not in hand:
                    continue
                baseline = choose_discard(hand)
                route = choose_route_discard(hand)
                rows.append({"game_id": data.get("game_id"), "seq": event.get("seq"), "actual": event.get("tile"), "baseline": baseline, "route": route})
    report = {
        "samples": len(rows),
        "baseline_matches": sum(row["actual"] == row["baseline"] for row in rows),
        "route_matches": sum(row["actual"] == row["route"] for row in rows),
        "different": sum(row["baseline"] != row["route"] for row in rows),
        "rows": rows[:1000],
    }
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
    return report
