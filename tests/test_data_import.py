"""mj.data_import 契约测试：单行损坏不中止全文件、字段口径正确、
kind 分流正确。"""
import unittest

from mj import data_import as di


class DecisionKindTests(unittest.TestCase):
    def test_decision_kind_extracts_decision_row(self):
        lines = iter([
            '{"time":"2026-09-21T00:00:00Z","kind":"decision","payload":'
            '{"decision_id":"d1","game_id":"g1","round_no":1,"seat":0,'
            '"state_hash":"h1","policy_version":"v1","schema_version":"1",'
            '"config_hash":"c1","decision":{"action":"discard","tile":"1w"}}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(result.total_lines, 1)
        self.assertEqual(result.success_lines, 1)
        self.assertEqual(result.failed_lines, 0)
        self.assertEqual(len(result.decisions), 1)
        d = result.decisions[0]
        self.assertEqual(d.decision_id, "d1")
        self.assertFalse(d.decision_id_synthesized)
        self.assertEqual(d.action, "discard")
        self.assertEqual(d.tile, "1w")

    def test_decision_without_decision_id_gets_synthesized(self):
        # 旧日志（阶段2之前）没有 decision_id 字段——audit_data.json 确认
        # 210,175 条 decision 记录里 has_policy_version=0，同样缺新字段。
        lines = iter([
            '{"time":"2026-09-15T00:00:00Z","kind":"decision","payload":'
            '{"game_id":"g_old","round_no":3,"seat":1,'
            '"decision":{"action":"pass"}}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(len(result.decisions), 1)
        d = result.decisions[0]
        self.assertTrue(d.decision_id_synthesized)
        self.assertTrue(d.decision_id.startswith("derived-"))

    def test_hu_detail_kind_extracts_local_estimate_round_result(self):
        lines = iter([
            '{"time":"2026-09-20T05:39:31Z","kind":"hu_detail","payload":'
            '{"game_id":"a_5a06a8f48d67_r1_b4_t0","round_no":5,"seat":0,'
            '"fan":2,"detail":["平胡","爆头"]}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(len(result.round_results), 1)
        rr = result.round_results[0]
        self.assertEqual(rr.source, "local_estimate")
        self.assertEqual(rr.fan, 2)
        self.assertEqual(rr.winner_seat, 0)


class MalformedLineTests(unittest.TestCase):
    def test_single_bad_json_line_does_not_abort_rest_of_file(self):
        lines = iter([
            '{"time":"t1","kind":"decision","payload":{"game_id":"g1","seat":0,'
            '"decision":{"action":"discard"}}}',
            'THIS IS NOT JSON {{{',
            '{"time":"t2","kind":"decision","payload":{"game_id":"g1","seat":1,'
            '"decision":{"action":"pass"}}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(result.total_lines, 3)
        self.assertEqual(result.success_lines, 2)
        self.assertEqual(result.failed_lines, 1)
        self.assertEqual(len(result.decisions), 2)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].category, di.CATEGORY_JSON_DECODE)
        self.assertEqual(result.errors[0].line_no, 2)

    def test_blank_lines_counted_as_success_not_error(self):
        lines = iter(["", "   ", "\n"])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(result.total_lines, 3)
        self.assertEqual(result.success_lines, 3)
        self.assertEqual(result.failed_lines, 0)

    def test_missing_kind_field_does_not_crash(self):
        lines = iter(['{"time":"t1","payload":{}}'])
        result = di.parse_decision_log_lines(lines)
        # kind=None 落入"未识别 kind"分支，记一条错误但不中止/不抛异常。
        self.assertEqual(result.success_lines, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].category, di.CATEGORY_UNKNOWN_KIND)

    def test_error_summary_is_redacted_not_raw_payload(self):
        # json_decode_error 的摘要来自异常信息（简短），不是原始行内容——
        # 这里验证即使原始行很长，摘要也不会把整行塞进 errors 表。
        long_junk = "x" * 500
        lines = iter([long_junk])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(len(result.errors), 1)
        self.assertLessEqual(len(result.errors[0].summary), 200)
        self.assertNotIn("x" * 500, result.errors[0].summary)

    def test_utf8_replacement_char_flagged(self):
        # 模拟 UTF-8 解码失败后用 errors='replace' 产生的 U+FFFD 占位符，
        # 应归类为 utf8_decode_error 而不是当作正常行跳过。
        lines = iter(["broken \ufffd payload"])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].category, di.CATEGORY_UTF8_DECODE)
        self.assertLessEqual(len(result.errors[0].summary), 200)


class ErrorKindMappingTests(unittest.TestCase):
    def test_action_rejected_kind_becomes_error_row(self):
        lines = iter([
            '{"time":"t1","kind":"action_rejected","payload":'
            '{"game_id":"g1","phase":"discard","error":"illegal tile","rejections":1}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].category, di.CATEGORY_ACTION_REJECTED)
        self.assertEqual(result.errors[0].game_id, "g1")

    def test_fallback_sent_kind_becomes_error_row(self):
        lines = iter([
            '{"time":"t1","kind":"fallback_sent","payload":'
            '{"game_id":"g1","tile":"1w","accepted":true}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(result.errors[0].category, di.CATEGORY_FALLBACK_SENT)

    def test_error_kind_with_429_classified_as_rate_limited(self):
        lines = iter([
            '{"time":"t1","kind":"error","payload":{"game_id":"g1","error":"429 rate limited"}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(result.errors[0].category, di.CATEGORY_RATE_LIMITED)

    def test_error_kind_with_409_classified_as_conflict(self):
        lines = iter([
            '{"time":"t1","kind":"error","payload":{"game_id":"g1","error":"409 already done"}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(result.errors[0].category, di.CATEGORY_CONFLICT_409)

    def test_error_kind_with_timeout_text_classified_as_timeout(self):
        lines = iter([
            '{"time":"t1","kind":"error","payload":{"game_id":"g1","error":"timeout waiting"}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(result.errors[0].category, di.CATEGORY_TIMEOUT)

    def test_generic_error_text_is_not_defaulted_to_timeout(self):
        # A4 返修：无法判断具体类别的通用错误不得默认归为 timeout。
        lines = iter([
            '{"time":"t1","kind":"error","payload":{"game_id":"g1",'
            '"error":"connection reset by peer"}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(result.errors[0].category, di.CATEGORY_UNCLASSIFIED_ERROR)
        self.assertNotEqual(result.errors[0].category, di.CATEGORY_TIMEOUT)

    def test_high_frequency_kinds_are_not_imported_as_errors(self):
        lines = iter([
            '{"time":"t1","kind":"state","payload":{"game_id":"g1"}}',
            '{"time":"t2","kind":"notify","payload":{"game_id":"g1"}}',
        ])
        result = di.parse_decision_log_lines(lines)
        self.assertEqual(result.success_lines, 2)
        self.assertEqual(len(result.errors), 0)
        self.assertEqual(len(result.decisions), 0)


class PortalEventsTests(unittest.TestCase):
    def test_extracts_round_ended_events_as_server_truth(self):
        data = {
            "game_id": "a_5a06a8f48d67_r1_b4_t0",
            "blocks": [
                {"events": [
                    {"type": "tile_discarded", "seat": 1, "tile": "3w"},
                    {"type": "round_ended", "seat": 0,
                     "data": {"fan": 4, "detail": ["平胡", "财飘", "爆头"],
                               "scores": [24, -8, -8, -8], "next_dealer": 0}},
                ]},
                {"events": [
                    {"type": "round_ended", "seat": 1,
                     "data": {"fan": 1, "detail": ["平胡"],
                               "scores": [-1, 3, -1, -1], "next_dealer": 1}},
                ]},
            ],
        }
        result = di.parse_portal_events_json(data)
        game, round_results, errors = result
        self.assertEqual(len(round_results), 2)
        first, second = round_results
        self.assertEqual(first.round_no, 1)
        self.assertEqual(first.fan, 4)
        self.assertEqual(first.source, "server_truth")
        self.assertEqual(second.round_no, 2)
        self.assertEqual(second.fan, 1)

    def test_missing_game_id_is_recorded_as_error_not_crash(self):
        game, round_results, errors = di.parse_portal_events_json({"blocks": []})
        self.assertIsNone(game)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].category, di.CATEGORY_SCHEMA_MISSING_FIELD)

    def test_game_with_no_round_ended_events_is_not_an_error(self):
        game, round_results, errors = di.parse_portal_events_json(
            {"game_id": "g1", "blocks": [{"events": []}]})
        self.assertIsNotNone(game)
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(round_results), 0)


if __name__ == "__main__":
    unittest.main()
