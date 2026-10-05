"""首轮真实测试前的离线 preflight，外加可选的只读在线令牌作用域校验。

语义纠错（本轮"评审误读规则"纠错的第二项）：
- 默认离线模式只能确认 MJ_TOKEN 是否存在，**无法确认其作用域**（是全局
  自由匹配令牌，还是绑定某个正式赛事的 scoped 令牌——两者用的是同一个
  环境变量名，物理上无法仅凭字符串区分）。因此绝不能仅凭 MJ_TOKEN 存在
  就宣称"可以进入测试房"。
- 离线模式下检查全部通过时，状态命名为 ``OFFLINE_READY``，不得叫
  ``READY``——避免让人误以为"可以直接开始"。
- 新增可选的只读在线检查模式 ``--check-test-tokens``：从
  MJ_TOKEN_1..MJ_TOKEN_4 读取四枚测试 scoped token，分别调用
  ``GET /api/me`` 和 ``GET /api/tournaments/me/rules``，验证四枚令牌
  确实绑定同一个非空 tournament_id、且 config 与准备测试的 M/Rounds
  一致。全部通过后才输出 ``TEST_ROOM_READY``。该模式只读：不
  register、不 ready、不创建房间、不提交任何动作（只调用
  MahjongApi.me()/MahjongApi.rules()，均为纯 GET）。
- 任何输出都只给令牌安全指纹（token_fingerprint），不输出令牌原值。

默认（不带 --check-test-tokens）完全离线：不连接服务器、不创建房间、
不修改门户状态。

用法：
  python3 tools/live_preflight.py                    # 纯离线检查
  python3 tools/live_preflight.py --check-test-tokens # 额外做只读在线令牌校验
"""
import argparse
import importlib
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from mj.security import scan, token_fingerprint  # noqa: E402

REQUIRED_MODULES = ["mj.bot", "mj.api", "mj.rules", "mj.responses", "mj.state",
                     "mj.observability", "mj.datastore", "mj.logging", "mj.sanitize",
                     "mj.tournament"]

# 生产合法性定向测试：只跑覆盖需求书硬规则门禁的测试模块，不跑全量
# 套件（避免 preflight 本身耗时过长；全量套件由 CI/发布前流程单独跑）。
TARGETED_TEST_MODULES = [
    "tests.test_production_gate",
    "tests.test_bot_cli_token",
    "tests.test_security",
    "tests.test_response_gang",
    "tests.test_game_id_discovery",
    "tests.test_match_retry",
    "tests.test_run_test_room",
]

TEST_TOKEN_ENV_NAMES = ["MJ_TOKEN_1", "MJ_TOKEN_2", "MJ_TOKEN_3", "MJ_TOKEN_4"]
DEFAULT_SERVER = "https://10.240.169.190:18080"

# 三类令牌与三类入口对照表（需求二：输出必须明确区分，不得笼统地用
# "有没有 MJ_TOKEN" 一个信号去覆盖三种完全不同的场景）。
#
# 注意 global_token 与 formal_scoped_token 复用同一个环境变量名
# MJ_TOKEN——这正是"离线无法确认作用域"的根本原因：拿到一个非空字符串，
# 物理上无法区分它是全局自由匹配令牌还是绑定了具体赛事的 scoped 令牌，
# 必须通过在线调用（例如 GET /api/tournaments/me/rules 返回 tournament_id
# 是否非空）才能确认。
TOKEN_CATEGORIES = {
    "global_token": {
        "env_var": "MJ_TOKEN",
        "purpose": "自由对战：POST /api/match",
        "entry_point": "python3 -m mj.bot",
    },
    "test_scoped_tokens": {
        "env_var": "MJ_TOKEN_1..MJ_TOKEN_4",
        "purpose": "自建测试房：四枚令牌须绑定同一个测试锦标赛",
        "entry_point": "python3 tools/run_test_room.py",
    },
    "formal_scoped_token": {
        "env_var": "MJ_TOKEN",
        "purpose": "正式赛事：register/ready/多阶段状态机",
        "entry_point": "python3 -m mj.bot --wait",
    },
}
TOKEN_SCOPE_AMBIGUITY_NOTE = (
    "global_token 与 formal_scoped_token 复用同一个环境变量 MJ_TOKEN，"
    "离线阶段无法仅凭字符串存在区分两者——presence 不等于 scope 正确。"
    "test_scoped_tokens（MJ_TOKEN_1..4）同理：存在不代表四枚令牌真的绑定"
    "同一个测试锦标赛，只有 --check-test-tokens 的在线只读校验才能确认。"
)


