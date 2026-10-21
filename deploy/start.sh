#!/usr/bin/env bash
# 启动 ComfyUI（常驻 8188）和 AI Studio（.env 的 PORT，默认 6006）。llama-server 由 AI Studio 按需自动启停。
set -euo pipefail
source "$(dirname "$0")/env.sh"
running() { [ -f "$RUN_DIR/$1.pid" ] && kill -0 "$(cat "$RUN_DIR/$1.pid")" 2>/dev/null; }

bash "$(dirname "$0")/start_comfyui.sh" || true

SP=$(studio_port)
if running studio; then log "AI Studio 已在运行"; else
  log "启动 AI Studio → 0.0.0.0:$SP（日志 $LOG_DIR/studio.out）"
  cd "$APP_DIR"
  nohup "$APP_PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$SP" >> "$LOG_DIR/studio.out" 2>&1 &
  echo $! > "$RUN_DIR/studio.pid"
fi
sleep 3
curl -sf "http://127.0.0.1:$SP/login" >/dev/null && log "AI Studio 就绪（端口 $SP）：AutoDL 点「自定义服务」；算家云在实例上「开放端口 → 对外开放端口」" \
  || echo "⚠️ AI Studio 未就绪，请查看 $LOG_DIR/studio.out"
PW=$(grep -E "^ACCESS_PASSWORD=" "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- || true)
[ -n "$PW" ] && log "访问密码：$PW（在 $APP_DIR/.env 的 ACCESS_PASSWORD 修改，改完重启）"
