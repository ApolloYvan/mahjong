# 任务：统一账本 v3（本轮只实施 T0、T1、T3）

项目 /Users/yuanye/coding_workspace/mahjong，Python 3.9 标准库。在 docs/IMPL_UNIFIED_EV_V2.md（P0~P5 已交付）的同一份账本上扩展。10/8 12:00 提交，之后不能改。
第一步：把本 prompt 全文（含 T2、T4 规格）写入 docs/IMPL_TABLE_EV.md，再开始实施。

约束：
- 认人一律用 tools/mining_common.py::seat_names(game)（我们 user_id u_fd06550b5fb3，平台名「乾元用九」），不要按名字。
- 不要改 models/weights.json（线上 bot 正在用 P1+P2 打）；不要启动 bot；不要跑全量扫描（T0 工具、体检由用户执行）。
- 新开关在 mj/fit.py 默认权重里加，默认 0；异常一律吞掉回落原逻辑；新增耗时受 rev_time_budget_ms 约束，超预算回落。

规则事实（已用日志核实）：
- 连庄直上 ×8 固定。每番：庄家自摸 +24（三家各付 8）；闲家自摸 +10（庄付 8、另一闲付 1）。
- 胡者接庄（闲胡 1961 次全部胡者接庄；庄胡 791 次庄不变）；流局约 0.15%，庄不变，可忽略。
- 每个对局文件 = 一场 8 局（正式比赛每房 8 局）。

现有账本的三处错误：
1. 付出项用平均数（rev_loss_dealer 10.33 / rev_loss_idle 4.73）。闲家时，庄胡我付 8×番，另一闲胡我只付 1×番，差 8 倍，被平均数抹掉了。
2. 对手速度固定：_survival_series 三家同一个 hazard[turn]，即 (1-h)^3。
3. 没算庄位价值：胡者接庄，自己每次胡牌都额外值 V(局号)，第 8 局 V=0。漏掉它会系统性低估"现在就胡"。

========== 本轮实施 ==========

T0 数据工具（用户运行；multiprocessing.Pool；每 200 文件打印进度；参考 tools/batch_dashboard.py 的增量缓存写法，缓存放 tools/.cache/）

(a) tools/hazard_fit.py → models/hazard_table.json
- 样本：所有文件、所有座位的每一次摸牌（tile_drawn）；标签：这一摸是否自摸胡。
- 特征只用线上可观测的（先核对 bot 线上能拿到什么，拿不到的不用）：
  role（是否庄）、melds（副露组数 0/1/2/3+）、draw_idx（该座位本局第几摸，1..19+）、late_mid（该座位最近 3 张弃牌中数牌 3~7 的张数，0..3）。
  线上若能区分摸切/手切，加 tsumogiri_streak（0/1/2+）。
- 样本 <200 的格子按 late_mid → melds → role 顺序逐级回退到更粗的格子，把回退链写进 json。
- 同一文件里输出 fan_by_role：庄家胡、闲家胡时各自的平均番。
- 另外输出 models/opp_profile.json：每个名字的速度倍率 = (实际自摸数+30)/(表预测自摸数+30)；样本 <300 摸的名字不输出。
- 打印校准：按预测值分 10 档，列 预测 vs 实际 自摸率；另列 高手/我们/其他 三组的 实际/预测 比。

(b) tools/dealer_value.py → models/dealer_value.json
- 先算每个名字全历史的平均每局分 skill(name)。
- 对每个对局文件、每个局号 r（1..8）、每个座位：
  resid = (第 r+1 局到该文件最后一局的得分和) − skill(name) × (最后局号 − r)
- V(r) = 平均[resid | 第 r+1 局是庄] − 平均[resid | 第 r+1 局是闲]；V(8) = 0。
- 分 我们/高手/全体 三组打印：V(r)、样本数、未扣 skill 的原始差（对照用）。
- 写入文件的是：全体、扣 skill 后的值。

T1 分对手付分（开关 rev_opp_pay_enabled）
- 每一步"被别人先胡"拆到每个对手 o：o 先胡的概率 ≈ h_o / Σh × (1 − Π(1−h_o))。
  本轮三家 h 仍取 rollout_cal 的同一个值（T2 下一轮再换成动态值）。
