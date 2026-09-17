#!/usr/bin/env bash
# personal-db 端到端回归：脚手架 → init → 候选 → 校验 → 决定 → 入库 → 导出 → 查看器。
# 全程在 tests/.demo-project/ 里跑合成数据，不碰任何真实库。
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJ="$SKILL_DIR/tests/.demo-project"
PY="$(command -v python3)"

sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}';
  else sha256sum "$1" | awk '{print $1}'; fi
}

echo "== 1. 脚手架：把 app/ 复制成临时项目 =="
rm -rf "$PROJ"
mkdir -p "$PROJ"
cp -R "$SKILL_DIR/app/." "$PROJ/"

echo "== 2. init =="
cd "$PROJ"
"$PY" career_profile.py init

echo "== 3. 放入合成材料 =="
cp "$SKILL_DIR/examples/demo-material.txt" "$PROJ/materials/demo-material.txt"
SHA="$(sha256 "$PROJ/materials/demo-material.txt")"

echo "== 4. 生成候选与决定文件 =="
mkdir -p staging review
sed -e "s#__PROJECT_ROOT__#$PROJ#g" -e "s#__SHA256__#$SHA#g" \
  "$SKILL_DIR/examples/demo-candidates.template.json" > staging/candidates-demo.json
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

echo "== 8. export + validate-export =="
"$PY" career_profile.py export exports/career-profile.snapshot.json
"$PY" career_profile.py validate-export exports/career-profile.snapshot.json

echo "== 9. 断言快照内容 =="
"$PY" - "$PROJ/exports/career-profile.snapshot.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["person"]["fullName"]["zhCN"]["value"] == "张三", d["person"]
assert d["person"]["fullName"]["en"]["value"] == "Zhang San"
assert d["education"][0]["institution"]["value"] == "示例大学"
assert d["awards"][0]["name"]["value"] == "示例大学一等奖学金"
print("OK: 快照含 person/education/award 且值正确，profileRevision =", d["profileRevision"])
PYEOF

echo "== 10. 查看器 API 烟测 =="
PORT=8799
cd "$PROJ/web"
"$PY" serve.py --port "$PORT" >/dev/null 2>&1 &
SP=$!
trap 'kill "$SP" 2>/dev/null || true' EXIT
for _ in $(seq 1 25); do
  if curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/api/profile" | grep -q '"entities"'; then break; fi
  sleep 0.4
done
curl -s --noproxy '*' --max-time 3 "http://127.0.0.1:$PORT/api/profile" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print("OK: 查看器 entities=%d facts=%d sources=%d" % (d["counts"]["entities"], d["counts"]["facts"], d["counts"]["sources"]))'

echo
echo "全部通过。演示项目留在：$PROJ"
