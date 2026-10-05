# 麻将 AI 交接文档（HANDOFF）

> 给下一个接手的 AI / 评审者。最后更新：2026-09-20（重写 + 两轮独立评审修订）。
> 读法：**TL;DR → ○ → 七.①（阻断项）** 是必经路径；其余按需查。原版按时间追加的实验流水已压缩进附录 A。

---

## TL;DR — 今天先做这三件事

1. **确认令牌**（30 分钟，最高优先）：令牌状态记录自相矛盾（见 ○.3），且 **10/8 之后才能领参赛令牌**（要等组委会建赛事）。先把全局令牌跑通一次实战验证链路。
2. **建离线基线**（10 分钟）：`python -m mj.arena` —— 无参数即跑 2v2 对照。**这是全文最可用的一条命令**，不需要令牌、不需要网络。任何策略改动前后各跑一次。
3. **决定正式赛怎么打**（今天拍板，见第七节①）：
   - 方案 A **人工兜底**：门户点「报名」→ 人工点「资格确认」→ bot 用 `--room <赛事id>` 直连打 running 场。**成本 ~0，今天就能演练**。
   - 方案 B **补状态机**：`api.register()` + `stage_open/stage_done/qualified` + `--wait` 去上限。**成本 ~1 天，全自动**。
   - ⚠️ 现状 `--wait` 在 `stage_done` 后会**静默空转到 7200s 才退出**，看着像"在跑"，实为挂死。

---

## 〇、这是什么 · 当前语境

在线麻将 AI（平台规则变体：白板=财神百搭、只能自摸、直上三连庄 ×8）。平台 API `https://10.240.169.190:18080`（自签证书）。

### 0.1 比赛语境（决定优先级）

杭州麻将 AI 竞技赛：**10/8 12:00 提交代码+使用说明即报名截止** → 10/10 19:30 第一轮 → 10/12 15:00-18:00 决赛圈（3 轮连打，可能顺延）。提交的是**那一刻的代码**，所以 **10/8 = 策略冻结日**，真实实验窗口约 2.5 周。

提交物两项缺一不可：①《程序使用说明》放**申报页面正文**（怎么接入平台/怎么启动/依赖环境）；②源码二选一（仓库地址贴正文，或含 `.git` 的 <20M 压缩包）。**说明文档面向组委会，不要复用本文档的内部口吻。**

### 0.2 两条接入路线（别搞混）

| | 自由匹配（日常在用） | 正式锦标赛（比赛中） |
|---|---|---|
| 令牌 | **全局令牌**（门户「我的 AI 身份」） | **参赛令牌**（门户「报名」派发，scoped 绑赛事） |
| 入口 | `POST /api/match` | `POST /api/tournaments/{tid}/register` + `ready` |
| 发现场次 | `match` 直接返 `room_id` | 轮询 `/api/me` 的 `active_games` |
| 生命周期 | 单场 10 局，打完房间关停 | **多阶段状态机**，打完一阶段 ≠ 结束 |
| 我方支持 | ✅ 完整 | ⚠️ 见第七节①（**但 `--room <赛事id>` 直连已验证可用**，历史赛事 `t_65d538e905c5` 即此路径） |

> **仓库里 s1-s55 全部实战样本来自左列**（自由匹配）。右列的完整多阶段流程**尚未端到端跑过**。

### 0.3 令牌（⚠️ 记录自相矛盾，待你确认）

平台只存令牌的 **SHA-256**，明文**只显示一次**，丢了只能重新生成（旧令牌立即失效）。

- 签发入口：全局令牌 → 门户「我的 AI 身份」（可轮换）；参赛令牌 → 门户「赛事大厅 → 报名」（一次性弹窗）或「我的赛事 → 重新生成令牌」（`POST /portal/api/tournaments/{tid}/token`）。
- 参赛令牌**不能**调 `/api/match`（400 `TOKEN_NOT_SCOPED`）；全局令牌**不能**报名（403 `PORTAL_BINDING_REQUIRED`，v24 起匿名注册已删）。
- **矛盾点**：工作区版本的本文档记「`dc4b4a72`（2026-09-20 签发）为日常默认，`eec61e62` 已轮换作废」；但 **HEAD 提交 `97c0c66` 记「`eec61e62` 日常默认、`676faa…` 赛事专用（绑 `t_65d538e905c5`）、`ed0f8b4d` 可能仍有效」**。两版不一致，且 `676faa` 全令牌在仓库中**已不可恢复**（只存了前缀）。
  → **接手第一件事：用 `tools/probe_tokens.py` 逐个验活，确认哪个能用，并统一两处记录。**
- 平台当前 **`GET /portal/api/tournaments` 返回空**（我已直连核实，2026-09-20）——正式赛尚未建，**所以现在一个参赛令牌都拿不到**，最早 10/8 之后。

### 0.4 依赖与运行环境

**纯标准库，零第三方依赖**（Python 3.9+）。`gc.disable()`（`bot.py:10`）—— 搜索层靠 `lru_cache` 复用，GC 扫描是延迟毛刺源。

