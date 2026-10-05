# 任务：杭州麻将 AI 单轮修复（弃牌目标函数 + 进程存活 + 决策延迟）

**本任务一次性完成三项改动，做完直接上线验证，不分阶段。**
三项里**只有第 1 项会改变打法**，第 2、3 项在设计上不可能让任何一次决策变差
（一个是纯检测、一个是纯缓存，返回值零变化）——所以合并不破坏归因：
**如果线上结果变差，责任必然在第 1 项。**

你是本仓库的实施工程师。本任务由独立架构审计签发，依据是 21 个真实自由对战房、
210 场、1672 局的完整事件流与 418 MB 决策日志。**下面给出的所有数字都是实测值，
不是估计**。你不需要重新调查病因，你的工作是**按既定设计实施 + 用历史日志自证**。

仓库：`/Users/yuanye/coding_workspace/mahjong`（Python 3.9+，标准库，无第三方依赖，
**不是 Git 仓库**）。本 AI 的 user_id 是 `u_fd06550b5fb3`。

---

## 0. 病因（已确诊，不要再论证，也不要推翻）

线上胜率 **346/1672 = 20.69%**，95%CI [18.75%, 22.64%]，对 25% 基线 **z = -4.35
(p<1e-4)**。总分 -1398（-0.836/局）。**不是样本量问题。**

其它维度全部正常甚至偏好，逐条都已证伪：
- 规则引擎正确：146 个对局中本地 `hu_detail` 291 次 vs 服务端胜局 291 次，
  fan 分布 211/77/3 **逐值完全一致**；13,947 次摸牌决策中"能胡没胡" = 0。
- 409 不是主因：1,175 次拒绝里 89% 是 `pass` 被拒（代价为 0），且集中在两个最早的房。
- 弃牌无风险：本规则**只有自摸**，弃牌不可能点炮；喂牌率 12.56%，15 人中第 4 低。
- 分数转化效率正常：实际 -0.836 分/局，比按胜率做的横截面回归预测的 -1.27 **还高 0.43**。
- 副露是在帮我们：门清局胜率 12.0%，1/2/3+ 副露局分别是 22.7%/24.7%/26.2%。

**唯一断裂点**：`mj/shanten.py::route_shanten()` 在 `meld_groups == 0` 时用
`min(标准向听, 七对向听)` 做弃牌分层，导致七对路线劫持门清局的弃牌选择。

实测后果（剔除两个已知降级房、要求该局至少 4 次门清弃牌决策）：

| 该局门清阶段"七对路线领跑"的手数 | 局数 | 胜率 | 分/局 |
|---|---|---|---|
| 0（从未） | 589 | **25.1%** | **-0.05** ← 与随机基线持平 |
| 1–5 | 205 | 20.5% | +0.79 |
| **6+** | **121** | **1.7%** | **-5.63** ← 全部亏损在这里 |

控制起手向听后效应不变（各桶起手向听分布几乎相同）：
起手 ≤3 向听的"6+"局共 **70 局 0 胜**（基线 20–39%，p < 1.6e-8）。

收益端不存在：七对只值 fan 2，而我们 343 次胡牌里七对只占 13 次（3.8%），**与全场对手持平**。
即：付出了速度代价，没换来任何超额完成率。

代价的微观证据（各抽样 250 次弃牌决策，与"纯标准路线速度最优"对照）：

| 场景 | 与纯速度最优不同 | **标准向听严格更差** |
|---|---|---|
| 门清（七对逻辑开着） | 47.2% | **6.0%（15/250）** |
| 有副露（七对逻辑关着） | 22.4% | **0.0%（0/250）** |

另有 3000 次抽样显示：**98.5% 的弃牌是"生产自身目标函数"下的最优解**。
→ **搜索与打分实现是对的，错的是目标函数的启用条件。本任务只改启用条件，不改任何权重数值。**

---

## 1. 硬性约束（违反任何一条，本次改造判失败）

### 1.1 绝对禁止
- 不得发起任何网络请求；不得运行 `python3 -m mj.bot`；不得读写任何令牌。
- 不得新增第三方依赖。
- **不得调整任何权重数值**（`mj/fit.py::DEFAULT_WEIGHTS` 里一个数字都不许动，
  `models/weights.json` 当前不存在，不许创建）。本次改的是"这套权重在什么条件下
  生效"，不是"权重取多少"。
- `mj/bot.py` **受限允许**：只能在 `main()` 与 `play_game()` 的**最外层**加信号处理与
  终止记录（见 §3.5）。**不得**修改 `_play_game_loop` 内部任何决策、调度、seq 跟踪、
  409 恢复、限速语句。
