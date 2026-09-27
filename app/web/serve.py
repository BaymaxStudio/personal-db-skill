#!/usr/bin/env python3
"""个人资料库本地查看器。

只读服务：从 career_profile.sqlite3 组织出「分区 + 实体 + 事实 + 证据」的 JSON，
供 web/index.html 展示与复制。全程只读打开数据库，不写入、不修改任何数据。

用法：
    python3 serve.py [--port 8733]
然后浏览器打开 http://127.0.0.1:8733
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

WEB_DIR = Path(__file__).resolve().parent
DB_PATH = WEB_DIR.parent / "data" / "career_profile.sqlite3"
# 可选的本地配置（不进 Git）：给个人专属的内容加分区、字段名和排序，格式见 viewer-config.example.json
VIEWER_CONFIG_PATH = WEB_DIR / "viewer-config.json"

# 页面分区。顺序贴近常见网申表单：基本信息 → 教育 → 实习 → 科研 → 项目 → 校园 → 奖项 → 技能 → 证明人 → 其他。
# 「现成文案」只收没有挂到具体条目上的文案；挂了条目的文案跟着条目走。
SECTIONS = [
    {"id": "profile", "label": "基本信息", "en": "Profile"},
    {"id": "statements", "label": "现成文案", "en": "Statements"},
    {"id": "education", "label": "教育经历", "en": "Education"},
    {"id": "internship", "label": "实习经历", "en": "Internships"},
    {"id": "research", "label": "科研与论文", "en": "Research"},
    {"id": "project", "label": "项目与作品", "en": "Projects"},
    {"id": "campus", "label": "校园经历", "en": "Campus"},
    {"id": "award", "label": "获奖荣誉", "en": "Honors"},
    {"id": "skill", "label": "技能与证书", "en": "Skills"},
    {"id": "reference", "label": "证明人", "en": "References"},
    {"id": "other", "label": "其他", "en": "Other"},
]

ENTITY_SECTION = {
    "person": "profile",
    "education": "education",
    "award": "award",
    "skill": "skill",
}

# experience.type → 分区
EXPERIENCE_SECTION = {
    "professional": "internship",
    "research": "research",
    "project": "project",
    "leadership": "campus",
}

# 数据模型里没有单独类型、但网申表单单独成栏的内容，按实体 ID 前缀归区（先于 type 判断）。
# 证明人约定用 experience.reference-<姓名> 这样的实体 ID；其余前缀可在 viewer-config.json 里补。
SECTION_BY_ID_PREFIX = {
    "experience.reference-": "reference",
}

# 技能分类，按页面呈现顺序排列
SKILL_CATEGORIES = [
    {"id": "language", "label": "语言能力"},
    {"id": "research", "label": "研究方法"},
    {"id": "technical", "label": "技术工具"},
    {"id": "qualification", "label": "资格证书"},
    {"id": "other", "label": "其他"},
]

# 学位或专业里带这些字样的教育条目视为辅修，挂到同校的主修条目下
MINOR_PATTERN = re.compile(r"辅修|minor", re.IGNORECASE)

# 来源分级，按可信度从高到低排列；页面上每条事实的来源也按这个顺序展示。
SOURCE_TIERS = [
    {"id": "primary", "label": "一手", "hint": "机构出具的原始凭证：成绩单、证书、证明、offer 等"},
    {"id": "self", "label": "本人陈述", "hint": "本人撰写的补充陈述、总结等，没有第三方背书"},
    {"id": "secondary", "label": "二手", "hint": "简历等汇编材料"},
]
SOURCE_TIER_RANK = {t["id"]: i for i, t in enumerate(SOURCE_TIERS)}
# 分级只看来源 ID 和文件名：先认简历，再认本人陈述，其余视为一手凭证。
SECONDARY_SOURCE_PATTERN = re.compile(r"简历|履历|resume|(?<![a-z])cv(?![a-z])", re.IGNORECASE)
SELF_SOURCE_PATTERN = re.compile(r"本人|陈述|表述|补充|信息采集|总结")

FIELD_LABELS = {
    "person.fullName.zhCN": "中文姓名",
    "person.fullName.en": "英文姓名",
    "person.fullName.familyNameEn": "姓（拼音）",
    "person.fullName.givenNameEn": "名（拼音）",
    "person.contact.email": "邮箱",
    "person.contact.phone": "电话",
    "person.location.currentCity": "现居城市",
    "education.institution": "学校",
    "education.major": "专业",
    "education.degree": "学位",
    "education.startDate": "开始",
    "education.endDate": "结束",
    "education.gpa": "GPA",
    "education.averageScore": "平均分",
    "education.ranking": "排名",
    "education.location": "地点",
    "experience.type": "类型",
    "experience.title": "名称",
    "experience.organization": "机构",
    "experience.role": "角色",
    "experience.startDate": "开始",
    "experience.endDate": "结束",
    "experience.fact.topic": "题目",
    "experience.fact.overview": "项目介绍",
    "experience.fact.description": "工作描述",
    "experience.fact.insight": "一手判断",
    "experience.fact.recognition": "采信 / 获奖",
    "experience.fact.workflow": "协作方式",
    "experience.fact.url": "链接",
    "experience.fact.homeurl": "主页",
    "experience.fact.account": "账号",
    "experience.fact.gear": "拍摄设备",
    "experience.fact.theme": "题材",
    "experience.fact.department": "部门",
    "experience.fact.level": "结题等级",
    "experience.fact.relation": "关系",
    "experience.fact.role": "职务",
    "experience.fact.phone": "联系电话",
    "experience.fact.prep": "准备阶段",
    "experience.fact.fieldwork": "调研执行",
    "experience.fact.method": "研究方法",
    "experience.fact.samples": "样本与调研",
    "experience.fact.duty": "职责",
    "experience.fact.report": "成果",
    "experience.fact.score": "成绩",
    "experience.fact.chapters": "个人贡献",
    "experience.fact.assessment": "评价",
    "skill.name": "名称",
    "skill.category": "类别",
    "skill.level": "水平",
    "skill.details": "说明",
    "award.name": "奖项",
    "award.date": "日期",
    "award.details": "说明",
}

# 卡内字段呈现顺序：按「填表流程」排，而不是字段名字母序。
# 名称/定位 → 我的贡献 → 方法/执行 → 成果·采信·获奖，其余落到最后。
FIELD_PRIORITY = {
    "experience.fact.topic": 1,
    "experience.fact.overview": 2,
    "experience.fact.description": 3,
    "experience.fact.insight": 4,
    "experience.fact.role": 10,
    "experience.fact.duty": 11,
    "experience.fact.chapters": 12,
    "experience.fact.workflow": 13,
    "experience.fact.prep": 20,
    "experience.fact.fieldwork": 21,
    "experience.fact.method": 22,
    "experience.fact.samples": 23,
    "experience.fact.report": 30,
    "experience.fact.score": 31,
    "experience.fact.assessment": 32,
    "experience.fact.recognition": 33,
}
# 未列出的字段（教育 / 技能 / 奖项 / 游戏时长等）统一落到中段，组内仍按字段名稳定排序。
DEFAULT_FIELD_RANK = 500


def load_viewer_config() -> dict:
    """读取可选的 web/viewer-config.json；文件不存在时返回空配置。"""
    if not VIEWER_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(VIEWER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"viewer-config.json 读取失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("viewer-config.json 的根节点必须是 JSON 对象")
    return data


def build_sections(extra: list[dict]) -> list[dict]:
    """在默认分区里插入配置的额外分区；before 指定插在哪个分区前，缺省插在「其他」前。"""
    sections = [dict(s) for s in SECTIONS]
    for item in extra:
        if not isinstance(item, dict) or not item.get("id") or not item.get("label"):
            raise ValueError("viewer-config.json 的 extraSections 每项都要有 id 和 label")
        ids = [s["id"] for s in sections]
        at = ids.index(item.get("before", "other")) if item.get("before", "other") in ids else len(ids)
        sections.insert(at, {"id": item["id"], "label": item["label"], "en": item.get("en", "")})
    return sections


def field_rank(field_path: str, priority: dict[str, int] = FIELD_PRIORITY) -> int:
    return priority.get(field_path, DEFAULT_FIELD_RANK)


def locale_rank(locale: str) -> int:
    """同一字段的多语言版本：通用 → 中文 → 英文，成对相邻。"""
    if not locale:
        return 0
    return 1 if locale.startswith("zh") else 2


def decode_value(raw: str) -> str:
    """facts.value 存的是 JSON 字符串，取出真实文本。"""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return str(raw)
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def decode_list(raw: str | None) -> list[str]:
    """expressions.target_roles 存的是 JSON 数组；解析失败时按空列表处理。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return [str(raw)]
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def field_label(field_path: str, labels: dict[str, str] = FIELD_LABELS) -> str:
    if field_path in labels:
        return labels[field_path]
    if field_path.startswith("experience.fact."):
        return "要点"
    return field_path.rsplit(".", 1)[-1]


