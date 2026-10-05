# -*- coding: utf-8 -*-
"""权重 A/B：改一组权重值不值，离线打完再说，不烧比赛局数。

用 mj.arena.run_arena（镜像配对座位 + 每手独立派生 seed + 95% CI），结论从
置信区间边界推导，跨 0 就是 inconclusive——不看单次总分正负。

    # 激进档：爆头从 0.9 步向听重定价到 2.5 步，并把覆盖率曲线拉成线性
    python3 tools/weight_ab.py --set baotou_all_wait=25000 baotou_wait_power=1

    python3 tools/weight_ab.py --set baotou_all_wait=25000 --batches 48   # 样本翻倍

A = 当前权重 + 你指定的覆盖；B = 当前权重原样。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.arena import run_arena
from mj.fit import load_weights


def _coerce(text):
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _find_ci(node, path=""):
    """报告结构可能嵌套（也可能藏在 list 里），递归找出带 ci95 的那个统计块。"""
    if isinstance(node, dict):
        if "ci95" in node:
            return path, node
        for key, value in node.items():
            hit = _find_ci(value, "%s.%s" % (path, key) if path else key)
            if hit:
                return hit
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hit = _find_ci(value, "%s[%d]" % (path, i))
            if hit:
                return hit
    return None


def _outline(node, path="", depth=0, out=None):
    """找不到统计块时，把报告的骨架打出来——不能让我们对着 inconclusive 瞎猜。"""
    out = [] if out is None else out
    if depth > 3:
        return out
    if isinstance(node, dict):
        for key, value in sorted(node.items()):
            here = "%s.%s" % (path, key) if path else key
            if isinstance(value, (dict, list)):
                out.append("%s  <%s len=%d>" % (here, type(value).__name__,
                                                len(value)))
                _outline(value, here, depth + 1, out)
            elif isinstance(value, (int, float, bool)) or value is None:
                out.append("%s = %s" % (here, value))
    elif isinstance(node, list) and node:
        out.append("%s[0] ↓" % path)
        _outline(node[0], "%s[0]" % path, depth + 1, out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE",
                    help="A 侧要覆盖的权重，可给多个")
    # run_arena 要求 batches 是 SCHEDULE_PERIOD(16) 的正整数倍，
    # 否则镜像座位/初庄排班不均衡，它会直接抛 ValueError。
    ap.add_argument("--from-json", help="A 侧整组覆盖（如 tools/discard_fit.py 产出的 models/discard_fit_weights.json）")
    ap.add_argument("--batches", type=int, default=32)
    ap.add_argument("--hands", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20260916)
    # 必须是 current + real：claims="all" 是「无脑全吃碰」，会**完全绕过**
    # mj.responses.choose_chi / claim_assessment，那样测不到吃碰相关的权重改动。
    # 口径与现成的 tools/slow1_experiment.py 一致。
    ap.add_argument("--policy", default="current")
    ap.add_argument("--claims", default="real")
    ap.add_argument("--json", help="把完整报告写到这个文件")
    a = ap.parse_args()

    if not a.set and not a.from_json:
        ap.error("至少要给一个 --set KEY=VALUE 或 --from-json，否则 A 和 B 完全相同")
    from mj.arena import SCHEDULE_PERIOD
    if a.batches <= 0 or a.batches % SCHEDULE_PERIOD:
        near = max(SCHEDULE_PERIOD, round(a.batches / float(SCHEDULE_PERIOD)) * SCHEDULE_PERIOD)
        ap.error("--batches 必须是 %d 的正整数倍（镜像座位/初庄排班周期），"
                 "收到 %d。改成 %d 或 %d。"
                 % (SCHEDULE_PERIOD, a.batches, near, near + SCHEDULE_PERIOD))

    base = dict(load_weights())
    variant = dict(base)
    if a.from_json:
        with open(a.from_json, encoding="utf-8") as fh:
            loaded = json.load(fh)
        loaded = loaded.get("weights", loaded)
        a.set = list(a.set) + ["%s=%s" % kv for kv in loaded.items()]
    for item in a.set:
        if "=" not in item:
            ap.error("--set 要写成 KEY=VALUE，收到：%s" % item)
        key, _, value = item.partition("=")
        if key not in base and not key.startswith(("fd_", "fj_", "fc_")):
            ap.error("权重 %s 不存在。现有键见 mj/fit.py" % key)
        variant[key] = _coerce(value)

    print("A（激进档） vs B（当前）")
    for item in a.set:
        key = item.split("=")[0]
        print("   %-24s %s  →  %s" % (key, base.get(key, "缺省"), variant[key]))
    print("   对局量：%d batches × %d hands × 2（镜像）= %d 手\n"
          % (a.batches, a.hands, a.batches * a.hands * 2))

    try:
        report = run_arena(a.policy, a.policy, hands_per_batch=a.hands, batches=a.batches,
                           root_seed=a.seed, claims_a=a.claims, claims_b=a.claims,
                           weights_a=variant, weights_b=base, progress=True)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print("\nrun_arena 失败：%s: %s" % (type(exc).__name__, exc))
        raise SystemExit(1)

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("完整报告已写入 %s\n" % a.json)

    ci = report.get("score_delta_ci95") or {}
    deltas = report.get("batch_score_delta") or []
    if ci.get("status") == "ok" and deltas:
        per = float(a.hands)
        mean = sum(deltas) / len(deltas)
        print("A − B 每局分差（口径同 2026-09-24 A/B：批次分差 / 每批手数）：")
        print("   均值 %+.2f 分/局   95%% CI [%+.2f, %+.2f]   （%d 个配对批次）\n"
              % (mean / per, ci["low"] / per, ci["high"] / per, len(deltas)))
    hit = None if ci else _find_ci(report)
    if not hit and not ci:
        print("!! 没找到 ci95 统计块，下面是报告骨架（用来定位它藏在哪）：")
        for line in _outline(report)[:60]:
            print("   " + line)
        print()
    if hit:
        path, stats = hit
        ci = stats["ci95"]
        print("配对总分差（A − B），采样单位 = paired batch，n=%s" % stats.get("n"))
        print("   均值 %+.2f" % (stats.get("mean") or 0.0))
        if ci.get("status") == "ok":
            print("   95%% CI [%+.2f, %+.2f]" % (ci["low"], ci["high"]))
        else:
            print("   95%% CI 不可用：%s" % ci.get("reason"))
        print("   （统计块路径：%s）" % path)

    for key in ("conclusion", "conclusion_scope", "a_win_rate", "b_win_rate",
                "a_score", "b_score", "a_wins", "b_wins", "draws",
                "official_fidelity", "simulation_scope"):
        if key in report:
            print("%-20s %s" % (key, report[key]))
    for key in ("fans", "a_fans", "b_fans"):
        if key in report:
            print("%-20s %s" % (key, report[key]))

    print("""
读法：
  conclusion = a_better   → 激进档稳定胜出（CI 下界 > 0），可以上实战
  conclusion = b_better   → 激进档更差，别上
  conclusion = inconclusive → CI 跨 0，**不能**看均值正负下结论，加 --batches 再跑
  insufficient_samples    → batches 太少

注意 run_arena 的自述：它只模拟部分硬规则（杠 / 抓打圈 / 连庄 / 漂分未模拟），
official_fidelity 恒为 False。所以它能回答"速度换番数划不划算"，**不能**回答
财飘链相关的问题——那部分规则它根本没实现。""")


if __name__ == "__main__":
    main()
