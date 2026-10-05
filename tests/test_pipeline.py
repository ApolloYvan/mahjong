import json
import os
import tempfile
import unittest

from mj.gamesim import run_games


class PipelineTests(unittest.TestCase):
    def test_gamesim_output_is_selectable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "sim.json")
            report = run_games(rounds=1, output=path)
            with open(path, encoding="utf-8") as source:
                saved = json.load(source)
            self.assertEqual(saved, report)
