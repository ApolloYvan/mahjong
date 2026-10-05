# 实施任务：持财神门清手的「多路线期望值」打分（route EV）

## 背景（为什么做）
- 项目：杭州麻将 AI（`/Users/yuanye/coding_workspace/mahjong`），Python 3.9，只用标准库。10/8 12:00 提交代码，之后不能再改。
- 规则要点：白 = 财神（万能牌）；只能自摸；爆头（13 张摸任何牌都胡）×2；七对 ×2；七对·爆头 = 4 番；
  庄家胡一次收 24×番，闲家 10×番。
- 数据结论：强手 4 番以上的大牌里，「七对·爆头」最多（晴总总 12/21、Deepseek胡 10/15、我胡汉三 9/16、
  Nomad 9/12、貔貅 9/11）；他们每 100 次胡牌有 2.3–4 次七对·爆头，我们只有 0.5–1.7 次。
- 根因：我们的打分把「普通胡 / 普通爆头 / 七对 / 财神计划」分成几块各自打补丁，互相看不见。
  七对门槛（`mj/shanten.py::pair_route_allowed`）要求 ≥5 个真实对子（财神不算）且七对向听严格更快，
  而 6 对 + 1 财神 = 摸任何牌都胡的 4 番，这条路我们基本走不进去。
- 目标：在「门清 + 手上有财神」这一块，用一笔统一的账替换：每个候选弃牌同时算 4 条路线，
  各算「剩余摸牌内走完的概率 × 走完的得分」，选期望最高的。

## 范围（只做这些）
适用条件（全部满足才生效，否则一律走原有逻辑、行为完全不变）：
- 开关 `rule_route_ev_enabled` = 1（在 `mj/fit.py` 的默认权重里新增，默认 0）；
- 副露数 = 0（门清）；手上财神 ≥ 1；不在财飘链上（chain_count = 0 且 piao = 0）；不是抓打圈。

## 现有代码（必须复用，不要重写）
- `mj/shanten.py`：`shanten(counts, melds)`（标准向听，财神当万能）、`pair_shanten(counts)`（七对向听，
  已正确处理财神）、`ukeire(counts, melds)`、`combined_route`、`route_shanten`、`pair_route_allowed`。
- `mj/rules.py`：`baotou(counts13, melds)` —— **已经把七对算进去**（`_wins_any` 含 `seven_pairs`），
  所以「七对·爆头听」可直接用 `baotou()` 判定；`seven_pairs(counts14)`；`evaluate(...)` 算番。
- `mj/joker_ev.py`：`to_baotou_distance(counts13, melds)`（标准爆头的距离，不含七对）。
- `mj/discard_features.py`：`baotou_waits(counts13, melds)`、`wait_live_score(waits, visible)`。
- `models/rollout_cal.json`：`hazard`（按本家第几次摸牌，别家一次摸牌自摸的概率）、
  `pay_n` / `pay_d`（别家自摸时我们闲 / 庄平均付出）。
- 弃牌入口：`mj/strategy.py::choose_discard`（闲家）和 `mj/ev.py::choose_route_discard`（庄家等），
  两处都先算同向听层 `tier`，再依次走 小模型 → 拟合打分 → 原打分。
- 吃碰入口：`mj/responses.py::choose_peng` / `choose_chi`。
- 胡牌时弃胡等爆头（S1）：`mj/hu_strategy.py::_decline_discard_choice` 用 `baotou()` 判断，
  七对·爆头已自动覆盖，**不要改**，只加测试确认。
- `mj/bot.py::choose_discard` 已把 `rules["_ctx"] = {"wall", "dealer", "opp_melds"}` 传进打分。

## 要实现的东西

### 1. 新模块 `mj/route_ev.py`
对 13 张口径的手（打出候选牌后）计算 4 条路线，每条给出：距离 d（还差几步到听牌）、
每一步的前进牌数 u（活牌张数，按 `visible` 扣除已见）、听牌后每摸一张胡的活牌数 w、胡时番数 f：

| 路线 | 距离 d | 前进牌 u | 听牌后胡牌 w | 番 f |
|---|---|---|---|---|
| A 标准 | `shanten(c,0)` | `ukeire` 的活牌数 | 听牌时的听口活牌数 | 1 |
| B 标准爆头 | `to_baotou_distance(c,0)` | d=1 时 `baotou_waits` 的活牌数；d≥2 用 A 的 u 近似 | 未见牌总数（摸啥都胡） | 2 |
| C 七对 | `pair_shanten(c)` | 能凑成新对子的单张种类的活牌数 | 听口活牌数 | 2 |
| D 七对·爆头 | 到「13 张且 `baotou(c,0)` 为真、胡法为七对」的距离（按定义实现：6 个对子 + 1 张可自由配对的财神；2 张财神的情形要正确处理） | 能凑成新对子的活牌数 | 未见牌总数 | 4 |

