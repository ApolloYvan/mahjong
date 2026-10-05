"""S2 关卡：按三个头分别报告 top-1 一致率（验证标签 = 验证房间里"另一半房间选出的高手"的实际动作）。

    python3 tools/train/eval_heads.py --smoke
    caffeinate -i nice -n 15 python3 tools/train/eval_heads.py --max-minutes 30

- 出牌（摸牌阶段、非胡牌态；标签是打哪张/自己杠）：网络 vs 生产 vs ``mj/nn_discard.py``（它只在"同向听层"里挑、
  只管 chain=0/piao=0 的弃牌，这里按生产同样的口径取同向听层再让它选，覆盖不到的局面不计入它的分母）；
- 响应（吃低/中/高、碰、明杠、过）：网络 vs 生产。注意生产 ``mj/bot.py::_can_chi`` 恒为假——生产从不吃；
- 胡/飘/弃胡（摸牌阶段胡牌态）：网络 vs 生产，三分类（胡 / 飘=打财神 / 弃胡=打别的牌）。
每个头再按"手里有/没有财神"分层。生产 = ``models/weights.json`` 的当前配置（``frozen_file_weights``，同 arena2）；
网络不可用（回落）算错。"过"按 keep=0.2 降采样，统计时按 1/keep 加权（报告加权和不加权两个口径）。
"""
import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from build_dataset import iter_decisions, room_of, split_of  # noqa: E402
from mining_common import discover_files  # noqa: E402
from mj import nn_discard  # noqa: E402
from mj.bot import choose_action  # noqa: E402
from mj.fit import frozen_file_weights, load_weights  # noqa: E402
from mj.nn import encode as E  # noqa: E402
from mj.nn import policy  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts, visible_counts  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "train")


def head_class(snap, y):
    """(头, 类别标签)：D 头类别=动作下标；H 头 0 胡/1 飘/2 弃胡；R 头=动作下标。"""
    if snap["phase"] != "draw":
        return "R", y
    if E.A_HU in E.legal_actions(snap):
        return "H", (0 if y == E.A_HU else 1 if y == E.JOKER_IDX else 2)
    return "D", y


def action_class(snap, head, action):
    if action is None:
        return None
    y = E.action_to_index(snap, action)
    if y is None:
        return None
    if head == "H":
        return 0 if y == E.A_HU else 1 if y == E.JOKER_IDX else 2
    return y


