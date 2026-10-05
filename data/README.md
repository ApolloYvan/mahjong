# data/ 数据资产目录

本目录是可查询数据层的"入口文档 + 可提交资产"部分。原始日志、门户事件流和
SQLite 数据库**不进入本目录/不进入版本控制**，只有小型 fixture、聚合摘要
和本文档会被提交。

## 目录结构

```text
data/
  README.md              # 本文件
  schema_version.json     # SQLite schema 版本标签，与 mj/datastore.py:SCHEMA_VERSION 一致
  fixtures/
    protocol/              # 赛事生命周期、墙尾杠门槛等协议契约样本
    scoring/                # 番值/规则契约样本（含 fan4/fan2 冲突脱敏案例）
    failures/               # action_rejected/fallback_sent/429/超时/解析失败样本
  aggregates/               # 历史审计聚合摘要（derived_summary，非实时数据）
```

**不进入本目录、不提交到版本控制**（若未来初始化 Git 仓库，需写入
`.gitignore`；当前仓库尚无 `.git`，这条排除清单以本文档为约束依据）：

```text
data/archive/          # 冷归档：压缩后的原始日志/门户牌谱，不提交
data/index/             # SQLite 数据库文件所在目录（若约定放在这里）
logs/                    # 原始决策日志 JSONL（当前工作区不存在）
portal_events/          # 原始门户事件流 JSON（当前工作区不存在）
portal_cookie.txt        # 门户登录凭据
*.sqlite
*.sqlite3
```

## 数据边界（诚实声明）

截至本文档撰写时（2026-09-21），当前工作区**不包含**任何真实的
`logs/`、`portal_events/`、`models/` 目录或 SQLite 数据库。本目录下的所有
fixture 和聚合文件都明确标注了以下三种来源之一，不存在被冒充为"原始服务端
文件"的内容：

| provenance 标签 | 含义 | 可信度 |
|---|---|---|
| `derived_summary` / `derived_from_audit` | 数据来自 `/Users/yuanye/Documents/ChatGPT/麻将大赛/review/audit_data.json`（独立技术评审报告扫描真实历史日志产出的聚合结果），本次会话只做字段摘录/重排/程序化提取，不重新计算原始日志 | 高——来自真实历史扫描，但是"一次性快照"，不代表当前策略实时表现 |
| `synthetic_test_fixture` | 本次会话构造的合成测试数据，字段结构对齐真实 schema（`mj/logging.py`/`mj/reconcile.py`/`mj/tournament.py` 等模块的真实数据格式），但具体数值/手牌/game_id 是虚构的 | 仅用于验证代码逻辑，不代表真实对局结果 |
| `exported_from_local_sqlite_db` | 由 `tools/data_tool.py fixture-export` 从本地导入的数据库导出，字段完整性取决于导入源数据 | 取决于导入源；导入源为 synthetic 时，导出结果也是 synthetic |

**未来用户提供少量真实脱敏数据后**，应能直接把文件放进对应 glob 路径（如
`logs/*.jsonl`、`portal_events/*/*.json`）并运行：

```bash
python3 tools/data_tool.py import-logs "logs/*.jsonl" --db data/index/mahjong_history.sqlite
python3 tools/data_tool.py import-events "portal_events/*/*.json" --db data/index/mahjong_history.sqlite
```

不需要改代码——导入器只依赖文件格式（`.jsonl`/`.jsonl.gz`/`.json`），不依赖
文件名本身是否"看起来像真实数据"。

## SQLite Schema 概览（v2，阶段A返修）

完整 DDL 见 [`mj/datastore.py`](../mj/datastore.py)。核心表：

- `source_files`：已导入文件的路径、SHA-256、大小、mtime、格式、
  **`status`**（`importing`/`complete`/`failed`，阶段A新增）、导入/完成
  时间、总行/成功行/失败行、来源类型、`error_message`（失败时的脱敏错误
  摘要）。**幂等去重键**：`sha256`（内容不变的文件重复导入会被跳过，不
  重新解析）。**状态语义**：插入时状态为 `importing`；导入流程正常结束
  后显式置为 `complete`；导入过程中抛出未处理异常时显式置为 `failed`
  并保留已处理的行数统计——不允许一个中途失败的文件停留在"看似已完整
  导入"的状态。
