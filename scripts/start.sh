#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/root/autodl-tmp/ai-studio/platform-v2
PY=/root/autodl-tmp/envs/ai-studio-platform-v2/bin/python
LOG=/root/autodl-tmp/ai-studio/logs/xiaoduan-studio-v3.log
PID=/root/autodl-tmp/ai-studio/logs/xiaoduan-studio-v3.pid
PORT=6008

mkdir -p "$(dirname "$LOG")"
cd "$ROOT"
BUILD_SHA=$(git rev-parse HEAD)

stop_pid() {
  local pid="$1"
  [[ -n "${pid:-}" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -9 "$pid" 2>/dev/null || true
}

# Stop the PID-file process first. Historically a stale uvicorn could survive
# while the PID file pointed elsewhere; that made the health probe hit old code
# and falsely report a successful restart.
OLD_PID=$(cat "$PID" 2>/dev/null || true)
if [[ -n "${OLD_PID:-}" ]]; then
  stop_pid "$OLD_PID"
fi
rm -f "$PID"

# Stop any orphaned copy of this exact app/port. This host is dedicated to the
# Xiaoduan workbench; do not touch unrelated uvicorn processes on other ports.
while read -r stale_pid; do
  [[ -n "${stale_pid:-}" ]] || continue
  [[ "$stale_pid" == "$$" ]] && continue
  stop_pid "$stale_pid"
done < <(pgrep -f "uvicorn app\.main:app.*--port ${PORT}" 2>/dev/null || true)

# Fail closed if some unknown process still owns 6008. Never accept a health
# response from an old listener as proof that the newly launched process works.
if "$PY" - <<PYPORT >/dev/null 2>&1
import socket
s = socket.socket()
s.settimeout(0.5)
try:
    ok = s.connect_ex(("127.0.0.1", ${PORT})) == 0
finally:
    s.close()
raise SystemExit(0 if ok else 1)
PYPORT
then
  echo "端口 ${PORT} 仍被未知进程占用，拒绝启动以避免命中旧代码：" >&2
  ss -lntp 2>/dev/null | grep ":${PORT}" >&2 || true
  exit 1
fi

: > "$LOG"
echo "XIAODUAN_WEB_BOOT build_sha=$BUILD_SHA root=$ROOT port=$PORT" | tee -a "$LOG"
XIAODUAN_BUILD_SHA="$BUILD_SHA" nohup "$PY" -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  >> "$LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID"

for _ in $(seq 1 60); do
  # New process must still be alive BEFORE health is accepted.
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    echo "xiaoduan映画 V3 新进程已退出，拒绝把旧 listener 当成启动成功。最后日志：" >&2
    tail -n 120 "$LOG" >&2 || true
    exit 1
  fi

  if XIAODUAN_EXPECTED_BUILD="$BUILD_SHA" "$PY" - <<'PYREADY' >/dev/null 2>&1
import json
import os
import urllib.request
expected = os.environ["XIAODUAN_EXPECTED_BUILD"]
with urllib.request.urlopen('http://127.0.0.1:6008/api/v3/health', timeout=3) as response:
    data = json.load(response)
    ok = response.status == 200 and data.get("build_sha") == expected
    raise SystemExit(0 if ok else 1)
PYREADY
  then
    echo "xiaoduan映画 V3 已启动，PID：$NEW_PID"
    echo "BUILD_SHA：$BUILD_SHA"
    echo "日志：$LOG"
    exit 0
  fi
  sleep 1
done

echo "xiaoduan映画 V3 端口 ${PORT} 未在 60 秒内以当前 BUILD_SHA 就绪，最后日志：" >&2
tail -n 120 "$LOG" >&2 || true
exit 1
