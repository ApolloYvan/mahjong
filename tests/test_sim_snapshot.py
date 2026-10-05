"""G2.1：mj.sim.snapshot.build_snapshot 字段命名/口径核对（见模块 docstring
的实证依据——3c 用真实决策日志核实的 wall_remaining 偏移、melds 扁平 kind、
god_discarder_seat 用 -1 表示空）。"""
import os
import unittest

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402


def _fresh_engine(dealer=0):
    return RoundEngine(build_wall(seed=7), dealer=dealer, round_no=1)


class BuildSnapshotTests(unittest.TestCase):
    def test_wall_remaining_is_server_convention_not_engine_convention(self):
        engine = _fresh_engine()
        snap = build_snapshot(engine, 0)
        self.assertEqual(snap["wall_remaining"], engine.wall_remaining() - 1)

    def test_god_discarder_seat_uses_minus_one_for_none(self):
        engine = _fresh_engine()
        self.assertIsNone(engine.god_discarder_seat)
        snap = build_snapshot(engine, 0)
        self.assertEqual(snap["god"]["god_discarder_seat"], -1)

    def test_gang_meld_kind_is_flattened_to_server_naming(self):
        engine = _fresh_engine()
        engine.hands[0] = ["7b", "7b", "7b", "7b"] + engine.hands[0][4:]
        engine.apply_gang(0, "7b", "an", auto_draw=False)
        snap = build_snapshot(engine, 0)
        self.assertEqual(snap["melds"][0], [{"kind": "gang_an", "tiles": ["7b", "7b", "7b", "7b"]}])

    def test_phase_splits_response_peng_and_chi(self):
        engine = _fresh_engine()
        engine.catch_play = False
        engine.phase = "response"
        engine.response_stage = "peng"
        self.assertEqual(build_snapshot(engine, 1)["phase"], "response_peng")
        engine.response_stage = "chi"
        self.assertEqual(build_snapshot(engine, 1)["phase"], "response_chi")

    def test_draw_phase_drawn_tile_only_shown_to_current_turn_seat(self):
        engine = _fresh_engine()
        engine.step_draw(forced_tile="9w")
        self.assertEqual(build_snapshot(engine, engine.turn)["drawn_tile"], "9w")
        other = (engine.turn + 1) % 4
        self.assertEqual(build_snapshot(engine, other)["drawn_tile"], "")

    def test_chain_count_only_shown_to_owning_seat(self):
        # G2.1 3c 精确 seq 对齐核出来的真实 bug：服务端按座位下发
        # chain_count，不是全局广播——只有 chain_owner/god_discarder_seat
        # 自己能看到真实链数，其余座位一律看到 0（哪怕引擎内部这条链客观
        # 上还活着）。5 个逐例排查的真实决策记录全部是这个模式，见
        # mj/sim/snapshot.py 模块 docstring。
        engine = _fresh_engine()
        engine.chain_count = 2
        engine.chain_owner = 1
        engine.god_discarder_seat = 1
        self.assertEqual(build_snapshot(engine, 1)["god"]["chain_count"], 2)
        for other in (0, 2, 3):
            self.assertEqual(build_snapshot(engine, other)["god"]["chain_count"], 0)


if __name__ == "__main__":
    unittest.main()
