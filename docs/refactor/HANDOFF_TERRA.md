# 交接 Prompt：杭州麻将 Bot 实战可靠性收敛任务

> 本文档用于将当前工作交接给 GPT Terra。请将以下内容整体作为背景 prompt 提供给 Terra，
> 使其在不重复劳动、不违反既有约束的前提下继续本任务。

---

## 一、你正在接手什么

仓库：`/Users/yuanye/coding_workspace/mahjong`

背景：杭州麻将 Bot 完成了**首次真实自由对战**（10场、80局），暴露出一批线上可靠性问题
（非策略问题）。本任务的目标是**实战可靠性收敛**——修复协议交互层的真实缺陷，
**不得改动**：策略权重、arena（竞技场对抗评测）、胡牌收益阈值、吃碰偏好。这是一条
硬约束，贯穿全程，任何后续修复都不能触碰这些内容。

任务分两轮完成：
1. **第一轮**（8大项）：证据冻结 + 4个P0协议修复 + notify驱动状态获取 + 度量/report/postmortem修复 + 验收。
2. **第二轮**（独立复核后的"最终阻断返修"）：独立复核发现第一轮的notify驱动状态获取方案里有一个**未被测试覆盖的P0生产缺陷**（增量响应被丢弃导致死循环风险），以及响应窗口键设计上的一个纰漏。第二轮做了4个P0修复 + P1（timing/postmortem补完）+ 完整验收。

**当前状态：两轮任务均已完成，全部验收通过。** 除非用户给出新指令，不需要再做任何代码改动。

---

## 二、当前进度全景（已完成，全部验证通过）

### 第一轮 8 大项完成情况
| 项 | 内容 | 状态 |
|---|---|---|
| 一 | 证据冻结文档 [docs/refactor/LIVE_TEST_001.md](docs/refactor/LIVE_TEST_001.md) | ✅ 完成 |
| 二 | P0：暗杠牌数语义修复（`hand.count(drawn)>=3` 错误，应为`>=4`，因为 `my_hand` 含 `drawn_tile` 本身） | ✅ 完成 |
| 三 | P0：状态轮询限速隔离（action不再排在state pacer后面） | ✅ 完成 |
| 四 | P0：响应窗口"至多提交一次"（`_response_window_key`） | ✅ 完成（第二轮又修正了seq来源，见下） |
| 五 | P0/P1：notify驱动状态获取 | ✅ 完成（第一版有缺陷，第二轮已修复，见下） |
| 六 | 修复 `mj.timing` | ✅ 完成 |
| 七 | 修复 `mj.report` 与 `tools/postmortem.py` | ✅ 完成 |
| 八 | 验收 | ✅ 完成 |

### 第二轮"最终阻断返修"（独立复核发现的缺陷）完成情况
| 项 | 根因 | 修复 | 状态 |
|---|---|---|---|
| P0-1 | 官方协议 `seq=N` 通常只返回events、不保证含snapshot；旧版 `_fetch_snapshot` 假设增量请求也会带回snapshot，`_play_game_loop` 在 `not snapshot` 时直接 `continue`——events被丢弃、`processed_seq`不推进、反复请求同一seq、永远拿不到新决策依据 | 放弃"增量事件重放"思路，改为**notify只作唤醒/去重信号，请求参数恒为 `seq=0`（拉全量权威snapshot）**——如实命名为 **notify-driven canonical snapshot**（不是incremental event reducer） | ✅ 完成 |
| P0-2 | `_response_window_key` 读取 `snapshot.get("seq")` 作为窗口标识的seq来源，但snapshot内层seq字段不一定可靠 | 改签名为 `_response_window_key(game_id, snapshot, returned_seq)`，seq分量强制来自 `/state` 响应**外层**的 `response["seq"]` | ✅ 完成 |
| P0-3 | action 409后旧代码没有强制走全量恢复，可能在同一份陈旧snapshot上重复决策 | `_SeqTracker.force_recovery(reason)`，任意action 409立即调用；draw阶段fallback推迟到下一次循环基于**全新snapshot**才决策（不在409异常处理里就地用旧snapshot fallback） | ✅ 完成 |
| P0-4 | 缺少真实协议形态的端到端测试 | 新增 [tests/test_p0_final_review.py](tests/test_p0_final_review.py)（9个测试：初次全量/notify唤醒/重复notify/events-only/gap/SSE断线/watchdog/409恢复/finished/10场并发） | ✅ 完成 |
| P1 | `mj.timing`/`postmortem.py` 补完（第一轮遗留） | own/all-player timeout分离、动态座位映射、409分类、p50/p95/p99/max、`decision_attempt_outcome`增加可选`game_id`；`postmortem.py`删除`_claim_worthit`依赖、room-id显式必需 | ✅ 完成 |

---

## 三、关键事实（避免误解，务必牢记）

