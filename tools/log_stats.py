import json
import os
import sys
from collections import Counter

logs_dir = "logs"
prefix = sys.argv[1] if len(sys.argv) > 1 else "t_5f7d0a4a839d_r2"
kinds = Counter()
errors = Counter()
decisions = Counter()
state_ms = []
samples = []

for name in sorted(os.listdir(logs_dir)):
    if not name.endswith(".jsonl"):
        continue
    with open(os.path.join(logs_dir, name), encoding="utf-8", errors="ignore") as source:
        for line in source:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") or {}
            if not str(payload.get("game_id", "")).startswith(prefix):
                continue
            kind = item.get("kind")
            kinds[kind] += 1
            if kind == "state":
                ms = payload.get("state_request_ms")
                if isinstance(ms, (int, float)):
                    state_ms.append(ms)
            elif kind == "error":
                text = str(payload.get("error") or payload)[:120]
                errors[text] += 1
                if len(samples) < 5:
                    samples.append((payload.get("game_id"), text))
            elif kind == "decision":
                decision = payload.get("decision") or {}
                decisions[decision.get("action")] += 1

print("kinds:", dict(kinds))
print("decisions:", dict(decisions))
print("errors:", dict(errors))
if state_ms:
    state_ms.sort()
    print(f"state n={len(state_ms)} p50={state_ms[len(state_ms)//2]:.0f}ms p95={state_ms[int(len(state_ms)*0.95)]:.0f}ms")
for row in samples:
    print("sample:", row)