- `games`：`game_id`（主键）、session/room、policy_version、config_hash、
  开始/结束时间、完整度（`unknown`/`partial`/`complete`）。Upsert 时
  session_id/room_id/policy_version/config_hash 只补全当前为 `NULL` 的
  字段；`started_at` 取所有来源的最小值，`ended_at` 取所有来源的最大值
  （阶段A修复：旧版 `ended_at` 用 `COALESCE(old,new)`，导致其值永远停留
  在"第一次写入时的时间"）。
- `round_results`：`(game_id, round_no, winner_seat, source)` 唯一约束。
  `winner_seat` 在存储层用哨兵值 `-1` 代替 `NULL`（阶段A修复：SQLite 的
  `UNIQUE` 约束对 `NULL` 不做相等性判断，多条"流局/无赢家"记录不会被
  去重；`mj.datastore.denorm_winner_seat()` 在查询边界把 `-1` 转换回
  `None`，调用方不需要关心哨兵值本身）。`source` 区分 `server_truth`
  （服务端 `round_ended` 结算真相）与 `local_estimate`（本地 `hu_detail`
  规则估算），两者可以同时存在，供 `reconcile` 子命令逐字段对比。
- `decisions`：`decision_id` 主键。旧日志缺 `decision_id` 时按
  `(source_sha256, line_no, kind, game_id, round_no, seat, time)` 派生
  一个确定性 ID（`derived-` 前缀），并标记 `decision_id_synthesized=1`
  （阶段A修复：旧版只用 `game_id/round_no/seat/time` 四元组派生，理论上
  "同一时刻同一局同一座位记录两次决策"会碰撞；新版绑定来源文件内容+
  行号，天然不会碰撞）。缺 `policy_version`/`config_hash`/`schema_version`
  的历史记录显式映射为 `unknown_legacy`/`unknown`（阶段A修复：不再存
  `NULL` 让调用方自己猜测是"未知"还是"确实没有这个概念"）。
- `errors`：`(source_file_id, line_no, category)` 唯一约束。`category` 覆盖
  JSON/UTF-8/schema 解析失败，以及 `action_rejected`/`fallback_sent`/
  `timeout`/`rate_limited_429`/`conflict_409`/`unclassified_error`（阶段A
  新增：无法从文本判断具体类别的通用错误归入此类，不再默认归为
  `timeout`）等协议层异常。**`summary` 字段在写入前统一经过
  `mj.sanitize.sanitize_text()` 处理**（阶段A修复：旧版 `_redact_summary`
  只做截断不做替换，导致 UTF-8 错误行/Bearer 头/action_rejected 里的
  token 原样进库——见下方"统一脱敏"一节）。
- `aggregates`：`key`（主键）→ JSON 值，供 `summary`/`reconcile` 子命令
  缓存最近一次计算结果。

## 统一脱敏（阶段A新增：`mj/sanitize.py`）

所有对外输出（`errors.summary`、CLI stdout/stderr、`fixture-export` 导出、
未来异步日志异常文本）必须经过 `mj.sanitize`，不允许各模块各自实现脱敏：

- `sanitize(value)` / `sanitize_text(text, limit=...)`：递归/字符串级脱敏——
  字段名命中 `authorization`/`bearer`/`token`/`cookie`/`session`/
  `majiang_sid`/`password`/`secret` 等模式时整体替换为 `***REDACTED***`；
  任意位置出现的历史 64 位十六进制令牌样式或内嵌的 `Bearer <token>` 片段
  也会被替换（不依赖字段名，纯文本扫描）。
- `stable_pseudo_id(domain, value, salt)`：带域前缀的 HMAC-SHA256 稳定假
  ID（如 `game_1a2b3c...`），同一 `(domain, value, salt)` 恒定产出同一
  假 ID，不可逆。盐必须来自 `--salt` 参数或 `MJ_SANITIZE_SALT` 环境变量，
  **绝不写入任何输出**。
- `fixture-export` 缺盐时默认失败（`MissingSaltError`），除非显式传
  `--synthetic-source`（标记输入本身是合成数据，不含真实身份信息）。
  `--pseudonymize-ids` 会用稳定假 ID 替换导出 fixture 里的 `game_id`。

