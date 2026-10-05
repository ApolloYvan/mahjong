"""按 (game_id, round_no) 抓取「荣耀回放」里其他玩家的单局快照流。

用法:
    python3 tools/fetch_replays.py                       # 实时拉「胡大牌榜」watchable=true 的条目
    python3 tools/fetch_replays.py <game_id>:<round_no> [...]  # 手动指定，跳过榜单查询

2026-09-22: 目标列表原来是从榜单 JSON 手抄的一份写死列表，跑几次全是
skip，看不出榜单其实已经更新——改成每次运行都实时调用
/portal/api/leaderboard/huge-win?period=all（从 portal 页面源码里翻出来
的真实接口，用法见 tools/collect_all_rooms.py 同款 portal_cookie 会话），
自动发现新上榜的 (game_id, round_no)。「单场最高分榜」
(/portal/api/leaderboard/best-game) 结构上没有 round_no 字段，不是单局
回放能用的入口，这里不用。

回放接口本身是 /portal/api/replay/games/{game_id}/rounds/{round_no}（与
参与者专属的 /portal/api/games/{game_id}/events 不是同一套鉴权）——实测
确认三点:
  1. 只开放榜单标注的那一局，同一 game_id 换个 round_no 会 403 NOT_WATCHABLE
     ("该局不在可看的荣耀回放范围内")，不是整场回放。
  2. 榜单条目的 watchable=false 大概率也是 NOT_WATCHABLE，先只抓 true 的。
  3. 限流严格：连续几个请求就 429 RATE_LIMITED，必须退避重试。
返回体里 frames[].snapshot 里的 hands 是【全部 4 家的真实手牌】，比我们
自己的 round_ended.detail 汇总丰富得多，可以逐帧看对手怎么打的。

Cookie 从 portal_cookie.txt 读取（首行含 majiang_sid=...）。
输出到 models/replays/<game_id>_round<round_no>.json。
"""
import http.client
import json
import os
import ssl
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "models", "replays")


def fetch_leaderboard_targets(cookie, context):
    """实时拉「胡大牌榜」，返回 watchable=true 条目的 (game_id, round_no) 列表。"""
    conn = http.client.HTTPSConnection("10.240.169.190", 18080, timeout=20, context=context)
    try:
        conn.request("GET", "/portal/api/leaderboard/huge-win?period=all", headers={
            "Cookie": f"majiang_sid={cookie}",
            "Accept": "*/*",
            "Referer": "https://10.240.169.190:18080/portal/",
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        if resp.status != 200:
            raise SystemExit(f"榜单接口 HTTP {resp.status}: {body[:200]}")
        data = json.loads(body)
    finally:
        conn.close()
    targets = []
    for entry in data.get("top") or []:
        if entry.get("watchable") and entry.get("game_id") and entry.get("round_no") is not None:
            targets.append((entry["game_id"], entry["round_no"]))
    return targets


def _load_cookie():
    cookie_file = os.path.join(ROOT, "portal_cookie.txt")
    if not os.path.exists(cookie_file):
        raise SystemExit(f"缺少 {cookie_file}：写一行 'majiang_sid=<值>' 进去。")
    for line in open(cookie_file, encoding="utf-8"):
        line = line.strip()
        if line.startswith("majiang_sid="):
            return line.split("=", 1)[1]
    raise SystemExit(f"{cookie_file} 里没有找到 majiang_sid=... 这一行")


def fetch_one(game_id, round_no, cookie, context, max_retries=4):
    out_path = os.path.join(OUT_DIR, f"{game_id}_round{round_no}.json")
    if os.path.exists(out_path):
        return "skip", None
    path = f"/portal/api/replay/games/{game_id}/rounds/{round_no}"
    delay = 3.0
    for attempt in range(max_retries):
        conn = http.client.HTTPSConnection("10.240.169.190", 18080, timeout=20, context=context)
        try:
            conn.request("GET", path, headers={
                "Cookie": f"majiang_sid={cookie}",
                "Accept": "*/*",
                "Referer": "https://10.240.169.190:18080/portal/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            })
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            if response.status == 429:
                time.sleep(delay)
                delay *= 2
                continue
            if response.status != 200:
                return "http_error", f"{response.status} {body[:150]}"
            data = json.loads(body)
            with open(out_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            return "ok", len(data.get("frames") or [])
        except Exception as error:
            return "fail", f"{type(error).__name__} {str(error)[:120]}"
        finally:
            conn.close()
    return "rate_limited_giveup", None


def _parse_targets(args):
    targets = []
    for arg in args:
        game_id, _, round_no = arg.rpartition(":")
        targets.append((game_id, int(round_no)))
    return targets


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cookie = _load_cookie()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    if len(sys.argv) > 1:
        targets = _parse_targets(sys.argv[1:])
    else:
        targets = fetch_leaderboard_targets(cookie, context)
        print(f"实时榜单: watchable=true 共 {len(targets)} 条")

    counts = {}
    for game_id, round_no in targets:
        status, detail = fetch_one(game_id, round_no, cookie, context)
        counts[status] = counts.get(status, 0) + 1
        print(f"{game_id} round={round_no}: {status} {detail if detail is not None else ''}")
        if status == "ok":
            time.sleep(4)
    print(f"完成: {counts} -> {OUT_DIR}")


if __name__ == "__main__":
    main()