def load_profile(include_rejected: bool = False) -> dict:
    """读取数据库，组织成前端需要的结构。"""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"找不到数据库：{DB_PATH}")
    cfg = load_viewer_config()
    sections = build_sections(cfg.get("extraSections", []))
    prefixes = {**cfg.get("sectionPrefixes", {}), **SECTION_BY_ID_PREFIX}
    labels = {**FIELD_LABELS, **cfg.get("fieldLabels", {})}
    priority = {**FIELD_PRIORITY, **cfg.get("fieldPriority", {})}
    section_ids = [s["id"] for s in sections]

    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        entities = [
            dict(r) for r in conn.execute(
                "SELECT entity_id, entity_type, display_name FROM entities"
            )
        ]

        status_filter = "" if include_rejected else " AND f.status = 'confirmed'"
        facts = [
            dict(r) for r in conn.execute(
                "SELECT f.fact_id, f.entity_id, f.field_path, f.value, "
                "f.locale, f.status FROM facts f WHERE 1=1" + status_filter
            )
        ]

        sources = {}
        for row in conn.execute(
            "SELECT fs.fact_id, s.source_id, s.document_type, s.absolute_path, fs.locator, fs.excerpt "
            "FROM fact_sources fs JOIN sources s ON s.source_id = fs.source_id"
        ):
            file_name = Path(row["absolute_path"]).name
            sources.setdefault(row["fact_id"], []).append(
                {
                    "type": row["document_type"],
                    "file": file_name,
                    "tier": source_tier(row["source_id"], file_name),
                    "locator": row["locator"],
                    "excerpt": row["excerpt"],
                }
            )
        # 一手凭证排最前，其次本人陈述，简历最后
        for items in sources.values():
            items.sort(key=lambda s: (SOURCE_TIER_RANK[s["tier"]], s["file"]))

        expr_status = "" if include_rejected else " WHERE e.status = 'confirmed'"
        expressions = [
            dict(r) for r in conn.execute(
                "SELECT e.expression_id, e.purpose, e.locale, e.max_chars, "
                "e.target_roles, e.text, e.status FROM expressions e" + expr_status
            )
        ]
        expr_facts: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT expression_id, fact_id FROM expression_sources"
        ):
            expr_facts.setdefault(row["expression_id"], []).append(row["fact_id"])
        expr_subjects: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT expression_id, entity_id FROM expression_subjects"
        ):
            expr_subjects.setdefault(row["expression_id"], []).append(row["entity_id"])

        meta_rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM profile_meta")}
        migration = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    finally:
        conn.close()

    grouped: dict[str, list[dict]] = {}
    for fact in facts:
        item = {
            "id": fact["fact_id"],
            "field": fact["field_path"],
            "label": field_label(fact["field_path"], labels),
            "value": decode_value(fact["value"]),
            "locale": fact["locale"] or "",
            "status": fact["status"],
            "sources": sources.get(fact["fact_id"], []),
        }
        grouped.setdefault(fact["entity_id"], []).append(item)

    expr_by_entity: dict[str, list[dict]] = {}
    orphan_expressions: list[dict] = []
    for ex in expressions:
        item = {
            "id": ex["expression_id"],
            "purpose": ex["purpose"],
            "text": ex["text"],
            "locale": ex["locale"] or "",
            "maxChars": ex["max_chars"],
            "targetRoles": decode_list(ex["target_roles"]),
            "sourceFactIds": expr_facts.get(ex["expression_id"], []),
        }
        subjects = expr_subjects.get(ex["expression_id"], [])
        if subjects:
            for sid in subjects:
                expr_by_entity.setdefault(sid, []).append(item)
        else:
            orphan_expressions.append(item)

    result_entities = []
    for entity in entities:
        items = grouped.get(entity["entity_id"], [])
        # 按填表优先级排：名称/定位 → 贡献 → 方法 → 成果；同字段多语言相邻。
        items.sort(key=lambda x: (field_rank(x["field"], priority), locale_rank(x["locale"]), x["field"]))
        exp_type = first_value(items, ("experience.type",))
        result_entities.append(
            {
                "id": entity["entity_id"],
                "type": entity["entity_type"],
                "section": section_of(entity["entity_id"], entity["entity_type"], exp_type,
                                      prefixes, section_ids),
                "parent": None,
                "name": entity["display_name"],
                "start": first_value(items, ("experience.startDate", "education.startDate", "award.date")),
                "end": first_value(items, ("experience.endDate", "education.endDate")),
                "facts": items,
                "expressions": expr_by_entity.get(entity["entity_id"], []),
            }
        )

    for child_id, parent_id in find_parents(result_entities).items():
        next(e for e in result_entities if e["id"] == child_id)["parent"] = parent_id

    # 按分区排序；区内按开始时间倒序（新的在前），无日期的排在后面并保持入库顺序
    ordered = []
    for sid in section_ids:
        bucket = [e for e in result_entities if e["section"] == sid]
        bucket.sort(key=lambda e: e["start"] or "", reverse=True)
        ordered.extend(bucket)

    shown_sources = {
        s["file"] for f in facts for s in sources.get(f["fact_id"], [])
    }
    primary_backed = sum(
        1 for f in facts if any(s["tier"] == "primary" for s in sources.get(f["fact_id"], []))
    )
    return {
        "dbPath": str(DB_PATH),
        "schemaVersion": meta_rows.get("schema_version", ""),
        "dbSchemaVersion": migration["v"] if migration else None,
        "profileRevision": meta_rows.get("profile_revision"),
        "lastExportedAt": meta_rows.get("last_exported_at"),
        "counts": {
            "entities": len(result_entities),
            "facts": len(facts),
            "sources": len(shown_sources),
            "primaryBacked": primary_backed,
            "expressions": len(expressions),
        },
        "sections": sections,
        "sourceTiers": SOURCE_TIERS,
        "skillCategories": SKILL_CATEGORIES,
        "entities": ordered,
        "expressions": orphan_expressions,
    }


