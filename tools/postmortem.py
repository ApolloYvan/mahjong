"""对局复盘（postmortem）：只基于真实证据——已接受动作（``decision``）、
``decision_attempt``/``decision_attempt_outcome`` 决策链、``action_rejected``
（409 拒绝）与去重后的 ``round_ended`` 结算事件。不引入任何本地估算或
臆测；任何证据缺失的维度都显式输出 ``"unavailable"``，不用 0 或空值
伪装成"确认过、结果是零"。

独立复核最终阻断返修（原任务七 + 本轮 P1 item2）：
1. 删除对已移除私有函数 ``mj.responses._claim_worthit`` 的导入——不恢复
   这个私有旧函数，旧版基于它的"吃碰值不值"重放分析整体废弃。
2. 只分析：``decision``（真实已接受动作）、``decision_attempt``/
   ``decision_attempt_outcome``（决策尝试与最终结果，事件转换历史按
   最后一条取 final_outcome，与 mj.timing 同一口径）、``action_rejected``
   （409 拒绝及分类）、``round_ended``（去重后的结算真相，复用
   ``mj.report._extract_round_ended_events``）。
3. 无参数时不得默认为旧房间——``room_id`` 必须显式提供；缺失或所有
   证据源都为空时给出清晰的非零退出码，不静默产出一份看似正常的空报告。
"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.report import _extract_round_ended_events  # noqa: E402
from mj.responses import claim_assessment  # noqa: E402
from mj.timing import _classify_409_reason  # noqa: E402

UNAVAILABLE = "unavailable"

EXIT_OK = 0
EXIT_MISSING_ROOM_ID = 2
EXIT_NO_EVIDENCE = 3


def _match_game(game_id, room_id):
    return bool(game_id) and game_id.startswith(room_id + "_")


def _load_decision_log_records(room_id, log_pattern):
    """加载并按 kind 分桶，只保留属于 ``room_id`` 的记录（按 game_id 前缀
    匹配；``decision_attempt_outcome`` 缺失 game_id 时通过 decision_id 反查
    同一决策的 ``decision_attempt`` 记录，与 mj.timing 同一处理方式）。"""
    decision_id_to_game = {}
    raw_by_kind = defaultdict(list)
    for path in glob.glob(log_pattern):
        with open(path, encoding="utf-8", errors="ignore") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = item.get("kind")
                if kind is None:
                    continue
                raw_by_kind[kind].append(item)
                if kind == "decision_attempt":
                    payload = item.get("payload") or {}
                    did = payload.get("decision_id")
                    gid = payload.get("game_id")
                    if did is not None and gid is not None:
                        decision_id_to_game[did] = gid

    def _resolve_game_id(kind, payload):
        gid = payload.get("game_id")
        if gid:
            return gid
        if kind == "decision_attempt_outcome":
            return decision_id_to_game.get(payload.get("decision_id"), "")
        return ""

    scoped = defaultdict(list)
    for kind, items in raw_by_kind.items():
        for item in items:
            payload = item.get("payload") or {}
            gid = _resolve_game_id(kind, payload)
            if not _match_game(gid, room_id):
                continue
            scoped[kind].append(payload)
    return scoped


def _load_round_ended_events(room_id, event_pattern):
    """加载 room_id 前缀匹配的事件文件，用 seats 把物理座位映射到
    user_id/name，返回 (rounds, seats_by_game) —— rounds 已按
    (game_id, round_no) 去重（复用 mj.report 的权威提取逻辑）。"""
    rounds = []
    seats_by_game = {}
    for path in glob.glob(event_pattern):
        basename = os.path.basename(path)
        if not basename.startswith(room_id + "_"):
            continue
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        game_id = data.get("game_id") or os.path.splitext(basename)[0]
        if not _match_game(game_id, room_id):
            continue
        seats_by_game[game_id] = data.get("seats") or []
        for round_event in _extract_round_ended_events(data):
            rounds.append({"game_id": game_id, **round_event})
    return rounds, seats_by_game


def _identify_my_seat_by_game(decision_records):
    """规则：不能假设固定 seat——同一 game_id 下，decision/decision_attempt
    记录里出现频率最高的 seat 视为"我方座位"（与 mj.timing 同一识别方式，
    只依赖本对局自身的真实记录，不依赖任何硬编码常量）。"""
    votes = defaultdict(Counter)
    for kind in ("decision", "decision_attempt"):
        for payload in decision_records.get(kind, []):
            gid = payload.get("game_id")
            seat = payload.get("seat")
            if gid is not None and seat is not None:
                votes[gid][seat] += 1
    return {gid: counter.most_common(1)[0][0] for gid, counter in votes.items() if counter}


def _claim_effectiveness(decision_records, round_events):
    """按真实已接受 chi/peng 做观察性复盘，不把重放结果伪装成反事实收益。"""
    claims = defaultdict(list)
    open_melds = Counter()
    classifications = Counter()
    by_action = {"chi": Counter(), "peng": Counter()}
    first_meld = []
    for payload in decision_records.get("decision", []):
        decision = payload.get("decision") or {}
        action = decision.get("action")
        key = (payload.get("game_id"), payload.get("round_no"))
        # 「0/1/2+ 次副露」的局分桶要以实际打开的副露组为准，故包含
        # gang；下面的向听审计仍严格只统计本任务范围内的 chi/peng。
        if action in ("chi", "peng", "gang"):
            open_melds[key] += 1
        if action not in ("chi", "peng"):
            continue
        hand = payload.get("hand") or payload.get("my_hand") or []
        tile = decision.get("tile")
        take = decision.get("tiles") if action == "chi" else [tile, tile]
        if not hand or not tile or not take:
            continue
        snapshot = {**payload, "my_hand": hand, "window_tile": tile}
        assessment = claim_assessment(snapshot, tuple(take))
        before = assessment["before_shanten"]
        after = assessment["after_shanten"]
        change = ("unavailable" if after is None else
                  "improve" if after < before else "same" if after == before else "worsen")
        classifications[change] += 1
        by_action[action][change] += 1
        claims[key].append(assessment)
        if len(_melds_for_payload(payload)) == 0:
            first_meld.append({
                "action": action, "before_shanten": before,
                "before_ukeire": assessment["before_ukeire"],
                "after_shanten": after, "after_ukeire": assessment["after_ukeire"],
                "allowed_by_new_gate": assessment["allowed"],
            })

    seats = _identify_my_seat_by_game(decision_records)
    round_score = {}
    for event in round_events:
        seat = seats.get(event["game_id"])
        scores = event.get("scores") or []
        if seat is not None and seat < len(scores):
            round_score[(event["game_id"], event.get("round_no"))] = scores[seat]
    buckets = {"0": {"rounds": 0, "wins": 0, "net_score": 0},
               "1": {"rounds": 0, "wins": 0, "net_score": 0},
               "2+": {"rounds": 0, "wins": 0, "net_score": 0}}
    for key, score in round_score.items():
        count = open_melds.get(key, 0)
        bucket = "0" if count == 0 else "1" if count == 1 else "2+"
        buckets[bucket]["rounds"] += 1
        buckets[bucket]["wins"] += int(score > 0)
        buckets[bucket]["net_score"] += score
    return {
        "observation_note": (
            "仅为真实已接受声明与该局结算的观察性相关；不控制牌山、对手或策略变化，"
            "不得解释为吃碰的反事实收益。"),
        "claim_count_by_round": buckets,
        "route_shanten": dict(classifications),
        "by_action": {action: dict(counts) for action, counts in by_action.items()},
        "first_meld": {"count": len(first_meld), "before_after": first_meld},
    }


def _melds_for_payload(payload):
    melds = payload.get("melds") or []
    seat = payload.get("seat", -1)
    if isinstance(melds, dict):
        return melds.get(str(seat), melds.get(seat, []))
    if isinstance(melds, list) and 0 <= seat < len(melds) and isinstance(melds[seat], list):
        return melds[seat]
    return []


def build_postmortem(room_id, log_pattern="logs/*.jsonl", event_pattern="models/events/*.json"):
    if not room_id:
        raise ValueError("room_id 必须显式提供，不得默认为任何旧房间")

    decision_records = _load_decision_log_records(room_id, log_pattern)
    round_events, seats_by_game = _load_round_ended_events(room_id, event_pattern)

    has_log_evidence = any(decision_records.get(kind) for kind in
                           ("decision", "decision_attempt", "decision_attempt_outcome",
                            "action_rejected"))
    has_round_evidence = bool(round_events)

    report = {"room_id": room_id, "has_evidence": has_log_evidence or has_round_evidence}

    # 1) 已接受动作（真实 decision 记录）。
    decisions = decision_records.get("decision", [])
    report["accepted_actions"] = len(decisions) if decisions else UNAVAILABLE
    report["accepted_action_breakdown"] = (
        dict(Counter(p.get("decision", {}).get("action", "unknown") for p in decisions))
        if decisions else UNAVAILABLE
    )

    # 2) decision_attempt / decision_attempt_outcome 决策链——取每个
    #    decision_id 的最后一条 outcome 作为 final_outcome（事件转换历史，
    #    与 mj.timing 同一口径，不是重复脏数据）。
    attempts = decision_records.get("decision_attempt", [])
    outcomes = decision_records.get("decision_attempt_outcome", [])
    if attempts or outcomes:
        final_outcome_by_decision = {}
        for record in outcomes:
            did = record.get("decision_id")
            if did is not None:
                final_outcome_by_decision[did] = record.get("outcome")
        report["decision_attempts"] = len(attempts)
        report["final_outcome_breakdown"] = dict(Counter(final_outcome_by_decision.values()))
    else:
        report["decision_attempts"] = UNAVAILABLE
        report["final_outcome_breakdown"] = UNAVAILABLE

    # 3) action_rejected（409 拒绝）及分类。
    rejected = decision_records.get("action_rejected", [])
    if rejected:
        report["rejected_409_count"] = len(rejected)
        report["rejected_409_reason_breakdown"] = dict(Counter(
            _classify_409_reason(record.get("error", "")) for record in rejected
        ))
    else:
        report["rejected_409_count"] = UNAVAILABLE
        report["rejected_409_reason_breakdown"] = UNAVAILABLE

    # 4) round_ended 结算真相：本 AI 的胜/负/流局（动态识别座位/玩家，
    #    不假设固定 seat 或硬编码 user_id）。
    if round_events:
        my_seat_by_game = _identify_my_seat_by_game(decision_records)
        rounds_analyzed = len(round_events)
        draws = sum(1 for r in round_events if r.get("is_draw"))
        wins = 0
        losses = 0
        my_user_id = None
        for round_event in round_events:
            gid = round_event["game_id"]
            seats = seats_by_game.get(gid) or []
            my_seat = my_seat_by_game.get(gid)
            if my_seat is not None and my_seat < len(seats):
                candidate_uid = seats[my_seat].get("user_id")
                if candidate_uid:
                    my_user_id = my_user_id or candidate_uid
            if round_event.get("is_draw"):
                continue
            scores = round_event.get("scores") or []
            if my_seat is not None and my_seat < len(scores):
                if scores[my_seat] > 0:
                    wins += 1
                else:
                    losses += 1
        report["rounds_analyzed"] = rounds_analyzed
        report["draws"] = draws
        report["my_wins"] = wins
        report["my_losses"] = losses
        report["my_user_id"] = my_user_id or UNAVAILABLE
    else:
        report["rounds_analyzed"] = UNAVAILABLE
        report["draws"] = UNAVAILABLE
        report["my_wins"] = UNAVAILABLE
        report["my_losses"] = UNAVAILABLE
        report["my_user_id"] = UNAVAILABLE

    # seat_breakdown：明确只是物理座位维度的参考视图（与 mj.report 同一
    # 免责声明），不代表任何单一玩家的战绩结论。
    report["seat_breakdown_note"] = (
        "本节仅供参考：物理座位号在不同场次可能对应不同真实玩家，"
        "不得当作单一玩家的战绩结论——权威结果见 my_wins/my_losses/draws。"
    )
    report["claim_effectiveness"] = _claim_effectiveness(decision_records, round_events)
    return report


def cli_main(argv):
    if len(argv) < 2 or not argv[1].strip():
        print("用法: python3 tools/postmortem.py <room-id> [log-glob] [event-glob]", file=sys.stderr)
        print("错误: room-id 必须显式提供，不得省略（不会默认为任何旧房间）。",
              file=sys.stderr)
        return EXIT_MISSING_ROOM_ID
    room_id = argv[1].strip()
    log_pattern = argv[2] if len(argv) > 2 else "logs/*.jsonl"
    event_pattern = argv[3] if len(argv) > 3 else "models/events/*.json"
    try:
        report = build_postmortem(room_id, log_pattern=log_pattern, event_pattern=event_pattern)
    except ValueError as error:
        print(f"错误: {error}", file=sys.stderr)
        return EXIT_MISSING_ROOM_ID
    if not report.get("has_evidence"):
        print(f"房间 {room_id} 未找到任何真实证据"
              "（logs/*.jsonl 与 models/events/*.json 均无匹配记录）。",
              file=sys.stderr)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_NO_EVIDENCE
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv))