- 不得触碰下列文件：
  `mj/api.py`、`mj/rules.py`、`mj/hu_strategy.py`、`mj/defense.py`、
  `mj/tournament.py`、`mj/timing.py`、`mj/logging.py`、`mj/async_log.py`、
  `mj/arena.py`、`mj/weight_fit.py`、`mj/piao.py`、
  `tools/baohua_experiment.py`、`tools/slow1_experiment.py`。
  （理由：审计已逐条证明这些不是病因；`arena` 是同策略自博弈，对本缺陷
  **结构性地无判别力** —— 四家跑同一套弃牌函数，被同等拖慢，净分差恒为 0。）
- 不得修改 `mj/shanten.py` 中 `shanten()` / `_search()` / `_pair_score()` /
  `pair_shanten()` / `ukeire()` / `pair_ukeire()` / `_ukeire_cached()` /
  `_pair_ukeire_cached()` 的**任何计算逻辑**。它们的正确性已被 291/291 的服务端
  结算对账锁定。
- 不得修改 `mj/ev.py::hand_profile()`（它是诊断用的只读画像，不参与决策）。

### 1.2 改动前必须先做的事（第 0 步，写代码之前）
把这四个文件原样复制到 `docs/audit/rollback_2026-09-22/`：
```
mj/shanten.py  mj/strategy.py  mj/ev.py  mj/responses.py
```
本仓库没有 Git，**这是唯一的回滚手段**。没做这一步就开始改代码 = 任务失败。

---

## 2. 第 1 步：先建测量工具，跑出基线（**在此之前不许碰 `mj/` 任何文件**）

新增 `tools/pair_route_metric.py`（只读工具，输出默认写 `/tmp`，
**不得写入 `models/` 或 `logs/`**）。

### 2.1 它要做什么
1. 扫 `logs/*.jsonl`，取 `kind == "decision"` 且 `payload.decision.action == "discard"`
   且该座位副露组数为 0 的记录（副露组数 = `payload.melds[payload.seat]` 的长度；
   `melds` 是 4 座位列表）。
2. 对每条：把 `payload.hand` 去掉 `payload.decision.tile` 得到 13 张，算
   - `std = mj.shanten.shanten(counts, 0)`
   - `pair = mj.shanten.pair_shanten(counts)`
   - `pair_leads = pair < std`（**旧口径**）
   - `pair_allowed_new = mj.shanten.pair_route_allowed(counts, 0)`（**新口径**，
     第 2 步实现之前先返回 `pair_leads` 占位，保证工具能先跑出基线）
3. 扫 `models/events/*.json`，按 `(game_id, round_no)` 取 `round_ended` 的
   `winner` 与 `scores`。**座位必须通过每个文件的 `seats[].user_id` 定位
   `u_fd06550b5fb3`，不得用物理座位号**（同一玩家在同房不同场坐不同座位，
   按座位号汇总会把四个人的数据混在一起——这是本项目的历史 bug）。
4. 输出 JSON：
   - `decision_level`: 门清弃牌决策总数、`pair_leads` 占比、`pair_allowed_new` 占比
   - `round_level`: 按"本局 `pair_leads` 手数"分 `0 / 1-5 / 6+` 三桶，
     每桶输出局数、胜率、分/局
   - `round_level_new`: 同上，但用 `pair_allowed_new` 重算
5. CLI：`--logs`、`--events`、`--baseline <out.json>`、`--compare <old.json> <new.json>`。

### 2.2 事实对齐锚点（v2，2026-09-22 18:10 修订 —— v1 有误，见文末勘误）

**先做作用域冻结**：`models/events/` 里存在的 **21 个房间**就是本次分析的全集。
`decision_level` 和 `round_level` **都必须**按这个房间集合过滤。
原因：bot 仍在持续采样，`logs/2026-09-23.jsonl` 每分钟都在增长，日志里已经出现了
`a_75d156e0e24b` / `a_baaad9bc179a` / `a_035010eb4192` / `a_1049a959e50f` 等
**没有对应 events 的新房**。不按 events 房间集合过滤，`decision_level` 会随时间漂移，
锚点永远对不上。

| 指标 | 期望值 | 容差 |
|---|---|---|
| 冻结作用域 | 21 房 / 210 场 / 1672 局 | 精确 |
| 门清弃牌决策总数 | **7727** | ±2% |
| 其中 `pair_leads`（`pair_shanten < shanten`）占比 | **23.5%** | ±1.5pp |
| round_level `0` 桶 | **1110 局**，胜率 **0.236**，分/局 **-0.359** | 局数 ±5，比率 ±0.01 |
| round_level `1-5` 桶 | **308 局**，胜率 **0.195**，分/局 **-0.182** | 同上 |
| round_level `6+` 桶 | **129 局**，胜率 **0.031**，分/局 **-5.341** | 同上 |
| 三桶合计 | **1547 局** | ±5 |

