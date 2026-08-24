#!/usr/bin/env bash
set -u

PID=/root/autodl-tmp/ai-studio/logs/platform-v2.pid
OLD_PID=$(cat "$PID" 2>/dev/null || true)

if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
  kill "$OLD_PID"
  sleep 2
  echo "平台已停止：$OLD_PID"
else
  echo "平台未运行"
fi
rm -f "$PID"
