import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from mj.api import MahjongApi


def _mock_transport(conn_cls):
    conn = conn_cls.return_value
    response = conn.getresponse.return_value
    response.status = 200
    response.read.return_value = b"{}"
    times = []
    conn.request.side_effect = lambda *args, **kwargs: times.append(time.monotonic())
    return conn, times


class PacerTests(unittest.TestCase):
    """P0 修复：限速只作用于 GET /api/games/{id}/state（api.state()）——
    这是唯一有官方每令牌 16 次/秒频率限制的端点。action()/match()/me()/
    rules() 等不再占用同一个槽位队列，见 test_action_not_paced_behind_state_queue。"""

    def setUp(self):
        MahjongApi._pacers.clear()

    def test_pacer_spaces_state_requests(self):
        api = MahjongApi("https://example.invalid", "tok-spaces", pace_interval=0.05)
        with patch("http.client.HTTPSConnection") as conn_cls:
            conn, times = _mock_transport(conn_cls)
            api.state("g1")
            api.state("g1")
            api.state("g1")
        self.assertEqual(len(times), 3)
        # 槽位构造保证 ≥50ms 间距; 断言余量放宽到 30ms 抗系统抖动,
        # 仍能抓住未限速时的 ~0ms 间隔回归
        self.assertGreaterEqual(times[1] - times[0], 0.03)
        self.assertGreaterEqual(times[2] - times[1], 0.03)

    def test_pacer_disabled_sends_immediately(self):
        api = MahjongApi("https://example.invalid", "token", pace_interval=0)
        with patch("http.client.HTTPSConnection") as conn_cls:
            conn, times = _mock_transport(conn_cls)
            api.state("g1")
            api.state("g1")
        self.assertLess(times[1] - times[0], 0.045)

    def test_pacer_shared_per_token_not_across_tokens(self):
        with patch("http.client.HTTPSConnection") as conn_cls:
            conn, times = _mock_transport(conn_cls)
            same_a = MahjongApi("https://example.invalid", "tok-a", pace_interval=0.05)
            same_b = MahjongApi("https://example.invalid", "tok-a", pace_interval=0.05)
            other = MahjongApi("https://example.invalid", "tok-b", pace_interval=0.05)
            same_a.state("g1")
            same_b.state("g1")
            gap_same = times[1] - times[0]
            before = time.monotonic()
            other.state("g1")
            gap_other = times[2] - before
        self.assertGreaterEqual(gap_same, 0.03)
        self.assertLess(gap_other, 0.045)

    def test_me_match_rules_action_are_never_paced(self):
        """低频/动作关键路径调用不占用 state 槽位：即便先排了一大堆
        state 请求槽位，me()/match()/rules()/action() 仍应几乎立即发出，
        不受 pace_interval 影响。"""
        api = MahjongApi("https://example.invalid", "tok-unpaced", pace_interval=1.0)
        with patch("http.client.HTTPSConnection") as conn_cls:
            conn, times = _mock_transport(conn_cls)
            # 先占用一个槽位（下一个 state 请求要等 1s），但非 state 调用不应受影响。
            api.state("g1")
            before = time.monotonic()
            api.me()
            api.match({"M": 10, "Rounds": 8})
            api.rules()
            api.action("g1", "discard", tile="1w")
            elapsed = time.monotonic() - before
        self.assertEqual(len(times), 5)
        self.assertLess(elapsed, 0.2)

    def test_action_not_paced_behind_state_queue(self):
        """P0 核心断言：state 请求队列积压后，action 仍能立即进入发送
        路径（不排在 state 槽位后面）。"""
        api = MahjongApi("https://example.invalid", "tok-action", pace_interval=0.5)
        with patch("http.client.HTTPSConnection") as conn_cls:
            conn, times = _mock_transport(conn_cls)
            # 连续排队 5 个 state 请求槽位（下一个可用槽位在 ~2.5s 之后）。
            for _ in range(5):
                api.state("g1")
            before = time.monotonic()
            api.action("g1", "discard", tile="1w")
            elapsed = time.monotonic() - before
        self.assertLess(elapsed, 0.2)

    def test_concurrent_state_polling_does_not_delay_action_thread(self):
        """确定性并发测试（任务书要求）：多个线程持续对同一令牌发起
        state 轮询（构造积压），同时另一线程发起 action——action 必须
        几乎立即完成，不被 state 槽位队列拖慢。使用真实 threading.Thread
        （与生产 play_game 的并发对局线程模型一致），而不是单线程模拟
        时间流逝。"""
        api = MahjongApi("https://example.invalid", "tok-concurrent", pace_interval=0.1)
        action_elapsed = {}
        stop = threading.Event()

        def fake_https_connection(*args, **kwargs):
            conn = MagicMock()
            response = MagicMock()
            response.status = 200
            response.read.return_value = b"{}"
            conn.getresponse.return_value = response
            return conn

        with patch("http.client.HTTPSConnection", side_effect=fake_https_connection):
            def poll_state():
                while not stop.is_set():
                    try:
                        api.state("g1")
                    except Exception:
                        pass

            pollers = [threading.Thread(target=poll_state, daemon=True) for _ in range(4)]
            for thread in pollers:
                thread.start()
            time.sleep(0.15)  # 让 state 槽位队列真正积压起来

            def do_action():
                started = time.monotonic()
                api.action("g1", "discard", tile="1w")
                action_elapsed["value"] = time.monotonic() - started

            action_thread = threading.Thread(target=do_action)
            action_thread.start()
            action_thread.join(timeout=2.0)
            stop.set()
            for thread in pollers:
                thread.join(timeout=1.0)

        self.assertIn("value", action_elapsed)
        self.assertLess(action_elapsed["value"], 0.3)


if __name__ == "__main__":
    unittest.main()
