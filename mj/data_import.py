"""导入解析器：把 DecisionLog JSONL / 门户事件流 JSON 解析并**直接流式写入**
``mj.datastore``，不在内存中累积整份文件的记录列表。

返修背景（SONNET5_DATA_REVIEW.md P0"导入不是真正的流式处理"）：旧版
``parse_decision_log_lines()`` 逐行读取文件没错，但把全部 ``GameRow``/
``DecisionRow``/``RoundResultRow``/``ErrorRow`` 累积进一个 ``ParseResult``
列表，文件解析完才整体写库——10,000 行会同时保留 10,000 个 Python 对象，
历史 224 万行级别的文件会产生大量常驻对象，不构成可扩展的流式导入。

本次重构的核心变化：
- ``iter_decision_log_lines()``：生成器，逐行 yield ``LineOutcome``
  （单行的翻译结果），不做任何跨行累积；
- ``import_decision_log_stream()``：真正的生产导入入口，消费上面的生成器，
  **逐行直接写入 SQLite**，每 ``batch_size``（默认 2000，可配置）行提交
  一次事务；内存占用是 O(1)（不随文件总行数增长），不是"文件是逐行打开的"
  这种表面流式。
- 单行 try/except：坏行只记一条 error，不中止后续行处理，也不回滚已经
  成功写入、已提交的批次（未提交的当前批次里其它行仍会继续处理并计入
  下一次提交）。
- 所有写入数据库/落盘的摘要文本统一经过 ``mj.sanitize.sanitize_text()``——
  修复"脱敏只做截断不做替换"的 P0（历史反例：UTF-8 replace 行、
  kind=error 的 Bearer 文本、action_rejected.payload.error 中的 token
  均可能携带真实令牌）。
"""
import gzip
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

from . import datastore as ds
from .sanitize import sanitize_text

# 错误类别：覆盖数据保留方案"429、timeout、409、fallback、托管、解析失败"的口径，
# 加上 JSON/UTF-8/schema 三类文件级解析失败，以及"无法分类的通用错误"
# （A4 返修：generic kind=error 不得默认归为 timeout）。
CATEGORY_JSON_DECODE = "json_decode_error"
CATEGORY_UTF8_DECODE = "utf8_decode_error"
CATEGORY_SCHEMA_MISSING_FIELD = "schema_missing_field"
CATEGORY_ACTION_REJECTED = "action_rejected"
CATEGORY_FALLBACK_SENT = "fallback_sent"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_RATE_LIMITED = "rate_limited_429"
CATEGORY_CONFLICT_409 = "conflict_409"
CATEGORY_UNKNOWN_KIND = "unknown_kind"
CATEGORY_UNCLASSIFIED_ERROR = "unclassified_error"

UNKNOWN_LEGACY = ds.UNKNOWN_LEGACY  # "unknown_legacy"：缺 policy_version 的历史记录

# build_hash 全链路返修：旧日志（B3 生产接入之前写入的 JSONL）没有
# build_hash 字段，导入时不得伪造一个假哈希值（伪造的哈希会被误当成
# "这条记录确实来自某个可复现的代码版本"，与真实 build_hash 无法区分，
# 破坏"build_hash 用于区分策略变化 vs 同一份代码正常波动"这一用途）。
# 用显式哨兵字符串标记"这条记录来自不提供 build_hash 的旧版本"——与
# UNKNOWN_LEGACY 处理 policy_version 缺失的方式保持同一口径。
BUILD_HASH_UNAVAILABLE = "unavailable_unknown_legacy"


@dataclass
class GameRow:
    game_id: str
    session_id: Optional[str] = None
    room_id: Optional[str] = None
    policy_version: Optional[str] = None
    config_hash: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    completeness: str = "unknown"


@dataclass
class RoundResultRow:
    game_id: str
    round_no: Optional[int]
    winner_seat: Optional[int]
    fan: Optional[int]
    detail: Optional[list]
    scores: Optional[list]
    next_dealer: Optional[int]
    source: str  # 'server_truth' | 'local_estimate'


@dataclass
class DecisionRow:
    decision_id: str
    decision_id_synthesized: bool
    game_id: Optional[str]
    round_no: Optional[int]
    seat: Optional[int]
    state_hash: Optional[str]
    policy_version: Optional[str]
    schema_version: Optional[str]
    config_hash: Optional[str]
    action: Optional[str]
    tile: Optional[str]
    time: Optional[str]
    client_prepare_ms: Optional[float] = None
    action_request_ms: Optional[float] = None
    build_hash: Optional[str] = None