```bash
# ① 自由匹配（**必须带 --once**，见下）
python -m mj.bot <全局令牌> --once

# ② 指定房间重打（赛事直连也用这个）
python -m mj.bot <令牌> --room a_xxxxxxxxxxxx --once        # 自动房
python -m mj.bot <令牌> --room t_65d538e905c5 --once        # 赛事房

# ③ 平台分房赛（轮询 me() 等分房）—— ⚠️ 默认 7200s 上限
python -m mj.bot <参赛令牌> --wait

# ④ 测试房（4 令牌一整场）
python tools/run_test_room.py <t1> <t2> <t3> <t4>

# ⑤ 离线对照（无需令牌/网络，最易成功）
python -m mj.arena                    # 2v2 座位对拍
python -m mj.arena grid <批次数>       # 权重网格（**必须给批次数**，否则默认 40 批，耗时不可预期）

# ⑥ 测试
python -m unittest discover tests     # 唯一入口（无 pytest 配置）
```

**⚠️ 命令①的隐含前提（重要）**：`python -m mj.bot <令牌>` **漏掉 `--once` 会无限连场**——退出条件只有 `if args.once or args.room: return`（`bot.py:516`）。**原僵尸 bot 事故（9/18 凌晨整夜自动连场）修的是 `--room` 那条路径；自由匹配无 `--once` 仍然会一直循环。** 铁律：任何 bot 启动必须带终局退出条件。

**命令④的前提**：需要 **4 个绑定同一 test 锦标赛的参赛令牌**（`apis[0].me()["tournament_id"]` 校验），各自在门户「测试房间」报名一次派发、明文只显示一次。**当前没有参赛令牌 → 这条命令现在跑不了。**

**portal 对账链的前提**：`portal_cookie.txt`（gitignored，2026-12-13 到期）里的 `majiang_sid` 需**从浏览器开发者工具复制**（登录门户后取 Cookie）。到期即断链，需重新复制。

---

## 一、工程现状

### 1.1 代码规模

`mj/` 全目录 **2954 行**；**真正参与决策的 13 个文件合计 1820 行**。

### 1.2 测试

**30 个 `test_*.py` / 109 个 `test_` 函数**（原文档写的 104 已过时）。`test_bot_state.py` 最多（15 个，覆盖主循环状态机）。离线管线全链路都有测试（pipeline/data/samples/fit/ab/compare/benchmark/gamesim/timing/notify/security）——**这是答辩「完整性」栏的硬材料**。

### 1.3 三个已知债

**① 两份平行评分体（最大风险）**
`strategy._discard_score`（`strategy.py:33`，`b_*` 权重族）与 `ev.route_value`（`ev.py:34`）是两套骨架相同、但**各自独立实现**了字牌/七对/豪华门票/chain/piao/defense 项的评分函数。**改一处漏一处已有先例**（豪华门票就是两条路径各加一次才生效）。任何评分改动必须**同时**改两处，或用 `rules["_weights"]` 注入绕开。

**② CRLF 污染**
`mj/ev.py`、`mj/shanten.py`、`mj/strategy.py`、`mj/weight_fit.py` 工作区是 CRLF，HEAD 是 LF。其中 **`shanten.py` 与 `weight_fit.py` 在 `git diff --ignore-all-space` 下差异完全消失 = 零实质改动**（纯换行符）；`ev.py`(+3)、`strategy.py`(+4) 有实质改动。提交前需 `dos2unix` 或加 `.gitattributes`，否则 review 被整文件改写淹没。

**③ `api.py` 缺 `register()`**
现有方法：`me/ready/rules/tournament/match/state/notify/action/version` + 静态 `test_events`。**唯独缺正式赛必需的 `POST /api/tournaments/{id}/register`。**

### 1.4 令牌明文泄漏点（⚠️ 比文档原先写的严重）

`mj/security.py` 的正则扫描（`[a-f0-9]{64}`）**实际命中 3 个文件**：

```
./tools/room_status.py
./tools/probe_tokens.py
./tools/server_latency_probe.py
```

`docs/RELEASE.md` 的验收项是 `secret_hits=[]` —— **清掉一处达不到，必须三处全清**。另外 **`mj/security.py` 没有 `__main__`**，要手动跑：

```bash
python -c "from mj.security import scan; print(scan('.'))"
```

---

## 二、架构

### 2.1 模块地图

```
                        ┌─────────────┐
                        │  mj/bot.py  │  523 行 —— 唯一有状态的编排层
                        └──────┬──────┘
   ┌──────────┬──────────┬────┴────┬──────────┬──────────┐
   ▼          ▼          ▼         ▼          ▼          ▼
ev.py    strategy.py  responses  defense  hu_strategy  rules.py
(路线EV)  (基线评分)   (吃碰杠)   (读牌)   (胡/飘)     (番数判定)
   └────┬─────┘          │         │         │          │
        ▼                ▼         │         │          │
   joker_ev.py ────► shanten.py ◄──┘         │          │
   (白板枚举)        (向听/进张)              │          │
        └────────────────┴─────────┴─────────┴──────────┘
                                  ▼
                          tiles.py  (34 维 counts)
                          fit.py    (权重表)
   ─────────────────────────────────────────────────────
   api.py (HTTP/限速)   logging.py (JSONL)   notify.py (SSE)
```

**纯函数核心**：`ev / strategy / responses / defense / hu_strategy / rules / shanten / joker_ev / tiles` 全部是「快照 dict 进、决策 dict 出」的无状态纯函数，**没有一个类**。只有 `bot.py:play_game` 持有循环状态（`responded`/`rejections`/`pending_gang`/`opp_chain`），`api.py` 持有连接与限速器。这个设计让 109 个单测和离线 arena 对局能脱离网络跑。

