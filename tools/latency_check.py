"""10/6 实测验收：响应窗口静默（MJ_RESPONSE_QUIET）上线后，响应超时 / 丢失的吃碰 / 拉取延迟是否达标。
阈值来自外部评审（docs/EXPERT_Q_LATENCY.md 的回复）。只读日志和已采集的对局文件，不联网。

先采集对局：python3 tools/collect_all_rooms.py
    python3 tools/latency_check.py --since 2026-10-06T01:00           # since = 这批 bot 启动时刻（UTC，日志里的时间）
    python3 tools/latency_check.py --since 2026-10-03T17:42 --until 2026-10-04T01:00   # 同口径算 10/04 基线
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from baotou_funnel import room_first_seen  # noqa: E402
from build_dataset import iter_decisions  # noqa: E402
from mining_common import OUR_NAME, OUR_UID, merge_rounds  # noqa: E402
from mj import bot  # noqa: E402
from mj.fit import frozen_file_weights  # noqa: E402


def q(a, x):
    a = sorted(a)
    return a[int(x * (len(a) - 1))] if a else float("nan")


def log_metrics(since, until):
    fetch, pace, per_sec = [], [], Counter()
    n429, notifier_exit, fallback = 0, 0, 0
    quiet = {}
    rejected = Counter()
    for path in sorted(glob.glob("logs/2026-*.jsonl")):
        day = os.path.basename(path)[:10]
        if day < since[:10] or (until and day > until[:10]):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                t = line[10:36] if line.startswith('{"time": "') else ""
                if t[:19] < since[:19] or (until and t[:19] > until[:19]):
                    continue
                if '"state_request_metric"' in line:
                    p = json.loads(line)["payload"]
                    fetch.append(p["elapsed_ms"])
                    per_sec[t[:19]] += 1
                    if p.get("pace_ms") is not None:
                        pace.append(p["pace_ms"])
                    if p.get("n429"):
                        n429 = max(n429, p["n429"])
                    if p.get("quiet_n") is not None:
                        quiet[p["game_id"]] = max(quiet.get(p["game_id"], 0), p["quiet_n"])
                elif '"notifier_exit"' in line:
                    notifier_exit += 1
                elif '"safe_fallback"' in line:
                    fallback += 1
                elif '"action_rejected"' in line:
                    err = json.loads(line)["payload"].get("error", "")
                    rejected[err.split('"message":"')[-1].split('"')[0] if "message" in err else err[:40]] += 1
    busy = sum(1 for v in per_sec.values() if v >= 14) / max(1, len(per_sec))
    return {"fetch": fetch, "pace": pace, "busy": busy, "n429": n429, "notifier_exit": notifier_exit,
            "fallback": fallback, "rejected": rejected, "quiet": sum(quiet.values()), "n_fetch": len(fetch)}


def _init():
    frozen_file_weights().__enter__()


def scan(path):
    """-> Counter: 局数、我方各窗口超时、出牌超时、(窗口, 实际, 生产想做) 组合"""
    out = Counter()
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return out
    uids = [s.get("user_id") for s in game.get("seats") or []]
    if OUR_UID not in uids:
        return out
    me = uids.index(OUR_UID)
    rounds = {r["round_no"]: r for r in merge_rounds(game)}
    for r in rounds.values():
        out["rounds"] += 1
        for e in r["events"]:
            if e.get("type") == "timeout" and e.get("seat") == me:
                d = e.get("data") or {}
                out["timeout_%s_%s" % (d.get("kind"), d.get("window"))] += 1
            if e.get("type") == "pass" and e.get("seat") == me:
                out["pass_explicit"] += 1
    for rec in iter_decisions(path, keep=lambda nm, ph, ac: 1.0 if nm == OUR_NAME and ph != "draw" else 0.0, seed=0):
        snap = rec["snapshot"]
        i = int(rec["id"].split(":")[2])
        kind = None
        for e in rounds[rec["round_no"]]["events"][i + 1:i + 14]:
            if e.get("seat") == me and e["type"] in ("pass", "timeout", "chi", "peng", "gang"):
                kind = e["type"]
                break
        try:
            want = (bot._choose_action_production(snap) or {}).get("action") or "pass"
        except Exception:   # noqa: BLE001
            want = "error"
        out[("win", rec["phase"], kind, want)] += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="UTC，如 2026-10-06T01:00（bot 启动时刻）")
    ap.add_argument("--until", default=None)
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()
    first = room_first_seen()
    rooms = {r for r, t in first.items() if t >= args.since and (not args.until or t <= args.until)}
    files = [p for p in glob.glob("models/events/*.json") if os.path.basename(p).split("_r1")[0] in rooms]
    tot = Counter()
    with Pool(args.jobs, initializer=_init) as pool:
        for c in pool.imap_unordered(scan, files, chunksize=2):
            tot.update(c)
    lm = log_metrics(args.since, args.until)
    n = max(1, tot["rounds"])
    lost = {w: sum(v for k, v in tot.items() if isinstance(k, tuple) and k[2] == "timeout" and k[3] == w)
            for w in ("chi", "peng", "gang")}
    done = {w: sum(v for k, v in tot.items() if isinstance(k, tuple) and k[2] == w and k[3] == w)
            for w in ("chi", "peng", "gang")}
    resp_to = tot["timeout_response_peng"] + tot["timeout_response_chi"]
    resp_rate = resp_to / max(1, resp_to + tot["pass_explicit"] + sum(done.values()))
    lost_all = sum(lost.values())

    def verdict(ok, bad):
        return "通过" if ok else ("回退" if bad else "观察")

    print("=== latency_check：%s ~ %s，房 %d，对局文件 %d，局 %d（没采集的房先跑 tools/collect_all_rooms.py）===" % (
        args.since, args.until or "现在", len(rooms), len(files), tot["rounds"]))
    print("%-34s %-22s %-22s %s" % ("指标", "本批", "10/04 基线", "判定"))
    print("%-34s %-22s %-22s %s" % ("想吃碰杠却超时（次 / 每局）", "%d / %.3f" % (lost_all, lost_all / n), "约 0.155/局",
                                    verdict(lost_all <= 10 * n / 240, lost_all >= 20 * n / 240)))
    print("    其中 吃 %d/%d  碰 %d/%d  杠 %d/%d（超时丢 / 实际做成）" % (lost["chi"], done["chi"], lost["peng"], done["peng"],
                                                               lost["gang"], done["gang"]))
    print("%-34s %-22s %-22s %s" % ("响应窗口超时率", "%.1f%%（%.2f 次/局）" % (100 * resp_rate, resp_to / n), "8%（2.6 次/局）",
                                    verdict(resp_rate <= 0.03, resp_rate > 0.05)))
    f = lm["fetch"]
    print("%-34s %-22s %-22s %s" % ("拉取延迟 p50 / p90（ms）", "%.0f / %.0f" % (q(f, .5), q(f, .9)), "356 / 605",
                                    verdict(q(f, .5) <= 60 and q(f, .9) <= 200, q(f, .5) > 150)))
    if lm["pace"]:
        print("    其中本地限速排队 p50 / p90：%.0f / %.0f ms" % (q(lm["pace"], .5), q(lm["pace"], .9)))
    print("%-34s %-22s %-22s %s" % ("≥14 次/秒的秒数占比", "%.0f%%" % (100 * lm["busy"]), ">80%",
                                    verdict(lm["busy"] <= 0.10, lm["busy"] > 0.40)))
    print("%-34s %-22s %-22s %s" % ("出牌超时", str(tot["timeout_discard_None"]), "3 / 30 房",
                                    verdict(tot["timeout_discard_None"] <= 2, tot["timeout_discard_None"] >= 3)))
    print("%-34s %-22s %-22s %s" % ("429 / 通知线程退出 / 兜底触发", "%d / %d / %d" % (lm["n429"], lm["notifier_exit"], lm["fallback"]),
                                    "看不到", verdict(lm["n429"] + lm["notifier_exit"] + lm["fallback"] == 0, True)))
    late = lm["rejected"].get("peng only in peng window", 0) + lm["rejected"].get("chi only in chi window", 0)
    print("%-34s %-22s %-22s %s" % ("窗口外的吃碰 409", str(late), "27 / 30 房（约 0.011/局）",
                                    verdict(late / n <= 0.015, late / n > 0.03)))
    print("拉取 %.0f 次/局（基线约 130）；静默 %d 次（%.1f 次/局）" % (lm["n_fetch"] / n, lm["quiet"], lm["quiet"] / n))
    print("被拒动作明细：%s" % dict(lm["rejected"].most_common(6)))
    print("得分不作验收（240 局噪声约 ±1.5 分/局）。服务端代打比例用 tools/session_health.py 看。")


if __name__ == "__main__":
    main()
