#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/root/autodl-tmp/ai-studio/platform-v2
ENV_FILE="$ROOT/.env"
REGISTRY="$ROOT/config/llm_models.json"
LOG_DIR=/root/autodl-tmp/ai-studio/logs
LOG="$LOG_DIR/gemma-llama-server.log"
PID_FILE="$LOG_DIR/gemma-llama-server.pid"
RUNTIME_FILE="$LOG_DIR/gemma-runtime.env"
mkdir -p "$LOG_DIR"

get_env() {
  local key="$1" default_value="${2:-}" value
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
  if [[ -n "$configured" && -x "$configured" ]]; then printf '%s' "$configured"; return 0; fi
  for candidate in \
    /root/autodl-tmp/llama.cpp/build/bin/llama-server \
    /root/autodl-tmp/llama.cpp/build/bin/Release/llama-server \
    /root/autodl-tmp/llama.cpp/llama-server; do
    if [[ -x "$candidate" ]]; then printf '%s' "$candidate"; return 0; fi
  done
  candidate=$(command -v llama-server 2>/dev/null || true)
  [[ -n "$candidate" && -x "$candidate" ]] && printf '%s' "$candidate"
}

REQUIRED_ID=$(get_env STAGE04_REQUIRED_MODEL_ID qwen3-32b-abliterated)
REQUIRED_ALIAS=$(get_env STAGE04_REQUIRED_MODEL_ALIAS qwen3-32b)
mapfile -t MODEL_INFO < <(
python3 - "$REGISTRY" "$REQUIRED_ID" "$REQUIRED_ALIAS" <<'PY'
import json, sys
from pathlib import Path
registry = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
required_id, required_alias = sys.argv[2], sys.argv[3]
models = {str(x.get('id') or ''): x for x in registry.get('models', []) if isinstance(x, dict)}
item = models.get(required_id)
if not isinstance(item, dict):
    raise SystemExit(f'V3 required Qwen model missing from registry: {required_id}')
alias = str(item.get('alias') or '').strip()
if alias != required_alias:
    raise SystemExit(f'V3 required Qwen alias mismatch: {alias} != {required_alias}')
path = Path(str(item.get('path') or ''))
if not path.is_file():
    raise SystemExit(f'V3 required Qwen model file missing: {path}')
print(str(path))
print(alias)
print(str(item.get('reasoning') or 'off'))
print('true' if bool(item.get('enable_thinking', False)) else 'false')
print(str(item.get('label') or required_id))
PY
)
MODEL_PATH="${MODEL_INFO[0]:-}"
ALIAS="${MODEL_INFO[1]:-}"
REASONING="${MODEL_INFO[2]:-off}"
ENABLE_THINKING="${MODEL_INFO[3]:-false}"
MODEL_LABEL="${MODEL_INFO[4]:-$REQUIRED_ID}"

BIN=$(resolve_binary "$(get_env GEMMA_SERVER_BIN)" || true)
HOST=$(get_env GEMMA_HOST 0.0.0.0)
PORT=$(get_env GEMMA_PORT 6006)
CTX_SIZE=$(get_env GEMMA_CTX_SIZE 8192)
GPU_LAYERS=$(get_env GEMMA_N_GPU_LAYERS 999)
PARALLEL=$(get_env GEMMA_PARALLEL 1)
BASE_URL=$(get_env GEMMA_BASE_URL "http://127.0.0.1:${PORT}/v1")
MODELS_URL="${BASE_URL%/}/models"

[[ -n "$BIN" && -x "$BIN" ]] || { echo "V3 Qwen 启动失败：找不到 llama-server" >&2; exit 1; }
[[ -f "$MODEL_PATH" ]] || { echo "V3 Qwen 启动失败：模型文件不存在：$MODEL_PATH" >&2; exit 1; }
[[ "$GPU_LAYERS" =~ ^[0-9]+$ && "$GPU_LAYERS" != "0" ]] || { echo "V3 Qwen 要求正整数 GPU layers：$GPU_LAYERS" >&2; exit 1; }

check_ready() {
  MODELS_URL="$MODELS_URL" EXPECTED_ALIAS="$ALIAS" python3 - <<'PY' >/dev/null 2>&1
import json, os, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(os.environ['MODELS_URL'], timeout=5) as response:
    data = json.load(response)
ids = [str(x.get('id') or '') for x in data.get('data', []) if isinstance(x, dict)]
raise SystemExit(0 if os.environ['EXPECTED_ALIAS'] in ids else 1)
PY
}

if check_ready; then
  echo "V3 Qwen 已 READY：$MODEL_LABEL / $ALIAS"
  exit 0
fi
if pgrep -f "[l]lama-server.*--port[ =]${PORT}" >/dev/null 2>&1; then
  echo "V3 Qwen 启动拒绝复用错误模型：6006 已有非 ${ALIAS} llama-server" >&2
  exit 2
fi

CMD=("$BIN" --model "$MODEL_PATH" --host "$HOST" --port "$PORT" --ctx-size "$CTX_SIZE" --alias "$ALIAS" --parallel "$PARALLEL" --n-gpu-layers "$GPU_LAYERS" --jinja --no-webui)
HELP_TEXT=$(timeout 10 "$BIN" --help 2>&1 || true)
if grep -q -- '--reasoning ' <<<"$HELP_TEXT"; then CMD+=(--reasoning "$REASONING"); fi
if grep -q -- '--chat-template-kwargs' <<<"$HELP_TEXT"; then CMD+=(--chat-template-kwargs "{\"enable_thinking\":${ENABLE_THINKING}}"); fi

{
  echo
  echo "===== $(date '+%F %T') 启动 V3 Qwen GPU 工作区 ====="
  echo "model_id=$REQUIRED_ID"
  echo "label=$MODEL_LABEL"
  echo "model=$MODEL_PATH"
  echo "alias=$ALIAS"
  printf 'command='; printf '%q ' "${CMD[@]}"; echo
} >> "$LOG"

PID=$(python3 - "$LOG" "$PID_FILE" "${CMD[@]}" <<'PY'
import os, subprocess, sys
from pathlib import Path
log_path, pid_path, *command = sys.argv[1:]
env = os.environ.copy()
ld = env.get('LD_LIBRARY_PATH', '')
parts = [x for x in ld.split(':') if x and '/miniconda3' not in x and '/envs/' not in x]
if parts: env['LD_LIBRARY_PATH'] = ':'.join(parts)
else: env.pop('LD_LIBRARY_PATH', None)
env.pop('OMP_NUM_THREADS', None)
with open(log_path, 'ab', buffering=0) as output:
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, env=env, start_new_session=True, close_fds=True)
Path(pid_path).write_text(str(process.pid), encoding='utf-8')
print(process.pid, end='')
PY
)
cat > "$RUNTIME_FILE" <<EOF
GEMMA_EFFECTIVE_GPU_LAYERS=$GPU_LAYERS
GEMMA_EFFECTIVE_SERVER_BIN=$BIN
GEMMA_EFFECTIVE_MODEL_PATH=$MODEL_PATH
LLM_EFFECTIVE_MODEL_ID=$REQUIRED_ID
LLM_EFFECTIVE_MODEL_ALIAS=$ALIAS
LLM_EFFECTIVE_MODEL_LABEL=$MODEL_LABEL
EOF
sleep 1
kill -0 "$PID" 2>/dev/null || { tail -n 160 "$LOG" >&2 || true; exit 1; }
echo "V3 Qwen GPU 进程已启动，PID：$PID；等待 READY：$MODELS_URL"
