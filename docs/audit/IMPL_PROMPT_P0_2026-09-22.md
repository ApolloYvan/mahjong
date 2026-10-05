# 实施 Prompt（交给 Sonnet 5 / Gemini 3.8 Flash 逐步执行）

> 本文件由独立首席架构师签发。依据是 `docs/audit/LOSS_AUDIT_2026-09-22.md`。
> **你必须先完整读完那份报告的第 3、8、9、11 节再动手。**
> 三个阶段 **必须串行**。阶段 A 的机制指标不达标，**不准进入阶段 B**；阶段 B 未上线满 15 房，**不准进入阶段 C**。

---

## 阶段 A（P0）：收敛门清弃牌目标函数 —— 七对路线降级为"严格优势才走"

### A.0 先建测量，再改代码（强制顺序）

**第一个交付物不是代码修改，是一个只读的度量工具。** 在它跑出"改造前基线"之前不许碰 `mj/` 下任何文件。

**新增文件（唯一允许新增的）**：`tools/pair_route_metric.py`

要求：
- 输入：`--logs "logs/*.jsonl"`，可选 `--events "models/events/*.json"`（用于关联该局是否胡牌、该局净分）。
- 只读取 `kind == "decision"` 且 `payload.decision.action == "discard"` 且该座位副露组数为 0 的记录。
- 对每条记录，取 `hand` 去掉 `decision.tile` 后的 13 张，计算：
  - `std = mj.shanten.shanten(counts, 0)`
  - `pair = mj.shanten.pair_shanten(counts)`
  - `pair_leads = pair < std`
  - `pair_allowed_new = <阶段 A.1 定义的新判据>`
- 输出 JSON：
  1. `decision_level`：门清弃牌决策总数、`pair_leads` 占比、`pair_allowed_new` 占比；
  2. `round_level`：按 `(game_id, round_no)` 聚合，统计"本局 `pair_leads` 手数"落在 `0 / 1-5 / 6+` 三桶的局数、胜率、分/局（胜负与分数来自 events 的 `round_ended`，按 `seats[].user_id == u_fd06550b5fb3` 定位座位，**不得用物理座位号**）；
  3. 同样再输出一份用 `pair_allowed_new` 重算的 `round_level_new`。
- 必须支持 `--baseline out.json` / `--compare old.json new.json`，打印逐桶差值。
- **不得**发起任何网络请求；**不得**写入 `models/` 或 `logs/`，输出默认写 `/tmp`。

**必须先跑出并记录的基线（架构师已复核过的期望值，用于校验你的工具是否写对）**：

| 指标 | 期望值（允许 ±1 个百分点/±3 局的偏差） |
|---|---|
| 门清弃牌决策 `pair_leads` 占比 | **≈ 20.8%** |
| round_level `0` 桶 | ≈ 1063 局，胜率 ≈ 23.5%，分/局 ≈ -0.36 |
| round_level `1-5` 桶 | ≈ 298 局 |
| round_level `6+` 桶 | **≈ 122 局，胜率 ≈ 1.6%，分/局 ≈ -5.59** |

**如果你的工具跑不出上面这组数，说明工具写错了，停下来修工具，不要继续。** 这是本阶段唯一的"事实对齐锚点"。

### A.1 要修改的文件与改动目标

**允许修改的文件，只有这四个：**

1. `mj/shanten.py`
   - 新增纯函数 `pair_route_allowed(counts, meld_groups=0) -> bool`，语义：
     ```
     meld_groups != 0                      -> False（副露永久关闭七对，现有语义不变）
     real_pairs(counts) < 5                -> False   # real_pairs = 除"白"外 count>=2 的牌种数
     pair_shanten(counts) >= shanten(counts, 0)  -> False   # 必须严格更快
     否则                                   -> True
     ```
   - 修改 `route_shanten(counts, meld_groups=0)`：把 `return min(std, pair_shanten(counts))` 改为
     `return min(std, pair_shanten(counts)) if pair_route_allowed(counts, meld_groups) else std`。
   - 修改 `combined_route(counts, meld_groups=0)`：其内部 `pair = pair_shanten(counts)` 的分支必须与 `route_shanten` 口径一致 —— `pair_route_allowed` 为 False 时，**不得**返回 `pair_ukeire`，也**不得**把 pair 的进张并入 `std_waits`。
   - **禁止**改动 `shanten()`、`_search()`、`_pair_score()`、`pair_shanten()`、`ukeire()`、`pair_ukeire()` 的任何计算逻辑。它们的正确性已由 `tests/test_shanten_joker.py` 与真实结算对账（291/291 逐值一致）锁定。