@dataclass
class DecisionAttemptRow:
    """P0-1：对应 ``kind == "decision_attempt"`` 记录——调用 api.action()
    之前写入的\"尝试意图\"快照，outcome 固定为 'pending'（真正的结果由
    ``AttemptOutcomeRow`` / ``kind == "decision_attempt_outcome"`` 记录用
    同一个 decision_id 更新）。"""
    decision_id: str
    game_id: Optional[str]
    round_no: Optional[int]
    seat: Optional[int]
    state_hash: Optional[str]
    action: Optional[str]
    tile: Optional[str]
    policy_version: Optional[str]
    schema_version: Optional[str]
    config_hash: Optional[str]
    build_hash: Optional[str]
    attempted_at: Optional[str]


@dataclass
class AttemptOutcomeRow:
    """P0-1：对应 ``kind == "decision_attempt_outcome"`` 记录——
    success/timeout/非409 ApiError/409/fallback/hu 各分支产生的最终结果，
    用与对应 ``decision_attempt`` 相同的 decision_id 关联。"""
    decision_id: str
    outcome: str
    outcome_detail: Optional[str]
    resolved_at: Optional[str]


@dataclass
class ErrorRow:
    line_no: Optional[int]
    category: str
    summary: str
    game_id: Optional[str] = None
    decision_id: Optional[str] = None
    time: Optional[str] = None


@dataclass
class LineOutcome:
    """单行翻译结果：生成器逐行 yield 本对象，调用方立即处理/写库，不累积。"""
    line_no: int
    ok: bool
    game: Optional[GameRow] = None
    round_result: Optional[RoundResultRow] = None
    decision: Optional[DecisionRow] = None
    decision_attempt: Optional[DecisionAttemptRow] = None
    attempt_outcome: Optional[AttemptOutcomeRow] = None
    error: Optional[ErrorRow] = None


@dataclass
class ImportStats:
    total_lines: int = 0
    success_lines: int = 0
    failed_lines: int = 0
    games_written: int = 0
    round_results_written: int = 0
    decisions_written: int = 0
    errors_written: int = 0
    decision_attempts_written: int = 0
    attempt_outcomes_written: int = 0

    def as_dict(self) -> dict:
        return {
            "total_lines": self.total_lines,
            "success_lines": self.success_lines,
            "failed_lines": self.failed_lines,
            "games_written": self.games_written,
            "round_results_written": self.round_results_written,
            "decisions_written": self.decisions_written,
            "errors_written": self.errors_written,
            "decision_attempts_written": self.decision_attempts_written,
            "attempt_outcomes_written": self.attempt_outcomes_written,
        }


def open_text_lines(path: str) -> Iterator[str]:
    """按行迭代 .jsonl / .jsonl.gz，UTF-8 解码失败的字节序列用
    errors='replace' 保留可解析边界，但会在上层被记为 utf8_decode_error
    （通过检测 replace 字符 U+FFFD 出现）。"""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line


