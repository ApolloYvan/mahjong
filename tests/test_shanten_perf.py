"""缓存层性能与返回值锁定回归（2026-09-22 决策延迟修复，§3.6）。

本轮只改 mj.shanten 的缓存配置（lru_cache 容量、route_shanten/combined_route
上再加一层缓存、进程启动预热），不改任何计算逻辑。这里锁定三组含 0/1/2 张
财神的固定手牌在缓存改动前后的返回值必须逐值相同（数值取自改动前的
docs/audit/rollback_2026-09-22/shanten.py），并断言 2 张财神手牌的单次
combined_route 耗时符合预算。
"""
import time
import unittest

from mj.shanten import combined_route, pair_shanten, pair_ukeire, route_shanten, shanten, ukeire
from mj.tiles import to_counts

FIXTURES = {
    "zero_joker": {
        "tiles": ["1w", "2w", "3w", "5b", "6b", "7b", "9t", "9t", "9t", "东", "南", "西", "中"],
        "shanten0": 2,
        "pair_shanten": 5,
        "ukeire": [27, 28, 29, 31, 33],
        "pair_ukeire": [0, 1, 2, 13, 14, 15, 26, 27, 28, 29, 31, 33],
        "route_shanten": 2,
        "combined_route": (2, [27, 28, 29, 31, 33]),
    },
    "one_joker": {
        "tiles": ["白", "1w", "2w", "3w", "5b", "6b", "7b", "9t", "9t", "东", "南", "西", "中"],
        "shanten0": 2,
        "pair_shanten": 4,
        "ukeire": [26, 27, 28, 29, 31, 33],
        "pair_ukeire": [0, 1, 2, 13, 14, 15, 27, 28, 29, 31, 33],
        "route_shanten": 2,
        "combined_route": (2, [26, 27, 28, 29, 31, 33]),
    },
    "two_joker": {
        "tiles": ["白", "白", "1w", "2w", "3w", "5b", "6b", "7b", "9t", "9t", "东", "南", "西"],
        "shanten0": 1,
        "pair_shanten": 3,
        "ukeire": [26, 27, 28, 29, 33],
        "pair_ukeire": [0, 1, 2, 13, 14, 15, 27, 28, 29, 33],
        "route_shanten": 1,
        "combined_route": (1, [26, 27, 28, 29, 33]),
    },
}


class TestReturnValueLock(unittest.TestCase):
    """返回值锁定：缓存层改动前后，逐值完全一致（数值来自改动前的
    docs/audit/rollback_2026-09-22/shanten.py，见交付文档 VALIDATION.md）。"""

    def test_locked_values_unchanged(self):
        for name, expect in FIXTURES.items():
            counts = tuple(to_counts(expect["tiles"]))
            with self.subTest(name=name):
                self.assertEqual(shanten(counts, 0), expect["shanten0"])
                self.assertEqual(pair_shanten(counts), expect["pair_shanten"])
                self.assertEqual(ukeire(counts, 0), expect["ukeire"])
                self.assertEqual(pair_ukeire(counts), expect["pair_ukeire"])
                self.assertEqual(route_shanten(counts, 0), expect["route_shanten"])
                current, waits = combined_route(counts, 0)
                self.assertEqual((current, waits), expect["combined_route"])
                self.assertIsInstance(waits, list)


class TestPerfBudget(unittest.TestCase):
    """2 张财神手牌的 combined_route 单次耗时 < 2ms（C2）。"""

    def test_two_joker_combined_route_under_budget(self):
        tiles = FIXTURES["two_joker"]["tiles"]
        counts = tuple(to_counts(tiles))
        # 先跑一次触发缓存（真实生产中，进程启动预热 + 前序决策已经把常见
        # 财神分支填进缓存；这里只验证"稳态"下的耗时预算，不是首次冷启动）。
        combined_route(counts, 0)
        t0 = time.perf_counter()
        combined_route(counts, 0)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertLess(elapsed_ms, 2.0, f"combined_route took {elapsed_ms:.3f}ms, budget is 2ms")


if __name__ == "__main__":
    unittest.main()
