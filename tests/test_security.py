import tempfile
import unittest

from mj.security import scan, token_fingerprint


class SecurityTests(unittest.TestCase):
    def test_scan_clean_project_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(directory + "/clean.py", "w", encoding="utf-8") as output:
                output.write("token = os.environ['MJ_TOKEN_1']")
            self.assertEqual(scan(directory), [])

    def test_repo_scan_is_clean_after_token_cleanup(self):
        """离线收尾 P0：tools/room_status.py、tools/probe_tokens.py、
        tools/server_latency_probe.py 曾硬编码真实令牌，mj.security.scan('.')
        必须命中 0 个文件。"""
        import os
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(scan(repo_root), [])

    def test_token_fingerprint_is_short_deterministic_and_not_the_token(self):
        """安全指纹：确定性、短小（远短于 64 位令牌样式）、且不等于原值，
        用于日志/CLI 输出里区分令牌而不泄露原值。"""
        token = "a" * 64
        fingerprint = token_fingerprint(token)
        self.assertEqual(fingerprint, token_fingerprint(token))
        self.assertNotEqual(fingerprint, token)
        self.assertLess(len(fingerprint), len(token))
        self.assertIsNone(token_fingerprint(None))
        self.assertIsNone(token_fingerprint(""))

