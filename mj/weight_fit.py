"""[诊断工具 / historical_replay_diagnostic] 实战牌谱回放：固定真实摸牌
序列，回放不同权重策略，量化听牌率差异。

数据源：logs/*.jsonl 的 decision 记录（含 hand/drawn_tile/melds/dealer/scores）。
分段：墙数回跳 = 新一手；胜负：该段内我方是否有 hu 提交（近似成胡）。

重要限制（阶段3评审确认，不得省略）：
- 本工具输出的是历史牌谱回放诊断（source="historical_replay_diagnostic"），
  不是真实胜率，也不构成因果性的"改进证明"。
- 反事实偏差：回放使用的是**固定的真实摸牌序列**——如果某一步换成候选
  权重会做出不同的弃牌选择，真实对局中后续的摸牌序列并不一定仍然按原
  样发生（其他三家的应对、补花/杠后的摸牌位置等都会随之变化）。因此
  "候选权重听牌率更高"不能直接等价于"候选权重实战胜率更高"。
- 本工具从不自动写入生产权重文件（``mj/fit.py`` 的 ``load_weights()``
  只读，本模块没有任何写文件逻辑）；权重切换需要人工评审后手动更新。
- 无真实日志（``logs/*.jsonl`` 不存在或解析不出任何 segment）时，
  ``evaluate_configs()`` 返回 ``{"status": "insufficient_data", ...}``，
  不得用空数据宣称某组权重"更好"；``python -m mj.weight_fit`` CLI 遇到
  这种情况以非零退出码结束。
"""
import glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.ev import pair_route_bonus
from mj.fit import load_weights
from mj.joker_ev import joker_plan_value
from mj.rules import evaluate
from mj.shanten import combined_route, shanten
from .tiles import JOKER, TILE_INDEX, to_counts


def parse_segments(pattern="logs/*.jsonl"):
    """只按 discard 决策分段（墙数跳升 = 新一手）；hu 决策归属当前段。"""
    segments = []
    for path in sorted(glob.glob(pattern)):
        per_game = defaultdict(list)
        with open(path, encoding="utf-8", errors="ignore") as source:
            for line in source:
                if '"decision"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload") or {}
                gid = str(payload.get("game_id") or "")
                if not gid or (payload.get("decision") or {}).get("action") is None:
                    continue
                if payload.get("wall_remaining") is None:
                    continue
                per_game[gid].append(payload)
        for gid, payloads in per_game.items():
            cur = []
            won = False
            prev_wall = None
            for payload in payloads:
                wall = payload.get("wall_remaining")
                action = (payload.get("decision") or {}).get("action")
                if prev_wall is not None and wall > prev_wall + 5:
                    if len(cur) >= 4:
                        segments.append((gid, cur, won))
                    cur = []
                    won = False
                if action == "discard":
                    cur.append(payload)
                    prev_wall = wall
                elif action == "hu" and cur:
                    won = True
                elif action in ("chi", "peng", "gang") and cur:
                    prev_wall = wall
            if len(cur) >= 4:
                segments.append((gid, cur, won))
    return segments


def segment_facts(payloads):
    seat = payloads[0].get("seat")
    dealer = payloads[0].get("dealer") == seat
    return seat, dealer


def replay(segment_payloads, weights, won, rules_extra=None):
    """固定摸牌序列回放：返回听牌状态（真实摸牌序 + 策略弃牌）。"""
    seat, dealer = segment_facts(segment_payloads)
    rules = {"_weights": weights, "dealer_hint": dealer}
    if rules_extra:
        rules.update(rules_extra)
    hand = list(segment_payloads[0].get("hand") or [])
    drawn0 = segment_payloads[0].get("drawn_tile")
    if drawn0 in hand and len(hand) % 3 == 2:
        hand.remove(drawn0)
    tenpai_at = None
    meld_groups = 0
    for index, payload in enumerate(segment_payloads):
        melds_all = payload.get("melds") or []
        meld_target = 0
        if seat is not None and len(melds_all) == 4 and isinstance(melds_all[seat], list):
            meld_target = len(melds_all[seat])
        drawn = payload.get("drawn_tile")
        if drawn:
            hand.append(drawn)
        while meld_groups < meld_target:
            meld_groups += 1
            hand = hand[:-2] if len(hand) >= 2 else hand
        counts = to_counts(hand)
        current, _ = combined_route(counts, meld_groups)
        if current == 0 and tenpai_at is None:
            tenpai_at = index
        choice = policy_discard(hand, meld_groups, dealer, weights, rules)
        if choice in hand:
            hand.remove(choice)
        elif hand:
            hand.pop()
    return {"tenpai": tenpai_at is not None, "tenpai_turn": tenpai_at}


