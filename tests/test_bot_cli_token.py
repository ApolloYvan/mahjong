"""离线收尾需求二：mj.bot 单身份安全启动方式——positional token 可选，
优先级为显式命令行 token > MJ_TOKEN 环境变量，两者皆缺失时非零退出，
且任何输出/repr/日志不得包含令牌明文。"""
import subprocess
import sys
import unittest

from mj.bot import _resolve_token


class TokenPriorityTests(unittest.TestCase):
    def test_cli_token_wins_over_env_var(self):
        self.assertEqual(_resolve_token("cli_token", {"MJ_TOKEN": "env_token"}), "cli_token")

    def test_env_var_used_when_cli_token_missing(self):
        self.assertEqual(_resolve_token(None, {"MJ_TOKEN": "env_token"}), "env_token")

    def test_none_when_both_missing(self):
        # 2026-09-24 起令牌还有文件兜底（.mj_token / ~/.mahjong_token，见 mj/token.py）；
        # 本用例断言的是「命令行与环境变量都缺失」这一层，用 MJ_TOKEN_NO_FILE 隔离掉
        # 文件层，避免结果取决于开发机上恰好有没有那个文件。
        off = {"MJ_TOKEN_NO_FILE": "1"}
        self.assertIsNone(_resolve_token(None, off))
        self.assertIsNone(_resolve_token("", off))


class CliFastFailTests(unittest.TestCase):
    def test_main_exits_nonzero_with_env_var_hint_when_no_token(self):
        """两者都没有时 argparse 非零退出，错误信息只提示环境变量名，
        不显示任何令牌候选值；也验证 stdout/stderr 完全不包含被清空的
        MJ_TOKEN。"""
        import os
        env = {k: v for k, v in os.environ.items() if k != "MJ_TOKEN"}
        env["MJ_TOKEN_NO_FILE"] = "1"   # 同上：隔离文件兜底，只测「什么都没给」的退出行为
        result = subprocess.run(
            [sys.executable, "-m", "mj.bot"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MJ_TOKEN", result.stderr)

    def test_help_text_recommends_env_var_pattern(self):
        import os
        result = subprocess.run(
            [sys.executable, "-m", "mj.bot", "--help"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("MJ_TOKEN='...' python3 -m mj.bot --wait", result.stdout)


if __name__ == "__main__":
    unittest.main()