def _translate_decision_log_record(record: dict, line_no: int,
                                    source_sha256: str) -> LineOutcome:
    """把已经 json.loads 过的单条 DecisionLog 记录翻译成 LineOutcome。
    纯函数：只依赖传入的单条记录，不访问任何跨行状态，天然 O(1) 内存。"""
    kind = record.get("kind")
    payload = record.get("payload") or {}
    time_value = record.get("time")
    game_id = payload.get("game_id")

    if kind == "decision":
        decision_id = payload.get("decision_id")
        synthesized = False
        if not decision_id:
            decision_id = ds.derive_decision_id(
                source_sha256, line_no, kind, game_id=game_id,
                round_no=payload.get("round_no"), seat=payload.get("seat"),
                time_value=time_value,
            )
            synthesized = True
        decision = payload.get("decision") or {}
        policy_version = payload.get("policy_version") or UNKNOWN_LEGACY
        config_hash = payload.get("config_hash") or "unknown"
        schema_version = payload.get("schema_version") or "unknown"
        drow = DecisionRow(
            decision_id=decision_id,
            decision_id_synthesized=synthesized,
            game_id=game_id,
            round_no=payload.get("round_no"),
            seat=payload.get("seat"),
            state_hash=payload.get("state_hash"),
            policy_version=policy_version,
            schema_version=schema_version,
            config_hash=config_hash,
            action=decision.get("action") if isinstance(decision, dict) else None,
            tile=decision.get("tile") if isinstance(decision, dict) else None,
            time=time_value,
            client_prepare_ms=payload.get("client_prepare_ms"),
            action_request_ms=payload.get("action_request_ms"),
            build_hash=payload.get("build_hash") or BUILD_HASH_UNAVAILABLE,
        )
        grow = None
        if game_id:
            grow = GameRow(game_id=game_id, started_at=time_value, ended_at=time_value,
                            policy_version=None if policy_version == UNKNOWN_LEGACY else policy_version,
                            config_hash=None if config_hash == "unknown" else config_hash)
        return LineOutcome(line_no=line_no, ok=True, game=grow, decision=drow)

    if kind == "decision_attempt":
        # P0-1：调用 api.action() 之前写入的"尝试意图"快照。decision_id
        # 必须由调用方（mj/bot.py）显式提供——这里不做派生兜底，缺
        # decision_id 视为schema缺陷（走异常路径记一条 schema_missing_field
        # 错误），因为 attempt/outcome 关联链完全依赖同一个 decision_id，
        # 派生 ID 无法在 outcome 记录里被同样地重新计算出来。
        decision_id = payload.get("decision_id")
        if not decision_id:
            raise KeyError("decision_attempt missing decision_id")
        arow = DecisionAttemptRow(
            decision_id=decision_id,
            game_id=game_id,
            round_no=payload.get("round_no"),
            seat=payload.get("seat"),
            state_hash=payload.get("state_hash"),
            action=payload.get("action"),
            tile=payload.get("tile"),
            policy_version=payload.get("policy_version"),
            schema_version=payload.get("schema_version"),
            config_hash=payload.get("config_hash"),
            build_hash=payload.get("build_hash") or BUILD_HASH_UNAVAILABLE,
            attempted_at=time_value,
        )
        return LineOutcome(line_no=line_no, ok=True, decision_attempt=arow)

    if kind == "decision_attempt_outcome":
        # P0-1：某次 attempt 的最终结果（success/timeout/api_error/
        # conflict_409/fallback_sent/hu），用同一个 decision_id 关联。
        decision_id = payload.get("decision_id")
        if not decision_id:
            raise KeyError("decision_attempt_outcome missing decision_id")
        orow = AttemptOutcomeRow(
            decision_id=decision_id,
            outcome=payload.get("outcome") or "unknown",
            outcome_detail=sanitize_text(payload.get("outcome_detail"), limit=200)
            if payload.get("outcome_detail") else None,
            resolved_at=time_value,
        )
        return LineOutcome(line_no=line_no, ok=True, attempt_outcome=orow)

    if kind == "hu_detail":
        rrow = RoundResultRow(
            game_id=game_id, round_no=payload.get("round_no"), winner_seat=payload.get("seat"),
            fan=payload.get("fan"), detail=payload.get("detail"), scores=None, next_dealer=None,
            source="local_estimate",
        )
        grow = GameRow(game_id=game_id, started_at=time_value, ended_at=time_value) if game_id else None
        return LineOutcome(line_no=line_no, ok=True, game=grow, round_result=rrow)

    if kind == "round_end":
        # 历史遗留字段（mj/logging.py:DecisionLog.round_end）；当前生产路径未调用，
        # 但兼容解析：result 结构未知，尽力提取 winner/fan 字段，缺失则跳过。
        inner = payload.get("result") or {}
        if isinstance(inner, dict) and ("fan" in inner or "winner" in inner):
            rrow = RoundResultRow(
                game_id=game_id, round_no=inner.get("round_no"), winner_seat=inner.get("winner"),
                fan=inner.get("fan"), detail=inner.get("detail"), scores=inner.get("scores"),
                next_dealer=inner.get("next_dealer"), source="local_estimate",
            )
            return LineOutcome(line_no=line_no, ok=True, round_result=rrow)
        return LineOutcome(line_no=line_no, ok=True)

    if kind == "action_rejected":
        summary = sanitize_text(json.dumps({
            "phase": payload.get("phase"), "error": payload.get("error"),
            "rejections": payload.get("rejections"),
        }, ensure_ascii=False), limit=160)
        erow = ErrorRow(line_no=line_no, category=CATEGORY_ACTION_REJECTED, summary=summary,
                         game_id=game_id, time=time_value)
        return LineOutcome(line_no=line_no, ok=True, error=erow)

    if kind == "fallback_sent":
        summary = sanitize_text(json.dumps({
            "tile": payload.get("tile"), "accepted": payload.get("accepted"),
            "error": payload.get("error"),
        }, ensure_ascii=False), limit=160)
        erow = ErrorRow(line_no=line_no, category=CATEGORY_FALLBACK_SENT, summary=summary,
                         game_id=game_id, time=time_value)
        return LineOutcome(line_no=line_no, ok=True, error=erow)

    if kind == "error":
        error_text = str(payload.get("error") or "")
        lowered = error_text.lower()
        if "429" in error_text:
            category = CATEGORY_RATE_LIMITED
        elif "409" in error_text:
            category = CATEGORY_CONFLICT_409
        elif "timeout" in lowered or "timed out" in lowered:
            category = CATEGORY_TIMEOUT
        else:
            # A4 返修：不再默认归为 timeout——无法从文本判断具体类别时，
            # 归入显式的"未分类错误"类别，保留原始（脱敏后）文本供人工核查。
            category = CATEGORY_UNCLASSIFIED_ERROR
        erow = ErrorRow(line_no=line_no, category=category,
                         summary=sanitize_text(error_text, limit=160),
                         game_id=game_id, time=time_value)
        return LineOutcome(line_no=line_no, ok=True, error=erow)

    # 'state'/'notify'/'piao_attempt'/'result'/'state_request_metric'/
    # 'state_dedup_summary'/'state_sample'/'anomaly_window'/'pass_window' 等
    # 其它 kind：本轮不建模专门的表（对应减量策略"正常轮询快照低频采样"——
    # 这些高频、低信息密度的记录本轮只统计条数，不逐条导入决策/结果表，
    # 避免数据库膨胀）。state_request_metric 是 P1 新增的紧凑度量（见
    # mj/bot.py::_play_game_loop / mj/timing.py），同样归入此白名单，不
    # 触发 CATEGORY_UNKNOWN_KIND 误报。
    if kind not in ("state", "notify", "piao_attempt", "result", "state_request_metric",
                    "state_dedup_summary", "state_sample", "anomaly_window", "pass_window"):
        erow = ErrorRow(line_no=line_no, category=CATEGORY_UNKNOWN_KIND,
                         summary=sanitize_text(f"unrecognized kind={kind!r}", limit=160),
                         game_id=game_id, time=time_value)
        return LineOutcome(line_no=line_no, ok=True, error=erow)
    return LineOutcome(line_no=line_no, ok=True)


