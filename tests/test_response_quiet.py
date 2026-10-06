"""2026-10-05 响应窗口静默（MJ_RESPONSE_QUIET，docs/EXPERT_Q_LATENCY.md）：
回应完一个窗口后，窗口剩余时间不再为本场拉 /state；但下一个窗口必须及时看到并回应；关闭开关后行为与旧版一致。

模拟服务器按真实时间推进：[0,0.5) 别人出牌阶段 → [0.5,1.5) 碰窗口（我们可回应）→ [1.5,1.8) 下家出牌阶段 → [1.8,2.8) 另一个碰窗口 → 结束。"""
import os
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import mj.bot as bot  # noqa: E402

HAND = ["1w", "4w", "7w", "2t", "5t", "8t", "3b", "6b", "东", "南", "西", "北", "中"]


def _snap(phase, turn, wall, last_discard, responding):
    return {"seat": 0, "phase": phase, "turn": turn, "dealer": 1, "round_no": 1, "wall_remaining": wall,
            "scores": [0, 0, 0, 0], "drawn_tile": "", "last_discard": last_discard, "hand_counts": [13, 13, 13, 13],
            "responding_seats": responding, "my_hand": list(HAND), "discards": [[], [], [], []],
            "melds": [[], [], [], []], "rules": {}, "god": {"baotou": False, "chain_count": 0, "catch_play": False,
                                                          "god_discarder_seat": -1}}


class _Server:
    def __init__(self):
        self.t0 = time.monotonic()
        self.state_calls = []      # (t, phase, wall)
        self.actions = []          # (t, action)

    def now(self):
        return time.monotonic() - self.t0

    def state(self, game_id, seq=0, timeout=None):
        t = self.now()
        if t < 0.5:
            s = _snap("draw", 1, 60, "", [])
        elif t < 1.5:
            s = _snap("response_peng", 1, 60, "9b", [0, 2, 3])
        elif t < 1.8:
            s = _snap("draw", 2, 59, "9b", [])
        elif t < 2.8:
            s = _snap("response_peng", 2, 59, "9t", [3, 0, 1])
        else:
            return {"seq": 999, "finished": True, "snapshot": dict(_snap("finished", 0, 0, "", []))}
        self.state_calls.append((t, s["phase"], s["wall_remaining"]))
        return {"seq": int(t * 1000), "snapshot": s}

    def action(self, game_id, **decision):
        self.actions.append((self.now(), decision.get("action")))
        return {}


def _run(quiet):
    server = _Server()
    api = MagicMock()
    api.rules.return_value = {}
    api.notify.return_value = MagicMock()
    api.notify.return_value.__iter__ = Mock(side_effect=TypeError("no sse in test"))
    api.state.side_effect = server.state
    api.action.side_effect = server.action
    with patch.object(bot, "RESPONSE_QUIET", quiet), patch.object(bot, "RECOVERY_WATCHDOG_INTERVAL", 0.05), \
            patch.object(bot, "WATCHDOG_INTERVAL", 0.05), patch.object(bot, "POLL_MIN_GAP", 0.05):
        bot.play_game(api, "g_test", MagicMock())
    return server


class ResponseQuietTests(unittest.TestCase):
    def test_quiet_skips_polls_but_catches_next_window(self):
        s = _run(0.95)
        passes = [t for t, a in s.actions if a == "pass"]
        self.assertEqual(len(passes), 2, s.actions)
        self.assertTrue(0.5 <= passes[0] < 1.5)
        self.assertTrue(1.8 <= passes[1] < 2.8, "第二个窗口必须在关窗前回应：%s" % passes)
        # 第一次看到碰窗口的那次拉取的上一次拉取时刻 = prev_sent；静默到 prev_sent+0.95，期间不得再拉
        k = next(i for i, c in enumerate(s.state_calls) if c[1] == "response_peng")
        prev_sent = s.state_calls[k - 1][0]
        during = [c for c in s.state_calls if passes[0] + 0.02 < c[0] < prev_sent + 0.9]
        self.assertEqual(during, [], "静默期内仍在拉取：%s" % during)

    def test_disabled_keeps_old_polling(self):
        s = _run(0.0)
        passes = [t for t, a in s.actions if a == "pass"]
        self.assertEqual(len(passes), 2, s.actions)
        after_first = [c for c in s.state_calls if passes[0] + 0.02 < c[0] < 1.4]
        self.assertGreater(len(after_first), 3, "关闭静默后应保持旧的轮询节奏")


if __name__ == "__main__":
    unittest.main()
