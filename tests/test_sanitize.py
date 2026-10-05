"""mj.sanitize 契约测试：覆盖 SONNET5_DATA_REVIEW.md P0 指出的三类真实
反例（UTF-8 错误行 / kind=error 的 Bearer 文本 / action_rejected token），
以及稳定假 ID 的盐机制。
"""
import unittest

from mj.sanitize import (
    MissingSaltError,
    get_salt,
    require_salt,
    sanitize,
    sanitize_text,
    scan_for_leaks,
    stable_pseudo_id,
)

REAL_LOOKING_TOKEN = "a" * 64  # 64 位十六进制令牌样式


class SanitizeTextTests(unittest.TestCase):
    def test_hex64_token_is_redacted_not_present(self):
        text = f"connection failed, Authorization token={REAL_LOOKING_TOKEN} rejected"
        cleaned = sanitize_text(text)
        self.assertNotIn(REAL_LOOKING_TOKEN, cleaned)
        self.assertIn("REDACTED", cleaned)

    def test_bearer_inline_is_redacted(self):
        text = "kind=error payload.error='Authorization: Bearer " + REAL_LOOKING_TOKEN + "'"
        cleaned = sanitize_text(text)
        self.assertNotIn(REAL_LOOKING_TOKEN, cleaned)

    def test_truncation_still_applies_after_redaction(self):
        cleaned = sanitize_text("x" * 500, limit=160)
        self.assertLessEqual(len(cleaned), 160 + len("...(truncated)"))


class LeakReproTests(unittest.TestCase):
    """三类真实反例：修复前会泄露，修复后必须不出现在输出里。"""

    def test_utf8_replacement_line_with_embedded_token_is_redacted(self):
        # UTF-8 replace 产生的占位符行，仍可能携带令牌片段。
        raw = f"broken \ufffd payload token={REAL_LOOKING_TOKEN}"
        cleaned = sanitize_text(raw)
        self.assertNotIn(REAL_LOOKING_TOKEN, cleaned)

    def test_kind_error_bearer_authorization_text_is_redacted(self):
        payload = {"game_id": "g1", "error": f"Authorization: Bearer {REAL_LOOKING_TOKEN}"}
        cleaned = sanitize(payload)
        self.assertNotIn(REAL_LOOKING_TOKEN, str(cleaned))

    def test_action_rejected_payload_error_token_is_redacted(self):
        payload = {
            "phase": "draw",
            "error": f"409 conflict, session token {REAL_LOOKING_TOKEN} invalid",
            "rejections": 2,
        }
        cleaned = sanitize(payload)
        self.assertNotIn(REAL_LOOKING_TOKEN, str(cleaned))
        self.assertEqual(scan_for_leaks(cleaned), [])
        # 修复前的反例：确认原始 payload 会被 scan_for_leaks 命中，防止
        # "根本没检测出问题"这种误报式通过。
        self.assertNotEqual(scan_for_leaks(payload), [])


class RecursiveKeyRedactionTests(unittest.TestCase):
    def test_authorization_bearer_cookie_session_keys_redacted(self):
        payload = {
            "Authorization": "Bearer xyz",
            "cookie": "majiang_sid=abc123",
            "nested": {"session_token": "deadbeef", "password": "hunter2"},
            "list_field": [{"majiang_sid": "s1"}, "plain text"],
        }
        cleaned = sanitize(payload)
        self.assertEqual(cleaned["Authorization"], "***REDACTED***")
        self.assertEqual(cleaned["cookie"], "***REDACTED***")
        self.assertEqual(cleaned["nested"]["session_token"], "***REDACTED***")
        self.assertEqual(cleaned["nested"]["password"], "***REDACTED***")
        self.assertEqual(cleaned["list_field"][0]["majiang_sid"], "***REDACTED***")
        self.assertEqual(cleaned["list_field"][1], "plain text")

    def test_normal_fields_pass_through_unchanged(self):
        payload = {"game_id": "g1", "seat": 0, "fan": 4, "detail": ["平胡"]}
        self.assertEqual(sanitize(payload), payload)

    def test_recursion_depth_guard_does_not_crash_on_deep_nesting(self):
        value = {}
        cursor = value
        for _ in range(200):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        cursor["leaf"] = "x"
        # 不应抛出 RecursionError；深度超限后应保守替换为 REDACTED。
        sanitize(value)