| 模块 | 行数 | 职责 | 关键函数:行号 |
|---|---|---|---|
| `bot.py` | 523 | 编排：主循环、动作判定、异常恢复、匹配/等分房 | `play_game:224` `choose_action:158` `choose_discard:42` `_gang_bomb_choice:138` `match_with_retry:382` |
| `rules.py` | 160 | **规则真相**：面子拆分+财神补缺、七对/豪华组数、爆头 34 张全判定、番数连乘 | `win_standard:67` `seven_pairs:83` `baotou:106` `evaluate:118` |
| `shanten.py` | 160 | 向听/进张搜索（财神参与），双路线合并 | `_search:14` `combined_route:144` `ukeire:113` `pair_ukeire:131` |
| `defense.py` | 127 | 逐对手 need 模型 → 弃牌危险度 | `opp_stats:26` `penalties:85` |
| `responses.py` | 117 | 吃/碰/杠声明，`CLAIM_MODE` 实验开关 | `choose_gang:36` `choose_peng:53` `choose_chi:79` `_mode_gate:65` |
| `ev.py` | 103 | 路线 EV：向听分层 + 活度加权进张 + 番值项 | `route_value:34` `choose_route_discard:89` |
| `strategy.py` | 99 | 基线评分（`b_*` 权重族） | `_discard_score:33` `choose_discard:85` |
| `joker_ev.py` | 81 | 白板角色枚举：留白爆头弹性 vs 全熔面子 | `joker_plan_value:65` `hand_all_wait:28` |
| `hu_strategy.py` | 54 | 爆头摸白的「直接胡 vs 弃白续飘」EV | `choose_hu_or_piao:28` `_piao_success_rate:19` |
| `tiles.py` | 61 | 牌码 ↔ 34 维 counts，全桌可见牌统计 | `to_counts:23` `visible_counts:30` |
| `fit.py` | 65 | 权重表唯一真相（**24 旋钮**）+ `load_weights:34` | `DEFAULT_WEIGHTS:6` |
| `api.py` | 151 | HTTP：thread-local 连接、令牌级限速器、429 退避 | `_pace:47` `request:71` `state:125` `action:134` |
| `logging.py` | 119 | JSONL 旁路日志，11 种事件 kind | `append:16` `action:26` `hu_detail`(bot.py 侧) |

### 2.2 一次出牌的决策链

```
play_game (bot.py:224)              # 主循环：短轮询 0.3s + SSE 唤醒
  └─ _fetch_snapshot → /state?seq=0 # 每轮拿全量快照（不消费事件流）
     └─ choose_action (bot.py:158)
        ├─ phase=="draw" && turn==seat
        │   ├─ can_hu → _gang_bomb_choice (杠爆 ×4 换 ×2)   bot.py:138
        │   │          └─ choose_hu_or_piao                 hu_strategy.py:28
        │   ├─ catch_restricted → 只能打刚摸的牌（抓打圈）   bot.py:169
        │   ├─ choose_gang                                  responses.py:36
        │   └─ choose_discard (bot.py:42)  ← 走两条评分路径之一
        │        ├─ use_route = 抓打圈 ∥ 链 ∥ 飘 ∥ 有财必拷响 ∥ 坐庄
        │        │   ├─ True  → ev.choose_route_discard     ev.py:89
        │        │   └─ False → strategy.choose_discard     strategy.py:85
        │        └─ defense.penalties → rules["_defense"]   defense.py:85
        └─ phase=="response_peng"/"response_chi"
            └─ choose_peng / choose_chi                     responses.py:53,79
```

### 2.3 五个关键设计决策（为什么是这样）

**① 向听是硬约束，不是权重。** 两条评分路径共用同一骨架——先按 `route_shanten` 分层筛出向听最优候选，再在层内比评分：

```python
# strategy.py:90-99 / ev.py:101-103 同一个模式
currents[tile] = route_shanten(counts, meld_groups)   # 第一层：硬向听
best = min(currents.values())
tier = [t for t in unique if currents[t] == best]     # 最优层
return max(tier, key=lambda t: _discard_score(...))   # 第二层：评分
```

这是 `b_shanten=10000`（`fit.py:17`）成为「铁顶」的机制：花活（爆头/七对/链）只允许在**同等向听内**兑现，不许明显拖慢胡牌。**推论：要翻转层内仲裁，溢价必须 > 10000/步**——「权重旋钮无法翻转仲裁」的根因就在这里。**唯一反例**：`slow1` 调到 12000 确实翻转了，但 arena EV -224 更差（见第三节「未关闭的实验」）。

**② 双路线并行（标准 vs 七对）。** `combined_route`（`shanten.py:144`）取向听更近者定向听；同向听时进张取两路线**并集**（真两侧听）；深向听（>2）不算进张（性能护栏）。

**③ 快照驱动，事件流只当唤醒信号。** 主循环**只用 `/state?seq=0` 拿全量快照**（`bot.py:199`），完全不消费事件流；SSE `notify`（`notify.py:5`）只用来 `wake.set()`。换来的是无状态漂移风险和可离线重放；代价是每轮全量快照，靠 pacer 限速压 429。

**④ 令牌级全局限速器。** `api.py:_pacers`（`api.py:22`）是**类级**共享的令牌 → pacer 表，`_pace`（`api.py:47`）在锁内预留槽位、锁外睡眠，`DEFAULT_PACE_INTERVAL = 1.0/14.0`（`api.py:24`）。原因：10 个对局线程共用一个用户配额（`/state` 16/s/user），超限触发 429 重试链（`api.py:127` `retries=8, backoff=0.4`）把 state 延迟抬到 **422ms×k**，而回合计时只有 ~2 秒 → 超时被服务器托管（s44 幽灵南事故）。`pace_interval=0` 可关（离线测试用）。

