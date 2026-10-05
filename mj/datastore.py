"""SQLite 数据层：schema 定义与幂等 upsert 辅助函数。

设计依据：`/Users/yuanye/Documents/ChatGPT/麻将大赛/数据保留与日志优化方案.md`
（"查询层：清洗后写入 SQLite……供脚本按字段检索和聚合"）与
`SONNET5_DATA_REVIEW.md` 返修要求（source_files 必须能区分
importing/complete/failed；decision_id 派生需加入 source SHA/line number/
kind 避免碰撞；round_results 的 nullable winner_seat 需要显式规范化去重
语义）。

只用标准库 `sqlite3`。不引入 pandas/Parquet/向量数据库。

**Schema v2 变更（相对 v1）**：
- `source_files` 新增 `status` 列（'importing'|'complete'|'failed'），
  导入开始时置为 'importing'，全部批次成功写完后置为 'complete'，写入过程
  中抛出未处理异常则置为 'failed'——不允许一个中途失败的文件在
  `source_files` 里表现得"看似已完整导入"。
- `round_results.winner_seat` 的 NULL（流局/无赢家）在 SQLite 的 UNIQUE
  约束语义下不会互相判等（两条 NULL 记录会被当成不同行，不会被去重），
  这是评审报告 P2 指出的确定性 bug。修复：用哨兵值 `DRAW_SEAT_SENTINEL`
  （-1）代替 NULL 存储，upsert/查询两侧统一在本模块边界做 None <-> -1
  转换，调用方（`data_import`/`reconcile`/CLI）不需要关心哨兵值本身。
- `decisions.decision_id`（派生场景）现在由
  `derive_decision_id(source_sha256, line_no, kind, ...)` 生成，天然
  绑定"来源文件内容 + 行号"，不再可能因为"同一时间戳出现多次决策"而碰撞
  （旧版只用 game_id/round_no/seat/time 四元组派生，理论上可碰撞）。

**幂等语义**（对应任务书"重复导入同一文件不得制造重复记录"）：
- `source_files` 以 `sha256` 唯一：同一内容文件重复导入会被识别为
  "already imported"，直接跳过，不重新解析。
- `games`：以 `game_id` 为主键，`INSERT ... ON CONFLICT DO UPDATE`合并字段
  （新信息补全 NULL 字段，不覆盖已有非空字段）；`started_at`/`ended_at`
  分别取全部来源里的最小值/最大值（而不是"只在当前为 NULL 时才写入"——
  旧版 bug：`ended_at` 用 `COALESCE(old,new)`，导致每条 decision 第一次
  写入后 `ended_at` 就永远停在第一条决策的时间，从未被更新为更晚的时间）。
- `round_results`：以 `(game_id, round_no, winner_seat, source)` 唯一，
  `winner_seat` 用哨兵值规范化 NULL。
- `decisions`：以 `decision_id` 为主键。
- `errors`：以 `(source_file_id, line_no, category)` 唯一。
"""
import hashlib
import json
import sqlite3
import time
from typing import Any, Optional

SCHEMA_VERSION = 3

# 流局/无赢家的 winner_seat 哨兵值。SQLite 的 UNIQUE 约束对 NULL 不做
# 相等性判断（每条 NULL 都被当成不同值），导致多条"无赢家"记录不会被
# 去重。用越界哨兵值代替 NULL 存储，在本模块的 upsert/查询边界做
# None <-> 哨兵值的转换，保持对外接口仍然是"没有赢家就是 None"。
DRAW_SEAT_SENTINEL = -1

UNKNOWN_LEGACY = "unknown_legacy"

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    mtime REAL,
    format TEXT NOT NULL,              -- 'decision_jsonl' | 'decision_jsonl_gz' | 'portal_events_json'
    source_type TEXT NOT NULL,         -- 'local_decision_log' | 'portal_event_stream'
    status TEXT NOT NULL DEFAULT 'importing',  -- 'importing' | 'complete' | 'failed'
    imported_at TEXT NOT NULL,
    finished_at TEXT,
    total_lines INTEGER NOT NULL DEFAULT 0,
    success_lines INTEGER NOT NULL DEFAULT 0,
    failed_lines INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_files_path ON source_files(path);