2. `mj/strategy.py`
   - `_discard_score` 里 `if meld_groups == 0:` 整块（`pair_count × b_pair`、`b_progress`、`baohua_ticket`、`pair_route_bonus`）改为 `if meld_groups == 0 and pair_route_allowed(counts, meld_groups):`。
   - **不得**改动 `b_shanten` / `b_ukeire` / `b_honor` / 财神相关的任何项，**不得**改动 `choose_discard` 的分层-排序两段式结构。

3. `mj/ev.py`
   - `route_value` 里 `if meld_groups == 0:` 整块（`pairs × pair`、`seven_pairs_route`、`baohua_ticket`、`pair_route_bonus`）同样加 `pair_route_allowed` 门。
   - `pair_route_bonus()` 自身加同一道门（它被两处调用，必须在函数内一次性收口，不要在两个调用点各补一次）。
   - **不得**改动 `chain` / `piao` / `must_kaoxiang` / `discard_joker_soft` 的任何项。

4. `mj/responses.py`
   - `claim_assessment` 里 `pair_route_effective = pair_shanten(before_counts) <= standard_before` 改为 `pair_route_allowed(before_counts, 0)`。
   - **这是唯一允许改动的门禁行。** 其余所有门禁分支（`route_shanten_worsens`、`non_worsening_open_hand`、`first_meld_improves`、`first_meld_neutral_no_pair_route`）的判据与返回值一律不动。

### A.2 禁止触碰的范围（硬边界）

以下任何一项被修改，本次改造直接判失败并回滚：

- `mj/bot.py` 的主循环、seq 跟踪、409 恢复、SSE、watchdog、response window key、任何调度或限速逻辑
- `mj/api.py`、`mj/tournament.py`、`mj/timing.py`、`mj/logging.py`、`mj/async_log.py`
- `mj/rules.py`（胡牌与番型判定 —— 审计已证明 291/291 与服务端逐值一致，动它只会制造回归）
- `mj/hu_strategy.py`（生产触发次数为 0，改它等于没改）
- `mj/defense.py`（喂牌率已优于中位）
- `mj/arena.py`、`mj/weight_fit.py`、`tools/baohua_experiment.py`、`tools/slow1_experiment.py`（审计已裁定其对本问题无判别力）
- `models/weights.json`（当前不存在，走 `DEFAULT_WEIGHTS`）与 `mj/fit.py` 里的任何权重数值 —— **本阶段一个权重数字都不许调**。本次改的是"这套权重在什么条件下生效"，不是"权重取多少"。
- 不得新增任何第三方依赖；不得发起任何网络请求；不得运行 `python3 -m mj.bot`。

### A.3 必须新增的回归测试

新增 `tests/test_pair_route_gate.py`，至少覆盖：

