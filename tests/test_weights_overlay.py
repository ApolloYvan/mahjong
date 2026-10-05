"""G2.2（docs/SIM_FOUNDATION_REPORT.md）：按座位覆盖权重的钩子。核心证明只有
一条——不使用 weights_overlay/frozen_file_weights 时，load_weights() 的返回值
与改动前逐字节相同（tests/test_rule_switches.py 等一切既有测试的通过本身就是
这条证明的一部分；这里再单独锁一遍返回值恒等于 DEFAULT_WEIGHTS）。
"""
import os
import unittest

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.fit import DEFAULT_WEIGHTS, frozen_file_weights, load_weights, weights_overlay  # noqa: E402


class DisabledByDefaultTests(unittest.TestCase):
    """未启用时逐字节不变。"""

    def test_load_weights_equals_defaults_when_nothing_active(self):
        self.assertEqual(load_weights(), dict(DEFAULT_WEIGHTS))

    def test_repeated_calls_are_stable(self):
        self.assertEqual(load_weights(), load_weights())


class WeightsOverlayTests(unittest.TestCase):
    def test_overlay_applies_only_within_context(self):
        before = load_weights()
        with weights_overlay({"rule_route_ev_enabled": 1, "rev_speed_enabled": 1}):
            during = load_weights()
            self.assertEqual(during["rule_route_ev_enabled"], 1)
            self.assertEqual(during["rev_speed_enabled"], 1)
            # 覆盖只叠加指定的键，其余键仍然是默认值。
            self.assertEqual(during["rev_margin"], DEFAULT_WEIGHTS["rev_margin"])
        after = load_weights()
        self.assertEqual(after, before)

    def test_overlay_nests_and_restores_previous_layer(self):
        with weights_overlay({"rev_speed_enabled": 1}):
            with weights_overlay({"rev_speed_enabled": 2}):
                self.assertEqual(load_weights()["rev_speed_enabled"], 2)
            self.assertEqual(load_weights()["rev_speed_enabled"], 1)
        self.assertEqual(load_weights()["rev_speed_enabled"], DEFAULT_WEIGHTS["rev_speed_enabled"])

    def test_overlay_does_not_mutate_default_weights_dict(self):
        with weights_overlay({"rule_route_ev_enabled": 1}):
            load_weights()
        self.assertEqual(DEFAULT_WEIGHTS["rule_route_ev_enabled"], 0)

    def test_exception_inside_context_still_restores(self):
        try:
            with weights_overlay({"rule_route_ev_enabled": 1}):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(load_weights(), dict(DEFAULT_WEIGHTS))


class FrozenFileWeightsTests(unittest.TestCase):
    """用 preloaded= 跳过真实文件 I/O——测试环境不允许碰 models/weights.json，
    这条路径也不例外（同 MJ_WEIGHTS_NO_FILE 的既有约定）。"""

    def test_disabled_by_default(self):
        self.assertEqual(load_weights(), dict(DEFAULT_WEIGHTS))

    def test_frozen_snapshot_used_within_context(self):
        with frozen_file_weights(preloaded={"rule_route_ev_enabled": 1}):
            self.assertEqual(load_weights()["rule_route_ev_enabled"], 1)
        self.assertEqual(load_weights()["rule_route_ev_enabled"], 0)

    def test_frozen_combines_with_overlay_overlay_wins(self):
        with frozen_file_weights(preloaded={"rev_speed_enabled": 1}):
            with weights_overlay({"rev_speed_enabled": 2}):
                self.assertEqual(load_weights()["rev_speed_enabled"], 2)
            # frozen 快照仍然生效，只是 overlay 退出后不再叠加。
            self.assertEqual(load_weights()["rev_speed_enabled"], 1)

    def test_explicit_path_untouched_by_frozen_snapshot(self):
        # frozen 只影响默认路径 "models/weights.json"；显式传别的 path 时，
        # load_weights 仍然按原逻辑试图读那个文件（这里传一个必然不存在的
        # 路径，落回 DEFAULT_WEIGHTS，用来确认没有被 frozen 快照污染）。
        with frozen_file_weights(preloaded={"rule_route_ev_enabled": 1}):
            other = load_weights(path="models/__does_not_exist__.json")
        self.assertEqual(other, dict(DEFAULT_WEIGHTS))


if __name__ == "__main__":
    unittest.main()
