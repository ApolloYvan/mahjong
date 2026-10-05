# 实施任务：高手情境策略挖掘管线（一次性完成）

你要在 `/Users/yuanye/coding_workspace/mahjong` 里实现一套**离线分析管线**：从本地真实对局数据里挖出「高手在什么情境下做了和我们不同的决策、而且那个决策确实带来更高得分」，输出可解释的规则、离线验证结论，以及前 3 条规则的**默认关闭**的实现开关。

**开工前必须先读完这三个文件**，它们是规格和已知陷阱：
1. `docs/STRATEGY_MINING.md` —— 方法设计（本任务的规格说明书）
2. `.claude/skills/mahjong-strategy/SKILL.md` —— 领域知识、schema 陷阱、已证伪的方向
3. `docs/RACE_DAY.md` 末尾「当前策略配置」—— 哪些改动已保留、哪些已回滚

---

## 一、背景（两段话）

这是一个杭州麻将对战平台的 AI。规则要点：白板 = 财神（百搭）；**只能自摸，没有点炮**（所以没有传统意义上的防守）；番数 = `分支因子 × 2^动作链次数 × (4白板?2:1) × (爆头?2:1)`；庄家自摸收 24×番，闲家自摸收 10×番（其中庄家独付 8×）；打出财神后一圈内其他人不能吃碰、只能打刚摸的牌（抓打圈）。

我们当前胜率 ~28% 已够，但**番/胡 1.17 远低于高手 1.31~1.53**，差距 100% 来自「胡牌里爆头占比」（我们 13%、高手 25%）。2026-09-24 一整天手工猜特征调权重，实战 3 个改动全部失败。本任务改用数据驱动：不猜高手在想什么，让数据告诉我们。

---

## 二、硬约束

- **Python 3.9 标准库**。没有 numpy / pandas / sklearn / scipy，**不许 pip install**，不许联网。决策树、k-medoids、自助法、BH 校正全部手写。
- 不用 `match` 语句、不用 `X | Y` 类型注解（3.9 不支持）。
- macOS 上 `multiprocessing` 默认 spawn：worker 函数必须是模块顶层函数、参数可 pickle。参照 `tools/joker_playbook.py` 的 `Pool(args.jobs)` 用法。
- **不修改 `mj/` 下任何现有行为**。唯一允许的 `mj/` 改动是第六节的「默认关闭的规则开关」，且必须通过「开关关闭时行为逐字节不变」的黄金测试。
- 不修改 `mj/fit.py` 里任何**现有**权重的值。
- 结论不许用 `data/replay.jsonl` 和 `models/replays/`（荣耀回放精选集，幸存者偏差已实证：14/14 的现象在全语料里 EV 只有 1/36）。它们只能用于人工抽查。

---

## 三、数据

### 3.1 位置
- `tools/models/events/*.json` 与 `models/events/*.json`，共约 1250 个文件。**按文件名 basename 去重**（两处有重叠）。
- 文件名 `a_<房间>_r1_b<场>_t0.json`：一个文件 = 一场 = 同 4 人连打最多 8 局。一个房间 10 场。

### 3.2 已核实的 schema
```
顶层:   batch, blocks, game_id, room_id, rounds, seats, status
seats:  [{name, user_id}] × 4            （座位号 = 下标）
rounds: [{dealer, is_draw, multiplier, round_no, scores[4], winner}]
blocks: [{dealer, round_no, seq_start, seq_end, start_hands[4][13|14], truncated, events[]}]
events: {seq, type, seat, tile, data, ts}
```
事件类型与 `data` 字段：
| type | data | 说明 |
|---|---|---|
| `tile_drawn` | `gang_replenish` | 只有摸牌者本人可见（重放时四家都有）|
| `tile_discarded` | `catch_play` | **是否处于抓打圈**，直接用 |
| `chi` | `tiles`（含被吃的那张）| 从手里移除 `tiles` 去掉 `tile` 后的两张 |
| `peng` | — | 从手里移除两张 `tile` |
| `gang` | `kind ∈ an/ming/bu` | 移除 4/3/1 张；`bu` 不新增副露组 |
| `pass` | — | 响应窗口放弃 |
| `timeout` | `kind ∈ response/discard`, `window` | **`kind=discard` 表示这一手是服务端代打，不是玩家决策** |
| `round_ended` | `dealer, detail, draw, fan, round_no, scores` | **`fan` 直接可用，是番数真值** |
| `game_ended` | `final_scores` | |

