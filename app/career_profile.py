#!/usr/bin/env python3
"""Career Profile SQLite 数据库与 Snapshot 导出工具（阶段一）。

职责：init / validate-candidates / apply-decisions / add-expression /
list-expressions / export / validate-export。
只使用 Python 标准库。候选事实必须经过用户确认（decisions.json）才能写入
正式库，导出只包含 status=confirmed 的事实。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROFILE_DB_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROFILE_DB_DIR / "data" / "career_profile.sqlite3"
DEFAULT_EXPORT_PATH = PROFILE_DB_DIR / "exports" / "career-profile.snapshot.json"
BACKUP_DIR = PROFILE_DB_DIR / "backups"


def _allowed_source_dirs() -> list[Path]:
    """唯一允许作为事实来源的资料目录（硬校验）。

    优先级：环境变量 PERSONAL_DB_MATERIALS（os.pathsep 分隔多个目录）
    > 同目录 config.json 里的 {"materials": [...]}
    > 默认项目内的 materials/ 目录。

    这样把这套代码复制到任何机器都能用：用户只要把原始材料
    （简历、成绩单、获奖证明、口述补充……）放进 materials/ 即可。
    """
    env = os.environ.get("PERSONAL_DB_MATERIALS")
    if env:
        return [Path(p).expanduser().resolve() for p in env.split(os.pathsep) if p.strip()]
    cfg = PROFILE_DB_DIR / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            mats = data.get("materials")
            if isinstance(mats, list) and mats:
                return [Path(p).expanduser().resolve() for p in mats if str(p).strip()]
        except (ValueError, OSError):
            pass
    return [(PROFILE_DB_DIR / "materials").resolve()]


ALLOWED_SOURCE_DIRS = _allowed_source_dirs()

CANDIDATE_VERSION = "1.0.0"
DECISION_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"
DB_SCHEMA_VERSION = 2

STATUSES = ("pending", "confirmed", "conflict", "rejected")
ACTIONS = ("accept", "reject", "replace")
ENTITY_TYPES = ("person", "education", "experience", "skill", "award")
EXPERIENCE_TYPES = ("professional", "research", "leadership", "project")
SKILL_CATEGORIES = ("language", "technical", "research", "qualification", "other")
# 文本类来源（可做证据摘录包含性检查）；Word/PDF 程序不解析。
TEXT_DOCUMENT_TYPES = ("md", "html", "txt")

STABLE_ID_RE = re.compile(r"^[a-z][a-z0-9._-]+$")
LOCALE_RE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FACT_SLUG_RE = re.compile(r"^experience\.fact\.[a-z0-9._-]+$")

# 导出时禁止出现在 Snapshot 中的键（来源信息、状态、候选元数据）。
FORBIDDEN_EXPORT_KEYS = frozenset(
    {
        "status",
        "locator",
        "excerpt",
        "absolutePath",
        "sourcePath",
        "sourceId",
        "documentType",
        "sha256",
        "registeredAt",
        "candidateId",
        "entityType",
        "conflictGroupId",
        "proposedValue",
    }
)

# field_path 白名单（与共享契约字段一一对应）。
FIELD_PATHS = frozenset(
    {
        # person
        "person.fullName.zhCN",
        "person.fullName.en",
        "person.fullName.givenNameEn",
        "person.fullName.familyNameEn",
        "person.contact.email",
        "person.contact.phone",
        "person.location.currentCity",
        "person.location.currentCountry",
        # education
        "education.institution",
        "education.degree",
        "education.major",
        "education.minor",
        "education.location",
        "education.startDate",
        "education.endDate",
        "education.gpa",
        "education.averageScore",
        "education.ranking",
        # experience（experience.fact.* 由前缀正则另行匹配）
        "experience.type",
        "experience.title",
        "experience.organization",
        "experience.role",
        "experience.location",
        "experience.startDate",
        "experience.endDate",
        # skill
        "skill.category",
        "skill.name",
        "skill.level",
        "skill.details",
        # award
        "award.name",
        "award.issuer",
        "award.date",
        "award.details",
    }
)

# 将 field_path 映射到导出记录中的键名（experience.fact.* → "facts" 叙述数组）。
FIELD_PATH_TO_EXPORT_KEY = {
    "education.institution": "institution",
    "education.degree": "degree",
    "education.major": "major",
    "education.minor": "minor",
    "education.location": "location",
    "education.startDate": "startDate",
    "education.endDate": "endDate",
    "education.gpa": "gpa",
    "education.averageScore": "averageScore",
    "education.ranking": "ranking",
    "experience.title": "title",
    "experience.organization": "organization",
    "experience.role": "role",
    "experience.location": "location",
    "experience.startDate": "startDate",
    "experience.endDate": "endDate",
    "skill.name": "name",
    "skill.level": "level",
    "skill.details": "details",
    "award.name": "name",
    "award.issuer": "issuer",
    "award.date": "date",
    "award.details": "details",
}


class ValidationError(Exception):
    """结构化校验失败（携带可读错误列表）。"""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_json(path: Path) -> object:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_text_document(document_type: str) -> bool:
    return document_type in TEXT_DOCUMENT_TYPES


def read_document_text(path: Path, document_type: str) -> str:
    """读取文本类来源的纯文本（供证据摘录包含性检查）。

    HTML 剥离脚本、样式和标签；其余按 UTF-8 读取。
    """
    raw = path.read_bytes().decode("utf-8", errors="replace")
    if document_type == "html":
        raw = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S | re.I)
        raw = re.sub(r"<[^>]+>", "\n", raw)
        raw = re.sub(r"&amp;", "&", raw)
        raw = re.sub(r"&lt;", "<", raw)
        raw = re.sub(r"&gt;", ">", raw)
        raw = re.sub(r"&quot;|&#0*34;", '"', raw)
        raw = re.sub(r"&#39;|&apos;", "'", raw)
        raw = re.sub(r"&nbsp;", " ", raw)
        raw = re.sub(r"\s+", " ", raw)
    return raw


def split_sql_statements(script: str) -> list[str]:
    """把 SQL 脚本拆成单条语句（本项目的 DDL 不含引号内分号）。"""
    return [part.strip() for part in script.split(";") if part.strip()]


# ---------------------------------------------------------------------------
# 数据库初始化与迁移
# ---------------------------------------------------------------------------

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS profile_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        absolute_path TEXT NOT NULL,
        document_type TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        registered_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entities (
        entity_id TEXT PRIMARY KEY,
        entity_type TEXT NOT NULL,
        display_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS facts (
        fact_id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL REFERENCES entities(entity_id),
        field_path TEXT NOT NULL,
        value TEXT NOT NULL,
        locale TEXT,
        status TEXT NOT NULL
            CHECK (status IN ('pending','confirmed','conflict','rejected')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS facts_confirmed_unique ON facts (
        entity_id, field_path, locale
    ) WHERE status = 'confirmed';
    CREATE TABLE IF NOT EXISTS fact_sources (
        fact_id TEXT NOT NULL REFERENCES facts(fact_id),
        source_id TEXT NOT NULL REFERENCES sources(source_id),
        locator TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        PRIMARY KEY (fact_id, source_id)
    );
    CREATE TABLE IF NOT EXISTS expressions (
        expression_id TEXT PRIMARY KEY,
        purpose TEXT NOT NULL,
        locale TEXT NOT NULL,
        max_chars INTEGER,
        target_roles TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('pending','confirmed','conflict','rejected')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS expression_sources (
        expression_id TEXT NOT NULL REFERENCES expressions(expression_id),
        fact_id TEXT NOT NULL REFERENCES facts(fact_id),
        PRIMARY KEY (expression_id, fact_id)
    );
    """,
    2: """
    CREATE TABLE IF NOT EXISTS expression_subjects (
        expression_id TEXT NOT NULL REFERENCES expressions(expression_id),
        entity_id TEXT NOT NULL REFERENCES entities(entity_id),
        PRIMARY KEY (expression_id, entity_id)
    );
    """,
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup_db(db_path: Path) -> Path | None:
    """复制现有数据库到 backups/，备份名含 UTC 时间与原库 SHA-256。"""
    if not db_path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    digest = sha256_file(db_path)
    ts = utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_DIR / f"career_profile.{ts}.{digest}.db"
    shutil.copy2(db_path, target)
    return target


