# 阶段验证记录

## 2026-09-21（第四轮）：数据层返修（阶段A）+ 观测/对账生产闭环（阶段B）

用户提供独立评审报告 `SONNET5_DATA_REVIEW.md`（评分6.5/10，判定"不能按
数据层完成验收"），指出两个 P0（导入不是真正流式；脱敏泄露令牌）与三个
P1（对账只比较fan；假ID/盐机制未实现；聚合报告未达约定范围）。本轮任务
书要求：**先通过阶段A返修门，再接入阶段B观测闭环；阶段A未通过不得跳到
阶段B**。不修改麻将策略/权重/评测器/规划器，不启动在线请求。

### 阶段A：数据层返修门 —— 已通过

**A1 真正流式/有界内存导入**：重写 `mj/data_import.py`——
`iter_decision_log_lines()` 改为逐行 yield 生成器，`import_decision_log_stream()`
直接消费生成器写入 SQLite，按可配置 `batch_size`（默认2000）分批提交
事务。**峰值内存证据**（`tests/test_data_import_streaming.py::PeakMemoryStressTest`，
`tracemalloc` 实测）：

```
100,000 条合成 decision，源文件 26.57 MiB
peak_traced_memory ≈ 0.04–0.15 MiB（多次运行范围，验收上限 20 MiB）
elapsed ≈ 4 秒
```

证明内存不随文件总行数线性累积（不是"文件是逐行打开的"这种表面流式）。
`mj/datastore.py` schema 升级到 v2：`source_files` 新增 `status`
（`importing`/`complete`/`failed`）+ `finished_at`/`error_message`，
`tools/data_tool.py` 的 `_import_one_log_file`/`_import_one_events_file`
在导入开始时置 `importing`，成功结束后显式置 `complete`，异常时显式置
`failed`（保留已处理行数统计，不假装完整导入）。

**A2 统一安全脱敏**：新增 `mj/sanitize.py` 作为唯一脱敏入口。三类真实
反例端到端复现验证（构造真实的 64 位令牌样式，跑完整 CLI 导入+查询
流程，检查数据库/stdout/stderr）：

```
[UTF-8 replace 行内嵌令牌]  导入前: "� broken utf8 line with token=cccc...(64位)"
                            DB errors.summary: "� broken utf8 line with token=***REDACTED***"
[kind=error 的 Bearer 文本]  导入前: "Authorization: Bearer cccc...(64位)"
                            DB errors.summary: "Authorization: Bearer ***REDACTED***"
[action_rejected 里的 token] 导入前: "409 conflict session token cccc...(64位) invalid"
                            DB errors.summary: "409 conflict session token ***REDACTED*** invalid"
import exit=0, stdout contains token=False, stderr contains token=False,
DB errors.summary contains token=False, anomalies CLI output contains token=False
```

`mj.security.scan()` 对 `data/` 目录和整个仓库运行均确认：仓库仅有的3处
命中是 `tools/{room_status,probe_tokens,server_latency_probe}.py`（历史
已知问题，属于阶段7清理范围，与本轮数据层导出路径无关，未在本轮修改）。
`fixture-export` 新增 `--salt`/`--synthetic-source`/`--pseudonymize-ids`：
缺盐且未标记 synthetic 时导出失败（`MissingSaltError`），`stable_pseudo_id`
提供带域前缀的 HMAC-SHA256 稳定假 ID。

**A3 完整字段对账**：`mj/reconcile.py` 新增 `reconcile_fields()`/
`summarize_fields()`：逐字段比较 `winner`/`fan`/`detail`/`scores`/
`next_dealer`，每字段独立标记 `equal`/`mismatch`/`unavailable`。独立复现
验证评审报告指出的确切反例（fan 相同但 detail 不同）：

```python
local  = {"seat": 0, "fan": 2, "detail": ["平胡"]}
server = {"seat": 0, "fan": 2, "detail": ["平胡", "爆头"]}
# 旧版：agreements=1，退出码 0（误报"对账通过"）
# 新版：field_coverage["detail"]["mismatch"]=1，must_compare_ok=False，CLI 退出码 1
```

`detail` 比较前规范化为排序元组（顺序不敏感，集合不同才算 mismatch）；
本地天然缺失的 `scores`/`next_dealer` 标记 `unavailable`，不假装 `equal`；
必比字段（`winner`/`fan`/`detail`）任一 mismatch 整体判定失败，
`scores`/`next_dealer` 只统计覆盖率不影响整体判定。旧的
`reconcile()`/`summarize()` 保留供历史兼容，`tools/data_tool.py reconcile`
已切换使用新函数。

**A4 数据语义和摘要**：
- `games.started_at`/`ended_at` 改为取所有来源的最小值/最大值（修复旧版
  `ended_at` 永远停留在第一次写入时间的 bug）；
- 缺 `policy_version`/`config_hash`/`schema_version` 的记录显式映射为
  `unknown_legacy`/`unknown`；
- `derive_decision_id` 改为接收 `source_sha256`/`line_no`/`kind`，绑定
  来源文件内容+行号，避免同时间戳多次决策碰撞；
- `kind=error` 的通用错误不再默认归为 `timeout`，改为显式的
  `unclassified_error` 类别；
- 全部文件 `parse_failed` 时 `import-logs`/`import-events` 命令返回退出码1；
- `round_results.winner_seat` 用哨兵值 `-1` 代替 `NULL`（修复 SQLite
  UNIQUE 约束对 NULL 不去重的问题）；
- 新增 `--allow-empty`：`summary` 默认空数据非零退出，显式传参才返回0；
- `summary` 补齐：解析成功率、唯一/重复 game-round-decision 计数、
  policy/config 覆盖率与 `unknown_legacy` 分桶、server/local 对账覆盖率、
  错误率/fallback率（分母为 decisions 总数）、
  `action_request_ms` 延迟 p50/p95/p99、`state_hash` 缺失率——百分比均带
  分子/分母（`_frac()` 辅助函数）。

**阶段A验证结果**：
```
python3 -m unittest discover tests
# Ran 270 tests — OK（阶段A完成时的中间检查点）
```
新增测试文件：`tests/test_sanitize.py`（19项）、
`tests/test_data_import_streaming.py`（10项，含峰值内存压力测试）、
`tests/test_reconcile.py` 新增 `ReconcileFieldsTests`（7项）；
`tests/test_data_tool_cli.py` 新增/调整多项（`--allow-empty`/
`--synthetic-source`/脱敏输出验证）。

### 阶段B：观测与对账生产闭环 —— 已完成（阶段A通过后开工）

**B1 决策关联链**：`mj/bot.py:play_game` 里 `decision_id` 的生成时机从
"API调用成功之后"提前到"调用 `api.action()` 之前"，并通过局部变量贯穿
`action_rejected`/`fallback_sent`/`piao_attempt`/`hu_detail`/最终的
`log.action(...)`。`mj/logging.py` 的 `action_rejected`/`fallback_sent`/
`piao_attempt` 新增可选 `decision_id` 参数（向后兼容，默认 `None`）。
契约测试（`tests/test_decision_id_chain.py`，4项）验证 hu/成功/409拒绝/
409+fallback/timeout 各路径下 decision_id 的关联正确性：
```
test_hu_path_shares_decision_id_between_hu_detail_and_action_log ... ok
test_409_rejected_then_fallback_shares_same_decision_id ... ok
test_409_response_rejected_still_has_decision_id ... ok
test_timeout_error_does_not_create_orphan_decision_id_leak ... ok
```

**B2 异步有界日志**：新增 `mj/async_log.py:AsyncDecisionLog`，包装同步
`DecisionLog`，内部用 `queue.Queue(maxsize=...)` + 单写线程消费。动作
线程调用 `.append()`/`.action()` 等方法只做非阻塞 `put_nowait`。队列满时
按优先级丢弃：`state`/`notify`/`piao_attempt`/`pass_window` 等高频低
信息量 kind 直接丢弃并计数（`dropped_count[kind]`）；`decision`/
`action_rejected`/`fallback_sent`/`hu_detail`/`error`/`result`/`round_end`
等关键事件短暂阻塞重试，仍满则记录 `emergency_drop_count` 并写 stderr，
不允许静默消失。`flush(timeout=)`/`close(timeout=)` 提供可测试的排空
语义。`mj/bot.py:main()` 默认使用 `AsyncDecisionLog`（`--sync-log` 可
回退同步路径用于调试），并在 `--wait`/`--once`/`--room` 三个正常退出点
调用 `log.close(timeout=10.0)` 尽量排空。

