"""baotou_slow1 单旋钮实验: 承重白保留溢价 (生产 3000 < 一步向听 10000 → 白被熔,
爆头率只有对手一半)。变体拉高 slow1 测: 爆头频率(fan2 计数) + EV(a_score) 是否改善。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.arena import compare

batches = int(sys.argv[1]) if len(sys.argv) > 1 else 30
variants = [
    {"tag": "production", "weights": {}},
    {"tag": "slow1_6000", "weights": {"baotou_slow1": 6000}},
    {"tag": "slow1_9000", "weights": {"baotou_slow1": 9000}},
    {"tag": "slow1_12000", "weights": {"baotou_slow1": 12000}},
]

for item in variants:
    row = compare("current", "current", claims_a="real", claims_b="real",
                  batches=batches, weights_a=item["weights"])
    fans = row.get("fans") or {}
    print(json.dumps({"tag": item["tag"], "a_score": row["a_score"], "b_score": row["b_score"],
                      "a_win_rate": row["a_win_rate"], "b_win_rate": row["b_win_rate"],
                      "a_fan2": fans.get("2", 0), "a_fan4plus": sum(v for k, v in fans.items() if int(k) >= 4),
                      "a_fans": fans}, ensure_ascii=False))