def current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> None:
    """在单事务内把数据库迁移到最新版本，任一步失败整体回滚。

    sqlite3 的 executescript 会隐式提交进行中的事务，因此这里把脚本拆成
    单条语句逐条执行，保证迁移原子性。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL,"
        " sha256 TEXT NOT NULL)"
    )
    conn.execute("BEGIN")
    try:
        current = current_schema_version(conn)
        for version in range(current + 1, DB_SCHEMA_VERSION + 1):
            sql = MIGRATIONS[version]
            for statement in split_sql_statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, sha256)"
                " VALUES (?, ?, ?)",
                (version, utcnow_iso(), sha256_bytes(sql.encode("utf-8"))),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def needs_migration(db_path: Path) -> bool:
    """数据库不存在、表缺失或版本落后时返回 True（迁移前需要备份）。"""
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='schema_migrations'"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return True
    conn = connect(db_path)
    try:
        return current_schema_version(conn) < DB_SCHEMA_VERSION
    finally:
        conn.close()


def ensure_meta_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO profile_meta (key, value)"
        " VALUES ('profile_revision', '0')"
    )


def get_profile_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM profile_meta WHERE key = 'profile_revision'"
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# 候选文件校验
# ---------------------------------------------------------------------------

def _check(cond: bool, errors: list[str], message: str) -> bool:
    if not cond:
        errors.append(message)
    return cond


def validate_stable_id(value: object, errors: list[str], path: str) -> bool:
    if not isinstance(value, str) or not STABLE_ID_RE.match(value):
        errors.append(f"{path}: 非法 stableId（需匹配 ^[a-z][a-z0-9._-]+$）：{value!r}")
        return False
    return True


def validate_locale(value: object, errors: list[str], path: str) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not LOCALE_RE.match(value):
        errors.append(f"{path}: 非法 locale：{value!r}")
        return False
    return True


def validate_candidates_document(data: object) -> dict:
    """校验候选文件结构（含来源路径、SHA-256、证据摘录、冲突分组）。

    返回规范化后的候选文档；失败抛出 ValidationError。
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ValidationError(["候选文件根节点必须是 JSON 对象"])
    if data.get("candidateVersion") != CANDIDATE_VERSION:
        errors.append(f"candidateVersion 必须是 {CANDIDATE_VERSION}")
    if not isinstance(data.get("generatedAt"), str) or not data["generatedAt"]:
        errors.append("generatedAt 必须是非空字符串（ISO 8601）")

    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources 必须是非空数组")
    source_ids: set[str] = set()
    source_map: dict[str, dict] = {}
    for idx, src in enumerate(sources if isinstance(sources, list) else []):
        sp = f"sources[{idx}]"
        if not isinstance(src, dict):
            errors.append(f"{sp}: 必须是对象")
            continue
        sid = src.get("sourceId")
        if isinstance(sid, str) and STABLE_ID_RE.match(sid):
            if sid in source_ids:
                errors.append(f"{sp}.sourceId: 重复的 sourceId {sid}")
            else:
                source_ids.add(sid)
        else:
            errors.append(f"{sp}.sourceId: 非法 stableId：{sid!r}")
        abs_path = src.get("absolutePath")
        doc_type = src.get("documentType")
        declared_sha = src.get("sha256")
        source_map.setdefault(sid, {})["absolutePath"] = abs_path
        source_map.setdefault(sid, {})["documentType"] = doc_type
        if not isinstance(abs_path, str) or not abs_path:
            errors.append(f"{sp}.absolutePath: 必须是非空绝对路径")
            continue
        path_obj = Path(abs_path)
        if not path_obj.is_absolute():
            errors.append(f"{sp}.absolutePath: 不是绝对路径：{abs_path}")
            continue
        if not isinstance(doc_type, str) or not doc_type:
            errors.append(f"{sp}.documentType: 必须是非空字符串")
        if not isinstance(declared_sha, str) or not SHA256_RE.match(declared_sha):
            errors.append(f"{sp}.sha256: 必须是 64 位小写十六进制")
        real = path_obj.resolve()
        if not any(real.is_relative_to(d) for d in ALLOWED_SOURCE_DIRS):
            errors.append(f"{sp}.absolutePath: 不在允许读取目录内：{abs_path}")
        elif not real.exists():
            errors.append(f"{sp}.absolutePath: 文件不存在：{abs_path}")
        else:
            actual = sha256_file(real)
            if declared_sha != actual:
                errors.append(
                    f"{sp}.sha256: 与文件实际 SHA-256 不一致（声明 {declared_sha}，"
                    f"实际 {actual}）"
                )
            source_map.setdefault(sid, {})["realPath"] = real

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        errors.append("candidates 必须是非空数组")
    candidate_ids: set[str] = set()
    locator_cache: dict[str, str] = {}
    # (entityKey, fieldPath, locale) -> 候选列表
    groups: dict[tuple, list[dict]] = {}
    grouped_by_conflict: dict[str, list] = {}

    for idx, cand in enumerate(candidates if isinstance(candidates, list) else []):
        cp = f"candidates[{idx}]"
        if not isinstance(cand, dict):
            errors.append(f"{cp}: 必须是对象")
            continue
        cid = cand.get("candidateId")
        if isinstance(cid, str) and STABLE_ID_RE.match(cid):
            if cid in candidate_ids:
                errors.append(f"{cp}.candidateId: 重复的候选 ID {cid}")
            else:
                candidate_ids.add(cid)
        else:
            errors.append(f"{cp}.candidateId: 非法 stableId：{cid!r}")
        entity_type = cand.get("entityType")
        entity_key = cand.get("entityKey")
        field_path = cand.get("fieldPath")
        locale = cand.get("locale")
        value = cand.get("proposedValue")
        status = cand.get("status")
        cg = cand.get("conflictGroupId")
        refs = cand.get("sourceRefs")

        if entity_type not in ENTITY_TYPES:
            errors.append(f"{cp}.entityType: 必须是 {ENTITY_TYPES} 之一，得到 {entity_type!r}")
        if not validate_stable_id(entity_key, errors, f"{cp}.entityKey"):
            entity_key = None
        if field_path not in FIELD_PATHS and not (
            isinstance(field_path, str) and FACT_SLUG_RE.match(field_path)
        ):
            errors.append(f"{cp}.fieldPath: 不在白名单内：{field_path!r}")
        if not validate_locale(locale, errors, f"{cp}.locale"):
            locale = None
        if not isinstance(value, str) or not value:
            errors.append(f"{cp}.proposedValue: 必须是非空字符串")
        elif len(value) > 4000:
            errors.append(f"{cp}.proposedValue: 超过 4000 字符")
        if status not in ("pending", "conflict"):
            errors.append(f"{cp}.status: 候选文件只允许 pending 或 conflict，得到 {status!r}")
        if cg is not None:
            validate_stable_id(cg, errors, f"{cp}.conflictGroupId")
        if not isinstance(refs, list) or not refs:
            errors.append(f"{cp}.sourceRefs: 必须是非空数组")
        else:
            seen_ref = set()
            for ridx, ref in enumerate(refs):
                rp = f"{cp}.sourceRefs[{ridx}]"
                if not isinstance(ref, dict):
                    errors.append(f"{rp}: 必须是对象")
                    continue
                sid = ref.get("sourceId")
                if not isinstance(sid, str) or sid not in source_ids:
                    errors.append(f"{rp}.sourceId: 未定义的来源 {sid!r}")
                locator = ref.get("locator")
                excerpt = ref.get("excerpt")
                if not isinstance(locator, str) or not locator:
                    errors.append(f"{rp}.locator: 必须是非空字符串")
                if not isinstance(excerpt, str) or not excerpt:
                    errors.append(f"{rp}.excerpt: 必须是非空字符串")
                if isinstance(sid, str) and sid in source_ids:
                    if sid in seen_ref:
                        errors.append(f"{rp}.sourceId: 同一候选重复引用来源 {sid}")
                    seen_ref.add(sid)
                    meta = source_map.get(sid, {})
                    real = meta.get("realPath")
                    doc_type = meta.get("documentType")
                    if real is not None and is_text_document(doc_type or ""):
                        text = locator_cache.get(str(real))
                        if text is None:
                            text = read_document_text(real, doc_type)
                            locator_cache[str(real)] = text
                        if isinstance(excerpt, str) and excerpt and excerpt not in text:
                            errors.append(
                                f"{rp}.excerpt: 未在来源文件中找到证据摘录"
                                f"（{doc_type}：{real.name}）"
                            )
        # 分组统计（供冲突规则校验）
        if entity_key is not None and isinstance(field_path, str) and (
            field_path in FIELD_PATHS or FACT_SLUG_RE.match(field_path)
        ):
            group_key = (entity_key, field_path, locale)
            groups.setdefault(group_key, []).append(cand)
            if cg is not None:
                grouped_by_conflict.setdefault(cg, []).append(cand)

    # 冲突分组一致性
    for group_key, members in groups.items():
        values = {m.get("proposedValue") for m in members}
        same_group_ids = {m.get("conflictGroupId") for m in members}
        if len(members) > 1 and len(values) == 1:
            errors.append(
                f"冲突规则: 同一 (entityKey, fieldPath, locale) 的相同值必须"
                f"合并为单个候选：{group_key}"
            )
        if len(members) > 1 and len(values) > 1:
            if not all(
                m.get("status") == "conflict" and m.get("conflictGroupId")
                for m in members
            ):
                errors.append(
                    f"冲突规则: 同一 (entityKey, fieldPath, locale) 的不同值必须共享"
                    f"同一非空 conflictGroupId 且 status=conflict：{group_key}"
                )
            if len(same_group_ids) != 1:
                errors.append(
                    f"冲突规则: 冲突组 ID 不一致：{group_key} -> {same_group_ids}"
                )
        if len(members) == 1 and members[0].get("status") == "conflict":
            errors.append(
                f"冲突规则: status=conflict 且未分组（冲突组至少需 2 个候选）：{group_key}"
            )
    for cg, members in grouped_by_conflict.items():
        keys = {(m.get("entityKey"), m.get("fieldPath"), m.get("locale")) for m in members}
        if len(keys) > 1:
            errors.append(
                f"conflictGroupId {cg}: 冲突组内候选必须属于同一事实"
                "（entityKey/fieldPath/locale 相同）"
            )
        if len(members) < 2:
            errors.append(f"conflictGroupId {cg}: 冲突组至少需要 2 个候选")

    if errors:
        raise ValidationError(errors)
    return data


