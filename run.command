#!/bin/bash
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$ROOT/backend"
COLLECTOR_APP="$ROOT/TouchBridge/TouchBridge.app"
COLLECTOR_BIN="$COLLECTOR_APP/Contents/MacOS/TouchBridge"
PYTHON="$BACKEND/venv/bin/python3"
WEB_PORT=8765
OSC_PORT=9000
LOG=/tmp/touchbridge_rpe.log
LAUNCH_LOG=/tmp/touchbridge_launcher.log

exec >>"$LAUNCH_LOG" 2>&1
echo ""
echo "=== $(date '+%Y-%m-%d %H:%M:%S') TouchBridge RPE launch ==="

notify() {
  /usr/bin/osascript -e "display notification \"$1\" with title \"TouchBridge RPE\"" >/dev/null 2>&1 || true
}

fail() {
  echo "ERROR: $1"
  /usr/bin/osascript -e "display dialog \"TouchBridge RPE 启动失败：\\n$1\\n\\n日志：$LAUNCH_LOG\" buttons {\"好\"} default button 1 with icon stop" >/dev/null 2>&1 || true
  exit 1
}

if [ ! -x "$COLLECTOR_BIN" ]; then
  command -v swiftc >/dev/null 2>&1 || {
    /usr/bin/xcode-select --install >/dev/null 2>&1 || true
    fail "需要先安装 Xcode Command Line Tools，安装完成后再双击一次。"
  }
  echo "Building native trackpad collector..."
  (cd "$ROOT/TouchBridge" && /bin/bash build.sh) || fail "触控板采集器编译失败。"
fi

command -v python3 >/dev/null 2>&1 || fail "没有找到 Python 3。"
if [ ! -x "$PYTHON" ]; then
  echo "Creating Python environment..."
  python3 -m venv "$BACKEND/venv" || fail "Python 虚拟环境创建失败。"
fi

if ! "$PYTHON" -c 'import flask, flask_socketio' >/dev/null 2>&1; then
  echo "Installing Python dependencies..."
  "$PYTHON" -m pip install -r "$ROOT/requirements.txt" -q || fail "依赖安装失败，请检查网络。"
fi

if [ -f /tmp/touchbridge_rpe.pid ]; then
  old_pid="$(cat /tmp/touchbridge_rpe.pid 2>/dev/null || true)"
  if [ -n "$old_pid" ] && ps -p "$old_pid" -o command= 2>/dev/null | grep -F "$BACKEND/app.py" >/dev/null; then
    kill "$old_pid" 2>/dev/null || true
  fi
fi
for spec in "TCP:$WEB_PORT" "UDP:$OSC_PORT"; do
  old_pids="$(lsof -ti -i"$spec" 2>/dev/null || true)"
  [ -n "$old_pids" ] && kill $old_pids 2>/dev/null || true
done
sleep 0.5

echo "Starting web/OSC backend..."
cd "$BACKEND" || fail "找不到 backend 目录。"
TOUCHBRIDGE_NO_BROWSER=1 nohup "$PYTHON" app.py >"$LOG" 2>&1 &
backend_pid=$!
echo "$backend_pid" >/tmp/touchbridge_rpe.pid

ready=0
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$WEB_PORT/" >/dev/null 2>&1; then
    ready=1
    break
  fi
  kill -0 "$backend_pid" 2>/dev/null || break
  sleep 0.25
done
[ "$ready" -eq 1 ] || fail "网页后端没有启动，请查看 $LOG。"

old_collectors="$(pgrep -f "^$COLLECTOR_BIN$" 2>/dev/null || true)"
[ -n "$old_collectors" ] && kill $old_collectors 2>/dev/null || true
/usr/bin/open -g "$COLLECTOR_APP" || fail "无法打开触控板采集器。"
collector_ok=0
for _ in $(seq 1 20); do
  if pgrep -f "$COLLECTOR_BIN" >/dev/null 2>&1; then
    collector_ok=1
    break
  fi
  sleep 0.25
done
[ "$collector_ok" -eq 1 ] || fail "触控板采集器启动后立即退出，请检查输入监控权限。"

/usr/bin/open "http://127.0.0.1:$WEB_PORT/"
notify "已启动：触控板、OSC 和制谱网页均已就绪"
echo "READY backend=$backend_pid web=$WEB_PORT osc=$OSC_PORT"
exit 0
