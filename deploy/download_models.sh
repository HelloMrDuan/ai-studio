#!/usr/bin/env bash
# 第 2 步：下载模型（按显存自动选 24GB / 32GB 方案）。优先魔搭 ModelScope（国内快），失败再走 hf-mirror。支持断点续传，可重复执行。
#   bash deploy/download_models.sh            # 必需模型（约 93GB）
#   bash deploy/download_models.sh --r2v      # 额外下载 H3 参考生视频模型（+23GB）
#   bash deploy/download_models.sh --no-llm   # 文字模型走外部 API 时不下载 GGUF
#   bash deploy/download_models.sh --24g      # 24GB 显卡（4090/4090D/3090）：H3 用 w6a8(16GB)，文字模型用 IQ4_XS(19.7GB)
#   有卡时自动按显存选择；无卡模式下默认 32GB 方案，24GB 显卡请务必加 --24g
set -uo pipefail
source "$(dirname "$0")/env.sh"
WITH_R2V=0; WITH_LLM=1
PROFILE=""
for a in "$@"; do case $a in --r2v) WITH_R2V=1;; --no-llm) WITH_LLM=0;; --24g) PROFILE=24g;; --32g) PROFILE=32g;; esac; done
if [ -z "$PROFILE" ]; then
  if [ "$HAS_GPU" = 1 ]; then
    VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -dc 0-9)
    if [ "$VRAM" -lt 30000 ]; then PROFILE=24g; else PROFILE=32g; fi
  else
    PROFILE=$(cat "$BASE/.studio_profile" 2>/dev/null || echo 32g)
  fi
fi
echo "$PROFILE" > "$BASE/.studio_profile"
log "显存方案：$PROFILE"
if [ "$PROFILE" = 24g ]; then
  H3_DIT=w6a8; H3_GB=15.98; GGUF=Qwen_Qwen3.6-35B-A3B-IQ4_XS.gguf; GGUF_GB=19.69
else
  H3_DIT=int8_convrot; H3_GB=20.97; GGUF=Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf; GGUF_GB=22.28
fi
M=$COMFY_DIR/models
mkdir -p "$M"/{diffusion_models,text_encoders,vae,loras}

FAILED=()
dl() {  # dl <仓库> <仓库内路径> <目标目录> <期望大小GB>
  local repo=$1 path=$2 dir=$3 gb=$4 name; name=$(basename "$path")
  local dest=$dir/$name
  if [ -f "$dest" ] && [ ! -f "$dest.aria2" ]; then
    local have; have=$(stat -c%s "$dest")
    if awk "BEGIN{exit !($have >= $gb*1e9*0.98)}"; then log "✓ 已存在 $name"; return 0; fi
  fi
  for url in "https://www.modelscope.cn/models/$repo/resolve/master/$path" "$HF_ENDPOINT/$repo/resolve/main/$path"; do
    log "下载 $name (${gb}GB) ← ${url%%/resolve*}"
    if aria2c -c -x 16 -s 16 -k 20M --file-allocation=none --console-log-level=warn --summary-interval=30 \
         --auto-file-renaming=false --allow-overwrite=true -d "$dir" -o "$name" "$url"; then
      return 0
    fi
  done
  FAILED+=("$name"); return 1
}

QI=Comfy-Org/Qwen-Image-2.1
H3=Comfy-Org/MiniMax-H3
# ---- 图片：Qwen-Image 2.1（文生图 + 最多 16 张参考图）----
dl $QI diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors "$M/diffusion_models" 7.26
dl $QI text_encoders/qwen3vl_8b_int8_convrot.safetensors           "$M/text_encoders" 9.35
dl $QI vae/qwen_image_2.1_vae_bf16.safetensors                     "$M/vae" 0.68
# ---- 视频：MiniMax H3（图生视频 / 首尾帧，带对白与音效）----
dl $H3 diffusion_models/minimax_h3_fl2va_pruned_${H3_DIT}.safetensors "$M/diffusion_models" $H3_GB
dl $H3 text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors     "$M/text_encoders" 27.14
dl $H3 vae/minimax_h3_video_vae_int8_convrot.safetensors                 "$M/vae" 2.81
dl $H3 vae/minimax_h3_audio_vae_fp32.safetensors                         "$M/vae" 0.61
dl $H3 loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors   "$M/loras" 1.96
if [ $WITH_R2V = 1 ]; then
  dl $H3 diffusion_models/minimax_h3_ref2va_pruned_${H3_DIT}.safetensors "$M/diffusion_models" $H3_GB
  dl $H3 loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors   "$M/loras" 1.96
fi
# ---- 文字模型：Qwen3.6-35B-A3B（MoE，激活 3B，Q4_K_M 约 22GB）----
if [ $WITH_LLM = 1 ]; then
  dl bartowski/Qwen_Qwen3.6-35B-A3B-GGUF $GGUF "$LLM_DIR" $GGUF_GB
fi

df -h "$BASE" | tail -1
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "以下文件下载失败，可重新运行本脚本断点续传：${FAILED[*]}"; exit 1
fi
log "模型全部就绪"
