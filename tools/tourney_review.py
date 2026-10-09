"""赛事逐局复盘：我们在本赛事打的每一场、每一局，和同桌对手（尤其是排行榜前列的）对照。

    python3 tools/fetch_tourney_games.py                      # 先下载打完的场
    python3 tools/tourney_review.py t_e3c195576228 > reports/tourney_review.txt

输出四段：
  1. 每场：四家总分与名次（标出谁是排行榜前 10）
  2. 每局一行：我们 / 本局赢家 —— 庄闲、起手白/向听、首次听牌、吃碰杠、到过爆头听、能胡没胡、超时、结果
  3. 汇总：我们 vs 同桌对手 vs 同桌前 10 名，按 全部/庄/闲/起手 0·1·2+ 白 分组
  4. 我们失分拆解：被谁胡、我们当庄时被胡、听了没胡、能胡没胡后输掉、超时
"""
import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]

from mining_common import OUR_UID, merge_rounds  # noqa: E402
from room_rounds import _seat_round  # noqa: E402


def _top10(tid):
    try:
        from mj.api import ApiError, MahjongApi
        from mj.token import resolve_token
        api = MahjongApi("https://10.240.169.190:18080", resolve_token(kind="scoped"))
        t = {}
        for _ in range(8):
            try:
                t = api.tournament(tid) or {}
                break
            except ApiError:
                time.sleep(1.5)
        rk = sorted(t.get("ranking") or [], key=lambda r: r.get("rank", 999))
        return {r["user_id"]: r["rank"] for r in rk[:10]}, {r["user_id"]: r for r in rk}
    except Exception as error:  # noqa: BLE001
        print("（取排行榜失败：%r，前 10 名标记省略）" % (error,))
        return {}, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tid")
    args = ap.parse_args()
    files = sorted(glob.glob(os.path.join(ROOT, "models", "events", args.tid + "_*.json")))
    if not files:
        raise SystemExit("还没有本赛事的对局文件，先跑 tools/fetch_tourney_games.py")
    top, ranking = _top10(args.tid)
    me_rank = ranking.get(OUR_UID, {})
    print("赛事 %s：已下载 %d 场。我们当前排名 %s，总分 %s" % (args.tid, len(files), me_rank.get("rank"),
                                                    me_rank.get("total_score")))
    if top:
        print("排行榜前 10：" + "  ".join("%d.%s" % (r, u) for u, r in sorted(top.items(), key=lambda x: x[1])))
    agg = defaultdict(lambda: defaultdict(float))
    loss = defaultdict(float)
    lines = []
    for p in files:
        with open(p, encoding="utf-8") as f:
            g = json.load(f)
        seats = g.get("seats") or []
        uids = [s.get("user_id") for s in seats]
        names = [(s.get("name") or s.get("nickname") or s.get("user_id") or "?")[:8] for s in seats]
        if OUR_UID not in uids:
            continue
        me = uids.index(OUR_UID)
        info = {r.get("round_no"): r for r in g.get("rounds") or []}
        tot = [0] * 4
        gid = os.path.basename(p)[:-5]
        lines.append("\n## %s  我们坐 %d 号位" % (gid, me))
        for rnd in merge_rounds(g):
            meta = info.get(rnd["round_no"]) or {}
            if not rnd.get("start_hands") or len(rnd["start_hands"]) != 4:
                continue
            end = next((e for e in rnd["events"] if e.get("type") == "round_ended"), None)
            d = (end or {}).get("data") or {}
            dealer = meta.get("dealer", d.get("dealer", rnd.get("dealer")))
            scores = meta.get("scores") or d.get("scores") or [0] * 4
            winner = None if d.get("draw") or meta.get("is_draw") else meta.get("winner", (end or {}).get("seat"))
            fan = d.get("fan")
            det = "/".join(d.get("detail") or [])
            for s in range(4):
                tot[s] += scores[s]
            xs = {s: _seat_round(rnd, s) for s in range(4)}
            show = [me] + ([winner] if winner is not None and winner != me else [])
            for s in show:
                x = xs[s]
                lines.append("r%-2d %-8s %s 起手白%d(+摸%d) 向听%d 首听%-3s 吃碰%-4s 爆头听%s 能胡没胡%d 超时%d | %s | %+d" % (
                    rnd["round_no"], ("我们" if s == me else names[s] + ("★%d" % top[uids[s]] if uids[s] in top else "")),
                    "庄" if dealer == s else "闲", x["j"], x["jd"], x["sh"],
                    x["ftn"] if x["ftn"] is not None else "-", x["claims"], "Y" if x["bt"] else "-", x["dec"], x["to"],
                    ("胡 %s番 %s" % (fan, det)) if winner == s else ("流局" if winner is None else "被胡"), scores[s]))
            for s in range(4):
                x = xs[s]
                grp = ["我们"] if s == me else (["同桌对手"] + (["同桌前10"] if uids[s] in top else []))
                keys = ["全部", "庄" if dealer == s else "闲", "起手%s白" % ("2+" if x["j"] >= 2 else x["j"])]
                for gname in grp:
                    for k in keys:
                        a = agg[(gname, k)]
                        a["n"] += 1
                        a["score"] += scores[s]
                        a["won"] += winner == s
                        a["fan"] += (fan or 0) if winner == s else 0
                        a["tn"] += x["ftn"] is not None
                        a["ftn"] += x["ftn"] or 0
                        a["bt"] += bool(x["bt"])
                        a["dec"] += x["dec"]
                        a["to"] += x["to"]
            # 我们的失分拆解
            if winner is not None and winner != me:
                loss["被胡总失分"] += scores[me]
                if dealer == me:
                    loss["当庄被胡失分"] += scores[me]
                if xs[me]["ftn"] is not None:
                    loss["我们听过牌却被胡(次)"] += 1
                if xs[me]["dec"]:
                    loss["能胡没胡后被胡(次)"] += 1
                    loss["能胡没胡后被胡失分"] += scores[me]
                if uids[winner] in top:
                    loss["被前10胡失分"] += scores[me]
            loss["超时(次)"] += xs[me]["to"]
        order = sorted(range(4), key=lambda s: -tot[s])
        lines.append("  本场总分：" + "  ".join("%s%s%s %+d" % (
            ("【我们】" if s == me else names[s]), ("★%d" % top[uids[s]]) if uids[s] in top else "",
            "(%s)" % uids[s] if s != me else "", tot[s]) for s in order))
    print("\n=== 汇总（每局平均）===")
    print("%-14s %5s %8s %7s %7s %8s %8s %9s %9s %7s" % ("", "局数", "分/局", "胜率", "番/胡", "听过牌", "首听摸", "到爆头听", "能胡没胡", "超时"))
    for k in ("全部", "庄", "闲", "起手0白", "起手1白", "起手2+白"):
        for gname in ("我们", "同桌对手", "同桌前10"):
            a = agg[(gname, k)]
            n = a["n"]
            if not n:
                continue
            print("%-14s %5d %+8.2f %6.1f%% %7.2f %7.1f%% %8.2f %8.1f%% %9.2f %7.2f" % (
                gname + "·" + k, n, a["score"] / n, 100 * a["won"] / n, a["fan"] / max(1, a["won"]),
                100 * a["tn"] / n, a["ftn"] / max(1, a["tn"]), 100 * a["bt"] / n, a["dec"] / n, a["to"] / n))
    print("\n=== 我们的失分拆解 ===")
    for k, v in loss.items():
        print("  %-16s %+.0f" % (k, v) if "失分" in k else "  %-16s %d" % (k, v))
    print("\n=== 逐局 ===（★n = 排行榜第 n 名；每局先列我们，再列本局赢家）")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
