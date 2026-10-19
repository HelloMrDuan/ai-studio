#!/usr/bin/env bash
# 启动 ComfyUI（常驻 8188）和 AI Studio（6006）。llama-server 由 AI Studio 按需自动启停。
set -euo pipefail
source "$(dirname "$0")/env.sh"
running() { [ -f "$RUN_DIR/$1.pid" ] && kill -0 "$(cat "$RUN_DIR/$1.pid")" 2>/dev/null; }

if running comfyui; then log "ComfyUI 已在运行"; else
  log "启动 ComfyUI → 127.0.0.1:8188（日志 $LOG_DIR/comfyui.log）"
  cd "$COMFY_DIR"
  nohup "$COMFY_PY" main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch \
    --output-directory "$BASE/data/comfy-output" >> "$LOG_DIR/comfyui.log" 2>&1 &
  echo $! > "$RUN_DIR/comfyui.pid"
fi
for i in $(seq 1 90); do curl -sf http://127.0.0.1:8188/system_stats >/dev/null && break; sleep 2; done
curl -sf http://127.0.0.1:8188/system_stats >/dev/null && log "ComfyUI 就绪" || echo "⚠️ ComfyUI 尚未就绪，请查看 $LOG_DIR/comfyui.log"

if running studio; then log "AI Studio 已在运行"; else
  log "启动 AI Studio → 0.0.0.0:6006（日志 $LOG_DIR/studio.out）"
  cd "$APP_DIR"
  nohup "$APP_PY" -m uvicorn app.main:app --host 0.0.0.0 --port 6006 >> "$LOG_DIR/studio.out" 2>&1 &
  echo $! > "$RUN_DIR/studio.pid"
fi
sleep 3
curl -sf http://127.0.0.1:6006/api/meta >/dev/null && log "AI Studio 就绪（端口 6006）：AutoDL 点「自定义服务」；算家云在实例上「开放端口」6006" \
  || echo "⚠️ AI Studio 未就绪，请查看 $LOG_DIR/studio.out"
