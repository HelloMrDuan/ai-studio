#!/usr/bin/env bash
set -u

PID_FILE=/root/autodl-tmp/ai-studio/logs/gemma-llama-server.pid
PID=$(cat "$PID_FILE" 2>/dev/null || true)

terminate_pid() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    # start_gemma 使用 start_new_session=True，优先终止整个进程组。
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  fi
}

terminate_pid "$PID"
while read -r process_id; do
  [[ "$process_id" == "$PID" ]] || terminate_pid "$process_id"
done < <(pgrep -f '[l]lama-server.*(--port[ =]6006|gemma)' 2>/dev/null || true)

for _ in $(seq 1 120); do
  alive=false
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    alive=true
  fi
  if pgrep -f '[l]lama-server.*(--port[ =]6006|gemma)' >/dev/null 2>&1; then
    alive=true
  fi
  [[ "$alive" == false ]] && break
  sleep 1
done

if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
  kill -KILL -- "-$PID" 2>/dev/null || kill -KILL "$PID" 2>/dev/null || true
fi
pkill -KILL -f '[l]lama-server.*(--port[ =]6006|gemma)' 2>/dev/null || true
rm -f "$PID_FILE"

python3 - <<'PY'
import socket
import time

deadline = time.time() + 20
while time.time() < deadline:
    sock = socket.socket()
    sock.settimeout(1)
    try:
        code = sock.connect_ex(('127.0.0.1', 6006))
    finally:
        sock.close()
    if code != 0:
        print('Gemma llama-server 已停止')
        raise SystemExit(0)
    time.sleep(1)
raise SystemExit('Gemma 进程已终止，但 6006 端口仍未释放')
PY
