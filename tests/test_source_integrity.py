"""来源指纹核验：保留旧指纹，区分变化、缺失与无来源事实。"""

import hashlib
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "app" / "source_integrity.py"
SPEC = importlib.util.spec_from_file_location("source_integrity_tests", MODULE)
integrity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(integrity)


class SourceIntegrityTests(unittest.TestCase):
    def test_changed_and_missing_files_preserve_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "profile.db"
            con = sqlite3.connect(database)
            con.executescript(
                "CREATE TABLE sources(source_id TEXT PRIMARY KEY, absolute_path TEXT, sha256 TEXT);"
                "CREATE TABLE facts(fact_id TEXT PRIMARY KEY, status TEXT);"
                "CREATE TABLE fact_sources(fact_id TEXT, source_id TEXT);"
            )
            expected = hashlib.sha256(b"original").hexdigest()
            for name in ("stable", "changed", "missing"):
                path = root / f"{name}.txt"
                path.write_bytes(b"original")
                con.execute("INSERT INTO sources VALUES (?, ?, ?)", (name, str(path), expected))
            (root / "changed.txt").write_bytes(b"edited")
            (root / "missing.txt").unlink()
            con.executemany("INSERT INTO facts VALUES (?, ?)", [
                ("fact.one", "confirmed"), ("fact.two", "confirmed"),
                ("fact.three", "rejected"), ("fact.four", "confirmed"),
            ])
            con.executemany("INSERT INTO fact_sources VALUES (?, ?)", [
                ("fact.one", "changed"), ("fact.one", "stable"),
                ("fact.two", "missing"), ("fact.three", "changed"),
            ])
            con.commit()
            before = list(con.iterdump())
            con.close()
            report = integrity.check_database(database)
            self.assertEqual(report["changedSources"], 1)
            self.assertEqual(report["missingSources"], 1)
            self.assertEqual(report["affectedConfirmedFacts"], 2)
            self.assertEqual(report["factsWithoutVerifiedSources"], 2)
            self.assertEqual(report["confirmedWithoutSources"], 1)
            self.assertNotIn(str(root), str(report))
            with sqlite3.connect(database) as after:
                self.assertEqual(list(after.iterdump()), before)

    def test_cli_missing_database_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-created.sqlite3"
            with self.assertRaises(SystemExit) as result:
                integrity.main(["--db", str(path)])
            self.assertEqual(result.exception.code, 2)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
