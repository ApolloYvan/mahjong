"""baohua_ticket 单旋钮实验: ≥5 对门清手里三张同种 = 豪华七对(×4)彩票。
门票罚分让策略捏住三张同种追豪华, 对照: EV(a_score) + fan 分布变化。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.arena import compare

batches = int(sys.argv[1]) if len(sys.argv) > 1 else 30
variants = [
    {"tag": "production", "weights": {}},
    {"tag": "ticket_800", "weights": {"baohua_ticket": 800}},
    {"tag": "ticket_1600", "weights": {"baohua_ticket": 1600}},
]

for item in variants:
    row = compare("current", "current", claims_a="real", claims_b="real",
                  batches=batches, weights_a=item["weights"])
    fans = row.get("fans") or {}
    print(json.dumps({"tag": item["tag"], "a_score": row["a_score"], "b_score": row["b_score"],
                      "a_win_rate": row["a_win_rate"], "b_win_rate": row["b_win_rate"],
                      "a_fans": fans}, ensure_ascii=False))
