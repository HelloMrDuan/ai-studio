#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/root/autodl-tmp/ai-studio/platform-v2
PY="${XIAODUAN_PYTHON:-/root/autodl-tmp/envs/ai-studio-platform-v2/bin/python}"
LOG=/root/autodl-tmp/ai-studio/logs/xiaoduan-edge-tts.log
PID=/root/autodl-tmp/ai-studio/logs/xiaoduan-edge-tts.pid
PORT="${XIAODUAN_TTS_PORT:-6011}"

mkdir -p "$(dirname "$LOG")"
cd "$ROOT"

if curl --noproxy '*' -fsS --connect-timeout 2 --max-time 3 \
  "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "Xiaoduan TTS 已处于 READY"
  exit 0
fi

if ! "$PY" -c 'import edge_tts' >/dev/null 2>&1; then
  echo "缺少 edge-tts，先执行：$PY -m pip install edge-tts" >&2
  exit 2
fi

OLD_PID=$(cat "$PID" 2>/dev/null || true)
if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
  echo "Xiaoduan TTS 正在启动，PID=$OLD_PID"
  exit 0
fi
rm -f "$PID"

nohup "$PY" -m uvicorn scripts.v3_edge_tts_server:app \
  --host 127.0.0.1 \
  --port "$PORT" \
  > "$LOG" 2>&1 </dev/null &
echo $! > "$PID"

for _ in $(seq 1 60); do
  if curl --noproxy '*' -fsS --connect-timeout 2 --max-time 3 \
    "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "Xiaoduan TTS 已启动，PID=$(cat "$PID")"
    echo "日志：$LOG"
    exit 0
  fi
  if ! kill -0 "$(cat "$PID")" 2>/dev/null; then
    echo "Xiaoduan TTS 启动失败：" >&2
    tail -n 100 "$LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "Xiaoduan TTS 未在 60 秒内 READY" >&2
tail -n 100 "$LOG" >&2 || true
exit 1