（合计 1547 < 1672 是正常的：差额是那些"整局没有任何门清弃牌决策"的局，
以及日志未覆盖到的局。）

**新判据实施后的期望值**（架构师已实测，供你自查实现是否等价）：

| 判据 | 决策级触发率 | round_level_new `0` / `1-5` / `6+` |
|---|---|---|
| 旧：`pair < std` | 23.53% | 1110 / 308 / 129 |
| **新：`real_pairs>=5` 且 `pair < std`** | **5.36%** | **1453 / 68 / 26** |
| 过紧对照：`real_pairs>=6` 且 `pair < std` | 0.71% | 1533 / 11 / 3 ← **过紧，不要用** |

**你的实现如果跑不出 5.36% / 1453 / 68 / 26 这组数，说明 `pair_route_allowed` 写错了**
（最可能的错：`real_pairs` 把白板算进去了，或用了 `<=` 而不是 `<`）。

跑通后存为 `/tmp/before.json`。

---

## 3. 第 2 步：实施改动（**只允许改这 4 个文件**）

### 3.1 `mj/shanten.py`

新增纯函数（放在 `route_shanten` 之前）：

```python
def pair_route_allowed(counts, meld_groups=0):
    """七对路线是否有资格参与弃牌分层。

    2026-09-22 实战审计：旧口径 route_shanten = min(std, pair) 会让七对路线在
    "持平"甚至"仅微弱领先"时就接管门清弃牌分层。真实 1672 局数据显示，一局中
    七对路线领跑 >=6 手的 121 局，胜率 1.7%、分/局 -5.63；而从未走七对路线的
    589 局胜率 25.1%、分/局 -0.05（与随机基线持平）。收益端：七对只值 fan 2，
    我们 343 次胡牌中七对仅 13 次（3.8%），与全场对手持平——付出速度代价却无
    超额完成率。故把门槛提高为"手上已经有真实的七对雏形，且七对路线严格更快"。

    回滚：把本函数改成 `return meld_groups == 0` 即完全恢复旧行为。
    """
    if meld_groups:
        return False            # 副露永久关闭七对（既有语义，不变）
    real_pairs = sum(1 for i, n in enumerate(counts)
                     if n >= 2 and i != JOKER_IDX)   # 白是万能牌，不算真实对子
    if real_pairs < 5:
        return False
    return pair_shanten(counts) < shanten(counts, meld_groups)   # 必须严格更快
```

修改 `route_shanten`（第 136–141 行）：
```python
    if meld_groups:
        return std
    if not pair_route_allowed(counts, meld_groups):
        return std
    return min(std, pair_shanten(counts))
```

修改 `combined_route`（第 144–160 行）：在 `if meld_groups:` 分支之后加一道
同口径的门 —— `pair_route_allowed` 为 False 时必须 `return current,
ukeire(counts, meld_groups)`，**不得返回 `pair_ukeire`，也不得把七对进张并入
`std_waits`**。两个函数的口径必须完全一致，否则会出现"分层说走标准路线、
进张却按七对算"的错位。

### 3.2 `mj/strategy.py`
第 51 行 `if meld_groups == 0:` 改为
`if meld_groups == 0 and pair_route_allowed(counts, meld_groups):`。
该块内的 `pair_count × b_pair`、`b_progress`、`baohua_ticket`、`pair_route_bonus`
随之一起被门控。
**不得**改动 `b_shanten` / `b_ukeire` / `b_honor` / 财神相关项，
**不得**改动 `choose_discard` 的"先按 route_shanten 分层、再按 _discard_score 排序"
两段式结构。

### 3.3 `mj/ev.py`
- 第 69 行 `if meld_groups == 0:`（在 `route_value` 内）加同一道门。
- `pair_route_bonus()`（第 26–31 行）在函数内部加同一道门（它有两个调用点，
  **必须在函数里一次性收口**，不要在两个调用点各补一次——本项目历史上
  "两份平行评分体"造成过改一处漏一处的故障）。
- **不得**改动 `chain` / `piao` / `must_kaoxiang` / `discard_joker_soft`。