- 付分：o 是庄或我是庄时 pay_o = 8 × fan_o，否则 pay_o = 1 × fan_o。
  fan_o 取 hazard_table.json 的 fan_by_role；文件缺失时默认 1.3。
- _win_and_lost 增加返回值 lost_pay（每步 lost 质量 × 该步加权付分，累加）。开关打开时，用 lost_pay 替换 lost × rev_loss_*。
- _ctx 补：我是否庄、庄家相对座位（mj/bot.py 组装 _ctx 处；取不到就回落旧付出项）。
- 接入 best_route、P2 速度前瞻、P4（_piao_bonus 与 choose_hu_or_piao 的两个按账分支）、P5 吃碰。

T3 庄位价值（开关 rev_dealer_value_enabled）
- 自己胡牌的每个结果都加 V(局号)：
  best_route：value = win × (fan × payout + V) − 付出项
  P2 速度前瞻：同上
  P4 飘：当场胡 = f × payout + V；飘 = s × (2f × payout + V) − (1−s) × 付出项
  P4 S1：当场胡 = f × payout + V；转爆头 = P × (2f × payout + V) − 付出项
  P5：吃碰前、吃碰后两侧都加
- 局号取 _ctx["round_no"]（1..8；没有就在 mj/bot.py 补）。取不到或 dealer_value.json 缺失 → V=0，等于没开。
- V 只进入按账分支。rev_piao_enabled / rev_decline_enabled 关闭时仍走旧阈值逻辑，不要改旧逻辑。

测试（MJ_WEIGHTS_NO_FILE=1，追加到 tests/test_route_ev.py）
- 新开关全关：tests/test_rule_switches.py 的固定校验值不变。这是唯一的一致性证明，不要拿新代码和新代码对比。
- T1：三家 h 相同、pay 取旧平均数时，与 P1 结果一致；闲家时，庄家胡概率上升比闲家上升让账下降得多（约 8 倍）。
- T3：构造 P=0.53 的 S1 例子（关 = 弃胡，开 = 当场胡）；局号 8 时与关闭一致；dealer_value.json 缺失时与关闭一致。
- 性能：超预算回落（用假时钟）。
- 只运行：tests.test_route_ev tests.test_rule_switches tests.test_responses tests.test_strategy tests.test_ev tests.test_hu_strategy

交付
1. 改动清单 + 测试输出尾部。
2. 给用户的命令：先跑 python3 tools/hazard_fit.py、python3 tools/dealer_value.py，贴出校准输出；启用方式是在 models/weights.json 里加 rev_opp_pay_enabled=1、rev_dealer_value_enabled=1（本次改了代码，要重启 bot 一次）。
3. 验收：5 房，用 python3 tools/batch_dashboard.py --since <UTC> 看。强手桌分/局和胜率上升、平均番不跌、discard 超时不增加；明显变差就关掉开关。

========== 下一轮（本轮只写进文档，不实施） ==========

T2 动态对手速度（开关 rev_opp_hazard_enabled；名字画像另开 rev_opp_profile_enabled）
- _survival_series 改为：按每个对手当前可观测状态查 hazard_table.json，逐步推进 draw_idx（副露、late_mid 保持当前值）；survive_step = Π_o (1 − h_o)。
- rev_opp_profile_enabled 打开时 h_o × 名字倍率（截断到 [0.6, 1.6]）；名字不在画像里 = 1。
- _ctx 补每个对手的：副露组数、本局已摸次数、最近 3 张弃牌、是否庄、名字。缺项用回退格子，不报错。
- 表缺失/损坏 → 回落 rollout_cal 旧逻辑。

T4 喂牌控制（开关 rev_feed_enabled）
- 只做平局裁决：与最终选择同向听层、账差 < rev_feed_margin（默认 0.3 分）的候选里，选喂牌代价最小的。
- feed(t) = Σ_o P(o 能吃碰 t) × Δh_o × 剩余摸数 × pay_o
  P(碰)：用超几何分布估 o 持有 ≥2 张 t；吃只对下家粗估；Δh_o = o 副露 +1 时查表的差值；pay_o 同 T1。
- 效果：闲家时主要避免喂庄家；我是庄时避免喂任何人；庄家快听时，喂另一闲家的代价很小，不会被限制。