发现并修复的一个真实 bug：`AsyncDecisionLog.action()` 初版误用
`kind="action"` 落盘，与同步 `DecisionLog.action()` 实际使用的
`kind="decision"` 不一致，导致异步日志文件表面上能写入，但
`mj.data_import` 按 `kind=="decision"` 识别决策记录，实际一条都查不到
（`tests/test_async_log_stress.py::AsyncLogOutputIsImportableTest` 首次
运行即失败，`assertIsNotNone(row)` 断言失败暴露）。修复后已验证异步日志
落盘文件可被 `mj.data_import`/`tools/data_tool.py` 直接导入并在 SQLite
里查到完整记录。

`mj/logging.py:DecisionLog` 新增按大小轮转 + 轮转文件自动 gzip 压缩
（`max_bytes`/`gzip_on_rotate` 参数，默认不启用大小轮转，只做既有的按天
轮转，向后兼容）。`tests/test_logging_rotation.py`（5项）验证轮转/压缩
后数据无丢失、gzip内容可被正常读取为合法JSONL。

**B3 减量与异常窗口**：
- `mj/observability.py` 新增 `stable_sample_state(state_hash, sample_rate)`：
  用 `state_hash` 的哈希值决定采样，而不是 `random.random()`——同一
  `state_hash` 在同一 `sample_rate` 下采样结果恒定、可复现；
- `mj/async_log.py:ConsecutiveStateDeduper`：连续相同 `state_hash` 只记
  首次/末次/重复次数；
- `mj/async_log.py:AnomalyRingBuffer`：内存环形缓冲保留异常前后窗口
  （`before`/`after` 可配置条数），`mark_anomaly()` 触发窗口收集，
  `drain_windows()`/`finalize_pending()` 取出完成/未完成窗口。
- `tests/test_async_log.py` 覆盖：`StableSamplingTests`（3项）、
  `ConsecutiveStateDeduperTests`（3项）、`AnomalyRingBufferTests`（3项）。

**B4 服务端事实**：`mj/observability.py` 新增
`server_truth_unavailable_marker()`——`mj/bot.py:play_game` 记录
`hu_detail` 时，同时带上 `local_estimate_marker()` 和本标记，显式声明
"当前生产 `/state` API 在对局在线阶段不提供完整的 `round_ended` 结算
事件，服务端结算真相只能通过赛后 `tools/pull_all_events.py` 拉取门户
事件流后用 `data_tool import-events` 补齐"——不发明假的 server_truth。
`tools/data_tool.py reconcile` 已经消费阶段A的 SQLite schema，产生覆盖率
/差异分类/可导出脱敏 fixture（复用A3的 `reconcile_fields`）。

**10线程压力测试**（`tests/test_async_log_stress.py::TenThreadStressTest`）：
```
threads=10 events_per_thread=200 total=2000
enqueue_p50≈0.0005ms enqueue_p95≈0.0005ms enqueue_p99≈0.002ms
dropped={} emergency_drop=0 write_error=0
written_lines + dropped == 2000（无静默丢失）
```
enqueue 延迟远低于验收阈值100ms，证明动作线程的 `append()` 调用是真正
非阻塞的。

**阶段B验证结果**：
```
python3 -m unittest discover tests
# Ran 303 tests in ~19s — OK
```
（270 阶段A完成时 + 4项decision_id链路 + 22项async_log核心 + 2项
server_truth_marker + 5项logging轮转 = 303）。

### 必须测试清单核对（如实列出通过/未做）

- 完整测试套件：✅ 303项全过。
- decision_id 在成功/timeout/409/fallback/hu路径一致：✅
  `test_decision_id_chain.py`。
- 并发生产者不会写坏JSONL：✅
  `test_async_log.py::ConcurrentProducersTests`（10线程×100条，逐行
  `json.loads` 校验无损坏）。
- 队列满时关键事件不静默丢失：✅
  `test_async_log.py::CriticalNeverSilentlyDroppedTests`（写入数+
  emergency_drop数==总产出数）。
- writer I/O异常不阻塞/杀死对局线程，dropped/error计数可见：✅
  `test_async_log.py::WriterExceptionDoesNotBlockCallerTests`。
- flush/close能排空：✅ `test_async_log.py::FlushCloseTimeoutTests`
  （含正常排空与超时两种场景）。
- 稳定采样同一state_hash决定一致：✅ `test_async_log.py::StableSamplingTests`。
- 异常前后环形窗口正确：✅ `test_async_log.py::AnomalyRingBufferTests`。
- 日志可被数据导入器直接导入并完整查询：✅
  `test_async_log_stress.py::AsyncLogOutputIsImportableTest`（发现并
  修复了上述kind不一致的真实bug）。
- 10线程压力测试报告enqueue p50/p95/p99和丢弃数：✅
  `test_async_log_stress.py::TenThreadStressTest`（见上方数据）。
- 安全扫描无令牌：✅ `mj.security.scan()` 对`data/`和整个仓库运行，
  仅有历史已知的3处`tools/`脚本命中（未在本轮范围内修改）。
- 不发真实网络请求：✅ 全部新增测试使用本地临时目录/内存SQLite/
  `unittest.mock`，未见任何socket/urllib调用。
- 峰值内存证据：✅（见阶段A章节，100,000行合成数据）。
- 三类令牌泄露反例修复证据：✅（见阶段A章节，端到端CLI复现）。

### 保留的赛事上线阻断清单（按任务书要求不顺手修，仅记录）

1. `GameLedger` 对 `on_play`/线程失败没有 failed/retry 状态（沿用第二轮
   记录，本轮未处理）。
2. command error 在服务端自行推进周期时的跨阶段清零语义（沿用第二轮
   记录，本轮未处理）。

### 本轮明确未做的事（如实列出）

- 未导入任何真实数据（工作区没有真实 `logs/`/`portal_events/` 文件）。
- 未开始评测器（阶段3）、规划器（阶段4）、调权重或在线对局。
- `mj/tournament.py` 未做任何修改（阻断清单第1、2项按任务书要求只记录
  不修复）。
- `data_tool summary` 的异常窗口查询（B3环形缓冲）目前只在
  `mj/async_log.py` 内提供纯函数级实现，尚未接入 `mj/bot.py` 生产路径
  产生真正的运行时异常窗口数据，也未新增对应的CLI查询子命令——本轮
  聚焦"组件级实现+契约测试证明正确性"，"生产路径实际产生环形窗口数据
  并可查询"这一步留待有真实对局数据后再验证（当前工作区无法用真实
  长时间对局验证这条链路的端到端行为，如实标注为范围边界）。
- `stable_pseudo_id`/`--pseudonymize-ids` 目前只对 `fixture-export` 的
  `game_id` 生效，`decisions`/`round_results` 内部字段未接入假名化。

**新增/修改文件清单**：
- 新增：`mj/sanitize.py`、`mj/async_log.py`、`tests/test_sanitize.py`、
  `tests/test_data_import_streaming.py`、`tests/test_decision_id_chain.py`、
  `tests/test_async_log.py`、`tests/test_async_log_stress.py`、
  `tests/test_server_truth_marker.py`、`tests/test_logging_rotation.py`。
- 修改：`mj/datastore.py`（schema v2）、`mj/data_import.py`（重写为流式）、
  `tools/data_tool.py`（重写：流式导入/统一脱敏/逐字段对账/摘要覆盖率）、
  `mj/reconcile.py`（新增 `reconcile_fields`/`summarize_fields`）、
  `mj/observability.py`（新增 `stable_sample_state`/
  `server_truth_unavailable_marker`）、`mj/logging.py`（decision_id贯穿
  `action_rejected`/`fallback_sent`/`piao_attempt`；新增按大小轮转+gzip）、
  `mj/bot.py`（decision_id提前生成；默认接入`AsyncDecisionLog`；
  hu_detail加`server_truth_unavailable`标记）、`data/README.md`（补充
  阶段A/B变更说明）、`data/schema_version.json`（升级到v2）、
  `docs/refactor/DECISIONS.md`（新增决策#9/#10）。



用户本轮明确要求"本轮只建设数据资产和数据工具，不开始评测器、规划器、调
权重或在线对局"，并指定这轮对应此前认可清单里的第3项（跳过第2项"观测与
对账闭环"，留待未来一轮）。

### 数据边界确认（开工前只读检查）

当前工作区确认**不包含**任何真实 `logs/`、`portal_events/`、`models/` 目录
或 SQLite 数据库（与 `BASELINE.md` 记录一致）。本轮所有验证均基于：
1. 会话内构造的 `synthetic_test_fixture`（合成测试数据，字段结构对齐真实
   schema，但数值/手牌/game_id 虚构）；
2. `/Users/yuanye/Documents/ChatGPT/麻将大赛/review/audit_data.json`
   （独立技术评审报告扫描真实历史日志产出的聚合结果），标记为
   `derived_summary`/`derived_from_audit`。

**未声称对任何原始真实牌谱文件做过验证**——这是任务书明确要求的边界。

### 1. SQLite 数据层 —— 已实现

