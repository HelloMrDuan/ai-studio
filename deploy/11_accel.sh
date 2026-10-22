#!/usr/bin/env bash
# 第 11 步（可选）：推理加速（P1-1）——编译安装 SageAttention 2，ComfyUI 启动时自动加 --use-sage-attention。
#   bash deploy/11_accel.sh          # 下载源码并编译（需要 nvcc；16 核约 10~20 分钟，建议 GPU 模式下运行）
#   bash deploy/11_accel.sh --test   # 只检查是否装好
# 装好后重启：bash deploy/stop.sh && bash deploy/start.sh。不想用可在 .env 写 COMFY_SAGE=0。
# 视频推理缓存（EasyCache）是 ComfyUI 自带节点，无需安装：.env 的 VIDEO_CACHE（草稿，默认 0.2）/ VIDEO_CACHE_FINAL（成品，默认 0 关闭）。
set -uo pipefail
source "$(dirname "$0")/env.sh"
[ -x "$COMFY_PY" ] || die "没有 ComfyUI 环境：请先运行 deploy/01_comfyui.sh"
check() { "$COMFY_PY" -c "import sageattention, torch; print('SageAttention 已安装：', sageattention.__file__)"; }
if [ "${1:-}" = "--test" ]; then check || die "SageAttention 未安装"; exit 0; fi
check 2>/dev/null && { log "SageAttention 已安装，跳过"; exit 0; }
NVCC=$(command -v nvcc || ls /usr/local/cuda/bin/nvcc 2>/dev/null) || die "找不到 nvcc（CUDA 编译器），无法编译 SageAttention"
export CUDA_HOME=${CUDA_HOME:-$(dirname "$(dirname "$NVCC")")}
SRC=$BASE/SageAttention; rm -rf "$SRC"
log "下载 SageAttention 源码"
for i in 1 2 3 4 5; do
  git clone -q --depth 1 https://github.com/thu-ml/SageAttention.git "$SRC" 2>/dev/null && break
  rm -rf "$SRC"; mkdir -p "$SRC"
  curl -fsSL --retry 3 https://codeload.github.com/thu-ml/SageAttention/tar.gz/refs/heads/main | tar xz -C "$SRC" --strip-components=1 \
    && [ -f "$SRC/setup.py" ] && break
  rm -rf "$SRC"; sleep 5
done
[ -f "$SRC/setup.py" ] || die "SageAttention 源码下载失败（GitHub 不通），稍后重试"
ARCH=$("$COMFY_PY" -c "import torch; c=torch.cuda.get_device_capability(); print(f'{c[0]}.{c[1]}')" 2>/dev/null || echo "8.9")
log "编译 SageAttention（显卡架构 $ARCH，约 10~20 分钟）"
cd "$SRC"
"$COMFY_PY" -m pip install -q setuptools wheel ninja
TORCH_CUDA_ARCH_LIST=$ARCH MAX_JOBS=${MAX_JOBS:-12} EXT_PARALLEL=4 NVCC_APPEND_FLAGS="--threads 4" \
  "$COMFY_PY" -m pip install . --no-build-isolation 2>&1 | grep -v notice | tail -5
cd /; check || die "SageAttention 编译失败，见上方输出"
log "完成。重启后生效：bash deploy/stop.sh && bash deploy/start.sh"
