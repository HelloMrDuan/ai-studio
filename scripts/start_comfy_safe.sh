#!/usr/bin/env bash
set -Eeuo pipefail

COMFY="${COMFY_DIR:-/root/autodl-tmp/ai-studio/ComfyUI}"
PYTHON="${COMFY_PYTHON:-/root/autodl-tmp/envs/ai-studio-comfy/bin/python}"
LOG="${COMFY_LOG:-/root/autodl-tmp/ai-studio/logs/comfyui.log}"
PIDFILE="${COMFY_PIDFILE:-/root/autodl-tmp/ai-studio/logs/comfyui.pid}"
HOST="${COMFY_HOST:-0.0.0.0}"
PORT="${COMFY_PORT:-8188}"

mkdir -p "$(dirname "$LOG")"

if curl --noproxy '*' -fsS --connect-timeout 2 --max-time 3 \
  "http://127.0.0.1:${PORT}/system_stats" >/dev/null 2>&1; then
  echo "ComfyUI 已处于 READY"
  exit 0
fi

if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "ComfyUI 正在启动，PID=$PID"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

cd "$COMFY"
unset OMP_NUM_THREADS

echo >> "$LOG"
echo "===== $(date '+%F %T') Xiaoduan V3 safe ComfyUI start =====" >> "$LOG"

# H3 on AutoDL runs inside a much smaller cgroup memory limit than the host RAM
# reported by /proc/meminfo. Disable the host-sized pinned-memory budget and
# asynchronous weight offload so MiniMax H3 staging respects the container
# memory envelope. cache-none also prevents prior image/FaceID model caches from
# competing with the H3 text encoder / VAE / diffusion model.
nohup env -u OMP_NUM_THREADS MALLOC_ARENA_MAX=2 \
  "$PYTHON" main.py \
  --listen "$HOST" \
  --port "$PORT" \
  --disable-pinned-memory \
  --disable-async-offload \
  --cache-none \
  >> "$LOG" 2>&1 </dev/null &

PID=$!
echo "$PID" > "$PIDFILE"

for _ in $(seq 1 120); do
  if curl --noproxy '*' -fsS --connect-timeout 2 --max-time 3 \
    "http://127.0.0.1:${PORT}/system_stats" >/dev/null 2>&1; then
    echo "ComfyUI 已启动，PID=$PID"
    echo "日志：$LOG"
    exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "ComfyUI 启动失败，最后日志：" >&2
    tail -n 120 "$LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "ComfyUI 未在 120 秒内 READY，最后日志：" >&2
tail -n 120 "$LOG" >&2 || true
exit 1
