# 杭州麻将 AI 项目当前交接文档

最后更新：2026-09-22

本文档是当前项目的最新交接入口，优先级高于早期的 `HANDOFF.md` 和历史阶段交接文档。早期文档中的旧令牌、旧测试数量、旧性能结论和“尚未修复”的协议问题可能已经过时；不要直接照抄。

## 1. 项目目标与当前结论

目标是在杭州麻将 AI 竞技赛中提交一个可稳定接入官方平台、全自动决策、能在有限轮次内取得较高积分的 Bot。

当前代码已经完成两轮实战可靠性收敛，并完成两次真实自由对战。协议层已经基本可用，当前主要矛盾从“动作交互失败”转为“吃碰策略和有限轮次下的得分效率”。

当前可以继续进行自由对战测试。标准命令是：

```bash
cd /Users/yuanye/coding_workspace/mahjong
python3 -m mj.bot --once
```

不要省略 `--once`。省略后程序会在一个自由匹配房结束后继续申请下一房，适合持续采样，但调试和首次验证必须使用 `--once`。

当前环境中的 `MJ_TOKEN` 已经成功完成过自由对战，属于自由匹配使用的全局令牌。不要把 `MJ_TOKEN_1..4` 测试房令牌混用到自由对战，也不要把正式赛事报名令牌用于 `/api/match`。

## 2. 官方平台规则中必须牢记的内容

平台地址：`https://10.240.169.190:18080`

自由对战流程是：全局令牌调用 `POST /api/match` → 得到 `room_id` → 从 `/api/me` 或赛事详情发现 `game_id` → `/api/games/{id}/state` 获取权威快照 → `/api/games/{id}/action` 提交动作。

自由匹配标准配置是 `M=10, Rounds=8`，一房为10场并发、每场8局，即每个玩家一次获得约80局样本。`/api/match` 的 body 中 M/Rounds 是可承受上限，显式低于服务默认值会返回 `NO_ROOM_AVAILABLE`；不能通过 `--once` 设置 M=1/2/4/6/8。每用户同时在打场数判据是“正在进行的对局数 + 房间 M ≤16”，实际通常是一个 M=10 房结束后再匹配下一房。

重要规则：

- 白板“白”是财神，可以替代任意牌。
- 财神本身不能普通吃、碰、杠、胡，但爆头摸白可以选择胡或弃胡打白续飘。
- 只能自摸，不能点炮。
- 打出财神后形成抓打圈；非打财神者通常只能打刚摸到的牌，且不能吃、碰、明杠。
- 吃最多两摊；碰和杠不受两摊吃限制。
- 碰/明杠窗口优先于吃窗口。
- 最后20张牌（最后10墩）禁杠。
- 摸牌后可胡但不强制立即胡；杠后补牌也进入正常决策窗口，可胡、续杠或弃胡继续链，超时由服务端自动胡。
- 服务器不会下发 `allowed_actions`；客户端必须依据快照自行判定合法动作，非法动作返回409。
- 正式赛晋级排序链是 `total_score → place_points → god_count`；决赛主要看总得分。

状态协议：

- `GET /state?seq=0` 返回全量权威快照。
- `GET /state?seq=N` 可能只返回 N 之后的 events，不保证有 snapshot。
- SSE `/notify` 只发送 seq 信号，不发送牌面；当前实现使用 notify 唤醒，然后重新拉取 `seq=0` 的 canonical snapshot，不做本地事件 reducer。
- 409 或 gap 后必须用 `seq=0` 重建全量快照。
- `settled` 是局间结算暂停，不要拿上一局手牌提交动作。

## 3. 代码与数据结构

仓库：`/Users/yuanye/coding_workspace/mahjong`

项目是 Python 3.9+ 标准库项目，无第三方依赖。仓库没有可依赖的正式 Git 历史，修改前必须以当前工作区代码和测试为准。

核心文件：

