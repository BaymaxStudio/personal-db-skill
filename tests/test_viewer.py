#!/usr/bin/env python3
"""查看器仅使用合成文件与数据库的边界和一致性回归。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("personal_db_viewer", ROOT / "app" / "web" / "serve.py")
viewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(viewer)


class ViewerStaticTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("public page", encoding="utf-8")
        for name in ("serve.py", "config.json", "viewer-config.json", "打开资料库.command"):
            (self.web / name).write_text("private contents", encoding="utf-8")
        (self.root / "private.txt").write_text("outside contents", encoding="utf-8")
        self.web_patch = patch.object(viewer, "WEB_DIR", self.web)
        self.web_patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.web_patch.stop)

    def request(self, path: str, hosts=None, bind="127.0.0.1", port=8733):
        handler = viewer.Handler.__new__(viewer.Handler)
        handler.path = path
        handler.headers = Message()
        for host in ["127.0.0.1:8733"] if hosts is None else hosts:
            handler.headers.add_header("Host", host)
        handler.server = Mock(server_address=(bind, port))
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.send_error = Mock()
        handler._serve_profile = Mock()
        handler.do_GET()
        return handler

    def test_only_explicit_public_page_is_served(self):
        for path in ("/", "/index.html", "/index.html?refresh=1"):
            with self.subTest(path=path):
                handler = self.request(path)
                handler.send_response.assert_called_once_with(200)
                handler.send_error.assert_not_called()
                self.assertEqual(handler.wfile.getvalue(), b"public page")

    def test_api_remains_available_without_static_file_access(self):
        handler = self.request("/api/profile")
        handler._serve_profile.assert_called_once_with()
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_private_files_and_path_encodings_are_rejected(self):
        for path in (
            "/serve.py", "/config.json", "/viewer-config.json", "/打开资料库.command",
            "/../private.txt", "/..//private.txt", "/./index.html", "/index.html;ignored",
            "/%2e%2e/private.txt", "/%252e%252e/private.txt", "/%2e%2e%2fprivate.txt",
            "/%2fprivate.txt", "/%2Findex.html", "/%69ndex.html", "/index.html%00",
            "/..\\private.txt", "/..%5cprivate.txt", "//index.html", "/index.html#fragment",
            "/..//" + str(self.root / "private.txt"), "http://localhost/index.html",
            "http://[invalid", "/web/", "/style.css",
        ):
            with self.subTest(path=path):
                handler = self.request(path)
                handler.send_error.assert_called_once_with(404, "not found")
                handler.send_response.assert_not_called()
                self.assertEqual(handler.wfile.getvalue(), b"")

    def test_index_symlink_cannot_publish_another_file(self):
        (self.web / "index.html").unlink()
        for target in (self.root / "private.txt", self.web / "serve.py"):
            with self.subTest(target=target):
                (self.web / "index.html").symlink_to(target)
                handler = self.request("/")
                handler.send_error.assert_called_once_with(404, "not found")
                (self.web / "index.html").unlink()

    def test_directory_and_missing_page_are_not_served(self):
        (self.web / "index.html").unlink()
        self.request("/").send_error.assert_called_once_with(404, "not found")
        (self.web / "index.html").mkdir()
        self.request("/").send_error.assert_called_once_with(404, "not found")

    def test_only_local_or_explicit_bound_host_with_correct_port_is_accepted(self):
        for host in ("127.0.0.1:8733", "localhost:8733", "LOCALHOST:8733", "[::1]:8733"):
            with self.subTest(host=host):
                handler = self.request("/api/profile", hosts=[host])
                handler._serve_profile.assert_called_once_with()
                handler.send_error.assert_not_called()
        explicit = self.request("/api/profile", hosts=["192.0.2.1:8733"], bind="192.0.2.1")
        explicit._serve_profile.assert_called_once_with()
        ipv6 = self.request("/api/profile", hosts=["[2001:db8::1]:8733"], bind="2001:db8::1")
        ipv6._serve_profile.assert_called_once_with()
        standard = self.request("/api/profile", hosts=["localhost"], port=80)
        standard._serve_profile.assert_called_once_with()

    def test_external_malformed_duplicate_and_wrong_port_hosts_are_rejected(self):
        for hosts in (
            [], ["localhost:8733", "example.test:8733"], ["localhost:8733", "localhost:8733"],
            ["example.test:8733"], ["127.0.0.1:8734"], ["localhost"], ["127.0.0.1:"],
            ["user@localhost:8733"], ["http://localhost:8733"], ["localhost:8733/path"],
            ["localhost:8733#fragment"], ["localhost:8733?query"], ["::1:8733"],
            ["127.0.0.1:8733,example.test:8733"], ["localhost:8733\\example.test"],
        ):
            with self.subTest(hosts=hosts):
                handler = self.request("/api/profile", hosts=hosts)
                handler.send_error.assert_called_once_with(403, "host not allowed")
                handler._serve_profile.assert_not_called()
                self.assertEqual(handler.wfile.getvalue(), b"")

    def test_wildcard_bind_does_not_allow_arbitrary_host(self):
        for bind in ("0.0.0.0", "::"):
            with self.subTest(bind=bind):
                self.request("/api/profile", hosts=["example.test:8733"], bind=bind).send_error.assert_called_once_with(403, "host not allowed")
                self.request("/api/profile", hosts=["localhost:8733"], bind=bind)._serve_profile.assert_called_once_with()


class ViewerProfileTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # URI 中有特殊字符也应读取指定文件，不能截断成另一个数据库。
        self.db_path = self.root / "profile?#.sqlite3"
        self.source = self.root / "source.txt"
        self.source.write_text("Synthetic evidence", encoding="utf-8")
        self.source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT, display_name TEXT);
            CREATE TABLE facts (fact_id TEXT PRIMARY KEY, entity_id TEXT, field_path TEXT,
                                value TEXT, locale TEXT, status TEXT);
            CREATE TABLE sources (source_id TEXT PRIMARY KEY, document_type TEXT,
                                  absolute_path TEXT, sha256 TEXT, registered_at TEXT);
            CREATE TABLE fact_sources (fact_id TEXT, source_id TEXT, locator TEXT, excerpt TEXT);
            CREATE TABLE expressions (expression_id TEXT PRIMARY KEY, purpose TEXT, locale TEXT,
                                      max_chars INTEGER, target_roles TEXT, text TEXT, status TEXT);
            CREATE TABLE expression_sources (expression_id TEXT, fact_id TEXT);
            CREATE TABLE expression_subjects (expression_id TEXT, entity_id TEXT);
            CREATE TABLE profile_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);
            INSERT INTO schema_migrations VALUES (2);
            INSERT INTO entities VALUES ('person.owner', 'person', 'Sample');
            INSERT INTO facts VALUES ('fact.valid', 'person.owner', 'person.fullName.en',
                                      '"Sample"', 'en', 'confirmed');
            INSERT INTO facts VALUES ('fact.rejected', 'person.owner', 'person.contact.email',
                                      '"old@example.com"', NULL, 'rejected');
            INSERT INTO facts VALUES ('fact.pending', 'person.owner', 'person.contact.phone',
                                      '"100"', NULL, 'pending');
            INSERT INTO facts VALUES ('fact.unsourced', 'person.owner', 'person.location.currentCity',
                                      '"Test City"', NULL, 'confirmed');
            INSERT INTO fact_sources VALUES ('fact.valid', 'source.test', 'L1', 'Synthetic evidence');
            INSERT INTO fact_sources VALUES ('fact.rejected', 'source.test', 'L1', 'Synthetic evidence');
            INSERT INTO fact_sources VALUES ('fact.pending', 'source.test', 'L1', 'Synthetic evidence');
        """)
        self.conn.execute("INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
                          ("source.test", "txt", str(self.source), self.source_sha, "2026-09-28"))
        self.conn.commit()
        self.db_patch = patch.object(viewer, "DB_PATH", self.db_path)
        self.cfg_patch = patch.object(viewer, "VIEWER_CONFIG_PATH", self.root / "absent.json")
        self.db_patch.start()
        self.cfg_patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.conn.close)
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.cfg_patch.stop)

    def add_expression(self, expression_id, fact_ids, status="confirmed", subject=None):
        self.conn.execute("INSERT INTO expressions VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (expression_id, "summary", "en", None, "[]", expression_id, status))
        self.conn.executemany("INSERT INTO expression_sources VALUES (?, ?)",
                              [(expression_id, fact_id) for fact_id in fact_ids])
        if subject:
            self.conn.execute("INSERT INTO expression_subjects VALUES (?, ?)", (expression_id, subject))
        self.conn.commit()

    def test_invalid_expression_dependencies_are_hidden_without_writing(self):
        self.add_expression("valid", ["fact.valid"])
        self.add_expression("valid.attached", ["fact.valid"], subject="person.owner")
        self.add_expression("stale", ["fact.valid", "fact.rejected"])
        self.add_expression("pending.fact", ["fact.pending"])
        self.add_expression("no.source", ["fact.unsourced"])
        self.add_expression("missing.fact", ["fact.missing"])
        self.add_expression("no.refs", [])
        self.add_expression("awaiting.review", ["fact.valid"], status="pending")
        original_rows = self.conn.execute("SELECT * FROM expressions ORDER BY expression_id").fetchall()
        result = viewer.load_profile()
        self.assertEqual([e["id"] for e in result["expressions"]], ["valid"])
        self.assertEqual([e["id"] for e in result["entities"][0]["expressions"]], ["valid.attached"])
        self.assertEqual(result["counts"]["expressions"], 2)
        self.assertEqual(result["counts"]["invalidExpressions"], 5)
        self.assertEqual(result["counts"]["pendingExpressions"], 1)
        self.assertEqual(original_rows, self.conn.execute("SELECT * FROM expressions ORDER BY expression_id").fetchall())
        self.assertNotIn("stale", json.dumps(viewer.load_profile(include_rejected=True)))

    def test_profile_connection_is_read_only(self):
        original_connect = sqlite3.connect
        attempts = []

        def connect_read_only(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                    conn.execute("DELETE FROM facts")
                conn.rollback()
                attempts.append(args[0])
                return conn
            except BaseException:
                conn.close()
                raise

        with patch.object(viewer.sqlite3, "connect", side_effect=connect_read_only):
            result = viewer.load_profile()
        self.assertEqual(len(attempts), 1)
        self.assertIn("mode=ro", attempts[0])
        self.assertEqual(result["counts"]["facts"], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts").fetchone()[0], 4)

    def test_all_queries_share_one_snapshot_during_concurrent_commit(self):
        self.add_expression("valid", ["fact.valid"])
        original_connect = sqlite3.connect
        writer = self.conn

        class ConcurrentConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if sql.startswith("SELECT entity_id, entity_type"):
                    rows = cursor.fetchall()
                    writer.execute("UPDATE facts SET status='rejected' WHERE fact_id='fact.valid'")
                    writer.commit()
                    return rows
                return cursor

        def connect_reader(*args, **kwargs):
            return original_connect(*args, factory=ConcurrentConnection, **kwargs)

        with patch.object(viewer.sqlite3, "connect", side_effect=connect_reader):
            result = viewer.load_profile()
        self.assertEqual([e["id"] for e in result["expressions"]], ["valid"])
        self.assertEqual(result["counts"]["facts"], 2)
        self.assertEqual(self.conn.execute("SELECT status FROM facts WHERE fact_id='fact.valid'").fetchone()[0], "rejected")
        self.assertEqual(viewer.load_profile()["counts"]["invalidExpressions"], 1)

    def test_source_changes_are_reported_without_rewriting_fingerprints(self):
        self.source.write_text("Changed synthetic evidence", encoding="utf-8")
        result = viewer.load_profile()
        self.assertEqual(result["integrity"]["changedSources"], 1)
        facts = result["entities"][0]["facts"]
        source = next(f for f in facts if f["id"] == "fact.valid")["sources"][0]
        self.assertEqual(source["integrity"], "changed")
        self.assertEqual(self.conn.execute("SELECT sha256 FROM sources").fetchone()[0], self.source_sha)

    def test_distinct_sources_with_same_basename_are_counted_separately(self):
        other_dir = self.root / "another"
        other_dir.mkdir()
        other = other_dir / "source.txt"
        other.write_text("Synthetic evidence", encoding="utf-8")
        self.conn.execute("INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
                          ("source.another", "txt", str(other), self.source_sha, "2026-09-28"))
        self.conn.execute("INSERT INTO fact_sources VALUES (?, ?, ?, ?)",
                          ("fact.valid", "source.another", "L1", "Synthetic evidence"))
        self.conn.commit()
        self.assertEqual(viewer.load_profile()["counts"]["sources"], 2)


if __name__ == "__main__":
    unittest.main()