### 3.4 `mj/responses.py`
第 163 行：
```python
    pair_route_effective = pair_shanten(before_counts) <= standard_before
```
改为
```python
    pair_route_effective = pair_route_allowed(before_counts, 0)
```
**这是本文件唯一允许改动的行。** 其余门禁分支（`route_shanten_worsens`、
`non_worsening_open_hand`、`first_meld_improves`、
`first_meld_neutral_no_pair_route`）的判据与返回值一律不动。

> 预期副作用（这是好事，不要当成 bug）：被 `route_shanten_worsens` 拒绝的
> 134 次碰中有一部分会转为放行——因为旧 `route_shanten` 含七对项，一个"会破坏
> 七对雏形"的碰会被误判成"向听变差"。

### 3.5 进程存活与终止可观测性（**不改打法**）

**问题实据**：房间 `a_282b85347354` 的决策日志在 `2026-09-22T16:04:15` 戛然而止 ——
没有 `result`、没有 `error`、没有任何终止记录，而服务端事件流一直跑到 16:14。
该房后 5.5/8 局由服务端代打（429 次 `timeout{kind:"discard"}`，5.50 次/局；
其余 19 房 ≤0.10 次/局）。该房 -287 分，是全样本最差的一房，相对我们自己的
平均 -66.6 分，**单次进程死亡直接损失约 -220 分（占总亏损 16%）**。

改动一（`mj/bot.py` 最外层）：注册 `SIGTERM`/`SIGINT` 处理，退出前必须
`log.close(timeout=10.0)` 并写一条 `kind="error"`、`payload.reason="signal_exit"`
的终止记录。**当前任何非正常退出都不留痕迹，这本身就是必须修掉的可观测性缺陷。**

改动二（新增 `tools/session_guard.py`，**独立脚本，不嵌入 bot 进程**）：
监控 `logs/<date>.jsonl` 的尾部时间戳；**超过 90 秒无新记录且该房未出现
`kind="result"` 且 `finished=true`** → 打印告警并返回非零退出码。
**本轮只做检测与告警，不做自动重启**（自动重启涉及令牌与房间状态，是另一个
决策，不在本次授权范围内）。

### 3.6 决策延迟预算（**不改打法，返回值必须零变化**）

**问题实据**：`combined_route` 在含 2 张财神的手牌上单次耗时 **≈11 ms**，
无财神时 **≈0.009 ms**（**1200 倍**）。一次门清弃牌要对 14 张候选各算一次，
10 场并发在 GIL 下串行。实测 `client_prepare_ms`：0 财神 p99=124ms；
1 财神 p99=228ms；**2 财神 p99=339ms、max 827ms**。
后果：48,875 次响应窗口里 6,909 次（14.1%）超时；其中
**手上确有对子、碰为合法的窗口 1,235 次，179 次（14.5%）因超时丢失**；
**手上确有 3 张、明杠合法的窗口 13 次，全部丢失**。

允许改动：**只能改 `mj/shanten.py` 的缓存层** —— `lru_cache` 容量、
`_search` 的 joker 分支记忆化、进程启动时的预热。
**严禁改变任何函数的返回值。** 这是硬约束：本项改动的全部价值在于
"同样的决策，更快地做出来"。

---

## 4. 第 3 步：必须新增的回归测试

新增 `tests/test_pair_route_gate.py`，至少 8 项：

1. `test_five_real_pairs_and_strictly_faster_allows_pair_route`
   —— 5 对 + 3 单张的门清手且 `pair_shanten < shanten` → True，`route_shanten` 仍返回 pair 值。
2. `test_four_pairs_blocks_pair_route_even_if_faster`
   —— 4 对的手即使七对更快也 False，`route_shanten == shanten(counts, 0)`。
3. `test_tie_blocks_pair_route`
   —— `pair_shanten == shanten` 时 False。**"持平不走七对"是本次改造的核心，必须有独立测试。**
4. `test_joker_not_counted_as_real_pair`
   —— 2 张白 + 4 对 → `real_pairs == 4` → False。（不能让万能牌把 4 对刷成 5 对解锁七对路线。）
5. `test_open_hand_never_allows_pair_route` —— `meld_groups >= 1` 恒 False。
6. `test_combined_route_does_not_leak_pair_ukeire_when_blocked`
   —— 被挡住时 `combined_route` 的进张集合与 `ukeire(counts, mg)` **完全相等**。
7. `test_discard_score_pair_terms_inactive_when_blocked`
   —— 4 对的门清手里，`baohua_ticket` / `pair_route_bonus` 不再影响
   `_discard_score` 的相对排序（**用两次调用的差值断言，不要断言绝对分值**）。
