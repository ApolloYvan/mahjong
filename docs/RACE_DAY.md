# 正式比赛准备清单

> 两个硬约束（服务端强制，事后无法补救）：
> 1. **ready 必须在开赛前提交** —— 开赛后一律 `409 TOURNAMENT_STARTED`，晚 1 分钟 = 上不了场
> 2. **开赛时刻 ±90 秒内必须有已认证请求** —— 进程那一刻得活着

---

## 一、赛前一天：一次性准备

### 1. 存全局令牌（门户「我的 AI 身份」）

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 -c "from mj.token import save_token; print('已存到', save_token(input('粘贴全局令牌: '), kind='global'))"
```

### 2. 全量测试必须通过

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 -m unittest discover tests 2>&1 | tail -15
```

结尾应是 `OK`。已知例外（与 v1.4/v1.5 改动无关、改动前就失败）：`tests.test_responses` 里两个暗杠用例 `test_all_choose_gang_cases`、`test_synthetic_count4_case_allows_gang`。除这两个之外出现 `FAILED` 就不要上场。

### 3. 打一个自由匹配房，确认打牌链路正常

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 -m mj.bot --server https://10.240.169.190:18080
```

打完后体检，`服务端代打` 应 ≤ 1%：

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 tools/session_health.py --last 1 --gaps
```

---

## 二、门户报名之后：立刻做

参赛令牌**按赛事派发、只显示一次**。拿到就存，否则后面全部步骤都会 401。

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 -c "import getpass; from mj.token import save_token; print('已存到', save_token(getpass.getpass('粘贴参赛令牌（不回显）: ')))"
```

确认两个令牌指向**不同**文件：

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 -c "
from mj.token import describe
print('参赛令牌:', describe('scoped'))
print('全局令牌:', describe('global'))"
```

- 参赛令牌 → `.mj_token`（赛事：register / ready / me）
- 全局令牌 → `.mj_token_global`（自由匹配 `/api/match`）

两个作用域互斥，混用会被 `400 TOKEN_NOT_SCOPED` 拒绝。存对了程序会自己挑。

---

## 三、开赛当天

### T−30min　起守门（终端 1，不要关）

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 tools/tourney_guard.py
```

它每 15 秒打印一次：开赛倒计时、报名、**出席**。并自动幂等提交 register / ready。

### T−10min　确认出席

终端 1 里必须出现：

```
出席=True
   ✅ 报名 + 出席都已确认
```

**`出席=True` 是唯一重要的那个值。没变 True 就不要做任何别的事**，直接看第五节排查。

### T−10min　起对战（终端 2，不要关）

```bash
cd /Users/yuanye/coding_workspace/mahjong && caffeinate -dims sh -c 'until python3 -m mj.bot --wait --server https://10.240.169.190:18080; do sleep 10; done'
```

（意外退出 10 秒后自动拉起；赛事正常结束才停。见第六节。）

正常输出（2026-10-09 起出席以 ready 接口的返回为准——服务端详情里没有出席字段）：

```
接入指南 v35，与本 bot 一致。
[赛事] 报名期｜出席：确认中…
[赛事] ✅ 出席确认成功：第1阶段，服务端返回 ready=True
[赛事] 报名期｜出席：已确认（ready 接口返回 ready=True）
[赛事] 比赛进行中｜第1/4阶段｜出席：已确认（…）｜进行中对局 10 场
```

**必须看到「✅ 出席确认成功」那一行。** 每个新阶段（stage_open）都会再出现一次。
服务端详情间歇 404（TOURNAMENT_GONE）时沿用上一次的状态，不再在「报名期 / 尚未发现赛事」之间来回跳。

### 开赛后　两个终端都不要关

多阶段赛事（报名人数 ≥17 时是 海选 → 16强 → 8强 → 决赛）：

- `stage_done` = 阶段之间等管理员推进，**正常状态**
- `stage_open` = 下一阶段确认期，**出席不跨阶段继承，必须重新 ready**（守门工具自动做）
- 只有 `finished` / `closed` / `void` 才是真结束

---

## 四、绝对禁忌

| 禁忌 | 后果 |
|---|---|
| **赛前 30 分钟内跑自由匹配**（`python3 -m mj.bot` 不带 `--wait`） | 它**从不调 ready**，且打完就退出 —— 2026-09-24 就是这么丢掉整个海选的 |
| 开赛后才起进程 | ready 一律 409，本阶段无法挽回 |
| 中途关掉 `--wait` | 违反 90 秒保活，会被剔除 |
| 只看到 `报名=True` 就以为没事 | 报名 ≠ 出席。服务端按出席分桌 |

---

## 五、出问题时

### 出席一直是 False

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 tools/tourney_guard.py --once; echo "退出码=$?"
```

退出码 0 = 报名+出席都 OK。非 0 看打印的错误码：

