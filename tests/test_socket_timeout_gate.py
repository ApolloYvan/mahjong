"""锁死：socket 超时必须走快速失败分支，不能被重试放大。

2026-09-23 实测根因。Python 3.9 里 socket.timeout 不是 TimeoutError（3.10 起
才合并），所以只写 `except TimeoutError` 的三处 handler 在本机是死代码，socket
超时会落进 OSError 重试分支：state() 是 retries=8、action() 是 retries=2，
单次调用最坏阻塞数分钟，而响应窗口只有约 2 秒。后果见
docs/audit/STALL_2026-09-23.md：四次 11~17 分钟整进程停滞，两个房间 69~73%
的牌由服务端代打而报废。
"""
import socket
import time
import unittest

from mj.api import ApiError, MahjongApi


class _TimingOutConnection:
    """每次请求都抛 socket.timeout，并记账被调用了几次。"""

    def __init__(self, counter):
        self.counter = counter
        self.timeout = None

    def request(self, *args, **kwargs):
        self.counter.append(1)
        raise socket.timeout("simulated read timeout")

    def close(self):
        pass


class SocketTimeoutFastFailTests(unittest.TestCase):
    def setUp(self):
        self.api = MahjongApi("http://127.0.0.1:1", "t", timeout=0.01, pace_interval=0)
        self.calls = []
        self.api._connection = lambda: _TimingOutConnection(self.calls)

    def test_socket_timeout_is_not_retried(self):
        """socket 超时只尝试一次就抛出——不得被 retries 放大。"""
        started = time.monotonic()
        with self.assertRaises(socket.timeout):
            self.api.request("GET", "/x", retries=8, backoff=0.4)
        self.assertEqual(len(self.calls), 1, "socket 超时被重试了 %d 次" % len(self.calls))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_state_path_does_not_amplify(self):
        """state() 的 retries=8 不得把一次 socket 超时放大成 8 次。"""
        with self.assertRaises(socket.timeout):
            self.api.state("g1", 0, timeout=2.0)
        self.assertEqual(len(self.calls), 1)

    def test_action_path_does_not_amplify(self):
        with self.assertRaises(socket.timeout):
            self.api.action("g1", "discard", "1w")
        self.assertEqual(len(self.calls), 1)

    def test_other_oserrors_still_retry(self):
        """非超时的网络错误仍然按 retries 重试——本次修复不得收窄它。"""
        calls = []

        class _Broken:
            timeout = None

            def request(self, *a, **k):
                calls.append(1)
                raise OSError("connection reset")

            def close(self):
                pass

        self.api._connection = lambda: _Broken()
        with self.assertRaises(ApiError):
            self.api.request("GET", "/x", retries=3, backoff=0.0)
        self.assertEqual(len(calls), 4, "retries=3 应当总共尝试 4 次")
