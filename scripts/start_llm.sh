#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/root/autodl-tmp/ai-studio/platform-v2
ENV_FILE="$ROOT/.env"
REGISTRY="$ROOT/config/llm_models.json"
SELECTION=/root/autodl-tmp/ai-studio/data/platform-v2/llm_selection.json
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

STAGE04_REQUIRED_MODEL_ID=$(get_env STAGE04_REQUIRED_MODEL_ID qwen3-32b-abliterated)
STAGE04_REQUIRED_MODEL_ALIAS=$(get_env STAGE04_REQUIRED_MODEL_ALIAS qwen3-32b)

mapfile -t MODEL_INFO < <(
python3 - "$REGISTRY" "$SELECTION" "$STAGE04_REQUIRED_MODEL_ID" "$STAGE04_REQUIRED_MODEL_ALIAS" <<'PY'
import json
import sys
from pathlib import Path

registry_path = Path(sys.argv[1])
selection_path = Path(sys.argv[2])
required_model_id = str(sys.argv[3]).strip()
required_alias = str(sys.argv[4]).strip()
registry = json.loads(registry_path.read_text(encoding="utf-8"))
models = {
    str(x.get("id") or ""): x
    for x in registry.get("models", [])
    if isinstance(x, dict) and str(x.get("id") or "")
}
if not models:
    raise SystemExit("LLM registry has no models")

selected = ""
try:
    selected = str(
        json.loads(selection_path.read_text(encoding="utf-8")).get("selected_model") or ""
    )
except Exception:
    pass

default_id = str(registry.get("default_model") or "")
model = None
if selected and selected == required_model_id:
    item = models.get(selected)
    if not isinstance(item, dict):
        raise SystemExit(
            f"Required selected LLM registry entry is missing: {selected}"
        )
    alias = str(item.get("alias") or "").strip()
    if alias != required_alias:
        raise SystemExit(
            "Required selected LLM alias mismatch: "
            f"model_id={selected} alias={alias or '<empty>'} "
            f"required_alias={required_alias}"
        )
    path = Path(str(item.get("path") or ""))
    if not path.is_file():
        raise SystemExit(
            f"Required selected LLM model file does not exist: {path}"
        )
    model = item
else:
    # Preserve legacy fallback only when Stage04's required model was not
    # explicitly selected. An explicit Qwen selection is always fail-closed.
    candidates = [selected, default_id] + list(models)
    for model_id in candidates:
        item = models.get(model_id)
        if not item:
            continue
        path = Path(str(item.get("path") or ""))
        if path.is_file():
            model = item
            break

if model is None:
    raise SystemExit("No installed LLM model from registry")

print(str(model.get("id") or ""))
print(str(model.get("path") or ""))
print(str(model.get("alias") or model.get("id") or ""))
print(str(model.get("reasoning") or "off"))
print("true" if bool(model.get("enable_thinking", False)) else "false")
print(str(model.get("label") or model.get("id") or "LLM"))
PY
)

MODEL_ID="${MODEL_INFO[0]:-}"
MODEL_PATH="${MODEL_INFO[1]:-}"
ALIAS="${MODEL_INFO[2]:-}"
REASONING="${MODEL_INFO[3]:-off}"
ENABLE_THINKING="${MODEL_INFO[4]:-false}"
MODEL_LABEL="${MODEL_INFO[5]:-$MODEL_ID}"

CONFIGURED_BIN=$(get_env GEMMA_SERVER_BIN)
BIN=$(resolve_binary "$CONFIGURED_BIN" || true)
HOST=$(get_env GEMMA_HOST 0.0.0.0)
PORT=$(get_env GEMMA_PORT 6006)
CTX_SIZE=$(get_env GEMMA_CTX_SIZE 8192)
GPU_LAYERS=$(get_env GEMMA_N_GPU_LAYERS 999)
PARALLEL=$(get_env GEMMA_PARALLEL 1)
BASE_URL=$(get_env GEMMA_BASE_URL "http://127.0.0.1:${PORT}/v1")
MODELS_URL="${BASE_URL%/}/models"

