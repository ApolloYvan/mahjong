"""胡牌番型分类：hu_detail 全量统计——平胡/爆头/七对/杠开 占比与 fan 分布。"""
import glob
import json
from collections import Counter, defaultdict

types = Counter()
fan_by_type = defaultdict(list)
total = 0
for path in glob.glob("logs/*.jsonl"):
    with open(path, encoding="utf-8", errors="ignore") as source:
        for line in source:
            if '"kind": "hu_detail"' not in line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") or {}
            fan = payload.get("fan") or 0
            detail = tuple(payload.get("detail") or [])
            key = "+".join(detail) if detail else "unknown"
            types[key] += 1
            fan_by_type[key].append(fan)
            total += 1

print(f"总胡牌: {total}")
for key, count in types.most_common():
    fans = fan_by_type[key]
    avg_fan = sum(f for f in fans if f) / max(1, len([f for f in fans if f]))
    print(f"  {key}: {count} ({count / max(1, total):.0%}) avg_fan={avg_fan:.2f}")
