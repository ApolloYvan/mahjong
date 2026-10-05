"""验证 data/fixtures/ 下的合成/derived fixture 确实能驱动真实代码路径，
不只是静态 JSON 文档。这是"回归层"fixture 的可执行性保证——防止 fixture
和代码脱节（fixture 声称的断言实际上跑不通）。
"""
import json
import os
import unittest

from mj.bot import _gang_bomb_choice
from mj.rules import evaluate
from mj.state import normalize
from mj.tiles import TILE_INDEX, to_counts

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")


def _load(relative_path):
    with open(os.path.join(DATA_DIR, relative_path), encoding="utf-8") as handle:
        return json.load(handle)


class WallTailGangFixtureTests(unittest.TestCase):
    def test_wall_remaining_20_forbids_bomb_gang(self):
        fixture = _load("fixtures/protocol/wall_tail_gang_boundary.json")
        snapshot = fixture["cases"][0]["snapshot"]
        self.assertEqual(snapshot["wall_remaining"], 20)
        self.assertIsNone(_gang_bomb_choice(snapshot))

    def test_wall_remaining_21_allows_bomb_gang(self):
        fixture = _load("fixtures/protocol/wall_tail_gang_boundary.json")
        snapshot = fixture["cases"][1]["snapshot"]
        self.assertEqual(snapshot["wall_remaining"], 21)
        result = _gang_bomb_choice(snapshot)
        self.assertEqual(result, {"action": "gang", "tile": "1w"})


class ChainCountFixtureTests(unittest.TestCase):
    def test_god_field_precedence_and_no_double_count(self):
        fixture = _load("fixtures/scoring/chain_count_god_field_precedence.json")
        case = fixture["case"]
        snapshot = case["snapshot"]
        state = normalize(snapshot)
        self.assertEqual(state.chain_count, case["expected_canonical_chain_count"])

        hand = snapshot["my_hand"]
        drawn = snapshot["drawn_tile"]
        fan_without_gang_open = evaluate(
            to_counts(hand), TILE_INDEX[drawn], chain_count=state.chain_count,
            gang_open=False,
        )
        fan_with_gang_open = evaluate(
            to_counts(hand), TILE_INDEX[drawn], chain_count=state.chain_count,
            gang_open=True,
        )
        self.assertIsNotNone(fan_without_gang_open)
        self.assertIsNotNone(fan_with_gang_open)
        # 2026-09-24 correctness 修正：gang_open 是独立的 ×2 番值来源，
        # 与 chain_count 是两套互不相关的机制（真实数据核实，见
        # docs/experiments/OFFLINE_REPORT.md「剩余差距」十一.6）。
        self.assertEqual(fan_with_gang_open["fan"], fan_without_gang_open["fan"] * 2)


class SevenPairsFixtureTests(unittest.TestCase):
    def test_standard_seven_pairs(self):
        from mj.rules import seven_pairs
        fixture = _load("fixtures/scoring/seven_pairs_and_joker_pair.json")
        case = fixture["cases"][0]
        counts = to_counts(case["hand13_plus_draw"])
        self.assertEqual(seven_pairs(counts), case["expected_seven_pairs_return"])

    def test_seven_pairs_with_joker_pair(self):
        from mj.rules import seven_pairs
        fixture = _load("fixtures/scoring/seven_pairs_and_joker_pair.json")
        case = fixture["cases"][1]
        counts = to_counts(case["hand13_plus_draw"])
        self.assertEqual(seven_pairs(counts), case["expected_seven_pairs_return"])


class CatchPlayFixtureTests(unittest.TestCase):
    def test_all_cases_match_state_catch_restricted(self):
        fixture = _load("fixtures/protocol/catch_play_restriction.json")
        for case in fixture["cases"]:
            state = normalize(case["snapshot"])
            self.assertEqual(
                state.catch_restricted(), case["expected_catch_restricted"],
                msg=case["description"],
            )