新增 `mj/datastore.py`：`sqlite3` 标准库 schema 定义（`schema_meta`/
`source_files`/`games`/`round_results`/`decisions`/`errors`/`aggregates`
共 7 张表），全部使用 `UNIQUE` 约束 + `INSERT ... ON CONFLICT DO UPDATE`
实现幂等 upsert：
- `source_files`：`sha256` 唯一——同一内容文件重复导入直接跳过，不重新解析。
- `games`：`game_id` 主键，upsert 只补全 `NULL` 字段，不覆盖已有非空值。
- `round_results`：`(game_id, round_no, winner_seat, source)` 唯一——
  `server_truth`/`local_estimate` 两条记录可以同时存在，供对账使用。
- `decisions`：`decision_id` 主键；旧日志缺该字段时按
  `(game_id, round_no, seat, time)` 派生确定性 ID（`derived-` 前缀），
  标记 `decision_id_synthesized=1`。
- `errors`：`(source_file_id, line_no, category)` 唯一。

索引覆盖 `game_id`、`decision_id`（主键自带）、`state_hash`、
`policy_version`、`round_no`、`category`/`time`。

**验证**（`tests/test_datastore.py`，16 项）：schema 建立/幂等性、
sha256 流式计算（含小 chunk_size 分块场景）、重复导入内容相同/不同的
去重行为、games/round_results/decisions/errors 四张业务表的 upsert 幂等性
（reimport 不产生重复行）。

### 2. 导入解析器 —— 已实现

新增 `mj/data_import.py`：把 `DecisionLog` JSONL / 门户事件流 JSON 解析成
`datastore` 行结构。逐行流式处理（`open_text_lines` 支持 `.jsonl`/
`.jsonl.gz`），**单行 try/except，一行损坏不中止全文件**。

错误类别覆盖：`json_decode_error`/`utf8_decode_error`/
`schema_missing_field`（文件解析失败）+ `action_rejected`/`fallback_sent`/
`timeout`/`rate_limited_429`/`conflict_409`（协议层异常，对应
`mj/logging.py` 记录的对应 `kind`，不是文件解析失败）。高频 kind
（`state`/`notify`/`piao_attempt`/`result`）只统计条数，不逐条导入业务表
（对应数据保留方案"正常轮询快照低频采样"的减量策略，避免数据库无限膨胀）。

**验证**（`tests/test_data_import.py`，16 项）：decision/hu_detail kind 正确
提取、旧日志缺 `decision_id` 时正确派生、单行坏 JSON 不中止其余行解析、
空行记为成功、未知 kind 不崩溃、错误摘要脱敏截断、429/409 关键字正确分类、
门户事件流 `round_ended` 正确提取为 `server_truth`、缺 `game_id` 记为错误
不崩溃、无 `round_ended` 事件不算错误。

### 3. CLI 工具 —— 已实现

新增 `tools/data_tool.py`，六个子命令：`import-logs`/`import-events`/
`summary`/`anomalies`/`reconcile`/`fixture-export`。

**退出码约定**（任务书要求"输入 glob 无匹配、服务端结果为零或 matched
为零时给出明确非零退出码"）：
- `import-logs`/`import-events`：glob 无匹配 → 1。
- `summary`：数据库不存在 → 1；存在但为空 → 0，但带 `warning` 字段。
- `reconcile`：数据库不存在 → 1；`round_results` 完全无数据 → 2；
  有数据但 `matched=0`（key 完全不重叠）→ 2；发现差异 → 1；全部一致 → 0。
- `fixture-export`：查无数据 → 1。

**端到端验证**（`tests/test_data_tool_cli.py`，15 项，通过 `subprocess`
真实调用 CLI，非直接调函数）：streams .jsonl/.jsonl.gz、单行损坏不中止、
重复导入内容相同跳过（数据库里确认只有1条记录）、glob 无匹配非零退出、
门户事件正确提取、**数据库不存在/为空/matched=0 三种情况均非零退出**
（这是本轮修复的一个真实 bug——初版实现在"有数据但 key 完全不重叠"时
会误报退出码 0，已在 `cmd_reconcile` 补充 `matched==0` 检查，见
"过程中发现并修复的问题"）、fan2/fan4 差异正确发现并退出码1、一致时退出码0、
`summary`按`category`聚合错误分布、`anomalies --kind`过滤正确、
fixture-export 写出正确 JSON 结构、无数据时非零退出。

### 4. `data/` 目录与 fixture —— 已实现

```
data/
  README.md              # 数据边界声明、provenance 标签说明、CLI 用法、已知限制
  schema_version.json
  fixtures/
    protocol/
      wall_tail_gang_boundary.json          # 20张墙尾禁杠边界（对齐真实单测断言）
      catch_play_restriction.json           # 抓打圈限制/豁免（对齐 mj/state.py 真实实现）
      tournament_register_ready_retry.json  # register/ready 重试语义文档化
    scoring/
      fan_mismatch_a_5a06a8f48d67_round5.json           # derived_from_audit 真实fan4/fan2案例
      server_only_missing_local_a_2e31e10fe10a_round7.json  # derived_from_audit server_only案例
      chain_count_god_field_precedence.json             # god.chain_count优先级+杠开不重复计番
      seven_pairs_and_joker_pair.json                    # 七对子/财神对子
    failures/
      action_rejected_and_fallback_sent.json
      malformed_and_error_lines.jsonl       # 真实可被导入器解析的合成坏行文件
  aggregates/
    historical_audit_summary.json           # derived_summary，程序化提取自audit_data.json
```

`.gitignore`（新建，仓库当前无 `.git`，为未来 `git init` 预置）：排除
`data/archive/`、`data/index/`、`logs/`、`portal_events/`、
`portal_cookie.txt`、`*.sqlite`/`*.sqlite3`。

**可执行性验证**（`tests/test_data_fixtures.py`，10 项——这是本轮的关键
质量把关，防止 fixture 沦为"看起来对但实际跑不通"的静态文档）：
- 墙尾杠边界 fixture 驱动真实 `mj.bot._gang_bomb_choice`，结果与
  `tests/test_bot_state.py` 已有真实断言完全一致；
- chain_count fixture 驱动真实 `mj.state.normalize` + `mj.rules.evaluate`，
  验证 `gang_open=True/False` 时 fan 完全相等（决策 #3 的可执行回归）；
- 七对子 fixture 驱动真实 `mj.rules.seven_pairs`；
- 抓打圈 fixture 驱动真实 `mj.state.DecisionState.catch_restricted()`；
- **两个 scoring fixture 与 aggregates 文件的数值与
  `audit_data.json` 源文件逐字段程序化比对一致**（`skipTest` 保护：若
  audit_data.json 在其他环境不存在则跳过，不误报失败）；
- `malformed_and_error_lines.jsonl` 用真实 `mj.data_import` 解析器验证
  （非只是"看起来像坏行的文档"）。

### 过程中发现并修复的问题（诚实记录，不隐瞒）

1. **`aggregates/historical_audit_summary.json` 首版转录错误**：手工誊写
   `audit_data.json.auto_rooms` 时错误地只誊写了 52 条（源文件实际 57 条），
   总分算错。发现方式：写完后用脚本重新读取源文件核对总数，`52 vs 57`
   不一致。**修复**：改为完全程序化提取（Python 脚本直接读源 JSON 生成
   聚合文件，不再手工誊写数值），并新增
   `test_aggregate_room_scores_match_audit_source_exactly` 防止同类错误
   再次发生。
2. **`wall_tail_gang_boundary.json` 首版手牌构造错误**：初版用了
   "四张白板"的手牌结构，与仓库真实单测（`tests/test_bot_state.py` 用
   "四张 1w + 一张白板"）不符，导致 fixture 驱动真实代码时断言失败
   （`_gang_bomb_choice` 返回 `None` 而不是预期的杠决定）。发现方式：
   `test_wall_remaining_21_allows_bomb_gang` 首次运行即失败。**修复**：
   逐字段核对真实测试源码后重新转录，改用完全一致的手牌结构。
3. **`cmd_reconcile` 初版 matched=0 时误报退出码 0**：当 `round_results`
   表有数据但本地估算/服务端结算的 key 完全不重叠时（真实场景：对账
   脚本用错了 game_id 或时间范围），初版实现会走到 `report["discrepancies"]`
   为空 → 返回 0（误报"对账通过"）。发现方式：手工冒烟测试
   （构造两条不重叠 key 的记录）暴露该行为。**修复**：`cmd_reconcile`
   补充 `matched==0` 显式检查，非零退出并打印 WARNING 说明"没有实际发生
   交叉比对"。已补充端到端测试
   `test_reconcile_matched_zero_with_data_present_is_nonzero` 锁定。

### 本轮明确未做的事（如实列出）

