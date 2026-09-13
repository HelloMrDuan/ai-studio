#!/usr/bin/env bash
set -Eeuo pipefail

TEMPORAL="${TEMPORAL_BIN:-/root/autodl-tmp/bin/temporal}"
DATA_DIR="${TEMPORAL_DATA_DIR:-/root/autodl-tmp/ai-studio/data/temporal}"
LOG="${TEMPORAL_LOG:-/root/autodl-tmp/ai-studio/logs/temporal-dev.log}"
PIDFILE="${TEMPORAL_PIDFILE:-/root/autodl-tmp/ai-studio/logs/temporal-dev.pid}"
UI_PORT="${TEMPORAL_UI_PORT:-8233}"
DB="$DATA_DIR/temporal.sqlite"

if [[ ! -x "$TEMPORAL" ]]; then
  echo "Temporal CLI 不存在：$TEMPORAL" >&2
  echo "先执行：bash scripts/install_temporal_cli.sh" >&2
  exit 2
fi

mkdir -p "$DATA_DIR" "$(dirname "$LOG")"

if (echo > /dev/tcp/127.0.0.1/7233) >/dev/null 2>&1; then
  echo "Temporal Server 已处于 READY"
  exit 0
fi

OLD_PID=$(cat "$PIDFILE" 2>/dev/null || true)
if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
  echo "Temporal Server 正在启动，PID=$OLD_PID"
  exit 0
fi
rm -f "$PIDFILE"

nohup "$TEMPORAL" server start-dev \
  --ui-port "$UI_PORT" \
  --db-filename "$DB" \
  > "$LOG" 2>&1 </dev/null &
echo $! > "$PIDFILE"

for _ in $(seq 1 90); do
  if (echo > /dev/tcp/127.0.0.1/7233) >/dev/null 2>&1; then
    echo "Temporal Server 已启动，PID=$(cat "$PIDFILE")"
    echo "Server: 127.0.0.1:7233"
    echo "UI: http://127.0.0.1:${UI_PORT}"
    echo "日志：$LOG"
    exit 0
  fi
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Temporal Server 启动失败：" >&2
    tail -n 120 "$LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "Temporal Server 未在 90 秒内 READY" >&2
tail -n 120 "$LOG" >&2 || true
exit 1
