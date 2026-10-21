#!/usr/bin/env bash
# 启动 ComfyUI（常驻 127.0.0.1:8188）并等待就绪。传 --restart 先结束旧进程（AI Studio 自动恢复时调用）。
set -uo pipefail
source "$(dirname "$0")/env.sh"
running() { [ -f "$RUN_DIR/comfyui.pid" ] && kill -0 "$(cat "$RUN_DIR/comfyui.pid")" 2>/dev/null; }

if [ "${1:-}" = "--restart" ]; then
  if running; then
    log "结束 ComfyUI（pid $(cat "$RUN_DIR/comfyui.pid")）"
    kill "$(cat "$RUN_DIR/comfyui.pid")" 2>/dev/null
    for i in $(seq 1 15); do running || break; sleep 1; done
    running && kill -9 "$(cat "$RUN_DIR/comfyui.pid")" 2>/dev/null
  fi
  pkill -9 -f "$COMFY_DIR/main.py" 2>/dev/null; pkill -9 -f "main.py --listen 127.0.0.1 --port 8188" 2>/dev/null
  rm -f "$RUN_DIR/comfyui.pid"; sleep 2
fi

if running; then log "ComfyUI 已在运行"; else
  log "启动 ComfyUI → 127.0.0.1:8188（日志 $LOG_DIR/comfyui.log）"
  cd "$COMFY_DIR"
  nohup "$COMFY_PY" main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch \
    --output-directory "$BASE/data/comfy-output" >> "$LOG_DIR/comfyui.log" 2>&1 < /dev/null &
  echo $! > "$RUN_DIR/comfyui.pid"
fi
for i in $(seq 1 120); do curl -sf http://127.0.0.1:8188/system_stats >/dev/null && break; sleep 2; done
if curl -sf http://127.0.0.1:8188/system_stats >/dev/null; then log "ComfyUI 就绪"; else
  echo "⚠️ ComfyUI 尚未就绪，请查看 $LOG_DIR/comfyui.log"; exit 1; fi