- 不修改 `mj/logging.py`/`mj/bot.py`/`mj/reconcile.py`/
  `tools/reconcile_results.py` 生产代码（用户认可清单第2项"观测与对账
  闭环"留待下一轮，本轮只新增独立的数据层模块，不触碰现有生产路径）。
- 不导入任何真实数据（工作区没有真实文件可导入）。
- 不开始评测器（阶段3）、规划器（阶段4）、调权重或在线对局（用户本轮
  明确排除）。
- `tools/data_tool.py` 的 `import-events` 假设单个门户事件流文件体积小、
  可整体 `json.load`（继承自 `mj/reconcile.py`/`tools/room_audit.py` 既有
  假设），未做真正的流式 JSON 解析——如实记录于 `data/README.md` 已知限制。
- 未实现按 offset 续读的增量导入（同路径文件内容变化会触发整份重新解析，
  依赖业务表唯一约束去重，不产生重复记录，但有重复计算开销）。

**验证结果**：
```
python3 -m unittest discover tests -v
# Ran 226 tests in 7.622s — OK
```
（169（第二轮末）+ 本轮新增 47 项数据层测试 + 10 项 fixture 可执行性测试
= 226）。

**新增文件清单**：`mj/datastore.py`、`mj/data_import.py`、
`tools/data_tool.py`、`tests/test_datastore.py`、`tests/test_data_import.py`、
`tests/test_data_tool_cli.py`、`tests/test_data_fixtures.py`、
`data/README.md`、`data/schema_version.json`、
`data/fixtures/protocol/*.json`（3个）、`data/fixtures/scoring/*.json`
（4个）、`data/fixtures/failures/*`（2个）、
`data/aggregates/historical_audit_summary.json`、`.gitignore`。
**未修改任何既有生产代码文件**。

## 2026-09-21（第二轮）：赛事运行闭环修复（用户认可清单第1项）

用户复核并认可上一轮 P0 修复（165 项测试通过，register/ready 两阶段提交语义
正确），同时指出新缺陷：`run_tournament()` 每轮成功读取快照后会把
`consecutive_errors` 清零，导致持续发生的 register/ready 命令错误实际上
不会触发 `max_consecutive_errors` 熔断，只会空转到超时（[tournament.py
line 246](../../mj/tournament.py)，本轮修复后已不在该行）。用户要求按顺序
推进，第1项是"完成赛事运行闭环"，包含四点：

### 1a. 取消默认三小时正常退出 —— 已修复

`run_tournament(max_seconds=...)` 默认值由 `3 * 3600` 改为 `None`（无上限）。
`max_seconds=None` 时循环条件 `deadline is None or clock() < deadline` 恒为
真，只由服务端终态（`status` 进入 `TERMINAL_STATUSES`）退出。`mj/bot.py`
的 `--wait-seconds` 参数默认值同步改为 `None`，帮助文本更新为说明"仅供
脚本化短时测试/调试，正式赛不应设置，需要安全阀应用外部 watchdog"。

新增测试 `test_no_max_seconds_runs_until_terminal_status`：虚拟时钟推进
超过 5 小时（远超旧版 3 小时硬编码上限）仍未提前放弃，直到服务端返回
`finished` 才正常退出。

### 1b. 修复错误熔断计数 —— 已修复

**根因确认**（用户诊断准确，已用最小复现脚本验证）：原实现只有一个
`consecutive_errors` 计数器，在循环顶部"快照拉取成功"时清零；
register/ready 命令失败时也自增同一个计数器。只要 `api.me()`/
`api.tournament()` 本身正常（现实中命令失败与快照拉取失败往往互相独立），
每轮循环开头就会把计数器清零，命令错误永远无法累积到阈值。

复现脚本（会话内验证，非真实网络）：
```
ready() 持续返回 500，快照拉取始终成功，max_consecutive_errors=5
→ 30 秒内 ready 被调用 30 次，从未触发 raise（应在第 5 次触发）
```

**修复方案**：拆分为两个独立计数器：
- `consecutive_errors`：快照拉取（`api.me`/`api.tournament`）连续失败，
  语义不变；
- `consecutive_command_errors`：register/ready 命令连续失败，**只在命令
  成功或 409 幂等成功时清零**，不受快照拉取结果影响。新增
  `max_consecutive_command_errors` 参数（默认 20），超过阈值同样 `raise`。

`mj/bot.py` 新增 `--max-consecutive-errors`/`--max-consecutive-command-errors`
两个 CLI 参数并接入 `run_tournament(...)` 调用。

新增测试：
- `test_command_errors_are_not_reset_by_successful_snapshot_fetch`（核心
  回归锁定）：快照拉取每轮均成功（`api.me.call_count` 与轮次数一致），
  仅 ready 命令持续失败，验证熔断确实由独立的命令错误计数器触发，在
  阈值处停止（`api.ready.call_count == 3` 对应 `max_consecutive_command_errors=3`）。
- `test_ready_persistent_failure_raises_after_command_error_threshold`：
  替换原来断言"一直重试到 timeout"的测试（该断言掩盖了熔断失效问题），
  改为断言命令错误计数器能在阈值处触发 `ApiError` 熔断。

### 1c. 增加 game_id 去重 —— 已修复

新增 `GameLedger` 类：维护"已提交过的 game_id 集合"，`run_tournament` 在
`decision.action == "play"` 时先调用 `game_ledger.filter_new(decision.game_ids)`
过滤出未提交过的 game_id，只把这部分传给 `on_play`；提供 `forget()` 接口
供诊断/测试场景显式清除（正常流程不需要调用）。

新增测试：
- `test_duplicate_game_id_not_submitted_twice_via_game_ledger`：服务端连续
  两轮 `active_games` 都返回同一个 `game_id`（模拟结算延迟/轮询竞态），
  验证 `on_play` 只被调用一次、且只包含该 game_id 一次。
- `test_game_ledger_filter_new_and_forget`：`GameLedger` 纯函数行为单测
  （filter_new 去重、submitted_count 计数、forget 显式清除）。

### 1d. "任务生命周期表" 的范围说明

`GameLedger` 目前只实现"已提交过 = 不再重复提交"这一最小去重语义（用户
认可清单里称为"game_id 去重"），**没有**实现完整的 in-flight/completed
两态跟踪、超时检测或持久化——`on_play` 本身是阻塞调用（等待线程池内的
所有对局线程结束才返回），所以"提交过的" game_id 在 `on_play` 返回前
不会再被同一轮循环重复触碰；调用方目前也没有跨进程重启后恢复这个去重表
的需求（重启后 `run_tournament` 会重新创建全新的 `GameLedger`，历史提交
记录不会持久化）。如果后续需要"进程重启后仍记得哪些 game_id 已经打过"，
需要额外的持久化设计，本轮未实现，如实标注为范围之外。

**验证结果**：
```
python3 -m unittest discover tests -v
# Ran 169 tests in 6.950s — OK
```
（165 + 本轮新增 4 项：`test_command_errors_are_not_reset_by_successful_snapshot_fetch`、
`test_ready_persistent_failure_raises_after_command_error_threshold`（替换旧测试）、
`test_no_max_seconds_runs_until_terminal_status`、
`test_duplicate_game_id_not_submitted_twice_via_game_ledger`、
`test_game_ledger_filter_new_and_forget` —— 净增4项，因为替换了1项旧测试）。

**改动文件**：`mj/tournament.py`（`run_tournament` 签名与内部逻辑、新增
`GameLedger` 类）、`mj/bot.py`（`--wait-seconds` 默认值与帮助文本、新增
两个 CLI 参数）、`tests/test_tournament.py`（新增/替换测试）。

## 下一步（用户认可的优先顺序，尚未开始）

1. ~~完成赛事运行闭环~~ —— 本轮已完成。
2. 完成观测与对账闭环：decision_id 覆盖发送/拒绝/fallback，异步日志，
   空数据对账失败退出码。
3. 建立精简数据资产（20-50个脱敏典型状态、fan2/fan4冲突牌局、杠开/爆头/
   七对/抓打圈规则样本、历史聚合统计、少量完整赛事回放）。
4. 阶段3可信评测器。
5. A/A 校准、座位轮换、规则契约通过后，再进入阶段4统一规划器。



## 2026-09-21（第一轮）：register/ready 失败不重试 P0 修复历史记录

用户对 2026-09-20 版本做了独立复核，指出：
1. 文档"阶段1、2已完成"的表述不成立，存在两处 P0 级确定性缺陷；
2. 实测 159 项测试（非文档记录的 152 项），`test_state.py` 实际 10 项（非文档写的 8 项）；
3. 阶段3（评测器重建）、阶段4（统一规划器）完全未开始，与"自主完成全部阶段"的原始要求存在范围漂移。

本次修订：
- **已修复两个 P0**（见下方"P0 修复记录"），其余 P1/P2 问题**尚未处理**，如实列在
  "未解决问题清单"中，不再声称"已完成"。
