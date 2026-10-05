"""协议对齐纠错需求四：mj.bot.match_with_retry 自由匹配错误处理——
FEATURE_DISABLED/TOKEN_NOT_SCOPED/PORTAL_BINDING_REQUIRED 必须立即退出，
不得循环重试；只有明确的 MATCH_BUSY/429/网络瞬态才退避重试；不改变
POST /api/match 默认 M=10、Rounds=8。"""
import unittest
from unittest.mock import MagicMock, patch

from mj.api import ApiError
from mj.bot import match_with_retry


class MatchWithRetryPermanentErrorTests(unittest.TestCase):
    def test_feature_disabled_exits_immediately_without_retry(self):
        api = MagicMock()
        api.match.side_effect = ApiError(403, '{"error":"FEATURE_DISABLED"}')
        with patch("mj.bot.time.sleep") as mock_sleep:
            with self.assertRaises(SystemExit):
                match_with_retry(api, max_seconds=100)
        api.match.assert_called_once()
        mock_sleep.assert_not_called()

    def test_token_not_scoped_exits_immediately_without_retry(self):
        api = MagicMock()
        api.match.side_effect = ApiError(400, '{"error":"TOKEN_NOT_SCOPED"}')
        with patch("mj.bot.time.sleep") as mock_sleep:
            with self.assertRaises(SystemExit) as ctx:
                match_with_retry(api, max_seconds=100)
        api.match.assert_called_once()
        mock_sleep.assert_not_called()
        self.assertIn("TOKEN_NOT_SCOPED", str(ctx.exception))

    def test_portal_binding_required_exits_immediately_with_hint(self):
        api = MagicMock()
        api.match.side_effect = ApiError(403, '{"error":"PORTAL_BINDING_REQUIRED"}')
        with patch("mj.bot.time.sleep") as mock_sleep:
            with self.assertRaises(SystemExit) as ctx:
                match_with_retry(api, max_seconds=100)
        api.match.assert_called_once()
        mock_sleep.assert_not_called()
        self.assertIn("我的AI身份", str(ctx.exception))

    def test_match_busy_429_retries_then_succeeds(self):
        api = MagicMock()
        api.match.side_effect = [ApiError(429, '{"error":"MATCH_BUSY"}'), {"game_id": "g1"}]
        with patch("mj.bot.time.sleep") as mock_sleep:
            result = match_with_retry(api, max_seconds=100)
        self.assertEqual(result, {"game_id": "g1"})
        self.assertEqual(api.match.call_count, 2)
        mock_sleep.assert_called()

    def test_network_transient_error_retries(self):
        api = MagicMock()
        api.match.side_effect = [OSError("connection reset"), {"game_id": "g2"}]
        with patch("mj.bot.time.sleep") as mock_sleep:
            result = match_with_retry(api, max_seconds=100)
        self.assertEqual(result, {"game_id": "g2"})
        self.assertEqual(api.match.call_count, 2)
        mock_sleep.assert_called()

    def test_401_still_raises_immediately(self):
        api = MagicMock()
        api.match.side_effect = ApiError(401, '{"error":"UNAUTHORIZED"}')
        with patch("mj.bot.time.sleep") as mock_sleep:
            with self.assertRaises(ApiError) as ctx:
                match_with_retry(api, max_seconds=100)
        self.assertEqual(ctx.exception.status, 401)
        mock_sleep.assert_not_called()

    def test_default_match_payload_unchanged(self):
        """不改变 POST /api/match 默认 M=10、Rounds=8。"""
        api = MagicMock()
        api.match.return_value = {"game_id": "g3"}
        match_with_retry(api, max_seconds=100)
        api.match.assert_called_once_with({"M": 10, "Rounds": 8})


if __name__ == "__main__":
    unittest.main()
