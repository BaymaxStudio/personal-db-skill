"""写入边界回归：只使用临时合成数据库与材料。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from test_career_profile import (
    CareerProfileTestCase,
    cp,
    decision,
    make_candidate,
    standard_candidates,
    write_candidates_file,
    write_decisions_file,
)


class TestWriteIntegrity(CareerProfileTestCase):
    def setUp(self):
        super().setUp()
        self.init_db()

    def apply_items(self, items, decisions=None, sources=None):
        write_candidates_file(self.candidates_path, sources or self.sources, items)
        write_decisions_file(
            self.decisions_path, decisions or [decision(item, "accept") for item in items]
        )
        return cp.cmd_apply_decisions(self.candidates_path, self.decisions_path, self.db_path)

    def seed_name(self):
        item = standard_candidates()[0]
        self.apply_items([item])
        return item

    def add_expression(self, ids, status="confirmed"):
        return cp.cmd_add_expression(
            db_path=self.db_path, expression_id="expression.test", purpose="intro",
            locale="en", text="A synthetic expression.", source_fact_ids=ids,
            target_roles=["test"], max_chars=None, subject_ids=["person.owner"],
            status=status, export_path=None,
        )

    def test_sqlite_backup_includes_committed_wal(self):
        writer = cp.connect(self.db_path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE profile_meta SET value='42' WHERE key='profile_revision'")
            writer.commit()
            self.assertTrue(self.db_path.with_name(self.db_path.name + "-wal").exists())
            path = cp.backup_db(self.db_path)
            with closing(sqlite3.connect(path)) as restored:
                self.assertEqual(cp.get_profile_revision(restored), 42)
                self.assertEqual(restored.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        finally:
            writer.close()

    def test_repeated_backups_are_distinct(self):
        fixed = cp.utcnow()
        with patch.object(cp, "utcnow", return_value=fixed):
            first = cp.backup_db(self.db_path)
            second = cp.backup_db(self.db_path)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())
        self.assertIn(cp.sha256_file(first), first.name)

    def test_failed_backup_leaves_no_published_or_temporary_file(self):
        with patch.object(cp, "sha256_file", side_effect=OSError("synthetic digest failure")):
            with self.assertRaisesRegex(OSError, "synthetic"):
                cp.backup_db(self.db_path)
        self.assertEqual(list(cp.BACKUP_DIR.iterdir()), [])

    def test_failed_backup_prevents_write(self):
        candidate = standard_candidates()[0]
        with patch.object(cp, "backup_db", side_effect=OSError("backup failed")):
            with self.assertRaisesRegex(OSError, "backup failed"):
                self.apply_items([candidate])
        self.assertEqual(self.query("SELECT fact_id FROM facts"), [])
        self.assertEqual(self.query("SELECT value FROM profile_meta"), [("0",)])

    def test_null_locale_confirmed_slot_is_unique_and_batch_rolls_back(self):
        old = standard_candidates()[1]
        self.apply_items([old])
        duplicate = {**old, "candidateId": "candidate.email.second", "proposedValue": "other@example.com"}
        independent = standard_candidates()[0]
        with self.assertRaisesRegex(cp.ValidationError, "已有确认事实"):
            self.apply_items([independent, duplicate])
        self.assertEqual(self.query("SELECT fact_id FROM facts"), [(old["candidateId"],)])
        self.assertEqual(self.query("SELECT value FROM profile_meta"), [("1",)])

    def test_conflict_group_cannot_accept_both_values(self):
        one = standard_candidates()[1]
        one.update(status="conflict", conflictGroupId="conflict.email")
        two = {**one, "candidateId": "candidate.email.two", "proposedValue": "other@example.com"}
        with self.assertRaisesRegex(cp.ValidationError, "已有确认事实"):
            self.apply_items([one, two])
        self.assertEqual(self.query("SELECT fact_id FROM facts"), [])
        self.assertEqual(self.query("SELECT source_id FROM sources"), [])

    def test_source_id_cannot_change_path_type_or_digest(self):
        self.seed_name()
        before = self.query("SELECT absolute_path, document_type, sha256 FROM sources")
        other = self.resume_dir / "other.md"
        other.write_text((self.resume_dir / "source-cv.md").read_text(), encoding="utf-8")
        alternatives = [
            {**self.sources[0], "absolutePath": str(other), "sha256": cp.sha256_file(other)},
            {**self.sources[0], "documentType": "txt"},
        ]
        for source in alternatives:
            with self.subTest(source=source):
                with self.assertRaisesRegex(cp.ValidationError, "新的 sourceId"):
                    self.apply_items([standard_candidates()[1]], sources=[source])
        material = self.resume_dir / "source-cv.md"
        material.write_text(material.read_text() + "changed\n", encoding="utf-8")
        changed_source = {**self.sources[0], "sha256": cp.sha256_file(material)}
        with self.assertRaisesRegex(cp.ValidationError, "新的 sourceId"):
            self.apply_items([standard_candidates()[1]], sources=[changed_source])
        self.assertEqual(self.query("SELECT absolute_path, document_type, sha256 FROM sources"), before)
        self.assertEqual(len(self.query("SELECT fact_id FROM facts")), 1)

    def link_document(self, fact_id):
        source_path = self.resume_dir / "new.txt"
        source_path.write_text("Alex Example", encoding="utf-8")
        return {
            "linkVersion": cp.LINK_VERSION, "generatedAt": cp.utcnow_iso(),
            "sources": [{"sourceId": "source.new", "absolutePath": str(source_path),
                         "documentType": "txt", "sha256": cp.sha256_file(source_path)}],
            "links": [{"linkId": "link.new", "factId": fact_id, "sourceRefs": [
                {"sourceId": "source.new", "locator": "L1", "excerpt": "Alex Example"}
            ]}],
            "unlinks": [{"unlinkId": "unlink.old", "factId": fact_id,
                         "sourceId": self.sources[0]["sourceId"], "reason": "replace evidence"}],
        }

    def apply_link_document(self, document, decisions):
        path = self.tmp / "links.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        write_decisions_file(self.decisions_path, decisions)
        return cp.cmd_apply_links(path, self.decisions_path, self.db_path)

    def test_rejected_or_omitted_link_cannot_justify_removing_last_source(self):
        item = self.seed_name()
        document = self.link_document(item["candidateId"])
        for link_action in ("reject", None):
            decisions = [{"candidateId": "unlink.old", "action": "accept"}]
            if link_action:
                decisions.append({"candidateId": "link.new", "action": link_action})
            with self.subTest(link_action=link_action):
                with self.assertRaisesRegex(cp.ValidationError, "至少要保留一个来源"):
                    self.apply_link_document(document, decisions)
        self.assertEqual(self.query("SELECT source_id FROM fact_sources"), [("source.resume.md",)])
        self.assertEqual(self.query("SELECT value FROM profile_meta"), [("1",)])

    def test_link_replacement_checks_accepted_final_state(self):
        item = self.seed_name()
        document = self.link_document(item["candidateId"])
        self.apply_link_document(document, [
            {"candidateId": "unlink.old", "action": "accept"},
            {"candidateId": "link.new", "action": "accept"},
        ])
        self.assertEqual(self.query("SELECT source_id FROM fact_sources"), [("source.new",)])

    def test_link_validation_runs_again_inside_write_transaction(self):
        item = self.seed_name()
        document = self.link_document(item["candidateId"])
        states = []
        original = cp.validate_links_document
        def observe(data, conn):
            states.append(conn.in_transaction)
            return original(data, conn)
        with patch.object(cp, "validate_links_document", side_effect=observe):
            self.apply_link_document(document, [
                {"candidateId": "unlink.old", "action": "accept"},
                {"candidateId": "link.new", "action": "accept"},
            ])
        self.assertEqual(states, [False, True])

    def test_supersede_keeps_reference_and_demotes_expression(self):
        old = self.seed_name()
        self.add_expression([old["candidateId"]])
        new = {**old, "candidateId": "candidate.name.new", "proposedValue": "New Name",
               "supersedes": old["candidateId"]}
        self.apply_items([new])
        self.assertEqual(self.query("SELECT status FROM expressions"), [("pending",)])
        self.assertEqual(self.query("SELECT fact_id FROM expression_sources"), [(old["candidateId"],)])
        self.assertEqual(self.query("SELECT old_fact_id, new_fact_id FROM fact_supersessions"),
                         [(old["candidateId"], new["candidateId"])])
        self.assertEqual(self.query("SELECT status FROM facts WHERE fact_id=?", (old["candidateId"],)),
                         [("rejected",)])

    def test_later_failure_rolls_back_supersession_and_expression_status(self):
        old = self.seed_name()
        email = standard_candidates()[1]
        self.apply_items([email])
        self.add_expression([old["candidateId"]])
        new = {**old, "candidateId": "candidate.name.new", "supersedes": old["candidateId"]}
        duplicate = {**email, "candidateId": "candidate.email.new"}
        with self.assertRaises(cp.ValidationError):
            self.apply_items([new, duplicate])
        self.assertEqual(self.query("SELECT status FROM expressions"), [("confirmed",)])
        self.assertEqual(self.query("SELECT status FROM facts WHERE fact_id=?", (old["candidateId"],)),
                         [("confirmed",)])
        self.assertEqual(self.query("SELECT * FROM fact_supersessions"), [])

    def test_expression_rejects_enumeration_references(self):
        self.seed_name()
        for item in (standard_candidates()[5], standard_candidates()[9]):
            self.apply_items([item])
            with self.assertRaisesRegex(cp.ValidationError, "分类枚举"):
                self.add_expression([item["candidateId"]])
        self.assertEqual(self.query("SELECT * FROM expressions"), [])

    def test_write_rejects_future_schema_without_changes(self):
        conn = cp.connect(self.db_path)
        conn.execute("INSERT INTO schema_migrations VALUES (4, 'future', 'future')")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(cp.ValidationError, "高于程序支持"):
            self.apply_items([standard_candidates()[0]])
        self.assertEqual(self.query("SELECT * FROM facts"), [])
        self.assertEqual(list(cp.BACKUP_DIR.glob("*")), [])


class TestSchemaMigration(CareerProfileTestCase):
    def setUp(self):
        super().setUp()
        conn = cp.connect(self.db_path)
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, sha256 TEXT NOT NULL)")
        for version in (1, 2):
            sql = cp.MIGRATIONS[version]
            for statement in cp.split_sql_statements(sql):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                         (version, cp.utcnow_iso(), cp.sha256_bytes(sql.encode("utf-8"))))
        cp.ensure_meta_row(conn)
        conn.commit()
        conn.close()

    def test_v2_to_v3_is_atomic_and_preserves_data(self):
        conn = cp.connect(self.db_path)
        conn.execute("UPDATE profile_meta SET value='7'")
        conn.commit()
        cp.migrate(conn)
        self.assertEqual(cp.current_schema_version(conn), 3)
        self.assertEqual(cp.get_profile_revision(conn), 7)
        self.assertIn("COALESCE", conn.execute("SELECT sql FROM sqlite_master WHERE name='facts_confirmed_unique'").fetchone()[0])
        conn.close()

    def test_duplicate_null_locales_block_migration_and_preserve_old_schema(self):
        conn = cp.connect(self.db_path)
        conn.execute("INSERT INTO entities VALUES ('person.owner', 'person', 'Name', 'now', 'now')")
        for fid in ("fact.one", "fact.two"):
            conn.execute("INSERT INTO facts VALUES (?, 'person.owner', 'person.contact.email', '\"a@example.com\"', NULL, 'confirmed', 'now', 'now')", (fid,))
        conn.commit()
        with self.assertRaisesRegex(cp.ValidationError, "需先人工裁决"):
            cp.migrate(conn)
        self.assertEqual(cp.current_schema_version(conn), 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 2)
        self.assertNotIn("COALESCE", conn.execute("SELECT sql FROM sqlite_master WHERE name='facts_confirmed_unique'").fetchone()[0])
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='fact_supersessions'").fetchone())
        conn.close()

    def test_apply_to_v2_migrates_as_part_of_same_transaction(self):
        items = [standard_candidates()[0]]
        write_candidates_file(self.candidates_path, self.sources, items)
        write_decisions_file(self.decisions_path, [decision(items[0], "accept")])
        cp.cmd_apply_decisions(self.candidates_path, self.decisions_path, self.db_path)
        self.assertEqual(self.query("SELECT version FROM schema_migrations"), [(1,), (2,), (3,)])
        self.assertEqual(len(self.query("SELECT * FROM facts")), 1)
        backup = next(cp.BACKUP_DIR.glob("*.db"))
        with closing(sqlite3.connect(backup)) as restored:
            self.assertEqual(cp.current_schema_version(restored), 2)
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 0)

    def test_failed_apply_rolls_back_migration(self):
        one = standard_candidates()[1]
        one.update(status="conflict", conflictGroupId="conflict.email")
        two = {**one, "candidateId": "candidate.email.two", "proposedValue": "b@example.com"}
        write_candidates_file(self.candidates_path, self.sources, [one, two])
        write_decisions_file(self.decisions_path, [decision(item, "accept") for item in (one, two)])
        with self.assertRaises(cp.ValidationError):
            cp.cmd_apply_decisions(self.candidates_path, self.decisions_path, self.db_path)
        self.assertEqual(self.query("SELECT version FROM schema_migrations"), [(1,), (2,)])
        self.assertEqual(self.query("SELECT * FROM facts"), [])

    def test_missing_or_modified_migration_record_is_rejected(self):
        conn = cp.connect(self.db_path)
        conn.execute("DELETE FROM schema_migrations WHERE version=1")
        conn.commit()
        with self.assertRaisesRegex(cp.ValidationError, "不连续"):
            cp.migrate(conn)
        conn.execute("UPDATE schema_migrations SET version=1, sha256='invalid'")
        conn.commit()
        with self.assertRaisesRegex(cp.ValidationError, "指纹"):
            cp.migrate(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main()