class IdentifierFieldPreservesCorrelationTests(unittest.TestCase):
    """P1-3 返修：session_id/user_id/nickname/player_id/account_id 是分析
    关联用的标识符字段，不是凭据。不能因为字段名里含 "session" 这类子串
    就直接整体丢弃（REDACTED），导致同一 session 的多条记录失去关联能力。
    有盐时应走 stable_pseudo_id 带盐假名化路径，保留跨记录关联；无盐时
    退化为 REDACTED（安全默认）。"""

    SALT = "test-fixed-salt-do-not-use-in-prod"

    def test_session_id_is_not_blindly_redacted_when_salt_provided(self):
        payload = {"session_id": "real_session_abc123", "game_id": "g1"}
        cleaned = sanitize(payload, salt=self.SALT)
        self.assertNotEqual(cleaned["session_id"], "***REDACTED***")
        self.assertNotIn("real_session_abc123", cleaned["session_id"])
        self.assertTrue(cleaned["session_id"].startswith("session_"))

    def test_same_session_id_across_records_maps_to_same_pseudo_id(self):
        """核心关联能力验证：同一个原始 session_id 出现在两条不同记录里，
        脱敏后必须映射到同一个假 ID，否则分析管线无法把它们关联起来。"""
        record_a = {"session_id": "s_real_1", "game_id": "g1"}
        record_b = {"session_id": "s_real_1", "game_id": "g2"}
        cleaned_a = sanitize(record_a, salt=self.SALT)
        cleaned_b = sanitize(record_b, salt=self.SALT)
        self.assertEqual(cleaned_a["session_id"], cleaned_b["session_id"])
        # 不同原始值必须映射到不同假 ID（不能把所有 session 都塌缩成一个值）。
        record_c = {"session_id": "s_real_2", "game_id": "g3"}
        cleaned_c = sanitize(record_c, salt=self.SALT)
        self.assertNotEqual(cleaned_a["session_id"], cleaned_c["session_id"])

    def test_user_id_nickname_player_id_account_id_all_preserve_correlation(self):
        payload = {
            "user_id": "u_real", "nickname": "张三", "player_id": "p_real",
            "account_id": "a_real",
        }
        cleaned = sanitize(payload, salt=self.SALT)
        for key in ("user_id", "nickname", "player_id", "account_id"):
            self.assertNotIn("real", str(cleaned[key]))
            self.assertNotIn("张三", str(cleaned[key]))
            self.assertNotEqual(cleaned[key], "***REDACTED***")

    def test_identifier_fields_redacted_when_no_salt_available(self):
        """没有盐时无法计算稳定假 ID，安全默认是整体 REDACTED（宁可丢失
        关联能力，也不能把原始标识符明文写出去）。"""
        payload = {"session_id": "real_session_abc123"}
        cleaned = sanitize(payload, salt=None)
        self.assertEqual(cleaned["session_id"], "***REDACTED***")

    def test_credential_fields_still_fully_redacted_even_with_salt(self):
        """凭据类字段（session_token 等）无论是否有盐，一律整体 REDACTED——
        不会被误判为"标识符"走假名化路径。"""
        payload = {"session_token": "real_secret_token", "cookie": "c=1"}
        cleaned = sanitize(payload, salt=self.SALT)
        self.assertEqual(cleaned["session_token"], "***REDACTED***")
        self.assertEqual(cleaned["cookie"], "***REDACTED***")


class StablePseudoIdTests(unittest.TestCase):
    SALT = "test-fixed-salt-do-not-use-in-prod"

    def test_deterministic_for_same_input(self):
        a = stable_pseudo_id("user", "real_user_123", self.SALT)
        b = stable_pseudo_id("user", "real_user_123", self.SALT)
        self.assertEqual(a, b)

    def test_different_domain_prefix_changes_output_even_for_same_value(self):
        a = stable_pseudo_id("user", "same_value", self.SALT)
        b = stable_pseudo_id("game", "same_value", self.SALT)
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("user_"))
        self.assertTrue(b.startswith("game_"))

    def test_output_does_not_contain_raw_value_or_salt(self):
        pseudo = stable_pseudo_id("user", "sensitive_nickname_张三", self.SALT)
        self.assertNotIn("sensitive_nickname", pseudo)
        self.assertNotIn(self.SALT, pseudo)

    def test_none_value_passes_through_as_none(self):
        self.assertIsNone(stable_pseudo_id("user", None, self.SALT))

    def test_missing_salt_raises(self):
        with self.assertRaises(MissingSaltError):
            stable_pseudo_id("user", "x", None)


class SaltResolutionTests(unittest.TestCase):
    def test_get_salt_prefers_explicit_over_env(self):
        self.assertEqual(get_salt("explicit-salt"), "explicit-salt")

    def test_get_salt_returns_none_when_absent(self):
        import os
        old = os.environ.pop("MJ_SANITIZE_SALT", None)
        try:
            self.assertIsNone(get_salt(None))
        finally:
            if old is not None:
                os.environ["MJ_SANITIZE_SALT"] = old

    def test_require_salt_raises_when_missing_and_not_synthetic(self):
        import os
        old = os.environ.pop("MJ_SANITIZE_SALT", None)
        try:
            with self.assertRaises(MissingSaltError):
                require_salt(None, allow_missing_if_synthetic=False)
        finally:
            if old is not None:
                os.environ["MJ_SANITIZE_SALT"] = old

    def test_require_salt_allows_missing_when_marked_synthetic(self):
        import os
        old = os.environ.pop("MJ_SANITIZE_SALT", None)
        try:
            self.assertIsNone(require_salt(None, allow_missing_if_synthetic=True))
        finally:
            if old is not None:
                os.environ["MJ_SANITIZE_SALT"] = old

    def test_require_salt_uses_explicit_value(self):
        self.assertEqual(require_salt("abc"), "abc")


if __name__ == "__main__":
    unittest.main()
