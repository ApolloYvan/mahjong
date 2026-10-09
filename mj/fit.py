"""从历史决策样本校准路线评分权重。"""
import json
import os
import threading
from collections import Counter

DEFAULT_WEIGHTS = {
    "shanten": 10000,
    "ukeire": 100,
    "pair": 35,
    "seven_pairs": 150,
    "joker": 80,
    "chain": 500,
    "piao": 800,
    "must_kaoxiang": 300,
    "seven_pairs_route": 550,
    "discard_joker": -5000,
    "tenpai_claim_margin": 1,   # 听牌后吃碰要求听口至少加宽这么多种；调成 -99 即回滚
    "b_shanten": 10000,
    "b_ukeire": 100,
    "b_ukeire3": 0,
    "b_joker": 20,
    "b_pair": 25,
    "b_honor": 300,
    "b_progress": 180,
    # 2026-09-23 实测重定价：爆头原本只值 3000，而一步向听是 b_shanten=10000——
    # 也就是说只要能快一步，机器人永远选快、不选爆头。但实测爆头值得多：
    #   * 番 ×2 => 收入/胡 16.83 -> 33.66
    #   * 听口从约 11 张活张 -> 全部；实测听牌进张 <=3 种胜率 21.0%，16+ 种 63.6%
    #   * 两变量回归（46 名玩家）：番/胡 +0.1 = +0.272 分/局，比胜率 +1pp 还多
    # 按"爆头带来的胜率提升约等于 1 步向听"重定价为 9000（0.9 步）。
    # 依据表：同样 1 张财神 + 2 组副露时，我们爆头率 24%，腾蛇-0638 73%、
    # 两脚离地 57%、爆头研究所 55%、Astra-0 50%（tools/joker_playbook.py）。
    # 回滚：改回 3000。
    "baotou_all_wait": 9000,
    "baotou_no_slow": 1500,
    "baotou_faster": 2200,
    # 2026-09-24：3000 → 11000、9000 → 13000。**修的是非单调性，不是加码。**
    # joker_plan_value 相对全速方案的净值 = premium − gap × b_shanten(10000)：
    #     gap 0 步 +1500 │ 1 步 **−7000** │ 2 步 0 │ 3 步 −1000 │ 4 步 −2000
    # 「留白慢一步」比「慢两步」还差 7000 分——2026-09-22 那次只按 gap 线性外推修了
    # gap>=2（gap_base=20000 追平速度惩罚），slow1 原样留在 3000，最近、最常见的
    # 一档被漏掉了。改完净值单调下降：+1500 / +1000 / 0 / −1000 / −2000。
    # 证据（tools/baotou_shape.py，1白1露 那一格）：
    #   高手 爆头   n=57   对子0.12 刻子0.32 孤张0.00   ← 非财神牌全做成完整面子、
    #   高手 非爆头 n=208  对子1.26 刻子0.20 孤张0.12     财神单吊作将，摸什么都胡
    #   我们 非爆头 n=414  对子1.24 刻子0.27 孤张0.18   ← 七列和高手失败手完全重合
    # 我们那一格的爆头样本 <12（joker_playbook 报 1%，高手 16~21%）。失败手形状
    # 一致、成功手缺席 = 我们从没走上那条路，而不是走了没走通。
    # 回滚：改回 3000 / 9000。
    # ↑ 2026-09-24 离线 A/B 回滚（tools/weight_ab.py，64 配对批次 / 1024 手）：
    #   score_delta = +13.16 ± 10.30（旧值 − 新值），换算 **新值亏 0.82 分/局**，
    #   CI95 [−0.44, +2.08] 跨 0；胜率 新值 47.7% vs 旧值 52.3%。
    #   点估计为负 → 比赛期先回滚。**但这个 A/B 对本改动结构性偏置**：模拟器
    #   产出的 4番 只占 0.6%（真实高手 2.91%），且 gang/catch_play_circle 未建模、
    #   chain_piao 只部分实现——留财神的价值全在大牌尾巴上，它测得到代价、
    #   测不到收益。所以这不是"机制被证伪"，是"还没有能证实它的证据"。
    #   重新启用：改回 11000 / 13000，然后跑一批**专门的 10 房**，判据是
    #   tools/baotou_shape.py --cell 1,1 里「我们 爆头」那一行会不会出现
    #   （当前 414 手里 <12 手；高手同格 57/265 = 21.5%）。
    # 2026-09-24 实战证伪（3 房 237 局，build 551cd0b12012 确认生效）：
    #   胜率 28.4% → 24.1%，分/局 +0.17 → −0.886，爆头占胡牌 13.4% → ≤8.8%。
    #   离线 A/B 早预测胜率掉 4~5pp，实战吻合；预期的爆头收益完全没出现。
    #   结论：「留白慢一步」定价低确实存在，但抬高它只让我们更慢，不产生爆头——
    #   爆头形状要求非财神牌全做成完整面子（对子≈0，见 tools/baotou_shape.py），
    #   而评分仍在奖励对子（b_pair / pair_route / seven_pairs_route）。
    #   留住财神是必要条件，不是充分条件。
    "baotou_slow1": 3000,
    "baotou_slow1_dealer": 9000,
    "baotou_hold_slack": 6,
    "baotou_gap_base": 20000,
    "baotou_gap_step": 9000,
    # 听口覆盖率 → 爆头溢价的插值指数。2 = 凸（保守）；设成极大值退化回
    # 「满 34 张才给 all_wait、否则只给 no_slow」的旧口径。见 joker_ev._best_plan。
    # 2026-09-24 赛前回滚：999 → frac**999 == 0，premium 退化回旧的
    # 「满 34 张给 all_wait、否则只给 no_slow」。想恢复梯度改回 2。
    "baotou_wait_power": 999,
    # 弃牌位置价值：高手的弃牌顺序是 字牌 → 幺九 → 二八 → 中张，而我们原先
    # 对数牌的位置零偏好（见 mj/ev.py route_value 的注释与实测数据）。
    # 量级只够当同向听时的平局裁决。回滚：三个全设 0。
    # 2026-09-24 **已证伪，故全部归零**（代码路径保留，改回 250/100/250 即可复现）。
    # 实测：把我们打幺九的比例从 21% 拉到约 70% 之后，与歪比巴卜的一致率
    # 61.7% → 59.8%（**变差**），与两脚离地 64.7% → 65.3%（持平）。
    # 而且作用点错位：高手分歧最大的是 3向（41~55%），我们的行为变化却集中在
    # 1向/2向（35~38%），3向 只动了 12~15%——最早期同向听候选之间的进张差异很大，
    # 250 分够不着。结论：幺九/中张的顺序偏好**不是**差距来源。
    "discard_terminal": 0,
    "discard_28": 0,
    "keep_middle": 0,
    "pair_route": 600,
    "baohua_ticket": 800,
    # 2026-09-24 策略挖掘（tools/hypotheses.py H5，见
    # docs/experiments/OFFLINE_REPORT.md）：非爆头可胡时，手上财神>=2 张的
    # 情境下，top cohort（≥300局分/局前10名）弃胡（continue 而不是直接胡）
    # 的比例是我们的 5 倍（jokers==2: 45.2% vs jokers==3: 83.3%，我们整体
    # 非爆头弃胡率只有 4.2%），且这么做同人内部净赚 Δ(y_fan)=+1.01
    # 95%CI[0.88,1.16]（房间聚类自助 1000 次），跨 85/95 名高手方向一致，
    # 垫底玩家同情境弃胡率显著更低（z=7.53,p<1e-13，对照C），70/30 按房间
    # 留出复现（train 0.86[0.76,0.96] / test 0.81[0.70,0.93]，用宽松的
    # jokers>=1 分歧率口径核实；jokers>=2 窄口径的独立复现见报告）。
    #
    # 2026-09-24 收敛任务 S1 重写（同一开关，逻辑重写，键名/默认值不变，
    # 见 tools/five_questions.py 与 docs/experiments/OFFLINE_REPORT.md
    # 「五问结论」S1）：H5 只验证了「财神>=2」这一格，S1 用同一份数据把
    # 分层做全（摸到的是否白板 × 财神数 × 是否庄 × 牌墙余量）后发现「财神
    # >=2」不是唯一显著的一格——「财神==1」与「本次摸到白板」同样显著且
    # 方向一致。三个候选条件（同人内部 Δ(y_score)，房间聚类自助 CI，
    # 70/30 房间留出全部复现）：
    #   财神>=2                  Δ=13.39 [10.55,16.61]  覆盖率(我方)0.51%  预期收益0.068
    #   财神>=2 或摸白            Δ=10.63 [ 9.06,12.26]  覆盖率(我方)1.15%  预期收益0.122
    #   财神>=1 或摸白（本次采用） Δ=10.54 [ 9.38,11.85]  覆盖率(我方)2.23%  预期收益0.235
    # 采用「财神>=1 或摸白」：CI 最紧、覆盖率最高、预期收益（Δ×覆盖率）
    # 最大。三者跨人一致性相近（79~85 人方向一致 / 85~95 人总数），
    # top vs bottom 同情境弃胡率差距同样显著（对照C）。
    #
    # 同一轮 S1 还发现：弃胡后打哪张牌本身是独立可分的信号——真实弃胡按
    # 打出的牌分三类，(c)"打别的牌、留住摸到的牌"的最终 y_score=25.0，
    # 显著高于 (b)"原样弃掉摸到的牌"的 14.7（y_hu 差异 z=3.70,p=0.0002）与
    # (a)"打财神"的负分（n=2）。原实现固定打 drawn_tile，只覆盖了 (a)/(b)
    # 两个更差的类别，从未真正走到 (c)——已一并在
    # mj.hu_strategy._decline_discard_choice 里改为按向听/进张挑最优的
    # 非财神牌，完整推导见该函数与 _decline_small_hu_tile 的 docstring。
    #
    # 2026-09-24 correctness 修正（用户实测反馈后修复，不是本轮初次发现）：
    # 上面 Δ=10.54/覆盖率2.23%/预期收益0.235 这组数字统计口径是"只要真实
    # 弃胡了就算"，没有验证弃完之后是不是真的转成爆头——`decline_discard_
    # choice` 初版实现也只按向听/进张挑"看起来最好"的牌，没有验证弃完后
    # `rules.baotou()` 是否为真，可能弃了却仍是非爆头形，两头不讨好。
    # 已修复：候选弃牌必须满足"弃完之后 baotou() 为真"，不存在这样的牌就
    # 不弃（返回 None，直接胡）；另加一道墙尾闸门
    # （`wall_remaining - 20 < 8` 时不弃）。用
    # `tools/s1_recheck_baotou.py` 把这条新条件套回 decisions.csv 重新算：
    # 旧口径的 1511 次真实弃胡里 164 次（10.9%）根本没转成爆头，把这些也
    # 算进"弃胡"高估了收益；只保留"当时确实存在能转爆头的牌"这个更严格的
    # 人群（n=2290，旧口径 8581）重算——
    #   Δ(y_score) = 7.23 95%CI[3.97,10.82]（train 7.22[3.06,11.69] /
    #   test 7.99[1.57,15.11]，留出复现仍成立）
    #   覆盖率(我方) = 0.585%（旧口径 2.23% 是高估）
    #   预期收益 = Δ×覆盖率 ≈ 0.042（旧口径 0.235 是高估，约 5.6 倍）
    #   top vs bottom：88.97% vs 46.38%（z=10.58,p≈0，仍非常显著）
    # 完整数字与三个候选条件对比方法论见 OFFLINE_REPORT.md「五问结论」S1。
    #
    # 2026-09-24 收窄（第三次修订，用户反馈）：「财神>=1或摸白」是在"S1 会
    # 触发"的全体人群上一次性选出来的，没有再按"财神数 x 副露数"细分。用
    # `tools/s1_narrow.py` 在 top cohort（10人）+ 嘎达嘎达（uid=
    # u_45062bea0121，独立100场单人对照）两个人群上按（有效财神数:1/2+ x
    # 副露数:0/1/2+）分格重算，只保留"高手弃胡率>=40% 且 Δ(y_score) 房间
    # 聚类自助 CI 下界>0"的格子（以 top cohort 为准，嘎达嘎达独立验证方向）：
    #   (财神1,副露1)、(财神2+,副露0)、(财神2+,副露1)、(财神2+,副露2+) 保留
    #   (财神1,副露0)、(财神1,副露2+) 剔除（前者两个人群都没通过CI门槛；
    #   后者 top 的 CI 下界精确等于 0.0，不满足严格 >0）
    # 等价简化：有效财神>=2 时任意副露都触发；有效财神==1 时只有副露==1
    # 才触发。完整分格数字见 mj/hu_strategy.py::_decline_small_hu_tile
    # docstring 与 OFFLINE_REPORT.md「五问结论」S1（收窄版）。
    #
    # 机制指标（实战预登记，护栏与覆盖率见 docs/experiments/OFFLINE_REPORT.md）：
    # 命中情境（非爆头可胡 且 落在保留的财神数x副露数格子里 且存在能转爆头的牌
    # 且墙尾够深）时的弃胡率；护栏：胜率跌幅 ≤0.94pp，代打率 ≤0.2%。
    # 回滚：设 0（默认值即是 0，不生效）。
    "rule_decline_joker_hold_enabled": 0,
    # S1 扩格：财神==1 时副露 0 / 2+ 也弃胡转爆头（依据见
    # mj/hu_strategy.py::_decline_small_hu_tile）。需同时打开上面的 S1 总开关。
    "rule_decline_single_joker_all_melds_enabled": 0,
    # 爆头态补杠/暗杠后仍听任意 → 岭上必胡，杠开 ×2（见 mj/bot.py::_baotou_gang_open_choice）。
    "rule_baotou_gang_open_enabled": 0,
    # 留第 4 张（2026-10-08 tools/gangkai_study.py，仿玄武-2346）：持财神时摸到自己碰牌的第 4 张不当场补杠、
    # 也不打掉，留着和财神凑对；等其余牌成形时补杠 → 剩下正好是爆头听、岭上必胡 = 杠开·爆头 4番
    # （_baotou_gang_open_choice 接手）。墙剩 ≤ bu_defer_wall_min 时恢复当场补杠，免得过了墙尾 20 张杠不出去。
    "bu_defer_enabled": 0,
    # 无财神不碰「向听不变」的牌（mj/responses.py::_nojoker_flat_peng_veto），默认关
    "flat_peng_veto_nojoker": 0,
    "bu_defer_wall_min": 24,
    "rule_std_pair_enabled": 0,
    "rule_decline_single_joker_one_meld_enabled": 1,   # S1 的 1白/1露 格；推演弃胡更优 87%、高手弃 96.8%，保持 1
    # 杠会拆牌型（杠后向听 > 不杠向听）就不杠（暗/补/明杠）。tools/gang_study.py：这种局面高手只杠 11%~20%，
    # 我们 82%~100%；高手不杠的局胜率/得分明显高于我们杠了的局。见 mj/responses.py::gang_hurts_shape。
    "rule_gang_no_hurt_enabled": 0,
    # 弃牌小模型（mj/nn_discard.py，模型文件 models/nn_discard.json；缺文件时开关无效，自动回落）。
    "rule_nn_discard_enabled": 0,
    # 庄家无财神手是否用拟合打分（见 mj/ev.py::choose_route_discard）；0 = 回到原 route_value。
    "rule_fitted_discard_dealer_d0_enabled": 1,
    "rule_fitted_discard_enabled": 0,   # 见 mj/discard_features.py；fd_* 权重缺省 = 现行等价值
    "rule_fitted_discard_joker_enabled": 0,   # 持财神手；须同时写入 fj_* 拟合权重才生效
    "rule_fitted_claim_enabled": 0,           # 吃碰；见 mj/claim_features.py，须同时写入 fc_* 拟合权重
    "std_pair_ratio": 1.5,
    # 2026-09-24 收敛任务 S2（tools/five_questions_s2s3.py，完整数字见
    # docs/experiments/OFFLINE_REPORT.md「五问结论」S2 与
    # mj/responses.py::claim_assessment 里本键旁的注释）：听牌态碰/杠响应，
    # 高手接受率 80.6% 显著高于我们 65.0%（z=6.90,p≈5e-12），而同一状态下
    # 的吃响应两边基本持平（48~49%）——现有 tenpai_claim_margin=1 是用
    # "吃+碰合并"的旧口径校准的，方向对 chi、但对 peng/gang 过严。
    # 本键只放松 peng/gang（take 是两张同种牌）的听牌态门槛：allowed 判据从
    # "必须让听口比原来多至少 margin 种" 降为 "不比原来窄即可"（仍然要求
    # 不变差，只是不再要求额外加宽），chi 的判据完全不受影响。
    # Δ(y_score)=5.86 95%CI[4.01,7.39]，train/test 70/30 房间留出复现
    # （5.95[4.20,7.53] / 5.52[1.06,8.67]），覆盖率(我方) 8.13%——是本轮
    # 五问里预期收益最大的一条（Δ×覆盖率≈0.476）。
    # 注意：这与 mj/responses.py 2026-09-23 那次"听牌后吃碰必须加宽或爆头"
    # 的修复方向部分相反（那次是用吃+碰合并统计校准的，本次拆开后发现方向
    # 只在 chi 上成立），实战上线前建议按机制指标（听牌态 peng/gang 接受率）
    # 单独核实，不要与 chi 的行为混在一起看。
    # 回滚：设 0（默认值即是 0，不生效）。
    "rule_tenpai_peng_gang_relax_enabled": 0,
    # 2026-09-24 收敛任务 S6（tools/five_questions_s2s3.py 的姊妹分析，完整
    # 数字见 docs/experiments/OFFLINE_REPORT.md「五问结论」S6 与
    # mj/responses.py::claim_assessment 里本键旁的注释）：非听牌态、向听
    # 不变的 chi，我们接受率远高于高手（首副露 70.5% vs 21.9%，已有副露
    # 88.0% vs 23.9%），而"能改善向听"的 chi/peng 方向相反（高手接受率更
    # 高：90.6% vs 81.2%、96.9% vs 89.7%）——差距specifically 在"向听不变"
    # 这一档。只针对 chi，碰/杠不动。
    # 同人内 Δ(y_score)（accept vs pass）=-1.28 95%CI[-1.87,-0.60]（接受比
    # 跳过平均倒扣 1.28 分），70/30 房间留出复现（train -1.23[-1.97,-0.35] /
    # test -1.43[-2.29,-0.44]）。跨人一致性：全体 94 人里 61 人（64.9%）
    # 方向一致，样本量>=10 的 31 人里 21 人（67.7%）一致，含我方自己历史
    # 上（版本漂移期）曾接受过的 3106 次里方向也一致（Δ=-1.85,n=3106，
    # 全语料单人最大样本）。**如实标注一个薄弱点**：top cohort 10 人里只
    # 有 8 人在这一档有足够数据算方向，其中只有 3/8 一致；样本最大的两个
    # top 个体（n=14、n=31）反而是正方向——top-10 子集本身太薄（多数人
    # min(接受,跳过)只有1~6次），不能仅凭 top-10 子集判定，改用全体population
    # （含我方历史、含全部 cohort）作为主判据，这与 H5 一节"不是只有 top10
    # 会，普通玩家里也压倒性方向一致"的先例一致。
    # 命中次数/局 = 3106/10774 = 0.2883（我方非听牌 chi 决策点里向听不变的
    # 频率，按我方总局数折算）；行为改变比例 = 命中时我方实际接受(chi)的
    # 比例 = 2380/3106 = 0.7663；预期分/局 = 0.2883×0.7663×1.28 ≈ **0.283**
    # ——三条开关里排第二（S1 0.342 > S6 0.283 > S2 0.198）。
    # 机制指标（实战预登记）：命中情境（非听牌+chi+向听不变）时的接受率，
    # 线下 ours 基线 76.6%（历史，含代打前的版本混合）vs top 22.7%，开启后
    # 应显著下降；护栏：胜率跌幅 ≤0.94pp，代打率 ≤0.2%。
    # 回滚：设 0（默认值即是 0，不生效）。
    "rule_reject_neutral_chi_enabled": 0,
    # 2026-09-25 收敛任务 S7 追加（实战新数据，完整数字见
    # docs/experiments/OFFLINE_REPORT.md「五问结论」S7 与
    # mj/joker_ev.py::menqing_baotou_bonus 里本键旁的注释）。
    #
    # 背景：S6 上线后 5 房实战，门清占胡从 13.9%→22.7%，胜率 24.7%→27.6%，
    # 但番/胡从 1.25→1.15、爆头占胡从 20.3%→14.5%；joker_playbook 口径下
    # "2+财神/0副露"我方胡牌爆头率 0%(n>=8) vs 嘎达嘎达 45%，"1财神/0副露"
    # 0% vs 18%——比上一版 S7（只看"1张财神+shanten==1"）覆盖面更广，这次
    # 按财神数（1 / 2+）分格重新在门清（melds==0）+ shanten∈{1,2} 的弃牌
    # 决策上核实。
    #
    # 任务1结论（两格都显著，比上一版单格结果更干净）：
    #   财神数=1：弃牌后 to_baotou 下降比例 top 52.2% / ours 48.8% /
    #     bottom 47.6%，同人内 Δ(y_score)=+2.10 95%CI[1.60,2.55]，
    #     70/30 房间留出复现两段CI下界均>0，top10 人里 10/10 方向一致。
    #   财神数=2+：下降比例 top 80.3% / ours 75.9% / bottom 74.4%，
    #     Δ(y_score)=+3.03 95%CI[1.52,4.26]，留出复现CI下界>0，
    #     top10 人里 6/10 方向一致（弱于 1 财神格，但仍过线）。
    #
    # 任务2诊断（门清+财神>=2+可胡但 S1 的 rule_decline_joker_hold_enabled
    # 未触发的 hu_or_not 行，原因分布 top/ours/bottom）：
    #   没有能转成爆头形的候选弃牌 51.8% / 67.0% / 70.7%（占主导）；
    #   墙尾闸门拦截 0% / 0% / 0%（不相关）；
    #   本可触发但未触发（其余原因）48.2% / 33.0% / 29.3%。
    #   说明差距主要不在 S1 的胡/弃胡判断本身，而在更早的中局弃牌没有把
    #   手牌往"能转爆头"的方向摆——这正是本开关要覆盖的情境。
    #
    # **护栏（吸取 baotou_slow1 的实盘教训）**：baotou_slow1/baotou_all_wait
    # 那次全局调高爆头权重导致实盘胜率从 28.4% 掉到 24.1%（见 mj/fit.py
    # 上面 baotou_slow1 键旁的完整记录）——本次仍然只在门清
    # （meld_groups==0）+ shanten∈{1,2} + 财神数>=1 这一窄人群生效，不是
    # 全局爆头溢价，通过 mj/joker_ev.py::menqing_baotou_bonus 这个共享
    # helper 同时接入 mj/ev.py::route_value 与
    # mj/strategy.py::_discard_score（两条平行评分体都调用同一个函数，
    # 不各写一份）。
    #
    # 命中次数/局与行为改变比例：**本次改用 tools/s7_flip_rate_replay.py
    # 对生产代码 mj.bot.choose_discard 做真实重放（开/关本开关各选一次
    # 弃牌，比较是否不同），不是理论上限**（吸取上一版 S7 的教训）。
    # 命中数、翻转数、翻转率、预期分/局的最终数字见
    # docs/experiments/OFFLINE_REPORT.md「五问结论」S7 追加，本注释不重复
    # 摘抄以免和重放脚本的输出脱节。
    # 回滚：设 0（默认值即是 0，不生效）。
    "rule_menqing_baotou_enabled": 0,
    # 弃牌间 to_baotou_distance 每差 1 步的权重（只在
    # rule_menqing_baotou_enabled 打开时生效）。量级参考 pair=35、
    # ukeire=100——比它们大是因为 to_baotou 差 1 步通常意味着结构性差异
    # 更大（例如一个搭子是否已经成型），需要比"多一对子"更强的权重才能
    # 在同向听候选里稳定胜出。
    "menqing_baotou_weight": 200,

    # 2026-09-28 任务：持财神门清手的「多路线期望值」打分（mj/route_ev.py）。
    # 依据：强手 4 番以上大牌里「七对·爆头」（4番）出现最多，我们每 100 次
    # 胡牌只有 0.5~1.7 次、强手 2.3~4 次——七对路线门槛
    # （mj.shanten.pair_route_allowed 要求 ≥5 真实对子且严格更快）让「6对+
    # 1财神」这个摸任意牌都胡的 4 番型基本走不进去。本开关只在门清+持财神
    # 时，用统一的路线 EV（标准/标准爆头/七对/七对·爆头，各自算「剩余摸牌内
    # 走完的概率 x 走完的得分」）替换分块打补丁的旧逻辑；只有最优路线是
    # 七对系且比原逻辑的选择高出 rev_margin 才改写弃牌，否则行为不变。
    # 默认关闭；离线验证见 tools/route_ev_check.py，测试见
    # tests/test_route_ev.py。回滚：设 0（默认值即是 0，不生效）。
    "rule_route_ev_enabled": 0,
    # 改写弃牌所需的最小优势比例：EV(七对系最优候选) >= (1+rev_margin) x
    # EV(原逻辑会选的那张) 才改写，避免路线 EV 的估计噪声导致来回摇摆。
    "rev_margin": 0.10,
    # 庄/闲自摸赔率（fan x 该值 = 收入），与 mj.rules.payout 的真实赔率一致，
    # 独立成 rev_ 前缀权重只是为了这条路线 EV 内部可单独调、不影响其它地方
    # 对 payout() 的直接调用。
    "rev_payout_dealer": 24,
    "rev_payout_idle": 10,
    # models/rollout_cal.json 里 hazard 表缺少对应摸牌次数时的兜底危险率
    # （对手一次摸牌自摸的概率），取全表量级的中位数附近。
    "rev_hazard_default": 0.05,

    # 2026-09-28 追加：docs/IMPL_UNIFIED_EV_V2.md（统一账本 v2）。P1~P5 各自
    # 独立开关、默认关闭；关闭时与 rule_route_ev_enabled 首版行为逐一致
    # （tests/test_route_ev.py 的随机对比测试锁定）。
    #
    # P1 输钱项：账本加入"别家先自摸"的付出（win/lost 由 mj.route_ev._win_and_lost
    # 同时给出），并把"是否改写/否决"的判据从比例门槛改为差值门槛
    # （新-旧 >= max(rev_margin_abs, rev_margin x |旧|)），避免账本出现负值/
    # 小分母时旧的比例门槛失真。回滚：设 0（默认值即是 0，不生效，判据保持
    # 首版的比例门槛）。
    "rev_loss_enabled": 0,
    "rev_margin_abs": 0.5,
    # 庄/闲付出：rollout_cal.json 的 |pay_d|/|pay_n| 量级（本次未接入
    # rollout_cal 的 pay_n/pay_d 字段，按 P1 任务书给的缺省实测量级写死，
    # 可在 models/weights.json 按最新回放数据覆盖）。
    "rev_loss_dealer": 10.33,
    "rev_loss_idle": 4.73,

    # P2 速度前瞻：全部手牌（不限门清/财神），只在同向听 tier 内换牌，不改变
    # 向听。适用 chain=0、piao=0、非抓打圈、tier 向听 <=1。剪枝只对前
    # rev_speed_topk 个一步活牌数最多的候选做前瞻。回滚：设 0。
    "rev_speed_enabled": 0,
    "rev_speed_joker": 0,   # P2 是否也管持财神手（默认否：财神手由 route EV C/D/B 与拟合打分负责）
    "rev_speed_topk": 4,
    # 单次决策新增逻辑（P2 前瞻等）合计耗时预算，超预算立即放弃并回落原逻辑。
    "rev_time_budget_ms": 150,

    # P3：门清持财神范围内，最优路线为 B（标准爆头）时也允许改写弃牌/否决
    # 吃碰（原来只认 C/D）；B 路线距离 2 时的进张速率改用真实"能让
    # to_baotou_distance 下降"的活牌数，替代距离>=2 时借用标准向听 ukeire
    # 速率的粗糙近似。回滚：设 0。
    "rev_override_b_enabled": 0,

    # P4：财飘续飘 / 非爆头弃胡转爆头，改用按账决策替代
    # mj.hu_strategy.choose_hu_or_piao 原有的固定阈值（success_floor /
    # high_fan_force / chain_rate 系列）与 S1 的财神数x副露数格子表；硬性
    # 护栏（piao_force_hu、draws_left 墙尾闸门）保留不变。回滚：设 0。
    "rev_piao_enabled": 0,
    "rev_decline_enabled": 0,

    # P5：吃碰最前面整体比较"不吃碰"与"吃碰后打最优一张"两笔账（副露+1，
    # 七对路线作废，含 P2 前瞻与 P1 输钱项），账本有明确意见时可以否决原逻辑
    # 会接受的吃碰、也可以接受原逻辑会拒绝的吃碰；账本没有明确意见（差值不
    # 超过门槛）时回落原逻辑（含 rule_route_ev_enabled 的 claim_route_veto）。
    # 回滚：设 0。
    "rev_claim_enabled": 0,

    # 2026-09-28 追加（docs/IMPL_TABLE_EV.md，统一账本 v3 T1/T3；T2/T4 下一轮）。
    # 现有账本三处已知错误里的两处本轮修：
    #   1) 付出项用平均数（rev_loss_dealer/idle）——闲家时庄胡我付 8×番、另一
    #      闲胡我只付 1×番，8 倍之差被平均数抹掉。T1 用 tools/hazard_fit.py
    #      产出的 models/hazard_table.json::fan_by_role 按"谁是庄"精确加权。
    #   2) 没算庄位价值——胡者接庄，自己每次胡牌都额外值 V(局号)，第 8 局
    #      V=0。T3 用 tools/dealer_value.py 产出的 models/dealer_value.json。
    # （第三处"对手速度固定 (1-h)^3"是 T2，下一轮做，本轮只写文档。）
    # 两者都只进入按账分支（best_route/P2 前瞻/P4 飘与 S1/P5 吃碰），
    # rev_piao_enabled/rev_decline_enabled 关闭时仍走旧阈值逻辑不受影响；
    # 缺表/缺局号/JSON 损坏一律回落等价于关闭。回滚：设 0。
    "rev_opp_pay_enabled": 0,
    "rev_dealer_value_enabled": 0,
    # models/hazard_table.json 缺失或 fan_by_role 某个角色缺失时的兜底番值
    # （任务书给的量级：庄/闲胡牌平均番都在 1.3 附近）。
    "rev_fan_default": 1.3,

    # 2026-09-28 追加（docs/IMPL_ROUTE_CAL.md，统一账本 v4-C1，路线概率校准）。
    # 现有账本"走完的概率"这一侧用理论估算（p 按当前状态固定不变、七对摸
    # 财神不算推进），route_ev_check 的校准显示七对系被系统性低估——这也是
    # P1/T3 一打开就把七对压到 0、被迫关掉的根因。本开关打开后，
    # best_route/P2 前瞻/P5 吃碰查 tools/route_sim_fit.py 离线模拟产出的
    # models/route_cal.json（"坚持走某条路线"的完成曲线），查不到/表缺失
    # 一律回落理论 DP，逐一致行为不变。回滚：设 0。
    "rev_route_cal_enabled": 0,
}


