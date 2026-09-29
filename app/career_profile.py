#!/usr/bin/env python3
"""Career Profile SQLite 数据库与 Snapshot 导出工具（阶段一）。

职责：init / validate-candidates / apply-decisions / validate-links /
apply-links / relocate-sources / add-expression / list-expressions / export / validate-export。
只使用 Python 标准库。候选事实必须经过用户确认（decisions.json）才能写入
正式库，导出只包含 status=confirmed 的事实。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
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
LINK_VERSION = "1.0.0"
SCHEMA_VERSION = "1.1.0"
DB_SCHEMA_VERSION = 3

STATUSES = ("pending", "confirmed", "conflict", "rejected")
ACTIONS = ("accept", "reject", "replace")
ENTITY_TYPES = ("person", "education", "experience", "skill", "award")
EXPERIENCE_TYPES = ("professional", "research", "leadership", "project")
SKILL_CATEGORIES = ("language", "technical", "research", "qualification", "other")
# 文本类来源（可做证据摘录包含性检查）；Word/PDF 程序不解析。
TEXT_DOCUMENT_TYPES = ("md", "html", "txt")

STABLE_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{2,119}$")
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
    3: """
    DROP INDEX IF EXISTS facts_confirmed_unique;
    CREATE UNIQUE INDEX facts_confirmed_unique ON facts (
        entity_id, field_path, COALESCE(locale, '')
    ) WHERE status = 'confirmed';
    CREATE TABLE IF NOT EXISTS fact_supersessions (
        old_fact_id TEXT NOT NULL REFERENCES facts(fact_id),
        new_fact_id TEXT NOT NULL REFERENCES facts(fact_id),
        superseded_at TEXT NOT NULL,
        PRIMARY KEY (old_fact_id, new_fact_id),
        CHECK (old_fact_id != new_fact_id)
    );
    """,
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup_db(db_path: Path) -> Path | None:
    """备份完整已提交快照（含 WAL）；失败时不留下可误认为成功的文件。"""
    if not db_path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".career_profile.", suffix=".tmp", dir=BACKUP_DIR)
    os.close(fd)
    pending = Path(temporary)
    try:
        source = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            destination = sqlite3.connect(str(pending))
            try:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValidationError(["备份完整性检查失败，未生成备份文件"])
            finally:
                destination.close()
        finally:
            source.close()
        digest = sha256_file(pending)
        ts = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        target = BACKUP_DIR / f"career_profile.{ts}.{uuid.uuid4().hex}.{digest}.db"
        pending.rename(target)
        return target
    finally:
        pending.unlink(missing_ok=True)


def current_schema_version(conn: sqlite3.Connection) -> int:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone():
        return 0
    rows = conn.execute("SELECT version, sha256 FROM schema_migrations ORDER BY version").fetchall()
    versions = [row[0] for row in rows]
    if any(type(version) is not int or version < 1 for version in versions):
        raise ValidationError(["数据库迁移版本无效，未执行迁移"])
    current = versions[-1] if versions else 0
    if current > DB_SCHEMA_VERSION:
        raise ValidationError([f"数据库版本 {current} 高于程序支持的 {DB_SCHEMA_VERSION}，请使用兼容版本"])
    if versions != list(range(1, current + 1)):
        raise ValidationError(["数据库迁移记录不连续，未执行迁移"])
    for version, digest in rows:
        if digest != sha256_bytes(MIGRATIONS[version].encode("utf-8")):
            raise ValidationError([f"数据库迁移 {version} 指纹与程序不一致，未执行迁移"])
    return current


def migrate(conn: sqlite3.Connection) -> None:
    """在单事务内把数据库迁移到最新版本，任一步失败整体回滚。

    sqlite3 的 executescript 会隐式提交进行中的事务，因此这里把脚本拆成
    单条语句逐条执行，保证迁移原子性。
    """
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        current = current_schema_version(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL,"
            " sha256 TEXT NOT NULL)"
        )
        for version in range(current + 1, DB_SCHEMA_VERSION + 1):
            if version == 3:
                duplicates = conn.execute(
                    "SELECT entity_id, field_path, COALESCE(locale, ''), COUNT(*)"
                    " FROM facts WHERE status='confirmed'"
                    " GROUP BY entity_id, field_path, COALESCE(locale, '') HAVING COUNT(*) > 1"
                ).fetchall()
                if duplicates:
                    details = "; ".join(f"{e}/{f}/{loc or '(无语言)'}: {count} 条" for e, f, loc, count in duplicates)
                    raise ValidationError(["v3 迁移发现同字段同语言的多条确认事实，原库保持不变；需先人工裁决：" + details])
            sql = MIGRATIONS[version]
            for statement in split_sql_statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, sha256)"
                " VALUES (?, ?, ?)",
                (version, utcnow_iso(), sha256_bytes(sql.encode("utf-8"))),
            )
        if owns_transaction:
            conn.commit()
    except Exception:
        conn.rollback()
        raise


def needs_migration(db_path: Path) -> bool:
    """数据库不存在、表缺失或版本落后时返回 True（迁移前需要备份）。"""
    if not db_path.exists():
        return False
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return current_schema_version(conn) < DB_SCHEMA_VERSION
    finally:
        conn.close()


def _prepare_write(db_path: Path) -> tuple[sqlite3.Connection, Path | None]:
    """锁定写入后备份已提交状态，再把迁移和本次修改放进同一事务。"""
    existed = db_path.exists()
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_schema_version(conn)
        backup = backup_db(db_path) if existed else None
        migrate(conn)
        ensure_meta_row(conn)
        return conn, backup
    except Exception:
        conn.rollback()
        conn.close()
        raise


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
    if not isinstance(value, str) or not STABLE_ID_RE.fullmatch(value):
        errors.append(f"{path}: 非法 stableId（小写字母开头，限字母/数字/._-，长度 3–120）：{value!r}")
        return False
    return True


def validate_locale(value: object, errors: list[str], path: str) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not LOCALE_RE.fullmatch(value):
        errors.append(f"{path}: 非法 locale：{value!r}")
        return False
    return True


def _validate_field_value(field_path: str, value: object, errors: list[str], path: str) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: 必须是非空白字符串")
    elif len(value) > 4000:
        errors.append(f"{path}: 超过 4000 字符")
    elif field_path == "experience.type" and value not in EXPERIENCE_TYPES:
        errors.append(f"{path}: experience.type 必须是 {EXPERIENCE_TYPES} 之一")
    elif field_path == "skill.category" and value not in SKILL_CATEGORIES:
        errors.append(f"{path}: skill.category 必须是 {SKILL_CATEGORIES} 之一")


def _validate_sources(sources: object, errors: list[str]) -> tuple[set[str], dict]:
    """校验来源清单：stableId、绝对路径、允许目录、文件存在、SHA-256 一致。

    返回（已定义的 sourceId 集合，sourceId → 元数据）。候选文件与补挂来源文件共用。
    """
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
        if isinstance(sid, str) and STABLE_ID_RE.fullmatch(sid):
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
    return source_ids, source_map


def _validate_source_refs(
    refs: object,
    path: str,
    source_ids: set[str],
    source_map: dict,
    locator_cache: dict[str, str],
    errors: list[str],
) -> None:
    """校验一组证据引用：来源已定义、locator/excerpt 非空、不重复引用、文本来源逐字。"""
    if not isinstance(refs, list) or not refs:
        errors.append(f"{path}.sourceRefs: 必须是非空数组")
    else:
        seen_ref = set()
        for ridx, ref in enumerate(refs):
            rp = f"{path}.sourceRefs[{ridx}]"
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


def _validate_registered_sources(conn: sqlite3.Connection, sources: list[dict]) -> None:
    """sourceId 永远标识同一路径、类型和内容；更新材料须使用新 ID。"""
    errors: list[str] = []
    for src in sources:
        row = conn.execute(
            "SELECT absolute_path, document_type, sha256 FROM sources WHERE source_id = ?",
            (src["sourceId"],),
        ).fetchone()
        identity = (src["absolutePath"], src["documentType"], src["sha256"])
        if row is not None and tuple(row) != identity:
            errors.append(
                f"{src['sourceId']}: 已登记为另一份文件、类型或文件的旧版本；"
                "请使用新的 sourceId，原来源指纹不会被覆盖"
            )
    if errors:
        raise ValidationError(errors)


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

    source_ids, source_map = _validate_sources(data.get("sources"), errors)

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
        if isinstance(cid, str) and STABLE_ID_RE.fullmatch(cid):
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
        elif entity_type in ENTITY_TYPES and not field_path.startswith(entity_type + "."):
            errors.append(f"{cp}.fieldPath: 字段不属于 {entity_type} 实体")
        if not validate_locale(locale, errors, f"{cp}.locale"):
            locale = None
        _validate_field_value(field_path, value, errors, f"{cp}.proposedValue")
        if status not in ("pending", "conflict"):
            errors.append(f"{cp}.status: 候选文件只允许 pending 或 conflict，得到 {status!r}")
        if cg is not None:
            validate_stable_id(cg, errors, f"{cp}.conflictGroupId")
        if cand.get("supersedes") is not None:
            validate_stable_id(cand["supersedes"], errors, f"{cp}.supersedes")
        _validate_source_refs(refs, cp, source_ids, source_map, locator_cache, errors)
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
            _validate_field_value(
                candidate_map.get(cid, {}).get("fieldPath", ""), replacement, errors,
                f"{dp}.replacementValue",
            )
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

def _supersede(conn: sqlite3.Connection, cand: dict, now: str) -> None:
    """新值采纳时把被替换的旧事实置为 rejected（留痕不删），释放唯一键。

    旧事实必须已确认，且与新候选属于同一实体、字段和语言。
    """
    old_id = cand["supersedes"]
    row = conn.execute(
        "SELECT entity_id, field_path, locale, status FROM facts WHERE fact_id = ?",
        (old_id,),
    ).fetchone()
    cid = cand["candidateId"]
    if row is None:
        raise ValidationError([f"{cid}.supersedes: 库中不存在事实 {old_id}"])
    if row[3] != "confirmed":
        raise ValidationError([f"{cid}.supersedes: {old_id} 的状态是 {row[3]}，只能替换已确认事实"])
    if (row[0], row[1], row[2]) != (cand["entityKey"], cand["fieldPath"], cand.get("locale")):
        raise ValidationError([f"{cid}.supersedes: {old_id} 与新候选不是同一实体、字段和语言"])
    conn.execute(
        "UPDATE facts SET status = 'rejected', updated_at = ? WHERE fact_id = ?", (now, old_id)
    )
    conn.execute(
        "UPDATE expressions SET status = 'pending', updated_at = ?"
        " WHERE status='confirmed' AND expression_id IN"
        " (SELECT expression_id FROM expression_sources WHERE fact_id = ?)",
        (now, old_id),
    )


def cmd_apply_decisions(candidates_path: Path, decisions_path: Path, db_path: Path) -> int:
    cand_data = load_json(candidates_path)
    validate_candidates_document(cand_data)  # 先整体校验，失败即退出
    dec_data = load_json(decisions_path)
    decisions = validate_decisions_document(dec_data, cand_data["candidates"])

    candidates_by_entity: dict[str, list[dict]] = {}
    for cand in cand_data["candidates"]:
        candidates_by_entity.setdefault(cand["entityKey"], []).append(cand)

    conn, backup = _prepare_write(db_path)
    try:
        # 文件可能在准备决定与实际写入之间变化；锁内重新核实来源与身份。
        validate_candidates_document(cand_data)
        _validate_registered_sources(conn, cand_data["sources"])
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
            if status == "confirmed" and cand.get("supersedes"):
                _supersede(conn, cand, utcnow_iso())
            value = (
                dec["replacementValue"]
                if action == "replace" and dec["replacementValue"] is not None
                else cand["proposedValue"]
            )
            entity_key = cand["entityKey"]
            now = utcnow_iso()
            entity = conn.execute(
                "SELECT entity_type FROM entities WHERE entity_id = ?", (entity_key,)
            ).fetchone()
            if entity and entity[0] != cand["entityType"]:
                raise ValidationError([f"{fact_id}: entityKey 已属于 {entity[0]}，不能改为 {cand['entityType']}"])
            if status == "confirmed":
                occupied = conn.execute(
                    "SELECT fact_id FROM facts WHERE entity_id = ? AND field_path = ?"
                    " AND COALESCE(locale, '') = COALESCE(?, '') AND status='confirmed'",
                    (entity_key, cand["fieldPath"], cand.get("locale")),
                ).fetchone()
                if occupied:
                    raise ValidationError([
                        f"{fact_id}: 同一实体、字段和语言已有确认事实 {occupied[0]}；"
                        "请选择一个冲突值，替换旧值需显式指定 supersedes"
                    ])
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
            if status == "confirmed" and cand.get("supersedes"):
                conn.execute(
                    "INSERT INTO fact_supersessions (old_fact_id, new_fact_id, superseded_at)"
                    " VALUES (?, ?, ?)",
                    (cand["supersedes"], fact_id, now),
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
# validate-links / apply-links：给已确认事实补挂来源（事实本身不变）
# ---------------------------------------------------------------------------

def validate_links_document(data: object, conn: sqlite3.Connection) -> dict:
    """校验补挂来源文件（links 补挂、unlinks 撤销挂错的来源，至少有一类）。

    来源规则与候选文件相同；另要求目标事实已确认、同一事实不重复挂同一来源、
    已登记的 sourceId 指向同一份未改动的文件；撤销后每条事实至少保留一个来源。
    失败抛出 ValidationError。
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ValidationError(["补挂来源文件根节点必须是 JSON 对象"])
    if data.get("linkVersion") != LINK_VERSION:
        errors.append(f"linkVersion 必须是 {LINK_VERSION}")
    if not isinstance(data.get("generatedAt"), str) or not data["generatedAt"]:
        errors.append("generatedAt 必须是非空字符串（ISO 8601）")

    links = data.get("links", [])
    unlinks = data.get("unlinks", [])
    if not isinstance(links, list) or not isinstance(unlinks, list):
        raise ValidationError(["links / unlinks 必须是数组"])
    if not links and not unlinks:
        errors.append("links 与 unlinks 不能都为空")

    if links:
        source_ids, source_map = _validate_sources(data.get("sources"), errors)
    else:
        source_ids, source_map = set(), {}
    for idx, src in enumerate(data.get("sources") or []):
        if not isinstance(src, dict):
            continue
        row = conn.execute(
            "SELECT absolute_path, document_type, sha256 FROM sources WHERE source_id = ?",
            (src.get("sourceId"),),
        ).fetchone()
        if row and tuple(row) != (src.get("absolutePath"), src.get("documentType"), src.get("sha256")):
            errors.append(
                f"sources[{idx}].sourceId: {src.get('sourceId')} 已登记为另一份文件或"
                "文件的旧版本，请换一个新的 sourceId"
            )

    link_ids: set[str] = set()
    pairs: set[tuple] = set()
    locator_cache: dict[str, str] = {}
    for idx, link in enumerate(links):
        lp = f"links[{idx}]"
        if not isinstance(link, dict):
            errors.append(f"{lp}: 必须是对象")
            continue
        lid = link.get("linkId")
        if isinstance(lid, str) and STABLE_ID_RE.fullmatch(lid):
            if lid in link_ids:
                errors.append(f"{lp}.linkId: 重复的 linkId {lid}")
            link_ids.add(lid)
        else:
            errors.append(f"{lp}.linkId: 非法 stableId：{lid!r}")
        fact_id = link.get("factId")
        row = conn.execute(
            "SELECT status FROM facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        if row is None:
            errors.append(f"{lp}.factId: 库中不存在事实 {fact_id!r}")
        elif row[0] != "confirmed":
            errors.append(f"{lp}.factId: 只能给已确认事实补挂来源，{fact_id} 的状态是 {row[0]}")
        refs = link.get("sourceRefs")
        _validate_source_refs(refs, lp, source_ids, source_map, locator_cache, errors)
        for ref in refs if isinstance(refs, list) else []:
            sid = ref.get("sourceId") if isinstance(ref, dict) else None
            if (fact_id, sid) in pairs:
                errors.append(f"{lp}: 文件内重复给 {fact_id} 挂来源 {sid}")
            pairs.add((fact_id, sid))
            if conn.execute(
                "SELECT 1 FROM fact_sources WHERE fact_id = ? AND source_id = ?",
                (fact_id, sid),
            ).fetchone():
                errors.append(f"{lp}: {fact_id} 已经挂有来源 {sid}")

    removed: dict[str, set[str]] = {}
    for idx, unlink in enumerate(unlinks):
        up = f"unlinks[{idx}]"
        if not isinstance(unlink, dict):
            errors.append(f"{up}: 必须是对象")
            continue
        uid = unlink.get("unlinkId")
        if isinstance(uid, str) and STABLE_ID_RE.fullmatch(uid):
            if uid in link_ids:
                errors.append(f"{up}.unlinkId: 与已有 ID 重复 {uid}")
            link_ids.add(uid)
        else:
            errors.append(f"{up}.unlinkId: 非法 stableId：{uid!r}")
        fact_id, sid = unlink.get("factId"), unlink.get("sourceId")
        fact = conn.execute("SELECT status FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
        if fact is None or fact[0] != "confirmed":
            errors.append(f"{up}.factId: 只能撤销已确认事实的来源：{fact_id!r}")
        if not isinstance(unlink.get("reason"), str) or not unlink["reason"]:
            errors.append(f"{up}.reason: 必须写明撤销原因")
        if not conn.execute(
            "SELECT 1 FROM fact_sources WHERE fact_id = ? AND source_id = ?", (fact_id, sid)
        ).fetchone():
            errors.append(f"{up}: {fact_id} 并没有挂来源 {sid!r}")
            continue
        removed.setdefault(fact_id, set()).add(sid)
    for fact_id, sids in removed.items():
        remaining = {
            r[0] for r in conn.execute(
                "SELECT source_id FROM fact_sources WHERE fact_id = ?", (fact_id,)
            )
        } - sids
        added = {
            ref.get("sourceId")
            for link in links if isinstance(link, dict) and link.get("factId") == fact_id
            for ref in link.get("sourceRefs") or [] if isinstance(ref, dict)
        }
        if not remaining and not added:
            errors.append(f"{fact_id}: 撤销后没有任何来源，每条事实至少要保留一个来源")

    if errors:
        raise ValidationError(errors)
    return data


def _load_links(links_path: Path, db_path: Path) -> dict:
    if not db_path.exists():
        raise ValidationError([f"找不到数据库：{db_path}"])
    data = load_json(links_path)
    conn = connect(db_path)
    try:
        validate_links_document(data, conn)
    finally:
        conn.close()
    return data


def cmd_validate_links(links_path: Path, db_path: Path) -> int:
    data = _load_links(links_path, db_path)
    print(
        f"补挂来源文件校验通过：{len(data.get('sources', []))} 个来源，"
        f"{len(data.get('links', []))} 条补挂，{len(data.get('unlinks', []))} 条撤销"
    )
    return 0


def cmd_apply_links(links_path: Path, decisions_path: Path, db_path: Path) -> int:
    data = _load_links(links_path, db_path)
    as_candidates = [
        {**link, "candidateId": link["linkId"]} for link in data.get("links", [])
    ] + [
        {**unlink, "candidateId": unlink["unlinkId"]} for unlink in data.get("unlinks", [])
    ]
    decisions = validate_decisions_document(load_json(decisions_path), as_candidates)
    bad = [d["candidateId"] for d in decisions if d["action"] not in ("accept", "reject")]
    if bad:
        raise ValidationError([f"{cid}: 补挂来源只接受 accept 或 reject" for cid in bad])

    sources = {s["sourceId"]: s for s in data.get("sources", [])}
    accepted = [d["candidate"] for d in decisions if d["action"] == "accept"]
    to_link = [c for c in accepted if "linkId" in c]
    to_unlink = [c for c in accepted if "unlinkId" in c]
    conn, backup = _prepare_write(db_path)
    try:
        # 校验实际采纳的子集，避免拒绝的补挂掩盖删除最后来源的操作。
        if accepted:
            accepted_source_ids = {
                ref["sourceId"] for link in to_link for ref in link["sourceRefs"]
            }
            accepted_sources = [sources[sid] for sid in sorted(accepted_source_ids)]
            validate_links_document(
                {**data, "links": to_link, "unlinks": to_unlink, "sources": accepted_sources}, conn
            )
            _validate_registered_sources(conn, accepted_sources)
        now = utcnow_iso()
        for unlink in to_unlink:
            conn.execute(
                "DELETE FROM fact_sources WHERE fact_id = ? AND source_id = ?",
                (unlink["factId"], unlink["sourceId"]),
            )
            conn.execute(
                "UPDATE facts SET updated_at = ? WHERE fact_id = ?", (now, unlink["factId"])
            )
        for link in to_link:
            for ref in link["sourceRefs"]:
                src = sources[ref["sourceId"]]
                conn.execute(
                    "INSERT OR IGNORE INTO sources (source_id, absolute_path,"
                    " document_type, sha256, registered_at) VALUES (?, ?, ?, ?, ?)",
                    (src["sourceId"], src["absolutePath"], src["documentType"], src["sha256"], now),
                )
                conn.execute(
                    "INSERT INTO fact_sources (fact_id, source_id, locator, excerpt)"
                    " VALUES (?, ?, ?, ?)",
                    (link["factId"], ref["sourceId"], ref["locator"], ref["excerpt"]),
                )
            conn.execute(
                "UPDATE facts SET updated_at = ? WHERE fact_id = ?", (now, link["factId"])
            )
        for fact_id in {item["factId"] for item in accepted}:
            if not conn.execute(
                "SELECT 1 FROM fact_sources WHERE fact_id = ?", (fact_id,)
            ).fetchone():
                raise ValidationError([f"{fact_id}: 操作后没有来源，已回滚全部补挂与撤销"])
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
        f"已补挂 {len(to_link)} 条、撤销 {len(to_unlink)} 条"
        f"（否决 {len(decisions) - len(accepted)} 条）；profile_revision = {new_rev}{backup_note}"
    )
    return 0


# ---------------------------------------------------------------------------
# relocate-sources：资料文件挪了位置后，同步库里登记的路径
# ---------------------------------------------------------------------------

def cmd_relocate_sources(mapping_path: Path, db_path: Path, apply: bool) -> int:
    """按 {"moves": {旧绝对路径: 新绝对路径}} 更新 sources.absolute_path。

    只改路径，不改登记时的 SHA-256；新文件与登记时内容不同的会单独列出（文件在登记后被改过）。
    默认只预览，加 --apply 才写库（写库前自动备份）。
    """
    moves = load_json(mapping_path).get("moves")
    if not isinstance(moves, dict):
        raise ValidationError(["映射文件必须包含 moves 对象：{旧绝对路径: 新绝对路径}"])
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT source_id, absolute_path, sha256 FROM sources").fetchall()
    finally:
        conn.close()

    errors: list[str] = []
    updates: list[tuple[str, str]] = []
    changed: list[str] = []
    for source_id, old, registered_sha in rows:
        new = moves.get(old)
        if new is None:
            if not Path(old).exists():
                errors.append(f"{source_id}: 登记的文件已不在原位且映射里没有新位置：{old}")
            continue
        real = Path(new).resolve()
        if not any(real.is_relative_to(d) for d in ALLOWED_SOURCE_DIRS):
            errors.append(f"{source_id}: 新位置不在允许读取目录内：{new}")
        elif not real.is_file():
            errors.append(f"{source_id}: 新位置没有文件：{new}")
        else:
            if sha256_file(real) != registered_sha:
                changed.append(f"{source_id}（{real.name}）")
            updates.append((source_id, str(real)))
    if errors:
        raise ValidationError(errors)

    print(f"需要更新路径的来源 {len(updates)} 个，共登记 {len(rows)} 个")
    if changed:
        print("以下文件在登记后被改过（路径照常更新，内容差异原本就存在）：" + "、".join(changed))
    if not apply:
        print("预览完成；确认无误后加 --apply 写库")
        return 0
    conn, backup = _prepare_write(db_path)
    try:
        # 预览之后有其他写入时不能把旧计划应用到新来源身份。
        current_rows = conn.execute("SELECT source_id, absolute_path, sha256 FROM sources").fetchall()
        if sorted(current_rows) != sorted(rows):
            raise ValidationError(["来源登记在预览后发生变化，请重新运行 relocate-sources"])
        conn.executemany(
            "UPDATE sources SET absolute_path = ? WHERE source_id = ?",
            [(path, sid) for sid, path in updates],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    backup_note = f"，备份：{backup.name}" if backup else ""
    print(f"已更新 {len(updates)} 个来源路径{backup_note}")
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
    validate_stable_id(expression_id, errors, "--id")
    if not purpose.strip() or not (1 <= len(purpose) <= 120):
        errors.append("--purpose: 必须是非空字符串且 ≤120 字符")
    if not LOCALE_RE.fullmatch(locale):
        errors.append("--locale: 必须匹配 ^[a-z]{2,3}(-[A-Z]{2})?$")
    if not text.strip() or not (1 <= len(text) <= 8000):
        errors.append("正文: 必须是非空字符串且 ≤8000 字符")
    if max_chars is not None and max_chars < 1:
        errors.append("--max-chars: 必须是 ≥1 的整数")
    if not target_roles:
        errors.append("--target-roles: 至少需要一个目标岗位")
    for role in target_roles:
        if not role.strip() or not (1 <= len(role) <= 80):
            errors.append(f"--target-roles 项 {role!r}: 必须是 1–80 字符")
    if not source_fact_ids:
        errors.append("--source-fact-ids: 至少引用一个已确认事实")
    if status not in STATUSES:
        errors.append(f"--status: 只能是 {'/'.join(STATUSES)}")
    if errors:
        raise ValidationError(errors)

    if not db_path.exists():
        raise ValidationError([f"数据库不存在：{db_path}（请先运行 init）"])

    conn, backup = _prepare_write(db_path)
    try:
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

        enum_refs = conn.execute(
            "SELECT fact_id FROM facts WHERE field_path IN ('experience.type', 'skill.category')"
            f" AND fact_id IN ({placeholders})", tuple(source_fact_ids)
        ).fetchall()
        if enum_refs:
            raise ValidationError([
                "以下 source-fact-ids 是分类枚举，导出不提供 factId，不能作为表达引用："
                + ", ".join(row[0] for row in enum_refs)
            ])

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

def _group_export_facts(facts: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for fact in facts:
        groups.setdefault(fact["field_path"], []).append(fact)
    return groups


def _fact_value_record(facts: list[dict], fact_ids: dict) -> dict:
    """旧字段形状保留默认值，其余语言作为可引用的事实一并导出。"""
    priority = {"zh-CN": 0, "en": 1, None: 2, "": 2}
    ordered = sorted(facts, key=lambda fact: (
        priority.get(fact["locale"], 3), fact["locale"] or "", fact["fact_id"]
    ))
    locales: set[str | None] = set()
    variants = []
    for fact in ordered:
        locale = fact["locale"] or None
        if locale in locales:
            raise ValidationError([f"{fact['field_path']}: 同一字段不允许重复 locale"])
        locales.add(locale)
        record = {"factId": fact["fact_id"], "value": json.loads(fact["value"])}
        if locale:
            record["locale"] = locale
        fact_ids[fact["fact_id"]] = True
        variants.append(record)
    record = variants[0]
    if len(variants) > 1:
        record["alternatives"] = variants[1:]
    return record


def build_snapshot(conn: sqlite3.Connection) -> dict:
    """多次查询共用一个读事务，防止导出不同修订的混合快照。"""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        return _build_snapshot_in_transaction(conn)
    finally:
        if owns_transaction:
            conn.rollback()


def _build_snapshot_in_transaction(conn: sqlite3.Connection) -> dict:
    """把库中 confirmed 事实组装为契约 Snapshot。失败抛出 ValidationError。"""
    errors: list[str] = []
    rows = conn.execute(
        "SELECT e.entity_id, e.entity_type, f.fact_id, f.field_path, f.value, f.locale"
        " FROM facts f JOIN entities e ON e.entity_id = f.entity_id"
        " WHERE f.status = 'confirmed'"
        " ORDER BY e.entity_id, f.field_path, f.locale, f.fact_id"
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
        for fp, variants in _group_export_facts(person["facts"]).items():
            if fp.startswith("person.fullName."):
                full_name[fp.split(".")[-1]] = _fact_value_record(variants, fact_ids)
            elif fp.startswith("person.contact."):
                contact[fp.split(".")[-1]] = _fact_value_record(variants, fact_ids)
            elif fp.startswith("person.location."):
                location[fp.split(".")[-1]] = _fact_value_record(variants, fact_ids)
            else:
                errors.append(f"person 实体包含未知字段：{fp}（{person['id']}）")
        snapshot_person.update(fullName=full_name, contact=contact, location=location)

    # education / experiences / skills / awards
    def _build_records(items: list, entity_type: str) -> list:
        records = []
        for entity_id, facts in items:
            record: dict = {"id": entity_id}
            narratives: list = []
            has_type = False
            for fp, variants in _group_export_facts(facts).items():
                fact = variants[0]
                if entity_type == "experience" and fp == "experience.type":
                    if len(variants) != 1:
                        errors.append(f"{entity_id}: experience.type 只能有一条确认事实")
                    value = json.loads(fact["value"])
                    if value not in EXPERIENCE_TYPES:
                        errors.append(
                            f"{entity_id}.experience.type: 非法值 {value!r}"
                            f"（需 ∈ {EXPERIENCE_TYPES}）"
                        )
                    record["type"] = value
                    has_type = True
                elif entity_type == "skill" and fp == "skill.category":
                    if len(variants) != 1:
                        errors.append(f"{entity_id}: skill.category 只能有一条确认事实")
                    value = json.loads(fact["value"])
                    if value not in SKILL_CATEGORIES:
                        errors.append(
                            f"{entity_id}.skill.category: 非法值 {value!r}"
                            f"（需 ∈ {SKILL_CATEGORIES}）"
                        )
                    record["category"] = value
                elif entity_type == "experience" and FACT_SLUG_RE.match(fp):
                    for variant in variants:
                        if not variant["locale"]:
                            errors.append(f"{variant['fact_id']}: narrative 事实（{fp}）必须提供 locale")
                        narratives.append({
                            "factId": variant["fact_id"],
                            "text": json.loads(variant["value"]),
                            "locale": variant["locale"],
                        })
                        fact_ids[variant["fact_id"]] = True
                elif fp in FIELD_PATH_TO_EXPORT_KEY:
                    record[FIELD_PATH_TO_EXPORT_KEY[fp]] = _fact_value_record(
                        variants, fact_ids
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
    import tempfile
    from source_integrity import scan_source_integrity

    if not db_path.exists():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 1
    conn, _backup = _prepare_write(db_path)
    temporary_path: Path | None = None
    try:
        snapshot = build_snapshot(conn)
        errors = validate_snapshot(snapshot)
        orphaned = conn.execute(
            "SELECT COUNT(*) FROM facts f WHERE f.status='confirmed' AND NOT EXISTS "
            "(SELECT 1 FROM fact_sources fs WHERE fs.fact_id=f.fact_id)"
        ).fetchone()[0]
        if orphaned:
            errors.append(f"{orphaned} 条确认事实没有来源，不允许导出")
        if errors:
            raise ValidationError(errors)
        integrity = scan_source_integrity(conn)
        if any(integrity[key] for key in ("changedSources", "missingSources", "unreadableSources")):
            print(
                f"来源核验提示：变化 {integrity['changedSources']}，缺失 {integrity['missingSources']}，"
                f"无法读取 {integrity['unreadableSources']}；"
                f"{integrity['factsWithoutVerifiedSources']} 条确认事实没有指纹匹配的来源。"
                "本次导出保留本人确认的事实，结构校验通过不代表来源仍与登记一致。",
                file=sys.stderr,
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
        conn.execute(
            "INSERT INTO profile_meta (key, value) VALUES ('last_exported_at', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (snapshot["exportedAt"],),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    print(
        f"已导出 Snapshot：{output}"
        f"（profileRevision={snapshot['profileRevision']}）"
    )
    return 0


# ---------------------------------------------------------------------------
# Snapshot 契约校验（validate-export 与 export 内部共用）
# ---------------------------------------------------------------------------

def validate_snapshot(snapshot: object) -> list[str]:
    # 公共契约随程序离线分发，两端副本一致性由合成测试检查。
    from snapshot_contract import validate_snapshot_contract

    return validate_snapshot_contract(snapshot)


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

    p_vl = sub.add_parser("validate-links", help="校验补挂来源文件（给已确认事实加证据）")
    p_vl.add_argument("links", type=Path)
    p_vl.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_al = sub.add_parser("apply-links", help="按人工决定补挂来源（写库前自动备份）")
    p_al.add_argument("links", type=Path)
    p_al.add_argument("decisions", type=Path)
    p_al.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_rs = sub.add_parser("relocate-sources", help="资料文件挪位置后同步登记路径（默认预览）")
    p_rs.add_argument("mapping", type=Path, help='JSON：{"moves": {旧绝对路径: 新绝对路径}}')
    p_rs.add_argument("--apply", action="store_true", help="确认写库（写库前自动备份）")
    p_rs.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

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
        # 建好标准目录；没有另行配置素材目录时，材料放进项目内的 materials/
        default_materials = [(PROFILE_DB_DIR / "materials").resolve()]
        dirs = ("staging", "review", "exports") + (
            ("materials",) if ALLOWED_SOURCE_DIRS == default_materials else ()
        )
        for d in dirs:
            (PROFILE_DB_DIR / d).mkdir(parents=True, exist_ok=True)
        existed = args.db.exists()
        conn = connect(args.db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = current_schema_version(conn)
            backup = backup_db(args.db) if existed and current < DB_SCHEMA_VERSION else None
            migrate(conn)
            ensure_meta_row(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        note = f"，备份：{backup.name}" if backup else ""
        print(f"数据库已初始化：{args.db}{note}")
        print("素材目录：" + "、".join(str(d) for d in ALLOWED_SOURCE_DIRS))
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

    if args.command == "relocate-sources":
        return cmd_relocate_sources(args.mapping, args.db, args.apply)

    if args.command == "validate-links":
        return cmd_validate_links(args.links, args.db)

    if args.command == "apply-links":
        return cmd_apply_links(args.links, args.decisions, args.db)

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
