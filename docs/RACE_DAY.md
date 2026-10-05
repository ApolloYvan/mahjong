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

结尾必须是 `OK`。出现 `FAILED` 就不要上场。

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
cd /Users/yuanye/coding_workspace/mahjong && python3 -c "from mj.token import save_token; print('已存到', save_token(input('粘贴参赛令牌: ')))"
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
cd /Users/yuanye/coding_workspace/mahjong && python3 -m mj.bot --wait --server https://10.240.169.190:18080
```

正常输出：

```
接入指南 v35，与本 bot 一致。
[赛事] 报名期｜第1/4阶段｜出席：已确认
[赛事] 比赛进行中｜第1/4阶段｜出席：已确认｜进行中对局 10 场
```

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

## 附：当前策略配置

保留三处有实测证据的修复：

- `mj/ev.py` 弃财神奖励原挂在服务端从不下发的 `piao` 字段上（恒为 0，真 bug）
- 续飘成功率按剩余步数复利（原先只乘一次，剩 3 步时乐观算了三遍）
- `high_fan_force_fan` 3 → 12（弃财神分歧 2/2 → 0/2）

2026-09-24 试过的 8 个方向全部回滚（弃牌顺序偏好、tier 过滤、同向听优化目标、合计推到4、吃碰爆头优先、听口覆盖率梯度、留白溢价单调化等）。回滚点见 `.claude/skills/mahjong-strategy/SKILL.md` §8。

**已知未解决**：番/胡 1.17（高手 1.31~1.53）、爆头占胡牌 9~13%（高手 25%）。差距统计显著，机制未定位。