### 3.3 重放方法
参照 `tools/joker_playbook.py` 的 `merge_rounds()`：按 `round_no` 合并 blocks，取 `start_hands` 全非空的那个 block，事件按 `seq` 排序，逐事件维护四家手牌与副露。任一步 `remove` 失败（`ValueError`）就丢弃整局并计数。

### 3.4 必须处理的数据质量问题
1. **`truncated=True` 的 block**：该局事件不完整，整局丢弃，统计丢弃数。
2. **服务端代打**：`timeout(kind=discard)` 与同座位相邻的 `tile_discarded` 配对，那次弃牌标 `auto=1`，**不作为玩家决策**。配对规则请先抽样 20 例实证确认（我们实测过 `timeout` 之后 2 个事件内常常找不到同座位事件，代打弃牌可能排在 timeout **之前**），把最终采用的配对规则写进报告。
3. **同名不同人**：「腾蛇」有 `腾蛇-0638`（高手）、`腾蛇-7681`、`腾蛇-1120` 三个账号。**一律按 `user_id` 识别人，名字只用于展示。**
4. 我们的 uid：`u_fd06550b5fb3`，显示名「重生之我是雀神」。我们的历史决策混合了多个代码版本——行为率比较可以用，但「我们现在会怎么做」必须用反事实（第五节）。

---

## 四、可复用的现有代码（直接 import）

```python
from mj.tiles  import JOKER, ALL_TILES, TILE_INDEX, INDEX_TILE, JOKER_IDX, to_counts
from mj.shanten import shanten, pair_shanten, ukeire, route_shanten, combined_route
#   shanten(counts, meld_groups=0) / route_shanten(counts, meld_groups=0)
#   combined_route(counts, meld_groups=0) -> (向听, 听口/进张 列表)
from mj.rules  import win_standard, seven_pairs, baotou, evaluate, payout
#   baotou(counts13, meld_groups=0)  —— 13 张稳定态判爆头
#   evaluate(counts13, draw_idx, chain_count=0, piao=0, meld_groups=0, ...)
from mj.joker_ev import hand_wait_cover   # (counts13_tuple, meld_groups) -> (命中, 参与判定总数)
from mj.strategy import choose_discard            # 闲家常态路径
from mj.ev       import choose_route_discard      # 庄家 / 链中 / 抓打圈路径
```
`counts` 是 34 维；需要做缓存键时转 `tuple`。

---

## 五、关键正确性要求（每一条都有人踩过）

### 5.1 反事实必须模拟生产的分流（最重要）
生产代码 `mj/bot.py::choose_discard` 按局面**二选一**两个平行评分体：
```python
use_route = bool(catch_play or chain_count or piao)   # YouCaiBiKao 平台当前为 False
if is_dealer:
    use_route = True
    rules = {"dealer_hint": True}
discard = choose_route_discard(hand, melds, chain, piao, rules, visible) if use_route \
          else choose_discard(hand, melds, chain, piao, rules, visible)
```
**`tools/policy_diff.py` 只调了 `choose_discard`，忽略了分流**——2026-09-24 有人据此改了 `mj/ev.py`，结果对照实验一个字都没变，因为改的是另一条路径。反事实列 `our_policy_act` 必须按上面的逻辑选路径。另加一列 `our_policy_act_baseline_only`（只用 `choose_discard`），用于和 `policy_diff.py` 对账。

