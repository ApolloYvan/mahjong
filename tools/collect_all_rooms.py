"""动态 collect 自己参与过的全部 test-rooms（不再依赖手动传 room_id）。

用法:
    python3 tools/collect_all_rooms.py                 # 用默认 server, 抓全部房间
    python3 tools/collect_all_rooms.py --server <url>
    python3 tools/collect_all_rooms.py --status closed   # 只抓已结束的房间(默认)
    python3 tools/collect_all_rooms.py --status all       # 含还在进行中的房间

房间列表来自 /portal/api/test-rooms（分页），走 portal_cookie.txt 的
majiang_sid 会话（跟 tools/fetch_replays.py 用同一份 cookie）；每个房间
具体对局事件仍复用 mj.replay.collect_test_room()（即 tools/pipeline.py
collect 背后那套逻辑），走 /api/test-rooms/{room_id}/games，已验证不需要
额外 token。可以随时重复跑：已经存在的 models/events/<game_id>.json 会被
跳过，只补新增/新结束的房间——这就是"动态"的含义，不用每次手动列房间号。
"""
import argparse
import http.client
import json
import os
import ssl
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mj.replay import collect_test_room

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SERVER = "https://10.240.169.190:18080"


def _load_cookie():
    cookie_file = os.path.join(ROOT, "portal_cookie.txt")
    if not os.path.exists(cookie_file):
        raise SystemExit(f"缺少 {cookie_file}：写一行 'majiang_sid=<值>' 进去。")
    for line in open(cookie_file, encoding="utf-8"):
        line = line.strip()
        if line.startswith("majiang_sid="):
            return line.split("=", 1)[1]
    raise SystemExit(f"{cookie_file} 里没有找到 majiang_sid=... 这一行")


def list_rooms(cookie, context, per_page=24):
    rooms = []
    page = 1
    while True:
        conn = http.client.HTTPSConnection("10.240.169.190", 18080, timeout=20, context=context)
        try:
            conn.request("GET", f"/portal/api/test-rooms?page={page}&per_page={per_page}", headers={
                "Cookie": f"majiang_sid={cookie}",
                "Accept": "*/*",
                "Referer": "https://10.240.169.190:18080/portal/",
            })
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            if resp.status != 200:
                raise SystemExit(f"列表接口 HTTP {resp.status}: {body[:200]}")
            data = json.loads(body)
        finally:
            conn.close()
        rooms.extend(data["rooms"])
        if page >= data.get("total_pages", 1):
            break
        page += 1
        time.sleep(0.5)
    return rooms


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--status", default="closed", choices=["closed", "all"],
                        help="closed=只抓已结束的房间(默认，避免抓到还没打完的); all=不过滤")
    args = parser.parse_args()

    cookie = _load_cookie()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    rooms = list_rooms(cookie, context)
    if args.status == "closed":
        rooms = [r for r in rooms if r.get("status") == "closed"]
    print(f"共 {len(rooms)} 个房间待处理")

    total_new_batches = 0
    for room in sorted(rooms, key=lambda r: r.get("created_at", 0)):
        room_id = room["room_id"]
        try:
            n = collect_test_room(args.server, room_id)
            print(f"{room_id} ({room.get('status')}): 采集局数 {n}")
            total_new_batches += n
        except Exception as error:
            print(f"{room_id}: FAIL {type(error).__name__} {str(error)[:150]}")
        time.sleep(0.3)
    print(f"完成，累计采集局数 {total_new_batches} -> models/events/")


if __name__ == "__main__":
    main()