def iter_decision_log_lines(lines: Iterator[str], source_sha256: str = "") -> Iterator[LineOutcome]:
    """逐行流式翻译 DecisionLog JSONL。生成器：每次只在内存里保留当前一行
    的翻译结果，绝不累积整份文件。单行 JSON/UTF-8/schema 失败只记为该行的
    ``LineOutcome(ok=False)``，不抛异常中止迭代，调用方的 for 循环会自然
    继续处理下一行。"""
    for line_no, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            yield LineOutcome(line_no=line_no, ok=True)
            continue
        if "\ufffd" in stripped:
            yield LineOutcome(
                line_no=line_no, ok=False,
                error=ErrorRow(line_no=line_no, category=CATEGORY_UTF8_DECODE,
                                summary=sanitize_text(stripped, limit=160)),
            )
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            yield LineOutcome(
                line_no=line_no, ok=False,
                error=ErrorRow(line_no=line_no, category=CATEGORY_JSON_DECODE,
                                summary=sanitize_text(f"{exc.__class__.__name__}: {exc}", limit=160)),
            )
            continue
        try:
            yield _translate_decision_log_record(record, line_no, source_sha256)
        except Exception as exc:  # noqa: BLE001 - 任意 schema 缺陷都不应中止整份文件
            yield LineOutcome(
                line_no=line_no, ok=False,
                error=ErrorRow(line_no=line_no, category=CATEGORY_SCHEMA_MISSING_FIELD,
                                summary=sanitize_text(f"{exc.__class__.__name__}: {exc}", limit=160)),
            )


