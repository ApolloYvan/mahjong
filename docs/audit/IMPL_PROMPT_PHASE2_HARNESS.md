# 实施任务书 · 第 2 阶段 / 任务 1：离线反事实评测台

给 Sonnet 5 / Gemini 3.8 Flash 的逐步实施 prompt。
架构师：Claude Opus 5。日期：2026-09-23。

**这份任务书只做评测台，不改任何策略。** 策略改造是任务 2，另发。
先做评测台的理由：没有它，第 2 阶段每验证一个候选要 7 小时线上对局，
15 天最多试 3 个版本；有了它，一个候选 1 分钟，一天能试几十个。

---

## 0. 前置状态（做之前先确认，不一致就停下来问）

1. 第 1 轮修复已落地：`mj/shanten.py::pair_route_allowed` 存在，
   `strategy.py` / `ev.py` / `responses.py` 三处已接入。
2. 全量测试当前**应当只有 2 个 failure**，且都在
   `tests/test_claim_safety_audit.py`（硬编码 `8`），属于已授权的修订
   （授权例外 #1）。**如果失败数不是 2、或失败出现在别的文件，立刻停下来报告。**
3. 授权例外 #1 建议先做完再开始本任务，保证基线是绿的。

---

## 1. 禁止触碰的范围（硬约束）

- **不得修改 `mj/` 下的任何文件。** 本任务是纯新增工具 + 新增测试。
- 不得发起任何网络请求；不得运行 `python3 -m mj.bot`。
- 不得修改 `mj/fit.py::DEFAULT_WEIGHTS` 的任何数值，不得创建 `models/weights.json`。
- 不得修改 `tests/` 下任何既有测试文件（新增文件可以）。
- 不得删除或重写 `logs/*.jsonl`、`models/events/*.json`。

---

## 2. 要新增的文件

### 2.1 `tools/replay_core.py`（新增）

从 `models/events/*.json` 重建每一局的完全信息。

```python
def merge_rounds(game: dict) -> list[dict]:
    """把同一局的分页 block 合并。返回
    [{round_no, dealer, start_hands, events(按 seq 升序), truncated}]"""
```

**必须注意的坑（这是本任务唯一一个容易写错的地方）：**

`start_hands` 缺失时的取值是 `[None, None, None, None]`，在 Python 里**是真值**。
所以下面这行是错的：

```python
if b.get('start_hands'):            # ← 错：空块会覆盖掉好块
    d['start_hands'] = b['start_hands']
```

必须写成：

```python
sh = b.get('start_hands')
if sh and all(sh):                  # ← 对
    d['start_hands'] = sh
```

写错的后果实测过：可重放局数从 **1680 / 1680** 掉到 **237 / 1680**，
而且掉下来的那 237 局是系统性偏短的局，会让任何基于它的统计整体偏移。

```python
def replay_round(round_dict) -> (steps, hands, melds, rivers):
    """逐事件重放，维护四家手牌。遇到不一致抛 ReplayError。"""
```

支持的事件类型：`tile_drawn` / `tile_discarded` / `chi` / `peng` /
`gang`（`data.kind` ∈ `an` / `ming` / `bu`）/ `pass` / `timeout` /
`round_ended` / `game_ended`。遇到未知类型必须抛错，不许静默跳过。

`gang` 的三种 `kind` 语义：`an` 从手里移 4 张；`ming` 移 3 张；
`bu` 移 1 张并把既有的 `peng` 面子升级成 `gang`。

### 2.2 `tools/race_eval.py`（新增）

固定牌墙的自摸竞速评测台。

```python
def build_cases(events_glob, me_user_id) -> list[Case]
```

每个 `Case` 含：`game_id, round_no, seat, dealer, hand0(该座位起手牌),
draws=[(global_draw_index, tile), ...](该座位实际摸到的牌，按全局 seq 序),
won_hist, fan_hist, is_draw`。

