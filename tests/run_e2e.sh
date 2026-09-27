#!/usr/bin/env bash
# personal-db 端到端回归：单元测试 → 脚手架 → init → 候选 → 校验 → 决定 → 入库 → 补挂一手凭证
# → 挪动材料后同步路径 → 导出 → 查看器。全程在 tests/.demo-project/ 里跑合成数据，不碰任何真实库。
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJ="$SKILL_DIR/tests/.demo-project"
PY="$(command -v python3)"
export PYTHONDONTWRITEBYTECODE=1

sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}';
  else sha256sum "$1" | awk '{print $1}'; fi
}

echo "== 0. 单元测试 =="
"$PY" -m unittest discover -s "$SKILL_DIR/tests" -q 2>&1 >/dev/null | grep -E "^(Ran|OK|FAILED)"

echo "== 1. 脚手架：把 app/ 复制成临时项目 =="
rm -rf "$PROJ"
mkdir -p "$PROJ"
cp -R "$SKILL_DIR/app/." "$PROJ/"
unset PERSONAL_DB_MATERIALS

echo "== 2. init =="
cd "$PROJ"
"$PY" career_profile.py init

echo "== 3. 放入合成材料（简历、成绩单、本人补充陈述） =="
cp "$SKILL_DIR/examples/demo-material.txt" "$SKILL_DIR/examples/demo-transcript.txt" \
   "$SKILL_DIR/examples/demo-self-statement.md" "$PROJ/materials/"

echo "== 4. 生成候选与决定文件 =="
mkdir -p staging review
fill() {
  sed -e "s#__PROJECT_ROOT__#$PROJ#g" \
      -e "s#__SHA_RESUME__#$(sha256 "$PROJ/materials/demo-material.txt")#g" \
      -e "s#__SHA_TRANSCRIPT__#$(sha256 "$PROJ/materials/demo-transcript.txt")#g" \
      -e "s#__SHA_STATEMENT__#$(sha256 "$PROJ/materials/demo-self-statement.md")#g" "$1"
}
fill "$SKILL_DIR/examples/demo-candidates.template.json" > staging/candidates-demo.json
cp "$SKILL_DIR/examples/demo-decisions.template.json" review/decisions-demo.json

echo "== 5. validate-candidates（应通过） =="
"$PY" career_profile.py validate-candidates staging/candidates-demo.json

echo "== 6. 负向测试：坏 excerpt 必须被拒 =="
sed 's#"excerpt": "示例大学"#"excerpt": "不存在的字"#' staging/candidates-demo.json > staging/candidates-bad.json
if "$PY" career_profile.py validate-candidates staging/candidates-bad.json >/dev/null 2>&1; then
  echo "FAIL: 坏 excerpt 竟然通过了校验"; exit 1
else
  echo "OK: 坏 excerpt 被正确拒绝"
fi

echo "== 7. apply-decisions =="
"$PY" career_profile.py apply-decisions staging/candidates-demo.json review/decisions-demo.json

echo "== 8. 补挂一手凭证：GPA 等事实原来只引用简历，补挂成绩单 =="
fill "$SKILL_DIR/examples/demo-links.template.json" > staging/links-demo.json
cp "$SKILL_DIR/examples/demo-links-decisions.template.json" review/decisions-links-demo.json
"$PY" career_profile.py validate-links staging/links-demo.json
"$PY" career_profile.py apply-links staging/links-demo.json review/decisions-links-demo.json

echo "== 9. 挪动材料后同步登记路径 =="
mkdir -p materials/成绩单
mv materials/demo-transcript.txt materials/成绩单/demo-transcript.txt
printf '{"moves": {"%s": "%s"}}\n' "$PROJ/materials/demo-transcript.txt" \
  "$PROJ/materials/成绩单/demo-transcript.txt" > staging/moves-demo.json
"$PY" career_profile.py relocate-sources staging/moves-demo.json --apply

echo "== 10. export + validate-export =="
"$PY" career_profile.py export exports/career-profile.snapshot.json
"$PY" career_profile.py validate-export exports/career-profile.snapshot.json

echo "== 11. 断言快照内容 =="
"$PY" - "$PROJ/exports/career-profile.snapshot.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["person"]["fullName"]["zhCN"]["value"] == "张三", d["person"]
assert d["person"]["fullName"]["en"]["value"] == "Zhang San"
assert d["education"][0]["institution"]["value"] == "示例大学"
assert d["awards"][0]["name"]["value"] == "示例大学一等奖学金"
print("OK: 快照含 person/education/award 且值正确，profileRevision =", d["profileRevision"])
PYEOF

echo "== 12. 查看器 API 烟测 =="
PORT=8799
cd "$PROJ/web"
"$PY" serve.py --port "$PORT" >/dev/null 2>&1 &
SP=$!
trap 'kill "$SP" 2>/dev/null || true' EXIT
for _ in $(seq 1 25); do
  if curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/api/profile" | grep -q '"entities"'; then break; fi
  sleep 0.4
done
curl -s --noproxy '*' --max-time 3 "http://127.0.0.1:$PORT/api/profile" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
edu = next(e for e in d["entities"] if e["id"] == "education.bachelor")
gpa = next(f for f in edu["facts"] if f["field"] == "education.gpa")
assert [s["tier"] for s in gpa["sources"]] == ["primary", "secondary"], gpa["sources"]
assert gpa["sources"][0]["file"] == "demo-transcript.txt"
sections = {e["section"] for e in d["entities"]}
assert {"profile", "education", "internship", "research", "award", "skill"} <= sections, sections
print("OK: 查看器 entities=%d facts=%d sources=%d，GPA 的一手凭证排在最前" % (
    d["counts"]["entities"], d["counts"]["facts"], d["counts"]["sources"]))
'

echo
echo "全部通过。演示项目留在：$PROJ"