### 5.2 信息泄漏（P3）
重放时四家手牌全知，但**特征列只能用决策者当下可见的信息**：自己的手牌、四家弃牌与副露、自己摸到的牌、`catch_play`。对手暗牌只能用于：(a) 结果标签，(b) 训练对手听牌估计器的**标签**。
**必须有自动化测试**：把对手暗牌随机置换后重新生成该决策点，所有特征列必须完全不变。

### 5.3 用结果判优劣，不用我们自己的特征（P1）
2026-09-24 实测：拿我们评分函数里的「进张」去比较，**我们自己的历史弃牌**相对当前代码都显示 −1.54——任何偏离我们 argmax 的选择在我们自己的特征上都必然显得更差。所以「哪个动作更好」只能看**后来发生了什么**（第七节的 `y_*` 标签）。

### 5.4 技术水平混杂
若动作 A 主要是高手在做，A 组结果好可能只因为做 A 的人后面也打得好。**主比较用同一人内部**：同一 `user_id` 在同一情境桶里有时做 A、有时做 B，只比这些样本。

### 5.5 伪重复
同一房间的决策高度相关。**所有置信区间按 `room_id` 聚类自助法**（重采样房间，1000 次，百分位 CI），不许按决策点或按局重采样。

---

## 六、交付物

### 6.1 `tools/decision_table.py` → `data/analysis/decisions.csv`
每个**玩家本人的**决策点一行（排除 `auto=1`）。决策点类型：`discard`、`claim`（吃/碰/杠/pass 响应窗口，仅本方在响应名单时）、`hu_or_not`（摸到能胡的牌时：胡还是继续打）。

列（按 `docs/STRATEGY_MINING.md` 第一节，下面是最低要求）：

| 组 | 列 |
|---|---|
| 定位 | `room_id, game_id, round_no, seq, seat, uid, cohort, dtype` |
| 时点 | `is_dealer, turn_no, wall_left, to_dead(=wall_left−20)` |
| 本方手牌 | `shanten_std, shanten_7p, shanten_route, ukeire, ukeire_live, wait_cover, to_baotou, jokers, pairs, triplets, lone, melds, chi_used, chain, piao_out, joker_total` |
| 牌桌 | `opp_melds_max, opp_melds_sum, dealer_melds, catch_play, i_am_exempt, visible_joker, opp_tenpai_p1..p3`（6.2 产出后回填）|
| 场况 | `sess_score, sess_rank, gap_to_1st, gap_to_4th, rounds_left` |
| 动作 | `act_type, act_tile, act_cat(字/幺九/二八/中张/白), our_policy_act, our_policy_act_baseline_only, tier_gap` |
| 结果 | `y_hu, y_fan, y_score, y_bt, y_tenpai_3, y_opp_hu_4` |

定义：
- `wall_left`：用「136 − 起手发牌 − 已摸张数」重放推算，写明公式。
- `to_baotou`：去掉 1 张财神后，其余牌「全部做成完整面子」还差几步（爆头的结构 = 非财神牌全成面子、财神单吊作将，实测爆头手对子数 0.12 vs 普通听牌手 1.26）。无财神时为空。实现写清算法，并用 20 个手工构造的例子测。
- `chain / piao_out`：按规则重放——爆头态打出白板 = 飘，链+1；杠 = 链+1；打出其他牌（含非爆头态打白）= 链断归零。
- `cohort`：≥300 局玩家按分/局排序，前 10 = `top`，后 10 = `bottom`，我们 = `ours`，其余 `other`。允许 `--top-uids` 覆盖。
- `y_fan`：取 `round_ended.data.fan`（真值）。同时算一列从分数倒推的番，报告两者不一致的比例。
- `y_bt`：该座位本局在之后任一次弃牌后的 13 张稳定态是否爆头。
- `y_tenpai_3`：本方接下来 3 次弃牌后是否听牌。
- `y_opp_hu_4`：接下来 16 次摸牌（≈4 巡）内是否有对手自摸。
- `sess_*`：同一文件内本局之前各局 `scores` 的累计。

