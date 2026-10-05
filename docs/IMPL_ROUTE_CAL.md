# 任务：路线概率校准（统一账本 v4-C1）

项目 /Users/yuanye/coding_workspace/mahjong，Python 3.9 标准库。10/8 12:00 提交，之后不能改。
第一步：把本 prompt 全文写入 docs/IMPL_ROUTE_CAL.md，再开始实施。

硬性约束
- 开始跑任何测试或脚本前，先执行 `pgrep -f "mj.bot"`。有输出说明 bot 正在打牌：这时不准跑任何测试或脚本，只写代码，最后在交付里注明"测试未跑，等 bot 停"。已经发生过测试抢 CPU、导致 bot 一房 65 次超时。
- 不要改 models/weights.json；不要启动 bot；全量扫描和拟合由用户执行。
- 认人一律用 tools/mining_common.py::seat_names(game)（我们 user_id u_fd06550b5fb3）。
- 新开关在 mj/fit.py 默认权重里加，默认 0；异常一律吞掉回落原逻辑；受 rev_time_budget_ms 约束。

背景（为什么做）
mj/route_ev.py 的路线账：每条路线期望 = 走完的概率 × 番 × 收入 − 付出。"得多少分"这一侧已经基本算对（番、收入、付出、庄位价值），"走完的概率"这一侧不准，是现在的主要瓶颈：
- _routes / _win_and_lost 用理论估算：p = 前进牌活牌数 / 未见牌数，并假设每一步 p 不变；d=0 时 q = 听口活牌数 / 未见牌数。
- 已知问题：
  (1) p 按当前状态固定不变，没有反映走完一步后前进牌会变化（七对：单张越来越少，前进牌越来越少；标准：不同向听的前进牌差别很大）；
  (2) _pair_baotou_ukeire 明确跳过财神，摸到财神对七对·爆头的推进没算；
  (3) route_ev_check 的校准段显示：模型认为七对希望最低的那一档，实际仍有 10.7% 胡成七对（高手 22%），说明七对被低估。
- 后果：P1 付出项、T3 庄位价值这些原理正确的项一打开，就放大了对慢路线的低估，七对被压到 0、平均番下跌，所以都被迫关掉了。概率不校准，账上加任何项都会更偏。

方法：离线模拟"坚持走某条路线"，得出真实的走完概率曲线；在线查表替换理论估算（离线深算、在线快用）。

========== C1a 模拟拟合工具 tools/route_sim_fit.py → models/route_cal.json ==========

采样状态
- 从对局文件的每个弃牌决策点（高手 + 我们 + 其他，seat_names 认人），取出决策者当时的 13 张（弃牌后）、副露组数、可见牌（自己手牌 + 全部弃牌 + 全部副露）、牌墙剩余。
- 按文件名 md5 分组：md5 % 5 != 0 的文件用于拟合，== 0 的留作检验（C1b 用）。
- 参数 --states N（默认 4000），分层抽样：保证每个 (路线, d) 组合都有样本。

对每个状态、每条可行路线 R ∈ {A 标准, B 标准爆头, C 七对, D 七对·爆头}，做 K 次模拟（--rollouts，默认 24）
- 牌池 = 未见牌（136 − 可见），按均匀分布随机摸；不模拟对手，别人先胡的风险仍由现有 hazard 负责。
- 每一步：摸 1 张，判断 R 是否完成（R 的距离函数到达"胡"），完成就记下步数；否则按"坚持 R"贪心弃牌：选弃后 R 距离最小的；距离相同时，选弃后 R 前进牌活牌数最大的（沿用 mj/route_ev.py 里的距离函数和前进牌函数，财神按规则当百搭，摸到财神要能推进路线）。
- 最多 18 步（本家整局最多约 16 摸）。
- 结果：每个状态、每条路线得到累计完成曲线 F_R(k)，k = 1..18。

聚合成表，键 = (路线 R, 距离 d, 当前前进牌活牌数分档, 手上财神数 0/1/2+, 副露组数 0/1/2+)
- 活牌分档：0-2 / 3-5 / 6-8 / 9-12 / 13-16 / 17+。d 只做 0..4，超出的回落理论估算。
- 每个键存平均完成曲线 F(k)（k = 1..18）和样本数；样本 < 30 的键按 副露 → 财神 → 活牌分档 的顺序逐级回退，把回退链写进 json。
- 打印对照表：每个 (R, d) 下，理论估算（现有 _win_and_lost，不含 survive）给出的 F(4)、F(8)、F(12)，和模拟值并排。

