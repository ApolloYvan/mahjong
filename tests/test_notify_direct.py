"""锁死：SSE 唤醒流不得走环境代理。

2026-09-23 实测：mj/api.py 里 state/action 用 http.client（不读 HTTP_PROXY，
直连，p50 4~17ms），而 notify 原来用 urllib.request.urlopen（默认读环境代理）。
实测 proxy_bypass("10.240.169.190") is False —— 赛事服务器这个内网地址的
SSE 长连接被塞进了本机代理，决策请求没有。

后果：代理缓冲流式响应 -> 唤醒延迟 -> 退化成 5 秒 watchdog，而响应窗口只有
约 2 秒。实测"我方手上确有两张、能碰"的 2915 个窗口里 19.8%（576 次）因
超时完全没反应，占"没碰"的一多半——此前被误读成策略上的"该碰不碰"。
"""
import unittest
import urllib.request

from mj.api import MahjongApi


class NotifyBypassesProxyTests(unittest.TestCase):
    def test_direct_opener_has_no_proxy_handler(self):
        """build_opener(ProxyHandler({})) 的作用是把读环境变量的默认
        ProxyHandler 整个排除掉——所以正确结果是 handler 列表里没有它。"""
        names = [type(h).__name__ for h in MahjongApi._direct_opener().handlers]
        self.assertFalse(any("Proxy" in n for n in names),
                         "notify 的 opener 里出现了 ProxyHandler: %s" % names)

    def test_default_opener_would_use_proxy(self):
        """对照：默认 opener 确实带 ProxyHandler。若这条挂了，说明标准库行为
        变了，本文件第一条的意义需要重新评估。"""
        names = [type(h).__name__ for h in urllib.request.build_opener().handlers]
        self.assertTrue(any("Proxy" in n for n in names))

    def test_notify_does_not_call_module_level_urlopen(self):
        """notify 必须走自己的 opener，不得回退到 urllib.request.urlopen。"""
        import inspect
        source = inspect.getsource(MahjongApi.notify)
        self.assertNotIn("urllib.request.urlopen(", source)
        self.assertIn("_direct_opener()", source)

    def test_direct_opener_is_cached(self):
        self.assertIs(MahjongApi._direct_opener(), MahjongApi._direct_opener())
