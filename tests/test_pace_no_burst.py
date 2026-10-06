"""2026-10-06：限速器在深排队时不得"重开"成两股并发（旧规则排队 > 8 槽就从当前时刻重开 → 瞬时速率翻倍 → 429）。"""
import threading
import time
import unittest
from unittest.mock import patch

import mj.api as api_mod
from mj.api import MahjongApi


class PaceNoBurstTests(unittest.TestCase):
    @patch.object(api_mod, "PACE_RESET_SLOTS", 40)   # v1.3 起默认 40；显式写死，防止默认值以后再变时测试失去意义
    def test_deep_queue_keeps_rate(self):
        api = MahjongApi("https://example.invalid", "token-pace-test-%d" % time.time_ns(), pace_interval=0.01)
        stamps = []
        lock = threading.Lock()

        def worker():
            api._pace()
            with lock:
                stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(30)]   # 30 个同时到达 = 排队深 30 槽（旧规则会在第 9 个重开）
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stamps.sort()
        # 任意 0.05 秒窗口里最多约 5 个（间隔 0.01），留余量取 7；旧规则下重开会出现两股并发，窗口内远超这个数
        worst = max(sum(1 for s in stamps if a <= s < a + 0.05) for a in stamps)
        self.assertLessEqual(worst, 7, "限速器出现突发：0.05 秒内放出 %d 个" % worst)
        self.assertGreaterEqual(stamps[-1] - stamps[0], 0.25)


if __name__ == "__main__":
    unittest.main()
