# -*- coding: utf-8 -*-
"""开赛守门：确保我们真的能上场。开赛前跑它，别靠人盯终端。

2026-09-24 的实盘损失（错过「应牌友要求的四测」101 人海选，直接淘汰）：
上一轮自由匹配的 bot 07:31 打完房正常退出，赛事 08:00 开赛，中间 25 分钟
没有任何进程在跑。ready 只在开赛前受理（之后一律 409 TOURNAMENT_STARTED），
而接入指南 §2.6 还有一条：「分桌只认开赛时刻 ≤90s 内有已认证请求者」——
所以开赛那一刻进程必须活着，事后无法补救。

本工具做四件事，每 15 秒一轮：
  1. 读 start_at / register_deadline，打印**倒计时**
  2. 开赛前自动幂等 register + ready（409/403 原文打印，不静默）
  3. my_ready 没变 true 就持续告警（这是唯一真正重要的那个布尔值）
  4. 一直轮询保持「已认证请求」活着，满足 90s 保活规则

    MJ_TOKEN='...' python3 tools/tourney_guard.py
    MJ_TOKEN='...' python3 tools/tourney_guard.py --once      # 只体检一次就退出
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.api import ApiError, MahjongApi
from mj.token import describe, resolve_token


def _hms(seconds):
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(seconds))
    return "%s%d:%02d:%02d" % (sign, seconds // 3600, seconds % 3600 // 60, seconds % 60)


def check(api, auto):
    rows = (api.me() or {}).get("tournaments") or []
    if not rows:
        print("%s  没有可见赛事（令牌未绑定任何锦标赛，或全部已结束）"
              % time.strftime("%H:%M:%S"))
        return False
    all_ok = True
    for item in rows:
        tid = item.get("id") or item.get("tournament_id")
        start = item.get("start_at") or 0
        deadline = item.get("register_deadline") or 0
        now = time.time()
        registered = bool(item.get("my_registered"))
        ready = bool(item.get("my_ready"))
        status = item.get("status")
        stage = (item.get("stage") or {})
        line = ("%s  %s  阶段%s/%s %s/%s  报名=%s 出席=%s 资格=%s"
                % (time.strftime("%H:%M:%S"), tid, stage.get("no"), stage.get("total"),
                   status, item.get("stage_status"),
                   registered, ready, item.get("qualified")))
        if start:
            line += "  开赛倒计时 %s" % _hms(start - now)
        print(line)

        # 报名与出席都必须在开赛前完成；开赛后服务端一律 409，补发无意义但要看见
        if auto and tid:
            if not registered and (not deadline or now < deadline):
                _try(api.register, tid, "register")
            if registered and not ready:
                _try(api.ready, tid, "ready")

        if not registered:
            print("   ❌ 未报名。截止 %s"
                  % (time.strftime("%H:%M:%S", time.localtime(deadline)) if deadline else "无"))
            all_ok = False
        elif not ready:
            remain = (start - now) if start else None
            mark = "❌" if (remain is None or remain > 0) else "💀"
            print("   %s 已报名但**未确认出席**——这一项不变 true 就上不了场%s"
                  % (mark, "" if remain is None or remain > 0 else "（已开赛，本阶段无法挽回）"))
            all_ok = False
        elif status in ("running",) and not (item.get("my_games") or []):
            print("   ⚠ 已出席但还没分到对局，属正常（分桌中）——保持进程在跑")
        else:
            print("   ✅ 报名 + 出席都已确认")
    return all_ok


def _try(fn, tid, name):
    try:
        fn(tid)
        print("   → %s 提交成功" % name)
    except ApiError as error:
        print("   → %s 被拒: HTTP %s %s %s"
              % (name, error.status, error.code or "", (error.body or "")[:80]))
    except (OSError, TimeoutError) as error:
        print("   → %s 网络错误: %r" % (name, error))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="https://10.240.169.190:18080")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-auto", action="store_true", help="只体检，不自动提交 register/ready")
    a = ap.parse_args()

    token = resolve_token(kind="scoped")
    if not token:
        raise SystemExit("找不到令牌。存一次即可：\n"
                         "  python3 -c \"from mj.token import save_token; "
                         "print(save_token(input('令牌: ')))\"")
    print("令牌来源:", describe("scoped"))
    api = MahjongApi(a.server, token)

    print("开赛守门启动。它会一直轮询以满足「开赛时刻 ≤90s 内有已认证请求」的保活要求。")
    print("**看一个值就够了：出席=True。它不变 True 就一定上不了场。**\n")
    while True:
        try:
            ok = check(api, not a.no_auto)
        except ApiError as error:
            print("%s  查询失败: HTTP %s %s"
                  % (time.strftime("%H:%M:%S"), error.status, error.code or ""))
            ok = False
        except (OSError, TimeoutError) as error:
            print("%s  网络错误: %r" % (time.strftime("%H:%M:%S"), error))
            ok = False
        if a.once:
            raise SystemExit(0 if ok else 1)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
