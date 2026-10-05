# 架构决策记录

本文件记录关键架构决策、依据、考虑过的替代方案与回退方式。按阶段追加，不回改
历史条目（如需修正，新增条目并注明"修正 #N"）。

## 决策 #1：不引入 Git，改用文件级改动清单代替 commit/hunk 暂存

**背景**：提示词第三节要求"只暂存明确修改的文件或 hunks，不得把开工前已有变更
混入提交；无法安全区分时不要提交，改为输出 diff 清单"。但本仓库经核实**不是
Git 仓库**（`git status` 报 `fatal: not a git repository`），没有 `.git` 目录、
没有历史提交、没有"开工前已有的未提交修改"这个 Git 语义上的对象。

**决策**：不初始化新的 Git 仓库（避免造成"这是全新历史"的误导，也避免额外引入
版本控制系统的运维负担超出本次任务范围），改为：
1. 每个阶段结束后在 `VALIDATION.md` 中列出本阶段新增/修改的文件清单及理由；
2. 不执行任何会被误解为"覆盖工作区"的操作；
3. 若用户后续希望初始化 Git 仓库，可自行执行 `git init` 并以当前文件状态作为
   首个提交。

**替代方案考虑**：曾考虑直接 `git init` 后逐文件 `git add`，但这样会把"开工前
就存在的代码"和"本次新增/修改的代码"混在同一个初始提交里，达不到提示词要求
的"清晰区分 pre-existing / 本次新增变更"的目的，反而制造假象。故放弃。

## 决策 #2：DecisionState 作为规范状态的单一真相，但阶段1只做"读取口径修正"

**背景**：提示词要求"建立规范化 `DecisionState`……统一解析：座位、阶段、手牌、
副露、公开弃牌、庄家、墙余量、摸牌、响应窗口、有效规则、`god.chain_count`、
飘/抓打圈及赛事上下文"，同时又要求"本阶段不得改变牌策略。旧策略通过规范状态
适配运行"。

**决策**：新增 `mj/state.py`，提供：
- `DecisionState`（dataclass）：只读视图，字段命名与原始快照语义保持一致；
- `canonical_chain_count` / `canonical_piao`：优先读 `god.chain_count`/`god.piao`，
  仅当 `god` 字典存在但缺该键时才回退顶层字段（用于兼容测试夹具），两者皆缺失
  时返回 0；
- `melds_for_seat` / `meld_count`：从 `mj/bot.py` 原有的 `_melds_for_seat`/
  `_meld_count` 原样抽出，行为完全不变（只是有了统一的公开入口，供 `bot.py`
  和未来的 `tournament.py`/`planner.py` 共用，减少重复实现漂移的风险）。

`mj/bot.py` 的 `hu_result`/`can_hu`/`choose_discard` 三处调用点从直接读
`snapshot.get("chain_count", 0)` 改为调用 `canonical_chain_count(snapshot)`
（`piao` 同理）。**这是本阶段唯一改变的"读取路径"，不改变任何评分函数、
权重、吃碰门控**——`strategy.py`/`ev.py`/`responses.py` 全部未改动。

**为什么不是"整个 legacy 决策链路都改跑 DecisionState 对象"**：提示词要求
"旧策略通过规范状态适配运行"，允许"适配"而非"整体重写调用方式"。当前
`strategy.choose_discard`/`ev.choose_route_discard` 等函数签名是
`(tiles, meld_groups, chain_count, piao, rules, visible)` 的位置参数形式，
已经被 9 个模块直接调用（`bot.py`/`arena.py`/`gamesim.py`/`weight_fit.py`/
`samples.py`/`compare.py`/`special.py`/`ab.py`/多个 tools 脚本）。在阶段1
把这些调用点全部改造成传 `DecisionState` 对象，属于"下一阶段的大重构"，
违反"每阶段只修改本阶段需要的文件"的约束，且会显著放大本阶段的回归面。
阶段1选择的口径是：**`DecisionState` 已经存在并可用，`bot.py` 的读取入口
已经改为通过它的规范化函数取值；旧评分函数本体保持位置参数签名不变**。
阶段4（统一合法动作与决策规划器）会是收敛更多调用点到 `DecisionState`
的合适时机，因为那时会新建 `planner.py` 作为统一入口，可以在那一层内部
把 `DecisionState` 转成旧函数需要的位置参数，不需要旧函数本身改签名。

**回退方式**：`mj/state.py` 是新增文件，删除该文件并把 `bot.py` 三处调用点
改回 `snapshot.get("chain_count", 0)` 即可完全回退到修复前行为（不推荐，
因为这样会重新引入链字段错读 bug）。

