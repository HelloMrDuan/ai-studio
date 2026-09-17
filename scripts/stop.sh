#!/usr/bin/env bash
set -u

PID=/root/autodl-tmp/ai-studio/logs/xiaoduan-studio-v3.pid
OLD_PID=$(cat "$PID" 2>/dev/null || true)

if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
  kill "$OLD_PID"
  sleep 2
  echo "xiaoduan映画 V3 已停止：$OLD_PID"
else
  echo "xiaoduan映画 V3 未运行"
fi
rm -f "$PID"