# ---------------------------------------------------------------------------
# 决定文件校验
# ---------------------------------------------------------------------------

def validate_decisions_document(data: object, candidates: list[dict]) -> list[dict]:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ValidationError(["决定文件根节点必须是 JSON 对象"])
    if data.get("decisionVersion") != DECISION_VERSION:
        errors.append(f"decisionVersion 必须是 {DECISION_VERSION}")
    if not isinstance(data.get("decidedBy"), str) or not data["decidedBy"]:
        errors.append("decidedBy 必须是非空字符串")
    if not isinstance(data.get("decidedAt"), str) or not data["decidedAt"]:
        errors.append("decidedAt 必须是非空字符串（ISO 8601）")
    decisions = data.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        errors.append("decisions 必须是非空数组")

    candidate_map = {c["candidateId"]: c for c in candidates}
    seen: set[str] = set()
    out: list[dict] = []
    for idx, dec in enumerate(decisions if isinstance(decisions, list) else []):
        dp = f"decisions[{idx}]"
        if not isinstance(dec, dict):
            errors.append(f"{dp}: 必须是对象")
            continue
        cid = dec.get("candidateId")
        action = dec.get("action")
        replacement = dec.get("replacementValue")
        if not isinstance(cid, str) or cid not in candidate_map:
            errors.append(
                f"{dp}.candidateId: 候选文件中不存在 {cid!r}（决定文件不得放入未处理的候选）"
            )
        elif cid in seen:
            errors.append(f"{dp}.candidateId: 重复决定 {cid}")
        else:
            seen.add(cid)
        if action not in ACTIONS:
            errors.append(f"{dp}.action: 必须是 {ACTIONS} 之一，得到 {action!r}")
        if action == "replace":
            if not isinstance(replacement, str) or not replacement:
                errors.append(f"{dp}.replacementValue: replace 必须提供非空字符串")
            elif len(replacement) > 4000:
                errors.append(f"{dp}.replacementValue: 超过 4000 字符")
        if cid in candidate_map:
            out.append(
                {
                    "candidateId": cid,
                    "action": action,
                    "replacementValue": replacement if isinstance(replacement, str) else None,
                    "candidate": candidate_map[cid],
                }
            )
    if errors:
        raise ValidationError(errors)
    return out