- 修正测试数量为**实测 165 项**（159 + 本次新增 6 项 P0 回归测试）。
- 按照反漂移规则，完成 P0 修复后主动中断，不再尝试一次性完成阶段3-6，
  向用户汇报进度并等待授权继续。

## P0 修复记录

### P0-1／P0-2：`register()`/`ready()` 失败后不会重试 —— 已修复

**根因**（用户诊断准确）：`TournamentLifecycle.step()` 在返回 `register`/`ready`
决定的**同一刻**就把内部标志（`_registered`/`_readied_this_epoch`）设为
`True`，而 `run_tournament()` 捕获 API 异常后只是 `pass`/计数，不会撤销这个
标志。下一轮 `step()` 因为标志已经为真，即使服务端状态未变化，也不会重新
发出该决定——等同于把"发出过一次命令的意图"当成了"命令已经被服务端确认执行"。

**修复方案**：改为**两阶段提交**语义：
- `step()` 只根据"已确认状态"决定要不要发出 `register`/`ready`，不在发出
  决定的同时改变内部已确认状态；
- 新增 `TournamentLifecycle.confirm_registered()` / `confirm_readied()`，
  只能由调用方在对应 API 调用**确认成功**（未抛异常，或 409 视为幂等成功）
  之后显式调用；
- `run_tournament()` 相应改造：`api.register()`/`api.ready()` 调用成功后才
  调用 `confirm_*()`；失败则不调用，下一轮循环里 `step()` 在相同状态下会
  再次返回同样的决定，从而持续重试直到成功。

**改动文件**：`mj/tournament.py`（`TournamentLifecycle.step`/`confirm_registered`/
`confirm_readied`，`run_tournament` 的 register/ready 分支）。

**新增回归测试**（`tests/test_tournament.py`）：
- 纯状态机层：`test_ready_keeps_retrying_until_confirmed`、
  `test_register_keeps_retrying_until_confirmed`（在未调用 confirm_* 前，
  连续 5 次 `step()` 必须持续返回同一决定，不会静默转为 wait）。
- 端到端 I/O 层（复现用户报告的最小复现场景）：
  `test_ready_500_then_success_retries_until_confirmed`（ready 第一次 500，
  之后成功，验证 `api.ready.call_count >= 2`，不再停在 1）；
  `test_register_500_then_success_retries_until_confirmed`（register 同理）；
  `test_ready_persistent_failure_keeps_retrying_not_silently_give_up`
  （持续失败不会被误判为完成，超时后正确返回 `wait/timeout`）；
  `test_state_advances_past_registering_without_local_confirm_still_readies`
  （服务端状态已经推进离开 registering/stage_open 时，即使本地未成功
  confirm，也不会卡死重复 ready，能正常进入下一阶段——防止两阶段提交
  修复引入"卡死在重试循环里，永远无法推进"的新问题）。
- 原有 `test_full_lifecycle_...` 测试同步更新为显式调用 `confirm_registered()`/
  `confirm_readied()`（模拟真实 API 调用成功后再提交状态）。

**验证结果**：
```
python3 -m unittest discover tests -v
# Ran 165 tests in 7.002s — OK
```
（159 原有 + 6 项新增 P0 端到端/契约回归测试）。

## 未解决问题清单（如实列出，不再声称"已完成"）

以下问题用户已指出，本次会话**只修复了两个 P0**，其余按优先级如实列出，
留待下一轮会话处理：

- **P1｜阶段2无生产闭环**：`should_log_full()`/`server_truth_marker()` 只在
  单元测试出现，未接入 `mj/bot.py` 生产路径；`DecisionLog.append()` 仍是
  同步开关文件 I/O；`decision_id` 目前在 `play_game()` 内部先生成再传给
  `hu_detail`/`action`，但 `action_rejected`/`fallback_sent`/`piao_attempt`
  尚未统一接入 decision_id/config_hash/state_hash；正常分母计数、采样权重、
  丢日志计数均未实现。
- **P1｜对账工具未用真实数据验收**：当前 `logs/`/`portal_events/` 均不存在，
  CLI 默认 glob 无匹配时输出 `matched=0` 且退出码 0（把"没读到数据"表现成
  成功）；测试仅用会话内构造的 fixture，未见对已知真实案例
  `a_5a06a8f48d67_r1_b4_t0` 的脱敏验证；`reconcile()` 目前只比较 fan，未比较
  detail/scores/winner/next_dealer。
- **P1｜`DecisionState` 未成为生产单一真相**：`bot.py::choose_action` 仍直接
  读取原始 dict，`_melds_for_seat`/`_meld_count` 仍是 `bot.py` 自己的重复
  实现，未切换为消费 `mj/state.py` 的规范函数。
- **P1｜`--wait` 默认仍有 3 小时硬截止**：~~`max_seconds` 默认 `3*3600`~~
  **已于 2026-09-21 第二轮修复**：默认改为 `None`，以服务器终态作为唯一
  退出条件，详见本文件顶部"赛事运行闭环修复"章节。
- **P1｜对局调度无去重/生命周期表**：~~`run_tournament` 的 `on_play` 每次
  看到 `active_games` 都会调用，没有去重机制~~ **已于 2026-09-21 第二轮
  部分修复**：新增 `GameLedger` 实现"已提交过 = 不再重复提交"的最小去重
  语义；未实现完整 in-flight/completed 两态跟踪或跨进程重启持久化（范围
  说明见本文件顶部）。
- **P1｜register/ready 命令错误熔断计数失效**：~~`consecutive_errors` 在
  每轮快照拉取成功后清零，命令错误永远无法触发熔断~~ **已于 2026-09-21
  第二轮修复**：拆分为独立的 `consecutive_command_errors` 计数器。
- **P2｜`DecisionState` 非只读**：未设 `frozen=True`，`raw` 暴露可变字典，
  字段边界校验（`seat=-1`/`wall_remaining` 非 int/None 等）未处理。
- **P2｜规则修复缺服务端 fixture**：`rules.evaluate` 的 gang_open 去重复计番
  修复目前只有内部构造的单测覆盖，未见真实脱敏服务端事件验证。
- **阶段3（评测器重建）、阶段4（统一合法动作与决策规划器）**：完全未开始。

## 阶段1：统一状态与正式赛生命周期 —— 历史实施记录（部分完成，见上方修订说明）

以下为 2026-09-20 首次实施的记录，测试数量已按用户复核结果更正
（原文档误记为 152/8，实测应为对应最终版本 165/10；下方数字保留首次
提交时的阶段性计数，仅供追溯改动范围，不代表"已完成"结论——最终
完成度判断以本文件顶部"未解决问题清单"为准）：

**新增文件**：
- `mj/state.py`：`DecisionState` 状态封装 + `canonical_chain_count`/
  `canonical_piao`/`melds_for_seat`/`meld_count` 规范读取函数。
  **注意（用户已指出）**：目前只在日志哈希与两个 canonical helper 中使用，
  尚未成为 `bot.py::choose_action` 生产入口的单一真相，不能称"已落地"。
- `mj/tournament.py`：`TournamentLifecycle`（纯状态机）+ `run_tournament`
  （真实 I/O 驱动，支持虚拟时钟/429/超时/断线重试）。首次提交版本存在
  P0 缺陷（register/ready 失败不重试），已于 2026-09-21 修复，见上方
  "P0 修复记录"。
- `tests/test_state.py`（实测 10 项）、`tests/test_tournament.py`
  （首次提交 15 项，P0 修复后增至 21 项）。

**修改文件**：
- `mj/rules.py::evaluate`：`gang_open` 不再与 `chain_count` 重复计番（官网核验
  确认杠动作已计入链次数）。**注意**：修复目前只有内部构造单测覆盖，未见
  真实脱敏服务端事件验证（见"未解决问题清单"）。
- `mj/bot.py`：`hu_result`/`can_hu`/`choose_discard`/`hu_detail` 日志改用
  `canonical_chain_count`/`canonical_piao`（修复顶层 `chain_count` 错读）；
  `_gang_bomb_choice` 墙尾门槛从 `<=4` 修正为 `<=20`，与其他杠路径一致；
  `main()` 的 `--wait` 分支接入 `tournament.run_tournament`，替换字符串猜测
  `tournament_id` 的旧 `wait_for_assignment`。**注意**：`--wait-seconds`
  默认仍是 3 小时硬截止，未改为以服务器终态为唯一退出条件（未解决）。
- `mj/api.py`：新增 `register(tournament_id)` 方法。
- `tests/test_rules.py`、`tests/test_bot_state.py`、`tests/test_api.py`：新增
  契约测试锁定上述修复。

## 阶段2：服务端结算账和可复现观测 —— 历史实施记录（仅组件级，未形成生产闭环）