**⑤ 日志是旁路，绝不炸主流程。** `DecisionLog.append` 吞掉 `OSError`（`logging.py:22`）——文件被同步盘/杀软锁定时丢一条记录而不挂掉对局线程（曾炸掉 4 个对局线程整场作废）。`action_rejected` / `fallback_sent` 让「客户端到底发过什么」全程可对账。

### 2.4 离线训练/评测层

```
materials              collect               extract            fit
测试房 finished 局 ──► models/events/*.json ──► 低噪声样本 ──┬─► weight_fit.py ──► 旋钮网格
(免认证端点)                                                └─► samples.py ─────► fit.py ─► weights.json
                    ↕ 被复用的对局引擎
              arena.py (四人对抗 · 真计分)  ←──►  gamesim.py (单人沙盘 · 无对手)
```

| 工具 | 干什么 | 关键限制 |
|---|---|---|
| `mj/arena.py` | **唯一真正的四人对抗场**：`play_hand:128` 四家手牌 + 副露 + `chi_take/peng_take` 声明轮询，`settle:118` 实战计分（庄 +24/-8×3；闲 +10、庄 -8、余 -1），`run_session:185` 赢家坐庄，`compare:202` 2v2 座位对拍，`baotou_grid:220` 权重网格 | **任何策略改动先在这里 A/B**。`SeatStrategy:25` 有 base/current/route/pure × real/all/peng/none 组合 |
| `mj/weight_fit.py` | **真正的参数拟合器**：`parse_segments:22`（`wall_remaining` 跳升 +5 切手、段内 ≥4 次弃牌才留）、`replay:72`（固定真实摸牌序）、`policy_discard:107`（独立复刻决策评分）、`evaluate_configs:137`（网格 ukeire/pair/joker/honor/progress） | **目标函数是听牌率**（`tenpai_rate`/`avg_tenpai_turn`，`weight_fit.py:168`），**不是得分 EV**——这解释了「权重层已达局部最优」：拟合目标本身不含得分。依赖 `models/events/*.json` |
| `mj/gamesim.py` | 单人自摸沙盘：`play(seed, strategy, max_draws=80):47`，一副牌墙、14 张起手、自动杠 | ⚠️ **无对手、无吃碰、无防守**。只验证「弃牌函数本身」，对喂牌/被自摸/庄链**完全盲**。`len(rest)<=20`（`gamesim.py:61`）只用于**禁止杠**，不是流局；循环在墙耗尽或 `max_draws=80` 用尽时以 `win:False` 结束。**gamesim 上的正收益必须去 arena 复核** |
| `mj/fit.py` | **不是拟合器**：只统计 baseline/route 命中计数，若 route 落后就硬编码改三个值（`fit.py:57-59`：pair=20/joker=120/chain=350） | 现 `models/weights.json` 是 samples=52 的产物，**只覆盖 8 键**；真正调校过的值在 `fit.py:DEFAULT_WEIGHTS`（24 键） |
| `mj/ab.py` / `benchmark.py` | 随机手牌比向听/进张/留白/七对（`ab.run:24`）；`wilson:6` 给胜负率加 95% CI | 有置信区间，不是拿单场方差当结论 |

**权重表的一个坑（死键）**：`DEFAULT_WEIGHTS` 里 `discard_joker: -5000`（`fit.py:16`）**无人读取**（只有 `special.py:28` 拿它当统计计数键）；`ev.py:83` 读的是 `weights.get("discard_joker_soft", -800)`，而 **`discard_joker_soft` 不在 DEFAULT_WEIGHTS** → 永远走 -800 硬编码回退。`baotou_faster: 2200`（`fit.py:26`）也是死键（只在 arena 网格里定义，无读取点）。**调这三个键不会有任何效果。**

---

## 三、当前策略栈（已验证配置，勿轻易回退）

`mj/bot.py:play_game` 主循环：快照短轮询（`/state?seq=0`，`POLL_MIN_GAP=0.3` @ `bot.py:193` + SSE 唤醒）。
死局防护：同局连续 10 次 404 → 弃局；房间 404 → 干净退出（曾防住平台故障）。

**弃牌评分**（`strategy.py` baseline + `ev.py` route，**坐庄/有链/抓打圈/有财必拷响走 route**，`bot.py:64-69`）：
- 双路线并行（`shanten.combined_route`）：标准向听与七对向听取更近者定向听，同向听进张取并集；深向听（>2）不搜进张
- `pair_shanten` 与平台判定一致：四张=两对、不要求七种异种（三豪华七对存在即铁证）；财神可配单张或自配
- **白板枚举**（`joker_ev.joker_plan_value`）：留 keep∈{1,J} 张白作爆头弹性 vs 全熔面子求速，取最优；全留白慢两步以上剪枝
- **听牌活度加权**（s42，`_wait_live_score`）：进张按**可见牌剩余张数**加权（剩 ≥2 记 1.0 / 剩 1 记 0.5 / 亮光死听记 0）。首秀 **+457 历史最佳**
- **豪华门票**（`baohua_ticket=800` @ `fit.py:30`，s53 上线，**待兑现**）：≥5 对的门清手里三张同种 = 豪华七对(×4)彩票，拆第三张罚 800 分（两条路径均生效：`ev.py:77`、`strategy.py:60`）。**arena 30 批三变体完全一致**——非接线 bug，是触发态极稀有（~1 场 1 次）。判据 = `hu_detail` 首次出现「豪华七对」（历史 643 条为 0）
- **向听主导**：`b_shanten=10000/步`（铁顶）；进张 `b_ukeire=100`（仅 `current<=2`，`strategy.py:41`）
- **孤立字牌先打 +300**（`b_honor=300`）；**白弃出 -800**（`strategy.py:72` 硬编码 / `ev.py:83` 走死键回退）

