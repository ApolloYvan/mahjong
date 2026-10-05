"""离线协议对齐热修：extract_active_game_ids() 统一发现自己的 game_id
（需求二），以及 MahjongApi.ready(tournament_id) 路径兼容（需求四）。"""
import unittest
from unittest.mock import MagicMock

from mj.api import MahjongApi
from mj.tournament import GameLedger, extract_active_game_ids


class ExtractActiveGameIdsTests(unittest.TestCase):
    def test_prefers_tournament_info_my_games(self):
        """优先读取 tournament_info.my_games，即使 active_games/me.active_games
        也存在其他内容，也不降级混合。"""
        me = {"active_games": ["legacy_g"]}
        info = {"my_games": ["g1", "g2"], "active_games": ["other_g"]}
        self.assertEqual(extract_active_game_ids(me, info), ["g1", "g2"])

    def test_falls_back_to_tournament_info_active_games(self):
        """my_games 缺失/为空时兼容 tournament_info.active_games。"""
        me = {"active_games": ["legacy_g"]}
        info = {"active_games": [{"game_id": "g3"}]}
        self.assertEqual(extract_active_game_ids(me, info), ["g3"])

    def test_falls_back_to_legacy_me_active_games(self):
        """两者都缺失时兼容旧版 me.active_games。"""
        me = {"active_games": ["g_legacy_1", {"game_id": "g_legacy_2"}]}
        info = {}
        self.assertEqual(extract_active_game_ids(me, info), ["g_legacy_1", "g_legacy_2"])

    def test_supports_string_and_object_elements_mixed(self):
        """支持元素为字符串 game_id 或包含 game_id 的对象，混合格式。"""
        info = {"my_games": ["g_str", {"game_id": "g_obj"}, {"id": "g_id_field"}]}
        self.assertEqual(extract_active_game_ids({}, info), ["g_str", "g_obj", "g_id_field"])

    def test_dedupes_preserving_stable_order(self):
        """去重并保持稳定顺序（首次出现位置）。"""
        info = {"my_games": ["g1", "g2", "g1", {"game_id": "g2"}, "g3"]}
        self.assertEqual(extract_active_game_ids({}, info), ["g1", "g2", "g3"])

    def test_ignores_empty_spectator_and_malformed_items(self):
        """忽略空值、观战项和无 game_id 的异常项，不崩溃。"""
        info = {"my_games": [
            None, "", "  ", "g_valid",
            {"spectator": True, "game_id": "g_watch"},
            {"is_spectator": True, "game_id": "g_watch2"},
            {"watching": True, "game_id": "g_watch3"},
            {"no_game_id_field": 1},
            42, 3.14, [],
        ]}
        self.assertEqual(extract_active_game_ids({}, info), ["g_valid"])

    def test_excludes_items_explicitly_marked_not_mine(self):
        """不得把明确属于其他用户/其他座位的比赛加入（mine=False /
        is_mine=False 的显式标记）。"""
        info = {"my_games": [
            {"game_id": "g_mine", "mine": True},
            {"game_id": "g_not_mine", "mine": False},
            {"game_id": "g_other_seat", "is_mine": False},
        ]}
        self.assertEqual(extract_active_game_ids({}, info), ["g_mine"])

    def test_empty_everywhere_returns_empty_list(self):
        self.assertEqual(extract_active_game_ids({}, {}), [])
        self.assertEqual(extract_active_game_ids(None, None), [])

    def test_game_ledger_dedup_semantics_unchanged(self):
        """GameLedger 去重语义保持不变：filter_new 只放行未提交过的
        game_id，重复调用不会重复放行。"""
        ledger = GameLedger()
        first = ledger.filter_new(extract_active_game_ids({}, {"my_games": ["g1", "g2"]}))
        self.assertEqual(first, ["g1", "g2"])
        second = ledger.filter_new(extract_active_game_ids({}, {"my_games": ["g1", "g2", "g3"]}))
        self.assertEqual(second, ["g3"])
        self.assertEqual(ledger.submitted_count, 3)


class ReadyPathCompatibilityTests(unittest.TestCase):
    def _make_api(self):
        api = MahjongApi("https://example.invalid", "tok")
        api.request = MagicMock(return_value={})
        return api

    def test_ready_with_tournament_id_uses_scoped_path(self):
        """有 tournament_id 时使用 /api/tournaments/{tid}/ready。"""
        api = self._make_api()
        api.ready("t_123")
        api.request.assert_called_once_with("POST", "/api/tournaments/t_123/ready", {})

    def test_ready_without_tournament_id_uses_legacy_path(self):
        """无参数时保留旧 /api/tournaments/me/ready 兼容入口。"""
        api = self._make_api()
        api.ready()
        api.request.assert_called_once_with("POST", "/api/tournaments/me/ready", {})

    def test_ready_does_not_call_both_endpoints_in_one_invocation(self):
        """单次成功调用不重复请求两个 ready 端点。"""
        api = self._make_api()
        api.ready("t_456")
        self.assertEqual(api.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