# ---------------------------------------------------------------------------
# G2.2（docs/SIM_FOUNDATION_REPORT.md，阶段一模拟决策地基）：按座位覆盖权重，
# 供 tools/arena2.py 在同一进程里让 4 个座位用不同配置对局，不用真的写 4 份
# weights.json 来回切换文件。唯一允许触碰生产决策路径的改动——未启用（不
# 使用 weights_overlay/frozen_file_weights 这两个上下文管理器）时，
# load_weights() 的返回值与改动前逐字节相同，见 tests/test_weights_overlay.py
# 的对照测试。
# ---------------------------------------------------------------------------
_OVERRIDE = None            # 当前生效的覆盖字典；None = 不覆盖（默认状态）
_FROZEN_FILE_WEIGHTS = None  # 冻结的文件权重快照；None = 不冻结，照常每次读文件
_TLS = threading.local()    # 线程级覆盖：实战按房随机 A/B 里，同一进程的多个对局线程各自带着自己房的配置


class thread_weights_overlay:
    """线程级覆盖（只影响调用它的这个线程，``mj.bot.play_game`` 用它给"这一房"的配置）。叠加在全局 ``weights_overlay``
    之上（线程级优先）。没人使用时 ``load_weights()`` 的返回值与改动前逐字节相同。"""

    def __init__(self, overlay):
        self.overlay = overlay
        self._prev = None

    def __enter__(self):
        self._prev = getattr(_TLS, "override", None)
        _TLS.override = self.overlay
        return self

    def __exit__(self, exc_type, exc, tb):
        _TLS.override = self._prev
        return False