**吃碰门控**（`responses.py`，`CLAIM_MODE="all"` @ `responses.py:10`）：**全吃**（白除外、吃 ≤2 摊、墙尾 >4）。
> 实证过程值得记住：历史「向听+1 门控」因 `_meld_groups` 返回 4（座位数）**恒放行 = 实为全吃**；修复后真过滤实测 **-269/320 手**，全吃反而稳定占优。对称对照终审（9/18）：四座同模式 160 手/模式，all -112 vs plus1/flat/strict 均 -173（三种门控行为完全一致）。**声明过滤方向永久关闭**。

**摸白决策**（`hu_strategy.py`）：直接胡 vs 弃白续飘的结构化 EV（对手副露 ≥2 时偏保守）。

**防守读牌 v2**（`defense.py`）：逐对手 need 模型——副露 = 需求（吃 +3/碰 +2 每组）、牌河 = 放弃（−1/张）；冲刺对手（副露 ≥2）×2；邻接喂牌（±2 内）加成；对手弃过的字不再计碰风险。危险度 = 120×需求压力 + 60×剩余张，≤900，扣减弃牌评分。

### 已证伪 / 已回退（勿再试）

| 尝试 | 结论 |
|---|---|
| **财飘宣言**（爆头态打白博 ×4） | **证伪**。s47 实战 5/5 宣言全部无果，爆头赢从 ~1 次/局崩到 0，**0 胜/10 局 -273 历史最差**。「下一摸必胡」只属于**手里留白的爆头态**（白就是构成全听的百搭）；打白宣言 = 亲手拆全听。观察到的对手 fan4 是**幸存者偏差**（真听 ~1-3 张/34）。已回退并钉死回归测试。**教训：观察到的对手大牌先查幸存者偏差再查机制；「明显 +2 番」的操作先用 portal 事件流验证必胡承诺存在** |
| 冻圈弃白（`_should_freeze`） | **已移除**（用户决策）。白 = 百搭 + 全场唯一免吃碰安全牌，双重价值不该为防御丢弃 |
| 破庄冲刺（`opp_chain` 抑制慢速爆头） | 拉低胡率，已回滚（检测代码留在 `bot.py`，评分侧已移除） |
| 坐庄慢两步赌爆头（15000 档） | 已证伪回滚 |
| 深向听进张（`b_ukeire3`） | 噪声级无效（默认 0 关闭） |
| 权重微调（`b_*`） | 局部最优，网格+实战双证，`ukeire=200` 明显变差 |
| 链/飘方向 | **正式关闭**。267 次弃白仅 3 次 15 手内见链（2 次无关）——打白不种链，白触发（需摸白+爆头态）全历史 0 次 |

### 未关闭的实验

- **豪华门票**（s53-s55 三场）：豪华七对仍 **0** 次（稀有度高于预期），爆头/财飘无回退信号。继续观测，判据 = `hu_detail` 出现「豪华七对」
- **爆头频率差距**：我方 f2+ 11.7% vs 对手 ~22%，差距 ≈ 全部在**爆头频率**（我方 11%/胡 vs 对手 24%/胡；对手 63 次爆头赢 **92% 为持白式**，非宣言流）。权重旋钮全面排查后关闭：`slow1` 6000/9000 数学上不可能翻转仲裁（premium 必须 > `b_shanten` 10000/步）、`slow1` 12000 翻转但 **arena EV -224 更差**、`faster` 是死键、`no_slow` 只覆盖白完全冗余态。arena 自娱爆头率 19.2% 证明**策略可达**，真实桌 11% 受对局环境影响。**结论：非权重可修项，接受并持续观测**

---

## 四、实证结论（勿重复实验）