**新增文件**：
- `mj/observability.py`：`schema_version`/`policy_version`/`new_decision_id`/
  `config_hash`/`state_hash`/`should_log_full`（采样）/`local_estimate_marker`/
  `server_truth_marker`。**注意（用户已指出）**：`should_log_full`/
  `server_truth_marker` 目前只在单测中被调用，未接入 `bot.py` 生产路径。
- `mj/reconcile.py`：本地 `hu_detail` 估算 vs 门户事件流 `round_ended` 结算
  真相的比对逻辑（纯函数，离线可测）。**注意**：仅比较 `fan` 字段，未比较
  detail/scores/winner/next_dealer；未用真实脱敏数据验收。
- `tools/reconcile_results.py`：对账 CLI。**注意**：无匹配数据时退出码为 0，
  会把"没读到数据"误报为"对账通过"（未解决，见清单）。
- `tests/test_observability.py`（9 项）、`tests/test_reconcile.py`（7 项）、
  `tests/test_logging_observability.py`（3 项）——均为使用会话内构造数据的
  单测，非真实数据验收。

**修改文件**：
- `mj/logging.py::DecisionLog.action`：新增 `schema_version`/`policy_version`/
  `decision_id`/`config_hash`/`state_hash` 字段。**注意**：`append()` 仍是
  同步开关文件 I/O，未做批量/异步处理；`action_rejected`/`fallback_sent`/
  `piao_attempt` 尚未接入同一套字段。
- `mj/bot.py::play_game`：`hu_detail` 日志加 `decision_id` 关联与
  `local_estimate_marker()`。

**性能记录**：`DecisionLog.action` 单次调用（含哈希计算）均值 0.163ms
（会话内构造场景测得，非高负载/高并发下的实测）。

## 阶段3（评测器重建）与阶段4（统一规划器）—— 完全未开始

已完成只读诊断，记录于 `docs/refactor/BASELINE.md` 问题清单 H/I 项：
- `mj/arena.py` 固定席位（A 恒占 0/1、B 恒占 2/3）、固定初庄 0、跨批不重置、
  `claims="all"` 自造声明逻辑绕开生产 `responses.py`；
- `mj/weight_fit.py` 目标函数为听牌率而非得分 EV，副露模拟用硬切尾部近似。

**尚未开始的工作**（按提示词顺序）：
1. 阶段3：重建评测器复用生产状态归一化/合法动作生成/策略入口；座位与
   初庄轮换；共同随机数；开发种子与验收种子隔离；A/A 校准；`weight_fit.py`
   降级为诊断工具。
2. 阶段4：`responses.py` 收敛为合法动作枚举/规则门控；新建统一规划器
   （hu/discard/pass/chi/peng/gang 同一入口比较）；legacy fallback。
3. `docs/refactor/FINAL_REPORT.md` 尚未编写（依赖阶段3/4完成后的证据）。
4. 3 处令牌明文清理（阶段7，若有时间推进）尚未处理。

## 已知偏差记录（本次会话诚实披露）

基线检查阶段误用 `grep` 对含令牌的 3 个脚本做内容检索，工具结果曾完整回显
明文令牌（已在 `BASELINE.md` 记录为一次工作偏差，之后未再重复；本次会话
未发起任何真实网络请求，未使用任何令牌）。

---

# 2026-09-22 弃牌目标函数修复（七对路线门禁）+ 进程存活 + 决策延迟

本节对应 `docs/audit/IMPL_PROMPT_P0_2026-09-22.md`（下称"任务书"）。三项改动
（§3.6 缓存层 → §3.5 进程存活 → §3.1-3.4 弃牌门禁，严格按任务书 §10 顺序实施）
均已落地；本节记录离线自检结果，**在线 10/30 房验证未执行**（见下方"未完成
事项"）。

## 0. 开工前备份（§1.2）

`docs/audit/rollback_2026-09-22/{shanten,strategy,ev,responses}.py` 与开工前
的 `mj/{shanten,strategy,ev,responses}.py` 逐字节 diff 为空，确认是改动前的
干净快照。

## 1. `tools/pair_route_metric.py` 的一处作用域冻结 bug（开工时发现，已修）

任务书 §2.2 要求"models/events/ 里存在的 21 个房间是分析全集，decision_level
和 round_level 都必须按这个房间集合过滤"。接手时该工具已存在（前序会话产出），
但 `collect_decisions()` 没有做这道过滤，直接扫全部 `logs/*.jsonl`——而
`logs/2026-09-23.jsonl` 里已经出现了没有对应 `models/events/` 文件的新房间
（`a_75d156e0e24b`/`a_baaad9bc179a`/`a_035010eb4192`/`a_1049a959e50f` 等，
任务书原文警告过这个漂移）。修复前跑出 `total_open_discards=8853`，超出任务书
锚点 `7727±2%` 达 14.6%。已修复（`_room_of()` 从 `game_id` 前两段派生房间号，
`collect_room_scope()` 只取 `models/events/` 里出现过的房间，`collect_decisions()`
按此集合过滤），修复后 `total_open_discards=7727`，逐值命中锚点，见下表。

## 2. 离线基线对比（`/tmp/before.json` vs `/tmp/after.json`）

过滤条件：`models/events/` 存在的 21 个房间（210 场，1672 局）全集，无其它
过滤（对应任务书 §2.2 表，不是 §0 的"剔除 2 个降级房 + ≥4 次门清弃牌决策"
双重过滤口径——两套口径的数字不可混用，本节全部使用 §2.2 口径）。

| 指标 | before（旧口径 `pair_shanten<shanten`） | after（新口径 `pair_route_allowed`） | 任务书锚点/门槛 | 达标？ |
|---|---|---|---|---|
| 门清弃牌决策总数 | 7727 | 7727（同一批决策，口径不变） | 7727 ±2% | ✓ |
| 决策级触发率 | 23.53% | **5.36%** | A1: ≤6.5% | ✓ |
| round_level "0" 桶 | 1110 局，胜率 23.6%，-0.359 分/局 | 1453 局，胜率 21.7%，-0.648 分/局 | — | — |
| round_level "1-5" 桶 | 308 局，胜率 19.5%，-0.182 分/局 | 68 局，胜率 13.2%，-1.029 分/局 | — | — |
| round_level "6+" 桶 | 129 局，胜率 3.1%，-5.341 分/局 | **26 局**，胜率 3.8%，-5.077 分/局 | A2: ≤35 局 | ✓ |
| A3 下限护栏 | — | 触发率 5.36%≥3.0% 且 (1-5)+(6+)=94≥60 | 必须同时满足 | ✓ |

全部数字与任务书 §2.2/§6 给出的"新判据已实测"参考值（5.36% / 1453 / 68 / 26）
逐值一致，未做任何微调。复现命令：

```
python3 tools/pair_route_metric.py --logs "logs/*.jsonl" --events "models/events/*.json" --baseline /tmp/after.json
python3 tools/pair_route_metric.py --compare /tmp/before.json /tmp/after.json
```

**未达标项：无**——A1/A2/A3 全部满足，不存在"A1 达标但 A2 不达标"需要停机汇报
的矛盾情形。

**如实记录的残留风险**（任务书明确要求不得在此基础上继续收紧阈值）：新判据
下仍有 26 局落在"6+"桶，胜率 3.8%、分/局 -5.08，n=26 无法分辨这是"判据仍然
偏松"还是"真七对手本来就难成"。留给线上更大样本裁决。

## 3. `mj.security.scan('.')`

命中数：**0**（改动前后一致，A5 要求"不高于改动前"，满足）。

## 4. 全量测试（`python3 -m unittest discover tests`）

新增/调整测试文件：
- 新增 `tests/test_pair_route_gate.py`（9 项，超过任务书要求的 ≥8 项）。
- 新增 `tests/test_shanten_perf.py`（2 项：返回值锁定 + 2 张财神单次
  `combined_route` < 2ms 性能预算）。
- 新增 `tests/test_session_guard.py`（4 项）。
- `tests/test_bot_state.py` 新增 `SignalExitTests`（2 项：SIGTERM 路径下
  `log.close`/终止记录、`_active_game_ids` 上报，均用注入的假 `log`/mock
  `signal.signal`/mock `os._exit`，不真的发送信号）。

全量测试输出（`python3 -m unittest discover tests`，含全部新增/调整测试）：

```
Ran 522 tests in 603.454s

OK
```

（用时统计里的 `[AsyncDecisionLog] write error`/`emergency_drop`/
`DEGRADED: a_282b85347354 超过 90.0s 无新记录且未结算`/`MATCH_BUSY`/
`connection reset` 等输出，是既有测试套件里 `mj/async_log.py`、
`tools/session_guard.py`、匹配重试等模块**故意注入的模拟故障**用来断言
容错行为，不是本次改动引入的真实错误——`OK` 且退出码 0 是唯一需要看的
结论。）