已验证的三类真实反例（`tests/test_sanitize.py`/`tests/test_data_tool_cli.py`
均覆盖，且做过端到端 CLI 手工复现）：UTF-8 replace 错误行里嵌的令牌、
`kind=error` 的 `Authorization: Bearer <token>` 文本、`action_rejected`
payload 里的 token 字符串——均确认不出现在数据库、CLI 输出或导出 fixture
中。`mj.security.scan()` 对导出目录和整个仓库均无命中。

## 完整字段对账（阶段A返修：`mj.reconcile.reconcile_fields`）

旧版 `reconcile()`/`summarize()` 只比较 `fan` 是否相等就判定"整条一致"——
独立复现反例：本地/服务端 `fan` 都是 2，但 `detail` 分别是 `["平胡"]`/
`["平胡","爆头"]`，旧版仍报 `agreements=1`、退出码 0。

新入口 `reconcile_fields(local, server)` 逐字段输出
`winner`/`fan`/`detail`/`scores`/`next_dealer` 各自的 `equal`/`mismatch`/
`unavailable` 状态：
- `detail` 比较前规范化为"排序后的元组"（顺序不敏感，但元素集合不同算
  mismatch）；
- 本地估算天然没有 `scores`/`next_dealer` 时标记 `unavailable`，计入覆盖率
  统计，不假装 `equal`，也不误判为规则错误（`mismatch`）；
- 必比字段 `winner`/`fan`/`detail` 任一 `mismatch` → 整体判定失败
  （`must_compare_ok=False`）；`scores`/`next_dealer` 只统计覆盖率，不
  影响整体判定。

`tools/data_tool.py reconcile` 已切换为使用这套逐字段对账，退出码见下方
"退出码约定"。旧的 `mj.reconcile.reconcile()`/`summarize()` 仍保留（供
历史测试和"只需要粗略 fan 对比"的简单场景使用），但不再是 CLI 的对账
实现。

## 真正流式 / 有界内存导入（阶段A返修：`mj.data_import`）

旧版 `parse_decision_log_lines()` 逐行读取文件没错，但把全部记录累积进
一个 `ParseResult` 列表，文件解析完才整体写库——10万行会同时保留10万个
Python 对象。新版 `import_decision_log_stream()` 逐行直接写入 SQLite，
每 `--batch-size`（默认 2000，可配置）行提交一次事务，内存占用是 O(1)。

**峰值内存验收证据**（`tests/test_data_import_streaming.py::PeakMemoryStressTest`，
`tracemalloc` 实测）：100,000 条合成 decision，源文件 26.57 MiB，峰值追踪
内存 <1 MiB（远低于 20 MiB 的验收上限），耗时约 4 秒。证明峰值不随文件
总行数线性累积。

`source_files.status` 全流程可见：导入开始时为 `importing`，正常结束后
显式置为 `complete`；导入过程中抛出未处理异常时显式置为 `failed`（保留
已处理的行数统计，绝不假装成 `complete`）。

## CLI 用法

见 [`tools/data_tool.py`](../tools/data_tool.py) 顶部 docstring，或直接运行
`python3 tools/data_tool.py <subcommand> --help`。子命令：

```text
data_tool import-logs <glob...> --db <path> [--batch-size N]     # 流式导入决策日志 JSONL/.jsonl.gz
data_tool import-events <glob...> --db <path>                    # 导入门户事件流 JSON
data_tool summary --db <path> [--policy ...] [--allow-empty]     # 打印聚合摘要（含覆盖率/延迟分位数）
data_tool anomalies --db <path> --kind ... [--limit 30]          # 按类型查询异常样本（已脱敏）
data_tool reconcile --db <path> [--game ...]                     # 服务端结算 vs 本地估算逐字段对账
data_tool fixture-export --db <path> --game ... --round ... --output ... [--salt ...] [--synthetic-source] [--pseudonymize-ids]
```

**退出码约定**（避免"没读到数据"被误报为"成功"）：
- `import-logs`/`import-events`：glob 无匹配文件 → 退出码 1；**全部文件
  parse_failed（阶段A新增）→ 退出码 1**。
