#!/usr/bin/env bash
# 停止全部服务（包括 llama-server）
source "$(dirname "$0")/env.sh"
for name in studio comfyui; do
  f=$RUN_DIR/$name.pid
  if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then
    log "停止 $name"; kill "$(cat "$f")"; sleep 2; kill -9 "$(cat "$f")" 2>/dev/null || true
  fi
  rm -f "$f"
done
pgrep -f "$LLAMA_DIR/build/bin/llama-server" >/dev/null && { log "停止 llama-server"; pkill -f "$LLAMA_DIR/build/bin/llama-server"; }
log "已停止"