性能与进度
- multiprocessing.Pool；每 100 个状态打印一次进度和预计剩余时间。
- 距离函数、前进牌函数都用 lru_cache（键为 counts 元组）。
- 目标：--states 4000 --jobs 8 全量 ≤ 30 分钟。超出就把 --rollouts 默认值调低，并在交付里说明。
- 增量缓存放 tools/.cache/（参考 tools/batch_dashboard.py 的写法），键 = 状态 + 代码版本。

========== C1b 检验工具 tools/route_cal_check.py（只用留出文件）==========

对留出文件里每个门清且持财神的弃牌决策点，分别用"理论估算"和"查表"两种方式，算出每条路线的胜率（含 survive，即现有 _win_and_lost 的 win）。输出：
1. 路线级校准：模型认为最优路线是 C 或 D 的点，分档列 预测七对系胡率 vs 实际七对系胡率，两种模型并排，分 高手 / 我们。
2. 整体校准：最优路线胜率分 10 档，列 预测 vs 实际胡牌率（任意路线），两种模型并排，分 高手 / 我们。
3. 汇总：两种模型的 Brier 分数和对数损失，数值越低越好。

========== C1c 在线接入（开关 rev_route_cal_enabled）==========

- mj/route_ev.py 新增查表函数：输入 (R, d, 活牌数, 财神数, 副露数)，按回退链找到完成曲线 F(k)。
- 开关打开且查到表时，用曲线替换 DP：
  - 第 k 步完成的概率 f(k) = F(k) − F(k−1)；
  - S(k) = 前 k 步 survive 连乘（沿用 _survival_series）；
  - win = Σ_k f(k) × S(k)；
  - lost = Σ_k (1 − F(k−1)) × S(k−1) × (1 − s_k)；
  - lost_pay 同理，按 pay_per_step 加权；
  - 只累加到 n_draws 为止。
- 查不到或表缺失 → 回落现有 _win_and_lost，行为与关闭完全一致。
- 接入 best_route、_speed_candidate_value（P2）、claim_ev_decision（P5）。
- models/route_cal.json 与其他表一样，只在进程启动时加载一次（在交付说明里写明：生成表之后要重启 bot）。

========== 测试（MJ_WEIGHTS_NO_FILE=1，追加到 tests/test_route_ev.py）==========

- 开关关闭：tests/test_rule_switches.py 的固定校验值不变。这是唯一的一致性证明，不要拿新代码和新代码对比。
- 表缺失时与关闭一致：测试里把缓存设为 {}，不要设 None（设 None 会去读真实文件；上一轮就有两个测试因此失败）。
- 构造一张人工表（F 已知），验证 win、lost 与手算一致；验证 win + lost + 剩余 = 1。
- 回退链：稀疏键能回退到粗键。
- 性能：查表路径下，单次 best_route 耗时不增加。
- 只运行：tests.test_route_ev tests.test_rule_switches tests.test_responses tests.test_strategy tests.test_ev tests.test_hu_strategy（先 pgrep 确认 bot 没在跑）。

========== 交付 ==========

1. 改动清单 + 测试输出尾部。
2. 给用户的命令（bot 停着时跑）：
   python3 tools/route_sim_fit.py --states 4000 --jobs 8
   python3 tools/route_cal_check.py --jobs 8
   让用户把两份输出都贴回来。先看 C1b：只有查表模型的 Brier 分数和对数损失都比理论估算低、七对系不再被低估，才启用。
3. 启用方式：models/weights.json 加 rev_route_cal_enabled=1，然后重启 bot。之后按顺序逐个重新打开 rev_loss_enabled → rev_opp_pay_enabled + rev_dealer_value_enabled，每步 5 房，用 batch_dashboard 验收：平均番不跌、七对占胡约 2%、强手桌分/局不变差。
4. 如实说明：模拟不含对手吃碰打乱摸牌顺序，也不含"中途换路线"的灵活性；完成曲线是"坚持一条路线"的下限估计。