- `mj/bot.py`：唯一有状态的在线编排层；主循环、动作提交、SSE、seq跟踪、409恢复、fallback。
- `mj/api.py`：HTTP客户端；state请求独立限速，action不占state pacer。
- `mj/responses.py`：吃、碰、杠响应策略和当前吃碰安全门禁。
- `mj/rules.py`：胡牌、七对、爆头和番型计算。
- `mj/shanten.py`：标准路线/七对路线向听与有效进张。
- `mj/strategy.py`、`mj/ev.py`：弃牌评分的两条历史路径；改权重时必须意识到两套评分并存。
- `mj/hu_strategy.py`：爆头摸白时“立即胡/弃白续飘”选择。
- `mj/logging.py`、`mj/async_log.py`：JSONL旁路日志、异步写入、异常窗口和紧凑状态指标。
- `mj/timing.py`：按动态座位区分自己和全桌的超时、409、延迟与notify指标。
- `mj/report.py`：按 `user_id` 汇总玩家结果；`round_ended` 是结算真相。
- `tools/pipeline.py`：`collect/report/timing` 统一入口。
- `tools/postmortem.py`：基于真实决策和事件流的房间复盘。
- `tools/live_preflight.py`：通用离线检查工具，输出信息较多；自由对战不依赖它，可直接运行 bot。

## 4. 当前在线实现的重要修复

以下修复已经完成并有测试覆盖：

1. 暗杠牌数语义修复：生产快照的 `my_hand` 已包含 `drawn_tile`，暗杠要求手牌总数达到4张，不能用旧的“手牌三张 + 额外摸牌”错误语义。
2. state和action限速隔离：只有state请求经过每用户16/s的pacer，action不再排在状态轮询后面。
3. 响应窗口至多提交一次：窗口键包含外层返回的 `returned_seq`，重复窗口和409陈旧窗口不重复提交。
4. 409强制seq=0恢复：不再用触发409的旧snapshot立即fallback；draw fallback必须在新权威snapshot上重新判断。
5. notify-driven canonical snapshot：events-only响应不会被当成决策快照；会记录指标并恢复全量snapshot。
6. watchdog自适应：SSE健康时 watchdog 5秒；SSE断线时切到1秒恢复轮询，恢复后回到5秒。
7. 吃碰安全门禁：模拟声明后的所有合法弃牌，比较最优后继的 `route_shanten/ukeire`；首副露原则上必须改善，禁止向听恶化，并保护有效七对路线。
8. 报告和复盘：按每场 `seats` 的 `user_id` 动态映射，不再把物理座位当玩家；`postmortem.py` 已删除对旧私有函数 `_claim_worthit` 的依赖。

当前不应擅自改动：比赛协议、state/action调度、胡牌判定、吃碰安全门禁、策略权重和arena，除非用户明确提出新的证据或需求。

## 5. 两次真实自由对战证据

### 房间一：`a_d4dba30c1a75`

事件文件在 `models/events/a_d4dba30c1a75_*.json`，10场、80局、79次胡、1次流局。

按玩家汇总：

- 白虎-0211：+261，28胜
- 重生之我是雀神（本AI，`u_fd06550b5fb3`）：-35，18胜
- 朱雀-5888：-82，16胜
- 鲲鹏-7543：-144，17胜

四人总分为0。旧版报告曾按物理座位汇总并把 draws 错报为0；新版 `mj.report` 已修正。

历史日志中协议问题很严重：3267次decision attempt、312次409（9.55%）、本AI响应超时673次、出牌超时6次。主要问题是状态轮询挤压动作请求、旧响应窗口去重错误和非法暗杠。

### 房间二：`a_b478b2cbc5db`

事件文件在 `models/events/a_b478b2cbc5db_*.json`，10场、80局、无流局。

按玩家汇总：

- 赌怪：+74，23胜
- 今晚打老虎：+72，22胜
- 啾咪啾咪：0，20胜
- 重生之我是雀神（本AI）：-146，15胜

四人总分为0。三家从本AI获得的支付几乎相等（约135、132、131），没有看到针对性合谋迹象；本AI主要是胡牌次数少、庄家胜率低、若干局吃碰后没有继续成牌。

关键策略证据：