参数：`--jobs`、`--limit N`（只跑前 N 个文件）、`--no-counterfactual`（反事实最贵，约 50ms/次，默认只对 `top` 与 `ours` 计算）。

**先用 `--limit 20` 做 profiling，把单文件耗时和全量外推时间写进报告。** 全量目标 ≤ 60 分钟（6 核）。

### 6.2 `tools/opp_tenpai.py` → `data/analysis/opp_tenpai_model.json`
对手听牌估计器。标签 = 对手**真实**向听 ≤ 0；特征只用公开信息（副露数、副露类型、巡数、其弃牌中字牌比例与幺九比例、最近 3 巡是否摸切）。模型用分桶频率表（带拉普拉斯平滑）即可。按房间留出 30% 输出**校准表**（预测概率 10 档 vs 实际频率）。然后回填 `decisions.csv` 的 `opp_tenpai_p*`。

### 6.3 `tools/divergence_mine.py` → `data/analysis/divergence.csv`
实现 `docs/STRATEGY_MINING.md` 第二节全部内容：
- 情境桶 + **分层回退**（两组都 ≥30 用精确桶，否则依次丢 `sess_rank → catch_play → chain → wall 段`），每行标注回退层级
- **覆盖率** = 我们（`ours`）决策点落入该桶的比例
- 行为率比较：两比例 z 检验 + **Benjamini-Hochberg**（FDR 5%）
- 噪声地板：分歧率 ≤5% 的桶丢弃
- 结果 Δ：**同人内部**按 `min(nA, nB)` 加权汇总；跨人混合 Δ 作为辅助列
- 房间聚类自助 CI
- `预期收益 = Δ × 覆盖率`，按它排序

### 6.4 `tools/rule_extract.py` → `data/analysis/rules.csv`
- 手写 CART：Gini、深度 ≤4、叶子 ≥200
- 规则枚举：离散特征上 ≤3 个条件的合取，support ≥100、lift ≥1.5
- 手写 k-medoids（k=6~10，多次随机初始化，轮廓系数在 5000 点子样本上选 k）
- 每条规则输出：条件、support、lift、Δ、CI、覆盖率、**在几名 top 玩家身上方向一致**、**70/30 按房间切分的留出复现结果**
- 入选条件（全部满足才标 `selected=1`）：Δ 的 CI 下界 >0；≥2 名 top 玩家方向一致；`bottom` 组不这么做；留出集复现；能写成 ≤3 个条件的 if

### 6.5 `tools/hypotheses.py` → 写入报告
对 `docs/STRATEGY_MINING.md` 第四节的 H1–H7 **逐条按文中的定义和通过标准**给出结论：`通过 / 否决 / 样本不足`。几个必须先做的存在性检查：
- H1：先统计「非爆头态弃白」之后同座位 `catch_play` 是否为 true（验证机制成立），以及全平台这类弃白的总数与分布
- H4：判据是同人内部的 **Δ = E[y|飘] − E[y|胡]**，不是飘的比例
- H5：先测高手的非爆头弃胡率是否显著 >0，≈0 则直接否决

### 6.6 前 3 条规则的开关实现（**默认关闭**）
按「预期收益」取前 3 条 `selected=1` 的规则：
- 每条一个权重开关 `rule_<id>_enabled: 0`，加进 `mj/fit.py` 的 `DEFAULT_WEIGHTS`
- **弃牌类规则必须同时作用于两个评分体**：在 `mj/ev.py` 里写一个共用函数，由 `mj/ev.py::route_value` 和 `mj/strategy.py::_discard_score` **两处都调用**（参照现有的 `pair_route_bonus` 收口方式）
- 每条规则的代码注释写明：来源数据、Δ 与 CI、覆盖率、回滚方法

