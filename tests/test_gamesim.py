import tempfile
import unittest

from mj.gamesim import play
from mj.strategy import choose_discard


class GameSimTests(unittest.TestCase):
    def test_simulation(self):
        with tempfile.TemporaryDirectory() as directory:
            from mj.gamesim import run_games
            report = run_games(rounds=2, output=directory + "/sim.json")
            self.assertEqual(report["rounds"], 2)
            self.assertIn("baseline_wins", report["stats"])

    def test_report_has_detail_counts(self):
        from mj.gamesim import run_games
        with tempfile.TemporaryDirectory() as directory:
            report = run_games(rounds=1, output=directory + "/sim.json")
            self.assertIn("detail_counts", report)

        result = play(11, choose_discard, max_draws=2)
        self.assertIn("fan", result)
