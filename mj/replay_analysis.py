"""离线事件回放：计算各事件到下一事件的时间差和窗口完成情况。"""
import glob
import json
import os
from collections import Counter


def replay(pattern="models/events/*.json", output="models/replay_report.json"):
    gaps = []
    timeout_windows = Counter()
    for path in glob.glob(pattern):
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        for block in data.get("blocks", []):
            events = block.get("events", [])
            for previous, current in zip(events, events[1:]):
                gaps.append(current.get("ts", 0) - previous.get("ts", 0))
            for event in events:
                if event.get("type") == "timeout":
                    timeout_windows[event.get("data", {}).get("kind", "unknown")] += 1
    report = {
        "events": len(gaps) + (1 if gaps else 0),
        "gap_seconds": {
            "min": min(gaps) if gaps else 0,
            "max": max(gaps) if gaps else 0,
            "avg": sum(gaps) / len(gaps) if gaps else 0,
        },
        "timeouts": dict(timeout_windows),
    }
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
    return report
