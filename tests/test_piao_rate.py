import json
import os
import tempfile
import unittest

from mj.piao import rate_for


class PiaoRateTests(unittest.TestCase):
    def test_reads_stratified_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rate.json")
            with open(path, "w", encoding="utf-8") as output:
                json.dump({"rate": 0.2, "groups": {"dealer|1|2": {"rate": 0.75}}}, output)
            self.assertEqual(rate_for({"dealer": True, "seat": 1, "chain_count": 2}, path), 0.75)