CREATE INDEX IF NOT EXISTS idx_source_files_status ON source_files(status);

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    session_id TEXT,
    room_id TEXT,
    policy_version TEXT,
    config_hash TEXT,
    started_at TEXT,
    ended_at TEXT,
    completeness TEXT NOT NULL DEFAULT 'unknown'  -- 'unknown' | 'partial' | 'complete'
);
CREATE INDEX IF NOT EXISTS idx_games_room ON games(room_id);
CREATE INDEX IF NOT EXISTS idx_games_policy ON games(policy_version);

CREATE TABLE IF NOT EXISTS round_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    round_no INTEGER,
    winner_seat INTEGER NOT NULL DEFAULT -1,  -- -1 = 无赢家/流局哨兵值，见 DRAW_SEAT_SENTINEL
    fan INTEGER,
    detail TEXT,                       -- JSON 数组文本
    scores TEXT,                       -- JSON 四家分数数组文本
    next_dealer INTEGER,
    source TEXT NOT NULL,              -- 'server_truth' | 'local_estimate'
    source_file_id INTEGER,
    UNIQUE(game_id, round_no, winner_seat, source),
    FOREIGN KEY(source_file_id) REFERENCES source_files(id)
);
CREATE INDEX IF NOT EXISTS idx_round_results_game ON round_results(game_id, round_no);
CREATE INDEX IF NOT EXISTS idx_round_results_source ON round_results(source);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    decision_id_synthesized INTEGER NOT NULL DEFAULT 0,
    game_id TEXT,
    round_no INTEGER,
    seat INTEGER,
    state_hash TEXT,
    policy_version TEXT,
    schema_version TEXT,
    config_hash TEXT,
    build_hash TEXT,
    action TEXT,
    tile TEXT,
    time TEXT,
    client_prepare_ms REAL,
    action_request_ms REAL,
    source_file_id INTEGER,
    FOREIGN KEY(source_file_id) REFERENCES source_files(id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_game ON decisions(game_id, round_no);
CREATE INDEX IF NOT EXISTS idx_decisions_state_hash ON decisions(state_hash);
CREATE INDEX IF NOT EXISTS idx_decisions_policy ON decisions(policy_version);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(time);

-- P0-1 返修：decision_attempts 记录"调用 api.action() 之前"的完整尝试意图
-- （decision_id/game_id/state_hash/action/tile/policy_version/
-- schema_version/config_hash/build_hash），并在动作结果明朗后（成功/
-- TimeoutError/非409 ApiError/409/fallback/hu）用同一个 decision_id 更新
-- outcome 字段——保证即使 API 调用超时（连响应都没收到），"曾经尝试过
-- 什么动作"这件事本身也已经落盘，可以在事后从日志/SQLite 精确还原。
CREATE TABLE IF NOT EXISTS decision_attempts (
    decision_id TEXT PRIMARY KEY,
    game_id TEXT,
    round_no INTEGER,
    seat INTEGER,
    state_hash TEXT,
    action TEXT,
    tile TEXT,
    policy_version TEXT,
    schema_version TEXT,
    config_hash TEXT,
    build_hash TEXT,
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'pending',  -- 'pending'|'success'|'timeout'|'api_error'|'conflict_409'|'fallback_sent'|'hu'
    outcome_detail TEXT,
    resolved_at TEXT,
    source_file_id INTEGER,
    FOREIGN KEY(source_file_id) REFERENCES source_files(id)
);
CREATE INDEX IF NOT EXISTS idx_decision_attempts_game ON decision_attempts(game_id, round_no);
CREATE INDEX IF NOT EXISTS idx_decision_attempts_outcome ON decision_attempts(outcome);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER,
    line_no INTEGER,
    category TEXT NOT NULL,            -- 见 data_import.ErrorCategory
    game_id TEXT,
    decision_id TEXT,
    summary TEXT NOT NULL,             -- 经 mj.sanitize 处理后的脱敏摘要，不含原始 payload
    time TEXT,
    UNIQUE(source_file_id, line_no, category),
    FOREIGN KEY(source_file_id) REFERENCES source_files(id)
);
CREATE INDEX IF NOT EXISTS idx_errors_kind_time ON errors(category, time);
CREATE INDEX IF NOT EXISTS idx_errors_game ON errors(game_id);