[[ -n "$BIN" && -x "$BIN" ]] || { echo "LLM 启动失败：没有找到 llama-server：${CONFIGURED_BIN:-<空>}" >&2; exit 1; }
[[ -n "$MODEL_PATH" && -f "$MODEL_PATH" ]] || { echo "LLM 启动失败：模型文件不存在：${MODEL_PATH:-<空>}" >&2; exit 1; }
[[ "$GPU_LAYERS" =~ ^[0-9]+$ && "$GPU_LAYERS" != "0" ]] || { echo "LLM GPU 模式要求 GEMMA_N_GPU_LAYERS 为正整数：$GPU_LAYERS" >&2; exit 1; }

check_ready_alias() {
  MODELS_URL="$MODELS_URL" EXPECTED_ALIAS="$ALIAS" python3 - <<'PY' >/dev/null 2>&1
import json
import os
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(os.environ["MODELS_URL"], timeout=5) as response:
    data = json.load(response)
ids = [
    str(x.get("id") or "")
    for x in data.get("data", [])
    if isinstance(x, dict)
]
raise SystemExit(0 if os.environ["EXPECTED_ALIAS"] in ids else 1)
PY
}

if check_ready_alias; then
  echo "LLM 已 READY：$MODEL_LABEL / $ALIAS"
  exit 0
fi

PID=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
  echo "LLM 进程正在启动，PID：$PID"
  exit 0
fi

EXISTING_PID=$(pgrep -f "[l]lama-server.*--port[ =]${PORT}" | head -n1 || true)
if [[ -n "$EXISTING_PID" ]]; then
  echo "$EXISTING_PID" > "$PID_FILE"
  echo "检测到正在启动的 LLM 进程，PID：$EXISTING_PID"
  exit 0
fi

CMD=("$BIN" --model "$MODEL_PATH" --host "$HOST" --port "$PORT" --ctx-size "$CTX_SIZE" --alias "$ALIAS" --parallel "$PARALLEL" --n-gpu-layers "$GPU_LAYERS" --jinja --no-webui)

HELP_TEXT=$(timeout 10 "$BIN" --help 2>&1 || true)
if grep -q -- '--reasoning ' <<<"$HELP_TEXT"; then
  CMD+=(--reasoning "$REASONING")
fi
if grep -q -- '--chat-template-kwargs' <<<"$HELP_TEXT"; then
  CMD+=(--chat-template-kwargs "{\"enable_thinking\":${ENABLE_THINKING}}")
fi

{
  echo
  echo "===== $(date '+%F %T') 启动 LLM GPU 工作区 ====="
  echo "model_id=$MODEL_ID"
  echo "label=$MODEL_LABEL"
  echo "binary=$BIN"
  echo "model=$MODEL_PATH"
  echo "alias=$ALIAS"
  echo "gpu_layers=$GPU_LAYERS"
  echo "reasoning=$REASONING"
  echo "enable_thinking=$ENABLE_THINKING"
  printf 'command='; printf '%q ' "${CMD[@]}"; echo
} >> "$LOG"

PID=$(python3 - "$LOG" "$PID_FILE" "${CMD[@]}" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

log_path, pid_path, *command = sys.argv[1:]
env = os.environ.copy()
ld = env.get("LD_LIBRARY_PATH", "")
parts = [
    item for item in ld.split(":")
    if item and "/miniconda3" not in item and "/envs/" not in item
]
if parts:
    env["LD_LIBRARY_PATH"] = ":".join(parts)
else:
    env.pop("LD_LIBRARY_PATH", None)
env.pop("OMP_NUM_THREADS", None)

with open(log_path, "ab", buffering=0) as output:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
Path(pid_path).write_text(str(process.pid), encoding="utf-8")
print(process.pid, end="")
PY
)

cat > "$RUNTIME_FILE" <<EOF
GEMMA_EFFECTIVE_GPU_LAYERS=$GPU_LAYERS
GEMMA_EFFECTIVE_SERVER_BIN=$BIN
GEMMA_EFFECTIVE_MODEL_PATH=$MODEL_PATH
LLM_EFFECTIVE_MODEL_ID=$MODEL_ID
LLM_EFFECTIVE_MODEL_ALIAS=$ALIAS
LLM_EFFECTIVE_MODEL_LABEL=$MODEL_LABEL
EOF

sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  echo "LLM GPU 进程启动后立即退出，最后日志：" >&2
  tail -n 160 "$LOG" >&2 || true
  exit 1
fi

echo "LLM GPU 进程已启动，PID：$PID"
echo "调度器将进入 WARMING_UP 并等待：$MODELS_URL"
