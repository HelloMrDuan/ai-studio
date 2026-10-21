#!/usr/bin/env bash
# 第 10 步（可选）：画质增强（P3-1 超分 / P3-2 补帧）。可在无卡模式下运行（只下载/安装），自检需要 GPU 模式。
#   bash deploy/10_enhance.sh             # Real-ESRGAN 超分模型 + RIFE 补帧节点
#   bash deploy/10_enhance.sh --seedvr2   # 另装 SeedVR2 超分节点（更清晰、更慢，需要 data/workflows/upscale_seedvr2.json）
#   bash deploy/10_enhance.sh --test      # 只跑自检
# 装好后重启 ComfyUI 与应用：bash deploy/stop.sh && bash deploy/start.sh
set -euo pipefail
source "$(dirname "$0")/env.sh"
SEEDVR2=0; TEST_ONLY=0
for a in "$@"; do [ "$a" = "--seedvr2" ] && SEEDVR2=1; [ "$a" = "--test" ] && TEST_ONLY=1; done
cd "$APP_DIR"; [ -f .env ] || die "请先运行 deploy/05_app.sh"
[ -x "$COMFY_PY" ] || die "没有 ComfyUI 环境：请先运行 deploy/01_comfyui.sh"
setenv() { if grep -q "^$1=" .env; then sed -i "s#^$1=.*#$1=$2#" .env; else echo "$1=$2" >> .env; fi; }
UP=$COMFY_DIR/models/upscale_models
NODE=$COMFY_DIR/custom_nodes/ComfyUI-Frame-Interpolation

gh_get() {  # gh_get <github 下载地址> <目标文件>
  local url=$1 dest=$2
  [ -s "$dest" ] && { log "✓ 已存在 $(basename "$dest")"; return 0; }
  mkdir -p "$(dirname "$dest")"
  for u in "${GH_PROXY}${url}" "$url"; do
    [ -z "$u" ] && continue
    if aria2c -c -x 8 -s 8 --file-allocation=none --console-log-level=warn --auto-file-renaming=false --allow-overwrite=true \
         -d "$(dirname "$dest")" -o "$(basename "$dest")" "$u"; then return 0; fi
  done
  log "⚠ 下载失败：$url"; return 1
}

selftest() {
  [ "$HAS_GPU" = 1 ] || { log "无卡模式：跳过自检（开卡后运行 bash deploy/10_enhance.sh --test）"; return 0; }
  local t=/tmp/enh_test; mkdir -p $t
  ffmpeg -v error -y -f lavfi -i testsrc2=size=768x432:rate=24:d=2 -f lavfi -i sine=d=2 -shortest -pix_fmt yuv420p $t/src.mp4
  local model; model=$(grep -E "^UPSCALE_MODEL=" .env | cut -d= -f2-); model=${model:-realesr-animevideov3.pth}
  log "自检：超分 768x432 → 1920x1080（$model）"
  local t0=$SECONDS
  "$COMFY_PY" scripts/upscale.py --input $t/src.mp4 --output $t/up.mp4 --model "$UP/$model" --width 1920 --height 1080 | tail -2
  log "超分 48 帧用时 $((SECONDS - t0)) 秒：$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 $t/up.mp4)"
}
if [ $TEST_ONLY = 1 ]; then selftest; exit 0; fi

# ---------- 1. Real-ESRGAN 超分模型（spandrel 直接加载，ComfyUI 自带依赖） ----------
gh_get https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth "$UP/realesr-animevideov3.pth" || die "超分模型下载失败"   # 动漫/漫剧，快
gh_get https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth "$UP/RealESRGAN_x4plus.pth" || log "⚠ 写实超分模型没下载成功（可选）"   # 写实，较慢
"$COMFY_PY" -c "import spandrel" 2>/dev/null || "$COMFY_PY" -m pip install -q spandrel
setenv COMFYUI_PYTHON "$COMFY_PY"

# ---------- 2. RIFE 补帧（ComfyUI-Frame-Interpolation） ----------
if [ ! -d "$NODE" ]; then
  log "安装 ComfyUI-Frame-Interpolation"
  gh_clone https://github.com/Fannovel16/ComfyUI-Frame-Interpolation "$NODE"
fi
REQ=$NODE/requirements-no-cupy.txt; [ -f "$REQ" ] || REQ=$NODE/requirements.txt
"$COMFY_PY" -m pip install -q -r "$REQ"
CK=$(grep -E "^RIFE_CKPT=" .env | cut -d= -f2-); CK=${CK:-rife47.pth}
gh_get "https://github.com/styler00dollar/VSGAN-tensorrt-docker/releases/download/models/$CK" "$NODE/ckpts/rife/$CK" \
  || log "⚠ RIFE 权重预下载失败，首次补帧时节点会自动下载"

# ---------- 3. SeedVR2（可选） ----------
if [ $SEEDVR2 = 1 ]; then
  SV=$COMFY_DIR/custom_nodes/ComfyUI-SeedVR2_VideoUpscaler
  [ -d "$SV" ] || gh_clone https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler "$SV"
  [ -f "$SV/requirements.txt" ] && "$COMFY_PY" -m pip install -q -r "$SV/requirements.txt"
  log "SeedVR2 已安装：模型在首次运行时自动下载。在 ComfyUI 里搭好 加载视频→SeedVR2→保存视频 的工作流，"
  log "  导出 API 格式存为 data/workflows/upscale_seedvr2.json，占位符 {{VIDEO}} {{WIDTH}} {{HEIGHT}} {{SHORT}} {{SEED}} {{FPS}}"
fi
selftest
log "完成。重启后在 ④视频 每个镜头的「延长 / 动作参考 / 增强」里使用，或 ⑤成片 → 画质增强 批量处理"
