"""服务端结算账对账：服务端 round_ended 是结算真相，本地 rules.evaluate 只是
local_estimate。本模块提供纯函数比对逻辑，用于自动发现"服务端 fan4、本地 fan2"
一类差异（独立技术评审报告已核验的真实案例：``a_5a06a8f48d67_r1_b4_t0`` 第 5 手，
服务端胡牌 fan4「平胡、财飘、爆头」，本地 hu_detail 记录为 fan2）。

两类真实数据源的 schema 并不相同，因此提供两个专门的解析函数把它们归一化成同一种
比对结构，而不是发明一种两边都不是的"理想格式"：

1. **本地估算**：``mj/logging.py:DecisionLog`` 写入的 ``hu_detail`` 记录
   （JSONL，一行一个 ``{"kind": "hu_detail", "payload": {...}}``），字段见
   ``mj/bot.py:play_game`` 的 ``log.append("hu_detail", {...})`` 调用——
   已含 game_id/seat/round_no/fan/detail/chain_count/piao。
2. **服务端结算真相**：门户事件流（``tools/pull_all_events.py`` 拉取的
   ``portal_events/<room>/<batch>.json``），单个游戏对象含 ``blocks[].events[]``，
   其中 ``type == "round_ended"`` 的事件带 ``seat``（赢家座位）与
   ``data``（含 fan/detail/scores/next_dealer 等）。这与现有
   ``tools/room_audit.py`` 读取的是同一份数据，本模块不重新造轮子，只是把
   它变成可编程比对的结构，供自动化测试和 CI 级对账使用（而不是像
   ``tools/room_audit.py`` 那样只打印文本）。

本模块不做任何网络请求，只处理已经落盘的记录，可完全离线测试。
"""
from typing import Any, Dict, Iterable, List, Optional


def match_key(game_id: Optional[str], round_no: Any, seat: Any) -> tuple:
    """本地估算记录与服务端结算记录的关联键：同一局同一轮同一赢家座位。"""
    return (game_id, round_no, seat)


def local_estimates_from_hu_detail_lines(lines: Iterable[dict]) -> Dict[tuple, dict]:
    """从 hu_detail 类型的 DecisionLog JSONL 记录中提取本地估算。
    ``lines`` 为已经 ``json.loads`` 过的记录（``{"kind": ..., "payload": ...}``）。
    若同一 key 出现多条（不应该发生，但防御性处理），保留最后一条。"""
    result = {}
    for record in lines:
        if record.get("kind") != "hu_detail":
            continue
        payload = record.get("payload") or {}
        key = match_key(payload.get("game_id"), payload.get("round_no"), payload.get("seat"))
        result[key] = {
            "game_id": payload.get("game_id"),
            "round_no": payload.get("round_no"),
            "seat": payload.get("seat"),
            "fan": payload.get("fan"),
            "detail": payload.get("detail"),
        }
    return result


def server_truths_from_portal_game(data: dict) -> Dict[tuple, dict]:
    """从单个门户事件流游戏对象（``pull_all_events`` 拉取的原始 JSON）中提取
    ``round_ended`` 服务端结算真相，按 (game_id, round_no, winner_seat) 索引。

    ``round_no`` 采用与 ``tools/room_audit.py`` 一致的推断规则：从 1 开始，
    每次 ``round_ended`` 后按 ``data.get("round_no")``（若服务端提供）或
    自增计数推进——门户事件里 round_ended 不一定总带 round_no 字段，这里
    与现有已验证工具保持相同口径，避免引入第二套不一致的推断逻辑。
    """
    game_id = data.get("game_id")
    result = {}
    current = 1
    for block in data.get("blocks", []):
        for event in block.get("events") or []:
            if event.get("type") != "round_ended":
                continue
            payload = event.get("data") or {}
            winner = event.get("seat")
            key = match_key(game_id, current, winner)
            result[key] = {
                "game_id": game_id,
                "round_no": current,
                "seat": winner,
                "fan": payload.get("fan"),
                "detail": payload.get("detail"),
                "scores": payload.get("scores"),
                "next_dealer": payload.get("next_dealer"),
            }
            current = (payload.get("round_no") or current) + 1
    return result


