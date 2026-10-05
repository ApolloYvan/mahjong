"""离线 preflight（tools/live_preflight.py）契约测试：默认离线、不输出
令牌明文、状态语义正确（OFFLINE_READY/NOT_READY/TEST_ROOM_READY）、
--check-test-tokens 在线只读校验（用 mock，不发真实网络请求）。

纠错要点（本轮"评审误读规则"纠错第二项）：
- 离线模式只能确认 MJ_TOKEN presence，不能确认 scope，因此顶层
  status 绝不能叫 READY——只能是 OFFLINE_READY / NOT_READY。
- 只有 --check-test-tokens 在线只读校验（四枚 MJ_TOKEN_1..4 都验证
  tournament_id 一致 + config 匹配）全部通过后，才能升级为
  TEST_ROOM_READY。
"""
import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "live_preflight", os.path.join(REPO_ROOT, "tools", "live_preflight.py"))
live_preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(live_preflight)


class OfflinePreflightStatusSemanticsTests(unittest.TestCase):
    def test_offline_status_is_never_named_ready(self):
        """离线模式全部检查通过时状态必须是 OFFLINE_READY，绝不能是裸的
        READY（presence 类检查不构成"可进入测试房"的证明）。"""
        report = live_preflight.run_preflight()
        self.assertIn(report["status"], ("OFFLINE_READY", "NOT_READY"))
        self.assertNotEqual(report["status"], "READY")

    def test_report_contains_no_raw_token_when_tokens_present(self):
        env_backup = dict(os.environ)
        os.environ["MJ_TOKEN"] = "f" * 64
        os.environ["MJ_TOKEN_1"] = "a" * 64
        try:
            report = live_preflight.run_preflight()
        finally:
            os.environ.clear()
            os.environ.update(env_backup)
        serialized = str(report)
        self.assertNotIn("f" * 64, serialized)
        self.assertNotIn("a" * 64, serialized)
        self.assertEqual(report["checks"]["token"]["status"], "present")
        self.assertIsNotNone(report["checks"]["token"]["fingerprint"])

    def test_token_presence_alone_does_not_block_or_claim_readiness(self):
        """presence 检查是信息性的：MJ_TOKEN 缺失不阻塞 OFFLINE_READY，
        存在也不会让状态变成任何形式的"可进入测试房"声明。"""
        env_backup = os.environ.pop("MJ_TOKEN", None)
        try:
            report_absent = live_preflight.run_preflight()
        finally:
            if env_backup is not None:
                os.environ["MJ_TOKEN"] = env_backup
        self.assertEqual(report_absent["checks"]["token"]["status"], "absent")

        os.environ["MJ_TOKEN"] = "f" * 64
        try:
            report_present = live_preflight.run_preflight()
        finally:
            del os.environ["MJ_TOKEN"]
        # presence 与 absence 两种情况下，只要其余离线检查结果相同，
        # 顶层 status 应该一致——token presence 本身不改变 OFFLINE_READY
        # 判定（不阻塞也不单独赋予额外信任）。
        self.assertEqual(report_absent["status"], report_present["status"])
        self.assertNotIn("READY", report_present["checks"]["token"].get("status", ""))

    def test_output_documents_three_token_categories_and_entry_points(self):
        """输出必须明确区分三类令牌与三类入口：global_token（自由匹配）、
        test_scoped_tokens（自建测试房）、formal_scoped_token（正式赛事）。"""
        report = live_preflight.run_preflight()
        categories = report["token_categories"]
        self.assertIn("global_token", categories)
        self.assertIn("test_scoped_tokens", categories)
        self.assertIn("formal_scoped_token", categories)
        self.assertIn("/api/match", categories["global_token"]["purpose"])
        self.assertIn("mj.bot", categories["global_token"]["entry_point"])
        self.assertIn("MJ_TOKEN_1", categories["test_scoped_tokens"]["env_var"])
        self.assertIn("run_test_room", categories["test_scoped_tokens"]["entry_point"])
        self.assertIn("--wait", categories["formal_scoped_token"]["entry_point"])
        self.assertIn("token_scope_ambiguity_note", report)

    def test_not_ready_when_a_blocking_check_fails(self):
        original = live_preflight.check_security_scan
        live_preflight.check_security_scan = lambda: {"ok": False, "hit_count": 1, "hits": ["x.py"]}
        try:
            report = live_preflight.run_preflight()
        finally:
            live_preflight.check_security_scan = original
        self.assertEqual(report["status"], "NOT_READY")

    def test_report_includes_required_machine_readable_fields(self):
        report = live_preflight.run_preflight()
        checks = report["checks"]
        self.assertIn("python_and_imports", checks)
        self.assertIn("security_scan", checks)
        self.assertIn("log_dir_writable", checks)
        self.assertIn("versions", checks)
        self.assertIn("build_hash", checks["versions"])
        self.assertIn("log_schema_version", checks["versions"])
        self.assertIn("datastore_schema_version", checks["versions"])
        self.assertIn("targeted_production_tests", checks)
        self.assertIn("test_tokens", checks)