class ScoringMismatchFixtureConsistencyTests(unittest.TestCase):
    """验证两个 scoring fixture 里手工转录的 fan4/fan2 数值与
    audit_data.json 源数据完全一致（防止转录错误——已在本轮发现并修复过
    一次 57 vs 52 房间数的转录错误，这里加测试防止同类错误再次发生）。"""

    def test_fan_mismatch_case_matches_audit_source(self):
        audit_path = (
            "/Users/yuanye/Documents/ChatGPT/麻将大赛/review/audit_data.json"
        )
        if not os.path.exists(audit_path):
            self.skipTest("audit_data.json not available in this environment")
        with open(audit_path, encoding="utf-8") as handle:
            audit = json.load(handle)
        by_game = {m["game_id"]: m for m in audit["portal_mismatches"]}

        fixture = _load("fixtures/scoring/fan_mismatch_a_5a06a8f48d67_round5.json")
        case = fixture["case"]
        source = by_game[case["game_id"]]
        self.assertEqual(case["server_truth"]["fan"], source["server_fan"])
        self.assertEqual(case["server_truth"]["detail"], source["server_detail"])
        self.assertEqual(case["local_estimate"]["fan"], source["local_fan"])

    def test_server_only_case_matches_audit_source(self):
        audit_path = (
            "/Users/yuanye/Documents/ChatGPT/麻将大赛/review/audit_data.json"
        )
        if not os.path.exists(audit_path):
            self.skipTest("audit_data.json not available in this environment")
        with open(audit_path, encoding="utf-8") as handle:
            audit = json.load(handle)
        by_game = {m["game_id"]: m for m in audit["portal_mismatches"]}

        fixture = _load(
            "fixtures/scoring/server_only_missing_local_a_2e31e10fe10a_round7.json"
        )
        case = fixture["case"]
        source = by_game[case["game_id"]]
        self.assertEqual(case["server_truth"]["fan"], source["server_fan"])
        self.assertIsNone(case["local_estimate"])
        self.assertIsNone(source["local_fan"])

    def test_aggregate_room_scores_match_audit_source_exactly(self):
        audit_path = (
            "/Users/yuanye/Documents/ChatGPT/麻将大赛/review/audit_data.json"
        )
        if not os.path.exists(audit_path):
            self.skipTest("audit_data.json not available in this environment")
        with open(audit_path, encoding="utf-8") as handle:
            audit = json.load(handle)
        expected_sum = sum(r["score"] for r in audit["auto_rooms"])
        expected_count = len(audit["auto_rooms"])

        aggregate = _load("aggregates/historical_audit_summary.json")
        self.assertEqual(aggregate["room_level_net_scores"]["total_rooms"], expected_count)
        self.assertEqual(
            aggregate["room_level_net_scores"]["sum_of_net_scores"], expected_sum,
        )


class MalformedJsonlFixtureImportTests(unittest.TestCase):
    """验证 failures/malformed_and_error_lines.jsonl 能被真实导入器正确解析，
    不只是一份静态文档。"""

    def test_real_importer_parses_fixture_correctly(self):
        from mj import data_import as di
        path = os.path.join(DATA_DIR, "fixtures", "failures",
                             "malformed_and_error_lines.jsonl")
        result = di.parse_decision_log_lines(di.open_text_lines(path))
        self.assertEqual(result.total_lines, 7)
        self.assertEqual(result.failed_lines, 1)
        self.assertEqual(len(result.decisions), 2)
        categories = {e.category for e in result.errors}
        self.assertIn(di.CATEGORY_JSON_DECODE, categories)
        self.assertIn(di.CATEGORY_RATE_LIMITED, categories)
        self.assertIn(di.CATEGORY_TIMEOUT, categories)
        self.assertIn(di.CATEGORY_CONFLICT_409, categories)


if __name__ == "__main__":
    unittest.main()
