"""服务端结算对账测试：能自动发现"服务端 fan4、本地 fan2"一类差异
（复现独立技术评审报告已核验的真实案例结构，不使用真实房间数据/令牌）。
"""
import unittest

from mj.reconcile import (
    local_estimates_from_hu_detail_lines,
    match_key,
    reconcile,
    reconcile_fields,
    server_truths_from_portal_game,
    summarize,
    summarize_fields,
)


class LocalEstimateExtractionTests(unittest.TestCase):
    def test_extracts_hu_detail_records_only(self):
        lines = [
            {"kind": "decision", "payload": {"game_id": "g1"}},
            {"kind": "hu_detail", "payload": {
                "game_id": "g1", "round_no": 5, "seat": 2, "fan": 2,
                "detail": ["平胡", "爆头"],
            }},
        ]
        result = local_estimates_from_hu_detail_lines(lines)
        self.assertEqual(len(result), 1)
        key = match_key("g1", 5, 2)
        self.assertEqual(result[key]["fan"], 2)


class ServerTruthExtractionTests(unittest.TestCase):
    def test_extracts_round_ended_events(self):
        data = {
            "game_id": "g1",
            "blocks": [{
                "events": [
                    {"type": "tile_discarded", "seat": 0, "tile": "1w"},
                    {"type": "round_ended", "seat": 2, "data": {
                        "fan": 4, "detail": ["平胡", "财飘", "爆头"],
                        "scores": [10, -8, 34, -8], "next_dealer": 2,
                    }},
                ],
            }],
        }
        result = server_truths_from_portal_game(data)
        key = match_key("g1", 1, 2)
        self.assertIn(key, result)
        self.assertEqual(result[key]["fan"], 4)
        self.assertEqual(result[key]["next_dealer"], 2)

    def test_round_no_advances_across_rounds(self):
        data = {
            "game_id": "g1",
            "blocks": [{
                "events": [
                    {"type": "round_ended", "seat": 0, "data": {"fan": 1}},
                    {"type": "round_ended", "seat": 1, "data": {"fan": 2}},
                ],
            }],
        }
        result = server_truths_from_portal_game(data)
        self.assertIn(match_key("g1", 1, 0), result)
        self.assertIn(match_key("g1", 2, 1), result)


class ReconcileTests(unittest.TestCase):
    def test_discovers_fan_mismatch_like_audit_report_case(self):
        """复现独立技术评审报告已核验的真实差异结构：服务端 fan4「平胡、财飘、
        爆头」，本地 hu_detail 记录为 fan2——这正是链字段错读导致的历史 bug。
        阶段1已修复该 bug，本测试验证：即使未来再次出现类似偏差，对账工具
        也能自动发现，而不需要人工逐条比对牌谱。"""
        local = local_estimates_from_hu_detail_lines([
            {"kind": "hu_detail", "payload": {
                "game_id": "a_5a06a8f48d67", "round_no": 1, "seat": 0,
                "fan": 2, "detail": ["平胡", "爆头"],
            }},
        ])
        server = server_truths_from_portal_game({
            "game_id": "a_5a06a8f48d67",
            "blocks": [{"events": [
                {"type": "round_ended", "seat": 0, "data": {
                    "fan": 4, "detail": ["平胡", "财飘", "爆头"],
                }},
            ]}],
        })
        report = reconcile(local, server)
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["agreements"], 0)
        self.assertEqual(len(report["discrepancies"]), 1)
        mismatch = report["discrepancies"][0]
        self.assertEqual(mismatch["local_fan"], 2)
        self.assertEqual(mismatch["server_fan"], 4)

    def test_agreement_when_fan_matches(self):
        local = {match_key("g1", 1, 0): {"fan": 2, "detail": ["平胡"]}}
        server = {match_key("g1", 1, 0): {"fan": 2, "detail": ["平胡"]}}
        report = reconcile(local, server)
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["agreements"], 1)
        self.assertEqual(report["discrepancies"], [])

    def test_local_only_and_server_only_are_tracked_separately(self):
        local = {match_key("g1", 1, 0): {"fan": 1}}
        server = {match_key("g1", 2, 1): {"fan": 1}}
        report = reconcile(local, server)
        self.assertEqual(report["matched"], 0)
        self.assertEqual(report["local_only"], [match_key("g1", 1, 0)])
        self.assertEqual(report["server_only"], [match_key("g1", 2, 1)])

    def test_server_only_represents_missing_local_hu_detail(self):
        """服务端有结果但本地没有 hu_detail 记录：可能是自动胡/托管兜底
        （评审报告已确认这类情况存在，不能被对账工具误判为"客户端多算"）。"""
        server = {match_key("g1", 3, 2): {"fan": 1, "detail": ["平胡"]}}
        report = reconcile({}, server)
        self.assertEqual(report["server_only"], [match_key("g1", 3, 2)])
        self.assertEqual(report["discrepancies"], [])

    def test_summarize_includes_mismatch_details(self):
        local = {match_key("g1", 1, 0): {"fan": 2, "detail": ["平胡", "爆头"]}}
        server = {match_key("g1", 1, 0): {"fan": 4, "detail": ["平胡", "财飘", "爆头"]}}
        report = reconcile(local, server)
        text = summarize(report)
        self.assertIn("MISMATCH", text)
        self.assertIn("local_fan=2", text)
        self.assertIn("server_fan=4", text)