def check_python_and_imports():
    problems = []
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except Exception as error:  # noqa: BLE001 - 需要捕获任意导入失败原因
            problems.append(f"import {name} failed: {error!r}")
    return {
        "ok": not problems,
        "python_version": sys.version.split()[0],
        "modules_checked": REQUIRED_MODULES,
        "problems": problems,
    }


def check_token_presence(environ=None):
    """离线检查：只能确认 MJ_TOKEN（global_token/formal_scoped_token 共用
    的环境变量）是否存在，只输出 present/absent 和安全指纹，绝不输出
    令牌原值。presence 不代表可以进入测试房或正式赛事——不作为
    OFFLINE_READY 的阻塞项（阻塞项见 run_preflight() 里的其余检查），
    只作为信息性展示。"""
    environ = environ if environ is not None else os.environ
    token = environ.get("MJ_TOKEN")
    return {
        "ok": True,  # 信息性检查，presence 与否都不阻塞 OFFLINE_READY
        "status": "present" if token else "absent",
        "fingerprint": token_fingerprint(token) if token else None,
        "note": "presence only; cannot confirm whether this is a global or "
                "formal-scoped token, or whether it is valid — see "
                "token_categories / --check-test-tokens for scope verification",
    }


def check_test_tokens_presence(environ=None):
    """离线检查：MJ_TOKEN_1..MJ_TOKEN_4（test_scoped_tokens）四枚测试
    令牌是否存在。同样只输出 present/absent 和安全指纹，presence 不代表
    四枚令牌真的绑定同一个测试锦标赛——真正的作用域校验需要
    --check-test-tokens 在线模式。信息性检查，不阻塞 OFFLINE_READY。"""
    environ = environ if environ is not None else os.environ
    tokens = {}
    for name in TEST_TOKEN_ENV_NAMES:
        token = environ.get(name)
        tokens[name] = {
            "status": "present" if token else "absent",
            "fingerprint": token_fingerprint(token) if token else None,
        }
    return {
        "ok": True,
        "tokens": tokens,
        "all_present": all(v["status"] == "present" for v in tokens.values()),
        "note": "presence only; use --check-test-tokens for online scope verification",
    }


def check_security_scan():
    hits = scan(REPO_ROOT)
    return {"ok": len(hits) == 0, "hit_count": len(hits), "hits": hits}


def check_log_dir_writable(log_dir="logs"):
    path = os.path.join(REPO_ROOT, log_dir)
    try:
        os.makedirs(path, exist_ok=True)
        probe_path = os.path.join(path, ".preflight_write_probe")
        with open(probe_path, "w", encoding="utf-8") as handle:
            handle.write("preflight")
        os.remove(probe_path)
        return {"ok": True, "path": path}
    except OSError as error:
        return {"ok": False, "path": path, "error": repr(error)}


def check_build_and_schema_versions():
    from mj.observability import SCHEMA_VERSION as log_schema_version, build_hash
    from mj.datastore import SCHEMA_VERSION as datastore_schema_version
    return {
        "ok": True,
        "build_hash": build_hash(),
        "log_schema_version": log_schema_version,
        "datastore_schema_version": datastore_schema_version,
    }