| 实验 | 结论 |
|---|---|
| 吃碰激进度 | none < peng < 不劣化 < 向听+1 ≈ **无脑全吃**（loose 已上线，再激进无增益） |
| **吃碰门控真伪 (9/18)** | 修复 `_meld_groups`（原返回 4=座位数→恒放行）后真过滤实测 **-269/320 手** → 生产改全吃；七对保护/存活逃逸均被竞技场证伪移除 |
| **对称对照终审 (9/18)** | 四座同模式 160 手/模式：all -112 vs plus1/flat/strict 均 -173，**全吃净优 +61** |
| 白板留将枚举 | 回放听牌率 0.477→0.500、均听 6.01→5.91；s18 rank1（初步正收益） |
| 防守 v2 need 模型 | s18 实战：喂牌/点炮支出 333→209，净分 +1→**+123 rank1** |
| 听牌活度加权 | s42 **+457（9 胜/10 局）历史最佳** + s43 +23 → 两连胜 |
| **1024杭麻竞技二测 (s20, -177)** | postmortem 实证：① freeze 修复守住 ② 吃碰门控漏对子路线（228 声明中 42 次打在七对潜手，已修）③ **对手庄家自摸 = 我方支付 51%**（320/627）：33 手庄赢中 28 手是故意的 fan1 快攻滚庄位，听牌时机/等张宽度我方与赢家无差（59.3 vs 57.0 墙数）——**非失误源** |
| **杠爆窗口 (v32/v33 情报)** | 爆头态摸成暗杠四张且杠后仍听任意 → gang 换 ×4（爆头×杠开）确定性胡，优于即胡 ×2；`_gang_bomb_choice` 用 34 张全胡判定（与平台同口径）。杠 = 链动作 +1 |
| **托管轮次（幽灵弃牌结案）** | 服务端回合计时 **~2 秒**；我方 `/state` 延迟 31ms 或 **422ms×k**（429 重试链：0.4s sleep + ~22ms ≈ 422ms，实证吻合）。pacer 后 **429 = 0 ✓、422 量化缝消失 ✓**，但**托管轮次未趋零**：s44 8.5%（修复前）、s45 7.1%、s46 8.9% → **主导因是服务器 state 长尾延迟（11-14% 轮询 >900ms）× 2 秒计时**，客户端侧已无低成本杠杆。损益：托管里**自动胡占 ~1/3 是白赚**，有害部分 ≈ 0.3 次/手，**接受现状** |
| **番值结构（15 场 1176 手）** | 胡率**已追平**（我方每座 21.5% vs 对手 21.1%）；**真正代差 = 番值上限**：对手 f2+ 兑现 22.2%，我方仅 8.7%；平均番 1.25 vs 1.09。我方链/飘利用率 ≈ 0 |
| **f2+ 来源分类（470 手 `hu_detail` 全量）** | 我方 f2+ 构成：**爆头 77%**、七对 19%、杠开 4%、链 0、飘 0 —— 与「链方向关闭」完全自洽。爆头是唯一大头，七对是第二杠杆，**通道均已满配** |
| 赛事 gid 格式 | `t_<hex>_bN_tN`（无 `_r1_`）；`--wait` 推导已兼容，`--room <赛事id>` 直连 |
| **日/夜池假说** | **已削弱，不作排期依据**。8 场样本 -5/-131/-131/-273/-300/+23/-49/-249 → 单场方差 ±150-200 **主导**，时段效应存疑 |

**评估基线（近 20 场稳定态）**：每手净分 **-0.8/手**（s21-s55 = -2286/2720 手）；胡率 20-27% 波动与对手每座持平；f2+ 11.7%；爆头全听兑现 75-100%；单场方差 ±200 **主导**。→ **策略层实验（宣言/冻圈/门票）全部闭环后进入纯攒样本期；不要再对着 6-8 场方差调权重。**

---

## 五、基础设施与工具链

**对局与评测**
- `mj/arena.py` — 4 座离线竞技场（claims=real 对齐生产门控）。`python -m mj.arena` 跑对照；`grid <批次数>` 跑权重网格。**任何策略改动先在这里 A/B**
- `mj/weight_fit.py` — 实战牌谱回放拟合器（固定真实摸牌序列，量听牌率）。`python -m mj.weight_fit`
- `tools/run_test_room.py` / `.ps1` — 4 令牌并发开测试房跑满一场（**当前无参赛令牌，跑不了**）
- `tools/pipeline.py` — 离线管线总入口（argparse 子命令）。**注意：`mj/pipeline.py` 不存在**

**复盘分析**（最常用 8 个）
- `tools/postmortem.py <room>`（默认 `t_65d538e905c5`）— 逐手重放：吃碰代价/听牌转化/庄链失血/七对存续
- `tools/fan_analysis.py` — 跨会话番值分布与我方胡率（按 `a_` 或 `t_65d538e905c5` 前缀过滤）
- `tools/hu_detail_survey.py` — f2+ 来源分类（**KPI 追踪口径**）
- `tools/real_session_report.py <room>` / `tools/hand_attribution.py <room>` — 批次战绩 / 逐手归因
- `tools/room_audit.py portal_events/<room>/` — portal 事件 vs 客户端日志对账（托管清单）
- `tools/fan_duipai.py` — 本地判胡引擎 vs 服务端口径对拍
- `tools/pace_verify.py` — pacer 前后延迟对比
- `tools/cross_session_stats.py` — 跨会话事件流对照

**portal 对账链**：`portal_cookie.txt`（`majiang_sid`，gitignored，2026-12-13 到期；需从浏览器复制）→ `tools/pull_all_events.py <room>`（拉 10 局事件流）→ `tools/room_audit.py portal_events/<room>/`（自动按 `user_id` 定位我方座位、逐局差集、托管清单）。**注意：每局座次重洗，座位必须按 `user_id` 定位而非固定序号。**

**数据**：`logs/*.jsonl` 决策日志（含 hand/drawn/wall/dealer/scores/opp_chain/hu_detail，午夜轮转）；`models/events/` 事件流；`models/dataset/` 训练样本；`models/weights.json` 权重覆盖（**仅 8 键**）。

---

## 六、已知坑