class ReconcileFieldsTests(unittest.TestCase):
    """A3 返修：逐字段对账，覆盖 SONNET5_DATA_REVIEW.md P1 的独立复现反例
    （fan 相同但 detail 不同，旧版会误报 agreements=1/退出码0）。"""

    def test_fan_equal_but_detail_differs_is_mismatch_not_agreement(self):
        # 独立复现的确切反例：本地/服务端 fan 都是 2，但 detail 不同。
        local = {("g1", 1, 0): {"seat": 0, "fan": 2, "detail": ["平胡"]}}
        server = {("g1", 1, 0): {"seat": 0, "fan": 2, "detail": ["平胡", "爆头"]}}
        report = reconcile_fields(local, server)
        self.assertEqual(report["matched"], 1)
        self.assertFalse(report["must_compare_ok"])
        self.assertEqual(report["field_coverage"]["fan"]["equal"], 1)
        self.assertEqual(report["field_coverage"]["detail"]["mismatch"], 1)

    def test_detail_order_insensitive_when_same_elements(self):
        local = {("g1", 1, 0): {"seat": 0, "fan": 4, "detail": ["平胡", "财飘", "爆头"]}}
        server = {("g1", 1, 0): {"seat": 0, "fan": 4, "detail": ["爆头", "平胡", "财飘"]}}
        report = reconcile_fields(local, server)
        self.assertEqual(report["field_coverage"]["detail"]["equal"], 1)
        self.assertTrue(report["must_compare_ok"])

    def test_local_missing_scores_and_next_dealer_is_unavailable_not_equal(self):
        # 本地估算天然没有 scores/next_dealer：必须标 unavailable，不能假装 equal。
        local = {("g1", 1, 0): {"seat": 0, "fan": 2, "detail": ["平胡"],
                                 "scores": None, "next_dealer": None}}
        server = {("g1", 1, 0): {"seat": 0, "fan": 2, "detail": ["平胡"],
                                  "scores": [10, -3, -3, -4], "next_dealer": 0}}
        report = reconcile_fields(local, server)
        self.assertEqual(report["field_coverage"]["scores"]["unavailable"], 1)
        self.assertEqual(report["field_coverage"]["scores"]["equal"], 0)
        self.assertEqual(report["field_coverage"]["next_dealer"]["unavailable"], 1)
        # scores/next_dealer 不是必比字段，不影响 must_compare_ok。
        self.assertTrue(report["must_compare_ok"])

    def test_winner_mismatch_is_classified_and_fails_must_compare(self):
        local = {("g1", 1, 0): {"seat": 0, "fan": 1, "detail": ["平胡"]}}
        server = {("g1", 1, 0): {"seat": 1, "fan": 1, "detail": ["平胡"]}}
        report = reconcile_fields(local, server)
        self.assertEqual(report["field_coverage"]["winner"]["mismatch"], 1)
        self.assertFalse(report["must_compare_ok"])

    def test_next_dealer_mismatch_is_classified(self):
        local = {("g1", 1, 0): {"seat": 0, "fan": 1, "detail": ["平胡"], "next_dealer": 1}}
        server = {("g1", 1, 0): {"seat": 0, "fan": 1, "detail": ["平胡"], "next_dealer": 2}}
        report = reconcile_fields(local, server)
        self.assertEqual(report["field_coverage"]["next_dealer"]["mismatch"], 1)
        # next_dealer 不是必比字段。
        self.assertTrue(report["must_compare_ok"])

    def test_summarize_fields_reports_coverage_and_samples(self):
        local = {("g1", 1, 0): {"seat": 0, "fan": 2, "detail": ["平胡"]}}
        server = {("g1", 1, 0): {"seat": 0, "fan": 4, "detail": ["平胡", "财飘", "爆头"]}}
        report = reconcile_fields(local, server)
        text = summarize_fields(report)
        self.assertIn("field=fan", text)
        self.assertIn("MISMATCH", text)

    def test_no_intersection_matched_zero(self):
        local = {("g1", 1, 0): {"seat": 0, "fan": 1}}
        server = {("g2", 1, 0): {"seat": 0, "fan": 1}}
        report = reconcile_fields(local, server)
        self.assertEqual(report["matched"], 0)
        self.assertEqual(len(report["local_only"]), 1)
        self.assertEqual(len(report["server_only"]), 1)


if __name__ == "__main__":
    unittest.main()