- **真实测试房间**：`a_d4dba30c1a75`，10场（`b0`-`b9`），共80局（79胜1流局）。
- **本AI身份**：`user_id = u_fd06550b5fb3`（昵称"重生之我是雀神"），总分**-35**，18胜61负1流局，四人中排**第2**。
- **四人总分必须为0**：验证过 `261 + (-35) + (-144) + (-82) = 0`。
- **真实409统计**：3267次decision_attempt，312次409（拒绝率**9.55%**），分类：`not your response turn`132、`cannot pass in phase 1`93、`already passed`39、`chi only in chi window`12、`peng only in peng window`4、非法gang 32。
- **本AI超时**：response 673次，discard 6次（区别于all-player的更大数字）。
- **物理座位会轮换**：不同场次里同一物理座位号可能对应不同真实玩家，所有统计代码都**动态识别**"我方座位"（不硬编码），已在 `mj.report`/`mj.timing`/`postmortem.py` 中统一处理。
- **`state_timing`目前全为0**：因为这份真实历史日志（`logs/2026-09-22.jsonl`）早于`state_request_metric`字段引入，无法回填。**这是如实反映数据缺失，不是bug**。要验证notify利用率/watchdog/recovery计数真的非零，需要**下一次真实对局**产生新日志。

---

## 四、核心文件与职责

| 文件 | 职责 |
|---|---|
| [mj/bot.py](mj/bot.py) | 核心决策循环 `_play_game_loop`；`_SeqTracker`（notify-driven canonical snapshot状态机）；`_response_window_key`（响应窗口去重）；`choose_action`/`choose_gang`等决策函数（**策略逻辑，不要动**，本任务只改协议交互层） |
| [mj/responses.py](mj/responses.py) | 响应窗口决策（peng/chi/gang），`choose_gang`已修复暗杠计数bug |
| [mj/api.py](mj/api.py) | HTTP客户端，`request()`新增`pace`参数区分state（限速）与action（不限速） |
| [mj/logging.py](mj/logging.py) / [mj/async_log.py](mj/async_log.py) | 决策日志落盘，含`state_request_metric`/`decision_attempt_outcome`等 |
| [mj/timing.py](mj/timing.py) | 度量分析：409率、超时分离、延迟分位数、notify利用率 |
| [mj/report.py](mj/report.py) | 按玩家（非物理座位）汇总胜负/得分，以去重后的`round_ended`事件为结算真相 |
| [tools/postmortem.py](tools/postmortem.py) | 复盘CLI，只基于真实证据（`decision`/`decision_attempt`/`action_rejected`/`round_ended`），room-id显式必需 |
| [tools/pipeline.py](tools/pipeline.py) | 统一CLI入口：`report`/`timing`子命令 |
| [docs/refactor/LIVE_TEST_001.md](docs/refactor/LIVE_TEST_001.md) | 首次实战证据冻结文档 |
| [data/fixtures/protocol/gang_hand_count_semantics.json](data/fixtures/protocol/gang_hand_count_semantics.json) | 暗杠计数修复的脱敏fixture |

---

## 五、验收结果（最新一次，可直接复现）

```bash
# 全量测试
cd /Users/yuanye/coding_workspace/mahjong && python3 -m unittest discover tests
# → Ran 497 tests in 134.194s / OK

# 安全扫描
python3 -c "from mj.security import scan; print(len(scan('.')))"
# → 0

# 三个CLI（room-id = a_d4dba30c1a75）
python3 tools/pipeline.py report --room-id a_d4dba30c1a75
python3 tools/pipeline.py timing --room-id a_d4dba30c1a75 --game-prefix a_d4dba30c1a75
python3 tools/postmortem.py a_d4dba30c1a75
```
以上三个CLI均给出与"关键事实"一节一致的数字，退出码均为0。

---

## 六、仍未解决 / 需要你关注的问题

1. **`state_timing`全零**：需要一次新的真实对局才能验证notify驱动机制在生产环境下的实际利用率（当前只有单测/模拟验证过逻辑正确性，见`tests/test_p0_final_review.py`、`tests/test_notify_seq_concurrency.py`）。
2. **历史遗留字段未清理**：`mj.report`/`mj.timing`里仍保留按物理座位聚合的旧字段（`seat_breakdown`等）供兼容参考，明确标注"仅供参考，不代表玩家结论"，未做破坏性删除。
3. **未发起过新的真实自由匹配**：按任务要求，本轮修复完成后不主动联网/不发起真实匹配——如果需要验证notify机制的真实效果，需要用户明确授权后才能进行。

---

## 七、硬性约束（Terra 必须遵守，不可协商）

- 不改动策略权重、arena、胡牌收益阈值、吃碰偏好。
- 不联网、不发起真实自由匹配（除非用户明确新指令）。
- 涉及真实用户/房间/对局标识时使用稳定假ID或指纹，不写入令牌原文。
- 任何数字结论（测试数、409数、分数等）必须来自实际运行工具验证，不得凭记忆或推测给出。

---

## 八、给 Terra 的建议行动

当前没有待办的代码修复任务。接下来请：
1. 先通读本文档 + [docs/refactor/LIVE_TEST_001.md](docs/refactor/LIVE_TEST_001.md)，建立对"证据从哪来、数字为什么是这些"的完整认知。
2. 如果用户给出新的问题/需求，先用 `python3 -m unittest discover tests` 确认当前基线仍是绿的，再动手。
3. 如果用户要求验证notify机制的真实效果，需要先获得用户明确授权才能发起新的真实对局（当前策略是"不发起"）。
4. 修改 `mj/bot.py` 等核心文件前，务必先读现有代码理解 `_SeqTracker`/`_response_window_key` 的设计意图（见上方"关键事实"与代码内详尽的中文docstring），避免重新引入本轮修复的bug。