def check_targeted_production_tests():
    """离线运行生产合法性定向测试（不连网、不建房）。"""
    result = subprocess.run(
        [sys.executable, "-m", "unittest"] + TARGETED_TEST_MODULES,
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    return {
        "ok": result.returncode == 0,
        "modules": TARGETED_TEST_MODULES,
        "returncode": result.returncode,
        "summary": result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "",
    }


def run_preflight():
    """纯离线检查，绝不发起任何网络请求。全部阻塞项通过时 status 为
    ``OFFLINE_READY``（不叫 READY——presence 类检查不构成"可进入测试房"
    的证明，只有 --check-test-tokens 在线校验通过后才能升级为
    ``TEST_ROOM_READY``，见 main()）。"""
    blocking_checks = {
        "python_and_imports": check_python_and_imports(),
        "security_scan": check_security_scan(),
        "log_dir_writable": check_log_dir_writable(),
        "versions": check_build_and_schema_versions(),
        "targeted_production_tests": check_targeted_production_tests(),
    }
    informational_checks = {
        "token": check_token_presence(),
        "test_tokens": check_test_tokens_presence(),
    }
    offline_ok = all(v.get("ok") for v in blocking_checks.values())
    report = {
        "status": "OFFLINE_READY" if offline_ok else "NOT_READY",
        "token_categories": TOKEN_CATEGORIES,
        "token_scope_ambiguity_note": TOKEN_SCOPE_AMBIGUITY_NOTE,
        "checks": {**blocking_checks, **informational_checks},
    }
    return report


def check_test_room_tokens_online(expected_m=None, expected_rounds=None,
                                   server=DEFAULT_SERVER, environ=None):
    """只读在线校验（仅在 --check-test-tokens 显式启用时才会被调用；
    默认离线 preflight 绝不会触发本函数，不会发起任何真实网络请求）。

    - 从 MJ_TOKEN_1..MJ_TOKEN_4 读取四枚令牌；
    - 分别对每枚令牌调用 GET /api/me 与 GET /api/tournaments/me/rules；
    - 验证四枚令牌都有非空且相同的 tournament_id；
    - 若提供 expected_m/expected_rounds，验证 config 与之一致；
    - 只打印安全指纹，不打印任何令牌原值；
    - 只读：本函数只调用 MahjongApi.me()/MahjongApi.rules()，两者都是
      纯 GET 请求，不调用 register/ready/match/action，不创建房间、
      不提交任何动作。
    """
    from mj.api import ApiError, MahjongApi

    environ = environ if environ is not None else os.environ
    missing = [name for name in TEST_TOKEN_ENV_NAMES if not environ.get(name)]
    if missing:
        return {"ok": False, "reason": "missing environment variables: " + ", ".join(missing)}

    per_token = []
    tournament_ids = []
    configs = []
    for name in TEST_TOKEN_ENV_NAMES:
        token = environ[name]
        entry = {"env": name, "fingerprint": token_fingerprint(token)}
        try:
            api = MahjongApi(server, token)
            me = api.me()
            rules = api.rules()
            tid = me.get("tournament_id")
            entry["ok"] = True
            entry["tournament_id_present"] = bool(tid)
            tournament_ids.append(tid)
            configs.append(rules.get("config") if isinstance(rules, dict) else None)
        except ApiError as error:
            entry["ok"] = False
            entry["error"] = f"HTTP {error.status}"
        except (OSError, TimeoutError) as error:
            entry["ok"] = False
            entry["error"] = "network_error: " + type(error).__name__
        per_token.append(entry)

    all_calls_ok = all(e.get("ok") for e in per_token)
    same_tournament = all_calls_ok and len(set(tournament_ids)) == 1 and bool(tournament_ids[0])

    config_ok = True
    config_detail = None
    if same_tournament and (expected_m is not None or expected_rounds is not None):
        config = configs[0] if configs else None
        config_detail = config
        if not isinstance(config, dict):
            config_ok = False
        else:
            if expected_m is not None and config.get("M") != expected_m:
                config_ok = False
            if expected_rounds is not None and config.get("Rounds") != expected_rounds:
                config_ok = False

    ok = all_calls_ok and same_tournament and config_ok
    return {
        "ok": ok,
        "per_token": per_token,
        "same_tournament": same_tournament,
        "tournament_id_present": bool(tournament_ids[0]) if tournament_ids else False,
        "config_ok": config_ok,
        "config": config_detail,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-test-tokens", action="store_true",
                        help="额外做只读在线令牌作用域校验（GET /api/me + "
                             "GET /api/tournaments/me/rules），验证四枚 "
                             "MJ_TOKEN_1..4 绑定同一测试锦标赛且 config "
                             "与期望 M/Rounds 一致。不 register/ready/建房/"
                             "提交动作。默认不启用（完全离线）。")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--expected-m", type=int, default=1,
                        help="--check-test-tokens 校验用：期望的 config.M（默认 1）")
    parser.add_argument("--expected-rounds", type=int, default=1,
                        help="--check-test-tokens 校验用：期望的 config.Rounds（默认 1）")
    args = parser.parse_args()

    report = run_preflight()

    if args.check_test_tokens:
        online = check_test_room_tokens_online(
            expected_m=args.expected_m, expected_rounds=args.expected_rounds,
            server=args.server,
        )
        report["test_tokens_online_check"] = online
        if report["status"] == "OFFLINE_READY" and online.get("ok"):
            report["status"] = "TEST_ROOM_READY"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["status"] != "TEST_ROOM_READY":
            sys.exit(1)
        return

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "OFFLINE_READY":
        sys.exit(1)


if __name__ == "__main__":
    main()