class CheckTestTokensOnlineTests(unittest.TestCase):
    """--check-test-tokens 只读在线校验：全程用 mock 的 MahjongApi，
    不发起任何真实网络请求（符合本轮"不得发起真实网络请求"的硬约束）。"""

    def _environ(self):
        return {name: f"{name.lower()}_secret_{'x' * 50}"
                for name in live_preflight.TEST_TOKEN_ENV_NAMES}

    def test_missing_env_vars_fails_without_network_call(self):
        with patch("mj.api.MahjongApi") as mock_ctor:
            result = live_preflight.check_test_room_tokens_online(environ={})
        mock_ctor.assert_not_called()
        self.assertFalse(result["ok"])
        for name in live_preflight.TEST_TOKEN_ENV_NAMES:
            self.assertIn(name, result["reason"])

    def test_all_four_tokens_same_tournament_and_matching_config_succeeds(self):
        def fake_ctor(server, token):
            api = MagicMock()
            api.me.return_value = {"tournament_id": "tourn_1"}
            api.rules.return_value = {"config": {"M": 1, "Rounds": 1}}
            return api

        with patch("mj.api.MahjongApi", side_effect=fake_ctor):
            result = live_preflight.check_test_room_tokens_online(
                expected_m=1, expected_rounds=1, environ=self._environ())
        self.assertTrue(result["ok"])
        self.assertTrue(result["same_tournament"])
        self.assertTrue(result["config_ok"])
        self.assertEqual(len(result["per_token"]), 4)
        for entry in result["per_token"]:
            self.assertIn("fingerprint", entry)
            self.assertNotIn("secret", json_dumps_safe(entry))

    def test_mismatched_tournament_id_fails(self):
        call_count = {"n": 0}

        def fake_ctor(server, token):
            call_count["n"] += 1
            api = MagicMock()
            api.me.return_value = {"tournament_id": "tourn_A" if call_count["n"] <= 3 else "tourn_B"}
            api.rules.return_value = {"config": {"M": 1, "Rounds": 1}}
            return api

        with patch("mj.api.MahjongApi", side_effect=fake_ctor):
            result = live_preflight.check_test_room_tokens_online(
                expected_m=1, expected_rounds=1, environ=self._environ())
        self.assertFalse(result["ok"])
        self.assertFalse(result["same_tournament"])

    def test_config_mismatch_with_expected_m_rounds_fails(self):
        def fake_ctor(server, token):
            api = MagicMock()
            api.me.return_value = {"tournament_id": "tourn_1"}
            api.rules.return_value = {"config": {"M": 10, "Rounds": 8}}
            return api

        with patch("mj.api.MahjongApi", side_effect=fake_ctor):
            result = live_preflight.check_test_room_tokens_online(
                expected_m=1, expected_rounds=1, environ=self._environ())
        self.assertFalse(result["ok"])
        self.assertTrue(result["same_tournament"])
        self.assertFalse(result["config_ok"])

    def test_only_read_only_methods_are_called(self):
        """只读约束：只调用 me()/rules()，绝不调用 register/ready/match/action。"""
        apis = []

        def fake_ctor(server, token):
            api = MagicMock()
            api.me.return_value = {"tournament_id": "tourn_1"}
            api.rules.return_value = {"config": {"M": 1, "Rounds": 1}}
            apis.append(api)
            return api

        with patch("mj.api.MahjongApi", side_effect=fake_ctor):
            live_preflight.check_test_room_tokens_online(environ=self._environ())
        for api in apis:
            api.me.assert_called_once()
            api.rules.assert_called_once()
            api.register.assert_not_called()
            api.ready.assert_not_called()
            api.match.assert_not_called()
            api.action.assert_not_called()


def json_dumps_safe(obj):
    import json
    return json.dumps(obj, default=str)


class MainCliStatusTests(unittest.TestCase):
    def test_default_mode_exits_zero_when_offline_ready(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "live_preflight.py")],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        report = __import__("json").loads(result.stdout)
        if report["status"] == "OFFLINE_READY":
            self.assertEqual(result.returncode, 0)
        else:
            self.assertNotEqual(result.returncode, 0)

    def test_check_test_tokens_without_env_vars_exits_nonzero_no_network(self):
        import subprocess
        env = {k: v for k, v in os.environ.items()
               if k not in live_preflight.TEST_TOKEN_ENV_NAMES}
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "live_preflight.py"),
             "--check-test-tokens"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        report = __import__("json").loads(result.stdout)
        self.assertNotEqual(report["status"], "TEST_ROOM_READY")
        self.assertIn("missing environment variables", report["test_tokens_online_check"]["reason"])


if __name__ == "__main__":
    unittest.main()
