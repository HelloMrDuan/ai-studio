#!/usr/bin/env bash
# 所有部署脚本共用的路径。程序、环境、模型、数据全部放在数据盘（系统盘通常很小，不要往里装东西）。
# 安装根目录：AutoDL 用 /root/autodl-tmp；其他平台（算家云等）自动选剩余空间最大的磁盘，
# 也可以手动指定：export BASE=/你的数据盘目录
pick_base() {
  if [ -d /root/autodl-tmp ]; then echo /root/autodl-tmp; return; fi
  if [ -d /root/sj-tmp ] && [ -w /root/sj-tmp ]; then echo /root/sj-tmp; return; fi   # 算家云数据盘
  local best="/" bestfree=0 free mnt
  while read -r free mnt; do
    case "$mnt" in /boot*|/dev*|/proc*|/sys*|/run*|/snap*|/etc*|/usr*|/var/lib/docker*) continue;; esac
    [ -d "$mnt" ] && [ -w "$mnt" ] || continue
    if [ "$free" -gt "$bestfree" ]; then best=$mnt; bestfree=$free; fi
  done < <(df -P -B1 -x tmpfs -x devtmpfs -x squashfs 2>/dev/null | awk 'NR>1 {print $4, $6}')
  if [ "$best" = "/" ]; then echo /root/studio-home; else echo "${best%/}/studio-home"; fi
}
export BASE=${BASE:-$(cat "$HOME/.studio_base" 2>/dev/null || pick_base)}
mkdir -p "$BASE" && echo "$BASE" > "$HOME/.studio_base"
export APP_DIR=${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
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
# 有些平台默认的清华源同步滞后（新发布的 ComfyUI 依赖会找不到），统一用阿里云 + 腾讯云兜底；可自行 export 覆盖
export PIP_INDEX_URL=${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}
export PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL:-https://mirrors.cloud.tencent.com/pypi/simple}
export HF_HOME=$BASE/.cache/huggingface
export HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com}
mkdir -p "$ENVS" "$LOG_DIR" "$RUN_DIR" "$LLM_DIR" "$PIP_CACHE_DIR"

# 基础 Python：用镜像自带的（推荐镜像 PyTorch 2.8 / Python 3.12 / CUDA 12.8）
BASE_PY=${BASE_PY:-$(command -v python3 || echo /root/miniconda3/bin/python)}
export BASE_PY

# 无卡模式（关掉 GPU 开机）检测：可以装环境、下模型，但不能编译 llama.cpp / 启动服务
if nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -qiv "no devices"; then export HAS_GPU=1; else export HAS_GPU=0; fi   # 无卡模式下 nvidia-smi -L 也可能返回 0

# 平台学术加速（AutoDL 的 /etc/network_turbo；其他平台可自己 export http_proxy）。只在访问外网的步骤里调用。
turbo_on() { [ -f /etc/network_turbo ] && source /etc/network_turbo >/dev/null 2>&1 || true; }
turbo_off() { unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY; }

# GitHub 克隆：有学术加速（AutoDL）直连；否则先走 GH_PROXY 镜像（默认 gh-proxy.com，可 export GH_PROXY= 关闭），失败再直连
if [ -f /etc/network_turbo ]; then GH_PROXY=${GH_PROXY-}; else GH_PROXY=${GH_PROXY-https://gh-proxy.com/}; fi
gh_clone() {  # gh_clone <github_url> <dir> [tag/branch]；镜像 → 备用镜像 → 直连，最多 3 轮（实测：国内直连常中途断开）
  local url=$1 dir=$2 ref=${3:-} args=(--depth 1) round u
  [ -n "$ref" ] && args+=(--branch "$ref")
  for round in 1 2 3; do
    for u in ${GH_PROXY:+"${GH_PROXY}${url}"} ${GH_PROXY:+"https://ghfast.top/${url}"} "$url"; do
      rm -rf "$dir"
      if timeout 600 git clone "${args[@]}" "$u" "$dir"; then
        git -C "$dir" remote set-url origin "$url"; return 0
      fi
    done
    sleep $((round * 5))
  done
  rm -rf "$dir"; return 1
}

# 容器可用内存（GB）与 CPU 核数（读 cgroup 限额；nproc/free 显示的是宿主机）
mem_limit_gb() {
  local b; b=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo max)
  case "$b" in max|"") free -g | awk '/Mem:/{print $2}';; *) echo $(( b / 1073741824 ));; esac
}
cpu_limit() {
  local q p n; n=$(nproc)
  if read -r q p < /sys/fs/cgroup/cpu.max 2>/dev/null && [ "$q" != max ]; then :;
  else q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null || echo -1); p=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us 2>/dev/null || echo 100000); fi
  if [ "${q:--1}" -gt 0 ] 2>/dev/null; then n=$(( (q + p - 1) / p )); fi
  echo $(( n < 1 ? 1 : n ))
}
build_jobs() {  # 每个编译进程按 2.5GB 内存估算，避免 OOM
  local c m; c=$(cpu_limit); m=$(( $(mem_limit_gb) * 2 / 5 )); [ $m -lt 1 ] && m=1
  echo $(( c < m ? c : m ))
}

# 网页端口：.env 里的 PORT（AutoDL 自定义服务 6006；算家云只对外开放 8080）；文字模型端口取自 LLM_BASE_URL
env_get() { grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
studio_port() { local p; p=$(env_get PORT); echo "${p:-6006}"; }
llm_port() { local u; u=$(env_get LLM_BASE_URL); u=${u:-http://127.0.0.1:8080/v1}; u=${u#*://}; u=${u%%/*}; echo "${u##*:}"; }

# ComfyUI 额外启动参数：.env 的 COMFY_ARGS；装了 SageAttention 自动加 --use-sage-attention（COMFY_SAGE=0 关闭）
comfy_args() {
  local a sage; a=$(env_get COMFY_ARGS); sage=$(env_get COMFY_SAGE)
  if [ "${sage:-auto}" != "0" ] && [[ "$a" != *--use-sage-attention* ]] && "$COMFY_PY" -c "import sageattention" 2>/dev/null; then
    a="$a --use-sage-attention"
  fi
  echo "$a"
}
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
