"""锁死：404 必须按 body 的 code 判型，不能只看 status。

接入指南 v35 起，404 下 TOURNAMENT_GONE（房暂时不可达，应重试）与
TOURNAMENT_NOT_FOUND / GAME_NOT_FOUND（不存在，应放弃）语义相反。

实测代价：2026-09-23 房间 a_e2eb6e654532 的 10 张桌里有 7 张各收到正好 10 次
404 后被整桌放弃，那 78 局只胡 1 局、69~73% 的弃牌由服务端代打——不是打输，
是弃权。见 docs/audit/STALL_2026-09-23.md。
"""
import unittest

from mj.api import PERMANENT_CODES, TRANSIENT_CODES, ApiError, is_transient


class ErrorCodeTests(unittest.TestCase):
    def test_code_parsed_from_json_body(self):
        self.assertEqual(ApiError(404, '{"code":"TOURNAMENT_GONE","message":"x"}').code,
                         "TOURNAMENT_GONE")

    def test_code_parsed_when_body_is_not_clean_json(self):
        """body 会先过 sanitize_text，可能不再是合法 JSON——仍须取出 code。"""
        self.assertEqual(ApiError(404, 'gateway noise {"code":"GAME_NOT_FOUND"} tail').code,
                         "GAME_NOT_FOUND")

    def test_missing_code_is_empty_string(self):
        self.assertEqual(ApiError(404, "").code, "")
        self.assertEqual(ApiError(404, "not json").code, "")


class TransientClassificationTests(unittest.TestCase):
    def test_gone_is_retryable(self):
        self.assertTrue(is_transient(ApiError(404, '{"code":"TOURNAMENT_GONE"}')))

    def test_not_found_is_terminal(self):
        for code in ("TOURNAMENT_NOT_FOUND", "GAME_NOT_FOUND"):
            self.assertFalse(is_transient(ApiError(404, '{"code":"%s"}' % code)), code)

    def test_unknown_code_defaults_to_retryable(self):
        """未知 code 保守按可重试处理：放弃一局的代价（整桌被服务端托管、
        69~73% 的牌由服务端代打）远高于多轮询几次。"""
        self.assertTrue(is_transient(ApiError(404, '{"code":"SOMETHING_NEW"}')))
        self.assertTrue(is_transient(ApiError(404, "")))

    def test_code_sets_do_not_overlap(self):
        self.assertEqual(TRANSIENT_CODES & PERMANENT_CODES, frozenset())


class CallSitesUseCodeTests(unittest.TestCase):
    def test_bot_404_sites_consult_is_transient(self):
        """两处 404 分支都必须经过 is_transient()，不得退回裸 status 判断。"""
        import inspect

        import mj.bot as bot
        source = inspect.getsource(bot)
        self.assertEqual(source.count("error.status == 404 and not is_transient(error)"), 2)

    def test_known_guide_version_is_current(self):
        import mj.bot as bot
        self.assertGreaterEqual(bot.KNOWN_GUIDE_VERSION, 35)
