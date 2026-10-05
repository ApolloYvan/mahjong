"""B2 返修：DecisionLog 按大小轮转 + gzip 归档测试。"""
import gzip
import json
import os
import tempfile
import unittest

from mj.logging import DecisionLog


class SizeBasedRotationTests(unittest.TestCase):
    def test_no_rotation_when_max_bytes_not_set(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory)
            for i in range(50):
                log.append("state", {"i": i, "padding": "x" * 200})
            files = os.listdir(directory)
            self.assertEqual(len(files), 1)  # 仅按天轮转，没有超过一天不会产生第二个文件

    def test_rotates_when_exceeding_max_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory, max_bytes=500, gzip_on_rotate=False)
            for i in range(100):
                log.append("state", {"i": i, "padding": "x" * 50})
            files = sorted(os.listdir(directory))
            # 应该产生多个轮转文件（当天主文件 + 若干 .N.jsonl）。
            self.assertGreater(len(files), 1)

    def test_rotated_file_is_gzip_compressed_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory, max_bytes=300)
            for i in range(80):
                log.append("state", {"i": i, "padding": "x" * 50})
            files = os.listdir(directory)
            gz_files = [f for f in files if f.endswith(".gz")]
            self.assertGreater(len(gz_files), 0)

    def test_gzip_archived_content_is_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory, max_bytes=300)
            for i in range(80):
                log.append("state", {"i": i, "padding": "x" * 50})
            gz_files = [f for f in os.listdir(directory) if f.endswith(".gz")]
            self.assertGreater(len(gz_files), 0)
            path = os.path.join(directory, gz_files[0])
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                lines = [json.loads(l) for l in handle if l.strip()]
            self.assertGreater(len(lines), 0)
            for record in lines:
                self.assertIn("kind", record)
                self.assertIn("payload", record)

    def test_no_data_loss_across_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory, max_bytes=400)
            n = 150
            for i in range(n):
                log.append("state", {"i": i})
            total_lines = 0
            for fname in os.listdir(directory):
                path = os.path.join(directory, fname)
                if fname.endswith(".gz"):
                    with gzip.open(path, "rt", encoding="utf-8") as handle:
                        total_lines += sum(1 for l in handle if l.strip())
                else:
                    with open(path, encoding="utf-8") as handle:
                        total_lines += sum(1 for l in handle if l.strip())
            self.assertEqual(total_lines, n)


if __name__ == "__main__":
    unittest.main()
