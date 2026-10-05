"""mj.datastore 契约测试：schema 建立、幂等 upsert、唯一约束去重。"""
import json
import os
import sqlite3
import tempfile
import unittest

from mj import datastore as ds


class SchemaTests(unittest.TestCase):
    def test_init_schema_creates_all_tables(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for expected in ("source_files", "games", "round_results", "decisions",
                          "errors", "aggregates", "schema_meta"):
            self.assertIn(expected, tables)

    def test_schema_version_recorded(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        self.assertEqual(row[0], str(ds.SCHEMA_VERSION))

    def test_init_schema_is_idempotent(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        ds.init_schema(conn)  # 不应报错


class Sha256Tests(unittest.TestCase):
    def test_sha256_file_matches_hashlib(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.txt")
            with open(path, "wb") as handle:
                handle.write(b"hello world" * 1000)
            expected = hashlib.sha256(b"hello world" * 1000).hexdigest()
            self.assertEqual(ds.sha256_file(path), expected)

    def test_sha256_file_streams_large_file_without_full_read(self):
        # 用小 chunk_size 验证分块读取路径也能得到正确结果（模拟大文件场景）。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.bin")
            content = os.urandom(1 << 16)
            with open(path, "wb") as handle:
                handle.write(content)
            import hashlib
            expected = hashlib.sha256(content).hexdigest()
            self.assertEqual(ds.sha256_file(path, chunk_size=97), expected)


class SourceFileDedupTests(unittest.TestCase):
    def test_reimport_same_content_is_detected_by_sha256(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        fid = ds.insert_source_file(
            conn, path="a.jsonl", sha256="deadbeef", size_bytes=10, mtime=1.0,
            fmt="decision_jsonl", source_type="local_decision_log",
        )
        conn.commit()
        found = ds.find_source_file_by_sha256(conn, "deadbeef")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], fid)
        # 不同路径但相同 sha256 也应命中（内容去重，不是路径去重）。
        found2 = ds.find_source_file_by_sha256(conn, "deadbeef")
        self.assertEqual(found2["path"], "a.jsonl")

    def test_different_sha256_creates_new_row(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        ds.insert_source_file(conn, path="a.jsonl", sha256="hash1", size_bytes=1,
                               mtime=1.0, fmt="decision_jsonl", source_type="local_decision_log")
        ds.insert_source_file(conn, path="a.jsonl", sha256="hash2", size_bytes=2,
                               mtime=2.0, fmt="decision_jsonl", source_type="local_decision_log")
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
        self.assertEqual(count, 2)

    def test_duplicate_sha256_rejected_by_unique_constraint(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        ds.insert_source_file(conn, path="a.jsonl", sha256="samehash", size_bytes=1,
                               mtime=1.0, fmt="decision_jsonl", source_type="local_decision_log")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            ds.insert_source_file(conn, path="b.jsonl", sha256="samehash", size_bytes=2,
                                   mtime=2.0, fmt="decision_jsonl",
                                   source_type="local_decision_log")


class GameUpsertTests(unittest.TestCase):
    def test_upsert_fills_null_fields_without_overwriting_existing(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        ds.upsert_game(conn, game_id="g1", policy_version="legacy_v1")
        ds.upsert_game(conn, game_id="g1", policy_version="should_not_overwrite",
                        room_id="room_a")
        conn.commit()
        row = conn.execute("SELECT * FROM games WHERE game_id='g1'").fetchone()
        # policy_version 已经是 legacy_v1，不应被第二次 upsert 的值覆盖。
        self.assertEqual(row["policy_version"], "legacy_v1")
        # room_id 之前是 NULL，应该被补全。
        self.assertEqual(row["room_id"], "room_a")

    def test_upsert_game_is_idempotent_no_duplicate_rows(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        for _ in range(3):
            ds.upsert_game(conn, game_id="g1", room_id="room_a")
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        self.assertEqual(count, 1)


class RoundResultUpsertTests(unittest.TestCase):
    def test_server_truth_and_local_estimate_coexist(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        ds.upsert_round_result(conn, game_id="g1", round_no=5, winner_seat=0, fan=4,
                                detail=["平胡", "财飘", "爆头"], scores=[10, -3, -3, -4],
                                next_dealer=0, source="server_truth")
        ds.upsert_round_result(conn, game_id="g1", round_no=5, winner_seat=0, fan=2,
                                detail=["平胡", "爆头"], scores=None, next_dealer=None,
                                source="local_estimate")
        conn.commit()
        rows = conn.execute(
            "SELECT source, fan FROM round_results WHERE game_id='g1' AND round_no=5 "
            "ORDER BY source"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        by_source = {r["source"]: r["fan"] for r in rows}
        self.assertEqual(by_source["server_truth"], 4)
        self.assertEqual(by_source["local_estimate"], 2)

    def test_reimport_same_key_updates_not_duplicates(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        ds.upsert_round_result(conn, game_id="g1", round_no=1, winner_seat=0, fan=1,
                                detail=["平胡"], scores=None, next_dealer=None,
                                source="server_truth")
        ds.upsert_round_result(conn, game_id="g1", round_no=1, winner_seat=0, fan=1,
                                detail=["平胡"], scores=[1, -1, 0, 0], next_dealer=1,
                                source="server_truth")
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM round_results WHERE game_id='g1' AND round_no=1"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        row = conn.execute(
            "SELECT scores FROM round_results WHERE game_id='g1' AND round_no=1"
        ).fetchone()
        self.assertEqual(json.loads(row["scores"]), [1, -1, 0, 0])


class DecisionUpsertTests(unittest.TestCase):
    def test_decision_id_primary_key_dedupes(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        for _ in range(3):
            ds.upsert_decision(
                conn, decision_id="d1", decision_id_synthesized=False, game_id="g1",
                round_no=1, seat=0, state_hash="h1", policy_version="v1",
                schema_version="1", config_hash="c1", action="discard", tile="1w",
                time_value="2026-09-21T00:00:00Z",
            )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_derive_decision_id_deterministic(self):
        a = ds.derive_decision_id("sha1", 5, "decision", game_id="g1", round_no=1, seat=0,
                                   time_value="2026-09-21T00:00:00Z")
        b = ds.derive_decision_id("sha1", 5, "decision", game_id="g1", round_no=1, seat=0,
                                   time_value="2026-09-21T00:00:00Z")
        c = ds.derive_decision_id("sha1", 6, "decision", game_id="g1", round_no=1, seat=0,
                                   time_value="2026-09-21T00:00:00Z")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("derived-"))

    def test_derive_decision_id_distinguishes_same_timestamp_different_line(self):
        # P2 返修：旧版只用 game/round/seat/time 四元组派生，同一时刻多次决策
        # 会碰撞；新版加入 source_sha256+line_no，天然不会碰撞。
        a = ds.derive_decision_id("shaX", 10, "decision", game_id="g1", round_no=1, seat=0,
                                   time_value="SAME_TIME")
        b = ds.derive_decision_id("shaX", 11, "decision", game_id="g1", round_no=1, seat=0,
                                   time_value="SAME_TIME")
        self.assertNotEqual(a, b)


class ErrorUpsertTests(unittest.TestCase):
    def test_error_dedup_by_file_line_category(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        fid = ds.insert_source_file(conn, path="a.jsonl", sha256="h1", size_bytes=1,
                                     mtime=1.0, fmt="decision_jsonl",
                                     source_type="local_decision_log")
        conn.commit()
        for _ in range(2):
            ds.upsert_error(conn, source_file_id=fid, line_no=42,
                             category="json_decode_error", summary="bad json")
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
        self.assertEqual(count, 1)


class AggregateUpsertTests(unittest.TestCase):
    def test_aggregate_upsert_replaces_value(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        ds.upsert_aggregate(conn, "data_quality", {"lines": 100})
        ds.upsert_aggregate(conn, "data_quality", {"lines": 200})
        conn.commit()
        row = conn.execute("SELECT value FROM aggregates WHERE key='data_quality'").fetchone()
        self.assertEqual(json.loads(row["value"])["lines"], 200)


if __name__ == "__main__":
    unittest.main()
