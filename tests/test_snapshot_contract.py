"""验证双语不丢失、两端契约一致和失败导出保留旧结果。"""

import contextlib
import copy
import io
import json
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from test_career_profile import CareerProfileTestCase, cp, make_candidate, standard_candidates

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
CONSUMER = ROOT / "tests/consumer"


class SnapshotContractTests(CareerProfileTestCase):
    def setUp(self):
        super().setUp()
        with contextlib.redirect_stdout(io.StringIO()):
            self.init_db()
            items = standard_candidates()
            self.write_standard_candidates(items)
            self.apply(items, [(item, "accept", None) for item in items])

    def snapshot(self):
        with sqlite3.connect(self.db_path) as connection:
            return cp.build_snapshot(connection)

    def bilingual_snapshot(self):
        item = make_candidate("candidate.school.zh", "education", "education.sample",
                              "education.institution", "示例大学", locale="zh-CN")
        with contextlib.redirect_stdout(io.StringIO()):
            self.write_standard_candidates([item])
            self.apply([item], [(item, "accept", None)])
        return self.snapshot()

    def test_all_language_values_and_ids_are_preserved(self):
        snapshot = self.bilingual_snapshot()
        school = snapshot["education"][0]["institution"]
        self.assertEqual(school["locale"], "zh-CN")
        self.assertEqual(school["value"], "示例大学")
        self.assertEqual(school["alternatives"], [{
            "factId": "candidate.education.inst.001", "value": "Sample University", "locale": "en",
        }])
        self.assertEqual(cp.validate_snapshot(snapshot), [])
        # 语言变体可作为表达的依据，不能只校验默认语言事实。
        with contextlib.redirect_stdout(io.StringIO()):
            cp.cmd_add_expression(
                db_path=self.db_path, expression_id="expression.school.en", purpose="intro",
                locale="en", text="Sample University", source_fact_ids=["candidate.education.inst.001"],
                target_roles=["analyst"], max_chars=None, subject_ids=[], status="confirmed", export_path=None,
            )
        self.assertEqual(cp.validate_snapshot(self.snapshot()), [])

    def test_export_failure_preserves_snapshot_and_metadata(self):
        self.output_path.parent.mkdir()
        self.output_path.write_text("previous snapshot")
        before = self.query("SELECT key,value FROM profile_meta ORDER BY key")
        with patch.object(cp.os, "replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                cp.cmd_export(self.db_path, self.output_path)
        self.assertEqual(self.output_path.read_text(), "previous snapshot")
        self.assertEqual(self.query("SELECT key,value FROM profile_meta ORDER BY key"), before)
        self.assertEqual(list(self.output_path.parent.glob(".*.tmp")), [])

    def test_export_rejects_confirmed_fact_without_source(self):
        self.output_path.parent.mkdir()
        self.output_path.write_text("previous snapshot")
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("DELETE FROM fact_sources WHERE fact_id='candidate.person.name.001'")
        with self.assertRaises(cp.ValidationError):
            cp.cmd_export(self.db_path, self.output_path)
        self.assertEqual(self.output_path.read_text(), "previous snapshot")

    def test_replaced_evidence_demotes_expression_without_blocking_export(self):
        old = "candidate.experience.fact.001"
        with contextlib.redirect_stdout(io.StringIO()):
            cp.cmd_add_expression(
                db_path=self.db_path, expression_id="expression.project", purpose="intro", locale="en",
                text="Original statement", source_fact_ids=[old], target_roles=["analyst"], max_chars=None,
                subject_ids=["experience.project"], status="confirmed", export_path=None,
            )
            replacement = make_candidate("candidate.project.updated", "experience", "experience.project",
                                         "experience.fact.main", "Updated statement", locale="en")
            replacement["supersedes"] = old
            self.write_standard_candidates([replacement])
            self.apply([replacement], [(replacement, "accept", None)])
            cp.cmd_export(self.db_path, self.output_path)
        self.assertEqual(cp.load_json(self.output_path)["expressions"], [])
        self.assertEqual(self.query("SELECT status FROM expressions"), [("pending",)])

    def test_snapshot_queries_share_one_read_transaction(self):
        writer = sqlite3.connect(self.db_path)
        writer.execute("PRAGMA journal_mode=WAL")
        reader = sqlite3.connect(self.db_path)
        changed = False

        def concurrent_write(sql):
            nonlocal changed
            if "FROM expressions WHERE" in sql and not changed:
                changed = True
                writer.execute("UPDATE profile_meta SET value='99' WHERE key='profile_revision'")
                writer.commit()

        reader.set_trace_callback(concurrent_write)
        try:
            snapshot = cp.build_snapshot(reader)
            self.assertTrue(changed)
            self.assertEqual(snapshot["profileRevision"], 1)
            self.assertEqual(cp.get_profile_revision(writer), 99)
            self.assertFalse(reader.in_transaction)
        finally:
            reader.close()
            writer.close()

    def test_consumer_schema_matches_app_contract(self):
        canonical = (APP / "career-profile.schema.json").read_text()
        javascript = (CONSUMER / "snapshot-schema.js").read_text()
        self.assertEqual(javascript.split("export const snapshotSchema = ", 1)[1], canonical.rstrip() + ";\n")

    def test_unsupported_contract_constraint_does_not_pass_silently(self):
        import snapshot_contract

        schema = json.loads((APP / "career-profile.schema.json").read_text())
        schema["maxProperties"] = 100
        schema_path = self.tmp / "unsupported-schema.json"
        schema_path.write_text(json.dumps(schema))
        with patch.object(snapshot_contract, "SCHEMA_PATH", schema_path):
            self.assertTrue(cp.validate_snapshot(self.snapshot()))

    @unittest.skipUnless(shutil.which("node"), "消费者一致性检查需要 Node.js")
    def test_python_and_real_consumer_agree_on_same_cases(self):
        standard = self.snapshot()
        bilingual = self.bilingual_snapshot()
        expression = {
            "id": "expression.check", "purpose": "intro", "locale": "en", "maxChars": None,
            "targetRoles": ["analyst"], "text": "Statement", "sourceFactIds": ["candidate.person.name.001"],
        }
        cases = [("current", standard, True), ("bilingual", bilingual, True)]
        legacy = copy.deepcopy(standard)
        legacy["schemaVersion"] = "1.0.0"
        cases.append(("legacy", legacy, True))

        def case(name, mutate, base=None):
            value = copy.deepcopy(base or standard)
            mutate(value)
            cases.append((name, value, False))

        case("missing_name", lambda s: s["person"].pop("fullName"))
        case("missing_location", lambda s: s["person"].pop("location"))
        case("unknown_field", lambda s: s.update(extra="forbidden"))
        case("boolean_revision", lambda s: s.update(profileRevision=True))
        case("bad_date", lambda s: s.update(exportedAt="2025-02-29T00:00:00Z"))
        case("unknown_version", lambda s: s.update(schemaVersion="9.0.0"))
        case("nonstring_ref", lambda s: s.update(expressions=[{**expression, "sourceFactIds": [123]}]))
        case("null_locale", lambda s: s.update(expressions=[{**expression, "locale": None}]))
        case("wrong_subject", lambda s: s.update(expressions=[{**expression, "subjectIds": ["expression.check"]}]))
        case("source_path", lambda s: s["person"]["contact"]["email"].update(absolutePath="/synthetic/private"))
        case("duplicate_locale", lambda s: s["education"][0]["institution"]["alternatives"][0].update(locale="zh-CN"), bilingual)
        case("duplicate_fact_id", lambda s: s["education"][0]["institution"]["alternatives"][0].update(factId="candidate.school.zh"), bilingual)
        case("nested_alternatives", lambda s: s["education"][0]["institution"]["alternatives"][0].update(alternatives=[]), bilingual)
        case("legacy_alternatives", lambda s: s.update(schemaVersion="1.0.0"), bilingual)
        script = (
            "import fs from 'node:fs';"
            f"import {{validateProfileSnapshot,buildProfileIndex}} from {json.dumps((CONSUMER/'profile.js').as_uri())};"
            "const inputs=JSON.parse(fs.readFileSync(0,'utf8'));"
            "console.log(JSON.stringify(inputs.map(p=>{const v=validateProfileSnapshot(p);return {valid:v.valid,"
            "refs:v.valid?[...buildProfileIndex(p).byRef.keys()]:[]}})));"
        )
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                input=json.dumps([item[1] for item in cases]), text=True,
                                capture_output=True, check=True)
        outcomes = json.loads(result.stdout)
        for (name, value, expected), outcome in zip(cases, outcomes, strict=True):
            with self.subTest(case=name):
                self.assertEqual(not cp.validate_snapshot(value), expected)
                self.assertEqual(outcome["valid"], expected)
        self.assertIn("candidate.school.zh", outcomes[1]["refs"])
        self.assertIn("candidate.education.inst.001", outcomes[1]["refs"])


if __name__ == "__main__":
    unittest.main()
