#!/usr/bin/env bash
# 所有部署脚本共用的路径。全部放在 AutoDL 数据盘 /root/autodl-tmp（系统盘只有 30GB，不要往里装东西）。
export BASE=${BASE:-/root/autodl-tmp}
export APP_DIR=${APP_DIR:-$BASE/ai-studio}
export ENVS=$BASE/envs                       # 所有 Python 虚拟环境都建在数据盘
export COMFY_DIR=$BASE/ComfyUI
export COMFY_PY=$ENVS/comfyui/bin/python
export APP_PY=$ENVS/studio/bin/python
export FF_DIR=$BASE/facefusion
export FF_PY=$ENVS/facefusion/bin/python
export LLAMA_DIR=$BASE/llama.cpp
export LLM_DIR=$BASE/models/llm
export LOG_DIR=$BASE/logs
export RUN_DIR=$BASE/run
# 缓存也放数据盘，避免撑爆系统盘
export PIP_CACHE_DIR=$BASE/.cache/pip
export HF_HOME=$BASE/.cache/huggingface
export HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com}
mkdir -p "$ENVS" "$LOG_DIR" "$RUN_DIR" "$LLM_DIR" "$PIP_CACHE_DIR"

# 基础 Python：用 AutoDL 镜像自带的（推荐镜像 PyTorch 2.8 / Python 3.12 / CUDA 12.8）
BASE_PY=${BASE_PY:-$(command -v python3 || echo /root/miniconda3/bin/python)}
export BASE_PY

# 无卡模式（AutoDL 关掉 GPU 开机）检测：可以装环境、下模型，但不能编译 llama.cpp / 启动服务
if nvidia-smi -L >/dev/null 2>&1; then export HAS_GPU=1; else export HAS_GPU=0; fi

# AutoDL 学术加速（GitHub / HuggingFace 更快）。只在需要访问外网的步骤里调用 turbo_on。
turbo_on() { [ -f /etc/network_turbo ] && source /etc/network_turbo >/dev/null 2>&1 || true; }
turbo_off() { unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY; }

log() { echo -e "\033[1;36m[$(date +%H:%M:%S)] $*\033[0m"; }
die() { echo -e "\033[1;31m[错误] $*\033[0m" >&2; exit 1; }

make_env() {  # make_env <名称> [--system-site-packages]
  local prefix=$ENVS/$1
  if [ -x "$prefix/bin/python" ]; then log "环境 $1 已存在"; return; fi
  "$BASE_PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" \
    || die "镜像自带 Python 版本过低（需要 ≥3.11）。请换用 Python 3.12 的 AutoDL 镜像，或 export BASE_PY=/path/to/python3.12"
  log "创建虚拟环境 $1 → $prefix"
  "$BASE_PY" -m venv ${2:-} "$prefix" || die "创建环境失败"
  "$prefix/bin/python" -m pip install -q -U pip wheel
}