def import_decision_log_stream(conn, lines: Iterator[str], *, source_file_id: int,
                                source_sha256: str, batch_size: int = 2000,
                                on_batch=None) -> ImportStats:
    """生产导入入口：逐行消费 ``iter_decision_log_lines``，**立即写入**
    SQLite（不缓冲整份文件），每 ``batch_size`` 行提交一次事务。

    内存占用证明：本函数体内没有任何随行数增长的列表/字典——``stats``是
    固定大小的计数器对象，每次循环迭代只持有当前这一行的 ``LineOutcome``。
    """
    stats = ImportStats()
    since_commit = 0
    for outcome in iter_decision_log_lines(lines, source_sha256=source_sha256):
        stats.total_lines += 1
        if outcome.ok:
            stats.success_lines += 1
        else:
            stats.failed_lines += 1
        if outcome.game is not None:
            ds.upsert_game(conn, game_id=outcome.game.game_id, session_id=outcome.game.session_id,
                            room_id=outcome.game.room_id, policy_version=outcome.game.policy_version,
                            config_hash=outcome.game.config_hash, started_at=outcome.game.started_at,
                            ended_at=outcome.game.ended_at, completeness=outcome.game.completeness)
            stats.games_written += 1
        if outcome.round_result is not None:
            rr = outcome.round_result
            ds.upsert_round_result(conn, game_id=rr.game_id, round_no=rr.round_no,
                                    winner_seat=rr.winner_seat, fan=rr.fan, detail=rr.detail,
                                    scores=rr.scores, next_dealer=rr.next_dealer, source=rr.source,
                                    source_file_id=source_file_id)
            stats.round_results_written += 1
        if outcome.decision is not None:
            d = outcome.decision
            ds.upsert_decision(conn, decision_id=d.decision_id,
                                decision_id_synthesized=d.decision_id_synthesized,
                                game_id=d.game_id, round_no=d.round_no, seat=d.seat,
                                state_hash=d.state_hash, policy_version=d.policy_version,
                                schema_version=d.schema_version, config_hash=d.config_hash,
                                action=d.action, tile=d.tile, time_value=d.time,
                                client_prepare_ms=d.client_prepare_ms,
                                action_request_ms=d.action_request_ms,
                                source_file_id=source_file_id, build_hash=d.build_hash)
            stats.decisions_written += 1
        if outcome.decision_attempt is not None:
            a = outcome.decision_attempt
            ds.upsert_decision_attempt(
                conn, decision_id=a.decision_id, game_id=a.game_id, round_no=a.round_no,
                seat=a.seat, state_hash=a.state_hash, action=a.action, tile=a.tile,
                policy_version=a.policy_version, schema_version=a.schema_version,
                config_hash=a.config_hash, build_hash=a.build_hash,
                attempted_at=a.attempted_at, source_file_id=source_file_id,
            )
            stats.decision_attempts_written += 1
        if outcome.attempt_outcome is not None:
            o = outcome.attempt_outcome
            ds.update_decision_attempt_outcome(
                conn, decision_id=o.decision_id, outcome=o.outcome,
                outcome_detail=o.outcome_detail, resolved_at=o.resolved_at,
            )
            stats.attempt_outcomes_written += 1
        if outcome.error is not None:
            e = outcome.error
            ds.upsert_error(conn, source_file_id=source_file_id, line_no=e.line_no,
                             category=e.category, game_id=e.game_id, decision_id=e.decision_id,
                             summary=e.summary, time_value=e.time)
            stats.errors_written += 1
        since_commit += 1
        if since_commit >= batch_size:
            conn.commit()
            since_commit = 0
            if on_batch:
                on_batch(stats)
    conn.commit()
    if on_batch:
        on_batch(stats)
    return stats


