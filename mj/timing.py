"""合并 Bot 决策日志与测试房事件，计算动作覆盖率、409 拒绝率与延迟分布。

独立复核最终阻断返修 P1 修复要点：
1. own/all-player timeout 分离——按每场 ``seats``/``decision`` 日志动态
   识别我方物理座位（不假设固定 seat 号，同一物理座位在不同场次可能是
   不同真实玩家）。
2. 409 数量/占比/原因分类（复用 LIVE_TEST_001 冻结证据同一套分类规则）。
3. action/state 请求 p50/p95/p99/max。
4. notify 利用率、seq gap、watchdog/recovery 次数（基于新增的
   ``state_request_metric``；历史日志早于该字段引入，会自然得到全 0——
   如实反映"这份历史日志没有这项数据"，不伪造）。
5. 删除误导性的旧 ``timeout_ratio``（分子是全部玩家超时总数、分母是本
   AI 一人的决策量，量纲不匹配，历史遗留 bug，见
   docs/refactor/LIVE_TEST_001.md §6）。
6. ``decision_attempt_outcome`` 现在可以携带可选 ``game_id``；本模块对
   缺失 game_id 的历史记录，通过 decision_id 反查同一决策的
   ``decision_attempt`` 记录（后者始终携带 game_id）来关联。
7. 同一 decision_id 先 conflict_409 后 fallback_sent，建模为该决策的
   "事件转换历史"——取同一 decision_id 的**最后一条** outcome 作为
   final_outcome，不按原始条数重复计入"决策已终结"的统计；但 409 发生
   次数本身（rejected_409_count）仍按原始 action_rejected 条数统计，
   与 final_outcome 是两个不同维度的计数，互不冲突。
"""
import glob
import json
import os
import re
import statistics
from collections import Counter, defaultdict


def _percentile(values, q):
    if not values:
        return 0
    values = sorted(values)
    index = min(len(values) - 1, int((len(values) - 1) * q))
    return values[index]


def _percentiles(values):
    return {
        "p50": statistics.median(values) if values else 0,
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else 0,
    }


# 与 docs/refactor/LIVE_TEST_001.md §3 冻结证据同一套分类规则（真实
# action_rejected.error 里的 message 字段子串匹配，大小写不敏感）。
_KNOWN_409_REASON_PATTERNS = [
    ("not_your_response_turn", "not your response turn"),
    ("already_passed", "already passed"),
    ("cannot_pass_in_phase_1", "cannot pass in phase 1"),
    ("chi_only_in_chi_window", "chi only in chi window"),
    ("peng_only_in_peng_window", "peng only in peng window"),
]


def _classify_409_reason(error_text):
    lowered = (error_text or "").lower()
    for name, pattern in _KNOWN_409_REASON_PATTERNS:
        if pattern in lowered:
            return name
    if "cannot gang" in lowered or re.search(r"\bgang\b", lowered):
        return "illegal_gang"
    return "other"


def _match_game(game_id, room_id, game_prefix):
    if game_prefix and not (game_id or "").startswith(game_prefix):
        return False
    if room_id and not (game_id or "").startswith(room_id + "_"):
        return False
    return True