---

## 七、测试

新增 `tests/test_decision_table.py`、`tests/test_mining_stats.py`、`tests/test_rule_switches.py`：
1. **信息泄漏测试**（5.2）：置换对手暗牌，特征列必须完全不变
2. **重放正确性**：手工构造一个含吃/碰/明杠/暗杠/补杠/抓打圈/代打的迷你对局文件，逐行断言输出
3. `to_baotou` 20 个手工例子
4. BH：已知 p 值列表的期望拒绝集合
5. 自助法：模拟数据上 95% CI 的覆盖率落在 [0.92, 0.98]
6. CART：构造一个单阈值可分的数据集，必须找到那个阈值
7. k-medoids：3 个分离良好的簇必须完全分开
8. **黄金测试（最重要）**：固定种子生成 2000 个随机局面，所有规则开关为 0 时，`choose_discard` / `choose_route_discard` / `mj.responses.choose_chi` / `choose_peng` / `mj.hu_strategy.choose_hu_or_piao` 的输出与改动前**逐一相同**

只跑相关模块，例如：
```bash
python3 -m unittest tests.test_decision_table tests.test_mining_stats tests.test_rule_switches
```
全部完成后再跑一次全量 `python3 -m unittest discover tests`，**必须 OK**。

---

## 八、回归锚点：必须和现有工具对得上

在**同一批文件**上跑下面这些现有工具，你的 `decisions.csv` 汇总必须复现它们的数字（差异要解释）：

| 现有工具 | 要复现的数 | 容差 |
|---|---|---|
| `tools/dealer_edge.py` | 我们的坐庄局数、坐闲局数、庄/闲胡率 | 局数完全相同；胡率 ±0.3pp |
| `tools/score_attribution.py` | 我们的收入/局、支出/局 | ±0.01 |
| `tools/joker_playbook.py` | 我们的胡牌数、总爆头占比 | 胡牌数相同；占比 ±0.5pp |
| `tools/policy_diff.py --player 歪比巴卜` | 与我们的一致率、层内比例 | 用 `our_policy_act_baseline_only` 列比，±1pp |

另外报告：用**正确分流**的 `our_policy_act` 算出的一致率，和 baseline-only 差多少。这个差值本身是一个重要发现。

---

## 九、最终交付：`docs/experiments/OFFLINE_REPORT.md`

必须包含，**不要写成散文**：
1. 数据概况：文件数、有效局数、丢弃局数及原因（截断 / 重放失败）、代打决策数、代打配对规则
2. 性能：各工具实际耗时
3. 回归锚点对账表（第八节）
4. 对手听牌估计器的校准表
5. **H1–H7 结论表**：`假设 | 结论 | Δ | 95% CI | 覆盖率 | 预期收益 | 跨人一致 | 留出复现 | 一句话理由`
6. `selected=1` 的规则全表，按预期收益排序
7. **前 3 条的实战预登记**，每条一段：假设、开关名、主指标（机制指标，不是分数）、通过阈值、护栏（胜率跌幅 ≤0.94pp、代打 ≤0.2%）、房数（10）
8. 已知局限：哪些结论可能受选择偏差影响、哪些桶样本不足

**如实报告。** 如果 H1–H7 全部否决、或者没有任何规则 `selected=1`，就这么写，并说明是样本不够、特征不够、还是真的没有差异——这本身就是有价值的结论。**不许看到结果之后再调阈值让规则通过。**

---

## 十、不要做的事

- 不要调现有权重、不要「顺手优化」任何策略代码
- 不要把规则开关默认打开
- 不要用精选回放下结论
- 不要按决策点重采样算 CI
- 不要跳过黄金测试
- 不要只改 `mj/ev.py` 或只改 `mj/strategy.py` 其中一个
- 遇到本 prompt 与代码实际不一致（字段名、函数签名），**以代码为准并在报告里记录**，不要猜