class weights_overlay:
    """上下文管理器：``with weights_overlay({"rule_route_ev_enabled": 1}):``
    块内所有 ``load_weights("models/weights.json")`` 调用都会在基础配置
    （默认权重 + 文件/冻结快照）上再叠加这份覆盖（覆盖优先级最高）。支持
    嵌套（退出时恢复上一层，不是恢复成 None）。"""

    def __init__(self, overlay):
        self.overlay = overlay
        self._prev = None

    def __enter__(self):
        global _OVERRIDE
        self._prev = _OVERRIDE
        _OVERRIDE = self.overlay
        return self

    def __exit__(self, exc_type, exc, tb):
        global _OVERRIDE
        _OVERRIDE = self._prev
        return False


class frozen_file_weights:
    """上下文管理器：对战开始时读一次 ``models/weights.json``，块内所有
    ``load_weights("models/weights.json")`` 调用都用这份快照，不再重复读
    文件——避免长时间对战里用户中途改文件导致前后局配置不一致，也省掉
    重复磁盘 I/O。只影响默认路径 ``models/weights.json``；显式传别的
    ``path`` 调 ``load_weights`` 不受影响。

    ``preloaded``：跳过文件 I/O，直接用给定的权重字典当"快照"——单元测试用
    这个参数（tests/test_weights_overlay.py），不去读真实文件（MJ_WEIGHTS_
    NO_FILE=1 的测试环境本来就不该有任何代码路径去碰 models/weights.json，
    这条路径也不例外）；``tools/arena2.py`` 之类的真实调用方不传，走文件。"""

    def __init__(self, path="models/weights.json", preloaded=None):
        self.path = path
        self.preloaded = preloaded

    def __enter__(self):
        global _FROZEN_FILE_WEIGHTS
        self._prev = _FROZEN_FILE_WEIGHTS
        if self.preloaded is not None:
            _FROZEN_FILE_WEIGHTS = dict(self.preloaded)
            return self
        try:
            with open(self.path, encoding="utf-8") as source:
                report = json.load(source)
            _FROZEN_FILE_WEIGHTS = report.get("weights") or {}
        except (FileNotFoundError, json.JSONDecodeError):
            _FROZEN_FILE_WEIGHTS = {}
        return self

    def __exit__(self, exc_type, exc, tb):
        global _FROZEN_FILE_WEIGHTS
        _FROZEN_FILE_WEIGHTS = self._prev
        return False


