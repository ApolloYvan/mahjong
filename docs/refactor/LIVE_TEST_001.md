# LIVE_TEST_001：首次真实自由对战证据冻结

本文档冻结杭州麻将 Bot 首次真实自由对战（10 场、80 局）的原始证据，供
后续「实战可靠性收敛」各项修复对照验证。数据来源：

- `logs/2026-09-22.jsonl`（决策日志，35213 行）
- `models/events/*.json`（10 个测试房事件文件，房间 `a_d4dba30c1a75`，
  批次 `b0..b9`）

room/game/user 标识使用服务端原生生成的稳定假 ID（形如
`a_d4dba30c1a75_r1_b0_t0` / `u_fd06550b5fb3`），本身已脱敏，不是明文
用户身份信息；**本文档不包含任何令牌原文**。

## 1. 对局规模

- 10 场（房间 `a_d4dba30c1a75`，批次 `b0`–`b9`，每场 1 个 `tournament_id`
  下的独立测试局）。
- 80 局（`round_ended` 事件去重后计数，见 §5 的口径说明）。
- 79 次胡、1 次流局（`round_ended.data.draw=true` 恰好 1 次，位于批次
  `b3`，`round_no=2`）。
- 四名玩家（物理座位随场次轮换）：
  - `u_45bdbad221c9`（昵称"白虎-0211"）
  - `u_fd06550b5fb3`（昵称"重生之我是雀神"）—— **本 AI**
  - `u_2426e09d254e`（昵称"鲲鹏-7543"）
  - `u_579186180cbb`（昵称"朱雀-5888"）

## 2. 本 AI 战绩（按 `user_id=u_fd06550b5fb3` 跨场汇总，不按物理座位）

| 指标 | 数值 |
|---|---|
| 总分 | **-35** |
| 胜局 | **18** |
| 负局 | **61** |
| 流局 | **1** |
| 四人排名 | **第 2**（总分排序：261 / **-35** / -82 / -144） |

四名玩家总分之和为 0（261 + (-35) + (-82) + (-144) = 0），核算自洽。

胜/负口径：某局 `round_ended.data.draw=false` 且本 AI 座位对应
`scores[seat] > 0` 记为胜，`<= 0` 记为负（本数据集中非胡家均为负分，
不存在 0 分非流局的边界样本）。

## 3. 409 拒绝统计

- `decision_attempt` 总数：**3267**
- `decision_attempt_outcome` 分布：`success=2937`、`conflict_409=312`、
  `hu=18`、`fallback_sent=16`
- 409 拒绝率：`312 / 3267 = 9.55%`
- 409 按 `action_rejected.error` 消息分类（`code=INVALID_ACTION`）：

| 分类 | 次数 |
|---|---|
| not your response turn | 132 |
| cannot pass in phase 1 | 93 |
| already passed | 39 |
| chi only in chi window | 12 |
| peng only in peng window | 4 |
| 非法 gang（`cannot gang <tile>`，按牌面细分 12 种） | 32 |
| **合计** | **312** |

非法 gang 的根因见 §7 P0（暗杠牌数语义 bug：`hand.count(drawn)>=3` 把
"手牌含摸到的这张共 3 张"误判为"已有 3 张+摸到 1 张=4 张"）。

## 4. Timeout 与延迟

- 我方动态座位（按 `user_id` 定位物理座位后过滤）超时：
  - `response` 超时：**673** 次
  - `discard` 超时：**6** 次
- 全部玩家（不区分己方/对手）超时：`response=4906`（其中我方 673，
  对手 4233）、`discard=82`（其中我方 6，对手 76）。旧版 `mj.report`/
  `mj.timing` 未做座位→user_id 映射，无法区分"我方超时"与"对手超时"，
  只能给出全局总量，不能作为"本 AI 表现"的直接结论（见 §7）。
- action 请求延迟（`decision.action_request_ms`，样本数 2955）：
  - p50 ≈ **270ms**（269.97ms）
  - p95 ≈ **569ms**（568.99ms）
  - p99 ≈ 590.57ms，max ≈ 2416.42ms

## 5. notify / state 关联现状

- `notify` 日志条数：**15036**
- `state` 日志条数：**0** —— 当前 `_fetch_snapshot()`（`mj/bot.py`）不再
  对每次轮询调用 `log.state()`（B3 返修后改为去重/采样路径），导致
  `mj.timing.analyze()` 里 `notify_to_state_matches` 恒为 0，**这不是
  notify 真的没有驱动到任何一次 state 请求，而是当前落盘路径本来就不
  记录逐次 state 请求**，指标本身在生产接入前就已经失效。
- 当前 state 轮询固定 `seq=0` 全量拉取（`api.state(game_id, 0, ...)`），
  未真正使用 notify 携带的 seq 做增量请求——notify 线程目前只承担"唤醒"
  职责，不驱动请求内容本身。

## 6. 原 pipeline report 已知失真点（不得直接作为玩家结论）

`mj.report.build_report()`（修复前）与 `mj.timing.analyze()`（修复前）
存在以下失真，源数据本身是完整的，失真来自聚合口径错误：

1. **座位聚合而非玩家聚合**：`report.py` 用 `scores[seat]`/`wins[seat]`
   按物理座位号（0-3）累加，但十场对局中四名玩家的物理座位是轮换的
   （同一玩家在不同场次坐在不同座位）。按座位汇总的"胜负"/"得分"
   混合了四个不同真实玩家的数据，不能代表任何一个真实玩家的战绩。
2. **`draws=0`（应为 1）**：旧版 `build_report()` 从事件文件顶层
   `data.get("rounds", [])` 读取结算记录，而该字段本身会丢失流局这条
   （批次 `b3` 的 `round_no=2`，`round_ended.data.draw=true` 只存在于
   `blocks[*].events[*]`，不在顶层 `rounds` 数组里——顶层 `rounds` 数组
   本身就是不完整的二次汇总，不是权威事件流）。
3. **`timeout_ratio` 无意义**：`mj.timing.analyze()` 旧版把"全部玩家的
   服务端超时总数"除以"本地记录的 decision 总数"（`total_server /
   (total_server + sum(decision_actions.values()))`），分子是四人合计、
   分母是本 AI 一人的决策量，量纲不匹配，数值不能解释为"本 AI 的超时
   概率"。
4. `mj.timing.analyze()` 的 `server_timeout_by_kind_and_seat` 同样按物理
   座位号索引，同一座位号在不同场次代表不同真实玩家，不能跨场次相加
   解读为"某玩家的超时次数"。

以上四点在本轮"实战可靠性收敛"中修复（见 `mj/report.py`、
`mj/timing.py` 改动及 `docs/refactor/LIVE_TEST_001.md` 本节记录的
"修复前基线"），修复后的口径与验收输出见任务八验收报告。