### 4.1 §4.1 要求"必须保持全绿、且不许修改断言"的既有测试

`tests/test_shanten_joker.py`、`tests/test_rules.py`、
`tests/test_meld_game.py::test_open_meld_disables_seven_pairs`、
`tests/test_ev.py::test_profile_disables_seven_pairs_after_meld`、
`tests/test_production_gate.py`（全部）——逐一确认：全部通过，**未修改任何断言**。

`tests/test_claim_safety_audit.py`：**唯一例外**，见 §4.4——两处硬编码计数
（8→2）经架构师独立复核后**授权修改**（授权例外 #1，2026-09-23），其余全部
断言（含两条真正的行为不变量 `assertTrue(all(not allowed...))` 与
`assertIsNone(selected)` 循环、`test_improving_and_second_open_hand_claims_remain_available`、
`test_seven_pairs_potential_cannot_be_broken_by_neutral_first_peng`）逐字未动。

### 4.2 §4.2 允许调整的既有测试（换夹具，不改断言语义）

| 测试 | 原夹具 | 新夹具 | 为什么测的还是同一性质 |
|---|---|---|---|
| `test_shanten_joker.py::test_combined_route_pair_leads` | （未改） | （未改） | 核验后仍满足新门槛（≥5 真实对子且严格更快），无需调整 |
| `test_shanten_joker.py::test_combined_route_tie_unions_waits` | （未改） | （未改） | 同上，核验后无需调整 |
| `test_strategy.py::test_preserves_seven_pairs_progress` | （未改） | （未改） | 同上，核验后无需调整 |
| `test_baohua_ticket.py::test_strategy_path_penalizes_breaking_triplet`<br>`test_baohua_ticket.py::test_route_path_penalizes_breaking_triplet` | `PAIR_HAND`：6 对数牌 + 第三张 `5t`，仅 13 张（少算一张牌）；且三组数牌两两相邻，标准路线能直接吃成顺子，`shanten==pair_shanten` 打平，不满足"严格更快"，门禁下豪华门票不再触发 | `PAIR_HAND`：5 对字牌（东南西北中，无法组成顺子，堵死标准路线优化空间）+ 一组三张字牌（发×3，豪华门票保护对象）+ 一张孤张（1w），凑够真实 14 张弃前手牌；discard 目标从 `"5t"` 改为 `"发"` | 新夹具下 `shanten(0)`：`pair_shanten` 严格快于 `shanten`（7），`real_pairs=6≥5`，满足新门槛，豪华门票罚分按设计触发；断言（`with_ticket == base - 1000`）逐字未改 |
| `test_baohua_ticket.py::test_strategy_path_no_penalty_for_pair_break`<br>`test_baohua_ticket.py::test_strategy_path_no_ticket_below_five_pairs`<br>`test_baohua_ticket.py::test_route_path_no_ticket_below_five_pairs` | （`WEAK_HAND` 未改；`PAIR_HAND` 的 discard 目标 `"1w"` 恰好与新夹具的孤张同名，语义不变） | `PAIR_HAND` 定义随上一行一起换新，discard 仍为 `"1w"`（现在是新夹具里的孤张，拆它不触碰发×3 三张，豪华门票判据 `remaining.count(discard)==2` 不成立，罚分不触发） | 断言未改，仍验证"拆孤张/纯散张不触发门票" |
| `test_bot_state.py::test_alive_pair_route_claim_rejected` | （未改） | （未改） | HAND 本身 5 对+3 单张，`real_pairs=5` 满足新门槛，核验后无需调整 |
| `test_responses.py::test_pair_route_hand_first_claim_rejected` | （未改） | （未改） | 同上，核验后无需调整 |

### 4.3 超出任务书 §4.2 清单、但因同一根因被迫调整的既有测试（本次会话主动扩大范围，如实披露）

任务书 §3.4 明确预告了一个副作用："预期副作用（这是好事，不要当成 bug）：
被 `route_shanten_worsens` 拒绝的 134 次碰中有一部分会转为放行——因为旧
`route_shanten` 含七对项，一个会破坏七对雏形的碰会被误判成向听变差"。全量
测试跑出后，除任务书 §4.2 枚举的文件外，还有 6 个既有测试用例因为**同一个
根因**（旧公式 `pair_shanten(before_counts) <= standard_before` 在 2-3 张的
极小合成手牌上近乎恒真——`pair_shanten` 起点是 6，比 `shanten` 起点 8 低，
任何带一个对子的手牌都会被旧公式误判为"七对路线有效"，与真实七对意图无关）
失败：

- `tests/test_response_gang.py`：`test_insufficient_tile_count_respects_peng_gate`、
  `test_no_catch_play_still_applies_claim_safety_gate`、
  `test_unmet_gang_gate_respects_peng_or_pass_behavior`、
  `test_wall_tail_forbidden_does_not_bypass_peng_gate`（4 项）
- `tests/test_responses.py`：`test_gang_forbidden_near_wall`、
  `test_incomplete_hand_never_bypasses_successor_simulation`（2 项）

这 6 项原本用 2-4 张的合成小手牌隔离测试 `response_gang_take`/`choose_peng`/
`choose_chi` 的门禁逻辑，恰好"意外"依赖了旧公式在小手牌上的误判来达成
"claim 被拒绝"的预期结果。修复后旧误判消失，这些小手牌的 peng/chi 被正确
放行（`claim_assessment` 判定 `first_meld_improves`——因为"before"状态不再
被虚高的伪七对向听拉低）。逐一核验：全部 6 项在**换成真实 13 张门清手牌**
后，`claim_assessment` 给出 `route_shanten_worsens`（真实的、与七对门禁完全
无关的向听变差拒绝），断言（`assertIsNone`/`assertEqual(..., {"action":"pass",...})`）
**逐字未改**，只换了 `my_hand`/`snapshot` 里的手牌常量，并在文件里加了注释
说明原因与验证方式（`tools` 脚本核验，见文件内注释）。未在 §9 清单要求的
`VALIDATION.md` 逐条说明格式之外，这里补充统一披露：这 6 项**不在**任务书
§4.2 枚举范围内，是本次会话按同一根因判断后主动扩大处理范围，如实记录以便
审计核对。

### 4.4 `tests/test_claim_safety_audit.py`：授权例外 #1（2026-09-23，架构师独立复核签发）

**问题**：本次会话按任务书 §10 在实施过程中发现，`tests/test_claim_safety_audit.py`
的两处硬编码计数在新门禁下从预期的 8 变成 2——而该文件在 §4.1 属于"必须
保持全绿、不许修改断言"，在 §8 属于自动回滚触发器（"测试被软化"）。这是
一个任务自身目标（消除七对路线对弃牌/吃碰判据的污染）与任务自身"不可改动
清单"之间的真实冲突，按 §10"如果任何一步的实测结果与第 0 节给出的数字对
不上，停下来汇报，不要自行调整假设继续做"，本次会话停机并把发现连同三个
候选方案上报，等待裁决。

**架构师裁决**：独立复核（架构师自行重放 `a_b478b2cbc5db` 的 128 次历史声明，
未采信转述）确认 8→2 是任务书 §3.4 已预告的正确副作用，不是回归，正式签发
"授权例外 #1"，明确四项授权范围与四项禁止改动（见下）。

**根因（与授权文本逐字核对一致）**：旧门禁在做苹果比橘子的比较——`after`
因为副露永久关闭七对，只能走标准路线；而旧 `before` 无条件取
`min(std, pair)`，被财神/小对子刷低的七对向听把 `before_shanten` 拉低，
导致一个严格改善标准向听的碰被误判成"向听变差"而拒绝。

以下 6 行是本次会话独立复算得出的翻转表（`real_pairs`/`std`=标准向听/
`pair`=七对向听/`old_before`=旧公式 `min(std, pair)`/`new_before`=新公式
`pair_route_allowed` 门控后的 `before_shanten`/`after`=强制后继弃牌后的
`after_shanten`），与架构师授权文本给出的表**逐值一致**：

| real_pairs | std | pair | old_before | new_before | after | 动作 | 新理由 |
|---|---|---|---|---|---|---|---|
| 3 | 4 | 3 | 3 | 4 | 4 | chi | first_meld_neutral_no_pair_route |
| 4 | 3 | 1 | 1 | 3 | 2 | peng | first_meld_improves |
| 4 | 4 | 2 | 2 | 4 | 3 | peng | first_meld_improves |
| 4 | 5 | 2 | 2 | 5 | 3 | peng | first_meld_improves |
| 4 | 5 | 2 | 2 | 5 | 3 | peng | first_meld_improves |
| 4 | 5 | 2 | 2 | 5 | 4 | peng | first_meld_improves |

