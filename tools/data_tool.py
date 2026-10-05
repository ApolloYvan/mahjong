"""可查询数据层 CLI：把 DecisionLog JSONL / 门户事件流 JSON 导入 SQLite，
供按 game/round/decision/policy/异常类型查询，生成聚合摘要与脱敏 fixture。

设计依据：`/Users/yuanye/Documents/ChatGPT/麻将大赛/数据保留与日志优化方案.md`
"大模型如何使用"一节，以及 `SONNET5_DATA_REVIEW.md` 返修要求（真正流式导入、
统一脱敏、完整字段对账、summary 覆盖率指标）。

子命令：
    data_tool import-logs <glob...> --db <path> [--batch-size N]
    data_tool import-events <glob...> --db <path>
    data_tool summary --db <path> [--policy ...] [--allow-empty]
    data_tool anomalies --db <path> --kind ... [--limit 30]
    data_tool reconcile --db <path> [--game ...]
    data_tool fixture-export --db <path> --game ... --round ... --output ... [--salt ...] [--synthetic-source]

只用标准库：argparse、glob、json、sqlite3（经 mj.datastore）。
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj import data_import as di
from mj import datastore as ds
from mj.reconcile import reconcile_fields, summarize_fields
from mj.sanitize import MissingSaltError, require_salt, sanitize, sanitize_text

DEFAULT_BATCH_SIZE = 2000


def _import_one_log_file(conn, path: str, batch_size: int = DEFAULT_BATCH_SIZE) -> dict:
    """导入单个决策日志文件（.jsonl 或 .jsonl.gz）。返回统计字典。

    A1 返修：真正的流式导入——不整份读入内存构造 ParseResult 列表，逐行
    直接写入 SQLite，每 ``batch_size`` 行提交一次事务（见
    ``mj.data_import.import_decision_log_stream``）。

    A1 返修：source_files.status 在导入开始时为 'importing'，成功结束后
    显式置为 'complete'；导入过程中若抛出未处理异常，显式置为 'failed'
    并重新抛出——绝不允许一个中途失败的文件停留在"看似已完整导入"的状态。

    P0-4 返修：幂等跳过逻辑收紧为"只有 status='complete' 的相同 SHA 文件
    才能跳过"——旧版对任意已存在的 source_files 行（不管 status 是
    'importing' 还是 'failed'）都直接跳过，导致进程在批量提交后中途崩溃
    的文件永远无法被重新导入，留下"数据库记录说曾经导入过，但实际只有
    部分批次真正写入"的半导入状态。

    现在对 status in ('importing', 'failed') 的既有记录，先调用
    ``delete_data_for_source_file`` 清理该 source_file_id 此前产生的全部
    部分数据（decisions/round_results/errors/decision_attempts），再调用
    ``reset_source_file_for_retry`` 把 source_files 行重置回 'importing'
    状态，然后完整重新走一遍导入流程——保证重试后的最终行数与"从未失败过、
    一次性完整导入"完全一致，不会因为重试而产生重复或残留数据。
    """
    sha256 = ds.sha256_file(path)
    existing = ds.find_source_file_by_sha256(conn, sha256)
    if existing is not None and existing["status"] == "complete":
        return {"path": path, "status": "skipped_duplicate_content",
                "sha256": sha256, "source_file_id": existing["id"]}

    retried_from_status = None
    if existing is not None:
        # status in ('importing', 'failed')：安全重试——先清理该文件此前
        # 产生的部分数据，再重置状态，避免重新导入后新旧数据混杂。
        retried_from_status = existing["status"]
        source_file_id = existing["id"]
        ds.delete_data_for_source_file(conn, source_file_id)
        ds.reset_source_file_for_retry(conn, source_file_id)
        conn.commit()
    else:
        fmt = "decision_jsonl_gz" if path.endswith(".gz") else "decision_jsonl"
        stat = os.stat(path)
        source_file_id = ds.insert_source_file(
            conn, path=path, sha256=sha256, size_bytes=stat.st_size, mtime=stat.st_mtime,
            fmt=fmt, source_type="local_decision_log",
        )
        conn.commit()  # 'importing' 状态先落盘，即使后面崩溃也能看到"曾经开始导入"

    try:
        stats = di.import_decision_log_stream(
            conn, di.open_text_lines(path), source_file_id=source_file_id,
            source_sha256=sha256, batch_size=batch_size,
        )
    except Exception as exc:  # noqa: BLE001 - 必须先标记 failed 再向上抛
        ds.mark_source_file_failed(conn, source_file_id, 0, 0, 0,
                                    sanitize_text(f"{exc.__class__.__name__}: {exc}", limit=300))
        conn.commit()
        return {"path": path, "status": "parse_failed", "sha256": sha256,
                "source_file_id": source_file_id, "error": sanitize_text(str(exc), limit=200)}

    ds.mark_source_file_complete(conn, source_file_id, stats.total_lines, stats.success_lines,
                                  stats.failed_lines)
    conn.commit()
    info = {
        "path": path, "status": "imported", "sha256": sha256,
        "source_file_id": source_file_id,
        "total_lines": stats.total_lines, "success_lines": stats.success_lines,
        "failed_lines": stats.failed_lines,
        "games": stats.games_written, "round_results": stats.round_results_written,
        "decisions": stats.decisions_written, "errors": stats.errors_written,
    }
    if retried_from_status is not None:
        info["status"] = "reimported_after_" + retried_from_status
        info["retried_from_status"] = retried_from_status
    return info


def _import_one_events_file(conn, path: str) -> dict:
    """导入单个门户事件流 JSON 文件（单个游戏对象）。幂等同上。

    门户事件文件本身是单个 JSON 对象（不是 JSONL），本轮沿用既有假设
    整体 ``json.load``（见 mj/data_import.py 顶部说明的已知限制），这类
    文件历史上体量远小于决策日志。"""
    sha256 = ds.sha256_file(path)
    existing = ds.find_source_file_by_sha256(conn, sha256)
    if existing is not None:
        return {"path": path, "status": "skipped_duplicate_content",
                "sha256": sha256, "source_file_id": existing["id"]}

    stat = os.stat(path)
    source_file_id = ds.insert_source_file(
        conn, path=path, sha256=sha256, size_bytes=stat.st_size, mtime=stat.st_mtime,
        fmt="portal_events_json", source_type="portal_event_stream",
    )
    conn.commit()

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        ds.upsert_error(conn, source_file_id=source_file_id, line_no=None,
                         category=di.CATEGORY_JSON_DECODE,
                         summary=sanitize_text(str(exc), limit=160))
        ds.mark_source_file_failed(conn, source_file_id, 1, 0, 1,
                                    sanitize_text(str(exc), limit=300))
        conn.commit()
        return {"path": path, "status": "parse_failed", "sha256": sha256,
                "source_file_id": source_file_id}

    # room_id 取自门户事件流目录约定 portal_events/<room>/<batch>.json
    # （与 tools/pull_all_events.py 写入约定一致）。
    room_id = os.path.basename(os.path.dirname(path)) or None

    try:
        stats = di.import_portal_events_stream(conn, data, source_file_id=source_file_id,
                                                room_id=room_id)
    except Exception as exc:  # noqa: BLE001
        ds.mark_source_file_failed(conn, source_file_id, 0, 0, 0,
                                    sanitize_text(f"{exc.__class__.__name__}: {exc}", limit=300))
        conn.commit()
        return {"path": path, "status": "parse_failed", "sha256": sha256,
                "source_file_id": source_file_id}

    if stats.failed_lines and not stats.success_lines:
        # parse_portal_events_json 判定为"整个文件失败"（缺 game_id 等）：
        # 明确标记 failed，不能表现成 complete。
        ds.mark_source_file_failed(conn, source_file_id, stats.total_lines, stats.success_lines,
                                    stats.failed_lines, "schema validation failed (see errors table)")
        conn.commit()
        return {"path": path, "status": "parse_failed", "sha256": sha256,
                "source_file_id": source_file_id}

    ds.mark_source_file_complete(conn, source_file_id, stats.total_lines, stats.success_lines,
                                  stats.failed_lines)
    conn.commit()
    return {
        "path": path, "status": "imported", "sha256": sha256,
        "source_file_id": source_file_id,
        "round_results": stats.round_results_written, "errors": stats.errors_written,
    }


def cmd_import_logs(args):
    conn = ds.connect(args.db)
    ds.init_schema(conn)
    paths = []
    for pattern in args.globs:
        paths.extend(sorted(glob.glob(pattern)))
    if not paths:
        print(f"no files matched: {args.globs}", file=sys.stderr)
        return 1
    totals = {"imported": 0, "skipped_duplicate_content": 0, "parse_failed": 0}
    for path in paths:
        info = _import_one_log_file(conn, path, batch_size=args.batch_size)
        totals[info["status"]] = totals.get(info["status"], 0) + 1
        print(json.dumps(info, ensure_ascii=False))
    print(f"# summary: {totals}", file=sys.stderr)
    if totals["parse_failed"] and not totals["imported"] and not totals["skipped_duplicate_content"]:
        # A4 返修：全部文件都 parse_failed 时命令必须非零退出，不能表现成成功。
        print("# ERROR: all files failed to parse", file=sys.stderr)
        return 1
    return 0


def cmd_import_events(args):
    conn = ds.connect(args.db)
    ds.init_schema(conn)
    paths = []
    for pattern in args.globs:
        paths.extend(sorted(glob.glob(pattern)))
    if not paths:
        print(f"no files matched: {args.globs}", file=sys.stderr)
        return 1
    totals = {"imported": 0, "skipped_duplicate_content": 0, "parse_failed": 0}
    for path in paths:
        info = _import_one_events_file(conn, path)
        totals[info["status"]] = totals.get(info["status"], 0) + 1
        print(json.dumps(info, ensure_ascii=False))
    print(f"# summary: {totals}", file=sys.stderr)
    if totals["parse_failed"] and not totals["imported"] and not totals["skipped_duplicate_content"]:
        # A4 返修：旧版即使全部 parse_failed 仍返回 0；现在必须非零退出。
        print("# ERROR: all files failed to parse", file=sys.stderr)
        return 1
    return 0


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def _frac(numerator, denominator):
    """带分子分母的百分比字符串（A4 返修：百分比必须带分子和分母，
    不能只给一个裸百分数）。"""
    if denominator == 0:
        return {"numerator": numerator, "denominator": denominator, "pct": None}
    return {"numerator": numerator, "denominator": denominator,
            "pct": round(100.0 * numerator / denominator, 2)}


def cmd_summary(args):
    if not os.path.exists(args.db):
        print(f"database not found: {args.db}", file=sys.stderr)
        return 1
    conn = ds.connect(args.db)
    ds.init_schema(conn)

    where = ""
    params = []
    if args.policy:
        where = "WHERE policy_version = ?"
        params.append(args.policy)

    games_count = conn.execute(f"SELECT COUNT(*) FROM games {where}", params).fetchone()[0]
    decisions_count = conn.execute(
        f"SELECT COUNT(*) FROM decisions {where}", params
    ).fetchone()[0]
    round_results_count = conn.execute("SELECT COUNT(*) FROM round_results").fetchone()[0]
    source_files_count = conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]

    error_rows = conn.execute(
        "SELECT category, COUNT(*) as n FROM errors GROUP BY category ORDER BY n DESC"
    ).fetchall()
    file_rows = conn.execute(
        "SELECT SUM(total_lines) as total, SUM(success_lines) as success, "
        "SUM(failed_lines) as failed FROM source_files"
    ).fetchone()
    file_status_rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM source_files GROUP BY status"
    ).fetchall()

    total_lines = file_rows["total"] or 0
    success_lines = file_rows["success"] or 0
    failed_lines = file_rows["failed"] or 0

    # 唯一/重复 game/round/decision（A4 要求：聚合报告必须包含这些指标）。
    unique_games = games_count
    unique_rounds = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT game_id, round_no FROM round_results)"
    ).fetchone()[0]
    total_round_rows = round_results_count
    unique_decisions = decisions_count
    total_decision_rows = conn.execute(f"SELECT COUNT(*) FROM decisions {where}", params).fetchone()[0]

    # policy/config/build 覆盖率与 unknown_legacy 分桶。
    policy_unknown = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE policy_version = ? OR policy_version IS NULL",
        (di.UNKNOWN_LEGACY,),
    ).fetchone()[0]
    config_unknown = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE config_hash = 'unknown' OR config_hash IS NULL"
    ).fetchone()[0]
    # build_hash 全链路返修（目标二）：覆盖率 = 有真实 build_hash（既非 NULL
    # 也非 BUILD_HASH_UNAVAILABLE 哨兵）的行数 / decisions 总行数。必须带
    # 分子、分母、百分比（_frac 已保证），不得只给裸百分数。
    build_hash_known = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE build_hash IS NOT NULL AND build_hash != ?",
        (di.BUILD_HASH_UNAVAILABLE,),
    ).fetchone()[0]
    build_hash_known_attempts = conn.execute(
        "SELECT COUNT(*) FROM decision_attempts WHERE build_hash IS NOT NULL AND build_hash != ?",
        (di.BUILD_HASH_UNAVAILABLE,),
    ).fetchone()[0]
    total_decision_attempts = conn.execute("SELECT COUNT(*) FROM decision_attempts").fetchone()[0]

    # server/local 对账覆盖率：round_results 里同时存在 server_truth 与
    # local_estimate 的 (game_id, round_no, winner_seat) 占比。
    server_keys = {(r["game_id"], r["round_no"], r["winner_seat"]) for r in conn.execute(
        "SELECT game_id, round_no, winner_seat FROM round_results WHERE source='server_truth'"
    )}
    local_keys = {(r["game_id"], r["round_no"], r["winner_seat"]) for r in conn.execute(
        "SELECT game_id, round_no, winner_seat FROM round_results WHERE source='local_estimate'"
    )}
    both_keys = server_keys & local_keys
    all_round_keys = server_keys | local_keys

    # 错误率/fallback/托管率：以 decisions 总数为分母（正常分母）。
    action_rejected_count = conn.execute(
        "SELECT COUNT(*) FROM errors WHERE category='action_rejected'"
    ).fetchone()[0]
    fallback_sent_count = conn.execute(
        "SELECT COUNT(*) FROM errors WHERE category='fallback_sent'"
    ).fetchone()[0]

    # 延迟 p50/p95/p99：来自 decisions.action_request_ms。
    latencies = sorted(
        row["action_request_ms"] for row in
        conn.execute("SELECT action_request_ms FROM decisions WHERE action_request_ms IS NOT NULL")
        if row["action_request_ms"] is not None
    )

    # 关键字段缺失率：state_hash/policy_version/config_hash 在 decisions 里的缺失比例。
    missing_state_hash = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE state_hash IS NULL"
    ).fetchone()[0]

    summary = {
        "db": args.db,
        "policy_filter": args.policy,
        "source_files": {
            "total": source_files_count,
            "by_status": {row["status"]: row["n"] for row in file_status_rows},
        },
        "parse_success_rate": _frac(success_lines, total_lines),
        "line_totals": {"total": total_lines, "success": success_lines, "failed": failed_lines},
        "games": {"unique": unique_games},
        "round_results": {
            "total_rows": total_round_rows,
            "unique_game_round": unique_rounds,
            "duplicate_rows": total_round_rows - unique_rounds if total_round_rows >= unique_rounds else 0,
        },
        "decisions": {
            "total_rows": total_decision_rows,
            "unique_decision_id": unique_decisions,
        },
        "policy_config_build_coverage": {
            "policy_version_unknown_legacy": _frac(policy_unknown, total_decision_rows),
            "config_hash_unknown": _frac(config_unknown, total_decision_rows),
            # 目标二要求：build_hash 覆盖率必须包含分子/分母/百分比
            # （_frac 已统一保证这个形状），decisions 与 decision_attempts
            # 分别报告——两张表是独立写入路径（decision/decision_attempt
            # 两种 kind），覆盖率可能不同。
            "build_hash_known_decisions": _frac(build_hash_known, total_decision_rows),
            "build_hash_known_decision_attempts": _frac(build_hash_known_attempts,
                                                          total_decision_attempts),
        },
        "server_local_reconciliation_coverage": {
            "server_only": len(server_keys - local_keys),
            "local_only": len(local_keys - server_keys),
            "both": len(both_keys),
            "coverage_of_all_round_keys": _frac(len(both_keys), len(all_round_keys)),
        },
        "errors_by_category": {row["category"]: row["n"] for row in error_rows},
        "action_rejected_rate": _frac(action_rejected_count, total_decision_rows),
        "fallback_sent_rate": _frac(fallback_sent_count, total_decision_rows),
        "action_request_ms_latency": {
            "count": len(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
        "missing_field_rate": {
            "state_hash": _frac(missing_state_hash, total_decision_rows),
        },
    }
    is_empty = (games_count == 0 and decisions_count == 0 and round_results_count == 0)
    if is_empty:
        summary["warning"] = "no data found in database (empty or filter matched nothing)"
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    ds.upsert_aggregate(conn, "latest_summary" + (f"_{args.policy}" if args.policy else ""),
                         summary)
    conn.commit()
    if is_empty and not args.allow_empty:
        # A4 返修：空数据库/空摘要默认非零退出，除非显式传 --allow-empty。
        print("# ERROR: empty summary and --allow-empty not set", file=sys.stderr)
        return 1
    return 0


def cmd_anomalies(args):
    if not os.path.exists(args.db):
        print(f"database not found: {args.db}", file=sys.stderr)
        return 1
    conn = ds.connect(args.db)
    ds.init_schema(conn)

    if args.kind:
        rows = conn.execute(
            "SELECT id, source_file_id, line_no, category, game_id, decision_id, "
            "summary, time FROM errors WHERE category = ? ORDER BY id DESC LIMIT ?",
            (args.kind, args.limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, source_file_id, line_no, category, game_id, decision_id, "
            "summary, time FROM errors ORDER BY id DESC LIMIT ?",
            (args.limit,),
        ).fetchall()

    items = [sanitize(dict(row)) for row in rows]
    print(json.dumps({"kind_filter": args.kind, "count": len(items), "items": items},
                      ensure_ascii=False, indent=2))
    if not items:
        print(f"# no anomalies found for kind={args.kind!r} (db may be empty)", file=sys.stderr)
    return 0


def cmd_reconcile(args):
    """A3 返修：逐字段对账（winner/fan/detail 必比，scores/next_dealer 覆盖
    率统计但不假装 equal），退出码语义：
    - 数据库不存在 → 1
    - round_results 完全无数据 → 2（未发生任何交叉比对）
    - matched=0（有数据但 key 完全不重叠）→ 2
    - 必比字段（winner/fan/detail）任一 mismatch → 1
    - 必比字段全部一致（可能有 scores/next_dealer 的 unavailable）→ 0
    """
    if not os.path.exists(args.db):
        print(f"database not found: {args.db}", file=sys.stderr)
        return 1
    conn = ds.connect(args.db)
    ds.init_schema(conn)

    where = "WHERE game_id = ?" if args.game else ""
    params = [args.game] if args.game else []

    local = {}
    server = {}
    for row in conn.execute(
        f"SELECT game_id, round_no, winner_seat, fan, detail FROM round_results "
        f"{where} AND source = 'local_estimate'" if where
        else "SELECT game_id, round_no, winner_seat, fan, detail FROM round_results "
             "WHERE source = 'local_estimate'",
        params,
    ):
        winner_seat = ds.denorm_winner_seat(row["winner_seat"])
        key = (row["game_id"], row["round_no"], winner_seat)
        local[key] = {"game_id": row["game_id"], "round_no": row["round_no"],
                      "seat": winner_seat, "fan": row["fan"],
                      "detail": json.loads(row["detail"]) if row["detail"] else None,
                      "scores": None, "next_dealer": None}
    for row in conn.execute(
        f"SELECT game_id, round_no, winner_seat, fan, detail, scores, next_dealer "
        f"FROM round_results {where} AND source = 'server_truth'" if where
        else "SELECT game_id, round_no, winner_seat, fan, detail, scores, next_dealer "
             "FROM round_results WHERE source = 'server_truth'",
        params,
    ):
        winner_seat = ds.denorm_winner_seat(row["winner_seat"])
        key = (row["game_id"], row["round_no"], winner_seat)
        server[key] = {"game_id": row["game_id"], "round_no": row["round_no"],
                       "seat": winner_seat, "fan": row["fan"],
                       "detail": json.loads(row["detail"]) if row["detail"] else None,
                       "scores": json.loads(row["scores"]) if row["scores"] else None,
                       "next_dealer": row["next_dealer"]}

    if not local and not server:
        print(f"no round_results data found (game filter={args.game!r}); "
              "this means nothing was reconciled, not that everything matched.",
              file=sys.stderr)
        return 2  # 明确区分于"命令执行本身失败"(1) 和"对账发现差异"

    report = reconcile_fields(local, server)
    print(summarize_fields(report))
    ds.upsert_aggregate(conn, "latest_reconciliation", {
        "matched": report["matched"],
        "must_compare_ok": report["must_compare_ok"],
        "field_coverage": report["field_coverage"],
        "local_only": len(report["local_only"]), "server_only": len(report["server_only"]),
    })
    conn.commit()
    if report["matched"] == 0:
        # 有数据但本地估算与服务端结算完全没有交集，不能表现成"对账通过"。
        print(f"WARNING: matched=0 (local_only={len(report['local_only'])}, "
              f"server_only={len(report['server_only'])}); nothing was actually "
              "cross-checked. Not treated as a passing reconciliation.",
              file=sys.stderr)
        return 2
    if not report["must_compare_ok"]:
        return 1
    return 0


def cmd_fixture_export(args):
    if not os.path.exists(args.db):
        print(f"database not found: {args.db}", file=sys.stderr)
        return 1
    conn = ds.connect(args.db)
    ds.init_schema(conn)

    decisions = [dict(row) for row in conn.execute(
        # 目标二：fixture-export 必须为每条相关记录保留 build_hash（不能
        # 只在 SQLite 里有，导出的 fixture 却丢了这个字段）。
        "SELECT decision_id, round_no, seat, state_hash, policy_version, action, tile, time, "
        "build_hash "
        "FROM decisions WHERE game_id = ? AND round_no = ? ORDER BY time",
        (args.game, args.round),
    )]
    decision_attempts = [dict(row) for row in conn.execute(
        "SELECT decision_id, round_no, seat, state_hash, action, tile, policy_version, "
        "schema_version, config_hash, build_hash, attempted_at, outcome, outcome_detail, "
        "resolved_at "
        "FROM decision_attempts WHERE game_id = ? AND round_no = ? ORDER BY attempted_at",
        (args.game, args.round),
    )]
    round_results = [dict(row) for row in conn.execute(
        "SELECT round_no, winner_seat, fan, detail, scores, next_dealer, source "
        "FROM round_results WHERE game_id = ? AND round_no = ?",
        (args.game, args.round),
    )]
    for rr in round_results:
        rr["winner_seat"] = ds.denorm_winner_seat(rr["winner_seat"])
        if rr.get("detail"):
            rr["detail"] = json.loads(rr["detail"])
        if rr.get("scores"):
            rr["scores"] = json.loads(rr["scores"])

    if not decisions and not round_results and not decision_attempts:
        print(f"no data found for game={args.game!r} round={args.round!r}", file=sys.stderr)
        return 1

    # A2 返修：外发导出必须经过统一脱敏，且缺盐时默认失败（除非显式标记
    # 输入为 synthetic）。game_id 提供可选稳定映射：--pseudonymize-ids 时
    # 用 stable_pseudo_id 替换 game_id，否则保留原值（供本地技术 fixture
    # 直接使用真实/合成 game_id 做人工核对）。
    try:
        salt = require_salt(args.salt, allow_missing_if_synthetic=args.synthetic_source)
    except MissingSaltError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    export_game_id = args.game
    if args.pseudonymize_ids:
        from mj.sanitize import stable_pseudo_id
        export_game_id = stable_pseudo_id("game", args.game, salt)
        for row in decisions:
            row["game_id"] = export_game_id
        for row in round_results:
            row["game_id"] = export_game_id
        for row in decision_attempts:
            row["game_id"] = export_game_id

    # P1-3 返修：sanitize() 传入 salt 时，session_id/user_id/nickname 等
    # 分析关联字段会走带盐稳定假 ID 路径而不是被整体 REDACTED——即使当前
    # decisions/round_results 查询列尚未包含这些字段，也不应该在未来加列
    # 后悄悄变成"因为字段名含 session 就丢失关联"。
    # 目标二：build_hash 字段名不命中任何脱敏模式（既非凭据也非标识符），
    # sanitize() 原样透传，三张表导出后仍各自保留 build_hash。
    decisions = [sanitize(row, salt=salt) for row in decisions]
    round_results = [sanitize(row, salt=salt) for row in round_results]
    decision_attempts = [sanitize(row, salt=salt) for row in decision_attempts]

    fixture = {
        "provenance": "exported_from_local_sqlite_db",
        "synthetic_source": bool(args.synthetic_source),
        "note": "本 fixture 由 data_tool fixture-export 从本地导入数据库导出，"
                "字段完整性取决于导入源数据；已经过 mj.sanitize 统一脱敏处理。",
        "game_id": export_game_id,
        "round_no": args.round,
        "decisions": decisions,
        "round_results": round_results,
        "decision_attempts": decision_attempts,
    }
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(fixture, handle, ensure_ascii=False, indent=2)
    print(f"wrote {args.output} ({len(decisions)} decisions, {len(round_results)} round_results, "
          f"{len(decision_attempts)} decision_attempts)")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="data_tool", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-logs", help="导入决策日志 JSONL/.jsonl.gz（真正流式，有界内存）")
    p.add_argument("globs", nargs="+", help="glob 模式，如 logs/*.jsonl")
    p.add_argument("--db", required=True)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                    help=f"每批提交的行数（默认 {DEFAULT_BATCH_SIZE}）")
    p.set_defaults(func=cmd_import_logs)

    p = sub.add_parser("import-events", help="导入门户事件流 JSON")
    p.add_argument("globs", nargs="+", help="glob 模式，如 portal_events/*/*.json")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_import_events)

    p = sub.add_parser("summary", help="打印聚合摘要（解析成功率/唯一去重/覆盖率/延迟分位数等）")
    p.add_argument("--db", required=True)
    p.add_argument("--policy", default=None)
    p.add_argument("--allow-empty", action="store_true",
                    help="允许空数据库/空摘要仍返回退出码0（默认空数据非零退出）")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("anomalies", help="按类型查询异常样本（已脱敏）")
    p.add_argument("--db", required=True)
    p.add_argument("--kind", default=None,
                    help="错误类别，如 action_rejected/fallback_sent/json_decode_error")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_anomalies)

    p = sub.add_parser("reconcile", help="服务端结算 vs 本地估算逐字段对账")
    p.add_argument("--db", required=True)
    p.add_argument("--game", default=None)
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("fixture-export", help="导出单局单轮脱敏 fixture")
    p.add_argument("--db", required=True)
    p.add_argument("--game", required=True)
    p.add_argument("--round", required=True, type=int)
    p.add_argument("--output", required=True)
    p.add_argument("--salt", default=None, help="稳定假 ID 盐（也可用 MJ_SANITIZE_SALT 环境变量）")
    p.add_argument("--synthetic-source", action="store_true",
                    help="标记输入本身就是合成数据，允许缺盐导出")
    p.add_argument("--pseudonymize-ids", action="store_true",
                    help="用稳定假 ID 替换 game_id（需要盐）")
    p.set_defaults(func=cmd_fixture_export)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
