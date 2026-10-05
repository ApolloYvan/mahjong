"""安全启动测试房：优先从 MJ_TOKEN_1..MJ_TOKEN_4 环境变量读取四枚测试
scoped token（推荐），也兼容旧版四位置参数方式。缺少任一环境变量时快速
失败，只提示变量名，不显示任何令牌值；任何输出/日志都不打印令牌原值。

推荐用法：
  MJ_TOKEN_1='...' MJ_TOKEN_2='...' MJ_TOKEN_3='...' MJ_TOKEN_4='...' python3 tools/run_test_room.py

旧版兼容用法（不推荐，令牌会出现在 shell 历史/进程列表里）：
  python3 tools/run_test_room.py <token1> <token2> <token3> <token4>
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.api import ApiError, MahjongApi
from mj.bot import play_game
from mj.logging import DecisionLog
from mj.replay import collect_test_room
from mj.security import token_fingerprint

TOKEN_ENV_NAMES = ["MJ_TOKEN_1", "MJ_TOKEN_2", "MJ_TOKEN_3", "MJ_TOKEN_4"]


def resolve_tokens(cli_tokens, environ):
    """令牌解析：显式命令行四位置参数（旧版兼容） > MJ_TOKEN_1..4
    环境变量（推荐）。命令行参数存在时优先使用；否则从环境变量读取，
    缺少任一变量时抛 ValueError，消息只包含缺失的变量名，不包含任何
    候选令牌值。纯函数，便于离线单测优先级与缺失场景。"""
    if cli_tokens:
        return list(cli_tokens)
    missing = [name for name in TOKEN_ENV_NAMES if not environ.get(name)]
    if missing:
        raise ValueError("missing required environment variables: " + ", ".join(missing))
    return [environ[name] for name in TOKEN_ENV_NAMES]


def main():
    parser = argparse.ArgumentParser(
        epilog="推荐用环境变量传四枚测试 scoped token，避免出现在 shell 历史/进程列表里：\n"
               "  MJ_TOKEN_1='...' MJ_TOKEN_2='...' MJ_TOKEN_3='...' MJ_TOKEN_4='...' "
               "python3 tools/run_test_room.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tokens", nargs="*", default=None,
                        help="四枚测试 scoped token（可选，旧版兼容方式）。"
                             "缺省时从 MJ_TOKEN_1..MJ_TOKEN_4 环境变量读取"
                             "（推荐，避免令牌出现在 shell 历史/进程列表里）。")
    parser.add_argument("--server", default="https://10.240.169.190:18080")
    args = parser.parse_args()

    if args.tokens and len(args.tokens) != 4:
        parser.error(f"tokens 位置参数必须恰好 4 个，收到 {len(args.tokens)} 个")

    try:
        tokens = resolve_tokens(args.tokens, os.environ)
    except ValueError as error:
        parser.error(str(error))
        return

    print("使用令牌指纹:", [token_fingerprint(t) for t in tokens])

    apis = [MahjongApi(args.server, token) for token in tokens]

    # 验证四个 /api/me 的 tournament_id 相同——四枚令牌必须绑定同一个
    # 测试锦标赛，不得混用不同赛事的令牌（混用会导致后续 ready/对局
    # 状态互相错位）。
    tournament_ids = [api.me().get("tournament_id") for api in apis]
    if len(set(tournament_ids)) != 1 or not tournament_ids[0]:
        raise SystemExit("四枚令牌的 tournament_id 不一致或为空，无法确定唯一测试锦标赛")
    tournament_id = tournament_ids[0]

    for api in apis:
        try:
            api.ready(tournament_id)
        except ApiError as error:
            if error.status != 409 or "TOURNAMENT_STARTED" not in error.body:
                raise

    rules = apis[0].tournament(tournament_id)["config"]
    log = DecisionLog()
    while True:
        active = []
        for api in apis:
            info = api.me()
            active.extend((api, game) for game in info.get("active_games") or [])
        if not active:
            try:
                collected = collect_test_room(args.server, tournament_id, token=apis[0].token)
                if collected:
                    print("已采集对局:", collected)
            except Exception as error:
                print("采集失败:", repr(error))
            status = apis[0].tournament(tournament_id)
            print(json.dumps(status, ensure_ascii=False))
            if status.get("status") in ("finished", "closed", "void"):
                return
            if status.get("status") == "registering":
                time.sleep(0.5)
                continue
            time.sleep(0.2)
            continue
        with ThreadPoolExecutor(max_workers=len(active)) as pool:
            futures = [pool.submit(play_game, api, game["game_id"], log, rules) for api, game in active]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    print("对局线程异常:", repr(error))


if __name__ == "__main__":
    main()
