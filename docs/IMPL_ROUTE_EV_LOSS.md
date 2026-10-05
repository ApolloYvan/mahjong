# 实施任务：route EV 第二步——把「别人先胡要付的钱」算进账，并把标准爆头纳入改写

## 背景
- 项目：`/Users/yuanye/coding_workspace/mahjong`，Python 3.9 标准库。10/8 12:00 提交，之后不能再改。
- 上一步已上线：`mj/route_ev.py`（开关 `rule_route_ev_enabled`，已打开）。门清 + 持财神时，对每个候选弃牌算 4 条路线
  （A 标准 1 番 / B 标准爆头 2 番 / C 七对 2 番 / D 七对·爆头 4 番）的期望，只有最优路线是 C/D 且明显更高时才改写弃牌。
  上线后 108 次胡牌：七对每百胡 1.3 → 4.6，平均番数 1.24 → 1.43（高手 1.44），「胡得小」已追平。
- 剩余差距转到胜率：我们 26.4%，高手 27.7%。原因之一：route EV 只算「胡了赚多少」，
  **没算「别家先自摸、我要付多少」**——慢路线的代价被低估。庄家被自摸平均付 10.33 分，闲家 4.73 分
  （`models/rollout_cal.json` 的 `pay_d` / `pay_n`，为负数）。

## 要做的两件事（各自一个开关，默认都关）

### A. 输钱项（开关 `rev_loss_enabled`，默认 0）
1. `mj/route_ev.py::_win_probability` 改为同时返回两项：
   - `win`：本家经该路线胡牌的概率（现有）；
   - `lost`：在本家走完之前，别家先自摸结束本局的概率（DP 里每一步被 `survive` 扣掉的那部分质量之和）。
   - 守恒：`win + lost + 剩余（流局或没走完）≈ 1`，写进测试。
2. `best_route` 的路线价值改为：
   `value = win × fan × payout − lost × loss`，其中 `loss = |pay_d|`（庄）或 `|pay_n|`（闲），从 `rollout_cal.json` 读，
   缺失时用权重 `rev_loss_dealer`（默认 10.33）/ `rev_loss_idle`（默认 4.73）。
   `rev_loss_enabled=0` 时 `value` 与现在完全一致（不减输钱项）。
3. **边界修正（必须做）**：加了输钱项后，期望值可能为负。现在的比较
   `best_value >= orig_value * (1 + rev_margin)`（弃牌改写）和 `best_after < before_value * (1 - rev_margin)`（吃碰否决）
   在负值时方向会反。统一改成「差值门槛」：
   `best_value - orig_value >= max(rev_margin_abs, rev_margin * abs(orig_value))`；
   否决：`before_value - best_after > max(rev_margin_abs, rev_margin * abs(before_value))`。
   新增权重 `rev_margin_abs`（默认 0.5，单位：分/局）。
   这条修正在 `rev_loss_enabled=0` 时也生效；需在测试里确认对现有正值场景的判定与原公式一致或更保守
   （给出对比：随机 500 手持财神门清手，新旧判定不同的次数，写进交付说明）。

### B. 标准爆头也可以改写（开关 `rev_override_b_enabled`，默认 0）
- 现在只有最优路线 ∈ {C, D} 才改写。开关打开后，最优路线为 **B（标准爆头）** 时也允许改写，
  判定门槛相同（差值门槛）。A（标准胡）永远不改写——那是现有打分已调好的地方。
- 吃碰否决同理：当前最优路线为 B 时，若吃碰后的最优路线（副露 +1，只算 A/B）明显更差，也否决。
- 范围不变：门清 + 持财神 + 不在财飘链 + 不是抓打圈；预筛 `_pair_rich` 只对 C/D 有意义，
  B 需要另一个便宜的预筛：候选里存在 `to_baotou_distance ≤ 2`。两者任一为真才算完整账。

## 必须复用 / 不要动
- 复用 `mj/route_ev.py` 现有结构（`_routes`、`best_route`、`maybe_override_discard`、`claim_route_veto`、`_pair_rich`）；
  两个弃牌入口（`mj/strategy.py::choose_discard`、`mj/ev.py::choose_route_discard`）和 `mj/responses.py` 的接入点不改。
- 小模型、拟合打分、S1 弃胡、杠门禁都不要动。
- 新权重全部加进 `mj/fit.py` 默认权重（`rev_` 前缀），附中文注释和依据。

## 验证脚本
`tools/route_ev_check.py` 增加参数 `--on KEY=VALUE ...`（可多个），把这些权重叠加到「开开关」那一侧，例如：
`python3 tools/route_ev_check.py --limit 200 --jobs 8 --on rev_loss_enabled=1 rev_override_b_enabled=1`。
「关开关」一侧 = 当前线上配置（`rule_route_ev_enabled=1`，两个新开关 0），这样比较的是**新增部分**的效果。
报告里增加：改写后最优路线的分布（B / C / D 各多少）。进度每 10 个文件打印一次（已有，保持）。

## 测试 `tests/test_route_ev.py`（追加，`MJ_WEIGHTS_NO_FILE=1`）
- DP 守恒：`win + lost ≤ 1`，且 hazard 全 0 时 `lost == 0`。
- 输钱项：同一手，`rev_loss_enabled=1` 时慢路线（d 大）的价值下降幅度 > 快路线。
- 负值门槛：构造 orig、best 都为负的情形，只有差值超过门槛才改写。
- 两个新开关都为 0：随机 300 手持财神门清手，两个弃牌入口选择与改动前逐张一致（差值门槛若改变了个别判定，
  测试里列出这些手并在交付说明解释）。
- B 改写：构造「标准爆头差一步、现有打分选了别的」的手，开 `rev_override_b_enabled` 后改打。
- 性能：范围内单次决策叠加逻辑，单进程 p99 < 50 ms（用固定随机种子的 200 手测）。
- 只运行 `tests.test_route_ev tests.test_responses tests.test_strategy tests.test_ev`；不跑全量，不跑验证脚本全量。

## 约束
- 任何异常吞掉并回落原逻辑；新逻辑全部受开关控制。
- 不在 bot 打房时运行耗 CPU 的脚本；不编造数据；不碰令牌文件。

## 交付
1. 改动文件与每处一句话说明。
2. 测试输出尾部；「差值门槛 vs 旧公式」在 500 手上的判定差异次数。
3. 给用户的命令：
   - 验证：`python3 tools/route_ev_check.py --limit 200 --jobs 8 --on rev_loss_enabled=1`，
     再跑一次 `--on rev_loss_enabled=1 rev_override_b_enabled=1`；
   - 启用：`models/weights.json` 加对应开关为 1（本次改了代码，需重启 bot 一次；之后改开关不用重启）。
4. 验收标准：改写点上「与高手一致率」不低于关开关一侧（安全检查）；单进程 p99 < 50 ms；
   最终以实战 5 房的 `luck_vs_masters`（对手修正后）与胜率为准——胜率上升且平均番数不跌才保留。
