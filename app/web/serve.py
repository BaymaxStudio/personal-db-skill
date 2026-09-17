#!/usr/bin/env python3
"""个人资料库本地查看器。

只读服务：从 career_profile.sqlite3 组织出「实体 + 事实 + 证据」的 JSON，
供 web/index.html 展示与复制。全程只读打开数据库，不写入、不修改任何数据。

用法：
    python3 serve.py [--port 8733]
然后浏览器打开 http://127.0.0.1:8733
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

WEB_DIR = Path(__file__).resolve().parent
DB_PATH = WEB_DIR.parent / "data" / "career_profile.sqlite3"

TYPE_LABELS = {
    "person": "个人",
    "education": "教育",
    "experience": "经历",
    "skill": "技能",
    "award": "奖项",
}

TYPE_ORDER = ["person", "education", "experience", "game", "skill", "award"]

# 页面分组顺序（经历按性质拆成 科研/实习/学生工作/项目 四组，贴近网申表单分区）
GROUP_ORDER = [
    "个人", "教育",
    "科研与论文", "实习", "社会实践·学生工作", "项目与作品",
    "技能", "奖项",
]

# experience.type → 页面分组。游戏 / 开源 / 摄影都属 project，归入「项目与作品」。
EXPERIENCE_GROUP = {
    "research": "科研与论文",
    "professional": "实习",
    "leadership": "社会实践·学生工作",
    "project": "项目与作品",
}
DEFAULT_EXPERIENCE_GROUP = "项目与作品"

FIELD_LABELS = {
    "person.fullName.zhCN": "姓名（中）",
    "person.fullName.en": "姓名（英）",
    "person.fullName.familyNameEn": "姓（英）",
    "person.fullName.givenNameEn": "名（英）",
    "person.contact.email": "邮箱",
    "person.contact.phone": "电话",
    "person.location.currentCity": "现居城市",
    "education.institution": "院校",
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
    "experience.fact.topic": "主题",
    "experience.fact.overview": "项目介绍",
    "experience.fact.description": "项目描述",
    "experience.fact.insight": "一手判断",
    "experience.fact.recognition": "采信/获奖",
    "experience.fact.wukong": "黑神话：悟空",
    "experience.fact.expedition33": "33号远征队",
    "experience.fact.worldatwar": "使命召唤：战争世界",
    "experience.fact.playtime": "游戏时长",
    "experience.fact.playtime-e33": "33号远征队时长",
    "experience.fact.workflow": "协作方式",
    "experience.fact.url": "链接",
    "experience.fact.homeurl": "组织主页",
    "experience.fact.relation": "关系",
    "experience.fact.role": "身份",
    "experience.fact.phone": "联系电话",
    "experience.fact.prep": "准备阶段",
    "experience.fact.fieldwork": "调研执行",
    "experience.fact.method": "方法",
    "experience.fact.samples": "样本",
    "experience.fact.duty": "职责",
    "experience.fact.report": "成果",
    "experience.fact.score": "成绩",
    "experience.fact.chapters": "章节",
    "experience.fact.assessment": "评价",
    "skill.name": "名称",
    "skill.category": "类别",
    "skill.level": "水平",
    "skill.details": "说明",
    "award.name": "奖项",
    "award.date": "日期",
    "award.details": "说明",
}

EXPERIENCE_TYPES = {
    "professional": "实习工作",
    "research": "科研",
    "leadership": "学生工作",
    "project": "项目",
}

SKILL_CATEGORIES = {
    "language": "语言",
    "technical": "技术",
    "research": "研究",
    "qualification": "资格",
    "other": "其他",
}

# 长文本字段：页面上默认完整展开，复制时最常用
LONG_FIELDS = ("experience.fact.", "award.details", "skill.details")

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


def field_rank(field_path: str) -> int:
    return FIELD_PRIORITY.get(field_path, DEFAULT_FIELD_RANK)


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


def field_label(field_path: str) -> str:
    if field_path in FIELD_LABELS:
        return FIELD_LABELS[field_path]
    if field_path.startswith("experience.fact."):
        return "要点"
    return field_path.rsplit(".", 1)[-1]


def load_profile(include_rejected: bool = False) -> dict:
    """读取数据库，组织成前端需要的结构。"""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"找不到数据库：{DB_PATH}")

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
            "SELECT fs.fact_id, s.document_type, s.absolute_path, fs.excerpt "
            "FROM fact_sources fs JOIN sources s ON s.source_id = fs.source_id"
        ):
            sources.setdefault(row["fact_id"], []).append(
                {
                    "type": row["document_type"],
                    "file": Path(row["absolute_path"]).name,
                    "excerpt": row["excerpt"],
                }
            )

        meta_rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM profile_meta")}
        migration = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    finally:
        conn.close()

    grouped: dict[str, list[dict]] = {}
    for fact in facts:
        item = {
            "id": fact["fact_id"],
            "field": fact["field_path"],
            "label": field_label(fact["field_path"]),
            "value": decode_value(fact["value"]),
            "locale": fact["locale"] or "",
            "status": fact["status"],
            "sources": sources.get(fact["fact_id"], []),
        }
        grouped.setdefault(fact["entity_id"], []).append(item)

    result_entities = []
    for entity in entities:
        items = grouped.get(entity["entity_id"], [])
        # 按填表优先级排：名称/定位 → 贡献 → 方法 → 成果；同字段多语言相邻。
        items.sort(key=lambda x: (field_rank(x["field"]), locale_rank(x["locale"]), x["field"]))
        exp_type = next(
            (i["value"] for i in items if i["field"] == "experience.type"), None
        )
        result_entities.append(
            {
                "id": entity["entity_id"],
                "type": entity["entity_type"],
                "typeLabel": TYPE_LABELS.get(entity["entity_type"], entity["entity_type"]),
                "group": group_of(entity["entity_type"], exp_type),
                "name": entity["display_name"],
                "start": first_value(items, ("experience.startDate", "education.startDate", "award.date")),
                "subtype": subtype_label(entity["entity_type"], items),
                "facts": items,
            }
        )

    # 按页面分组排序；组内按开始时间倒序（新的在前），个人档案保持在最前
    ordered = []
    for g in GROUP_ORDER:
        bucket = [e for e in result_entities if e["group"] == g]
        if g == "个人":
            ordered.extend(bucket)
        else:
            bucket.sort(key=lambda e: e["start"] or "", reverse=True)
            ordered.extend(bucket)
    # 兜底：万一有分组外的实体，接在后面
    ordered.extend(e for e in result_entities if e not in ordered)

    return {
        "dbPath": str(DB_PATH),
        "schemaVersion": meta_rows.get("schema_version", ""),
        "dbSchemaVersion": migration["v"] if migration else None,
        "counts": {
            "entities": len(result_entities),
            "facts": len(facts),
            "sources": len({s["file"] for v in sources.values() for s in v}),
        },
        "entities": ordered,
    }


def first_value(items: list[dict], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        for item in items:
            if item["field"] == field:
                return item["value"]
    return None


def group_of(entity_type: str, exp_type: str | None) -> str:
    """实体归到哪个页面分组。经历按 experience.type 拆成四组。"""
    if entity_type == "experience":
        return EXPERIENCE_GROUP.get(exp_type, DEFAULT_EXPERIENCE_GROUP)
    return TYPE_LABELS.get(entity_type, entity_type)


def subtype_label(entity_type: str, items: list[dict]) -> str:
    """给经历/技能补一个中文小标签。"""
    if entity_type == "experience":
        for item in items:
            if item["field"] == "experience.type":
                return EXPERIENCE_TYPES.get(item["value"], item["value"])
    if entity_type == "skill":
        for item in items:
            if item["field"] == "skill.category":
                return SKILL_CATEGORIES.get(item["value"], item["value"])
    return ""


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

    server = HTTPServer((args.host, args.port), Handler)
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