8. `test_strategy_and_ev_agree_on_gate`
   —— 同一手牌，`strategy._discard_score` 与 `ev.route_value` 对
   `pair_route_allowed` 的判定必须一致（防止两套评分体再次漂移）。

### 4.0 另两项改动的测试

新增 `tests/test_session_guard.py`（≥4 项）：
1. 假日志「90 秒静默 + 房间未结束」→ 判 degraded，返回非零码；
2. 「日志持续更新」→ 判健康，返回 0；
3. 「房间已结束（有 `kind=result` 且 `finished=true`）」即使静默也判健康；
4. `tests/test_bot_state.py` 新增：SIGTERM 路径下 `log.close` 被调用且写入终止记录
   （**用注入的假 log 对象断言，不要真的发信号**）。

新增 `tests/test_shanten_perf.py`（≥2 项）：
1. **返回值锁定**：对 3 组含 0/1/2 张白的固定手牌，硬编码 `shanten` /
   `pair_shanten` / `ukeire` / `combined_route` 的期望值，断言改缓存前后完全一致；
2. **性能断言**：2 张白的手牌，`combined_route` 单次 < 2 ms。

### 4.1 必须保持全绿、且**不许修改断言**的既有测试
```
tests/test_shanten_joker.py      （尤其 test_pair_shanten_* / test_pair_ukeire_mate_of_single）
tests/test_rules.py              （尤其 test_seven_pairs / test_seven_pairs_with_four_jokers）
tests/test_meld_game.py::test_open_meld_disables_seven_pairs
tests/test_ev.py::test_profile_disables_seven_pairs_after_meld
tests/test_claim_safety_audit.py （全部）
tests/test_production_gate.py    （全部）
```
它们红了说明你改过头了 —— **回去改实现，不要改测试**。

### 4.2 允许调整、但每一处都要写明理由的既有测试
```
tests/test_shanten_joker.py::test_combined_route_pair_leads
tests/test_shanten_joker.py::test_combined_route_tie_unions_waits
tests/test_strategy.py::test_preserves_seven_pairs_progress
tests/test_baohua_ticket.py（4 项）
tests/test_bot_state.py::test_alive_pair_route_claim_rejected
tests/test_responses.py::test_pair_route_hand_first_claim_rejected
```
调整方式**只能是**"把夹具换成满足新判据（≥5 个真实对子且严格更快）的手牌，
保持原断言语义不变"。
**严禁**改成 `assertNotEqual`、删除断言、加 `skip`、或放宽阈值。
每一处都要在 `docs/refactor/VALIDATION.md` 逐条列出：原夹具、新夹具、
为什么新夹具测的仍是同一个性质。

---

## 5. 第 4 步：历史日志验证（不发网络请求）

```bash
python3 tools/pair_route_metric.py --logs "logs/*.jsonl" --events "models/events/*.json" --baseline /tmp/after.json
python3 tools/pair_route_metric.py --compare /tmp/before.json /tmp/after.json
python3 -m unittest discover tests
python3 -c "from mj.security import scan; print(scan('.'))"
```

---

## 6. 离线验收指标（v2 修订，全部满足才算阶段完成）

| # | 指标 | 门槛 | 新判据实测 |
|---|---|---|---|
| A1 | 门清弃牌决策中 `pair_route_allowed` 为 True 的占比 | 从 23.5% 降到 **≤ 6.5%** | 5.36% ✓ |
| A2 | `round_level_new` 的 "6+" 桶局数 | 从 129 降到 **≤ 35** | 26 ✓ |
| A3 | **下限护栏**（防止把七对整个砍死）：决策级触发率 **≥ 3.0%**，且 `round_level_new` 的 "1-5"+"6+" 合计 **≥ 60 局** | 必须同时满足 | 5.36% / 94 局 ✓ |
| A4 | 全量测试 | 全绿，新增测试 ≥ 8 项 | — |
| A5 | `mj.security.scan('.')` | 命中数不高于改动前（已知 3 处历史命中不属本次范围） | — |
| A6 | 改动面 | 4 个策略文件 + `mj/bot.py` 外层 + 2 个新工具 + 3 个新测试文件 + 若干既有夹具调整 | — |
| B1 | `tools/session_guard.py --replay` 跑历史日志 | 21 房中**恰好 2 房**（`a_282b85347354`、`a_8afae15f071d`）被标 degraded，**其余 0 误报** | — |
| C1 | `tests/test_shanten_perf.py` 的返回值锁定 | 与改动前**逐值完全一致** | — |
| C2 | 2 张白手牌的 `combined_route` 单次耗时 | 从 ≈11 ms 降到 **< 2 ms** | — |

