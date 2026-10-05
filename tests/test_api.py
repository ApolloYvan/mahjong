import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from mj.api import ApiError, MahjongApi


class ApiTests(unittest.TestCase):
    def test_network_error_retries_then_raises_api_error(self):
        api = MahjongApi("https://example.invalid", "token")
        with patch("http.client.HTTPSConnection") as conn_cls:
            conn = conn_cls.return_value
            conn.request.side_effect = OSError("offline")
            with self.assertRaises(ApiError) as raised:
                api.me()
        self.assertEqual(raised.exception.status, 0)
        self.assertEqual(conn.request.call_count, 4)

    def test_connection_reused_between_requests(self):
        api = MahjongApi("https://example.invalid", "token")
        with patch("http.client.HTTPSConnection") as conn_cls:
            conn = conn_cls.return_value
            response = conn.getresponse.return_value
            response.status = 200
            response.read.return_value = b"{}"
            api.me()
            api.me()
        self.assertEqual(conn_cls.call_count, 1)
        self.assertEqual(conn.request.call_count, 2)

    def test_rate_limit_retries_with_backoff(self):
        api = MahjongApi("https://example.invalid", "token", pace_interval=0)
        with patch("http.client.HTTPSConnection") as conn_cls, patch("mj.api.time.sleep") as sleeper:
            conn = conn_cls.return_value
            limited, ok = MagicMock(), MagicMock()
            limited.status, limited.read.return_value = 429, b"{}"
            ok.status, ok.read.return_value = 200, b"{}"
            conn.getresponse.side_effect = [limited, ok]
            result = api.me()
        self.assertEqual(result, {})
        sleeper.assert_called_once()

    def test_http_error_raises_without_retry(self):
        api = MahjongApi("https://example.invalid", "token")
        with patch("http.client.HTTPSConnection") as conn_cls:
            conn = conn_cls.return_value
            response = conn.getresponse.return_value
            response.status = 404
            response.read.return_value = b"NOT_FOUND"
            with self.assertRaises(ApiError) as raised:
                api.me()
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(conn.request.call_count, 1)

    def test_register_posts_to_tournament_scoped_endpoint(self):
        api = MahjongApi("https://example.invalid", "token")
        with patch("http.client.HTTPSConnection") as conn_cls:
            conn = conn_cls.return_value
            response = conn.getresponse.return_value
            response.status = 200
            response.read.return_value = b"{}"
            api.register("t_abc123")
        args, kwargs = conn.request.call_args
        method, path = args[0], args[1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/tournaments/t_abc123/register")
