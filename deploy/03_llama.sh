#!/usr/bin/env bash
# 第 3 步：编译 llama.cpp（CUDA）。文字模型改用外部 API 的话可以跳过。
set -euo pipefail
source "$(dirname "$0")/env.sh"
# 无卡模式下没法探测显卡架构（native），除非手动指定 CUDA_ARCH（如 89 / 120）；否则开卡后再编译（12 核约 5~10 分钟）
if [ "$HAS_GPU" != 1 ] && [ -z "${CUDA_ARCH:-}" ]; then
  log "无卡模式：跳过 llama.cpp 编译，开 GPU 后执行 bash deploy/03_llama.sh"; exit 0
fi
# 无卡模式的容器通常只给 1GB 内存 / 0.5 核（如算家云），编译 CUDA 必然 OOM，留到开卡后
if [ "$HAS_GPU" != 1 ] && [ "$(mem_limit_gb)" -lt 8 ]; then
  log "无卡模式内存只有 $(mem_limit_gb)GB，编译会被 OOM：跳过，开 GPU 后自动编译"; exit 0
fi
turbo_on
if [ ! -d "$LLAMA_DIR/.git" ]; then
  gh_clone https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR" || die "克隆 llama.cpp 失败"
else
  git -C "$LLAMA_DIR" pull --ff-only || true
fi
turbo_off
command -v nvcc >/dev/null || export PATH=/usr/local/cuda/bin:$PATH
command -v nvcc >/dev/null || die "找不到 nvcc。请选用带 CUDA 开发包的 AutoDL 镜像（如 PyTorch 2.x / CUDA 12.8）"
cd "$LLAMA_DIR"
log "编译 llama-server（$(build_jobs) 并行，约 5~15 分钟）"
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH:-native} -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j "$(build_jobs)" --target llama-server
./build/bin/llama-server --version || true
log "llama.cpp 完成：$LLAMA_DIR/build/bin/llama-server"