1. `test_five_real_pairs_and_strictly_faster_allows_pair_route`：5 对 + 3 单张的门清手，`pair_shanten < shanten` → `pair_route_allowed` 为 True，`route_shanten` 仍返回 pair 值。
2. `test_four_pairs_blocks_pair_route_even_if_faster`：4 对的手即使 `pair_shanten < shanten` 也返回 False，`route_shanten == shanten(...)`。
3. `test_tie_blocks_pair_route`：`pair_shanten == shanten` 时返回 False（"持平不走七对"是本次改造的核心）。
4. `test_joker_not_counted_as_real_pair`：手里 2 张白 + 4 对 → `real_pairs == 4` → False。（白是万能牌，不能让它把 4 对刷成 5 对去解锁七对路线。）
5. `test_open_hand_never_allows_pair_route`：`meld_groups >= 1` 恒 False。
6. `test_combined_route_does_not_leak_pair_ukeire_when_blocked`：被挡住时 `combined_route` 返回的进张集合与 `ukeire(counts, mg)` 完全相等，不含任何只有七对路线才有的进张。
7. `test_discard_score_pair_terms_inactive_when_blocked`：构造一个 4 对的门清手，断言 `baohua_ticket` / `pair_route_bonus` 不再影响 `_discard_score` 的相对排序（用两次调用的差值断言，不要断言绝对分值）。
8. `test_strategy_and_ev_agree_on_gate`：同一手牌，`strategy._discard_score` 与 `ev.route_value` 对 `pair_route_allowed` 的判定必须一致（防止两套评分体再次漂移 —— 这是本项目历史上重复出现过的故障模式）。

**必须保持通过、且不许修改断言的既有锁定测试**（如果它们红了，说明你改过头了，回来改实现而不是改测试）：

```
tests/test_shanten_joker.py   （全部，尤其 test_pair_shanten_* / test_pair_ukeire_mate_of_single）
tests/test_rules.py           （全部，尤其 test_seven_pairs / test_seven_pairs_with_four_jokers）
tests/test_meld_game.py::test_open_meld_disables_seven_pairs
tests/test_ev.py::test_profile_disables_seven_pairs_after_meld
tests/test_claim_safety_audit.py （全部）
tests/test_responses.py       （全部）
tests/test_bot_state.py       （全部）
tests/test_production_gate.py （全部）
```

**允许调整、但必须逐条写明理由的既有测试**（这些测试锁定的正是被本次改造有意改变的行为）：
```
tests/test_shanten_joker.py::test_combined_route_pair_leads
tests/test_shanten_joker.py::test_combined_route_tie_unions_waits
tests/test_strategy.py::test_preserves_seven_pairs_progress
tests/test_baohua_ticket.py（4 项）
tests/test_bot_state.py::test_alive_pair_route_claim_rejected
tests/test_responses.py::test_pair_route_hand_first_claim_rejected
```
调整方式**只能是**"把夹具改成满足新判据（≥5 对且严格更快）的手牌，保持原断言语义不变"。
**严禁**把断言改成 `assertNotEqual`、删除断言、或加 `skip`。每一处调整都要在 `docs/refactor/VALIDATION.md` 里逐条列出：原夹具、新夹具、为什么新夹具仍然测的是同一个性质。

### A.4 历史日志验证方法（不发网络请求）

改完后重新跑 `tools/pair_route_metric.py`，产出 `after.json`，与 A.0 的 `before.json` 对比。

```bash
python3 tools/pair_route_metric.py --logs "logs/*.jsonl" --events "models/events/*.json" --baseline /tmp/after.json
python3 tools/pair_route_metric.py --compare /tmp/before.json /tmp/after.json
```

同时跑全量测试与安全扫描：
```bash
python3 -m unittest discover tests
python3 -c "from mj.security import scan; print(scan('.'))"
```

### A.5 阶段 A 验收指标（离线，全部必须满足）

| # | 指标 | 门槛 |
|---|---|---|
| A1 | 门清弃牌决策中 `pair_route_allowed` 为 True 的占比 | 从 ≈20.8% 降到 **< 5%** |
| A2 | `round_level_new` 中 "6+" 桶的局数 | 从 ≈122 降到 **< 30**（即 <2% 的局） |
| A3 | `round_level_new` 中 "0" 桶的局数 | **不得低于 1250 局**（防止把七对砍成 0，需要保留真七对） |
| A4 | 全量测试 | 全绿，且新增测试 ≥8 项 |
| A5 | `mj.security.scan('.')` | 命中数不高于改动前 |
| A6 | 被修改文件数 | **恰好 4 个 `mj/` 文件 + 新增 1 个工具 + 新增 1 个测试文件 + 调整既有测试**，不多不少 |