CREATE TABLE IF NOT EXISTS aggregates (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,               -- JSON 文本
    computed_at TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, index: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# 结构校验清单：即使 schema_meta 已经声明 SCHEMA_VERSION（可能是"伪 v3"——
# 早期某次运行把 schema_meta 写成了 3，但实际 DDL 没有真正跑完，decisions
# 缺 build_hash），也必须逐项核实这些索引确实存在，缺失就补齐。
_REQUIRED_DECISION_ATTEMPTS_INDEXES = (
    ("idx_decision_attempts_game", "decision_attempts(game_id, round_no)"),
    ("idx_decision_attempts_outcome", "decision_attempts(outcome)"),
)


def _verify_and_repair_schema(conn: sqlite3.Connection) -> None:
    """P0 返修：不能只信任 ``schema_meta`` 里记录的版本号——"伪 v3"场景下
    ``schema_meta.schema_version`` 已经被写成 3，但 ``decisions`` 表实际
    仍缺 ``build_hash`` 列（例如某次运行中途失败/被打断，只写了元数据没
    真正执行 DDL）。旧版 ``init_schema()`` 只在 ``before_version <
    SCHEMA_VERSION`` 时才跑迁移，会直接跳过这种"版本号对但结构不对"的
    情况，导致后续写入 ``decisions.build_hash`` 时抛
    ``OperationalError: table decisions has no column named build_hash``。

    改为：每次 ``init_schema()`` 调用都无条件做幂等结构校验（不看版本号），
    用 ``PRAGMA table_info``/``sqlite_master`` 逐项核实 decisions/
    decision_attempts 的必需列与索引是否真的存在，缺失才补齐；全部已存在
    时本函数是纯只读检查，不执行任何 ALTER/CREATE，不触碰已有业务数据。
    """
    if _table_exists(conn, "decisions"):
        columns = _column_names(conn, "decisions")
        if "build_hash" not in columns:
            conn.execute("ALTER TABLE decisions ADD COLUMN build_hash TEXT")
        for index_name, index_expr in (
            ("idx_decisions_game", "decisions(game_id, round_no)"),
            ("idx_decisions_state_hash", "decisions(state_hash)"),
            ("idx_decisions_policy", "decisions(policy_version)"),
            ("idx_decisions_time", "decisions(time)"),
        ):
            if not _index_exists(conn, index_name):
                conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {index_expr}")

    if not _table_exists(conn, "decision_attempts"):
        conn.executescript("""
CREATE TABLE IF NOT EXISTS decision_attempts (
    decision_id TEXT PRIMARY KEY,
    game_id TEXT,
    round_no INTEGER,
    seat INTEGER,
    state_hash TEXT,
    action TEXT,
    tile TEXT,
    policy_version TEXT,
    schema_version TEXT,
    config_hash TEXT,
    build_hash TEXT,
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'pending',
    outcome_detail TEXT,
    resolved_at TEXT,
    source_file_id INTEGER,
    FOREIGN KEY(source_file_id) REFERENCES source_files(id)
);
""")
    else:
        columns = _column_names(conn, "decision_attempts")
        if "build_hash" not in columns:
            conn.execute("ALTER TABLE decision_attempts ADD COLUMN build_hash TEXT")
    for index_name, index_expr in _REQUIRED_DECISION_ATTEMPTS_INDEXES:
        if not _index_exists(conn, index_name):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {index_expr}")


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等 schema 初始化 + 结构校验/修复。

    - 全新数据库：``CREATE TABLE IF NOT EXISTS`` 一次性建出全部 v3 表。
    - 既有数据库（无论 ``schema_meta`` 声明什么版本号）：每次都无条件执行
      ``_verify_and_repair_schema()`` 逐项核实必需列/表/索引是否真的存在，
      缺失才补齐——不再只在 ``before_version < SCHEMA_VERSION`` 时才检查
      （修复"伪 v3"：schema_meta 已经是 3 但结构实际不完整的场景）。全部
      已有业务数据（decisions/games/round_results/errors/...）原样保留，
      不做任何删除/重建/覆盖。
    - 只有结构校验/修复真正跑完（未抛异常）才把 ``schema_meta.schema_version``
      写入/保留为当前 ``SCHEMA_VERSION``；校验中途失败会向上抛出异常，不
      会误报"已经是最新版"。
    - 重复调用安全：第二次调用时全部列/表/索引已存在，``_verify_and_repair_schema``
      内部的存在性检查会跳过对应的 ALTER/CREATE，不会报错。
    """
    conn.executescript(DDL)
    _verify_and_repair_schema(conn)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def sha256_file(path: str, chunk_size: int = 1 << 20) -> str:
    """流式计算文件 SHA-256，不整份读入内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def derive_decision_id(source_sha256, line_no, kind, game_id=None, round_no=None,
                        seat=None, time_value=None) -> str:
    """旧日志缺 decision_id 时的确定性派生 ID。

    绑定"来源文件内容(sha256) + 行号 + kind"作为主要唯一性来源——同一份
    文件内容的同一行天然唯一，因此不可能碰撞（旧版只用 game_id/round_no/
    seat/time 四元组派生，理论上"同一时刻同一局同一座位记录两次决策"会
    产生碰撞，被评审报告标记为 P2）。game_id/round_no/seat/time 仍纳入
    哈希输入，便于调试时按内容反推，但不是唯一性的保证来源。
    """
    raw = (f"{source_sha256}|{line_no}|{kind}|{game_id}|{round_no}|{seat}|"
           f"{time_value}").encode("utf-8")
    return "derived-" + hashlib.sha256(raw).hexdigest()[:32]


def find_source_file_by_sha256(conn: sqlite3.Connection, sha256: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM source_files WHERE sha256 = ?", (sha256,))
    return cur.fetchone()


def insert_source_file(conn: sqlite3.Connection, *, path, sha256, size_bytes, mtime,
                        fmt, source_type) -> int:
    """插入一条 source_files 记录，初始状态为 'importing'。调用方必须在
    导入完成后调用 `mark_source_file_complete` 或 `mark_source_file_failed`
    ——不允许让记录停留在 'importing' 状态却被下游当成"已完整导入"。"""
    cur = conn.execute(
        "INSERT INTO source_files(path, sha256, size_bytes, mtime, format, source_type, "
        "status, imported_at, total_lines, success_lines, failed_lines) "
        "VALUES (?, ?, ?, ?, ?, ?, 'importing', ?, 0, 0, 0)",
        (path, sha256, size_bytes, mtime, fmt, source_type, _utc_now()),
    )
    return cur.lastrowid


def mark_source_file_complete(conn: sqlite3.Connection, source_file_id: int,
                               total: int, success: int, failed: int) -> None:
    conn.execute(
        "UPDATE source_files SET total_lines=?, success_lines=?, failed_lines=?, "
        "status='complete', finished_at=? WHERE id=?",
        (total, success, failed, _utc_now(), source_file_id),
    )


def mark_source_file_failed(conn: sqlite3.Connection, source_file_id: int,
                             total: int, success: int, failed: int,
                             error_message: str) -> None:
    """文件导入过程中抛出未处理异常时调用：明确标记为 'failed'，保留已经
    处理到的行数统计（不清零），但绝不允许状态呈现为 'complete'。"""
    conn.execute(
        "UPDATE source_files SET total_lines=?, success_lines=?, failed_lines=?, "
        "status='failed', finished_at=?, error_message=? WHERE id=?",
        (total, success, failed, _utc_now(), error_message[:500], source_file_id),
    )


# 向后兼容别名（阶段A返修前的旧名字），内部统一改用 mark_source_file_complete。
def finalize_source_file_counts(conn: sqlite3.Connection, source_file_id: int,
                                 total: int, success: int, failed: int) -> None:
    mark_source_file_complete(conn, source_file_id, total, success, failed)


def delete_data_for_source_file(conn: sqlite3.Connection, source_file_id: int) -> dict:
    """P0-4 返修：清理某个 source_file_id 产生的全部下游数据（decisions/
    round_results/errors/decision_attempts），用于"status=importing 或
    failed 的文件安全重试"场景——重新导入前必须先把上一次中途失败/中断
    遗留的部分数据删干净，不能让重试产生的新数据与旧的部分数据混杂，
    形成"半导入"状态（既不是 0 条也不是完整一次导入的行数）。

    返回各表实际删除的行数，供调用方在返回值/日志里说明"清理了多少条
    残留数据"。按外键依赖顺序删除（虽然当前 schema 没有级联删除）。
    """
    deleted = {}
    for table in ("decisions", "round_results", "errors", "decision_attempts"):
        cur = conn.execute(f"DELETE FROM {table} WHERE source_file_id = ?", (source_file_id,))
        deleted[table] = cur.rowcount
    return deleted


def reset_source_file_for_retry(conn: sqlite3.Connection, source_file_id: int) -> None:
    """把一条 source_files 记录重置回 'importing' 状态，清空计数与
    finished_at/error_message，供安全重试使用。必须与
    ``delete_data_for_source_file`` 配合调用（先清理下游数据，再重置状态），
    否则会出现"状态显示重新开始导入，但下游表仍残留上一次的部分数据"的
    不一致。"""
    conn.execute(
        "UPDATE source_files SET status='importing', total_lines=0, success_lines=0, "
        "failed_lines=0, finished_at=NULL, error_message=NULL, imported_at=? WHERE id=?",
        (_utc_now(), source_file_id),
    )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm_winner_seat(winner_seat):
    """None（无赢家/流局）在存储层转换为哨兵值，供 UNIQUE 约束正确去重。"""
    return DRAW_SEAT_SENTINEL if winner_seat is None else winner_seat


def denorm_winner_seat(winner_seat):
    """哨兵值转换回 None，供查询结果对外展示（调用方不应直接看到 -1）。"""
    return None if winner_seat == DRAW_SEAT_SENTINEL else winner_seat


def upsert_game(conn: sqlite3.Connection, *, game_id, session_id=None, room_id=None,
                 policy_version=None, config_hash=None, started_at=None, ended_at=None,
                 completeness=None) -> None:
    """幂等 upsert：
    - session_id/room_id/policy_version/config_hash：只用新值补全当前为
      NULL 的字段，不覆盖已有非空值（防止后导入的残缺来源冲掉先前更完整
      的记录）；
    - started_at：取所有来源里的最小值；ended_at：取所有来源里的最大值
      （修复旧 bug：旧版 `ended_at` 用 COALESCE(old,new)，导致其值永远
      停留在"第一次写入时的时间"，从未随后续更晚的决策更新）。
    """
    conn.execute(
        """
        INSERT INTO games(game_id, session_id, room_id, policy_version, config_hash,
                           started_at, ended_at, completeness)
        VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'unknown'))
        ON CONFLICT(game_id) DO UPDATE SET
            session_id = COALESCE(games.session_id, excluded.session_id),
            room_id = COALESCE(games.room_id, excluded.room_id),
            policy_version = COALESCE(games.policy_version, excluded.policy_version),
            config_hash = COALESCE(games.config_hash, excluded.config_hash),
            started_at = CASE
                WHEN games.started_at IS NULL THEN excluded.started_at
                WHEN excluded.started_at IS NULL THEN games.started_at
                WHEN excluded.started_at < games.started_at THEN excluded.started_at
                ELSE games.started_at END,
            ended_at = CASE
                WHEN games.ended_at IS NULL THEN excluded.ended_at
                WHEN excluded.ended_at IS NULL THEN games.ended_at
                WHEN excluded.ended_at > games.ended_at THEN excluded.ended_at
                ELSE games.ended_at END,
            completeness = CASE WHEN games.completeness = 'unknown'
                                 THEN COALESCE(excluded.completeness, games.completeness)
                                 ELSE games.completeness END
        """,
        (game_id, session_id, room_id, policy_version, config_hash, started_at, ended_at,
         completeness),
    )


def upsert_round_result(conn: sqlite3.Connection, *, game_id, round_no, winner_seat, fan,
                         detail, scores, next_dealer, source, source_file_id=None) -> None:
    conn.execute(
        """
        INSERT INTO round_results(game_id, round_no, winner_seat, fan, detail, scores,
                                   next_dealer, source, source_file_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id, round_no, winner_seat, source) DO UPDATE SET
            fan = excluded.fan,
            detail = excluded.detail,
            scores = excluded.scores,
            next_dealer = excluded.next_dealer,
            source_file_id = excluded.source_file_id
        """,
        (game_id, round_no, _norm_winner_seat(winner_seat), fan,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None,
         json.dumps(scores, ensure_ascii=False) if scores is not None else None,
         next_dealer, source, source_file_id),
    )


def upsert_decision(conn: sqlite3.Connection, *, decision_id, decision_id_synthesized, game_id,
                     round_no, seat, state_hash, policy_version, schema_version, config_hash,
                     action, tile, time_value, client_prepare_ms=None, action_request_ms=None,
                     source_file_id=None, build_hash=None) -> None:
    conn.execute(
        """
        INSERT INTO decisions(decision_id, decision_id_synthesized, game_id, round_no, seat,
                               state_hash, policy_version, schema_version, config_hash, build_hash,
                               action, tile, time, client_prepare_ms, action_request_ms, source_file_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(decision_id) DO UPDATE SET
            state_hash = excluded.state_hash,
            policy_version = excluded.policy_version,
            schema_version = excluded.schema_version,
            config_hash = excluded.config_hash,
            build_hash = excluded.build_hash,
            action = excluded.action,
            tile = excluded.tile,
            time = excluded.time,
            client_prepare_ms = excluded.client_prepare_ms,
            action_request_ms = excluded.action_request_ms,
            source_file_id = excluded.source_file_id
        """,
        (decision_id, int(decision_id_synthesized), game_id, round_no, seat, state_hash,
         policy_version, schema_version, config_hash, build_hash, action, tile, time_value,
         client_prepare_ms, action_request_ms, source_file_id),
    )


def upsert_decision_attempt(conn: sqlite3.Connection, *, decision_id, game_id=None, round_no=None,
                             seat=None, state_hash=None, action=None, tile=None,
                             policy_version=None, schema_version=None, config_hash=None,
                             build_hash=None, attempted_at, outcome="pending", outcome_detail=None,
                             resolved_at=None, source_file_id=None) -> None:
    """P0-1：在调用 ``api.action()`` 之前就必须先写入一条 attempt 记录
    （outcome='pending'）。之后无论走到哪个结果分支（success/timeout/
    非409 ApiError/409/fallback/hu），调用方都用同一个 ``decision_id``
    调用 ``update_decision_attempt_outcome`` 推进 outcome，而不是重新插入
    新行——保证"尝试了什么动作"与"这次尝试的最终结果"通过同一个
    decision_id 天然关联，即使超时（连响应都没收到）也不例外。"""
    conn.execute(
        """
        INSERT INTO decision_attempts(decision_id, game_id, round_no, seat, state_hash, action,
                                       tile, policy_version, schema_version, config_hash,
                                       build_hash, attempted_at, outcome, outcome_detail,
                                       resolved_at, source_file_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(decision_id) DO UPDATE SET
            game_id = excluded.game_id,
            round_no = excluded.round_no,
            seat = excluded.seat,
            state_hash = excluded.state_hash,
            action = excluded.action,
            tile = excluded.tile,
            policy_version = excluded.policy_version,
            schema_version = excluded.schema_version,
            config_hash = excluded.config_hash,
            build_hash = excluded.build_hash,
            attempted_at = excluded.attempted_at,
            source_file_id = excluded.source_file_id
        """,
        (decision_id, game_id, round_no, seat, state_hash, action, tile, policy_version,
         schema_version, config_hash, build_hash, attempted_at, outcome, outcome_detail,
         resolved_at, source_file_id),
    )


def update_decision_attempt_outcome(conn: sqlite3.Connection, *, decision_id, outcome,
                                     outcome_detail=None, resolved_at) -> None:
    """用同一个 decision_id 推进已存在的 attempt 记录到明确的最终结果。

    健壮性：正常路径下 attempt 行（outcome='pending'）总是先于 outcome
    行写入（``mj/bot.py`` 在调用 ``api.action()`` 之前就先记 attempt，
    拿到结果后才记 outcome）。但导入器按"单行独立处理、不假设跨行顺序"
    的原则工作（见 ``mj/data_import.py`` 模块说明），万一 attempt 行因为
    日志截断/文件缺失而不存在，这里改用 INSERT ... ON CONFLICT DO UPDATE
    ——保证 outcome 信息本身不会因为找不到父行就静默丢失（哪怕其余字段
    这种情况下是 NULL，仍然可以在 decision_attempts 里查到这次结果）。
    """
    conn.execute(
        """
        INSERT INTO decision_attempts(decision_id, attempted_at, outcome, outcome_detail,
                                       resolved_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(decision_id) DO UPDATE SET
            outcome = excluded.outcome,
            outcome_detail = excluded.outcome_detail,
            resolved_at = excluded.resolved_at
        """,
        (decision_id, resolved_at, outcome, outcome_detail, resolved_at),
    )


def get_decision_attempt(conn: sqlite3.Connection, decision_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM decision_attempts WHERE decision_id = ?", (decision_id,))
    return cur.fetchone()


def upsert_error(conn: sqlite3.Connection, *, source_file_id, line_no, category, game_id=None,
                  decision_id=None, summary, time_value=None) -> None:
    conn.execute(
        """
        INSERT INTO errors(source_file_id, line_no, category, game_id, decision_id, summary, time)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_file_id, line_no, category) DO UPDATE SET
            summary = excluded.summary,
            game_id = excluded.game_id,
            decision_id = excluded.decision_id,
            time = excluded.time
        """,
        (source_file_id, line_no, category, game_id, decision_id, summary, time_value),
    )


def upsert_aggregate(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO aggregates(key, value, computed_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, computed_at=excluded.computed_at",
        (key, json.dumps(value, ensure_ascii=False), _utc_now()),
    )
