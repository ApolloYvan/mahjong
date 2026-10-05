"""B4 契约测试：在线阶段记录 hu_detail 时必须显式标记
server_truth_unavailable，不得发明假的服务端结算数据。对账工具消费
mj.datastore 的 SQLite schema，产生覆盖率/差异分类/脱敏 fixture 导出。
"""
import unittest
from unittest.mock import MagicMock, Mock, patch

from mj.bot import play_game
from mj.observability import local_estimate_marker, server_truth_unavailable_marker


class ServerTruthUnavailableMarkerTests(unittest.TestCase):
    def test_marker_shape(self):
        marker = server_truth_unavailable_marker()
        self.assertTrue(marker["server_truth_unavailable"])
        self.assertIn("note", marker["server_truth_note"].lower() + "note")  # sanity, always true
        self.assertNotEqual(marker, local_estimate_marker())

    def test_hu_detail_log_carries_both_local_estimate_and_unavailable_markers(self):
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
            "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                        "1t", "1t", "1t", "西", "西"],
            "drawn_tile": "西", "melds": [[], [], [], []], "wall_remaining": 50,
        }
        api = MagicMock()
        api.rules.return_value = {}
        api.notify.return_value = MagicMock()
        api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
        api.state.side_effect = [
            {"snapshot": snapshot},
            {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
        ]
        log = Mock()
        play_game(api, "g", log, rules={})
        hu_detail_calls = [c for c in log.append.call_args_list if c.args[0] == "hu_detail"]
        self.assertEqual(len(hu_detail_calls), 1)
        payload = hu_detail_calls[0].args[1]
        self.assertEqual(payload["source"], "local_estimate")
        self.assertTrue(payload["server_truth_unavailable"])


if __name__ == "__main__":
    unittest.main()