`global_draw_index` = 该局内 `tile_drawn` 事件的序号（从 1 开始，跨座位统一计数）。

```python
def race(case, policy, *, wants_wall=False) -> (win_global_index | None, fan)
```

模拟"只摸打、不吃碰"：

1. 若 `len(hand0) == 14`（庄家），先用 policy 打一张。
2. 对每个 `(gidx, tile)`：摸进 → 判胡 → 不胡则用 policy 打一张。
3. 判胡**必须先用便宜的 `win_standard()` / `seven_pairs()` 过一遍，
   只有确认成和才调 `rules.evaluate()`。**
   原因：`evaluate()` 内部无条件计算 `baotou()`，而 `baotou()` 要跑 34 次
   `win_standard()`。每次摸牌都无条件调用它，实测让整个评测台慢一个量级。
4. `wall_remaining` 用 `84 - gidx` 还原（局内第 1 次摸牌时为 83，实测吻合）。
   **不许用 `len(剩余 draws)` 当剩余摸牌数** —— 那会泄漏"本局何时结束"
   这个未来信息（局是被对手胡牌截断的）。

### 2.3 `tests/test_replay_core.py`（新增，必须有）

| 测试 | 断言 |
|---|---|
| `test_all_rounds_replay_without_error` | 全部 `models/events/*.json`：局数 == 1680，成功 == **1680**，失败 == **0** |
| `test_start_hands_survive_empty_later_blocks` | 构造一个 game：block A 有完整 `start_hands`，block B 是 `[None]*4`，合并后 `start_hands` 仍是 A 的（**这条就是锁死 §2.1 那个坑**） |
| `test_unknown_event_type_raises` | 注入一个 `type: "bogus"` 的事件 → 抛 `ReplayError` |
| `test_wall_remaining_reconstruction` | 每局第 1 次 `tile_drawn` 对应 `84 - 1 == 83` |

### 2.4 `tests/test_race_eval.py`（新增，必须有）

| 测试 | 断言 |
|---|---|
| `test_case_count_and_historical_win_rate` | `len(cases) == 1672`；`sum(won_hist)/1672` 落在 `[0.205, 0.209]`（实测 0.2069） |
| `test_race_is_deterministic` | 同一 case 同一 policy 跑两次，结果完全相同 |
| `test_race_never_exceeds_available_draws` | 返回的 `win_global_index` 必在该 case 的 `draws` 里 |
| `test_policy_sees_no_future_information` | 用一个记录所有入参的 spy policy，断言它收到的 `wall_remaining` 序列只依赖 `gidx`，与该局总长度无关 |

---

## 3. 验收指标

- [ ] A1 `tests/test_replay_core.py` 全绿，且 **1680/1680、失败 0**。
- [ ] A2 `tests/test_race_eval.py` 全绿。
- [ ] A3 全量测试的 failure 数 **仍然是 2**（授权例外 #1 那两处），没有新增红。
- [ ] A4 `race_eval` 跑当前 `mj.strategy.choose_discard` 全量 1672 局，
      单进程耗时 **< 6 分钟**（实测干净环境 117 ms/局 ≈ 3.3 分钟）。
      如果远超，先查是不是 §2.2 第 3 条的 `evaluate()`/`baotou()` 写法。
- [ ] A5 把 A4 的结果（race 胜率、平均成和摸数、平均番）写进
      `docs/refactor/VALIDATION.md`，作为第 2 阶段的**冻结基线**。

## 4. 回滚条件

本任务是纯新增，不改 `mj/`。唯一的回滚条件是：
**如果为了让评测台跑通而需要修改 `mj/` 下任何文件——停下来报告，不要改。**
那说明评测台的设计和现有接口不兼容，需要架构师重新定接口。

## 5. 交付时要回答的问题

1. A4 的三个数字分别是多少？
2. 1680 局重放里有没有出现 `ReplayError`？如果有，是哪一类事件？
3. 有没有任何地方你需要改 `mj/` 才能跑通？