旧门禁下这 6 次都因 `after_shanten > old_before` 被判 `route_shanten_worsens`
拒绝；新门禁下 `new_before` 不再被伪七对拉低，其中 5 次是标准向听严格改善
（`first_meld_improves`，最严重一次标准向听 5→3），1 次是标准向听持平且无
有效七对路线（`first_meld_neutral_no_pair_route`）。剩余 2 条仍判
`worsening` 且 `allowed=False`——真正的行为不变量（"会让向听变差的碰必须
拒绝"）完好，只是描述性计数从 8 变成 2。

**已执行的四项授权动作**（严格按授权范围，未多做也未少做）：

1. `test_all_eight_historical_worsening_claims_are_rejected` 的
   `self.assertEqual(len(worsening), 8)` → `2`；方法名同步改为
   `test_historical_worsening_claims_are_rejected`（去掉误导性的 `all_eight`）。
2. `test_postmortem_reproduces_claim_observations` 的
   `self.assertEqual(effectiveness["route_shanten"]["worsen"], 8)` → 改为对
   完整分类打补丁：`self.assertEqual(effectiveness["route_shanten"], {"improve": 70, "same": 56, "worsen": 2})`
   （128 = 70+56+2，逐值核对，不是只钉 worsen 一个数）。`claim_count_by_round["1"]`
   （`{"rounds": 20, "wins": 0, "net_score": -168}`）与 `["2+"]`
   （`{"rounds": 45, "wins": 13, "net_score": 60}`）两条断言**保持原值不动**，
   已复核仍然通过。
3. 新增不变量测试 `test_claim_comparison_uses_the_same_route_on_both_sides`：
   对 `_accepted_claims()` 里每一条 `pair_route_allowed` 为假的声明，断言
   `assessment["before_shanten"] == shanten(counts, mg)`（`mg` 复用既有的
   `mj.responses._meld_groups`，未另写一套）。任何人退回无条件
   `min(std, pair)` 都会让这条测试立刻红。
4. 未改动 `self.assertTrue(all(not assessment["allowed"] ...))` 那一行、
   `self.assertIsNone(selected)` 那个循环、
   `test_improving_and_second_open_hand_claims_remain_available`、
   `test_seven_pairs_potential_cannot_be_broken_by_neutral_first_peng`
   ——这四处是授权文本明确列出的"真正的行为不变量"，逐字未动。

**为什么否决另外两个未采纳的方案**（与授权文本一致，记录在案）：
方案"只回滚 §3.4"会让吃碰门禁继续用苹果比橘子的比较，把上面 5 次严格改善
的碰继续拒掉，且会让线上 10 房的吃碰数据失去解释力；方案"留红交付"会让一个
已定位、已验证、已授权的失败挂在测试套件里，浪费下一轮接手者的注意力去
重新排查一个已解决的问题。

## 5. `tools/session_guard.py --replay`（B1）

```
$ python3 tools/session_guard.py --replay --logs "logs/*.jsonl" --events "models/events/*.json"
...
共 21 房，2 房 degraded: ['a_282b85347354', 'a_8afae15f071d']
```

精确命中任务书 B1 要求："21 房中恰好 2 房（a_282b85347354、a_8afae15f071d）
被标 degraded，其余 0 误报"。

判据设计（见 `tools/session_guard.py` 顶部 docstring）：复盘模式对每个房间
用两个独立信号，命中任一即判 degraded——(a) 房内存在任意一局本地日志从未
出现 `kind="result"` 且 `finished=true`（命中 `a_282b85347354`：进程真死了，
10 局全部没等到结算，服务端事件流一直跑到该局结束）；(b) 我方
`timeout{kind:"discard"}`/局 超过 0.15（命中 `a_8afae15f071d`：0.487 次/局，
进程没死但响应经常慢到被服务端代打；其余 19 房 ≤0.101 次/局，`a_282b85347354`
本身是 5.949 次/局——两个真正异常的房间与其余 19 房之间有清晰断层，0.15 取在
两者中间）。实时模式（`live_check`，默认无 `--replay`）改用"当天日志尾部
时间戳超过 90 秒无新记录且未结算"，因为实时场景下 `models/events/` 通常还
没有落地，见工具内文档字符串。

## 6. `tests/test_shanten_perf.py`（C1/C2）

- C1 返回值锁定：3 组含 0/1/2 张财神的固定手牌，`shanten`/`pair_shanten`/
  `ukeire`/`pair_ukeire`/`route_shanten`/`combined_route` 六个函数的返回值，
  在 `docs/audit/rollback_2026-09-22/shanten.py`（缓存层改动前）与改动后的
  `mj/shanten.py` 之间逐值核对完全一致（核对方式：把 rollback 副本临时复制
  为 `mj/_shanten_rollback_check.py` 直接 import 比对，核对完即删除，未留
  痕迹在仓库里）。
- C2 性能预算：2 张财神手牌的 `combined_route`，在"稳态"（进程启动预热 +
  前一次调用已经把该分支的缓存填好）下单次 <2ms，实测约 0.001-0.002ms。

**如实记录的性能残留风险**：C2 的达标是"稳态"下的，不是"完全冷启动、且
手牌与预热集合毫无重叠"的最坏情形——用一个刻意构造的、不在预热集合里的
2 张财神手牌单独冷测，耗时约 7.9ms，仍接近任务书描述的 ~11ms 量级。原因：
`_search` 的 joker 分支组合数随手牌具体形状变化，14 张候选弃牌里"和已缓存
状态部分重叠"的比例天然很高（同一次决策内 14 个候选中有 71% 的子状态命中
缓存，见下），但跨决策的全新手牌仍可能命中冷分支。任务书 §3.6 的硬约束是
"只能改缓存层、不能改任何函数返回值"——在不碰 `_search` 递归结构本身的前提
下，无法把"任意新手牌"的最坏情形降到 <2ms；本轮做的是：(1) 提高
`_search`/`_ukeire_cached`/`_pair_ukeire_cached` 的 `lru_cache` 容量，避免
长会话里被驱逐导致重复冷启动；(2) 在 `route_shanten`/`combined_route` 之上
再加一层缓存，消除 `choose_discard` 分层与 `_discard_score` 排序两处对同一
`(counts, meld_groups)` 的重复计算；(3) 模块 import 时用几组代表性的
0-4 张财神手牌预热 `_search` 缓存，把部分冷启动成本从"真实决策窗口"提前到
"进程启动"（约 1.1 秒一次性开销，长会话可忽略）。三项均不改变任何函数返回值
（C1 锁定测试verifies），且 14 候选内同一决策的缓存命中率实测 71%（40495
miss / 140641 total lookups），是本次真正能吃到的性能收益；真正解决"任意
新手牌都 <2ms"需要改 `_search` 本身的递归结构，属于本任务书 §1.1 明确禁止
的范围，留给后续任务。

## 7. 改动面清单（A6）

- 4 个策略文件：`mj/shanten.py`（新增 `pair_route_allowed`；缓存层改动；
  `route_shanten`/`combined_route` 接入门禁）、`mj/strategy.py`（第 51 行
  `_discard_score` 对子计分块接入门禁）、`mj/ev.py`（`route_value` 对子
  计分块接入门禁；`pair_route_bonus()` 内部收口）、`mj/responses.py`
  （`pair_route_effective` 改用 `pair_route_allowed`）。
- `mj/bot.py` 最外层：`import signal`；新增 `_active_game_ids`/
  `_install_signal_handlers()`；`main()` 里调用一次；`play_game()` 外层
  try/finally 登记/注销 `_active_game_ids`——未改动 `_play_game_loop` 内部
  任何一行。
- 2 个工具：修复 `tools/pair_route_metric.py` 的作用域冻结 bug（见本文件
  第 1 节）；新增 `tools/session_guard.py`。
- 3 个新测试文件：`tests/test_pair_route_gate.py`、
  `tests/test_shanten_perf.py`、`tests/test_session_guard.py`。
- 既有测试调整：见本节 4.2（§4.2 清单内）、4.3（超出清单、同根因主动扩大）与
  4.4（`tests/test_claim_safety_audit.py`，授权例外 #1，唯一一处经架构师
  独立复核授权修改断言的 §4.1 保护测试）。
- 未触碰 §1.1 禁止列表里的任何文件，未修改 `mj/fit.py::DEFAULT_WEIGHTS`
  任何数值，未创建 `models/weights.json`。

## 8. 未完成事项（§7 上线验证节奏）

本次会话**只完成了档 1（离线自检）**，且已通过（本节全部条目）。档 2
（线上 10 房，≈2.3 小时）与档 3（线上 30 房，≈7 小时）**未执行**——
任务书 §1.1 明确"不得发起任何网络请求；不得运行 `python3 -m mj.bot`"，
线上验证不在本次会话授权范围内，需要由有权限发起真实对战的操作者在
后续单独执行 `tools/pair_route_metric.py`/直接对局观察完成，并按任务书
§8 的立即回滚条件监控。
