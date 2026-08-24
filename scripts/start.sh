#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/root/autodl-tmp/ai-studio/platform-v2
PY=/root/autodl-tmp/envs/ai-studio-platform-v2/bin/python
LOG=/root/autodl-tmp/ai-studio/logs/platform-v2.log
PID=/root/autodl-tmp/ai-studio/logs/platform-v2.pid

mkdir -p "$(dirname "$LOG")"
cd "$ROOT"

OLD_PID=$(cat "$PID" 2>/dev/null || true)
if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
  echo "平台已经运行，PID：$OLD_PID"
  exit 0
fi

rm -f "$PID"
nohup "$PY" -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 6008 \
  > "$LOG" 2>&1 &
echo $! > "$PID"

for _ in $(seq 1 60); do
  if "$PY" - <<'PYREADY' >/dev/null 2>&1
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:6008/api/gpu/status', timeout=3) as response:
    raise SystemExit(0 if response.status == 200 else 1)
PYREADY
  then
    echo "平台已启动，PID：$(cat "$PID")"
    echo "默认 GPU 工作区由统一调度器切换到 Gemma。"
    echo "日志：$LOG"
    exit 0
  fi
  if ! kill -0 "$(cat "$PID")" 2>/dev/null; then
    echo "平台启动失败，最后日志：" >&2
    tail -n 120 "$LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "平台端口 6008 未在 60 秒内就绪，最后日志：" >&2
tail -n 120 "$LOG" >&2 || true
exit 1
