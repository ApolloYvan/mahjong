import glob
import json

GID = "a_442453072b99_r1_b3_t0"
lines = []
for path in glob.glob("logs/*.jsonl"):
    with open(path, encoding="utf-8", errors="ignore") as source:
        for line in source:
            if GID not in line or '"kind": "state"' not in line:
                continue
            lines.append(line)

print(f"state 行总数: {len(lines)}")
with_snap = 0
for line in lines:
    if '"snapshot"' in line:
        with_snap += 1
print(f"含 snapshot 的: {with_snap}")
# 抽查第 100、150、200 条
for i in (99, 149, 199):
    if i < len(lines):
        print(f"--- line {i+1} ---")
        print(lines[i][:400])