- 本AI吃65次、碰63次，共128次，是四家最激进。
- 只有一次副露的20局：0胜、-168分。
- 两次以上副露的45局：13胜、+60分。
- 128次吃碰中有8次使route_shanten恶化，8局全部失败，合计-59分。
- 本AI胡牌15次、总收入252、总支付398，净分-146。
- 本AI庄家局14次、庄胡3次；赌怪庄家局25次、庄胡12次。

房间二的协议可靠性已经大幅改善：

- 3207次决策尝试、4次409，拒绝率0.125%。
- action p95约16ms。
- 本AI出牌超时5次、响应超时206次。
- state请求10792次，其中notify触发7703、watchdog触发3075、recovery触发14。
- state p95约1.15秒，说明10场并发仍接近16/s状态配额，但watchdog修复后请求量已下降。

房间二的结论是：协议层已经可用，但策略不应回到“所有合法吃碰都接受”。当前吃碰门禁已经针对这个证据修复，下一场真实房间用于验证门禁效果。

## 6. 当前测试状态

Sonnet最新报告：

- 全量测试：502 passed，约121.5秒。
- security scan：0命中。
- 吃碰安全审计和notify并发定向测试通过。

本会话独立复核过的定向测试：

```bash
python3 -m unittest tests.test_claim_safety_audit tests.test_notify_seq_concurrency tests.test_p0_final_review tests.test_responses
```

结果：31项全部通过。

标准全量测试命令：

```bash
python3 -m unittest discover tests
```

安全扫描：

```bash
python3 -c "from mj.security import scan; print(scan('.'))"
```

## 7. 下一步工作顺序

当前没有必须继续开发的阻断项。下一步应进行一次新的真实自由对战，验证新吃碰门禁和watchdog的生产效果：

```bash
python3 -m mj.bot --once
```

完成后拿终端输出的房间ID执行：

```bash
python3 tools/pipeline.py collect https://10.240.169.190:18080 <room_id>
python3 tools/pipeline.py report --room-id <room_id>
python3 tools/pipeline.py timing --room-id <room_id> --game-prefix <room_id>
python3 tools/postmortem.py <room_id>
```

重点验收：

- 10场、80局完整结束。
- 非法暗杠为0。
- 409率保持低于1%。
- 本AI出牌超时尽量为0，至少不能继续出现系统性超时。
- `state_timing` 有非零真实数据。
- `claim_effectiveness` 不再出现上一房间的硬编码/旧统计，能反映新房间。
- 新门禁下“单副露后完全不胡”的现象明显下降。

如果金丝雀房满足这些条件，再运行不带 `--once` 的连续自由对战采样。连续采样期间不要同时启动第二个M=10自动房；重复调用 `/api/match` 在等待期是幂等的，不会创建第二房。

## 8. 复盘命令的注意事项

`tools/pipeline.py report/timing` 默认会写 `models/report.json` 或 `models/timing_report.json`。如果当前环境对工作区写入受限，可直接在 Python 中把 output 指向 `/tmp`，这不是项目逻辑错误。

所有事件流应按房间过滤。不要把不同房间混入同一份统计。`round_ended` 的 `scores` 是该局结算分量，汇总时按去重后的 `(game_id, round_no)` 累加；物理座位号只能作为参考视图。

## 9. 安全和隐私

- 绝不把任何令牌写入文档、日志、fixture、commit或聊天。
- `room_id` 是赛后数据访问凭证，不要公开分享；在内部复盘文档中可使用稳定假ID。
- 门户排行榜接口需要OpenID登录态，Bot令牌不能直接访问；不要保存浏览器Cookie。
- 测试房令牌和正式赛事令牌都是scoped token，只能用于绑定的赛事/房间。
- 赛后事件流可用于离线算法优化，但只能在平台授权范围内使用；不要尝试读取进行中的复盘数据。

## 10. 接手原则

接手者第一步不是重写架构，而是读取本文档、`docs/refactor/LIVE_TEST_001.md`、当前 `mj/bot.py` 和 `mj/responses.py`，确认最新测试基线。任何新的策略判断都必须同时给出：真实事件证据、样本量、可能的牌运混淆和可验证的离线/在线验收标准。