# ---------------------------------------------------------------------------
# 实体显示名
# ---------------------------------------------------------------------------

ALIAS_FIELD_PATHS = {
    "person.fullName.zhCN",
    "person.fullName.en",
    "education.institution",
    "experience.title",
    "skill.name",
    "award.name",
}


def entity_display_name(candidates: list[dict]) -> str:
    for cand in candidates:
        if cand.get("fieldPath") in ALIAS_FIELD_PATHS:
            return str(cand.get("proposedValue"))[:80]
    return str(candidates[0]["entityKey"])


# ---------------------------------------------------------------------------
# apply-decisions
# ---------------------------------------------------------------------------

def cmd_apply_decisions(candidates_path: Path, decisions_path: Path, db_path: Path) -> int:
    cand_data = load_json(candidates_path)
    validate_candidates_document(cand_data)  # 先整体校验，失败即退出
    dec_data = load_json(decisions_path)
    decisions = validate_decisions_document(dec_data, cand_data["candidates"])

    candidates_by_entity: dict[str, list[dict]] = {}
    for cand in cand_data["candidates"]:
        candidates_by_entity.setdefault(cand["entityKey"], []).append(cand)

    backup = backup_db(db_path)
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        for dec in decisions:
            cand = dec["candidate"]
            fact_id = cand["candidateId"]
            existing = conn.execute(
                "SELECT 1 FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if existing:
                raise ValidationError([f"{fact_id}: 该候选已应用（重复应用被拒绝）"])
            action = dec["action"]
            status = "confirmed" if action in ("accept", "replace") else "rejected"
            value = (
                dec["replacementValue"]
                if action == "replace" and dec["replacementValue"] is not None
                else cand["proposedValue"]
            )
            entity_key = cand["entityKey"]
            now = utcnow_iso()
            conn.execute(
                "INSERT INTO entities (entity_id, entity_type, display_name,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(entity_id) DO UPDATE SET updated_at = excluded.updated_at",
                (
                    entity_key,
                    cand["entityType"],
                    entity_display_name(candidates_by_entity.get(entity_key, [])),
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO facts (fact_id, entity_id, field_path, value, locale,"
                " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fact_id,
                    entity_key,
                    cand["fieldPath"],
                    json.dumps(value, ensure_ascii=False),
                    cand.get("locale"),
                    status,
                    now,
                    now,
                ),
            )
            for ref in cand["sourceRefs"]:
                src = next(
                    s for s in cand_data["sources"] if s["sourceId"] == ref["sourceId"]
                )
                conn.execute(
                    "INSERT OR IGNORE INTO sources (source_id, absolute_path,"
                    " document_type, sha256, registered_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        ref["sourceId"],
                        src["absolutePath"],
                        src["documentType"],
                        src["sha256"],
                        now,
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO fact_sources (fact_id, source_id,"
                    " locator, excerpt) VALUES (?, ?, ?, ?)",
                    (fact_id, ref["sourceId"], ref["locator"], ref["excerpt"]),
                )
        new_rev = get_profile_revision(conn) + 1
        conn.execute(
            "INSERT INTO profile_meta (key, value) VALUES ('profile_revision', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(new_rev),),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise
    finally:
        conn.close()

    backup_note = f"，备份：{backup.name}" if backup else "（首次应用，无既有库可备份）"
    print(f"已应用 {len(decisions)} 个决定；profile_revision = {new_rev}{backup_note}")
    return 0


# ---------------------------------------------------------------------------
# add-expression / list-expressions（主观表达沉淀入口）
# ---------------------------------------------------------------------------

def _split_ids(raw: str) -> list[str]:
    """把逗号分隔的 ID 串切成去重且保序的列表。"""
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def cmd_add_expression(
    *,
    db_path: Path,
    expression_id: str,
    purpose: str,
    locale: str,
    text: str,
    source_fact_ids: list[str],
    target_roles: list[str],
    max_chars: int | None,
    subject_ids: list[str],
    status: str,
    export_path: Path | None,
) -> int:
    """写入或更新一条主观表达（迭代式沉淀）。

    与项目其余写库路径一致：写库前自动备份，全程单事务，失败整体回滚，
    成功后递增 profile_revision。引用的事实必须是 confirmed，否则导出
    会因“引用不存在”而校验失败——这是本函数提前拦截的核心错误。
    """
    errors: list[str] = []
    if not STABLE_ID_RE.match(expression_id):
        errors.append("--id: 必须匹配 ^[a-z][a-z0-9._-]+$ 且至少 3 字符")
    if not (1 <= len(purpose) <= 120):
        errors.append("--purpose: 必须是非空字符串且 ≤120 字符")
    if not LOCALE_RE.match(locale):
        errors.append("--locale: 必须匹配 ^[a-z]{2,3}(-[A-Z]{2})?$")
    if not (1 <= len(text) <= 8000):
        errors.append("正文: 必须是非空字符串且 ≤8000 字符")
    if max_chars is not None and max_chars < 1:
        errors.append("--max-chars: 必须是 ≥1 的整数")
    if not target_roles:
        errors.append("--target-roles: 至少需要一个目标岗位")
    for role in target_roles:
        if not (1 <= len(role) <= 80):
            errors.append(f"--target-roles 项 {role!r}: 必须是 1–80 字符")
    if not source_fact_ids:
        errors.append("--source-fact-ids: 至少引用一个已确认事实")
    if status not in STATUSES:
        errors.append(f"--status: 只能是 {'/'.join(STATUSES)}")
    if errors:
        raise ValidationError(errors)

    if not db_path.exists():
        raise ValidationError([f"数据库不存在：{db_path}（请先运行 init）"])

    backup = backup_db(db_path)
    conn = connect(db_path)
    try:
        migrate(conn)
        ensure_meta_row(conn)
        conn.commit()

        placeholders = ",".join("?" for _ in source_fact_ids)
        confirmed = {
            r[0]
            for r in conn.execute(
                "SELECT fact_id FROM facts"
                f" WHERE status='confirmed' AND fact_id IN ({placeholders})",
                tuple(source_fact_ids),
            ).fetchall()
        }
        missing = [fid for fid in source_fact_ids if fid not in confirmed]
        if missing:
            raise ValidationError(
                [
                    "以下 source-fact-ids 不存在或尚未 confirmed"
                    "（导出会因引用不存在而失败）：" + ", ".join(missing)
                ]
            )

        if subject_ids:
            entity_ph = ",".join("?" for _ in subject_ids)
            known_entities = {
                r[0]
                for r in conn.execute(
                    "SELECT entity_id FROM entities"
                    f" WHERE entity_id IN ({entity_ph})",
                    tuple(subject_ids),
                ).fetchall()
            }
            missing_entities = [e for e in subject_ids if e not in known_entities]
            if missing_entities:
                raise ValidationError(
                    ["以下 subject-ids 不是已知实体：" + ", ".join(missing_entities)]
                )

        existing = conn.execute(
            "SELECT 1 FROM expressions WHERE expression_id = ?", (expression_id,)
        ).fetchone()
        action = "更新" if existing else "新建"

        conn.execute("BEGIN")
        now = utcnow_iso()
        conn.execute(
            "INSERT INTO expressions (expression_id, purpose, locale, max_chars,"
            " target_roles, text, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(expression_id) DO UPDATE SET"
            " purpose = excluded.purpose, locale = excluded.locale,"
            " max_chars = excluded.max_chars, target_roles = excluded.target_roles,"
            " text = excluded.text, status = excluded.status,"
            " updated_at = excluded.updated_at",
            (
                expression_id,
                purpose,
                locale,
                max_chars,
                json.dumps(target_roles, ensure_ascii=False),
                text,
                status,
                now,
                now,
            ),
        )
        conn.execute(
            "DELETE FROM expression_sources WHERE expression_id = ?",
            (expression_id,),
        )
        for fact_id in source_fact_ids:
            conn.execute(
                "INSERT INTO expression_sources (expression_id, fact_id)"
                " VALUES (?, ?)",
                (expression_id, fact_id),
            )
        conn.execute(
            "DELETE FROM expression_subjects WHERE expression_id = ?",
            (expression_id,),
        )
        for entity_id in subject_ids:
            conn.execute(
                "INSERT INTO expression_subjects (expression_id, entity_id)"
                " VALUES (?, ?)",
                (expression_id, entity_id),
            )
        new_rev = get_profile_revision(conn) + 1
        conn.execute(
            "INSERT INTO profile_meta (key, value) VALUES ('profile_revision', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(new_rev),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    backup_note = f"，备份：{backup.name}" if backup else ""
    print(
        f"已{action} expression {expression_id}"
        f"（status={status}，引用 {len(source_fact_ids)} 个事实）；"
        f"profile_revision = {new_rev}{backup_note}"
    )
    if export_path is not None:
        cmd_export(db_path, export_path)
    return 0


def cmd_list_expressions(db_path: Path) -> int:
    """列出已沉淀的主观表达，便于复用与避免重复撰写。"""
    if not db_path.exists():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 1
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT expression_id, purpose, locale, max_chars, target_roles,"
            " status, length(text) FROM expressions ORDER BY expression_id"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("尚无任何 expression（主观表达库为空）")
        return 0
    print(f"共 {len(rows)} 条 expression：")
    for eid, purpose, loc, mc, roles_json, status, text_len in rows:
        try:
            roles = "、".join(json.loads(roles_json))
        except Exception:
            roles = str(roles_json)
        limit = f"{mc} 字" if mc else "不限字数"
        print(
            f"  - {eid}  [{status}]  purpose={purpose}  {loc}  "
            f"上限{limit}  正文 {text_len} 字"
        )
        print(f"      目标岗位：{roles}")
    return 0


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _fact_value_record(fact: dict, fact_ids: dict) -> dict:
    record = {"factId": fact["fact_id"], "value": json.loads(fact["value"])}
    if fact["locale"]:
        record["locale"] = fact["locale"]
    fact_ids[fact["fact_id"]] = True
    return record


def build_snapshot(conn: sqlite3.Connection) -> dict:
    """把库中 confirmed 事实组装为契约 Snapshot。失败抛出 ValidationError。"""
    errors: list[str] = []
    rows = conn.execute(
        "SELECT e.entity_id, e.entity_type, f.fact_id, f.field_path, f.value, f.locale"
        " FROM facts f JOIN entities e ON e.entity_id = f.entity_id"
        " WHERE f.status = 'confirmed'"
        " ORDER BY e.entity_id"
    ).fetchall()

    by_entity: dict[str, dict] = {}
    fact_ids: dict[str, bool] = {}
    for entity_id, entity_type, fact_id, field_path, raw_value, locale in rows:
        by_entity.setdefault(entity_id, {"entity_type": entity_type, "facts": []})
        by_entity[entity_id]["facts"].append(
            {
                "fact_id": fact_id,
                "field_path": field_path,
                "value": raw_value,
                "locale": locale,
            }
        )

    person: dict | None = None
    education: list = []
    experiences: list = []
    skills: list = []
    awards: list = []

    for entity_id, bag in by_entity.items():
        etype = bag["entity_type"]
        facts = bag["facts"]
        if etype == "person" and person is None:
            person = {"id": entity_id, "facts": facts}
        elif etype == "person":
            errors.append(f"存在多个 person 实体：{entity_id}")
        elif etype == "education":
            education.append((entity_id, facts))
        elif etype == "experience":
            experiences.append((entity_id, facts))
        elif etype == "skill":
            skills.append((entity_id, facts))
        elif etype == "award":
            awards.append((entity_id, facts))
        else:
            errors.append(f"未知实体类型 {etype}：{entity_id}")

    # person 对象
    if person is None:
        errors.append("person 实体不存在（至少需要 person.owner）")
        snapshot_person = None
    else:
        snapshot_person = {"id": person["id"]}
        full_name: dict = {}
        contact: dict = {}
        location: dict = {}
        for fact in person["facts"]:
            fp = fact["field_path"]
            if fp.startswith("person.fullName."):
                full_name[fp.split(".")[-1]] = _fact_value_record(fact, fact_ids)
            elif fp.startswith("person.contact."):
                contact[fp.split(".")[-1]] = _fact_value_record(fact, fact_ids)
            elif fp.startswith("person.location."):
                location[fp.split(".")[-1]] = _fact_value_record(fact, fact_ids)
            else:
                errors.append(f"person 实体包含未知字段：{fp}（{person['id']}）")
        if full_name:
            snapshot_person["fullName"] = full_name
        if contact:
            snapshot_person["contact"] = contact
        if location:
            snapshot_person["location"] = location

    # education / experiences / skills / awards
    def _build_records(items: list, entity_type: str) -> list:
        records = []
        for entity_id, facts in items:
            record: dict = {"id": entity_id}
            narratives: list = []
            has_type = False
            for fact in facts:
                fp = fact["field_path"]
                if entity_type == "experience" and fp == "experience.type":
                    value = json.loads(fact["value"])
                    if value not in EXPERIENCE_TYPES:
                        errors.append(
                            f"{entity_id}.experience.type: 非法值 {value!r}"
                            f"（需 ∈ {EXPERIENCE_TYPES}）"
                        )
                    record["type"] = value
                    has_type = True
                elif entity_type == "skill" and fp == "skill.category":
                    value = json.loads(fact["value"])
                    if value not in SKILL_CATEGORIES:
                        errors.append(
                            f"{entity_id}.skill.category: 非法值 {value!r}"
                            f"（需 ∈ {SKILL_CATEGORIES}）"
                        )
                    record["category"] = value
                elif entity_type == "experience" and FACT_SLUG_RE.match(fp):
                    if not fact["locale"]:
                        errors.append(
                            f"{fact['fact_id']}: narrative 事实（{fp}）必须提供 locale"
                        )
                    narratives.append(
                        {
                            "factId": fact["fact_id"],
                            "text": json.loads(fact["value"]),
                            "locale": fact["locale"],
                        }
                    )
                    fact_ids[fact["fact_id"]] = True
                elif fp in FIELD_PATH_TO_EXPORT_KEY:
                    record[FIELD_PATH_TO_EXPORT_KEY[fp]] = _fact_value_record(
                        fact, fact_ids
                    )
                else:
                    errors.append(f"{entity_id}: 未知字段 {fp}")
            if entity_type == "experience":
                if not has_type:
                    errors.append(f"{entity_id}: 缺少 experience.type（契约必填）")
                record["facts"] = narratives
            records.append(record)
        return records

    education_records = _build_records(education, "education")
    experience_records = _build_records(experiences, "experience")
    skill_records = _build_records(skills, "skill")
    award_records = _build_records(awards, "award")

    # 契约必填字段检查
    for rec in education_records:
        if rec.get("institution") is None:
            errors.append(f"{rec['id']}: 缺少 education.institution（契约必填）")
    for rec in experience_records:
        if rec.get("title") is None:
            errors.append(f"{rec['id']}: 缺少 experience.title（契约必填）")
    for rec in skill_records:
        if rec.get("category") is None:
            errors.append(f"{rec['id']}: 缺少 skill.category（契约必填）")
        if rec.get("name") is None:
            errors.append(f"{rec['id']}: 缺少 skill.name（契约必填）")
    for rec in award_records:
        if rec.get("name") is None:
            errors.append(f"{rec['id']}: 缺少 award.name（契约必填）")

    # expressions（阶段一为空；未来从 expressions 表收集）
    expression_rows = conn.execute(
        "SELECT expression_id, purpose, locale, max_chars, target_roles, text"
        " FROM expressions WHERE status = 'confirmed' ORDER BY expression_id"
    ).fetchall()
    expressions = []
    for eid, purpose, eloc, max_chars, roles_json, text in expression_rows:
        try:
            roles = json.loads(roles_json)
            assert isinstance(roles, list)
        except Exception:
            errors.append(f"{eid}: target_roles 不是合法 JSON 数组")
            roles = []
        src_fact_ids = [
            r[0]
            for r in conn.execute(
                "SELECT fact_id FROM expression_sources WHERE expression_id = ?"
                " ORDER BY fact_id",
                (eid,),
            ).fetchall()
        ]
        subject_ids = [
            r[0]
            for r in conn.execute(
                "SELECT entity_id FROM expression_subjects"
                " WHERE expression_id = ? ORDER BY entity_id",
                (eid,),
            ).fetchall()
        ]
        record = {
            "id": eid,
            "purpose": purpose,
            "locale": eloc,
            "maxChars": max_chars,
            "targetRoles": roles,
            "text": text,
            "sourceFactIds": src_fact_ids,
        }
        if subject_ids:
            record["subjectIds"] = subject_ids
        expressions.append(record)

    if errors:
        raise ValidationError(errors)

    revision = get_profile_revision(conn)
    if revision < 1:
        raise ValidationError(["profile_revision 为 0，不允许导出（尚无已应用的决定）"])

    return {
        "schemaVersion": SCHEMA_VERSION,
        "profileRevision": revision,
        "exportedAt": utcnow_iso(),
        "person": snapshot_person,
        "education": education_records,
        "experiences": experience_records,
        "skills": skill_records,
        "awards": award_records,
        "expressions": expressions,
    }


def cmd_export(db_path: Path, output: Path) -> int:
    """导出正式 Snapshot（只含 confirmed 事实与 confirmed 表达）。"""
    if not db_path.exists():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 1
    if needs_migration(db_path):
        backup_db(db_path)
    conn = connect(db_path)
    try:
        migrate(conn)
        ensure_meta_row(conn)
        conn.commit()
        snapshot = build_snapshot(conn)
        errors = validate_snapshot(snapshot)
        if errors:
            raise ValidationError(errors)
        conn.execute(
            "INSERT INTO profile_meta (key, value) VALUES ('last_exported_at', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (utcnow_iso(),),
        )
        conn.commit()
    finally:
        conn.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(
        f"已导出 Snapshot：{output}"
        f"（profileRevision={snapshot['profileRevision']}）"
    )
    return 0


# ---------------------------------------------------------------------------
# Snapshot 契约校验（validate-export 与 export 内部共用）
# ---------------------------------------------------------------------------

def validate_snapshot(snapshot: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(snapshot, dict):
        return ["Snapshot 根节点必须是 JSON 对象"]
    for key in (
        "schemaVersion",
        "profileRevision",
        "exportedAt",
        "person",
        "education",
        "experiences",
        "skills",
        "awards",
        "expressions",
    ):
        if key not in snapshot:
            errors.append(f"缺少必填字段：{key}")
    if snapshot.get("schemaVersion") != SCHEMA_VERSION:
        errors.append(f"schemaVersion 必须是 {SCHEMA_VERSION}")
    rev = snapshot.get("profileRevision")
    if not isinstance(rev, int) or rev < 1:
        errors.append("profileRevision 必须是 ≥1 的整数")
    exp_at = snapshot.get("exportedAt")
    if not isinstance(exp_at, str):
        errors.append("exportedAt 必须是非空字符串")
    else:
        try:
            datetime.fromisoformat(exp_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"exportedAt 不是合法的 ISO 8601 时间：{exp_at}")

    fact_ids: dict[str, bool] = {}
    entity_ids: dict[str, bool] = {}

    def register_id(value: object, registry: dict, path: str) -> None:
        if not isinstance(value, str) or not STABLE_ID_RE.match(value):
            errors.append(f"{path}: 非法 stableId：{value!r}")
            return
        if value in registry:
            errors.append(f"{path}: 重复 ID {value}")
            return
        registry[value] = True

    def validate_locale_quiet(loc: object, path: str) -> None:
        if loc is None:
            return
        if not isinstance(loc, str) or not LOCALE_RE.match(loc):
            errors.append(f"{path}: 非法 locale：{loc!r}")

    def validate_fact_value(obj: object, path: str, required_locale: bool = False) -> None:
        if not isinstance(obj, dict):
            errors.append(f"{path}: 必须是对象")
            return
        if "factId" not in obj or "value" not in obj:
            errors.append(f"{path}: 缺少 factId 或 value")
        fid = obj.get("factId")
        if isinstance(fid, str) and STABLE_ID_RE.match(fid):
            if fid in fact_ids:
                errors.append(f"{path}.factId: 重复 factId {fid}")
            else:
                fact_ids[fid] = True
        elif not isinstance(fid, str):
            errors.append(f"{path}.factId: 必须是字符串")
        else:
            errors.append(f"{path}.factId: 非法 stableId：{fid!r}")
        val = obj.get("value")
        if not isinstance(val, str) or not (1 <= len(val) <= 4000):
            errors.append(f"{path}.value: 必须是非空字符串且 ≤4000 字符")
        loc = obj.get("locale")
        if required_locale and not isinstance(loc, str):
            errors.append(f"{path}.locale: 必填")
        validate_locale_quiet(loc, f"{path}.locale")

    person = snapshot.get("person")
    if not isinstance(person, dict):
        errors.append("person: 必须是对象")
    else:
        register_id(person.get("id"), entity_ids, "person.id")
        full_name = person.get("fullName")
        if full_name is not None:
            if not isinstance(full_name, dict):
                errors.append("person.fullName: 必须是对象")
            else:
                if not full_name:
                    errors.append("person.fullName: 至少需要 1 个字段")
                for k in ("zhCN", "en", "givenNameEn", "familyNameEn"):
                    if k in full_name:
                        validate_fact_value(full_name[k], f"person.fullName.{k}")
        for section in ("contact", "location"):
            data = person.get(section)
            if data is not None:
                if not isinstance(data, dict):
                    errors.append(f"person.{section}: 必须是对象")
                else:
                    for k, v in data.items():
                        validate_fact_value(v, f"person.{section}.{k}")

    def validate_record_array(arr: object, path: str, kind: str) -> None:
        if not isinstance(arr, list):
            errors.append(f"{path}: 必须是数组")
            return
        for idx, rec in enumerate(arr):
            rp = f"{path}[{idx}]"
            if not isinstance(rec, dict):
                errors.append(f"{rp}: 必须是对象")
                continue
            register_id(rec.get("id"), entity_ids, f"{rp}.id")
            if kind == "education":
                if "institution" not in rec:
                    errors.append(f"{rp}: 缺少必填字段 institution")
                for key in (
                    "institution",
                    "degree",
                    "major",
                    "minor",
                    "location",
                    "startDate",
                    "endDate",
                    "gpa",
                    "averageScore",
                    "ranking",
                ):
                    if key in rec:
                        validate_fact_value(rec[key], f"{rp}.{key}")
                if "isHighest" in rec and not isinstance(rec["isHighest"], bool):
                    errors.append(f"{rp}.isHighest: 必须是布尔值")
            elif kind == "experience":
                if "type" not in rec:
                    errors.append(f"{rp}: 缺少必填字段 type")
                elif rec["type"] not in EXPERIENCE_TYPES:
                    errors.append(f"{rp}.type: 必须 ∈ {EXPERIENCE_TYPES}")
                if "title" not in rec:
                    errors.append(f"{rp}: 缺少必填字段 title")
                for key in ("title", "organization", "role", "location", "startDate", "endDate"):
                    if key in rec:
                        validate_fact_value(rec[key], f"{rp}.{key}")
                facts = rec.get("facts")
                if "facts" not in rec:
                    errors.append(f"{rp}: 缺少必填字段 facts")
                elif not isinstance(facts, list):
                    errors.append(f"{rp}.facts: 必须是数组")
                else:
                    for fidx, nar in enumerate(facts):
                        # 叙述型事实为 narrativeFact：{factId, text, locale}
                        np = f"{rp}.facts[{fidx}]"
                        if not isinstance(nar, dict):
                            errors.append(f"{np}: 必须是对象")
                            continue
                        if "factId" not in nar or "text" not in nar:
                            errors.append(f"{np}: 缺少 factId 或 text")
                        nfid = nar.get("factId")
                        if isinstance(nfid, str) and STABLE_ID_RE.match(nfid):
                            if nfid in fact_ids:
                                errors.append(f"{np}.factId: 重复 factId {nfid}")
                            else:
                                fact_ids[nfid] = True
                        elif not isinstance(nfid, str):
                            errors.append(f"{np}.factId: 必须是字符串")
                        else:
                            errors.append(f"{np}.factId: 非法 stableId：{nfid!r}")
                        ntext = nar.get("text")
                        if not isinstance(ntext, str) or not (1 <= len(ntext) <= 4000):
                            errors.append(f"{np}.text: 必须是非空字符串且 ≤4000 字符")
                        nloc = nar.get("locale")
                        if not isinstance(nloc, str):
                            errors.append(f"{np}.locale: 必填")
                        validate_locale_quiet(nloc, f"{np}.locale")
            elif kind == "skill":
                if "category" not in rec:
                    errors.append(f"{rp}: 缺少必填字段 category")
                elif rec["category"] not in SKILL_CATEGORIES:
                    errors.append(f"{rp}.category: 必须 ∈ {SKILL_CATEGORIES}")
                if "name" not in rec:
                    errors.append(f"{rp}: 缺少必填字段 name")
                for key in ("name", "level", "details"):
                    if key in rec:
                        validate_fact_value(rec[key], f"{rp}.{key}")
            elif kind == "award":
                if "name" not in rec:
                    errors.append(f"{rp}: 缺少必填字段 name")
                for key in ("name", "issuer", "date", "details"):
                    if key in rec:
                        validate_fact_value(rec[key], f"{rp}.{key}")

    validate_record_array(snapshot.get("education"), "education", "education")
    validate_record_array(snapshot.get("experiences"), "experiences", "experience")
    validate_record_array(snapshot.get("skills"), "skills", "skill")
    validate_record_array(snapshot.get("awards"), "awards", "award")

    expressions = snapshot.get("expressions")
    if not isinstance(expressions, list):
        errors.append("expressions: 必须是数组")
    else:
        for idx, rec in enumerate(expressions):
            rp = f"expressions[{idx}]"
            if not isinstance(rec, dict):
                errors.append(f"{rp}: 必须是对象")
                continue
            register_id(rec.get("id"), entity_ids, f"{rp}.id")
            for key in ("purpose", "locale", "maxChars", "targetRoles", "text", "sourceFactIds"):
                if key not in rec:
                    errors.append(f"{rp}: 缺少必填字段 {key}")
            purpose = rec.get("purpose")
            if not isinstance(purpose, str) or not (1 <= len(purpose) <= 120):
                errors.append(f"{rp}.purpose: 必须是非空字符串且 ≤120 字符")
            validate_locale_quiet(rec.get("locale"), f"{rp}.locale")
            mc = rec.get("maxChars")
            if mc is not None and (not isinstance(mc, int) or mc < 1):
                errors.append(f"{rp}.maxChars: 必须是 ≥1 的整数或 null")
            roles = rec.get("targetRoles")
            if not isinstance(roles, list):
                errors.append(f"{rp}.targetRoles: 必须是数组")
            elif not all(isinstance(r, str) and 1 <= len(r) <= 80 for r in roles):
                errors.append(f"{rp}.targetRoles: 每项必须是非空字符串且 ≤80 字符")
            elif len(set(roles)) != len(roles):
                errors.append(f"{rp}.targetRoles: 不允许重复项")
            text = rec.get("text")
            if not isinstance(text, str) or not (1 <= len(text) <= 8000):
                errors.append(f"{rp}.text: 必须是非空字符串且 ≤8000 字符")
            subs = rec.get("subjectIds")
            if subs is not None:
                if not isinstance(subs, list):
                    errors.append(f"{rp}.subjectIds: 必须是数组")
                elif len(set(subs)) != len(subs):
                    errors.append(f"{rp}.subjectIds: 不允许重复项")
            srcs = rec.get("sourceFactIds")
            if not isinstance(srcs, list) or not srcs:
                errors.append(f"{rp}.sourceFactIds: 必须是非空数组")
            elif len(set(srcs)) != len(srcs):
                errors.append(f"{rp}.sourceFactIds: 不允许重复项")

    # 引用完整性
    for rec in expressions if isinstance(expressions, list) else []:
        if not isinstance(rec, dict):
            continue
        for sid in rec.get("sourceFactIds", []):
            if isinstance(sid, str) and STABLE_ID_RE.match(sid) and sid not in fact_ids:
                errors.append(
                    f"expressions {rec.get('id')}: sourceFactIds 引用不存在的事实 {sid}"
                )
        for sid in rec.get("subjectIds", []):
            if isinstance(sid, str) and STABLE_ID_RE.match(sid) and sid not in entity_ids:
                errors.append(
                    f"expressions {rec.get('id')}: subjectIds 引用不存在的实体 {sid}"
                )

    # 禁止键递归检查
    def walk(obj: object, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in FORBIDDEN_EXPORT_KEYS:
                    errors.append(f"{path}.{k}: 导出内容禁止包含 {k}")
                walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for idx, v in enumerate(obj):
                walk(v, f"{path}[{idx}]")

    walk(snapshot, "snapshot")

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Career Profile 本地数据库工具（阶段一）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化数据库（可重复执行，不清空数据）")
    p_init.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_vc = sub.add_parser("validate-candidates", help="校验候选文件结构")
    p_vc.add_argument("candidates", type=Path)
    p_vc.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_ad = sub.add_parser("apply-decisions", help="应用人工决定（写库前自动备份）")
    p_ad.add_argument("candidates", type=Path)
    p_ad.add_argument("decisions", type=Path)
    p_ad.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_exp = sub.add_parser("export", help="导出正式 Snapshot（只含 confirmed）")
    p_exp.add_argument("output", type=Path)
    p_exp.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_ae = sub.add_parser(
        "add-expression", help="写入/更新一条主观表达（写库前自动备份）"
    )
    p_ae.add_argument(
        "--id", required=True, help="稳定 ID，如 expression.why-us.companyA"
    )
    p_ae.add_argument("--purpose", required=True, help="用途，如 why-us / career-goal")
    p_ae.add_argument("--locale", default="zh-CN")
    p_ae.add_argument("--text", help="正文（与 --text-file 二选一）")
    p_ae.add_argument(
        "--text-file", type=Path, help="从文件读取正文（推荐，中文长文本）"
    )
    p_ae.add_argument("--target-roles", required=True, help="目标岗位，逗号分隔")
    p_ae.add_argument(
        "--source-fact-ids", required=True, help="引用的已确认事实 ID，逗号分隔"
    )
    p_ae.add_argument("--subject-ids", default="", help="相关实体 ID，逗号分隔（可选）")
    p_ae.add_argument("--max-chars", type=int, default=None, help="字数上限")
    p_ae.add_argument("--status", default="confirmed", choices=list(STATUSES))
    p_ae.add_argument(
        "--export",
        nargs="?",
        const=DEFAULT_EXPORT_PATH,
        type=Path,
        default=None,
        help="写库后自动重新导出 Snapshot（不带值则用默认路径）",
    )
    p_ae.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_le = sub.add_parser("list-expressions", help="列出已沉淀的主观表达")
    p_le.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_ve = sub.add_parser("validate-export", help="校验 Snapshot 符合共享契约")
    p_ve.add_argument("snapshot", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _arg_parser().parse_args(argv)

    if args.command == "init":
        backup = backup_db(args.db) if needs_migration(args.db) else None
        conn = connect(args.db)
        try:
            migrate(conn)
            ensure_meta_row(conn)
            conn.commit()
        finally:
            conn.close()
        # 建好标准目录，新用户直接把材料放进 materials/ 即可
        for d in ("materials", "staging", "review", "exports"):
            (PROFILE_DB_DIR / d).mkdir(parents=True, exist_ok=True)
        note = f"，备份：{backup.name}" if backup else ""
        print(f"数据库已初始化：{args.db}{note}")
        print(f"标准目录已就绪：materials/ staging/ review/ exports/（材料请放进 materials/）")
        return 0

    if args.command == "validate-candidates":
        data = load_json(args.candidates)
        validate_candidates_document(data)
        print(
            f"候选文件校验通过：{len(data['sources'])} 个来源，"
            f"{len(data['candidates'])} 个候选"
        )
        return 0

    if args.command == "apply-decisions":
        return cmd_apply_decisions(args.candidates, args.decisions, args.db)

    if args.command == "export":
        return cmd_export(args.db, args.output)

    if args.command == "add-expression":
        text = args.text
        if args.text_file is not None:
            if text is not None:
                print("错误: --text 与 --text-file 只能二选一", file=sys.stderr)
                return 1
            text = args.text_file.read_text(encoding="utf-8").strip()
        if text is None:
            print("错误: 必须提供 --text 或 --text-file", file=sys.stderr)
            return 1
        return cmd_add_expression(
            db_path=args.db,
            expression_id=args.id,
            purpose=args.purpose,
            locale=args.locale,
            text=text,
            source_fact_ids=_split_ids(args.source_fact_ids),
            target_roles=_split_ids(args.target_roles),
            max_chars=args.max_chars,
            subject_ids=_split_ids(args.subject_ids),
            status=args.status,
            export_path=args.export,
        )

    if args.command == "list-expressions":
        return cmd_list_expressions(args.db)

    if args.command == "validate-export":
        data = load_json(args.snapshot)
        errors = validate_snapshot(data)
        if errors:
            for e in errors:
                print(f"错误: {e}", file=sys.stderr)
            return 1
        print("Snapshot 校验通过（符合共享契约）")
        return 0

    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValidationError as exc:
        for e in exc.errors:
            print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)