- **`python -m mj.bot <令牌>` 漏掉 `--once` 会无限连场**——僵尸 bot 事故只修了 `--room` 路径，自由匹配仍会循环。**任何 bot 启动必须带终局退出条件**
- **同用户双令牌勿并发跑两个 bot 实例**（日志与排名互相污染）
- **令牌**：会作废（401 → 找用户重新签发）；平台只存 SHA-256（明文只显示一次）；**明文泄漏点 3 处待清**（见 1.4）
- **房 404**：正式房可能秒 404（平台故障）——死局防护已内置，**别手动重试**
- **429** = 轮询超频，`POLL_MIN_GAP` 别调低于 0.3s
- **`round_no` = 批次内第几手（1-8）**，不是场次号——曾因此出 bug
- **JSONL 有截断行**（进程被杀），解析必须 `try/except`；日志文件被同步盘/杀软锁定时 `append` 会丢记录（设计如此，**勿去掉 try/except**）
- **`tools/` 下报告脚本改过多次缩进 bug**——修改时注意循环结构
- **`real_session_report` / `hand_attribution` 的房间参数要带 `a_` 前缀**（如 `a_250cf7e20f27`），否则 0 手（`gid.startswith(ROOM)` 前缀匹配）
- **`_baotou_bonus` 已被 `joker_plan_value` 取代删除**；arena/weight_fit 改权重走 `_weights` 键
- **死键警告**：`discard_joker`、`discard_joker_soft`、`baotou_faster` 三个权重**无读取点**，调了没效果（见 2.4 末）
- **数我方胡牌一律以 `hu_detail` 记录为准**——`hand_attribution`/`fan_analysis` 按分数差判胜会漏计合并段（s44：18 vs 真值 22）；`decision`/`hu_detail` 还会被旁路容错丢 ~3 条/场（正常损耗）
- **portal 牌河会隐藏被吃碰的弃牌**——s44 b0 我方打的 2t 被碰走后从打出行消失，别误判成丢牌；**多出的牌**才需要查
- **平台指南版本自检**：`bot.py:24 KNOWN_GUIDE_VERSION = 34`（服务器当前 v34 / 2026-09-14）。启动时拉 `GET /portal/api/guide/version` 比对，发现 BREAKING 即提示人工核对。v29 是唯一近期 breaking（管理页可关自由匹配/测试房 → 403 `FEATURE_DISABLED`，**永久条件勿重试**）

---

## 七、缺口与下一步

### ① 🔴 正式锦标赛路径（有硬 deadline，10/10 当天会出局）

比赛跑的是「参赛令牌 + `register`/`ready` + 多阶段状态机」。现状 `bot.py:main` 的 `--wait` 分支只做了 `wait_for_assignment`（轮询 `me()` 等分房 + 每 5 分钟补 `ready`），缺三样：

| 缺什么 | 后果 |
|---|---|
| `api.register()` | `POST /api/tournaments/{id}/register` 调不了 |
| `stage_open`/`stage_done`/`qualified` 状态机 | 打完一阶段被当成结束退出；`stage_open` 需**每阶段重新确认出席**（不跨阶段继承），漏确认按「已确认 ∧ 开赛时刻 90s 内在线」取人 → **可能被剔除** |
| `--wait` 默认 7200s（`bot.py:441`） | 决赛圈 15:00-18:00 是 **3 小时** → 中途退出。且在 `stage_done` 后会**静默空转到 7200s**（`bot.py:472`），看着像"在跑"实为挂死 |

**两条可走的路，今天拍板：**

- **方案 A · 人工兜底（成本 ~0，今天可演练）**：门户点「报名」→ 赛事推进时在门户点「**资格确认**」按钮（与 `ready` 同语义，`资料.md:227`）→ bot 用 **`--room <赛事id> --once` 直连**打 running 场。**这条路已被验证过**（历史赛事 `t_65d538e905c5` 就是这么打的）。代价：需要人在 10/10、10/12 守着点确认按钮，且每场要手动重启。
- **方案 B · 补状态机（成本 ~1 天，全自动）**：补 `api.register()` + 官方模板的 `status` 多阶段循环（`资料.md:353-388` 有可直接借鉴的骨架）+ `--wait` 改成终态退出、无上限保活。

另需注意：**正式赛 id 一律 404 于免认证数据 API**（`/api/test-rooms/...`），复盘得走 owner 登录态 `GET /portal/api/games/{id}/events`；`active_games` 在阶段间隙为**空**（空转 ≠ 结束，唯一进度真相是 `tournament.status`）；决赛并列会**自动加赛**（新 `game_id` 自动出现，需持续轮询发现）。

### ② 消除核心维护风险
`strategy._discard_score`（`strategy.py:33`）与 `ev.route_value`（`ev.py:34`）的重复评分项抽成共享函数。决策层最脆的地方（已有「豪华门票改一处漏一处」先例），4 个文件还带 CRLF 污染。

### ③ 提交物（10/8）
《程序使用说明》面向**组委会**；源码二选一（仓库地址 或 含 `.git` 的 <20M 压缩包）。**提交前**：① 清 3 处令牌明文（`python -c "from mj.security import scan; print(scan('.'))"` 要返回 `[]`）② `dos2unix` 清 CRLF ③ 确认 `logs/`、`portal_events/`、`portal_cookie.txt` 未入包。

### ④ 攒样本（长期）
KPI 用 `hu_detail_survey.py`（f2+）+ `fan_analysis.py`（番值）。**不要再对着 6-8 场方差调权重。**

---

## 附录 A：场次流水（原文档实验日志的压缩）

**s1-s20 建栈期**：初版速度 -138/-26/-75/-147/-104/-111 → s7 组合拳（防守+爆头+字牌修复）**+90 rank1** → s8-s10 实验层已回滚 -143/-174/≈0 → s11-s14 回滚+宽松吃碰 空场/-72/-175/+13 → s15 平台深夜故障（防护自证，零损失退出）→ s16 -136 → s17 七对并入+白板枚举 +1 → s18 防守 v2 **+123 rank1（喂牌支出 209 创最佳）** → s19 vs 强敌 -148（对手 +286 怪物庄链）→ s20 1024杭麻竞技二测 -177（**赛事房 `t_65d538e905c5`**）。

