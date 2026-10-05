"""协议对齐纠错需求三：安全启动测试房——tokens 位置参数改为可选，无
位置参数时从 MJ_TOKEN_1..MJ_TOKEN_4 读取，缺少任一环境变量时快速失败
只显示变量名，四枚令牌 tournament_id 必须一致，ready 必须带 tid。"""
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "run_test_room", os.path.join(REPO_ROOT, "tools", "run_test_room.py"))
run_test_room = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_test_room)


class ResolveTokensTests(unittest.TestCase):
    def test_cli_tokens_take_priority_over_env_vars(self):
        cli_tokens = ["t1", "t2", "t3", "t4"]
        environ = {"MJ_TOKEN_1": "env1", "MJ_TOKEN_2": "env2",
                   "MJ_TOKEN_3": "env3", "MJ_TOKEN_4": "env4"}
        self.assertEqual(run_test_room.resolve_tokens(cli_tokens, environ), cli_tokens)

    def test_env_vars_used_when_no_cli_tokens(self):
        environ = {"MJ_TOKEN_1": "env1", "MJ_TOKEN_2": "env2",
                   "MJ_TOKEN_3": "env3", "MJ_TOKEN_4": "env4"}
        self.assertEqual(run_test_room.resolve_tokens(None, environ),
                          ["env1", "env2", "env3", "env4"])
        self.assertEqual(run_test_room.resolve_tokens([], environ),
                          ["env1", "env2", "env3", "env4"])

    def test_missing_any_env_var_fails_fast_with_only_variable_names(self):
        environ = {"MJ_TOKEN_1": "env1", "MJ_TOKEN_3": "env3"}
        with self.assertRaises(ValueError) as ctx:
            run_test_room.resolve_tokens(None, environ)
        message = str(ctx.exception)
        self.assertIn("MJ_TOKEN_2", message)
        self.assertIn("MJ_TOKEN_4", message)
        self.assertNotIn("env1", message)
        self.assertNotIn("env3", message)

    def test_all_env_vars_missing_lists_all_four(self):
        with self.assertRaises(ValueError) as ctx:
            run_test_room.resolve_tokens(None, {})
        message = str(ctx.exception)
        for name in run_test_room.TOKEN_ENV_NAMES:
            self.assertIn(name, message)


class RunTestRoomCliTests(unittest.TestCase):
    def test_cli_exits_nonzero_with_env_var_names_when_missing(self):
        import subprocess
        env = {k: v for k, v in os.environ.items()
               if k not in run_test_room.TOKEN_ENV_NAMES}
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "run_test_room.py")],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        for name in run_test_room.TOKEN_ENV_NAMES:
            self.assertIn(name, result.stderr)

    def test_help_text_recommends_env_var_pattern(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "run_test_room.py"), "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("MJ_TOKEN_1", result.stdout)
        self.assertIn("MJ_TOKEN_4", result.stdout)

    def test_wrong_positional_count_fails_fast(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "run_test_room.py"),
             "t1", "t2", "t3"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