def nn_discard_pick(snap, weights):
    god = snap.get("god") or {}
    if (god.get("chain_count") or 0) or not nn_discard._load():
        return None
    me = snap["seat"]
    hand = list(snap["my_hand"])
    mg = len(snap["melds"][me])
    best, tier = None, []
    scores = {}
    for t in sorted(set(hand)):
        rem = list(hand)
        rem.remove(t)
        scores[t] = route_shanten(to_counts(rem), mg)
    m = min(scores.values())
    tier = [t for t in scores if scores[t] == m]
    visible = visible_counts(hand, snap.get("discards") or [], snap.get("melds") or [])
    rules = {"_ctx": {"wall": snap.get("wall_remaining", 60), "dealer": snap.get("dealer") == me,
                      "opp_melds": [len(snap["melds"][s]) for s in range(4) if s != me],
                      "catch_play": bool(god.get("catch_play")), "round_no": snap.get("round_no")}}
    pick = nn_discard.choose(tier, hand, mg, visible, weights, rules)
    return TILE_INDEX[pick] if pick else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-per-head", type=int, default=2500)
    ap.add_argument("--pass-keep", type=float, default=0.2)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        args.max_per_head = 60
    model = args.model or (os.path.join(CACHE, "nn_policy_smoke.json") if args.smoke
                           else os.path.join(ROOT, "models", "nn_policy.json"))
    if not os.path.exists(model):
        print("没有模型文件 %s（先跑 train_s2.py）" % model)
        sys.exit(2)
    os.environ["MJ_NN_POLICY_PATH"] = model
    with open(os.path.join(CACHE, "masters.json"), encoding="utf-8") as f:
        masters = json.load(f)["masters_from"]
    net, err = policy.get_net()
    if net is None:
        print("模型加载失败：%s" % err)
        sys.exit(2)
    started = time.time()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None
    files = [p for p in discover_files() if split_of(room_of(os.path.basename(p))[1]) == "val"]
    random.Random(args.seed).shuffle(files)
    # stats[(头, 分层)] = {"n","wn","net","prod","nnd","nnd_n"}（加权/不加权各记一份）
    st = defaultdict(lambda: defaultdict(float))
    n_head = defaultdict(int)
    nn_fallback = defaultdict(int)
    weights = load_weights()
    with frozen_file_weights():
        for path in files:
            if deadline and time.time() > deadline:
                break
            if all(n_head[h] >= args.max_per_head for h in "DRH"):
                break
            parity = room_of(os.path.basename(path))[1] % 2
            sel = set(masters.get(str(1 - parity), []))

            def keep(name, phase, action):
                if name not in sel:
                    return 0.0
                return args.pass_keep if action.get("action") == "pass" else 1.0
            for rec in iter_decisions(path, keep=keep, seed=args.seed):
                snap, ctx, action = rec["snapshot"], rec["ctx"], rec["action"]
                y = E.action_to_index(snap, action)
                if y is None:
                    continue
                head, cls = head_class(snap, y)
                if n_head[head] >= args.max_per_head:
                    continue
                n_head[head] += 1
                w = 1.0 / rec["keep_prob"]
                joker = "有财神" if JOKER in snap["my_hand"] else "无财神"
                nn_act, log = policy.nn_action(snap, ctx, net=net, budget_s=10.0)
                if nn_act is None:
                    nn_fallback[head] += 1
                prod = choose_action(snap, None)
                nn_ok = action_class(snap, head, nn_act) == cls
                prod_ok = action_class(snap, head, prod) == cls
                nd = None
                if head == "D" and y < 34:
                    nd = nn_discard_pick(snap, weights)
                for key in ((head, "全部"), (head, joker)):
                    s = st[key]
                    s["n"] += 1
                    s["w"] += w
                    s["net"] += nn_ok
                    s["net_w"] += w * nn_ok
                    s["prod"] += prod_ok
                    s["prod_w"] += w * prod_ok
                    if nd is not None:
                        s["nnd_n"] += 1
                        s["nnd"] += nd == y
                if head == "R" and cls != E.A_PASS:
                    s = st[("R", "非过")]
                    s["n"] += 1
                    s["net"] += nn_ok
                    s["prod"] += prod_ok
    names = {"D": "出牌", "R": "响应", "H": "胡飘弃胡"}
    print("=== eval_heads（%s，%.0fs；验证房间里另一半选出的高手，%s） ===" % (
        "冒烟" if args.smoke else "全量", time.time() - started, os.path.basename(model)))
    print("%-8s %-6s %6s | %-16s | %-16s | %s" % ("头", "分层", "n", "网络 加权/不加权", "生产 加权/不加权", "nn_discard(覆盖 n)"))
    for head in "DRH":
        for strat in ("全部", "有财神", "无财神", "非过"):
            s = st.get((head, strat))
            if not s or not s["n"]:
                continue
            nd = "%.3f (n=%d)" % (s["nnd"] / s["nnd_n"], s["nnd_n"]) if s["nnd_n"] else "-"
            if strat == "非过":
                print("%-8s %-6s %6d | 网络 %.3f           | 生产 %.3f           | -" % (
                    names[head], strat, s["n"], s["net"] / s["n"], s["prod"] / s["n"]))
            else:
                print("%-8s %-6s %6d | %.3f / %.3f     | %.3f / %.3f     | %s" % (
                    names[head], strat, s["n"], s["net_w"] / s["w"], s["net"] / s["n"],
                    s["prod_w"] / s["w"], s["prod"] / s["n"], nd))
    print("网络回落（不可用，算错）：%s；各头样本数 %s" % (dict(nn_fallback) or "无", dict(n_head)))


if __name__ == "__main__":
    main()