## 决策 #3：修复 `rules.evaluate` 的链/杠开重复计番，而不是保留双重计番再在别处扣减

**背景**：官网规则核验已确认"杠动作已经计入链次数"。修复链字段错读 bug 之后
（决策 #2），`chain_count` 会开始读到真实非零值，而旧代码里 `gang_open=True`
时额外 `fan *= 2`，会与 `2 ** chain_count` 里已经包含的那一次杠贡献重复相乘，
导致杠开胡的番值被系统性地多算一倍。

**决策**：把 `gang_open=True` 分支从"再乘一次 2"改为"只追加 detail 标签，
不再参与番值计算"（`mj/rules.py:evaluate`）。这样修复后，`evaluate(...,
chain_count=1, gang_open=True)` 与 `evaluate(..., chain_count=1)` 在
`fan` 上完全相等，只是 `detail` 多一个"杠开"标签用于可观测性。

**替代方案考虑**：
1. 保留 `gang_open×2`，改为链计数不含杠动作贡献——被否决，因为这需要在
   `bot.py`/服务端交互层再造一套"链次数减去杠次数"的近似计算，容易产生新的
   偏差，且与官网核验的规范字段语义（`god.chain_count` 已包含杠动作）直接矛盾。
2. 在 `bot.py` 调用层扣减而不改 `rules.py`——被否决，因为 `rules.evaluate`
   是单一规则真相，任何两处独立扣减都会重新制造"两份平行评分体"式的维护
   风险（历史上豪华门票就是因为两处独立实现而"改一处漏一处"）。

**影响面**：`mj/gamesim.py:_evaluate_hand` 里也有一段绕过 `rules.evaluate`、
自行计算 `2 ** chain_count` 并追加"杠开"标签的兜底分支（当 `evaluate` 返回
`None` 但 `win_standard` 判定为真时触发）。这段代码本次未改动，因为它本身
不含 `gang_open×2` 的重复乘法（只在 `win_standard` 分支追加标签，不二次
乘番），不受本次修复影响，予以保留。

**回退方式**：`mj/rules.py:evaluate` 内 `gang_open` 分支恢复 `fan *= 2` 一行
即可回退（不推荐，因为这会重新导致 fan4→fan2 一类的对账偏差）。

## 决策 #4：新增 `mj/tournament.py`，状态机与真实 I/O 分离

**背景**：提示词要求"补齐正式赛 register/ready、多阶段 open/done、同阶段故障
重赛、active_games 暂空、候补、晋级、加赛和终态处理"，验收要求"录制形状或
假服务覆盖完整生命周期；使用虚拟时钟测试长等待；覆盖 429、超时、断线、
非法动作与恢复"。

**决策**：拆成两层：
1. `TournamentLifecycle`：纯状态机，`step(TournamentSnapshot) -> Decision`，
   不做任何网络 I/O、不依赖真实时钟，可以用手工构造的快照序列在毫秒级完成
   全部生命周期分支的单测（见 `tests/test_tournament.py` 的
   `LifecycleStateMachineTests`，覆盖 `registering→running→stage_done→
   stage_open→running→finished` 完整链路、同阶段号故障重赛重新 ready、
   `active_games` 为空不判定结束、`closed`/`void` 终态、未知状态保守等待）。
2. `run_tournament`：真实 I/O 循环，接收可注入的 `clock`/`sleep` 回调（用于
   虚拟时钟测试）、`on_play` 回调（用于接入真正的对局线程池），捕获
   `ApiError`/`OSError`/`TimeoutError` 并退避重试，连续错误超过阈值才放弃。

**关键设计取舍**：
- **不从 `game_id` 猜测 `tournament_id`**：`run_tournament` 优先使用
  `api.me()` 返回的 `tournament_id` 字段。这是提示词第五节"必须直接使用
  服务端 `tournament_id`，不能从 `game_id` 字符串猜测"的直接落实。
- **`active_games` 为空 ≠ 结束**：`TournamentLifecycle.step` 对
  `status == "running"` 且 `active_games` 为空的情况显式返回
  `Decision(action="wait", reason="running_no_active_games")`，只有
  `status` 落在 `{"finished", "closed", "void"}` 才判定为 `done`。
- **ready 幂等 + 重赛可重新确认**：通过 `_entered_stage_open` 检测"状态
  边沿"（上一次观测状态不是 `stage_open`，这一次是）来决定是否需要重新
  `ready`，而不是"只在第一次见到 `stage_open` 时 ready 一次"。这样同阶段号
  故障重赛导致状态从 `stage_open` 掉回 `running` 再回到 `stage_open` 时，
  会被判定为"重新进入"从而重新 `ready`（`test_same_stage_number_fault_
  restart_requires_reready` 覆盖此分支）。
- **超时不是 SystemExit**：旧 `wait_for_assignment` 超时返回 `None`，
  `main()` 里 `print("等待分房超时，退出"); break`。新 `run_tournament`
  超时返回 `Decision(action="wait", reason="timeout")`，调用方决定如何处理
  （目前 `bot.main()` 直接打印状态并返回，不抛异常，便于外部重试脚本判断
  退出码/日志而不需要捕获异常）。

**范围边界（本阶段未做的事）**：
- 候补/晋级（`qualified`/`qualify_role`）的判定逻辑本阶段只做"透传服务端
  字段到 `TournamentSnapshot`"，不做晋级排序计算——这是提示词阶段5"赛事
  价值层"的职责，阶段1不提前实现，避免在没有真实赛事数据校验的情况下
  臆造排序规则。
- 加赛（决赛同分自动出现新 `game_id`）在当前状态机里会被自然地当作
  "`running` 状态下 `active_games` 出现新条目"处理（`play` 决定会带上
  新的 `game_id` 列表），不需要专门的"加赛"分支——因为服务端本身就是通过
  `active_games` 暴露新对局，状态机的 `running`/`play` 路径已经能覆盖，
  没有找到需要特殊区分"加赛 game_id"与"正常 game_id"的官方依据。
- `main()` 里 `--wait` 分支目前用 `run_tournament` 的 `on_play` 回调直接
  提交线程池执行 `play_game`（沿用不变的 legacy 策略），**不改变一手牌
  内部的决策逻辑**。

**回退方式**：`mj/bot.py:main()` 的 `--wait` 分支可以整体替换回旧的
`wait_for_assignment` 循环（该函数已从 `bot.py` 中移除，如需回退需要从
版本历史或本次基线记录中恢复实现）；`mj/tournament.py` 是新增文件，
删除后只影响 `--wait` 参数路径，不影响 `--room`/自由匹配路径。

## 决策 #5：`api.py` 新增 `register()`，不改动其余方法

**背景**：HANDOFF.md 与独立技术评审报告一致确认 `api.py` 缺
`POST /api/tournaments/{id}/register`，导致正式赛报名必须人工在门户点击。

**决策**：新增 `MahjongApi.register(tournament_id)`，直接对应
`POST /api/tournaments/{tournament_id}/register`，请求体为空 JSON 对象
（与 `ready()` 的实现风格一致）。不改动 `me/ready/rules/tournament/match/
state/notify/action/version/test_events` 任何现有方法的行为。

**回退方式**：删除该方法即可，`run_tournament` 在 `register` 分支会因为
`AttributeError` 而在下一次循环被 `except (ApiError, OSError, TimeoutError)`
之外的异常打断——因此如果回退这个方法，必须同时回退 `tournament.py` 依赖
它的 `register` 分支（两者是同一决策的一部分，不能只回退一半）。

## 决策 #6：不在阶段1处理 CRLF 换行符统一

**背景**：BASELINE.md 已记录 `mj/ev.py`、`mj/shanten.py`、`mj/strategy.py`、
`mj/weight_fit.py` 为 CRLF，其余文件为 LF；HANDOFF.md 也提到这是"提交前
需要 dos2unix，否则 review 被整文件改写淹没"的已知债务。

**决策**：本阶段不做任何换行符统一处理，原因：
1. 本仓库不是 Git 仓库（决策 #1），没有历史基线可以用
   `git diff --ignore-all-space` 复核"哪些是纯换行符改动、哪些是实质改动"，
   强行统一换行符会让本次实际改动（少量行）淹没在整个文件被重写的噪声里，
   反而降低本次改动的可审查性；
2. 提示词第三节要求"不删除、不覆盖、不自动格式化与当前阶段无关的文件"——
   换行符统一属于与阶段1目标（状态归一化、生命周期状态机、已知协议缺陷）
   无关的格式化操作；
3. 本次对这四个文件的改动都是新增/替换整段代码（`bot.py` 的调用点、
   `rules.py` 的 `evaluate` 函数体），保存时会保留 `edit` 工具读取到的
   原始换行符风格，不会引入新的混合换行问题。

**后续处理建议**：留给阶段7（发布清理，若推进到该阶段）统一处理，届时如果
用户已经初始化了 Git 仓库，可以先打一个"仅换行符转换"的独立提交，与后续
实质性改动分开，便于 review。

## 决策 #7：`hu_detail` 日志同步改用规范字段，为阶段2服务端对账做准备

**背景**：提示词阶段2要求"服务端 round_ended 的 winner/fan/detail/scores/
next_dealer 是结算真相，本地规则计算只能标为 local_estimate"，需要先有
一致的本地字段口径才能做有意义的对账。

**决策**：`mj/bot.py:play_game` 里记录 `hu_detail` 日志时，`chain_count`/
`piao` 字段改用 `canonical_chain_count(snapshot)`/`canonical_piao(snapshot)`，
与 `hu_result`/`can_hu` 使用同一套规范读取，避免出现"决策用的是 god.chain_count，
落盘日志却还是顶层字段"的口径不一致（这正是独立技术评审报告里发现的
"fan4 vs fan2"根因之一：字段读取点不统一）。

## 决策 #8（2026-09-21 返修）：`TournamentLifecycle` 改为两阶段提交，修复用户复核发现的 P0 缺陷

**背景**：用户独立复核并给出最小复现实测，证实 `register()`/`ready()` API
调用失败后，状态机不会在下一轮重新发出对应决定——根因是 `step()` 在返回
决定的同一次调用里就把内部标志设为"已完成"，而不是等待外部确认。这是
两个可能导致正式赛因一次瞬时网络错误被判定为未报名/未确认出席而被剔除的
确定性缺陷，级别是 P0，此前文档"阶段1已完成"的表述是不成立的。

**决策**：引入两阶段提交（propose → confirm）模式：
- `step()` 只做"提议"：根据当前已确认的内部状态和外部快照，决定接下来
  应该发出什么命令，绝不在提议的同时修改自己的已确认状态；
- 新增两个显式确认方法 `confirm_registered()`/`confirm_readied()`，只应由
  `run_tournament()` 在对应 API 调用真正成功（未抛异常，或 409 视为幂等
  成功）之后调用；
- 如果 API 调用失败，`run_tournament()` 不调用 confirm 方法，下一轮循环
  `step()` 会在相同快照状态下重新提议同样的命令，形成自然重试，直到成功
  或外部把 `max_consecutive_errors`/`max_seconds` 熔断。

**为什么不是"在 run_tournament 里自己维护一个独立的重试计数器包一层"**：
被否决，因为那样会制造两份状态（`TournamentLifecycle` 内部的"是否已确认"
和 `run_tournament` 外部的"是否重试过"），两者不同步的风险和历史上"两份
平行评分体"的教训一样——真正的修复应该让"内部状态"本身就诚实反映"服务端
是否已经确认"，而不是在外面再打个补丁掩盖内部状态说谎的问题。

**新引入的回归风险与对策**：两阶段提交容易引入新 bug——"如果 confirm 因为
异常从未被调用，状态机会不会永远卡在重复发送同一个命令，即使服务端事实上
已经认可了"？用
`test_state_advances_past_registering_without_local_confirm_still_readies`
验证：只要服务端快照的 `status` 本身已经离开 `registering`/`stage_open`
进入下一阶段（如 `running`），状态机会正常识别为"该周期已经结束"（因为
`step()` 判断的是"当前 status 是否仍处于需要确认的状态"，不是"本地是否
调用过 confirm"），从而避免真正卡死。

**回退方式**：`confirm_registered()`/`confirm_readied()` 是新增方法，如需
完全回退到旧的（有缺陷的）语义，需要把 `step()` 内联恢复"发出决定即设置
标志"的写法，并同步删除 `run_tournament()` 里的两处 `confirm_*()` 调用
（不建议回退，会重新引入 P0 缺陷）。

## 决策 #9（2026-09-21 阶段A返修）：数据层从"骨架级原型"修复为"真正流式+统一脱敏+逐字段对账"

**背景**：独立评审报告 `SONNET5_DATA_REVIEW.md` 对上一轮数据层交付给出
6.5/10 评分，指出两个 P0（导入不是真正流式；脱敏实现会泄露令牌）与三个
P1（对账仍只比较 fan；稳定假 ID/盐机制未实现；聚合报告未达约定范围），
判定"不能按数据层完成验收"。任务书明确要求"阶段A未通过，不得跳到阶段B"。

**决策**：
1. 新增 `mj/sanitize.py` 作为唯一脱敏入口，`mj/data_import.py`/
   `tools/data_tool.py` 的所有对外输出（errors.summary/CLI 输出/
   fixture-export）统一改为调用它，不再各自实现脱敏逻辑。
2. 重写 `mj/data_import.py`：`iter_decision_log_lines()` 改为逐行 yield
   的生成器，`import_decision_log_stream()` 直接消费生成器写入 SQLite，
   按可配置 `batch_size`（默认2000）分批提交事务——不再有任何
   "整份文件先收集成列表再统一写库"的中间态。保留
   `parse_decision_log_lines`（现在是 `collect_decision_log_lines_for_testing`
   的别名）供小规模单测继续使用整体断言，明确注释说明生产路径
   （`tools/data_tool.py`）不经过这个别名。
3. `mj/datastore.py` schema 升级到 v2：`source_files` 新增
   `status`（`importing`/`complete`/`failed`）+ `finished_at`/
   `error_message`；`round_results.winner_seat` 用哨兵值 `-1` 代替
   `NULL`（修复 SQLite UNIQUE 约束对 NULL 不去重的问题）；
   `derive_decision_id` 签名改为接收 `source_sha256`/`line_no`/`kind`，
   天然避免同时间戳碰撞。
4. `mj/reconcile.py` 新增 `reconcile_fields()`/`summarize_fields()`：
   逐字段比较 winner/fan/detail/scores/next_dealer，detail 顺序不敏感
   但集合不同判 mismatch，本地天然缺失的 scores/next_dealer 标记
   `unavailable` 而不是假装 equal。旧的 `reconcile()`/`summarize()`
   保留不删（仍有测试覆盖，供历史兼容），但 CLI 改用新函数。
5. `tools/data_tool.py` 全面改写：`import-logs` 使用流式导入 API 并显式
   管理 source_files 状态机（导入前置 importing，成功置 complete，异常
   置 failed 并重新对外报告 `parse_failed`）；`summary` 补齐解析成功率/
   唯一去重计数/policy覆盖率/unknown_legacy分桶/server-local对账覆盖率/
   错误率/延迟p50-p95-p99/缺字段率，百分比均带分子分母；新增
   `--allow-empty`（默认空数据非零退出）；`anomalies` 输出经
   `sanitize()` 处理；`fixture-export` 缺盐默认失败，`--synthetic-source`
   /`--pseudonymize-ids` 提供豁免与假名化开关。

**验证证据**：
- 270 项测试全部通过（`python3 -m unittest discover tests`）。
- 峰值内存证据：100,000 行合成 decision（源文件 26.57 MiB），
  `tracemalloc` 实测峰值 <1 MiB（验收上限 20MiB），耗时约4秒
  （`tests/test_data_import_streaming.py::PeakMemoryStressTest`）。
- 三类真实泄露反例（UTF-8 replace 行内嵌令牌、`kind=error` 的
  `Authorization: Bearer` 文本、`action_rejected.payload.error` 中的
  token）均做过端到端 CLI 手工复现：导入后数据库/stdout/stderr 均不含
  原始 64 位令牌，`mj.security.scan()` 对导出目录和整个仓库均无命中
  （仓库既有的 `tools/{room_status,probe_tokens,server_latency_probe}.py`
  的令牌命中是历史已知问题，属于阶段7清理范围，本轮未处理，不属于
  本次数据层导出路径）。
- `reconcile_fields` 独立复现验证：fan 相同但 detail 不同时
  `must_compare_ok=False`，CLI 退出码从旧版误报的 0 变为 1。

**回退方式**：如需回退到阶段A之前的实现，`mj/sanitize.py` 是新增文件可
直接删除；`mj/data_import.py`/`mj/datastore.py`/`tools/data_tool.py` 需要
从本次改动前的版本恢复（不建议回退，会重新引入已确认的令牌泄露与内存
无界增长问题）。

## 决策 #10（2026-09-21 阶段B）：观测生产闭环——决策关联链、异步有界日志、减量采样

**背景**：阶段A返修通过后（决策#9证据），任务书要求接入观测/对账生产
闭环：decision_id 必须在决策产生时（调用 `api.action()` 之前）创建，
覆盖 action_sent/action_confirmed/action_rejected/fallback_sent/
piao_attempt/hu_local_estimate 全链路；日志写入改为有界队列+单写线程
异步批量追加，动作线程只做非阻塞 enqueue；正常 state 按 state_hash 稳定
采样，异常前后保留环形窗口。

**决策**：
1. `mj/bot.py:play_game` 里 decision_id 的生成时机从"API 调用成功之后"
   提前到"决定要发送这个动作之前"（`api.action()` 调用前），并通过
   局部变量贯穿传递给 `action_rejected`/`fallback_sent`/`piao_attempt`/
   `hu_detail`/最终的 `log.action(...)` 调用——同一次决策的所有日志条目
   现在共享同一个 decision_id，即使中途被拒绝/回退/超时。
2. 新增 `mj/async_log.py`：`AsyncDecisionLog` 包装现有 `DecisionLog`，
   内部用 `queue.Queue(maxsize=...)` + 单个后台写线程消费队列、批量
   `writelines()` 落盘。写线程异常不传播到调用线程（记录到内部
   `dropped_count`/`write_error_count`），`flush(timeout=...)` 提供
   可测试的排空语义。队列满时按优先级丢弃：`state`/`notify` 等高频低
   信息量 kind 可丢弃并计数；`action`/`action_rejected`/`fallback_sent`/
   `hu_detail`/`error` 等关键事件永不静默丢弃——队列满时改为阻塞短暂
   等待后仍满则记录"紧急丢弃"计数并写 stderr，不允许静默消失。
3. `mj/observability.py` 新增 `stable_sample_state()`：用
   `state_hash` 的哈希值决定采样与否（而不是 `random.random()`），保证
   同一逻辑状态在同一采样率下的采样结果是确定性的、可复现的。
4. 不在这一轮触碰 `mj/tournament.py`（阻断清单第1、2项按任务书要求只
   写入清单，不顺手修）。

**回退方式**：`mj/async_log.py` 是新增文件，删除后 `mj/bot.py` 可以改回
直接使用同步 `DecisionLog`（`log.action(...)` 签名不变，`AsyncDecisionLog`
是透明包装，回退只需要把 `main()` 里创建 log 对象的地方从
`AsyncDecisionLog(DecisionLog())` 改回 `DecisionLog()`）。

依据 anti-regression 规则（"执行包含多个强依赖阶段、大量文件修改和验证
步骤的超长复杂工程任务……iteration 消耗过半但仍有后续阶段未完成时，必须
主动中断当前执行，输出已完成部分的总结与下一步计划，请求用户授权继续"）：

本次会话只处理用户复核报告中的两个 P0（`register`/`ready` 失败不重试），
不在同一轮会话内尝试补齐 P1（阶段2生产闭环、对账真实数据验收、
`DecisionState` 单一真相、`--wait` 硬截止、对局去重表）、P2（`DecisionState`
只读性、规则修复的服务端 fixture）以及阶段3/4的全部内容。原因：
1. 这些问题各自需要独立的设计取舍和较大改动面（例如"阶段2生产闭环"涉及
   给 `bot.py` 主循环接入采样判断，需要仔细验证不引入动作线程阻塞回归；
   "对账真实数据验收"需要用户提供或授权访问真实脱敏牌谱）；
2. 一次性处理会重复本次会话已经暴露的问题模式——写了大量新代码/测试但
   缺乏真实数据/生产路径验证，声称"完成"实际是"骨架级"；
3. 遵循用户裁定的判断："可以继续，但必须先返修阶段1-2"——本次先完成
   两个确定性最强、复现证据最充分的 P0，再向用户汇报，由用户决定下一步
   优先修哪些 P1，避免在没有反馈的情况下继续放大范围漂移。

## 决策 #11（2026-09-22）：七对路线弃牌门禁——门槛取"≥5 个真实对子 且 严格更快"

**背景**：独立架构审计（`docs/audit/IMPL_PROMPT_P0_2026-09-22.md`）依据 21 房
210 场 1672 局真实自由对战日志确诊：`mj/shanten.py::route_shanten()` 在
`meld_groups == 0` 时用 `min(标准向听, 七对向听)` 做弃牌分层，导致七对路线在
"持平甚至仅微弱领先"时就接管门清弃牌分层。实测：一局中七对路线领跑 ≥6 手的
121 局，胜率 1.7%、分/局 -5.63；从未走七对路线的 589 局胜率 25.1%、分/局
-0.05（与随机基线持平）。收益端：七对只值 fan 2，343 次胡牌里七对仅 13 次
（3.8%），与全场对手持平——付出速度代价换不回超额完成率。

**决策**：新增 `mj.shanten.pair_route_allowed(counts, meld_groups)`，把"七对
路线是否参与门清弃牌分层/评分"收紧为：`meld_groups == 0` 且手上已有 **≥5 个
真实对子**（财神不计入，`JOKER_IDX` 显式排除）且 `pair_shanten(counts) <
shanten(counts, meld_groups)`（**严格更快**，持平不算）。接入 4 个文件的
7 个调用点（`mj/shanten.py` 的 `route_shanten`/`combined_route` 内部缓存层，
`mj/strategy.py::_discard_score` 的对子相关计分块，`mj/ev.py::route_value` 的
对应计分块与 `pair_route_bonus()` 内部收口，`mj/responses.py::claim_assessment`
的 `pair_route_effective` 判据），不改 `mj/fit.py::DEFAULT_WEIGHTS` 任何数值——
只改这套权重的启用条件，不改权重取值。

**门槛数值怎么定的**（历史日志实测，见 `tools/pair_route_metric.py`）：

| 判据 | 决策级触发率 | round_level_new（0 / 1-5 / 6+ 局数） |
|---|---|---|
| 旧：`pair_shanten < shanten`（无对子数门槛） | 23.53% | 1110 / 308 / 129 |
| `real_pairs>=4 且严格更快` | （未采用，测试证明 4 对时旧公式在小样本上仍可能误触发，见下） | — |
| **`real_pairs>=5 且严格更快`（采用）** | **5.36%** | **1453 / 68 / 26** |
| `real_pairs>=6 且严格更快`（过紧对照） | 0.71% | 1533 / 11 / 3 |

`real_pairs>=6` 会把"1-5"+"6+"两桶压到只剩 14 局，跌破 §6 A3 下限护栏
（决策级触发率 ≥3.0% 且 "1-5"+"6+" 合计 ≥60 局）——说明会把七对路线整个
砍死，不只是砍掉滥用的部分。`real_pairs>=5` 同时满足 A1（≤6.5%）、A2
（"6+" 桶 ≤35 局）与 A3 下限护栏，是精确卡在"够窄到消除滥用、又不至于
把真七对手也砍死"这个区间内的唯一取值。

**明确否决的替代方案**：

1. **直接删掉七对路线（`pair_route_allowed` 恒为 `False`）**。否决理由：
   收益端数据显示七对本身不是零收益（343 次胡牌里 13 次是七对，3.8%，
   与全场对手持平）——问题是"滥用"不是"存在本身"，真正≥5对的手仍应该
   享受七对加速。删掉整条路线是用"typeI 错误"换"typeII 错误"，且违反
   `docs/audit` 反复强调的"只改启用条件、不改权重取值"的最小改动原则。
2. **只调 `mj/fit.py::DEFAULT_WEIGHTS` 里 `pair_route`/`b_pair`/`baohua_ticket`
   等权重数值，不改启用条件**。否决理由：审计已经定位病灶是"门槛"（何时
   进入七对分层）而不是"力度"（进入之后给多少分）——调低权重只会让七对
   路线在"持平"时touch的次数不变、只是每次代价小一点，1672 局回放显示
   问题集中在**决策次数**（一局里七对路线领跑 ≥6 手的 121 局全部亏损），
   不是**单次强度**；本任务书 §1.1 也明确禁止改动 `DEFAULT_WEIGHTS` 任何
   数值。
3. **把门槛（"≥5 对"这个数字）做成 `weights` 里的可配置项**（例如
   `pair_route_min_real_pairs`）。否决理由：`DEFAULT_WEIGHTS` 目前只装
   "计分强度"参数，混入一个"离散级门槛/整数计数阈值"参数会让 `weight_fit.py`
   之类的自动调参工具在错误的维度上搜索（该维度不是连续可优化的分数系数，
   而是需要靠历史分桶统计确定的结构性门槛）；且任务书 §1.1 明确本轮只改
   "这套权重在什么条件下生效"，不新增可调参数面。门槛作为纯代码常量（`5`）
   写在 `pair_route_allowed()` 里，连同其推导依据（上表）一起留痕在本决策
   记录里，比藏进权重配置更透明、更不容易被后续自动调参悄悄改掉。

**已知残留风险（如实记录，不在本轮继续调）**：新判据下仍有 26 局落在
"6+" 桶，胜率 3.8%、分/局 -5.08（n=26，95% CI 宽到无法与"真七对手本来就难
成"这个原假设区分）。禁止在 n=26 上继续收紧阈值——那是对着噪声过拟合。
留给线上更大样本（`docs/audit` 建议至少 15 房，本次改动只做到"档1 离线
自检"，未做线上验证，见 `VALIDATION.md` 本节）裁决。

**回退方式**：
1. 用 `docs/audit/rollback_2026-09-22/{shanten,strategy,ev,responses}.py`
   覆盖回 `mj/` 对应文件（§3.1-3.4 的全部改动，含 §3.6 缓存层改动，因为
   缓存层改动与门禁改动在同一批 `mj/shanten.py` 编辑里，一并回退不影响
   正确性——缓存层本身不改变任何返回值，回退与否都不影响结果，只是为了
   干净起见连同回退）。
2. 删除 `tests/test_pair_route_gate.py`、`tests/test_shanten_perf.py`。
3. 还原 `tests/test_response_gang.py`、`tests/test_responses.py`、
   `tests/test_baohua_ticket.py` 中本轮调整过的夹具（`git` 不可用，本仓库
   靠本文档 + `docs/audit/rollback_2026-09-22/` 做回滚依据；这三个测试文件
   的调整是"换夹具不改断言"，若回退了 `mj/` 的门禁实现，这些新夹具在旧
   实现下的断言可能不再成立，需要连同测试改动一起回退，或参照本文档
   "允许调整的既有测试"一节里记录的原夹具重新替换回去）。
4. `mj/bot.py` 的 §3.5 信号处理、`tools/session_guard.py`、
   `tools/pair_route_metric.py` 的作用域冻结修复是独立的、无行为风险的
   可观测性/测量修复，与门禁实现无强耦合，回退门禁时可以保留不动。
5. 若连同回退 `tests/test_claim_safety_audit.py` 的授权修改（见 决策 #12），
   把该文件的两处计数改回 8、删掉 `test_claim_comparison_uses_the_same_route_on_both_sides`、
   方法名改回 `test_all_eight_historical_worsening_claims_are_rejected`。

## 决策 #12（2026-09-23）：`tests/test_claim_safety_audit.py` 硬编码计数 8→2——授权例外 #1

**背景**：决策 #11 落地后，`tests/test_claim_safety_audit.py` 出现 2 处失败：
`test_all_eight_historical_worsening_claims_are_rejected` 期望 `len(worsening) == 8`，
实测 2；`test_postmortem_reproduces_claim_observations` 期望
`effectiveness["route_shanten"]["worsen"] == 8`，实测 2。该文件在任务书 §4.1
属于"必须保持全绿、不许修改断言"清单，在 §8 属于自动回滚触发器（"测试被
软化：任何既有断言被删除、弱化或 skip"）——本次会话按 §10"结果与预期对不上，
停下来汇报，不要自行调整假设继续做"，停机并把根因、复算证据与三个候选方案
（改计数、留红交付、只回滚 §3.4）上报，等待裁决。

**架构师裁决（授权例外 #1，独立复核，非转述采信）**：确认这是决策 #11 §3.4
小节已预告的正确副作用（"预期副作用：被 route_shanten_worsens 拒绝的 134 次
碰中有一部分会转为放行"的具体实例），不是回归；架构师自行重放
`a_b478b2cbc5db` 的 128 次历史声明复核，逐值确认下表，正式授权修改，并要求
同时加固一条不变量测试作为对价。

**根因**：旧门禁比较是"苹果比橘子"——`after`（碰后）因副露永久关闭七对，
只能走标准路线；`before`（碰前）却无条件取 `min(std, pair)`，被小对子/财神
刷低的七对向听把 `before_shanten` 拉低，导致标准向听严格改善的碰被误判为
"向听变差"而拒绝。6 条历史声明因此被误拒（`VALIDATION.md` §4.4 有完整
逐值表），其中 5 条实际是 `first_meld_improves`（最严重一次标准向听 5→3），
1 条是 `first_meld_neutral_no_pair_route`。

**授权范围（四项，逐项落地，未多做未少做）**：
1. `test_all_eight_historical_worsening_claims_are_rejected` 的
   `assertEqual(len(worsening), 8)` → `2`；方法名同步改为
   `test_historical_worsening_claims_are_rejected`。
2. `test_postmortem_reproduces_claim_observations` 的
   `assertEqual(effectiveness["route_shanten"]["worsen"], 8)` → 补丁为完整
   分类 `{"improve": 70, "same": 56, "worsen": 2}`（128 条历史声明逐值核对，
   不是只钉一个数）；`claim_count_by_round["1"]`/`["2+"]` 两条不动，已复核
   仍通过。
3. 新增不变量测试 `test_claim_comparison_uses_the_same_route_on_both_sides`：
   对每条 `pair_route_allowed` 为假的历史声明，断言
   `before_shanten == shanten(counts, meld_groups)`（复用既有的
   `mj.responses._meld_groups`），锁死"两侧口径必须一致"这条不变量——
   任何人退回无条件 `min(std, pair)` 都会让它立刻红。
4. 明确不动：`assertTrue(all(not assessment["allowed"] ...))`、
   `assertIsNone(selected)` 循环、
   `test_improving_and_second_open_hand_claims_remain_available`、
   `test_seven_pairs_potential_cannot_be_broken_by_neutral_first_peng`——
   这四处是真正的行为不变量，8→2 只是描述性统计，定义变了数字就变，
   不构成"软化"。

**明确否决的替代方案**（架构师裁决文本原文）：
1. **只回滚 §3.4（`mj/responses.py`）**：会让吃碰门禁继续用苹果比橘子的
   比较，把上面 5 次严格改善的碰继续拒掉；半修比不修更难查，且会让线上
   10 房的吃碰数据失去解释力（下一轮"吃碰质量"诊断建立在这批数据上）。
2. **留红交付**：一个已定位、已验证、已授权的失败挂在测试套件里，下一轮
   任何人接手都会先去查它，浪费一整轮注意力。

**回退方式**：见 决策 #11 回退步骤第 5 条。