概率模型（纯 Python，确定性，不要随机模拟）：
- 本家剩余摸牌次数 N = max(0, (wall_remaining − 20)) // 4 + 1（`wall_remaining` 从 `rules["_ctx"]["wall"]` 取，没有就用 60）。
- 未见牌数 U = 136 − 已见（手牌 + `visible`）。每次摸牌：还没听牌时以 p = u/U 前进一步；听牌后以 q = w/U 胡。
- 别家先胡的概率：每轮 3 家各摸一张，每张按 `hazard[本家第几次摸牌]` 自摸；本家本局已摸次数从弃牌数近似。
- 用 DP 算 P_r = 在 N 次摸牌内、别家都没先胡的前提下本家经路线 r 胡牌的概率。
- 期望 EV_r = P_r × 收入(f)，收入 = 庄 24×f / 闲 10×f。庄闲从 `rules["_ctx"]["dealer"]` 或 `rules["dealer_hint"]` 取。
- 手的总价值 = max_r EV_r（其余路线不加分，保持简单）。
- 所有常数（10/24、hazard 缺省值、margin）放进 `mj/fit.py` 默认权重，前缀 `rev_`，方便线上调。

### 2. 弃牌接入（叠加，不替换）
- 在两个弃牌入口里，**在算 `tier` 之前**加一段（七对候选可能不在标准同向听层里）：
  范围满足时，对所有候选弃牌算 route EV；设 `best_rev` 为 EV 最高的候选、其最优路线为 r*。
- 只有当 r* ∈ {C 七对, D 七对·爆头}，且 `EV(best_rev) ≥ (1 + rev_margin) × EV(原逻辑会选的那张)`
  时，才改打 `best_rev`；否则完全按原逻辑。`rev_margin` 默认 0.10。
- 原逻辑选牌需要先正常算一次（复用现有函数），不要复制打分代码。
- 注意：小模型、拟合打分都不要动；开关关闭时两个入口的行为必须和现在逐张一致。

### 3. 吃碰接入
- 范围满足（门清 + 有财神）时，若当前手的 route EV 最优路线是 C 或 D，且
  碰/吃后（副露 1 组，七对路线作废）A/B 路线的最优 EV < 当前 EV × (1 − rev_margin)，则放弃这次碰/吃。
- 只在 `choose_peng` / `choose_chi` 开头加否决，不改原有门禁顺序。

### 4. 离线验证脚本 `tools/route_ev_check.py`
重放全部对局文件（参考 `tools/nn_extract.py` 的重放方式和 `baotou_funnel.MASTERS`），在范围内的弃牌点上统计：
- 覆盖：范围内决策点数；叠加逻辑改变选择的比例（按财神数、真实对子数分格）。
- 与高手对照：高手的实际选择 vs 原逻辑 vs 叠加后，一致率各多少（只看叠加改了选择的点）。
- 校准：按预测的 P(七对类胡牌) 分档，对比这些局实际以七对/七对·爆头胡牌的比例（高手、我们分开）。
- 速度：每个决策点叠加逻辑的平均/最大耗时。
- 用 `multiprocessing.Pool`，每 500 个文件打印进度；**只写脚本，不要运行全量**（由用户执行）。

### 5. 测试 `tests/test_route_ev.py`（环境变量 `MJ_WEIGHTS_NO_FILE=1`）
- 路线距离：6 对 + 1 财神 → D 距离 0 且 `baotou()` 为真；5 对 + 1 财神 + 2 单张 → D 距离 1；
  2 张财神的边界情形；标准爆头手 → B 距离 0。
- 开关关闭：随机 300 手（含财神），两个弃牌入口的选择与改动前逐张一致。
- 范围外（有副露 / 无财神 / chain>0）即使开关打开也与原逻辑一致。
- 叠加生效：构造一手「5 对 + 财神、标准路线稍快」的牌，开关打开后改打单张走七对。
- 吃碰否决：七对·爆头差一步的门清手，别家打出可碰的牌时放弃碰；开关关闭时照碰。
- S1 回归：七对普通听 + 财神、摸到能胡的牌、打一张即成七对·爆头时，`_decline_small_hu_tile` 返回该张。
- 性能：范围内单次决策叠加逻辑 < 30 ms（本机）。
- 只运行新增测试和 `tests/test_discard_features.py`、`tests/test_strategy.py`、`tests/test_ev.py`、
  `tests/test_hu_strategy.py`、`tests/test_responses.py`；**不要跑全量测试**（全量很慢，由用户执行）。

## 约束
- 不改已有函数的签名和默认行为；新逻辑全部由 `rule_route_ev_enabled` 控制，默认关闭。
- 任何异常都要吞掉并回落到原逻辑（线上不能因为新逻辑卡住出牌）。
- 不在 bot 打房时运行耗 CPU 的脚本；不编造数据；不打印、不提交任何令牌文件。
- 代码风格与周围一致：中文注释、说明依据的数据和日期。

## 交付
1. 改动文件清单与每处改动一句话说明。
2. 新增测试的运行结果（粘贴输出尾部）。
3. 给用户的两条命令：离线验证 `python3 tools/route_ev_check.py`；启用方法（`models/weights.json` 加
   `"rule_route_ev_enabled": 1`，改了代码需重启 bot 一次）。
4. 验收标准（写在交付说明里）：
   - 离线：叠加改选的点上，与高手一致率高于原逻辑；校准表里预测概率与实际七对胡牌率大致对应；
     单次耗时 < 30 ms。
   - 实战 5 房：`python3 tools/fan_sources.py --since <启用时间>` 的七对·爆头每百胡明显上升，
     `python3 tools/gap_breakdown.py --since <启用时间>` 的 闲/1白、闲/2+白 分差缩小；否则关开关回滚。