def parse_portal_events_json(data: dict):
    """把单个门户事件流游戏对象翻译成 (game, round_results, errors) 三元组，
    与 ``mj/reconcile.py:server_truths_from_portal_game`` 使用同一套
    round_no 推断口径。门户事件流文件本身是单个 JSON 对象（不是 JSONL），
    只能整体 ``json.load`` 后传入本函数——这是已知且文档化的边界（见
    `data/README.md`"已知限制"），不在本轮流式化范围内（这类文件历史上
    体量远小于决策日志）。"""
    game_id = data.get("game_id")
    if not game_id:
        return None, [], [ErrorRow(
            line_no=None, category=CATEGORY_SCHEMA_MISSING_FIELD,
            summary="portal event object missing game_id",
        )]

    game = GameRow(game_id=game_id, room_id=None)
    round_results = []
    current = 1
    for block in data.get("blocks", []):
        for event in block.get("events") or []:
            if event.get("type") != "round_ended":
                continue
            payload = event.get("data") or {}
            winner = event.get("seat")
            round_results.append(RoundResultRow(
                game_id=game_id, round_no=current, winner_seat=winner,
                fan=payload.get("fan"), detail=payload.get("detail"),
                scores=payload.get("scores"), next_dealer=payload.get("next_dealer"),
                source="server_truth",
            ))
            current = (payload.get("round_no") or current) + 1
    return game, round_results, []


def import_portal_events_stream(conn, data: dict, *, source_file_id: int,
                                 room_id: Optional[str] = None) -> ImportStats:
    """把单个门户事件流游戏对象写入 SQLite。同样直接写入，不构造中间的
    全量列表结构对外暴露（内部 ``parse_portal_events_json`` 返回值本身
    体量受限于"单局的轮次数"，量级上不构成内存风险，这是文档化的既有假设）。"""
    stats = ImportStats()
    stats.total_lines = 1
    game, round_results, errors = parse_portal_events_json(data)
    if game is None:
        stats.failed_lines = 1
        for e in errors:
            ds.upsert_error(conn, source_file_id=source_file_id, line_no=e.line_no,
                             category=e.category, game_id=e.game_id, summary=e.summary)
            stats.errors_written += 1
        conn.commit()
        return stats
    stats.success_lines = 1
    ds.upsert_game(conn, game_id=game.game_id, room_id=room_id)
    stats.games_written += 1
    for rr in round_results:
        ds.upsert_round_result(conn, game_id=rr.game_id, round_no=rr.round_no,
                                winner_seat=rr.winner_seat, fan=rr.fan, detail=rr.detail,
                                scores=rr.scores, next_dealer=rr.next_dealer, source=rr.source,
                                source_file_id=source_file_id)
        stats.round_results_written += 1
    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# 测试专用小规模辅助函数（非生产导入路径）
# ---------------------------------------------------------------------------
# 下面这个函数把 ``iter_decision_log_lines`` 的生成器结果收集进列表，
# 仅用于单元测试里对"少量几行输入"做整体断言（如"这份 3 行的合成输入应该
# 产生 2 条 decisions"）——测试规模上不存在内存风险。生产导入命令
# （tools/data_tool.py）一律使用 ``import_decision_log_stream``，不调用
# 本函数，不会把整份大文件的结果收集进列表。
@dataclass
class CollectedLines:
    total_lines: int = 0
    success_lines: int = 0
    failed_lines: int = 0
    games: list = None
    round_results: list = None
    decisions: list = None
    errors: list = None

    def __post_init__(self):
        self.games = self.games or []
        self.round_results = self.round_results or []
        self.decisions = self.decisions or []
        self.errors = self.errors or []


def collect_decision_log_lines_for_testing(lines: Iterator[str], source_sha256: str = "") -> CollectedLines:
    """仅供测试使用：见上方模块级说明。"""
    out = CollectedLines()
    for outcome in iter_decision_log_lines(lines, source_sha256=source_sha256):
        out.total_lines += 1
        if outcome.ok:
            out.success_lines += 1
        else:
            out.failed_lines += 1
        if outcome.game is not None:
            out.games.append(outcome.game)
        if outcome.round_result is not None:
            out.round_results.append(outcome.round_result)
        if outcome.decision is not None:
            out.decisions.append(outcome.decision)
        if outcome.error is not None:
            out.errors.append(outcome.error)
    return out


# 向后兼容别名：阶段A返修前的单测大量使用 ``parse_decision_log_lines(lines)``
# 对"少量几行的合成输入"做整体断言。这类测试规模小（几行到几十行），不构成
# 内存风险，允许继续用整体收集的方式断言。生产导入命令
# （tools/data_tool.py）一律使用 ``import_decision_log_stream``，不经过
# 本别名，因此"真正的生产路径"仍然是有界内存流式写入。
parse_decision_log_lines = collect_decision_log_lines_for_testing
