"""S2：统计每个玩家按房间奇偶分开的"局数/累计得分"，并选出训练用的"高手"——**用另一半房间选人**，避免选择偏差。

    python3 tools/train/player_stats.py            # 秒级~1 分钟

- 房间 = game_id 第二段；``room_hash % 2`` 把房间分成两半（0/1）。
- ``masters_from[q]`` = 用奇偶 q 这半房间的数据挑出的高手（按 累计得分/局 排名前 ``--top-n``，且局数 >= ``--min-rounds``）。
  训练数据里，奇偶 p 的房间只取 ``masters_from[1-p]`` 这批人的决策——选人用的数据和训练标签用的数据不相交，
  "高手"的好成绩不会因为选人时用了同一批对局而被高估。
- ``top_all`` = 全历史（两半合计）得分/局排名前 ``--top-all`` 的玩家，作为**补充**（训练时降权，见 build_s2.py）；
  它有选择偏差，所以只是补充、不进"另一半选人"的验证口径。
- 我们自己的账号（OUR）不参与。输出 ``tools/.cache/train/masters.json``（含各自的局数/得分/局，供复核）。
"""
import argparse
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))

from baotou_funnel import OUR  # noqa: E402
from build_dataset import room_of  # noqa: E402
from mining_common import discover_files, seat_names  # noqa: E402

OUT = os.path.join(ROOT, "tools", ".cache", "train", "masters.json")


def collect(files):
    stats = defaultdict(lambda: {"0": [0, 0.0], "1": [0, 0.0]})
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                game = json.load(f)
        except (OSError, ValueError):
            continue
        names = seat_names(game)
        if len(names) != 4:
            continue
        parity = str(room_of(os.path.basename(path))[1] % 2)
        for rnd in game.get("rounds") or []:
            sc = rnd.get("scores")
            if not isinstance(sc, list) or len(sc) != 4:
                continue
            for seat, name in enumerate(names):
                if name and name != OUR:
                    st = stats[name][parity]
                    st[0] += 1
                    st[1] += sc[seat]
    return stats


def rank(stats, parities, min_rounds, top_n):
    rows = []
    for name, st in stats.items():
        n = sum(st[p][0] for p in parities)
        tot = sum(st[p][1] for p in parities)
        if n >= min_rounds:
            rows.append((tot / n, n, name))
    rows.sort(reverse=True)
    return rows[:top_n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--min-rounds", type=int, default=150, help="选人用的那半房间里至少打过这么多局")
    ap.add_argument("--top-all", type=int, default=30)
    ap.add_argument("--min-rounds-all", type=int, default=300)
    ap.add_argument("--limit-files", type=int, default=None)
    args = ap.parse_args()
    files = discover_files(limit=args.limit_files)
    stats = collect(files)
    out = {"masters_from": {}, "top_all": [], "detail": {}}
    for q in ("0", "1"):
        rows = rank(stats, [q], args.min_rounds, args.top_n)
        out["masters_from"][q] = [r[2] for r in rows]
        out["detail"]["from_" + q] = [{"name": r[2], "rounds": r[1], "score_per_round": round(r[0], 3)} for r in rows]
    rows = rank(stats, ["0", "1"], args.min_rounds_all, args.top_all)
    out["top_all"] = [r[2] for r in rows]
    out["detail"]["top_all"] = [{"name": r[2], "rounds": r[1], "score_per_round": round(r[0], 3)} for r in rows]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    n_players = len(stats)
    a, b = set(out["masters_from"]["0"]), set(out["masters_from"]["1"])
    print("=== player_stats（%d 个文件，%d 名玩家；明细 %s） ===" % (len(files), n_players, OUT))
    for q in ("0", "1"):
        d = out["detail"]["from_" + q]
        print("用奇偶 %s 的房间选出 %d 人：得分/局 %s ... 最低 %s（局数 %s..%s）" % (
            q, len(d), d[0]["score_per_round"] if d else None, d[-1]["score_per_round"] if d else None,
            d[0]["rounds"] if d else None, d[-1]["rounds"] if d else None))
    print("两半选出的高手重合 %d 人（重合越多说明排名越稳定）；全历史补充名单 %d 人，其中不在任一半高手里的 %d 人" % (
        len(a & b), len(out["top_all"]), len(set(out["top_all"]) - a - b)))


if __name__ == "__main__":
    main()