**A2 达不到 → 判据太松，收紧 `real_pairs` 门槛到 6 再测。A3 跌破 → 判据太紧，退回 5 并重新检查 `real_pairs` 的白板处理。**

---

## 阶段 B（P1）：在线存活与强制弃牌风暴护栏

> 阶段 A 全部验收通过后才开始。

### B.1 要修改的文件

- `mj/bot.py`：**只允许**在 `main()` 与 `play_game()` 的**外层**加监控与退出信号，不得修改 `_play_game_loop` 内部的任何决策/调度语句。
- 新增 `tools/session_guard.py`：一个独立的看门脚本，**不嵌入 bot 进程**。

### B.2 改动目标

1. **强制弃牌风暴自检**：`play_game` 结束时（或每 N 秒）统计本对局中自身 `decision` 提交数与服务端推进的轮次数之比；比值异常（例如连续 60 秒 state 在推进但本进程 0 次 `decision`）→ 写一条 `kind="anomaly"` 的日志并把该 `game_id` 标记为 degraded。
2. **进程存活**：`tools/session_guard.py` 外部监控 `logs/<date>.jsonl` 的 mtime 与尾部时间戳；超过 90 秒无新记录而房间未结束 → 打印明确告警并返回非零退出码。**本阶段只做检测与告警，不做自动重启**（自动重启涉及令牌与房间状态，属于另一个决策，不在本轮授权范围）。
3. `main()` 在收到 `SIGTERM`/`SIGINT` 时必须走 `log.close(timeout=10.0)` 并写一条 `kind="result"` 或 `kind="error"` 的终止记录 —— 当前 2026-09-22 16:04 的进程死亡**没有留下任何终止痕迹**，这本身就是必须修掉的可观测性缺陷。

### B.3 禁止触碰

- 不得改动任何策略文件（`strategy.py` / `ev.py` / `responses.py` / `shanten.py` / `rules.py` / `hu_strategy.py`）
- 不得改动 state/action 限速、seq 跟踪、409 恢复
- 不得实现自动重连/自动重新 `/api/match`

### B.4 必须新增的回归测试

`tests/test_session_guard.py`：
1. 用假日志文件（时间戳可注入）验证"90 秒静默 + 房间未结束"判定为 degraded，返回非零码；
2. 验证"日志持续更新"判定为健康，返回 0；
3. 验证"房间已结束（存在 `kind=result` 且 `finished=true`）"即使静默也判定为健康；
4. `tests/test_bot_state.py` 新增：SIGTERM 路径下 `log.close` 被调用且写入终止记录（用注入的假 log 对象断言，不要真的发信号）。

### B.5 历史日志验证方法

对 `logs/2026-09-23.jsonl` 跑 `tools/session_guard.py --replay`，必须**准确地**把 `a_282b85347354` 标记为 degraded（真值：16:04:15 后静默，房间到 16:14 才结束），并且**不得**把其余 20 个房误报为 degraded。

### B.6 阶段 B 验收指标

| # | 指标 | 门槛 |
|---|---|---|
| B1 | 历史回放误报 | 21 房中 **恰好 2 房**（`a_282b85347354`、`a_8afae15f071d`）被标 degraded，其余 0 误报 |
| B2 | 后续真实对战 | 连续 10 房内，`timeout{kind:"discard"}` 每房 **≤ 10 次**（历史正常房 0–8 次） |
| B3 | 终止可观测性 | 任何非正常退出都在日志里留下终止记录 |

---

## 阶段 C（P2）：响应窗口延迟预算

> 阶段 B 上线并稳定 10 房后才开始。**如果阶段 A/B 之后胜率已经达标，可以不做。**

