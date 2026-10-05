"""从测试房完整事件提取低噪声训练样本。"""
import json
import os


def extract_file(path, output_path):
    with open(path, encoding="utf-8") as source:
        data = json.load(source)
    samples = []
    for block in data.get("blocks", []):
        for event in block.get("events", []):
            event_type = event.get("type")
            if event_type not in ("tile_discarded", "peng", "chi", "gang", "hu", "round_ended"):
                continue
            samples.append({
                "game_id": data.get("game_id"),
                "batch": data.get("batch"),
                "dealer": block.get("dealer"),
                "seq": event.get("seq"),
                "seat": event.get("seat"),
                "type": event_type,
                "tile": event.get("tile"),
                "data": event.get("data") or {},
            })
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as output:
        json.dump(samples, output, ensure_ascii=False, indent=2)
    return len(samples)
