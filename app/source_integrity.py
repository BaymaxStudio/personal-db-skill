#!/usr/bin/env python3
"""核对已登记来源的文件指纹，不修改来源登记或事实。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        initial = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        final = os.fstat(stream.fileno())
    current = path.stat()
    def signature(stat: os.stat_result) -> tuple[int, ...]:
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if signature(initial) != signature(final) or signature(final) != signature(current):
        raise OSError("核验期间文件发生变化，请重试")
    return digest.hexdigest()


def scan_source_integrity(conn: sqlite3.Connection) -> dict:
    """读取调用方的同一数据库快照，返回无材料正文和完整路径的核验结果。"""
    references: dict[str, set[str]] = {}
    for fact_id, source_id in conn.execute(
        "SELECT fs.fact_id, fs.source_id FROM fact_sources fs "
        "JOIN facts f USING(fact_id) WHERE f.status='confirmed'"
    ):
        references.setdefault(fact_id, set()).add(source_id)
    confirmed_ids = {row[0] for row in conn.execute("SELECT fact_id FROM facts WHERE status='confirmed'")}
    by_source: dict[str, set[str]] = {}
    for fact_id, source_ids in references.items():
        for source_id in source_ids:
            by_source.setdefault(source_id, set()).add(fact_id)

    items: list[dict] = []
    for source_id, absolute_path, expected_digest in conn.execute(
        "SELECT source_id, absolute_path, sha256 FROM sources ORDER BY source_id"
    ):
        path = Path(absolute_path)
        try:
            actual_digest = _file_digest(path)
        except FileNotFoundError:
            status = "missing"
        except OSError:
            status = "unreadable"
        else:
            status = "verified" if actual_digest == expected_digest else "changed"
        items.append({
            "sourceId": source_id,
            "file": path.name,
            "integrity": status,
            "confirmedFacts": len(by_source.get(source_id, set())),
        })

    verified = {item["sourceId"] for item in items if item["integrity"] == "verified"}
    problematic = {item["sourceId"] for item in items if item["integrity"] != "verified"}
    return {
        "checkedSources": len(items),
        "changedSources": sum(item["integrity"] == "changed" for item in items),
        "missingSources": sum(item["integrity"] == "missing" for item in items),
        "unreadableSources": sum(item["integrity"] == "unreadable" for item in items),
        "affectedConfirmedFacts": sum(bool(ids & problematic) for ids in references.values()),
        "factsWithoutVerifiedSources": sum(not (references.get(fid, set()) & verified) for fid in confirmed_ids),
        "sources": items,
    }


def check_database(db_path: Path) -> dict:
    """只读核验数据库结构、事实来源和已登记文件。"""
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")
        result = scan_source_integrity(conn)
        result["databaseIntegrity"] = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        result["foreignKeyErrors"] = [list(row) for row in conn.execute("PRAGMA foreign_key_check")]
        result["confirmedWithoutSources"] = conn.execute(
            "SELECT COUNT(*) FROM facts f WHERE f.status='confirmed' AND NOT EXISTS "
            "(SELECT 1 FROM fact_sources fs WHERE fs.fact_id=f.fact_id)"
        ).fetchone()[0]
        return result
    finally:
        conn.rollback()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读核对个人资料库与来源文件；异常时返回 1")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parent / "data/career_profile.sqlite3")
    parser.add_argument("--json", action="store_true", help="输出结构化核验结果")
    args = parser.parse_args(argv)
    try:
        report = check_database(args.db)
    except (OSError, sqlite3.Error) as exc:
        parser.exit(2, f"无法核验：{exc}\n")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"已核验 {report['checkedSources']} 份来源；"
            f"变化 {report['changedSources']}，缺失 {report['missingSources']}，"
            f"无法读取 {report['unreadableSources']}；"
            f"{report['factsWithoutVerifiedSources']} 条确认事实没有指纹匹配的来源。"
        )
        for item in report["sources"]:
            if item["integrity"] != "verified":
                print(f"  {item['sourceId']}：{item['integrity']} · {item['file']}")
    failed = (
        report["databaseIntegrity"] != ["ok"] or report["foreignKeyErrors"]
        or report["confirmedWithoutSources"] or report["changedSources"]
        or report["missingSources"] or report["unreadableSources"]
    )
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