def load_weights(path="models/weights.json"):
    # 冻结快照优先于 MJ_WEIGHTS_NO_FILE：frozen_file_weights(preloaded=...)
    # 本来就不碰真实文件，测试环境（MJ_WEIGHTS_NO_FILE=1）下用它验证"冻结后
    # 用快照"这条路径是安全的，不违反"测试不许读真实 weights.json"的原则。
    # 两者都不启用时（_FROZEN_FILE_WEIGHTS is None，默认状态），下面这段的
    # 分支顺序和返回值与改动前逐字节相同。
    if path == "models/weights.json" and _FROZEN_FILE_WEIGHTS is not None:
        base = {**DEFAULT_WEIGHTS, **_FROZEN_FILE_WEIGHTS}
    elif path == "models/weights.json" and os.environ.get("MJ_WEIGHTS_NO_FILE") == "1":
        base = dict(DEFAULT_WEIGHTS)
    else:
        try:
            with open(path, encoding="utf-8") as source:
                report = json.load(source)
            base = {**DEFAULT_WEIGHTS, **(report.get("weights") or {})}
        except (FileNotFoundError, json.JSONDecodeError):
            base = dict(DEFAULT_WEIGHTS)
    tls = getattr(_TLS, "override", None)
    if tls:
        return {**base, **(_OVERRIDE or {}), **tls}
    if _OVERRIDE is not None:
        return {**base, **_OVERRIDE}
    return base


def fit(samples_path="models/decision_samples.json", output="models/weights.json"):
    with open(samples_path, encoding="utf-8") as source:
        samples = json.load(source)
    stats = Counter()
    for sample in samples:
        if sample.get("baseline_match"):
            stats["baseline"] += 1
        if sample.get("route_match"):
            stats["route"] += 1
        if sample.get("actual") == "白":
            stats["discard_joker"] += 1
    total = len(samples) or 1
    weights = dict(DEFAULT_WEIGHTS)
    if stats["route"] < stats["baseline"]:
        weights["pair"] = 20
        weights["joker"] = 120
        weights["chain"] = 350
    report = {"samples": len(samples), "stats": dict(stats), "weights": weights,
              "baseline_rate": stats["baseline"] / total, "route_rate": stats["route"] / total}
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
    return report