- `summary`：数据库文件不存在 → 退出码 1；数据库存在但为空 → **默认退出码
  1（阶段A修复：旧版空数据返回0），显式传 `--allow-empty` 才返回 0**，
  输出 JSON 中会带 `"warning": "no data found..."` 字段。
- `reconcile`：数据库不存在 → 1；`round_results` 表完全没有数据 → 2；
  有数据但 `matched=0`（本地/服务端 key 完全不重叠）→ 2；必比字段
  （winner/fan/detail）任一 mismatch → 1；必比字段全部一致（可能有
  scores/next_dealer 的 unavailable）→ 0。
- `fixture-export`：查无数据 → 1；缺盐且未标记 `--synthetic-source` → 1。

## `summary` 聚合报告字段（阶段A补齐）

`data_tool summary` 输出新增以下字段（百分比均带分子/分母，不是裸数字）：
`parse_success_rate`、`source_files.by_status`、`round_results.{total_rows,
unique_game_round,duplicate_rows}`、`decisions.{total_rows,unique_decision_id}`、
`policy_config_build_coverage.{policy_version_unknown_legacy,config_hash_unknown}`、
`server_local_reconciliation_coverage.{server_only,local_only,both,
coverage_of_all_round_keys}`、`errors_by_category`、
`action_rejected_rate`/`fallback_sent_rate`（分母为 decisions 总数，即
"正常分母"）、`action_request_ms_latency.{count,p50,p95,p99}`、
`missing_field_rate.state_hash`。

## 已知限制（不隐瞒）

1. **增量导入未实现按 offset 续读**：同一路径的按天滚动日志文件如果内容
   变化（如当天持续追加新行），会产生新的 SHA-256，从而触发整份文件重新
   解析。依赖业务表自身的唯一约束（`decision_id` 主键等）去重，不会产生
   重复记录，但会有重复解析的计算开销。这是任务书范围内可接受的已知限制，
   未来如需优化可以加一张"文件路径 → 已处理字节数"的续读表。
2. **门户事件流按整份 JSON 读取，不逐行流式**：单个门户事件流文件是一个
   JSON 对象（不是 JSONL），本轮沿用 `mj/reconcile.py`/`tools/room_audit.py`
   既有假设整体 `json.load`。这类文件历史上体积远小于决策日志（个位数 MB
   级），本轮未观察到需要流式处理的证据。
3. **高频 kind（`state`/`notify`/`piao_attempt`/`result`）不逐条导入业务表**：
   对应数据保留方案"正常轮询快照低频采样"的减量策略，导入器只统计这些
   记录的总行数（计入 `source_files.total_lines`/`success_lines`），不为
   它们单独建表——避免数据库随高频轮询记录无限膨胀。若未来需要这些记录的
   查询能力，需要新增对应表和导入分支，这不在本轮范围内。
4. **本轮未导入任何真实数据**：所有验证均基于会话内构造的合成 fixture、
   100,000 行合成 decision 的压力测试数据，或 `derived_summary`/
   `derived_from_audit` 聚合结果，不构成"已验证真实牌谱"的结论。
5. **`stable_pseudo_id`/`--pseudonymize-ids` 目前只对 `fixture-export` 的
   `game_id` 生效**：`decisions`/`round_results` 表内部字段（如 `seat`）
   本轮未接入假名化——本地技术 fixture 场景下这些字段通常不构成隐私风险，
   若未来需要对外发布更大范围数据，需要扩大假名化覆盖面，这不在本轮范围内。

## 大模型使用方式（避免把几百 MB 原文塞进上下文）

1. 先读本文件 + `schema_version.json` + `aggregates/*.json`，了解数据规模
   和已知问题，不读原始日志。
2. 需要具体样本时，调用 `data_tool anomalies --kind ... --limit 30` 或
   `data_tool summary`，只读取查询结果（几十条代表样本），不整份读日志。
3. 需要脱敏回归样本时，用 `data_tool fixture-export` 导出单局单轮的最小
   结构，写入 `fixtures/` 对应子目录，附上 `provenance`/`note` 字段说明
   来源和用途（参考本目录现有 fixture 的写法）。
4. 提出假设后，用独立脚本在全量数据库上验证（SQL 查询或 Python 脚本），
   再把小型结果交回上下文解释——不要求模型直接扫描整份 SQLite 文件内容。
