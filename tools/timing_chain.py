"""G3b 附属 / 阶段一 §5：从 ``logs/*.jsonl`` 里还原我方每次决策的时间线，
给阶段二的动态预算公式提供实测常数。

    python3 tools/timing_chain.py --limit-files 3      # 冒烟，<1 分钟
    python3 tools/timing_chain.py                       # 全量，交给用户执行

======================================================================
四段时间线怎么从日志字段拼出来
======================================================================
不是猜的时间戳相减，是直接读代码里已经算好的耗时字段（见 ``mj/bot.py``
决策主循环 + ``mj/logging.py`` 对应的 ``append`` 调用点）：

1. **notify 到达 → state 返回**：
   - ``notify`` 记录（自己的落盘时间 = notify 到达那一刻）；
   - ``state_request_metric`` 记录（``trigger="notify"``）的
     ``elapsed_ms`` 字段 = 这次 /state 请求本身的网络往返耗时（
     ``mj/logging.py::state_request_metric``，由 ``mj/bot.py`` 里
     ``fetch_started = time.monotonic()`` 到响应回来那一刻算出来，是
     **纯网络段**，本工具原样读取，不用时间戳相减近似）；
   - 额外报告"notify 落盘时间 → 对应 state_request_metric 落盘时间"的
     挂钟差（两条记录的 ``time`` 字段相减），这段比 ``elapsed_ms`` 多
     算了"notify 之后、真正发起 /state 请求之前"的排队/调度延迟——两个
     数字都报告，差值大就是排队重，差值小说明几乎是发起即请求。
2. **state 返回 → 决策完成**：``decision``（kind="decision"）记录的
   ``client_prepare_ms`` 字段——``mj/bot.py`` 里 ``started =
   time.monotonic()``（拿到快照之后）到 ``decision = choose_action(...)``
   算完那一刻，纯本地计算段，不含任何网络。
3. **决策完成 → action 返回**：``decision`` 记录的 ``action_request_ms``
   字段——``action_started = time.monotonic()`` 到 ``api.action(...)``
   返回（或抛异常）那一刻，纯网络段。超时（``TimeoutError``/
   ``socket.timeout``）不会写这条 ``decision`` 记录（``log.action()`` 只在
   成功路径才调用），改用 ``decision_attempt``（发起意图，``attempted_at``）
   +``decision_attempt_outcome``（outcome="timeout" 的落盘时间）这一对
   （靠共享的 ``decision_id`` 精确关联，不是时间最近匹配）算出同一段
   耗时，覆盖失败路径。

服务端回合开始时间/截止时间：**快照里没有这个字段**——通读
``mj/state.py``/``mj/responses.py``/``mj/bot.py`` 里所有 ``snapshot.get(...)``
调用点，没有任何 deadline/expires/started_at 类字段（HTTP 客户端自己的
请求超时是 ``mj/api.py::Api.__init__`` 里的 ``timeout=35``，跟游戏内
"出牌 3 秒/吃碰 1 秒" 的服务端节奏无关，量级也差 10 倍以上，别混）。
所以"从服务端开始计时到我们拿到快照"这个量本工具**估不出真值**，只能用
"notify 到达 → state 返回"的挂钟差做一个下界近似（真正的服务端计时
起点可能更早，我们观测不到），如实标注不是精确值。

======================================================================
"同时在打的场次数"分层
======================================================================
用 ``decision_attempt``（+1）/``decision_attempt_outcome``（-1）两类
记录按时间排成一条扫描线，算出每次决策发起（``decision_attempt`` 落盘）
那一刻，全体日志里（不分 game_id）有多少个"发起了但还没拿到结果"的
决策在同时进行，作为"同时在打的场次数"的代理指标——按 1/2/3/4+ 分层
分别报告各段耗时的 p50/p90/p99。

======================================================================
输出
======================================================================
终端只打印 ≤40 行汇总；完整每次决策的时间线明细写到
``reports/timing_chain_detail.tsv``。
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REPORT_PATH = os.path.join(ROOT, "reports", "timing_chain_detail.tsv")


def _iter_records(log_paths):
    for path in log_paths:
        with open(path, encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                yield obj


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * p))
    return sorted_vals[idx]


def _print_pct_line(label, vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        print("  %-28s 无数据" % label)
        return
    print("  %-28s n=%6d  p50=%8.1fms  p90=%8.1fms  p99=%8.1fms  max=%8.1fms" % (
        label, len(vals), _percentile(vals, 0.50), _percentile(vals, 0.90),
        _percentile(vals, 0.99), vals[-1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=None, help="逗号分隔的 log 文件列表，默认 logs/*.jsonl")
    ap.add_argument("--limit-files", type=int, default=None, help="只读前 N 个文件（冒烟）")
    args = ap.parse_args()

    log_paths = args.logs.split(",") if args.logs else sorted(glob.glob(os.path.join(ROOT, "logs", "*.jsonl")))
    if args.limit_files:
        log_paths = log_paths[:args.limit_files]
    print("读取 %d 个日志文件……" % len(log_paths))

    notifies = defaultdict(list)          # game_id -> [(time, seq)]
    state_metrics = defaultdict(list)     # game_id -> [(time, elapsed_ms, trigger)]
    decisions = {}                        # decision_id -> payload (kind=decision)
    attempts = {}                         # decision_id -> (time, payload)
    outcomes = {}                         # decision_id -> (time, outcome)

    for obj in _iter_records(log_paths):
        kind = obj.get("kind")
        t = obj.get("time")
        p = obj.get("payload") or {}
        if kind == "notify":
            notifies[p.get("game_id")].append((t, p.get("seq")))
        elif kind == "state_request_metric":
            state_metrics[p.get("game_id")].append((t, p.get("elapsed_ms"), p.get("trigger")))
        elif kind == "decision":
            did = p.get("decision_id")
            if did:
                decisions[did] = (t, p)
        elif kind == "decision_attempt":
            did = p.get("decision_id")
            if did:
                attempts[did] = (t, p)
        elif kind == "decision_attempt_outcome":
            did = p.get("decision_id")
            if did:
                outcomes[did] = (t, p.get("outcome"))

    for gid in notifies:
        notifies[gid].sort()
    for gid in state_metrics:
        state_metrics[gid].sort()

    # ---- 段 1：notify -> state_request_metric(trigger=notify) ----
    notify_wall_gaps = []
    state_elapsed_by_trigger = defaultdict(list)
    for gid, metrics in state_metrics.items():
        for t, elapsed, trigger in metrics:
            state_elapsed_by_trigger[trigger or "unknown"].append(elapsed)
        for t, elapsed, trigger in metrics:
            if trigger != "notify":
                continue
            candidates = [nt for nt, _seq in notifies.get(gid, ()) if nt <= t]
            if not candidates:
                continue
            notify_wall_gaps.append((_iso_to_ms(t) - _iso_to_ms(max(candidates))))

    # ---- 段 2/3：decision.client_prepare_ms / action_request_ms（成功路径）----
    prepare_ms_all = [p.get("client_prepare_ms") for _t, p in decisions.values()]
    action_ms_all = [p.get("action_request_ms") for _t, p in decisions.values()]

    # ---- 段 3 补：失败路径用 decision_attempt -> decision_attempt_outcome ----
    action_wait_failed = []
    timeout_records = []
    for did, (t_attempt, ap_) in attempts.items():
        if did in decisions:
            continue   # 成功路径已经从 decision.action_request_ms 拿到了
        out = outcomes.get(did)
        if not out:
            continue
        t_out, outcome = out
        wait_ms = _iso_to_ms(t_out) - _iso_to_ms(t_attempt)
        action_wait_failed.append(wait_ms)
        if outcome == "timeout":
            timeout_records.append((t_out, did, ap_, t_attempt, wait_ms))

    # ---- 并发分层：decision_attempt(+1)/decision_attempt_outcome(-1) 扫描线 ----
    events = []
    for did, (t, _p) in attempts.items():
        events.append((_iso_to_ms(t), 1, did))
    for did, (t, _o) in outcomes.items():
        events.append((_iso_to_ms(t), -1, did))
    events.sort()
    concurrency_at_attempt = {}
    live = 0
    for ts, delta, did in events:
        if delta == 1:
            live += 1
            concurrency_at_attempt[did] = live
        else:
            live = max(0, live - 1)

    def _bucket(n):
        return n if n <= 3 else 4

    by_concurrency_prepare = defaultdict(list)
    by_concurrency_action = defaultdict(list)
    for did, (t, p) in decisions.items():
        c = _bucket(concurrency_at_attempt.get(did, 1))
        by_concurrency_prepare[c].append(p.get("client_prepare_ms"))
        by_concurrency_action[c].append(p.get("action_request_ms"))

    # ---- 明细文件 ----
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as out:
        out.write("decision_id\tgame_id\tseat\tphase\tconcurrency\tclient_prepare_ms\taction_request_ms\toutcome\n")
        for did, (t, p) in decisions.items():
            out.write("%s\t%s\t%s\t%s\t%d\t%s\t%s\t%s\n" % (
                did, p.get("game_id"), p.get("seat"), p.get("phase"),
                concurrency_at_attempt.get(did, 1), p.get("client_prepare_ms"), p.get("action_request_ms"),
                (outcomes.get(did) or (None, "success"))[1]))

    print("\n=== 时间链汇总（完整明细见 %s） ===" % REPORT_PATH)
    print("决策记录 %d 条（成功路径），decision_attempt %d 条，超时 %d 条" % (
        len(decisions), len(attempts), len(timeout_records)))

    print("\n段1 state 请求本身耗时（elapsed_ms，按 trigger 分）：")
    for trig, vals in sorted(state_elapsed_by_trigger.items()):
        _print_pct_line(trig, vals)
    print("段1 附加：notify 落盘 -> state_request_metric(trigger=notify) 落盘的挂钟差：")
    _print_pct_line("notify_to_state_wall", notify_wall_gaps)

    print("\n段2 state 返回 -> 决策完成（client_prepare_ms，本地计算）：")
    _print_pct_line("全部", prepare_ms_all)

    print("\n段3 决策完成 -> action 返回（action_request_ms，成功路径 + 失败路径 attempt->outcome）：")
    _print_pct_line("成功路径", action_ms_all)
    _print_pct_line("失败路径(attempt->outcome)", action_wait_failed)

    print("\n按同时在打的场次数分层（decision_attempt/outcome 扫描线并发数）：")
    for c in sorted(by_concurrency_prepare):
        label = "并发=%s" % (c if c < 4 else "4+")
        _print_pct_line("%s 段2(计算)" % label, by_concurrency_prepare[c])
        _print_pct_line("%s 段3(action)" % label, by_concurrency_action[c])

    print("\n最近 5 次 outcome=timeout（HTTP 层超时，跟游戏内 3s/1s 节奏无关，"
          "见模块 docstring 关于 mj.api.Api timeout=35 的说明）：")
    timeout_records.sort(reverse=True)
    for t_out, did, ap_, t_attempt, wait_ms in timeout_records[:5]:
        print("  decision_id=%s game_id=%s seat=%s action=%s attempted_at=%s outcome_at=%s 等待=%.0fms" % (
            did, ap_.get("game_id"), ap_.get("seat"), ap_.get("action"), t_attempt, t_out, wait_ms))
    if not timeout_records:
        print("  （无——本批日志里没有真实的 HTTP 层超时事件）")

    print("\n最近 5 次「draw 阶段计算耗时最长」的决策（compute 逼近/挤占 3s 出牌预算的代理信号，"
          "不是真的超时——服务器超时不会体现在我们自己的日志里，只能靠 prepare_ms 逼近 3000ms 推断风险）：")
    draw_slow = sorted(
        ((p.get("client_prepare_ms") or 0, did, t, p) for did, (t, p) in decisions.items() if p.get("phase") == "draw"),
        reverse=True)
    for prepare, did, t, p in draw_slow[:5]:
        print("  decision_id=%s game_id=%s time=%s prepare_ms=%.0f action_request_ms=%s" % (
            did, p.get("game_id"), t, prepare, p.get("action_request_ms")))

    print("\n=== 阶段二动态预算公式用的常数 ===")
    action_sorted = sorted(v for v in action_ms_all if v is not None)
    print("action 往返 p99（决策完成->action返回，成功路径）：%.0fms" % _percentile(action_sorted, 0.99))
    notify_sorted = sorted(notify_wall_gaps)
    print("\"服务端开始计时\"到\"我们拿到快照\"的估计值：**估不出真值**，用 notify 到达->state 返回的挂钟差"
          "做下界近似，p50=%.0fms p99=%.0fms（真正的服务端计时起点可能更早，这只是我们能观测到的下界）" % (
              _percentile(notify_sorted, 0.50), _percentile(notify_sorted, 0.99)))
    print("快照里没有服务端回合开始时间/截止时间字段（通读 snapshot.get(...) 调用点确认，见模块 docstring）")


def _iso_to_ms(iso_str):
    from datetime import datetime
    if not iso_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return 0.0
    return dt.timestamp() * 1000.0


if __name__ == "__main__":
    main()
