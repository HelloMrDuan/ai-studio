#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/root/autodl-tmp/ai-studio/platform-v2
PY="${XIAODUAN_PYTHON:-/root/autodl-tmp/envs/ai-studio-platform-v2/bin/python}"
LOG=/root/autodl-tmp/ai-studio/logs/xiaoduan-temporal-worker.log
PID=/root/autodl-tmp/ai-studio/logs/xiaoduan-temporal-worker.pid

mkdir -p "$(dirname "$LOG")"
cd "$ROOT"

if ! (echo > /dev/tcp/127.0.0.1/7233) >/dev/null 2>&1; then
  echo "Temporal Server 127.0.0.1:7233 未就绪" >&2
  exit 2
fi

OLD_PID=$(cat "$PID" 2>/dev/null || true)
if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
  echo "V3 Temporal Worker 已运行，PID=$OLD_PID"
  exit 0
fi
rm -f "$PID"

nohup env \
  TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-127.0.0.1:7233}" \
  TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-default}" \
  XIAODUAN_TEMPORAL_TASK_QUEUE="${XIAODUAN_TEMPORAL_TASK_QUEUE:-xiaoduan-v3-production}" \
  "$PY" scripts/v3_temporal_worker.py \
  > "$LOG" 2>&1 </dev/null &
echo $! > "$PID"

sleep 2
if kill -0 "$(cat "$PID")" 2>/dev/null; then
  echo "V3 Temporal Worker 已启动，PID=$(cat "$PID")"
  echo "日志：$LOG"
  exit 0
fi

echo "V3 Temporal Worker 启动失败：" >&2
tail -n 120 "$LOG" >&2 || true
exit 1
