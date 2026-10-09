"""下载本赛事里我们打过的对局（每场全部局的事件流），存到 models/events/<game_id>.json，
和自由匹配的对局文件同一格式，现有分析工具都能直接读。

    python3 tools/fetch_tourney_games.py            # 赛事 id 从参赛令牌的 /api/me 取
    python3 tools/fetch_tourney_games.py --tid t_e3c195576228

- 对局列表：参赛令牌查 GET /api/tournaments/{id} 的 my_games（含各阶段全部场次）。
- 事件流：门户 GET /portal/api/games/{game_id}/events（portal_cookie.txt 的 majiang_sid）；
  没打完的场返回 403 GAME_NOT_FINISHED，跳过，下次再跑会补上。已下载的不重复下载。
- 只发很少的请求（每场 1 次，间隔 1 秒），不影响正在打的 bot。不打印令牌/cookie。
"""
import argparse
import http.client
import json
import os
import ssl
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mj.api import ApiError, MahjongApi  # noqa: E402
from mj.token import resolve_token  # noqa: E402

HOST, PORT = "10.240.169.190", 18080


def _cookie():
    for line in open(os.path.join(ROOT, "portal_cookie.txt"), encoding="utf-8"):
        if line.startswith("majiang_sid="):
            return line.strip().split("=", 1)[1]
    raise SystemExit("portal_cookie.txt 里没有 majiang_sid=... 这一行")


def _tournament(api, tid):
    for _ in range(8):
        try:
            return api.tournament(tid) or {}
        except ApiError as error:
            if error.status != 404:
                raise
            time.sleep(1.5)          # TOURNAMENT_GONE：服务端暂时不可达，重试
    raise SystemExit("赛事详情连续 404，稍后再跑")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tid", default=None)
    ap.add_argument("--out", default=os.path.join(ROOT, "models", "events"))
    args = ap.parse_args()
    api = MahjongApi("https://%s:%d" % (HOST, PORT), resolve_token(kind="scoped"))
    tid = args.tid or (api.me() or {}).get("tournament_id")
    if not tid:
        raise SystemExit("参赛令牌没有绑定赛事，用 --tid 指定")
    info = _tournament(api, tid)
    games = list(dict.fromkeys(info.get("my_games") or []))
    print("赛事 %s 状态 %s：我们的对局 %d 场" % (tid, info.get("status"), len(games)))
    os.makedirs(args.out, exist_ok=True)
    ctx = ssl._create_unverified_context()
    cookie = _cookie()
    got = skip = wait = 0
    for gid in games:
        path = os.path.join(args.out, gid + ".json")
        if os.path.exists(path):
            skip += 1
            continue
        for attempt in range(4):
            conn = http.client.HTTPSConnection(HOST, PORT, timeout=30, context=ctx)
            conn.request("GET", "/portal/api/games/%s/events" % gid,
                         headers={"Cookie": "majiang_sid=" + cookie, "Accept": "*/*",
                                  "Referer": "https://%s:%d/portal/" % (HOST, PORT)})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")
            conn.close()
            if resp.status == 429:
                time.sleep(3 * (attempt + 1))
                continue
            break
        if resp.status == 200:
            data = json.loads(body)
            data.setdefault("game_id", gid)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            got += 1
            print("  %s  OK  %d 局" % (gid, len(data.get("rounds") or data.get("blocks") or [])))
        elif "GAME_NOT_FINISHED" in body:
            wait += 1
        else:
            print("  %s  HTTP %s %s" % (gid, resp.status, body[:120]))
        time.sleep(1)
    print("新下载 %d 场，已存在 %d 场，未打完 %d 场 -> %s" % (got, skip, wait, args.out))


if __name__ == "__main__":
    main()
