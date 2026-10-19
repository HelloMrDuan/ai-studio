#!/usr/bin/env bash
# 第 3 步：编译 llama.cpp（CUDA）。文字模型改用外部 API 的话可以跳过。
set -euo pipefail
source "$(dirname "$0")/env.sh"
# 无卡模式下没法探测显卡架构（native），除非手动指定 CUDA_ARCH（如 89 / 120）；否则开卡后再编译（12 核约 5~10 分钟）
if [ "$HAS_GPU" != 1 ] && [ -z "${CUDA_ARCH:-}" ]; then
  log "无卡模式：跳过 llama.cpp 编译，开 GPU 后执行 bash deploy/03_llama.sh"; exit 0
fi
turbo_on
if [ ! -d "$LLAMA_DIR/.git" ]; then
  git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
else
  git -C "$LLAMA_DIR" pull --ff-only || true
fi
turbo_off
command -v nvcc >/dev/null || export PATH=/usr/local/cuda/bin:$PATH
command -v nvcc >/dev/null || die "找不到 nvcc。请选用带 CUDA 开发包的 AutoDL 镜像（如 PyTorch 2.x / CUDA 12.8）"
cd "$LLAMA_DIR"
log "编译 llama-server（约 5~10 分钟）"
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH:-native} -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j "$(nproc)" --target llama-server
./build/bin/llama-server --version || true
log "llama.cpp 完成：$LLAMA_DIR/build/bin/llama-server"