def policy_discard(hand, meld_groups, dealer, weights, rules):
    rules = dict(rules or {})
    if dealer:
        rules["dealer_hint"] = True
    best, best_key = None, None
    for tile in sorted(set(hand)):
        left = list(hand)
        left.remove(tile)
        counts = to_counts(left)
        std = shanten(counts, meld_groups)
        current, route_waits = combined_route(counts, meld_groups)
        key = -current * weights["b_shanten"]
        if current <= 2:
            key += len(route_waits) * weights["b_ukeire"]
        plan = joker_plan_value(counts, meld_groups, std,
                                weights["b_shanten"], rules, weights)
        if plan is not None and plan > key:
            key = plan
        if meld_groups == 0:
            key += pair_route_bonus(counts, meld_groups, weights)
        idx = TILE_INDEX[tile]
        if idx >= 27:
            before = counts[idx]
            key += weights["b_honor"] if before == 0 else (-weights["b_honor"] if before == 1 else -weights["b_honor"] * 3)
        key -= (rules or {}).get("_defense", {}).get(tile, 0)
        if best_key is None or key > best_key:
            best, best_key = tile, key
    return best


def evaluate_configs(batches_filter=None, grid=None):
    """返回诊断报告列表（正常情况），或 ``{"status": "insufficient_data",
    "reason": ...}``（无可用真实日志时）——调用方（含 CLI）必须显式处理
    后者，不得把空列表/零值报告误当作"已评测出结论"。"""
    segments = parse_segments()
    if batches_filter:
        segments = [s for s in segments if any(f in s[0] for f in batches_filter)]
    if not segments:
        return {
            "status": "insufficient_data",
            "reason": "no decision segments parsed from logs/*.jsonl "
                       "(missing logs, or no segment with >=4 discards)",
            "source": "historical_replay_diagnostic",
        }
    base = load_weights()
    grid = grid or [
        {"tag": "baseline", "weights": {}},
        {"tag": "ukeire200", "weights": {"b_ukeire": 200}},
        {"tag": "ukeire150", "weights": {"b_ukeire": 150}},
        {"tag": "pair60", "weights": {"b_pair": 60}},
        {"tag": "joker80", "weights": {"b_joker": 80}},
        {"tag": "honor500", "weights": {"b_honor": 500}},
        {"tag": "progress300", "weights": {"b_progress": 300}},
    ]
    report = []
    for item in grid:
        weights = {**base, **item["weights"]}
        total = lost = tenpai = tenpai_lost = 0
        turns = []
        for gid, segment, won in segments:
            outcome = replay(segment, weights, won)
            total += 1
            tenpai += outcome["tenpai"]
            if outcome["tenpai"]:
                turns.append(outcome["tenpai_turn"])
            if not won:
                lost += 1
                tenpai_lost += outcome["tenpai"]
        report.append({
            "tag": item["tag"],
            "source": "historical_replay_diagnostic",
            "counterfactual_bias_warning": (
                "回放使用固定的真实摸牌序列；候选权重若在某一步做出不同弃牌，"
                "真实对局中的后续摸牌序列不保证仍按原样发生，因此本报告的"
                "tenpai_rate 差异不能直接等价于真实胜率或因果性改进。"
            ),
            "hands": total,
            "tenpai_rate": round(tenpai / max(1, total), 3),
            "tenpai_rate_lost": round(tenpai_lost / max(1, lost), 3) if lost else None,
            "avg_tenpai_turn": round(sum(turns) / len(turns), 2) if turns else None,
        })
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", nargs="*", default=None, help="只评测包含这些前缀的房间")
    args = parser.parse_args()
    rows = evaluate_configs(batches_filter=args.filter)
    if isinstance(rows, dict) and rows.get("status") == "insufficient_data":
        print(json.dumps(rows, ensure_ascii=False))
        sys.exit(1)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