| 错误码 | 含义 | 处置 |
|---|---|---|
| `401 UNAUTHORIZED` | 令牌无效／过期 | 门户重新取参赛令牌，重新存 |
| `409 TOURNAMENT_STARTED` | 已开赛 | 本阶段无法挽回，挂着等下一阶段 |
| `409 NOT_REGISTERED` | 没报名 | 门户报名，且必须在 `register_deadline` 之前 |
| `409 NOT_QUALIFIED` | 上一阶段已淘汰 | 本届结束 |
| `403 PORTAL_BINDING_REQUIRED` | 匿名令牌 | 必须用门户签发的身份 |
| `404 TOURNAMENT_GONE` | 房暂时不可达 | **可重试**，不是"房不存在" |
| `400 TOKEN_NOT_SCOPED` | 令牌用错了地方 | 见第二节，两个令牌分开存 |

### 看不到任何赛事

正常情况是赛事还没创建，或令牌是上一届的。**参赛令牌按赛事派发，上一届的令牌查不到本届赛事。**

### 进程卡住不动

`mj.bot --wait` 只在状态变化时打印，长时间无输出是正常的。判断活没活：

```bash
cd /Users/yuanye/coding_workspace/mahjong && tail -3 logs/$(date +%Y-%m-%d).jsonl | cut -c1-200
```

---

## 六、赛后

采集对局数据（房间号从对局输出或门户拿）：

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 tools/pipeline.py collect https://10.240.169.190:18080 <房间号...>
```

体检（先回答「是打输的还是没打上」）：

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 tools/session_health.py --rooms <房间号...> --gaps
```

得分归因：

```bash
cd /Users/yuanye/coding_workspace/mahjong && python3 tools/score_attribution.py --top 6
```

---

## 附：当前策略配置（v1.5，2026-10-09 晚由 v1.6 回退）

- **v1.3**：状态拉取严格排队（吃碰超时减半）、异常兜底、`--wait` 状态机进程内自动重启。
- **v1.4**：早巡弃胡转爆头——手上 2 张以上财神、墙剩 54 张以上、副露 ≤1 时，差一步转爆头也先不胡（实战 10 次触发比当场胡净 +31）。
- **v1.5**：两个庄家开关——庄家无财神时用闲家那套出牌打分；庄家持财神时不碰"碰完向听不变、也不成爆头"的牌（27 房 A/B 庄局 +0.98，闲局两组相同）。
- **v1.6**：v1.5 + 少弃胡——关掉「差一步转爆头」的早巡弃胡（`s1_one_step_enabled=0`）和「1 财神 / 1 副露」那一格的弃胡（`rule_decline_single_joker_one_meld_enabled=0`）；2+ 财神能直接转爆头的弃胡保留。10/09 三组 A/B（各 5 房）去运气后 +1.15±1.09/局，v1.5 前三批 103 房 +0.22±0.27；证据偏弱（z≈1.6），采用理由是它只是撤掉两条证据本来就薄的弃胡规则、方向是「能胡就胡」更稳。
- **10/09 晚回退 v1.6 → v1.5**：少弃胡 24 房（B 组 + v1.6）对 v1.5 75 房：胜率 25.3% vs 25.8%（没变快），爆头占胡 27.6% vs 32.3%、2番+ 收入占比 45.4% vs 51.3%、每局收入 4.80 vs 4.96——少弃胡只少了大牌，B 组那 +1.15 是噪声。
- 回滚：`cp models/weights.v1.5.json models/weights.json`（或 v1.4 / v1.3），bot 每次决策都重读权重，改完立即生效（比赛中也可以）。

**已知未解决**：番/胡仍低于顶尖强手（约 1.4 vs 1.5），差在"手上有财神时转成爆头/大牌"。

---

## 六、比赛期间的运维（2026-10-05 补，按外部评审意见）

**启动方式**：进程意外退出（返回码非 0）自动重新拉起；赛事正常结束（返回码 0）才停：

```bash
caffeinate -dims sh -c 'until python3 -m mj.bot --wait; do sleep 10; done'
```

- 进程内也会自动重启：`--wait` 状态机遇到连续网络/命令错误时，30 秒后在进程内重来，不退出。
- 吃碰窗口静默（`MJ_RESPONSE_QUIET`）默认关闭：10/05 实测未达标，比赛不要打开。
- v1.3 起状态拉取默认严格排队（吃碰少丢一半）。比赛中如果出牌超时明显变多（`session_health` 每房 > 10 次），用 `MJ_PACE_RESET_SLOTS=8` 重启 bot 退回旧行为。

**机器**：插电；关闭低电量模式、自动更新和重启；合盖不休眠。比赛期间不在这台机器上跑对战平台或任何重计算——会和 bot 抢 CPU，吃碰窗口只有 1 秒。

**网络**：服务器在 10.240.x.x 内网段。尽量用有线网；要走 VPN 时，VPN 掉线是最大风险。

**每个阶段的冻结时刻都要有人盯**（海选、16 强、8 强、决赛共四次，10/12 下午连着三次）：
- 开赛前在门户或守门进程输出里确认"出席：已确认"。出席不跨阶段继承。
- 海选落在 17–32 名也要保持在线并确认：候补按名次递补。

**对局中**：
- 某场在进行中、但日志里超过 2 分钟没有新决策 → 重启 bot。
- 门户显示"重赛本轮"或进入新的确认期，bot 还停在旧对局里 → 重启 bot。
- 新进程会把进行中的对局全部重新接回。

**备用机**：装好同一份代码和令牌备用，但绝不能和主机同时运行（两个进程会对同一场提交冲突的动作）。