**s21-s26 新栈爬坡**：s21 四修首秀 **+315 rank2（7 胜/10 局）**；s22 -119、s23 -156（强敌场）→ 六场合计 -125/60 局。

**s27-s41 方差期**：s27 +20 → s28 +48 → s29 -60 → s30 -254 → s31 -169 → s32 +83 → s33 -199 → s34 -69 → s35-s38 -142/-188/空场/**+31 rank1** → s39 -147 → s40 -59 → s41 -110。

**s42-s55 近期（策略实验闭环期）**

| 场次 | 净分 | 胜 | 我方胡 | 备注 |
|---|---|---|---|---|
| s42 | **+457** | 9/10 | 31/80 (39%) | **听牌活度加权首秀，历史最佳**；爆头 4/4 |
| s43 | +23 | 4 | 22/80 | 活度加权两连胜 |
| s44 | -5 | 3 | 22/80 | 新令牌 `dc4b4a72` 首场；**幽灵弃牌事故**（触发 pacer + 409 全量落日志） |
| s45 | -87 | 2 | 18/80 | pacer 首场；三家大牌砸脸 |
| s46 | -131 | 3 | 18/80 | 对手 `0D+96`×3；pacer 双样本：429=0 ✓ |
| s47 | **-273** | 0 | 10/80 | **财飘宣言证伪场（历史最差）** |
| s48 | -300 | 0 | 10/80 | 回退确认活体；曾归因夜场池，后被削弱 |
| s49 | +23 | 4 | 18/80 | 回退后日段 |
| s50 | -49 | 5 | 20/80 | 日段第三场 |
| s51 | +15 | 5 | 19/80 | `a_f77b676c74ff`；**用户问的「双白冻圈」= 本场 b6-r1** |
| s52 | -249 | 1 | 10/80 | 冻圈移除前最后一场；日段大负 → **日/夜池假说被削弱** |
| s53 | -119 | 1 | 22/80 | 豪华门票首场：豪华 0、爆头 2 ✓、财飘 0 ✓ |
| s54 | -95 | 3 | 16/80 | 门票观测 round 2：豪华 0、无回退 |
| s55 | -156 | 2 | 16/80 | 门票观测 round 3：豪华 0（3 场未现） |

**累计**：s21-s55 = **-2286/2720 手 = -0.84/手**；f2+ 90/772 = 11.7%。

## 附录 B：平台规则要点（全部已实证）

- **牌具** 136 张，白板 = 财神（百搭）；财神本身不能被吃碰杠胡，可主动打出
- **抓打圈**：打出财神那一圈，其余玩家不能吃/碰/明杠（仅暗杠+自摸胡），且只能打刚摸到的牌；**打财神者本人豁免**。圈内吃碰后再打财神 = 财飘链 +1；打其他牌 = 链断解圈（2026-09-08 裁定）
- **只能自摸**，无点炮、无抢杠。**庄家胡 ×8**（三家各付 ×8），闲家胡 ×1。**直上三连庄**（首局即 ×8，不递增），流局庄家连庄
- **牌墙**最后 10 墩（20 张）保留不摸，此区间**禁止杠**，摸完流局
- **吃最多 2 摊**（服务端 409 强制）；碰/杠不限；**碰窗先于吃窗**
- **番型**：总番 = 1 × 分支 × 2^动作链 ×（4 白 ×2）×（爆头 ×2）。平胡 1 / 七对 2 / 豪华七对 4 / 双豪华 8 / 三豪华 16 / 杠开 2 / 杠爆 4 / 财飘 4 / 双财飘 8 / 三财飘 16。**全局最大 ×512**（三豪华七对 + 三财飘 + 4 白 + 爆头）
- **弃胡**：可胡不强制胡，可继续打牌（飘/杠链前提）；超时未响应 → 服务端自动胡兜底
- **超时兜底**：出牌 3s（可胡时自动胡，否则自动打最右一张）；碰/吃窗固定走满 1s（防时间侧信道）
- **1.6 可变参数必须读 config 不可写死**：`M / Rounds / BaseScore / YouCaiBiKao（有财必拷响）/ PengTimeoutSec / ChiTimeoutSec / DiscardTimeoutSec / StartAt / RegisterDeadlineAt`（`GET /api/tournaments/me/rules`）
- **v7 多阶段**：`registering → running → stage_done → stage_open → running → …(决赛) → finished`，任时可 `closed`/`void`。非决赛轮打完停在 `stage_done`（等管理员推进）；`stage_open` 需重新确认出席（**门户「资格确认」按钮同语义**）；决赛并列自动加赛
- **晋级判定链**：`total_score → place_points → god_count → user_id`，三键独立永不相加；名次分每场 1位+3 / 2位+1 / 3位−1 / 4位−3（同分共享并列区间平均，**不进总得分**）；**决赛轮纯总得分**、任何同分加赛至两两不同
- **v31 局间 5 秒 `settled` 窗口**：此间 `my_hand` 给的是**上一局剩牌不是新局手牌**，拿它算牌会错；提交任何动作一律 409 `INVALID_ACTION`（与「动作非法」同码，**必须按 code 判型别看 message**）
- **v33 杠后补牌不再自动结算**：补到能胡也停在决策窗口，可 hu / 续杠 / 弃胡打财神续飘

> 完整平台文档：`资料.md`（514 行，含 API 参考、错误码表、最小 Bot 模板）与 `models/guide.md`（服务器 v34 接入指南存档）。