def analyze(log_pattern="logs/*.jsonl", event_pattern="models/events/*.json",
            output="models/timing_report.json", room_id=None, game_prefix=None):
    decisions = []
    decision_attempts = []
    decision_outcomes_raw = []
    action_rejected_records = []
    fallback_records = []
    state_request_metrics = []
    notify_records = []
    state_records = []

    # 第一遍：不做 game 过滤，先把 decision_attempt 的 decision_id->game_id
    # 映射建出来——decision_attempt_outcome 可能缺失 game_id（历史日志），
    # 必须靠这张映射反查才能正确过滤/归属。
    decision_id_to_game = {}
    raw_items_by_kind = defaultdict(list)
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
                raw_items_by_kind[kind].append(item)
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

    for kind, items in raw_items_by_kind.items():
        for item in items:
            payload = item.get("payload") or {}
            gid = _resolve_game_id(kind, payload)
            if not _match_game(gid, room_id, game_prefix):
                continue
            if kind == "decision":
                decisions.append(item)
            elif kind == "decision_attempt":
                decision_attempts.append(payload)
            elif kind == "decision_attempt_outcome":
                decision_outcomes_raw.append({**payload, "_resolved_game_id": gid})
            elif kind == "action_rejected":
                action_rejected_records.append(payload)
            elif kind == "fallback_sent":
                fallback_records.append(payload)
            elif kind == "state_request_metric":
                state_request_metrics.append(payload)
            elif kind == "notify":
                notify_records.append(payload)
            elif kind == "state":
                state_records.append(payload)

    # 独立复核规则6：同一 decision_id 的 outcome 记录取"最后一条"为
    # final_outcome（事件转换历史，不是重复脏数据）。日志是逐行 append
    # 写入的，文件内天然按时间顺序；这里按 (文件读取顺序) 保序即可。
    final_outcome_by_decision = {}
    for record in decision_outcomes_raw:
        did = record.get("decision_id")
        if did is not None:
            final_outcome_by_decision[did] = record.get("outcome")
    final_outcome_breakdown = dict(Counter(final_outcome_by_decision.values()))

    # own-seat 动态识别（规则：不能假设固定 seat）——用本对局自己的
    # decision/decision_attempt 记录里出现频率最高的 seat 作为"我方座位"，
    # 不依赖任何硬编码常量或跨场次假设。
    seat_votes = defaultdict(Counter)
    for item in decisions:
        payload = item.get("payload") or {}
        gid = payload.get("game_id")
        seat = payload.get("seat")
        if gid is not None and seat is not None:
            seat_votes[gid][seat] += 1
    for payload in decision_attempts:
        gid = payload.get("game_id")
        seat = payload.get("seat")
        if gid is not None and seat is not None:
            seat_votes[gid][seat] += 1
    my_seat_by_game = {gid: counter.most_common(1)[0][0]
                        for gid, counter in seat_votes.items() if counter}

    # events/*.json：timeout 事件按 own/all 分离统计。
    own_timeouts = Counter()
    all_timeouts = Counter()
    for path in glob.glob(event_pattern):
        basename = os.path.basename(path)
        if room_id and not basename.startswith(room_id + "_"):
            continue
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        event_game_id = data.get("game_id") or os.path.splitext(basename)[0]
        if not _match_game(event_game_id, room_id, game_prefix):
            continue
        my_seat = my_seat_by_game.get(event_game_id)
        for block in data.get("blocks", []):
            for event in block.get("events", []):
                if event.get("type") != "timeout":
                    continue
                kind = (event.get("data") or {}).get("kind", "unknown")
                seat = event.get("seat")
                all_timeouts[kind] += 1
                if my_seat is not None and seat == my_seat:
                    own_timeouts[kind] += 1

    decision_actions = Counter(
        item.get("payload", {}).get("decision", {}).get("action", "unknown")
        for item in decisions
    )
    prepare = [item["payload"]["client_prepare_ms"] for item in decisions
               if "client_prepare_ms" in item.get("payload", {})]
    action_requests = [item["payload"]["action_request_ms"] for item in decisions
                       if "action_request_ms" in item.get("payload", {})]

    # 409 数量/占比/原因分类：直接用 action_rejected 原始条数（不去重），
    # 与冻结证据 docs/refactor/LIVE_TEST_001.md §3 的口径一致——这是
    # "服务端返回了多少次 409"，与 final_outcome_breakdown（"多少个决策
    # 最终停在 conflict_409"）是两个不同维度，互不冲突（规则6）。
    rejected_409_count = len(action_rejected_records)
    reason_breakdown = Counter(
        _classify_409_reason(record.get("error", "")) for record in action_rejected_records
    )
    action_attempts = len(decision_attempts)
    accepted_actions = len(decisions)
    rejected_409_rate = rejected_409_count / action_attempts if action_attempts else 0

    fallback_count = len(fallback_records)
    fallback_accepted = sum(1 for record in fallback_records if record.get("accepted"))
    fallback_success_rate = fallback_accepted / fallback_count if fallback_count else 0

    # state_request_metric：notify 利用率/seq gap/watchdog/recovery 次数、
    # state 请求延迟分布。历史日志（早于本字段引入）不会有这类记录，
    # 此时以下字段自然全 0——如实反映"这份日志没有该数据"，不伪造。
    trigger_counts = Counter(m.get("trigger") for m in state_request_metrics)
    state_gap_count = sum(1 for m in state_request_metrics if m.get("gap"))
    state_elapsed = [m.get("elapsed_ms", 0) for m in state_request_metrics
                     if m.get("elapsed_ms") is not None]
    notify_triggered_requests = trigger_counts.get("notify", 0)
    total_state_requests_new = len(state_request_metrics)
    notify_utilization = (notify_triggered_requests / total_state_requests_new
                          if total_state_requests_new else 0)

    # 向后兼容：旧版 kind="state"/"notify" 记录（真实历史日志走的是这条
    # 路径，早于 state_request_metric 引入）——继续支持，不删除。
    notify_seqs = [item.get("seq") for item in notify_records if item.get("seq") is not None]
    state_requested_seqs = [item.get("requested_seq") for item in state_records]
    notify_to_state_matches = sum(seq in state_requested_seqs for seq in notify_seqs)
    legacy_state_elapsed = [item.get("state_request_ms", 0) for item in state_records]
    legacy_pending = sum(bool(item.get("pending")) for item in state_records)
    legacy_gaps = sum(bool(item.get("gap")) for item in state_records)

    game_ids = set()
    for item in decisions:
        gid = (item.get("payload") or {}).get("game_id")
        if gid:
            game_ids.add(gid)
    for payload in decision_attempts:
        gid = payload.get("game_id")
        if gid:
            game_ids.add(gid)

    report = {
        "action_attempts": action_attempts,
        "accepted_actions": accepted_actions,
        "decision_actions": dict(decision_actions),
        "rejected_409_count": rejected_409_count,
        "rejected_409_rate": rejected_409_rate,
        "rejected_409_reason_breakdown": dict(reason_breakdown),
        "fallback_count": fallback_count,
        "fallback_success_rate": fallback_success_rate,
        "final_outcome_breakdown": final_outcome_breakdown,
        # own/all-player timeout 分离（规则：动态识别我方座位，不假设
        # 固定 seat）。
        "own_discard_timeouts": own_timeouts.get("discard", 0),
        "own_response_timeouts": own_timeouts.get("response", 0),
        "all_player_discard_timeouts": all_timeouts.get("discard", 0),
        "all_player_response_timeouts": all_timeouts.get("response", 0),
        "own_server_timeouts_by_kind": dict(own_timeouts),
        "all_player_server_timeouts_by_kind": dict(all_timeouts),
        "my_seat_by_game": dict(my_seat_by_game),
        "client_timing": {
            "prepare_ms": _percentiles(prepare),
            "action_request_ms": _percentiles(action_requests),
        },
        "state_timing": {
            # 新增（state_request_metric）：可能因历史日志而全 0。
            "requests": total_state_requests_new,
            "elapsed_ms": _percentiles(state_elapsed),
            "gap_count": state_gap_count,
            "trigger_counts": dict(trigger_counts),
            "notify_triggered_requests": notify_triggered_requests,
            "watchdog_triggered_requests": trigger_counts.get("watchdog", 0),
            "recovery_triggered_requests": trigger_counts.get("recovery", 0),
            "notify_utilization": notify_utilization,
            # 向后兼容旧版 kind="state" 记录路径。
            "legacy_requests": len(state_records),
            "legacy_pending": legacy_pending,
            "legacy_gaps": legacy_gaps,
            "legacy_request_p50_ms": statistics.median(legacy_state_elapsed) if legacy_state_elapsed else 0,
            "legacy_request_p95_ms": _percentile(legacy_state_elapsed, 0.95),
        },
        "notify_timing": {
            "notifications": len(notify_records),
            "seq_notifications": len(notify_seqs),
            # 旧口径：与 kind="state" 记录按 seq 直接匹配（真实历史日志
            # 场景下，因为 state 记录本身为 0 条，该值恒为 0——如实反映
            # §5 冻结证据里记录的"落盘路径本身不含逐次 state 记录"问题，
            # 不是 notify 真的没有驱动到任何一次 state 请求）。
            "notify_to_state_matches": notify_to_state_matches,
        },
        "game_count": len(game_ids),
    }
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
    return report