def reconcile(local: Dict[tuple, dict], server: Dict[tuple, dict]) -> dict:
    """比对本地估算与服务端结算真相，产出差异报告。

    参数为已经归一化的 dict（``match_key -> record``），由
    ``local_estimates_from_hu_detail_lines``/``server_truths_from_portal_game``
    或其他等价适配函数产出。

    返回结构：
    {
        "matched": 有本地估算也有服务端结果的条数,
        "agreements": fan 完全一致的条数,
        "discrepancies": [{"key": ..., "local_fan": ..., "server_fan": ...,
                            "local_detail": ..., "server_detail": ...}, ...],
        "local_only": [key, ...]   本地有估算但服务端结果缺失（可能是尚未落地/日志丢失）,
        "server_only": [key, ...]  服务端有结果但本地没有 hu_detail（可能是自动胡/托管兜底）,
    }
    """
    all_keys = set(local) | set(server)
    discrepancies = []
    agreements = 0
    matched = 0
    local_only = []
    server_only = []
    for key in sorted(all_keys, key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        loc = local.get(key)
        srv = server.get(key)
        if loc is not None and srv is not None:
            matched += 1
            local_fan = loc.get("fan")
            server_fan = srv.get("fan")
            if local_fan == server_fan:
                agreements += 1
            else:
                discrepancies.append({
                    "key": key,
                    "local_fan": local_fan,
                    "server_fan": server_fan,
                    "local_detail": loc.get("detail"),
                    "server_detail": srv.get("detail"),
                })
        elif loc is not None:
            local_only.append(key)
        else:
            server_only.append(key)
    return {
        "matched": matched,
        "agreements": agreements,
        "discrepancies": discrepancies,
        "local_only": local_only,
        "server_only": server_only,
    }


def summarize(report: dict) -> str:
    """人类可读摘要，供 CLI/日志打印。"""
    lines = [
        f"matched={report['matched']} agreements={report['agreements']} "
        f"discrepancies={len(report['discrepancies'])} "
        f"local_only={len(report['local_only'])} server_only={len(report['server_only'])}",
    ]
    for item in report["discrepancies"][:20]:
        lines.append(
            f"  MISMATCH key={item['key']} local_fan={item['local_fan']} "
            f"server_fan={item['server_fan']} local_detail={item['local_detail']} "
            f"server_detail={item['server_detail']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# A3 返修：完整字段对账（SONNET5_DATA_REVIEW.md P1"对账仍只比较 fan"）。
#
# 旧版 reconcile()/summarize() 只把 fan 相等当成"整条一致"，即使 detail 不同、
# 服务端独有的 scores/next_dealer 完全没被比较——独立复现：本地/服务端 fan
# 都是 2，但 detail 分别是 ["平胡"]/["平胡","爆头"]，旧版仍报 agreements=1、
# 退出码 0。下面的 ``reconcile_fields``/``summarize_fields`` 是新的逐字段
# 对账入口，`tools/data_tool.py cmd_reconcile` 改用这一套。旧的
# ``reconcile``/``summarize`` 保留不删（仍有测试覆盖，且没有生产代码依赖
# 它们做出"完整性"结论），仅供历史兼容与非常简单的 fan-only 场景使用。
# ---------------------------------------------------------------------------

MUST_COMPARE_FIELDS = ("winner", "fan", "detail")
COVERAGE_ONLY_FIELDS = ("scores", "next_dealer")
ALL_COMPARED_FIELDS = MUST_COMPARE_FIELDS + COVERAGE_ONLY_FIELDS


def _normalize_detail(detail):
    """detail 是一组番型标签（如 ["平胡","财飘","爆头"]），标签之间不是
    强顺序敏感的组合（服务端拼接顺序与本地估算的枚举顺序不保证一致），因此
    规范化为"排序后的元组"再比较——顺序不同不算 mismatch，但集合元素不同
    （包括数量不同/重复次数不同）算 mismatch。若本地/服务端任一方给出的
    是非 list 类型（如 None），保留原值供 unavailable 判定使用。"""
    if isinstance(detail, list):
        return tuple(sorted(detail))
    return detail


def _field_status(local_value, server_value, *, is_detail=False) -> str:
    """单字段状态：'equal' | 'mismatch' | 'unavailable'。
    'unavailable'：至少一侧字段本身缺失（None）——本地估算天然不产出
    scores/next_dealer，不能假装 equal，也不该被误判为规则错误（mismatch）。
    """
    if local_value is None or server_value is None:
        # 两侧都缺失也算 unavailable（没有可比较的信息，不是"两边一致"）。
        if local_value is None and server_value is None:
            return "unavailable"
        return "unavailable"
    if is_detail:
        return "equal" if _normalize_detail(local_value) == _normalize_detail(server_value) else "mismatch"
    return "equal" if local_value == server_value else "mismatch"


def reconcile_fields(local: Dict[tuple, dict], server: Dict[tuple, dict]) -> dict:
    """逐字段对账：比较 winner/fan/detail/scores/next_dealer 五个字段，
    每个字段独立标记 equal/mismatch/unavailable，而不是只看 fan 是否相等。

    ``local``/``server`` 的 value dict 期望包含（缺失的字段视为 None）：
    ``seat``（winner，即赢家座位——已经是 match_key 的一部分，这里仍单独
    比较是为了在两侧记录都显式携带 winner/seat 字段时发现自相矛盾的数据，
    例如索引用的 winner_seat 与记录内部字段不一致这种数据质量问题）、
    ``fan``、``detail``、``scores``、``next_dealer``。

    返回结构：
    {
        "matched": 交集条数,
        "field_coverage": {field: {"equal": n, "mismatch": n, "unavailable": n}},
        "records": [{"key", "fields": {field: status}, "local": {...}, "server": {...}}, ...],
        "must_compare_ok": bool  # winner/fan/detail 三个必比字段是否全部无 mismatch
        "local_only": [...], "server_only": [...],
    }
    """
    all_keys = set(local) | set(server)
    matched = 0
    local_only = []
    server_only = []
    field_coverage = {f: {"equal": 0, "mismatch": 0, "unavailable": 0} for f in ALL_COMPARED_FIELDS}
    records = []
    must_compare_ok = True

    for key in sorted(all_keys, key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        loc = local.get(key)
        srv = server.get(key)
        if loc is None:
            server_only.append(key)
            continue
        if srv is None:
            local_only.append(key)
            continue
        matched += 1
        loc_winner = loc.get("seat", key[2])
        srv_winner = srv.get("seat", key[2])
        field_values = {
            "winner": (loc_winner, srv_winner),
            "fan": (loc.get("fan"), srv.get("fan")),
            "detail": (loc.get("detail"), srv.get("detail")),
            "scores": (loc.get("scores"), srv.get("scores")),
            "next_dealer": (loc.get("next_dealer"), srv.get("next_dealer")),
        }
        field_statuses = {}
        for field, (lv, sv) in field_values.items():
            status = _field_status(lv, sv, is_detail=(field == "detail"))
            field_statuses[field] = status
            field_coverage[field][status] += 1
            if field in MUST_COMPARE_FIELDS and status == "mismatch":
                must_compare_ok = False
        records.append({"key": key, "fields": field_statuses, "local": loc, "server": srv})

    return {
        "matched": matched,
        "field_coverage": field_coverage,
        "records": records,
        "must_compare_ok": must_compare_ok,
        "local_only": local_only,
        "server_only": server_only,
    }


def summarize_fields(report: dict, max_samples: int = 20) -> str:
    """人类可读摘要：字段覆盖率统计 + 最多 ``max_samples`` 条不一致样本。"""
    lines = [
        f"matched={report['matched']} must_compare_ok={report['must_compare_ok']} "
        f"local_only={len(report['local_only'])} server_only={len(report['server_only'])}",
    ]
    for field, counts in report["field_coverage"].items():
        total = counts["equal"] + counts["mismatch"] + counts["unavailable"]
        lines.append(
            f"  field={field}: equal={counts['equal']} mismatch={counts['mismatch']} "
            f"unavailable={counts['unavailable']} (of {total})"
        )
    shown = 0
    for record in report["records"]:
        mismatched_fields = [f for f, s in record["fields"].items() if s == "mismatch"]
        if not mismatched_fields:
            continue
        if shown >= max_samples:
            break
        shown += 1
        lines.append(
            f"  MISMATCH key={record['key']} fields={mismatched_fields} "
            f"local={ {f: record['local'].get(f) for f in ('fan','detail','scores','next_dealer')} } "
            f"server={ {f: record['server'].get(f) for f in ('fan','detail','scores','next_dealer')} }"
        )
    return "\n".join(lines)