**A1/A2 达不到** → 判据太松，检查 `real_pairs` 是否误把白板计入。
**A3 跌破** → 判据太紧（`real_pairs>=6` 就会触发这一条：0.71% / 14 局），退回 `>=5`。
**A1 与 A2 矛盾**（A1 达标但 A2 不达标）→ 停下来汇报，不要自行加码收紧，这说明病因模型要修正。

> **残留风险，如实记录，不要试图调掉**：新判据下仍有 26 局落在 "6+" 桶，胜率 3.8%、
> 分/局 -5.08。n=26 无法分辨这是"判据仍然偏松"还是"真七对手本来就难成"。
> **禁止在 n=26 上继续调阈值**——那是过拟合。把它写进交付文档，留给线上 15 房的数据裁决。

## 7. 上线验证节奏（三档，时间成本已实测）

每房实测 **≈14 分钟**，连续自由对战无需人工值守（不要加 `--once`）。

### 档 1：离线自检 —— 0 房，必须先过
跑 `tools/pair_route_metric.py`，满足 §6 的 A1/A2/A3 + B1 + C1/C2。
**这一档不过，不许上线。** 它是确定性的，没有统计噪声。

### 档 2：线上 10 房（800 局，≈2.3 小时）—— 只看"有没有改坏"
这一档**不是显著性检验**，是回归体检。必须全部满足：

| 检查项 | 门槛 |
|---|---|
| 新增的非法动作 409（`cannot gang` / `chi only in chi window` / `peng only in peng window`） | **0**（容忍度为 0） |
| 本地 `hu_detail` vs 服务端 `round_ended` 的 winner/fan/detail | **逐值一致**（基线 291/291） |
| 每房 `timeout{kind:"discard"}` | **≤ 10 次**（历史正常房 0–8，崩溃房 429） |
| 有副露局胜率（1 / 2 / 3+ 组） | 不低于基线 22.7% / 24.7% / 26.2% **各 3 个百分点** |
| 门清局胜率（约 240 局样本，CI≈±5pp） | 从基线 **12.0%** 明显上抬；**若仍 ≤14%，停止，按 §8 回滚** |

### 档 3：线上 30 房（2400 局，≈7 小时）—— 才能说"变好了"
实测方差：每局得分 sd = **12.24**。单侧 α=0.05 / power=0.8：
- +0.8 分/局 → 1447 局 ≈ **18 房**
- +0.6 分/局 → 2572 局 ≈ **32 房**
- +0.4 分/局 → 5788 局 ≈ **72 房**

本轮预期效应是 **+0.40 ~ +0.50 分/局**，所以：
- **10 房读不出显著性，不要在 10 房宣称成功**；
- **30 房能看出方向**（总分应从 -0.836 抬到 -0.30 ~ -0.45 分/局）；
- **要严格证明 +0.4，需要 72 房（≈17 小时）** —— 这句话必须原样写进交付文档。

### 必须提前说清的期望管理
本轮**不会把总分打正**。四人零和，打平就是 0 分。本轮的目标是
**从"明显比对手差"回到"和对手基本持平"**：

| | 每局 | 1672 局折算 |
|---|---|---|
| 现状 | -0.836 | -1398 |
| 本轮预期 | **-0.30 ~ -0.45** | **-500 ~ -750** |
| 打正所需 | ≥ +0.01（胜率 ≥25.3%） | 下一轮的事 |

## 8. 立即回滚条件（任一触发）

1. **规则回归**：出现任何新的 `cannot gang` / `chi only in chi window` /
   `peng only in peng window` 类 409；或本地 `hu_detail` 与服务端 `round_ended` 的
   winner/fan/detail 出现**任何一条**不一致（当前基线 291/291 逐值一致，**容忍度为 0**）。
2. **有副露局变差**：1/2/3+ 副露局胜率相对基线（22.7% / 24.7% / 26.2%）下降 ≥3 个
   百分点 —— 说明门禁泄漏到了副露路径。
3. **门清局没改善**：15 房后门清局胜率仍 ≤14%（基线 12.0%）。
4. **总体反向**：15 房后总体胜率 ≤21.5% 且分/局 ≤-0.8。
5. **测试被软化**：任何既有断言被删除、弱化或 skip。
6. **范围越界**：修改了第 1.1 节列出的任何禁止文件，或动了任何权重数值。

回滚操作：用 `docs/audit/rollback_2026-09-22/` 里的 4 个文件覆盖回去，
删除 `tests/test_pair_route_gate.py`，还原第 4.2 节调整过的夹具。

---

## 9. 交付清单（三份，缺一不可）

