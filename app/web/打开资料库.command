#!/bin/bash
# 个人资料库 —— 双击启动本地查看器并自动打开浏览器。
# 服务在后台常驻，这个窗口可以直接关掉，不会把服务一起关掉。
#
# 这个启动器会自己定位到所在目录，不写死任何路径：
# 把整个项目搬到别处，双击它照样能用。

PORT=8733
# 脚本所在目录就是 web/，serve.py 与本文件同级
WEB_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${TMPDIR:-/tmp}/personal-db-viewer.log"

PY="$(command -v python3)"

# 只认自家服务：拿到 api/profile 且里面有 entities 字段才算数
probe() {
  curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/api/profile" | grep -q '"entities"'
}

if [ -z "$PY" ]; then
  echo "找不到 python3，启动不了。请先安装 Python 3。"
  exit 1
fi

if [ ! -f "$WEB_DIR/serve.py" ]; then
  echo "找不到项目文件：$WEB_DIR/serve.py"
  echo "请确认这个启动器和 serve.py 在同一个 web/ 目录里。"
  exit 1
fi

if probe; then
  echo "服务已经在跑了，直接开浏览器。"
else
  # 端口被别的程序占着，别硬抢
  if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
    echo "端口 $PORT 被别的程序占着，启动不了。"
    echo "看看是谁：lsof -i tcp:$PORT"
    exit 1
  fi

  echo "正在启动…"
  cd "$WEB_DIR" || exit 1
  nohup "$PY" serve.py --port "$PORT" > "$LOG" 2>&1 &

  # 等服务真的起来了再开浏览器，不然会先弹一个打不开的空页面
  for _ in $(seq 1 25); do
    probe && break
    sleep 0.4
  done

  if ! probe; then
    echo "启动失败，日志在这儿：$LOG"
    tail -20 "$LOG"
    exit 1
  fi
fi

open "http://127.0.0.1:$PORT"
echo ""
echo "已打开 http://127.0.0.1:$PORT"
echo "这个窗口可以关，服务继续在后台跑。"
echo "想停掉：在终端里执行  lsof -ti tcp:$PORT | xargs kill"
exit 0
