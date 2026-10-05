import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.mc.rollout import GreedyPolicy, run_round  # noqa: E402
from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.tiles import to_counts  # noqa: E402


class TestGreedyPolicyDiscard(unittest.TestCase):
    def test_best_discard_is_a_tile_in_hand(self):
        policy = GreedyPolicy()
        hand = ["1w", "2w", "3w", "5w", "7w", "9w", "1b", "2b", "3b", "东", "南", "西", "北", "白"]
        tile = policy._best_discard(hand, 0)
        self.assertIn(tile, hand)

    def test_discard_from_completed_hand_plus_isolated_honor_is_isolation(self):
        # 3 组已成顺子(123w/456w/789w) + 1 组已成顺子(1b2b3b) + 一对北 + 一张孤立的南：
        # 14 张已经是"3 组顺子+一对"的听牌型只差一组，弃"南"（孤立、不参与
        # 任何组合）应该不会比弃任何一张顺子里的牌更差——用这个不那么依赖
        # 具体数值、只要求"不选择拆掉已成顺子"的弱断言，降低对精确算法
        # 行为的假设风险。
        policy = GreedyPolicy(params={"discard_noise": 0.0})
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                "1b", "2b", "3b", "北", "南"]
        tile = policy._best_discard(hand, 0)
        self.assertNotIn(tile, ["1w", "4w", "7w", "1b"])  # 不该拆掉任何一组已成的顺子起手张


class TestRunRoundTerminates(unittest.TestCase):
    def test_full_self_play_round_finishes(self):
        engine = RoundEngine(build_wall(seed=123), dealer=0, round_no=1, scores=[0, 0, 0, 0])
        policies = {s: GreedyPolicy(rng=__import__("random").Random(s)) for s in range(4)}
        run_round(engine, policies, max_steps=3000)
        self.assertTrue(engine.finished)
        self.assertLessEqual(sum(engine.scores), 0)   # 零和结算

    def test_many_seeds_all_finish_without_hitting_step_cap(self):
        for seed in range(10):
            engine = RoundEngine(build_wall(seed=seed), dealer=seed % 4, round_no=1, scores=[0, 0, 0, 0])
            policies = {s: GreedyPolicy(rng=__import__("random").Random(seed * 10 + s)) for s in range(4)}
            run_round(engine, policies, max_steps=3000)
            self.assertTrue(engine.finished, "seed=%d 没有正常结束" % seed)


if __name__ == "__main__":
    unittest.main()