- 目标：把 `combined_route` 在含财神手牌上的 11 ms（无财神 0.009 ms，**1200 倍**）压下来，使门清弃牌的 `client_prepare_ms` p99 从 206 ms 降到 **< 60 ms**。
- 允许修改：`mj/shanten.py` 的**缓存层**（`lru_cache` 容量、预热、把 `_search` 的 joker 分支记忆化），**不得改变任何返回值**。
- 必须新增 `tests/test_shanten_perf.py`：对 3 组含 0/1/2 张白的固定手牌断言返回值与改动前完全一致（用硬编码的期望值锁定），并断言 2 白手牌的 `combined_route` 单次耗时 < 2 ms。
- 验收：`tests/test_shanten_joker.py` 全绿且返回值零变化；真实对战 5 房后，合法碰窗口的超时占比从 **14.5% 降到 < 6%**。

---

## 全局回滚条件（任一触发，立即整段回滚到本次改造前）

1. **规则回归**：真实对战中出现任何新的 `cannot gang` / `chi only in chi window` / `peng only in peng window` 类 409，或本地 `hu_detail` 与服务端 `round_ended` 的 winner/fan/detail 出现任何一条不一致（当前基线是 291/291 逐值一致，**容忍度为 0**）。
2. **有副露局变差**：有副露（≥1 组）局的胜率相对基线（1 组 22.7% / 2 组 24.7% / 3+ 组 26.2%）下降 ≥3 个百分点 —— 说明门禁改动泄漏到了副露路径。
3. **门清局没改善**：连续 15 房后，门清局胜率仍 ≤ 14%（基线 12.0%）—— 说明七对假设被证伪，按审计报告 §11 转向"听牌后转化率"调查。
4. **总体反向**：连续 15 房后总体胜率 ≤ 21.5% 且 分/局 ≤ -0.8。
5. **测试被软化**：发现任何既有断言被删除、改成弱断言或加了 skip。
6. **范围越界**：发现修改了 A.2 / B.3 列出的任何禁止文件。

**回滚操作**（本仓库无 Git，必须靠文件级清单）：改动前把 `mj/shanten.py`、`mj/strategy.py`、`mj/ev.py`、`mj/responses.py` 四个文件原样复制到 `docs/audit/rollback_2026-09-22/`，回滚即覆盖回去，并删除新增的 `pair_route_allowed` 相关测试文件。**这一步必须在写第一行代码之前完成。**

---

## 成功 / 失败的最终判定（照抄审计报告 §11，不得自行放宽）

**成功**（全部满足）：
1. 离线：`pair_leads`/`pair_route_allowed` 占比 <5%，"6+" 桶 <30 局，"0" 桶 ≥1250 局；
2. 真实对战 15 房（1200 局）后：总体胜率 **≥23.0%**、分/局 **≥-0.3**；
3. 旧口径"6+"子群胜率从 **1.7% 升到 ≥12%**；
4. 房间级垫底率从 **47.6% 降到 ≤30%**。

**假设被否定**（触发回滚 + 转向）：
- 离线指标达标但 15 房后胜率 ≤21.5% 且分/局 ≤-0.8。

**中性、不足以判定**：15 房后胜率落在 21.5%–23.0%。
→ **不得宣称成功**，继续采样到 32 房（2560 局）再读。样本量依据见审计报告 §9：
每局得分 sd=12.24，单侧 α=0.05 / power=0.8 下，+0.6 分/局需要 2572 局 ≈ 32 房；+0.8 分/局需要 1447 局 ≈ 18 房。**15 房只有能力检出 ≥+0.7 分/局的效应，这一点必须在任何结论里如实写明。**

---

## 交付清单（三份，缺一不可）

1. 代码改动 + 新增/调整的测试
2. `/tmp/before.json` 与 `/tmp/after.json` 的逐桶对比表，贴进 `docs/refactor/VALIDATION.md`
3. 在 `docs/refactor/DECISIONS.md` 新增一条决策记录，必须包含：为什么是"≥5 对且严格更快"而不是别的阈值、考虑过并否决了哪些替代方案（例如"直接删掉七对路线"、"只调权重不改门"）、以及精确的回滚步骤。
