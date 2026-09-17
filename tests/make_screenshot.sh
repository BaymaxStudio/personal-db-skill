#!/usr/bin/env bash
# 用合成数据渲染查看器截图，产物落到 docs/viewer-demo.png。全程本地，无外部请求。
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJ="$SKILL_DIR/tests/.demo-project"
OUT="$SKILL_DIR/docs/viewer-demo.png"
PORT=8798
PY="$(command -v python3)"

bash "$SKILL_DIR/tests/run_e2e.sh" >/dev/null
mkdir -p "$SKILL_DIR/docs"

cd "$PROJ/web"
"$PY" serve.py --port "$PORT" >/dev/null 2>&1 &
SP=$!
trap 'kill "$SP" 2>/dev/null || true' EXIT
for _ in $(seq 1 25); do
  if curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/api/profile" | grep -q '"entities"'; then break; fi
  sleep 0.4
done

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ ! -x "$CHROME" ]; then
  CHROME="$(command -v chromium || command -v google-chrome || command -v chromium-browser || true)"
fi
if [ ! -x "$CHROME" ]; then echo "找不到 Chrome/Chromium，跳过截图（不视为失败）"; exit 2; fi

UD="$(mktemp -d)"
rm -f "$OUT"
"$CHROME" --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --user-data-dir="$UD" --no-first-run --no-default-browser-check \
  --window-size=1360,720 --screenshot="$OUT" "http://127.0.0.1:$PORT" >/dev/null 2>&1 &
CP=$!
for _ in $(seq 1 30); do [ -s "$OUT" ] && break; sleep 1; done
kill -9 "$CP" 2>/dev/null || true
rm -rf "$UD"
if [ -s "$OUT" ]; then echo "截图已生成：$OUT"; else echo "截图失败"; exit 1; fi
