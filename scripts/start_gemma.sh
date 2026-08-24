#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/root/autodl-tmp/ai-studio/platform-v2
ENV_FILE="$ROOT/.env"
LOG_DIR=/root/autodl-tmp/ai-studio/logs
LOG="$LOG_DIR/gemma-llama-server.log"
PID_FILE="$LOG_DIR/gemma-llama-server.pid"
RUNTIME_FILE="$LOG_DIR/gemma-runtime.env"

mkdir -p "$LOG_DIR"

get_env() {
  local key="$1" default_value="${2:-}"
  local value
  value=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true)
  value="${value%$'\r'}"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "${value:-$default_value}"
}

resolve_binary() {
  local configured="$1" candidate
  if [[ -n "$configured" && -x "$configured" ]]; then
    printf '%s' "$configured"
    return 0
  fi
  for candidate in \
    /root/autodl-tmp/llama.cpp/build/bin/llama-server \
    /root/autodl-tmp/llama.cpp/build/bin/Release/llama-server \
    /root/autodl-tmp/llama.cpp/llama-server; do
    if [[ -x "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  candidate=$(command -v llama-server 2>/dev/null || true)
  [[ -n "$candidate" && -x "$candidate" ]] && printf '%s' "$candidate"
}

resolve_model() {
  local configured="$1"
  if [[ -n "$configured" && -f "$configured" ]]; then
    printf '%s' "$configured"
    return 0
  fi
  python3 - <<'PY'
import os
from pathlib import Path
roots = [Path('/root/autodl-tmp/models/llm'), Path('/root/autodl-tmp/models')]
candidates = []
for root in roots:
    if not root.exists():
        continue
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {'.git', '.cache', 'envs', 'node_modules', 'ComfyUI', 'facefusion', 'platform-v2', 'backups'}]
        for name in files:
            lowered = name.lower()
            if lowered.endswith('.gguf') and 'gemma' in lowered and not any(x in lowered for x in ('mmproj', 'projector', 'embedding')):
                path = Path(current) / name
                try:
                    candidates.append((path.stat().st_size, str(path.resolve())))
                except OSError:
                    pass
if not candidates:
    raise SystemExit(1)
candidates.sort(reverse=True)
print(candidates[0][1], end='')
PY
}

CONFIGURED_BIN=$(get_env GEMMA_SERVER_BIN)
CONFIGURED_MODEL=$(get_env GEMMA_MODEL_PATH)
BIN=$(resolve_binary "$CONFIGURED_BIN" || true)
MODEL_PATH=$(resolve_model "$CONFIGURED_MODEL" || true)
HOST=$(get_env GEMMA_HOST 0.0.0.0)
PORT=$(get_env GEMMA_PORT 6006)
ALIAS=$(get_env GEMMA_MODEL gemma)
CTX_SIZE=$(get_env GEMMA_CTX_SIZE 8192)
GPU_LAYERS=$(get_env GEMMA_N_GPU_LAYERS 999)
PARALLEL=$(get_env GEMMA_PARALLEL 1)
REASONING=$(get_env GEMMA_REASONING off)
MM_PROJECTOR=$(get_env GEMMA_MM_PROJECTOR_PATH "")
BASE_URL=$(get_env GEMMA_BASE_URL "http://127.0.0.1:${PORT}/v1")
MODELS_URL="${BASE_URL%/}/models"

[[ -n "$BIN" && -x "$BIN" ]] || { echo "Gemma 启动失败：没有找到可执行的 llama-server：${CONFIGURED_BIN:-<空>}" >&2; exit 1; }
[[ -n "$MODEL_PATH" && -f "$MODEL_PATH" ]] || { echo "Gemma 启动失败：没有找到可用的 Gemma GGUF：${CONFIGURED_MODEL:-<空>}" >&2; exit 1; }
[[ "$GPU_LAYERS" =~ ^[0-9]+$ && "$GPU_LAYERS" != "0" ]] || { echo "Gemma 三态 GPU 模式要求 GEMMA_N_GPU_LAYERS 为正整数：$GPU_LAYERS" >&2; exit 1; }

check_ready() {
  MODELS_URL="$MODELS_URL" python3 - <<'PY' >/dev/null 2>&1
import json, os, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(os.environ['MODELS_URL'], timeout=5) as response:
    data = json.load(response)
if not data.get('data'):
    raise SystemExit(1)
PY
}

if check_ready; then
  echo "Gemma 已 READY：$MODELS_URL"
  exit 0
fi

# 进程已经存在但还在装载模型时，立即返回给调度器。
# READY 轮询由 GPUOrchestrator 负责，禁止在这里杀掉并重启正在加载的模型。
PID=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
  echo "Gemma 进程正在启动，PID：$PID"
  exit 0
fi
EXISTING_PID=$(pgrep -f "[l]lama-server.*--port[ =]${PORT}" | head -n1 || true)
if [[ -z "$EXISTING_PID" ]]; then
  EXISTING_PID=$(pgrep -f '[l]lama-server.*gemma' | head -n1 || true)
fi
if [[ -n "$EXISTING_PID" ]]; then
  echo "$EXISTING_PID" > "$PID_FILE"
  echo "检测到正在启动的 Gemma 进程，PID：$EXISTING_PID"
  exit 0
fi

CMD=("$BIN" --model "$MODEL_PATH" --host "$HOST" --port "$PORT" --ctx-size "$CTX_SIZE" --alias "$ALIAS" --parallel "$PARALLEL" --n-gpu-layers "$GPU_LAYERS" --jinja --no-webui)

# Gemma 4 默认 thinking 可能耗尽短回答的输出预算，导致 OpenAI 接口 content 为空。
# 仅在当前 llama-server 明确支持参数时追加，兼容较旧构建。
HELP_TEXT=$(timeout 10 "$BIN" --help 2>&1 || true)
if grep -q -- '--reasoning ' <<<"$HELP_TEXT"; then
  CMD+=(--reasoning "$REASONING")
fi
if grep -q -- '--chat-template-kwargs' <<<"$HELP_TEXT"; then
  CMD+=(--chat-template-kwargs '{"enable_thinking":false}')
fi
if [[ -n "$MM_PROJECTOR" ]]; then
  if [[ ! -f "$MM_PROJECTOR" ]]; then
    echo "Gemma 启动失败：GEMMA_MM_PROJECTOR_PATH 不存在：$MM_PROJECTOR" >&2
    exit 1
  fi
  CMD+=(--mmproj "$MM_PROJECTOR")
fi

{
  echo
  echo "===== $(date '+%F %T') 启动 Gemma GPU 工作区 ====="
  echo "binary=$BIN"
  echo "model=$MODEL_PATH"
  echo "alias=$ALIAS"
  echo "gpu_layers=$GPU_LAYERS"
  echo "reasoning=$REASONING"
  echo "mm_projector=${MM_PROJECTOR:-<未配置>}"
  printf 'command='; printf '%q ' "${CMD[@]}"; echo
} >> "$LOG"

# 使用 Python 真正脱离父进程启动，避免 asyncio 的 communicate() 被后台进程文件描述符拖住。
PID=$(python3 - "$LOG" "$PID_FILE" "${CMD[@]}" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

log_path, pid_path, *command = sys.argv[1:]
env = os.environ.copy()
ld = env.get('LD_LIBRARY_PATH', '')
parts = [item for item in ld.split(':') if item and '/miniconda3' not in item and '/envs/' not in item]
if parts:
    env['LD_LIBRARY_PATH'] = ':'.join(parts)
else:
    env.pop('LD_LIBRARY_PATH', None)
with open(log_path, 'ab', buffering=0) as output:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
Path(pid_path).write_text(str(process.pid), encoding='utf-8')
print(process.pid, end='')
PY
)

printf 'GEMMA_EFFECTIVE_GPU_LAYERS=%s\nGEMMA_EFFECTIVE_SERVER_BIN=%q\nGEMMA_EFFECTIVE_MODEL_PATH=%q\n' \
  "$GPU_LAYERS" "$BIN" "$MODEL_PATH" > "$RUNTIME_FILE"

sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  echo "Gemma GPU 进程启动后立即退出，最后日志：" >&2
  tail -n 160 "$LOG" >&2 || true
  exit 1
fi

echo "Gemma GPU 进程已启动，PID：$PID"
echo "调度器将进入 WARMING_UP 并等待：$MODELS_URL"
