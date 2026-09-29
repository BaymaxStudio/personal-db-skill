#!/usr/bin/env python3
"""career_profile.py 的单元测试（unittest，临时目录夹具）。"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# 同一份测试既在库目录（career_profile.py 与 tests/ 同级）跑，也在 skill 仓库（代码在 app/）跑
ROOT = Path(__file__).resolve().parent.parent
PROFILE_DB_DIR = ROOT / "app" if (ROOT / "app" / "career_profile.py").exists() else ROOT
sys.path.insert(0, str(PROFILE_DB_DIR))

import career_profile as cp  # noqa: E402


SOURCE_TEXT = """# Alex Example
Phone: +86 100-0000-0000
Email: sample@example.com
City: Example City
Institute: Sample University
Degree: Bachelor of Science
Major: Computer Science
GPA: 3.60/4.0 Average Score: 90.00/100 Ranking: 8/100
Project: Example Research Project (2024-05 to 2025-05)
Project: Example Research Project (2024-05 to 2025-04)
Fieldwork: 12 sample sites
Class: Student Club Coordinator (2023-09 to 2024-09)
Band: Campus Ensemble (2022-09 to 2026-06)
Language: English IELTS 7.5
Award: Academic Scholarship (2025-11)
"""


def make_candidate(
    candidate_id,
    entity_type,
    entity_key,
    field_path,
    value,
    locale=None,
    status="pending",
    conflict_group_id=None,
    source_refs=None,
):
    return {
        "candidateId": candidate_id,
        "entityType": entity_type,
        "entityKey": entity_key,
        "fieldPath": field_path,
        "proposedValue": value,
        "locale": locale,
        "status": status,
        "conflictGroupId": conflict_group_id,
        "sourceRefs": source_refs or [{"sourceId": "source.resume.md", "locator": "L1", "excerpt": "Alex Example"}],
    }


def standard_sources(resume_dir: Path) -> list[dict]:
    path = resume_dir / "source-cv.md"
    return [
        {
            "sourceId": "source.resume.md",
            "absolutePath": str(path),
            "documentType": "md",
            "sha256": cp.sha256_file(path),
        }
    ]


def write_candidates_file(path: Path, sources: list[dict], candidates: list[dict]) -> Path:
    doc = {
        "candidateVersion": "1.0.0",
        "generatedAt": "2026-08-31T08:00:00Z",
        "sources": sources,
        "candidates": candidates,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return path


def write_decisions_file(path: Path, decisions: list[dict]) -> Path:
    doc = {
        "decisionVersion": "1.0.0",
        "decidedBy": "Sample Reviewer",
        "decidedAt": "2026-08-31T09:00:00Z",
        "decisions": decisions,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return path


def standard_candidates() -> list[dict]:
    """一组覆盖全部实体类型、满足契约必填的最小候选集。"""
    refs = [{"sourceId": "source.resume.md", "locator": "L1", "excerpt": "Email: sample@example.com"}]
    refs_name = [{"sourceId": "source.resume.md", "locator": "L1", "excerpt": "Alex Example"}]
    refs_title = [{"sourceId": "source.resume.md", "locator": "L1", "excerpt": "Example Research Project"}]
    return [
        make_candidate("candidate.person.name.001", "person", "person.owner", "person.fullName.en",
                       "Alex Example", locale="en", source_refs=refs_name),
        make_candidate("candidate.person.email.001", "person", "person.owner", "person.contact.email",
                       "sample@example.com", source_refs=refs),
        make_candidate("candidate.person.phone.001", "person", "person.owner", "person.contact.phone",
                       "+86 100-0000-0000", source_refs=refs),
        make_candidate("candidate.education.inst.001", "education", "education.sample",
                       "education.institution", "Sample University", locale="en"),
        make_candidate("candidate.education.gpa.001", "education", "education.sample",
                       "education.gpa", "3.60/4.0"),
        make_candidate("candidate.experience.type.001", "experience", "experience.project",
                       "experience.type", "research"),
        make_candidate("candidate.experience.title.001", "experience", "experience.project",
                       "experience.title", "Example Research Project", locale="en",
                       source_refs=refs_title),
        make_candidate("candidate.experience.start.001", "experience", "experience.project",
                       "experience.startDate", "2024-05"),
        make_candidate("candidate.experience.fact.001", "experience", "experience.project",
                       "experience.fact.main", "Coordinated fieldwork with 12 sample sites.",
                       locale="en"),
        make_candidate("candidate.skill.category.001", "skill", "skill.language.english",
                       "skill.category", "language"),
        make_candidate("candidate.skill.name.001", "skill", "skill.language.english",
                       "skill.name", "English", locale="en"),
        make_candidate("candidate.skill.level.001", "skill", "skill.language.english",
                       "skill.level", "IELTS 7.5", locale="en"),
        make_candidate("candidate.award.name.001", "award", "award.scholarship",
                       "award.name", "Academic Scholarship", locale="en"),
    ]


def decision(item: dict, action: str, replacement: str | None = None) -> dict:
    return {
        "candidateId": item["candidateId"],
        "action": action,
        "replacementValue": replacement,
    }


class CareerProfileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.resume_dir = self.tmp / "resume"
        self.resume_dir.mkdir()
        (self.resume_dir / "source-cv.md").write_text(SOURCE_TEXT, encoding="utf-8")
        self._orig_allowed = cp.ALLOWED_SOURCE_DIRS
        cp.ALLOWED_SOURCE_DIRS = [self.resume_dir.resolve()]
        self.db_path = self.tmp / "data" / "test.sqlite3"
        self.sources = standard_sources(self.resume_dir)
        self.candidates_path = self.tmp / "candidates.json"
        self.decisions_path = self.tmp / "decisions.json"
        self.output_path = self.tmp / "out" / "snapshot.json"
        self.backup_dir_cp = self.tmp / "backups"
        self._orig_backup_dir = cp.BACKUP_DIR
        cp.BACKUP_DIR = self.backup_dir_cp
        self._orig_project_dir = cp.PROFILE_DB_DIR
        cp.PROFILE_DB_DIR = self.tmp

    def tearDown(self):
        cp.ALLOWED_SOURCE_DIRS = self._orig_allowed
        cp.BACKUP_DIR = self._orig_backup_dir
        cp.PROFILE_DB_DIR = self._orig_project_dir
        self._tmp.cleanup()

    def write_standard_candidates(self, candidates=None):
        write_candidates_file(
            self.candidates_path, self.sources, candidates or standard_candidates()
        )
        return self.candidates_path

    def init_db(self) -> int:
        return cp.main(["init", "--db", str(self.db_path)])

    def apply(self, candidate_items, decisions):
        write_decisions_file(
            self.decisions_path, [decision(c, a, r) for (c, a, r) in decisions]
        )
        return cp.main(
            ["apply-decisions", str(self.candidates_path), str(self.decisions_path),
             "--db", str(self.db_path)]
        )

    def exec_apply_via_main(self, candidates, all_decisions):
        """把候选写文件后执行 apply-decisions；返回退出码。"""
        write_decisions_file(self.decisions_path, all_decisions)
        return cp.main(
            ["apply-decisions", str(self.candidates_path), str(self.decisions_path),
             "--db", str(self.db_path)]
        )

    def query(self, sql, params=()):
        conn = cp.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class TestInit(CareerProfileTestCase):
    def test_init_creates_schema(self):
        self.assertEqual(self.init_db(), 0)
        self.assertTrue(self.db_path.exists())
        tables = {r[0] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"schema_migrations", "profile_meta", "sources", "entities",
                    "facts", "fact_sources", "expressions", "expression_sources",
                    "expression_subjects"}
        self.assertTrue(expected.issubset(tables), f"缺少表：{expected - tables}")
        self.assertEqual(self.query("SELECT value FROM profile_meta WHERE key='profile_revision'"),
                         [("0",)])
        self.assertEqual(self.query("SELECT version FROM schema_migrations"),
                         [(version,) for version in range(1, cp.DB_SCHEMA_VERSION + 1)])

    def test_init_idempotent_keeps_data(self):
        self.init_db()
        conn = cp.connect(self.db_path)
        conn.execute("UPDATE profile_meta SET value='7' WHERE key='profile_revision'")
        conn.commit()
        conn.close()
        self.assertEqual(self.init_db(), 0)
        self.assertEqual(self.query("SELECT value FROM profile_meta WHERE key='profile_revision'"),
                         [("7",)])
        # 已是最新版本时不产生多余备份
        self.assertEqual(list(self.backup_dir_cp.glob("*")), [])

    def test_init_migration_backs_up_existing_db(self):
        self.init_db()
        self.assertEqual(self.init_db(), 0)
        # 模拟旧版本库：删除迁移记录后再次 init 应备份
        conn = cp.connect(self.db_path)
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
        conn.close()
        self.init_db()
        backups = list(self.backup_dir_cp.glob("career_profile.*.db"))
        self.assertEqual(len(backups), 1)
        self.assertRegex(
            backups[0].name,
            r"^career_profile\.\d{8}T\d{12}Z\.[0-9a-f]{32}\.[0-9a-f]{64}\.db$",
        )


class TestValidateCandidates(CareerProfileTestCase):
    def test_valid_document_passes(self):
        self.write_standard_candidates()
        self.assertEqual(cp.main(["validate-candidates", str(self.candidates_path)]), 0)

    def test_duplicate_candidate_id_rejected(self):
        cands = standard_candidates()
        dup = dict(cands[0])
        dup["candidateId"] = cands[1]["candidateId"]
        cands.append(dup)
        self.write_standard_candidates(cands)
        with self.assertRaises(cp.ValidationError) as ctx:
            cp.validate_candidates_document(cp.load_json(self.candidates_path))
        self.assertTrue(any("重复的候选 ID" in e for e in ctx.exception.errors))

    def test_sha_mismatch_rejected(self):
        cands = standard_candidates()
        self.write_standard_candidates(cands)
        doc = cp.load_json(self.candidates_path)
        doc["sources"][0]["sha256"] = "0" * 64
        with open(self.candidates_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        with self.assertRaises(cp.ValidationError) as ctx:
            cp.validate_candidates_document(cp.load_json(self.candidates_path))
        self.assertTrue(any("SHA-256 不一致" in e for e in ctx.exception.errors))

    def test_outside_allowed_dir_rejected(self):
        cands = standard_candidates()
        real = self.resume_dir / "source-cv.md"
        self.write_standard_candidates(cands)
        doc = cp.load_json(self.candidates_path)
        doc["sources"][0]["absolutePath"] = str(self.tmp / "outside" / "cv.md")
        with open(self.candidates_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        with self.assertRaises(cp.ValidationError) as ctx:
            cp.validate_candidates_document(cp.load_json(self.candidates_path))
        self.assertTrue(any("不在允许读取目录内" in e for e in ctx.exception.errors))

    def test_excerpt_not_in_text_source_rejected(self):
        cands = standard_candidates()
        cands[0]["sourceRefs"][0]["excerpt"] = "this sentence does not exist anywhere"
        self.write_standard_candidates(cands)
        with self.assertRaises(cp.ValidationError) as ctx:
            cp.validate_candidates_document(cp.load_json(self.candidates_path))
        self.assertTrue(any("未在来源文件中找到证据摘录" in e for e in ctx.exception.errors))

    def test_conflict_group_valid(self):
        """冲突组：同字段不同值、同组、status=conflict，应当通过。"""
        a = make_candidate("candidate.project.end.001", "experience", "experience.project",
                           "experience.endDate", "2025-05", status="conflict",
                           conflict_group_id="conflict.project-end",
                           source_refs=[{"sourceId": "source.resume.md", "locator": "L2",
                                         "excerpt": "Example Research Project (2024-05 to 2025-05)"}])
        b = make_candidate("candidate.project.end.002", "experience", "experience.project",
                           "experience.endDate", "2025-04", status="conflict",
                           conflict_group_id="conflict.project-end",
                           source_refs=[{"sourceId": "source.resume.md", "locator": "L3",
                                         "excerpt": "Example Research Project (2024-05 to 2025-04)"}])
        self.write_standard_candidates([a, b])
        self.assertEqual(cp.main(["validate-candidates", str(self.candidates_path)]), 0)

    def test_conflict_group_requires_group_id(self):
        a = make_candidate("candidate.project.end.001", "experience", "experience.project",
                           "experience.endDate", "2025-05", status="conflict")
        b = make_candidate("candidate.project.end.002", "experience", "experience.project",
                           "experience.endDate", "2025-04", status="conflict")
        self.write_standard_candidates([a, b])
        with self.assertRaises(cp.ValidationError) as ctx:
            cp.validate_candidates_document(cp.load_json(self.candidates_path))
        self.assertTrue(any("conflictGroupId" in e for e in ctx.exception.errors))

    def test_single_conflict_candidate_rejected(self):
        a = make_candidate("candidate.project.end.001", "experience", "experience.project",
                           "experience.endDate", "2025-05", status="conflict",
                           conflict_group_id="conflict.project-end")
        self.write_standard_candidates([a])
        with self.assertRaises(cp.ValidationError) as ctx:
            cp.validate_candidates_document(cp.load_json(self.candidates_path))
        self.assertTrue(any("至少需 2 个候选" in e for e in ctx.exception.errors))

    def test_duplicate_same_value_not_merged_rejected(self):
        a = make_candidate("candidate.project.end.001", "experience", "experience.project",
                           "experience.endDate", "2025-05", status="conflict",
                           conflict_group_id="conflict.project-end")
        b = make_candidate("candidate.project.end.002", "experience", "experience.project",
                           "experience.endDate", "2025-05", status="conflict",
                           conflict_group_id="conflict.project-end")
        self.write_standard_candidates([a, b])
        with self.assertRaises(cp.ValidationError) as ctx:
            cp.validate_candidates_document(cp.load_json(self.candidates_path))
        self.assertTrue(any("相同值必须合并为单个候选" in e for e in ctx.exception.errors))


class TestApplyDecisions(CareerProfileTestCase):
    def setUp(self):
        super().setUp()
        self.init_db()
        self.write_standard_candidates()

    def test_accept_writes_confirmed_and_revision(self):
        cands = standard_candidates()
        self.write_standard_candidates(cands)
        # 只决定 person.email
        write_decisions_file(self.decisions_path, [
            decision(cands[1], "accept"),  # person.email
        ])
        code = cp.main(["apply-decisions", str(self.candidates_path),
                        str(self.decisions_path), "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        row = self.query("SELECT value, locale, status FROM facts WHERE fact_id=?",
                         ("candidate.person.email.001",))
        self.assertEqual(row, [('"sample@example.com"', None, "confirmed")])
        self.assertEqual(self.query("SELECT value FROM profile_meta WHERE key='profile_revision'"),
                         [("1",)])

    def test_backup_created_before_apply(self):
        cands = standard_candidates()
        write_decisions_file(self.decisions_path, [decision(cands[1], "accept")])
        with sqlite3.connect(self.db_path) as connection:
            before = list(connection.iterdump())
        cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                 "--db", str(self.db_path)])
        backups = list(self.backup_dir_cp.glob("career_profile.*.db"))
        self.assertEqual(len(backups), 1)
        self.assertIn(cp.sha256_file(backups[0]), backups[0].name)
        with sqlite3.connect(backups[0]) as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_reject_and_replace(self):
        cands = standard_candidates()
        write_decisions_file(self.decisions_path, [
            decision(cands[2], "reject"),  # person.phone
        ])
        cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                 "--db", str(self.db_path)])
        self.assertEqual(self.query(
            "SELECT status FROM facts WHERE fact_id='candidate.person.phone.001'"), [("rejected",)])
        write_decisions_file(self.decisions_path, [
            decision(cands[1], "replace", "new-email@example.com"),
        ])
        cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                 "--db", str(self.db_path)])
        row = self.query(
            "SELECT value, status FROM facts WHERE fact_id='candidate.person.email.001'")
        self.assertEqual(row, [('"new-email@example.com"', "confirmed")])

    def test_duplicate_application_rejected_and_rolled_back(self):
        cands = standard_candidates()
        # 第一次：应用 phone（写入）
        write_decisions_file(self.decisions_path, [decision(cands[2], "accept")])
        cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                 "--db", str(self.db_path)])
        # 第二次：先写 email，后写已应用的 phone -> 因 phone 重复应用而回滚
        write_decisions_file(self.decisions_path, [
            decision(cands[1], "accept"),
            decision(cands[2], "accept"),
        ])
        with self.assertRaises(cp.ValidationError) as ctx:
            cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                     "--db", str(self.db_path)])
        self.assertTrue(any("已应用" in e for e in ctx.exception.errors))
        # email 必须不存在（事务回滚），revision 仍为 1
        self.assertEqual(self.query("SELECT COUNT(*) FROM facts WHERE fact_id='candidate.person.email.001'"),
                         [(0,)])
        self.assertEqual(self.query("SELECT value FROM profile_meta WHERE key='profile_revision'"),
                         [("1",)])

    def test_conflict_no_auto_promotion(self):
        """冲突组内未被用户决定的候选不得自动晋升。"""
        a = make_candidate("candidate.project.end.001", "experience", "experience.project",
                           "experience.endDate", "2025-05", status="conflict",
                           conflict_group_id="conflict.project-end",
                           source_refs=[{"sourceId": "source.resume.md", "locator": "L2",
                                         "excerpt": "Example Research Project (2024-05 to 2025-05)"}])
        b = make_candidate("candidate.project.end.002", "experience", "experience.project",
                           "experience.endDate", "2025-04", status="conflict",
                           conflict_group_id="conflict.project-end",
                           source_refs=[{"sourceId": "source.resume.md", "locator": "L3",
                                         "excerpt": "Example Research Project (2024-05 to 2025-04)"}])
        # a/b 为冲突组候选；补 person 与 experience 必填字段供导出
        std = standard_candidates()
        person_name = std[0]
        exp_type, exp_title, exp_start = std[5], std[6], std[7]
        all_cands = [
            a, b, person_name, exp_type, exp_title, exp_start
        ]
        write_candidates_file(self.candidates_path, self.sources, all_cands)
        self.init_db()
        write_decisions_file(self.decisions_path, [
            decision(a, "accept"),
            decision(person_name, "accept"),
            decision(exp_type, "accept"),
            decision(exp_title, "accept"),
            decision(exp_start, "accept"),
        ])
        cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                 "--db", str(self.db_path)])
        rows = self.query(
            "SELECT fact_id, status FROM facts WHERE fact_id LIKE 'candidate.project.%'"
        )
        self.assertEqual(rows, [("candidate.project.end.001", "confirmed")])
        # 导出只含用户接受的 2025-05
        conn = cp.connect(self.db_path)
        try:
            snapshot = cp.build_snapshot(conn)
        finally:
            conn.close()
        self.assertEqual(snapshot["experiences"][0]["endDate"]["value"], "2025-05")

    def test_undecided_candidate_not_confirmed(self):
        cands = standard_candidates()
        write_decisions_file(self.decisions_path, [decision(cands[0], "accept")])
        cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                 "--db", str(self.db_path)])
        facts = self.query("SELECT fact_id FROM facts")
        self.assertEqual(facts, [("candidate.person.name.001",)])

    def test_duplicate_sources_merged(self):
        """同一事实由多个来源支持时应合并 sourceRefs；sources 表不重复。"""
        src_b = {
            "sourceId": "source.resume.b",
            "absolutePath": str(self.resume_dir / "source-cv.md"),
            "documentType": "md",
            "sha256": cp.sha256_file(self.resume_dir / "source-cv.md"),
        }
        refs = [
            {"sourceId": "source.resume.md", "locator": "L1", "excerpt": "Email: sample@example.com"},
            {"sourceId": "source.resume.b", "locator": "L1", "excerpt": "Email: sample@example.com"},
        ]
        cand = make_candidate("candidate.person.email.001", "person", "person.owner",
                              "person.contact.email", "sample@example.com", source_refs=refs)
        write_candidates_file(self.candidates_path, self.sources + [src_b], [cand])
        write_decisions_file(self.decisions_path, [decision(cand, "accept")])
        cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                 "--db", str(self.db_path)])

        cand2 = make_candidate("candidate.person.phone.001", "person", "person.owner",
                               "person.contact.phone", "+86 100-0000-0000", source_refs=refs)
        write_candidates_file(self.candidates_path, self.sources + [src_b], [cand2])
        write_decisions_file(self.decisions_path, [decision(cand2, "accept")])
        cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                 "--db", str(self.db_path)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM sources"), [(2,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM fact_sources"), [(4,)])


class TestSupersede(CareerProfileTestCase):
    """候选带 supersedes：采纳新值时旧事实置为 rejected。"""

    def setUp(self):
        super().setUp()
        self.init_db()
        self.cands = standard_candidates()
        self.write_standard_candidates(self.cands)
        self.apply(self.cands, [(self.cands[-2], "accept", None)])  # skill.level = IELTS 7.5
        self.old = "candidate.skill.level.001"

    def replacement(self, supersedes=None, field="skill.level"):
        c = make_candidate("candidate.skill.level.002", "skill", "skill.language.english", field,
                           "IELTS 7.5 (Academic)", locale="en",
                           source_refs=[{"sourceId": "source.resume.md", "locator": "L1",
                                         "excerpt": "Language: English IELTS 7.5"}])
        c["supersedes"] = supersedes or self.old
        return c

    def statuses(self):
        return dict(self.query("SELECT fact_id, status FROM facts WHERE fact_id LIKE 'candidate.skill.level.%'"))

    def test_accept_replaces_old_fact(self):
        new = self.replacement()
        self.write_standard_candidates([new])
        self.assertEqual(self.apply([new], [(new, "accept", None)]), 0)
        self.assertEqual(self.statuses(), {self.old: "rejected", "candidate.skill.level.002": "confirmed"})

    def test_reject_keeps_old_fact(self):
        new = self.replacement()
        self.write_standard_candidates([new])
        self.apply([new], [(new, "reject", None)])
        self.assertEqual(self.statuses(), {self.old: "confirmed", "candidate.skill.level.002": "rejected"})

    def test_mismatched_field_rolls_back(self):
        new = self.replacement(field="skill.details")
        self.write_standard_candidates([new])
        with self.assertRaises(cp.ValidationError):
            self.apply([new], [(new, "accept", None)])
        self.assertEqual(self.statuses(), {self.old: "confirmed"})


class TestRelocateSources(CareerProfileTestCase):
    """relocate-sources：文件挪位置后同步登记路径。"""

    def setUp(self):
        super().setUp()
        self.init_db()
        cands = standard_candidates()
        self.write_standard_candidates(cands)
        self.apply(cands, [(cands[1], "accept", None)])
        self.old = str(self.resume_dir / "source-cv.md")
        self.new = self.resume_dir / "01-简历" / "source-cv.md"
        self.new.parent.mkdir()
        self.mapping = self.tmp / "moves.json"

    def relocate(self, *extra):
        self.mapping.write_text(json.dumps({"moves": {self.old: str(self.new)}}), encoding="utf-8")
        return cp.main(["relocate-sources", str(self.mapping), "--db", str(self.db_path), *extra])

    def registered_path(self):
        return self.query("SELECT absolute_path FROM sources WHERE source_id='source.resume.md'")[0][0]

    def test_preview_does_not_write(self):
        Path(self.old).rename(self.new)
        self.assertEqual(self.relocate(), 0)
        self.assertEqual(self.registered_path(), self.old)

    def test_apply_updates_path_and_keeps_sha(self):
        sha_before = self.query("SELECT sha256 FROM sources")[0][0]
        Path(self.old).rename(self.new)
        self.assertEqual(self.relocate("--apply"), 0)
        self.assertEqual(self.registered_path(), str(self.new.resolve()))
        self.assertEqual(self.query("SELECT sha256 FROM sources")[0][0], sha_before)

    def test_missing_target_refused(self):
        with self.assertRaises(cp.ValidationError) as ctx:
            self.relocate("--apply")
        self.assertTrue(any("新位置没有文件" in e for e in ctx.exception.errors))
        self.assertEqual(self.registered_path(), self.old)


class TestLinks(CareerProfileTestCase):
    """validate-links / apply-links：给已确认事实补挂新来源。"""

    CERT_TEXT = "Certificate\nOverall Band Score 6.5\n"

    def setUp(self):
        super().setUp()
        self.init_db()
        cands = standard_candidates()
        self.write_standard_candidates(cands)
        self.apply(cands, [(cands[-2], "accept", None), (cands[0], "reject", None)])
        cert = self.resume_dir / "ielts.txt"
        cert.write_text(self.CERT_TEXT, encoding="utf-8")
        self.cert_source = {
            "sourceId": "evidence.ielts",
            "absolutePath": str(cert),
            "documentType": "txt",
            "sha256": cp.sha256_file(cert),
        }
        self.links_path = self.tmp / "links.json"
        self.level_fact = "candidate.skill.level.001"

    def write_links(self, links, sources=None):
        doc = {
            "linkVersion": "1.0.0",
            "generatedAt": "2026-09-26T08:00:00Z",
            "sources": sources or [self.cert_source],
            "links": links,
        }
        self.links_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def link(self, link_id, fact_id, excerpt="Overall Band Score 6.5", source_id="evidence.ielts"):
        return {
            "linkId": link_id,
            "factId": fact_id,
            "sourceRefs": [{"sourceId": source_id, "locator": "p1", "excerpt": excerpt}],
        }

    def validation_errors(self):
        conn = cp.connect(self.db_path)
        try:
            with self.assertRaises(cp.ValidationError) as ctx:
                cp.validate_links_document(cp.load_json(self.links_path), conn)
        finally:
            conn.close()
        return ctx.exception.errors

    def test_accept_adds_source_and_keeps_fact(self):
        self.write_links([self.link("link.level", self.level_fact)])
        write_decisions_file(self.decisions_path,
                             [{"candidateId": "link.level", "action": "accept", "replacementValue": None}])
        before = self.query("SELECT value, status FROM facts WHERE fact_id=?", (self.level_fact,))
        code = cp.main(["apply-links", str(self.links_path), str(self.decisions_path),
                        "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertEqual(
            {r[0] for r in self.query("SELECT source_id FROM fact_sources WHERE fact_id=?",
                                      (self.level_fact,))},
            {"source.resume.md", "evidence.ielts"},
        )
        self.assertEqual(self.query("SELECT value, status FROM facts WHERE fact_id=?",
                                    (self.level_fact,)), before)
        self.assertEqual(self.query("SELECT value FROM profile_meta WHERE key='profile_revision'"),
                         [("2",)])

    def test_rejected_link_not_written(self):
        self.write_links([self.link("link.level", self.level_fact)])
        write_decisions_file(self.decisions_path,
                             [{"candidateId": "link.level", "action": "reject", "replacementValue": None}])
        cp.main(["apply-links", str(self.links_path), str(self.decisions_path), "--db", str(self.db_path)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM fact_sources WHERE source_id='evidence.ielts'"),
                         [(0,)])

    def test_unconfirmed_or_missing_fact_rejected(self):
        self.write_links([
            self.link("link.rejected", "candidate.person.name.001"),
            self.link("link.missing", "candidate.nope.001"),
        ])
        errors = self.validation_errors()
        self.assertTrue(any("只能给已确认事实补挂来源" in e for e in errors))
        self.assertTrue(any("库中不存在事实" in e for e in errors))

    def test_existing_link_rejected(self):
        self.write_links([self.link("link.again", self.level_fact, excerpt="Language: English IELTS 7.5",
                                    source_id="source.resume.md")], sources=self.sources)
        self.assertTrue(any("已经挂有来源" in e for e in self.validation_errors()))

    def test_text_excerpt_must_be_verbatim(self):
        self.write_links([self.link("link.level", self.level_fact, excerpt="Band 7.0")])
        self.assertTrue(any("未在来源文件中找到证据摘录" in e for e in self.validation_errors()))

    def test_registered_source_changed_rejected(self):
        (self.resume_dir / "source-cv.md").write_text(SOURCE_TEXT + "edited\n", encoding="utf-8")
        changed = dict(self.sources[0], sha256=cp.sha256_file(self.resume_dir / "source-cv.md"))
        self.write_links([self.link("link.cv", "candidate.skill.name.001", excerpt="edited",
                                    source_id="source.resume.md")], sources=[changed])
        self.assertTrue(any("请换一个新的 sourceId" in e for e in self.validation_errors()))

    def write_unlinks(self, unlinks, links=None):
        doc = {"linkVersion": "1.0.0", "generatedAt": "2026-09-26T08:00:00Z",
               "sources": [self.cert_source] if links else [], "links": links or [],
               "unlinks": unlinks}
        self.links_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def unlink(self, unlink_id, fact_id, source_id="source.resume.md"):
        return {"unlinkId": unlink_id, "factId": fact_id, "sourceId": source_id, "reason": "挂错了"}

    def test_unlink_removes_wrong_source_when_replacement_added(self):
        self.write_unlinks([self.unlink("unlink.cv", self.level_fact)],
                           links=[self.link("link.level", self.level_fact)])
        write_decisions_file(self.decisions_path, [
            {"candidateId": "link.level", "action": "accept", "replacementValue": None},
            {"candidateId": "unlink.cv", "action": "accept", "replacementValue": None},
        ])
        self.assertEqual(cp.main(["apply-links", str(self.links_path), str(self.decisions_path),
                                  "--db", str(self.db_path)]), 0)
        self.assertEqual(self.query("SELECT source_id FROM fact_sources WHERE fact_id=?",
                                    (self.level_fact,)), [("evidence.ielts",)])

    def test_unlink_last_source_refused(self):
        self.write_unlinks([self.unlink("unlink.cv", self.level_fact)])
        self.assertTrue(any("至少要保留一个来源" in e for e in self.validation_errors()))

    def test_unlink_missing_pair_refused(self):
        self.write_unlinks([self.unlink("unlink.none", self.level_fact, source_id="evidence.ielts")])
        self.assertTrue(any("并没有挂来源" in e for e in self.validation_errors()))

    def test_replace_action_refused(self):
        self.write_links([self.link("link.level", self.level_fact)])
        write_decisions_file(self.decisions_path,
                             [{"candidateId": "link.level", "action": "replace", "replacementValue": "x"}])
        with self.assertRaises(cp.ValidationError):
            cp.main(["apply-links", str(self.links_path), str(self.decisions_path),
                     "--db", str(self.db_path)])


class TestExport(CareerProfileTestCase):
    def setUp(self):
        super().setUp()
        self.init_db()
        self.write_standard_candidates()
        cands = standard_candidates()
        accepted = [
            decision(cands[0], "accept"),  # name
            decision(cands[1], "accept"),  # email
            decision(cands[2], "accept"),  # phone
            decision(cands[3], "accept"),  # institution
            decision(cands[4], "accept"),  # gpa
            decision(cands[5], "accept"),  # experience.type
            decision(cands[6], "accept"),  # experience.title
            decision(cands[7], "accept"),  # startDate
            decision(cands[8], "accept"),  # experience fact narrative
            decision(cands[9], "accept"),  # skill.category
            decision(cands[10], "accept"),  # skill.name
            decision(cands[11], "accept"),  # skill.level
            decision(cands[12], "accept"),  # award.name
        ]
        write_decisions_file(self.decisions_path, accepted)
        self.assertEqual(cp.main(
            ["apply-decisions", str(self.candidates_path), str(self.decisions_path),
             "--db", str(self.db_path)]), 0)

    def test_export_only_confirmed(self):
        # 追加一个会被拒绝的事实（setUp 已应用标准候选）
        rej = make_candidate("candidate.person.loc.001", "person", "person.owner",
                             "person.location.currentCity", "Example City", locale="zh-CN")
        write_candidates_file(self.candidates_path, self.sources, [rej])
        write_decisions_file(self.decisions_path, [decision(rej, "reject")])
        cp.main(["apply-decisions", str(self.candidates_path), str(self.decisions_path),
                 "--db", str(self.db_path)])
        conn = cp.connect(self.db_path)
        try:
            snapshot = cp.build_snapshot(conn)
        finally:
            conn.close()
        self.assertEqual(snapshot["person"]["location"], {})
        self.assertEqual(snapshot["profileRevision"], 2)
        self.assertEqual(snapshot["person"]["contact"]["email"]["value"], "sample@example.com")

    def test_export_full_cli_flow_and_validate(self):
        code = cp.main(["export", str(self.output_path), "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertTrue(self.output_path.exists())
        code2 = cp.main(["validate-export", str(self.output_path)])
        self.assertEqual(code2, 0)
        conn = cp.connect(self.db_path)
        try:
            self.assertIsNotNone(cp.get_profile_revision(conn))
        finally:
            conn.close()


class TestValidateExport(CareerProfileTestCase):
    """validate-export 的契约校验：用合成数据现场导出一份快照再逐项篡改。"""

    def setUp(self):
        super().setUp()
        self.init_db()
        cands = standard_candidates()
        self.write_standard_candidates(cands)
        write_decisions_file(self.decisions_path, [decision(c, "accept") for c in cands])
        self.assertEqual(cp.main(["apply-decisions", str(self.candidates_path),
                                  str(self.decisions_path), "--db", str(self.db_path)]), 0)
        self.assertEqual(cp.main([
            "add-expression", "--id", "expression.intro.sample", "--purpose", "self-intro",
            "--locale", "en", "--text", "Sample introduction.", "--target-roles", "analyst",
            "--source-fact-ids", "candidate.experience.fact.001",
            "--subject-ids", "experience.project", "--db", str(self.db_path)]), 0)
        self.assertEqual(cp.main(["export", str(self.output_path), "--db", str(self.db_path)]), 0)

    def snapshot(self):
        return cp.load_json(self.output_path)

    def test_generated_snapshot_passes(self):
        self.assertEqual(cp.validate_snapshot(self.snapshot()), [])

    def test_missing_person_rejected(self):
        data = self.snapshot()
        del data["person"]
        errors = cp.validate_snapshot(data)
        self.assertTrue(any("person" in e for e in errors))

    def test_duplicate_fact_id_rejected(self):
        data = self.snapshot()
        data["education"][0]["institution"]["factId"] = data["person"]["contact"]["email"]["factId"]
        errors = cp.validate_snapshot(data)
        self.assertTrue(any("重复 factId" in e for e in errors))

    def test_bad_expression_fact_reference_rejected(self):
        data = self.snapshot()
        data["expressions"][0]["sourceFactIds"] = ["fact.does.not.exist"]
        errors = cp.validate_snapshot(data)
        self.assertTrue(any("引用不存在的事实" in e for e in errors))

    def test_bad_expression_subject_reference_rejected(self):
        data = self.snapshot()
        data["expressions"][0]["subjectIds"] = ["experience.not.exist"]
        errors = cp.validate_snapshot(data)
        self.assertTrue(any("引用不存在的实体" in e for e in errors))

    def test_forbidden_keys_rejected(self):
        data = self.snapshot()
        data["experiences"][0]["status"] = "confirmed"
        errors = cp.validate_snapshot(data)
        self.assertTrue(any("禁止包含 status" in e for e in errors))
        data2 = self.snapshot()
        data2["person"]["contact"]["email"]["absolutePath"] = "/secret/source.pdf"
        errors2 = cp.validate_snapshot(data2)
        self.assertTrue(any("禁止包含 absolutePath" in e for e in errors2))

    def test_invalid_locale_rejected(self):
        data = self.snapshot()
        data["person"]["fullName"]["en"]["locale"] = "english"
        errors = cp.validate_snapshot(data)
        self.assertTrue(any("非法 locale" in e for e in errors))

    def test_empty_expression_list_allowed(self):
        data = self.snapshot()
        data["expressions"] = []
        self.assertEqual(cp.validate_snapshot(data), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
