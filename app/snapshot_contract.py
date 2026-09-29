"""以公共契约的离线副本校验快照；仅实现该契约实际使用的关键字。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


SCHEMA_PATH = Path(__file__).with_name("career-profile.schema.json")
DATE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")
SUPPORTED_KEYWORDS = frozenset({
    "$schema", "$id", "title", "description", "$defs", "$ref", "type", "required",
    "properties", "additionalProperties", "minProperties", "allOf", "if", "then", "not",
    "const", "enum", "minItems", "uniqueItems", "items", "minLength", "maxLength",
    "pattern", "format", "minimum", "maximum",
})


def _matches_type(value: object, kind: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, float) and value.is_integer()),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[kind]


def _date_time(value: str) -> bool:
    if not DATE_TIME_RE.fullmatch(value):
        return False
    if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _validate(value: object, rule: dict, root: dict, path: str) -> list[str]:
    unknown = set(rule) - SUPPORTED_KEYWORDS
    if unknown:
        return [f"{path}: 契约包含尚未支持的约束 {', '.join(sorted(unknown))}"]
    if "format" in rule and rule["format"] != "date-time":
        return [f"{path}: 契约包含尚未支持的 format {rule['format']}"]
    if "$ref" in rule:
        target = root
        for part in rule["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return _validate(value, target, root, path)
    errors: list[str] = []
    for branch in rule.get("allOf", []):
        errors.extend(_validate(value, branch, root, path))
    if "if" in rule and not _validate(value, rule["if"], root, path):
        errors.extend(_validate(value, rule.get("then", {}), root, path))
    if "not" in rule and not _validate(value, rule["not"], root, path):
        errors.append(f"{path}: 当前版本禁止此字段组合")
    kinds = rule.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_matches_type(value, kind) for kind in kinds):
            return errors + [f"{path}: 类型必须为 {' / '.join(kinds)}"]
    if "const" in rule and value != rule["const"]:
        errors.append(f"{path}: 必须等于 {rule['const']!r}")
    if "enum" in rule and value not in rule["enum"]:
        errors.append(f"{path}: 不是允许的枚举值")
    if isinstance(value, dict):
        for key in rule.get("required", []):
            if key not in value:
                errors.append(f"{path}: 缺少必填字段 {key}")
        if len(value) < rule.get("minProperties", 0):
            errors.append(f"{path}: 字段数量不足")
        properties = rule.get("properties", {})
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in properties:
                errors.extend(_validate(child, properties[key], root, child_path))
            elif rule.get("additionalProperties") is False:
                errors.append(f"{child_path}: 不是允许字段（禁止包含 {key}）")
            elif isinstance(rule.get("additionalProperties"), dict):
                errors.extend(_validate(child, rule["additionalProperties"], root, child_path))
    if isinstance(value, list):
        if len(value) < rule.get("minItems", 0):
            errors.append(f"{path}: 数组项目不足")
        if rule.get("uniqueItems") and any(item in value[:idx] for idx, item in enumerate(value)):
            errors.append(f"{path}: 不允许重复项")
        for idx, child in enumerate(value):
            if "items" in rule:
                errors.extend(_validate(child, rule["items"], root, f"{path}[{idx}]"))
    if isinstance(value, str):
        if len(value) < rule.get("minLength", 0) or len(value) > rule.get("maxLength", float("inf")):
            errors.append(f"{path}: 字符串长度超出契约范围")
        if "pattern" in rule and re.fullmatch(rule["pattern"], value) is None:
            errors.append(f"{path}: 格式不符（非法 stableId 或非法 locale 语言标签）")
        if rule.get("format") == "date-time" and not _date_time(value):
            errors.append(f"{path}: 必须是 ISO 8601 日期时间")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            errors.append(f"{path}: 数值小于 {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            errors.append(f"{path}: 数值大于 {rule['maximum']}")
    return errors


def validate_snapshot_contract(snapshot: object) -> list[str]:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"无法读取快照契约 {SCHEMA_PATH}: {exc}"]
    errors = _validate(snapshot, schema, schema, "snapshot")
    # 先通过结构校验，再访问引用；任何无效 JSON 类型都只返回错误。
    if errors:
        return errors
    entities = [snapshot["person"]]
    for key in ("education", "experiences", "skills", "awards"):
        entities.extend(snapshot[key])
    entity_ids: set[str] = set()
    expression_ids: set[str] = set()
    fact_ids: set[str] = set()

    def register(identifier: str, registry: set[str], path: str) -> None:
        if identifier in registry:
            label = "factId" if registry is fact_ids else "ID"
            errors.append(f"{path}: 重复 {label} {identifier}")
        registry.add(identifier)

    def collect(value: object, path: str) -> None:
        if isinstance(value, dict):
            if "factId" in value:
                register(value["factId"], fact_ids, path)
                if "alternatives" in value:
                    variants = [value, *value["alternatives"]]
                    locales = [item.get("locale") for item in variants]
                    if len(set(locales)) != len(locales):
                        errors.append(f"{path}: 同一字段不允许重复 locale")
            for key, child in value.items():
                collect(child, f"{path}.{key}")
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                collect(child, f"{path}[{idx}]")

    for entity in entities:
        register(entity["id"], entity_ids, "entity.id")
        collect(entity, entity["id"])
    for expression in snapshot["expressions"]:
        register(expression["id"], expression_ids, "expression.id")
        for fact_id in expression["sourceFactIds"]:
            if fact_id not in fact_ids:
                errors.append(f"{expression['id']}: sourceFactIds 引用不存在的事实 {fact_id}")
        for entity_id in expression.get("subjectIds", []):
            if entity_id not in entity_ids:
                errors.append(f"{expression['id']}: subjectIds 引用不存在的实体 {entity_id}")
    for identifier in entity_ids & fact_ids:
        errors.append(f"ID 在实体与事实中重复：{identifier}")
    for identifier in expression_ids & (entity_ids | fact_ids):
        errors.append(f"ID 在表达与实体或事实中重复：{identifier}")
    if sum(record.get("isHighest", False) for record in snapshot["education"]) > 1:
        errors.append("education 只能有一条 isHighest=true 的记录")
    return errors