1. 代码改动 + 新增/调整的测试（附全量测试输出与安全扫描输出）
2. `/tmp/before.json` 与 `/tmp/after.json` 的逐桶对比表，贴进 `docs/refactor/VALIDATION.md`，
   并如实标注**哪些指标达标、哪些没达标**（不许只贴达标的）
3. `docs/refactor/DECISIONS.md` 新增一条决策记录，必须包含：
   - 为什么门槛是"≥5 个真实对子 **且** 严格更快"，而不是别的阈值
   - 明确否决了哪些替代方案及理由（至少要覆盖：直接删掉七对路线；只调权重不改门；
     把门槛做成可配置权重）
   - 精确的回滚步骤

---

## 10. 工作纪律

- **本轮一次做完三项，但实施顺序必须是 3.6（缓存）→ 3.5（存活）→ 3.1~3.4（策略）**：
  前两项不改行为，先落地可以让第三项的离线重放跑得更快、且不受进程问题干扰。
- **不许把"吃碰质量"的任何改动塞进本轮**。那是下一轮的事，且**技术上现在也做不了**：
  `claim_assessment` 依赖 `route_shanten`，本轮改完之后吃碰门禁的行为会自动变化
  （预计 134 次 `route_shanten_worsens` 拒绝中有一部分会翻转为放行）。
  **必须先看到改完之后的真实吃碰分布，才能谈要不要调吃碰。**
- **不许用 arena 的数字论证任何策略结论**：审计已实测裁定其对本类问题无判别力
  （同策略自博弈、`gang=False`、`catch_play_circle=False`、fan≥2 只产出 14.7% vs
  真实 26.2%、单手 4.4 秒）。
- **不许用"样本少"解释任何失败**：1672 局、z=-4.35、70 局 0 胜 —— 数据早就够了。
- 遇到与本文件矛盾的历史文档（`HANDOFF.md` 等），以本文件为准，并把矛盾点写进交付文档。
- 如果任何一步的实测结果与第 0 节给出的数字对不上，**停下来汇报，不要自行调整假设继续做**。


---

## 勘误（2026-09-22 18:10，架构师签发）

v1 的 §2.2 锚点有两处错误，实施方按规则停机质询后已确认并修正。**错在架构师，不在实施方。**

**错误 1：决策级 20.8% 是抽样统计量，被当成总体值写了，且给了不可能满足的 ±1pp 容差。**
该数字来自一次 250 条门清弃牌决策的随机抽样（52/250），其自身标准误就有 ±2.6pp。
全量精确值是 **23.53%**（n=7727）。实施方实测 23.78%，一直都是对的。

**错误 2：1063 / 298 / 122 这组局数来自一份不完整的日志快照。**
架构师于 17:03 抽取 `logs/*.jsonl`，而房间 `a_8afae15f071d` 的对局时间是 17:02–17:18 ——
快照只截到它开局第一分钟，该房只有 **11 局**进入统计（其余 20 房均为 73–79 局）。
日志此后从 57 MB 增长到 142 MB。补齐后正确值是 **1110 / 308 / 129**，
实施方实测的 +47/+10/+7 = +64 局差异**完全由这一个房解释**。

**错误 3：v1 的 A3 门槛（"0 桶 ≥1250 局"）根本不起作用。**
收紧判据会让 "0" 桶**变大**而不是变小，所以对它设下限无法防止"把七对砍死"。
v2 已改为真正的下限护栏：决策级触发率 ≥3.0% 且 "1-5"+"6+" ≥60 局
（该护栏能正确拒绝 `real_pairs>=6` 这个过紧变体）。

**流程教训（写给所有后续实施者）**：v1 的 §2.2 把两套**过滤条件不同**的数字放进同一份
文档却只标注了其中一套 —— §0 的 589/205/121 含"剔除 2 个降级房 + 该局 ≥4 次门清弃牌"
双重过滤，§2.2 的 1110/308/129 不含任何过滤。**任何数字表都必须在表头写明过滤条件。**
实施方按 §10 停机质询是完全正确的动作，不是阻塞。


---

## 授权例外 #1（2026-09-23，架构师签发，已独立复核）

**背景**：实施方按 §10 停机质询，报告 `tests/test_claim_safety_audit.py` 的两处
硬编码计数 `8` 在新门禁下变成 `2`，而该文件在 §4.1 属"不许改断言"、§8 属回滚触发器。

**架构师独立复核结论：这是预告过的正确副作用，不是回归。授权修改，且必须同时加固。**

复核证据（架构师自行跑 `a_b478b2cbc5db` 的 128 次历史声明，未采信转述）：