def first_value(items: list[dict], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        for item in items:
            if item["field"] == field:
                return item["value"]
    return None


def field_values(entity: dict, field: str) -> set[str]:
    """某字段在所有语言版本下的取值集合。"""
    return {f["value"] for f in entity["facts"] if f["field"] == field}


def source_tier(source_id: str, file_name: str) -> str:
    """来源材料属于哪一级：primary / self / secondary。"""
    text = f"{source_id} {file_name}"
    if SECONDARY_SOURCE_PATTERN.search(text):
        return "secondary"
    if "supplement" in source_id or SELF_SOURCE_PATTERN.search(file_name):
        return "self"
    return "primary"


def section_of(
    entity_id: str,
    entity_type: str,
    exp_type: str | None,
    prefixes: dict[str, str],
    section_ids: list[str],
) -> str:
    """实体归到哪个页面分区；配置里写了不存在的分区时落到「其他」。"""
    section = next((sec for pre, sec in prefixes.items() if entity_id.startswith(pre)), None)
    if section is None and entity_type == "experience":
        section = EXPERIENCE_SECTION.get(exp_type or "", "other")
    if section is None:
        section = ENTITY_SECTION.get(entity_type, "other")
    return section if section in section_ids else "other"


def is_minor(entity: dict) -> bool:
    texts = field_values(entity, "education.degree") | field_values(entity, "education.major")
    return any(MINOR_PATTERN.search(t) for t in texts)


def find_parents(entities: list[dict]) -> dict[str, str]:
    """找出从属关系：辅修挂到同校主修；链接落在某个主页之下的项目挂到该主页条目。

    只在同一分区内挂靠，页面上子条目随上级一起显示。
    """
    parents: dict[str, str] = {}

    educations = [e for e in entities if e["type"] == "education"]
    for minor in (e for e in educations if is_minor(e)):
        schools = field_values(minor, "education.institution")
        for major in educations:
            if major is not minor and not is_minor(major) and schools & field_values(major, "education.institution"):
                parents[minor["id"]] = major["id"]
                break

    homes = [
        (url.rstrip("/") + "/", e)
        for e in entities
        for url in field_values(e, "experience.fact.homeurl")
    ]
    for entity in entities:
        if entity["id"] in parents:
            continue
        for url in field_values(entity, "experience.fact.url"):
            home = next(
                (h for prefix, h in homes
                 if h is not entity and h["section"] == entity["section"] and url.startswith(prefix)),
                None,
            )
            if home:
                parents[entity["id"]] = home["id"]
                break
    return parents


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/profile":
            self._serve_profile()
            return
        if path in ("/", ""):
            path = "/index.html"
        target = WEB_DIR / path.lstrip("/").replace("..", "")
        if not target.exists() or not target.is_file():
            self.send_error(404, "not found")
            return
        self._serve_file(target)

    def _serve_profile(self) -> None:
        try:
            data = load_profile()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, target: Path) -> None:
        body = target.read_bytes()
        ctype = "text/html; charset=utf-8"
        if target.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="个人资料库本地查看器（只读）")
    parser.add_argument("--port", type=int, default=8733)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    # 用 ThreadingHTTPServer：浏览器会长期挂着一个 keep-alive 连接，
    # 单线程的 HTTPServer 会被这类连接堵死，之后所有请求（含启动脚本的探测）全部排队超时。
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"个人资料库查看器已启动：http://{args.host}:{args.port}")
    print(f"数据库：{DB_PATH}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
