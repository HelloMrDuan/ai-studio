#!/usr/bin/env bash
# 第 0 步：检查机器 + 安装系统依赖
set -euo pipefail
source "$(dirname "$0")/env.sh"

log "检查 GPU"
if [ "$HAS_GPU" = 1 ]; then
  nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv
else
  echo "⚠️  未检测到 GPU（无卡模式）：本次只安装环境和下载模型，GPU 相关步骤开卡后再做。"
fi
log "检查数据盘空间"
df -h "$BASE"
FREE_GB=$(df -BG --output=avail "$BASE" | tail -1 | tr -dc 0-9)
if [ "$FREE_GB" -lt 140 ]; then
  echo "⚠️  数据盘剩余 ${FREE_GB}GB。全部模型约 95GB（含参考生视频约 118GB）+ 环境约 20GB，建议在 AutoDL 控制台把数据盘扩容到 200GB。"
fi

log "安装系统依赖（ffmpeg、中文字体、编译工具、aria2）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk aria2 git git-lfs build-essential cmake \
  ccache libcurl4-openssl-dev pkg-config curl wget tmux lsof || die "apt 安装失败"
fc-list | grep -qi "Noto Sans CJK" && log "中文字体 OK"
ffmpeg -hide_banner -version | head -1
"$BASE_PY" --version
"$BASE_PY" -c "import torch; print('镜像自带 torch', torch.__version__, 'CUDA', torch.version.cuda)" 2>/dev/null || echo "镜像未带 torch（之后会自动安装）"
log "系统依赖完成"
