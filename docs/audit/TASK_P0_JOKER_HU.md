# 任务 P0：摸到财神时的拒胡过滤器（一行修改）

架构师已完成全部调研与验证，下面的结论不需要你复核。直接改。

## 改什么

`mj/bot.py::hu_result()` 第 159 行：

```python
# 现在（无条件拒绝）
if drawn == JOKER and not (result and result.get("baotou")):
    return None

# 改成（只在规则开启时拒绝）
if (rules or {}).get("YouCaiBiKao") and drawn == JOKER and not (result and result.get("baotou")):
    return None
```

同时改 `hu_result()` 和 `can_hu()` 的 docstring——现在那段"财神只能爆头胡"
的描述是错的，改成"仅在 `YouCaiBiKao` 开启时受限"。别留着错注释。

**依据（不用你验证）：** 真实对局重放显示，对手用摸来的白板在非爆头手上
当场自摸成和 **243 次**，服务端全部确认；我们 86 次机会 0 次。
这些房间 `rules` 全是空 `{}`。

## 一处已授权的测试修改

`tests/test_production_gate.py::test_non_baotou_hand_completed_by_joker_draw_cannot_hu`
锁死了旧行为。**已授权**你这样改：

- 给那个 `snapshot` 加上 `"rules": {"YouCaiBiKao": True}`，让现有断言在
  规则开启的前提下继续成立
- 重命名为 `test_non_baotou_joker_draw_gated_by_youcaibikao`

**一行都不许动的：** 同文件的 `test_joker_cannot_be_pengd_chid_ganged`
（财神不能吃碰杠）、`test_baotou_hand_drawing_joker_can_hu_with_baotou_flag`、
`test_baotou_joker_draw_enters_choose_hu_or_piao_not_intercepted`。

如果还有**别的**测试挡住你，停下来报告，别自己处理。

## 一个新增测试

`tests/test_joker_hu_gate.py`，两条断言，用这手真实牌（来自生产日志）：

```python
hand = ["1b","3b","4b","4b","6t","6t","白","白"]   # meld_groups=2, drawn="白"
# rules={}                     -> hu_result() 非 None，fan == 1
# rules={"YouCaiBiKao": True}  -> hu_result() is None
```

## 不要碰

`mj/rules.py`、`mj/hu_strategy.py`、`_gang_bomb_choice`、任何权重、
`mj/fit.py`、`models/weights.json`。不联网，不跑 `python3 -m mj.bot`。

## 交付

1. `python3 -m unittest discover tests` 的 passed/failed 数
   （改之前基线：**522 passed / 0 failed**）
2. 改动的文件清单
3. 有没有遇到本任务书没预料到的东西

就这三样。其余的验收统计由架构师做。
