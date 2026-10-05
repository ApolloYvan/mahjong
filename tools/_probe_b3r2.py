import glob
import json

GID = "a_442453072b99_r1_b3_t0"
for path in glob.glob("logs/*.jsonl"):
    with open(path, encoding="utf-8", errors="ignore") as source:
        for line in source:
            if GID not in line or '"kind": "decision"' not in line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") or {}
            if payload.get("round_no") != 2:
                continue
            dec = payload.get("decision") or {}
            melds = payload.get("melds") or []
            seat = payload.get("seat", 0)
            mg = len(melds[seat] or []) if isinstance(melds, list) and seat < len(melds) else "?"
            god = payload.get("god") or {}
            print(f"wall={payload.get('wall_remaining')} phase={payload.get('phase'):<14} "
                  f"mg={mg} drawn={payload.get('drawn_tile')!s:<4} "
                  f"catch={god.get('catch_play')} disc={god.get('god_discarder_seat')}")
            print(f"  hand={payload.get('hand')}")
            print(f"  melds={json.dumps(melds, ensure_ascii=False)}")
            print(f"  -> {dec}")