| real_pairs | 标准向听 | 七对向听 | 旧 before | 新 before | after | 动作 | 新理由 |
|---|---|---|---|---|---|---|---|
| 3 | 4 | 3 | 3 | 4 | 4 | chi | first_meld_neutral_no_pair_route |
| 4 | 3 | 1 | 1 | 3 | 2 | peng | **first_meld_improves** |
| 4 | 4 | 2 | 2 | 4 | 3 | peng | **first_meld_improves** |
| 4 | 5 | 2 | 2 | 5 | 3 | peng | **first_meld_improves** |
| 4 | 5 | 2 | 2 | 5 | 3 | peng | **first_meld_improves** |
| 4 | 5 | 2 | 2 | 5 | 4 | peng | **first_meld_improves** |

旧门禁在做**苹果比橘子**的比较：`after` 因为副露永久关闭七对，只能是标准路线；
而 `before` 无条件取 `min(std, pair)`，被财神刷低的七对值拉下去。
后四行的标准向听 5→3，是**严格改善 2 个向听的碰，被旧代码判成"变差"而拒绝**。
这正是 §3.4 预告的"134 次 `route_shanten_worsens` 会有一部分翻转放行"。

剩余 2 条（real_pairs=5、before=1、after=2/3）仍判 worsening 且 `allowed=False`
—— **行为不变量完好**。

### 授权范围（只允许做这四件事，多一件都不行）

1. `test_all_eight_historical_worsening_claims_are_rejected` 的
   `self.assertEqual(len(worsening), 8)` → **`2`**；
   方法名里的 `all_eight` 同步改为 `test_historical_worsening_claims_are_rejected`
   （名字里带过时数字本身就是误导）。
2. `test_postmortem_reproduces_claim_observations` 的
   `self.assertEqual(effectiveness["route_shanten"]["worsen"], 8)` → **`2`**。
3. **同一处补钉完整分类**，不要只钉一个数：
   `improve=70`、`same=56`、`worsen=2`（合计 128）。
   `claim_count_by_round["1"]`（20/0/-168）与 `["2+"]`（45/13/60）**保持原值不动**——
   已复核仍然通过。
4. **新增一条不变量测试**（这是本次授权的对价，不做就不算完成）：

```python
def test_claim_comparison_uses_the_same_route_on_both_sides(self):
    """副露永久关闭七对，所以 after 必然走标准路线；before 只有在
    pair_route_allowed 为真时才允许走七对路线。两侧口径不一致会把
    "严格改善标准向听的碰"误判成"变差"——2026-09-22 审计在本房 128 次
    历史声明里抓到 6 次这样的误拒，其中 5 次实际是 first_meld_improves，
    最严重一次标准向听 5→3 被拒。本测试锁死这个口径，任何人退回
    无条件 min(std, pair) 都会立刻红。"""
    for snapshot, _decision, assessment in _accepted_claims():
        counts = to_counts(snapshot["my_hand"])
        mg = len(_melds_for_seat(snapshot))     # 复用既有取法，不要另写一套
        if not pair_route_allowed(counts, mg):
            self.assertEqual(assessment["before_shanten"], shanten(counts, mg))
```

### 明确禁止（违反即回滚）
- **不得**改动 `self.assertTrue(all(not assessment["allowed"] ...))`
- **不得**改动 `self.assertIsNone(selected)` 那个循环
- **不得**改动 `test_improving_and_second_open_hand_claims_remain_available`
- **不得**改动 `test_seven_pairs_potential_cannot_be_broken_by_neutral_first_peng`
  （该夹具 5 个真对子，天然满足新判据，必须继续 `assertIsNone`）

**这两行断言才是真正的行为不变量。`8` 只是用被修的那个函数算出来的描述性统计，
定义变了数字就变——改它不叫软化，删那两行才叫。**

### 为什么否决另外两个选项
- **方案 3（只回滚 §3.4）**：等于让吃碰门禁继续用苹果比橘子的比较，把上面 5 次
  严格改善的碰继续拒掉。**半修比不修更难查**，且会让线上 10 房的吃碰数据
  失去解释力（下一轮"吃碰质量"的诊断就是建立在这批数据上的）。
- **方案 2（留红交付）**：一个已定位、已验证、已授权的失败挂在测试套件里，
  下一轮任何人接手都会先去查它，浪费一整轮的注意力。

### 交付要求
在 `docs/refactor/VALIDATION.md` 里贴出上面那张 6 行翻转表（含
real_pairs / std / pair / old_before / new_before / after / 新理由），
并写明"经架构师独立复核授权"。**不要只写"按指示修改了计数"。**
