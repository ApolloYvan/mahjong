"""汇总 tools/arena_suite.sh 跑出来的 reports/suite/*.json：每组 A−B 均值、95% CI、种子数，庄/闲分别的 A−B、得分/胜率/均番/吃碰杠；
评估器验收（V1/V2，已知答案）给出通过/不通过。完整表写 reports/arena_suite.txt，终端只打 <=40 行。
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROUPS = (
    ("V1", "已知答案：关 S1(rule_decline_joker_hold) vs 现行", "expect_neg"),
    ("V2", "已知答案：摸财神非爆头不胡(旧 bug) vs 现行", "expect_neg_sig"),
    ("D1", "庄家不强制 use_route/dealer_hint vs 现行", None),
    ("D2", "庄家不用 slow1_dealer 溢价 vs 现行", None),
    ("D3", "庄家不触发 S1 弃胡 vs 现行", None),
    ("C1", "关 tenpai_peng_gang_relax vs 现行", None),
    ("C2", "杠门槛更严(gang_strict) vs 现行", None),
    ("E1", "S1 放宽：墙尾门槛 8->4 vs 现行", None),
    ("E2", "S1 放宽：允许差一步转爆头的弃胡 vs 现行", None),
    ("E3", "S1 放宽：按期望值(p̂>p*+δ)决定弃胡 vs 现行", None),
)


def verdict(kind, d):
    if kind is None:
        return ""
    if d["n_seeds"] == 0 or (d["mean"] == 0 and d["lo"] == 0 and d["hi"] == 0):
        return "【验收】无信息（种子太少或双方完全一致）"
    if kind == "expect_neg":
        return "【验收】" + ("通过：判现行更好且 CI 排除 0" if d["hi"] < 0 else
                          "弱通过：方向对（现行更好）但 CI 含 0，分辨率不够" if d["mean"] < 0 else "**不通过**：平台判 A（关掉 S1）更好")
    return "【验收】" + ("通过：判现行显著更好" if d["hi"] < 0 else
                      "**不通过**：未能显著判出现行更好（评估器分辨率/功效不足，或有偏）" if d["mean"] < 0 else "**不通过**：方向反了")


def main():
    files = {}
    for path in glob.glob(os.path.join(ROOT, "reports", "suite", "*.json")):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        files[(d["tag"], d["layout"])] = d
    lines = []
    for tag, title, kind in GROUPS:
        for layout in ("1v3", "2v2"):
            d = files.get((tag, layout))
            if not d:
                continue
            lines.append("[%s %s] %s：A−B %+.3f 95%%CI [%+.3f, %+.3f]，%d 个种子 %s" % (
                tag, layout, title, d["mean"], d["lo"], d["hi"], d["n_seeds"], "" if d["lo"] > 0 or d["hi"] < 0 else "（CI 含 0）"))
            r = d["roles"]
            lines.append("    庄 A−B %+.2f[%+.2f,%+.2f] 得分 A %+.2f/B %+.2f 胜率 %.3f/%.3f 番 %.2f/%.2f | 闲 A−B %+.2f[%+.2f,%+.2f] 得分 %+.2f/%+.2f 胜率 %.3f/%.3f 番 %.2f/%.2f" % (
                d["dealer"][0], d["dealer"][0] - d["dealer"][2], d["dealer"][0] + d["dealer"][2],
                r["A_庄"]["score"], r["B_庄"]["score"], r["A_庄"]["win"], r["B_庄"]["win"], r["A_庄"]["fan"], r["B_庄"]["fan"],
                d["non_dealer"][0], d["non_dealer"][0] - d["non_dealer"][2], d["non_dealer"][0] + d["non_dealer"][2],
                r["A_闲"]["score"], r["B_闲"]["score"], r["A_闲"]["win"], r["B_闲"]["win"], r["A_闲"]["fan"], r["B_闲"]["fan"]))
            if tag.startswith("E") and d.get("s1"):
                a, b = d["s1"]["A"], d["s1"]["B"]
                lines.append("    S1 弃胡：A 触发 %d 次 胡率 %.0f%% 净 %+.1f/次 | B 触发 %d 次 胡率 %.0f%% 净 %+.1f/次" % (
                    a["n"], 100.0 * a["won"] / max(1, a["n"]), a["net"] / max(1, a["n"]),
                    b["n"], 100.0 * b["won"] / max(1, b["n"]), b["net"] / max(1, b["n"])))
            if tag.startswith("C"):
                lines.append("    吃/碰/杠 每局：庄 A %.2f/%.2f/%.2f B %.2f/%.2f/%.2f | 闲 A %.2f/%.2f/%.2f B %.2f/%.2f/%.2f" % (
                    *(r["A_庄"][k] for k in ("chi", "peng", "gang")), *(r["B_庄"][k] for k in ("chi", "peng", "gang")),
                    *(r["A_闲"][k] for k in ("chi", "peng", "gang")), *(r["B_闲"][k] for k in ("chi", "peng", "gang"))))
            v = verdict(kind, d)
            if v:
                lines.append("    " + v)
    out = os.path.join(ROOT, "reports", "arena_suite.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("=== arena_suite 汇总（完整 %s；A=改动，B=现行配置；A−B>0 表示改动更好） ===" % out)
    if not lines:
        print("还没有 reports/suite/*.json（先跑 tools/arena_suite.sh）")
        return
    head = lines if len(lines) <= 38 else [l for l in lines if not l.startswith("    吃/碰/杠")][:38]
    print("\n".join(head))
    if len(head) < len(lines):
        print("（行数超过 38，吃/碰/杠明细见 %s）" % out)


if __name__ == "__main__":
    main()
