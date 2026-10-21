#!/usr/bin/env bash
# 第 9 步（可选）：AI 配乐（P3-5）。下载 ACE-Step v1 3.5B（ComfyUI 原生支持，约 7.7GB，生成 1 分钟配乐约 10~20 秒）。
# 可在无卡模式下运行（只下载）；装好后在 ⑤成片 → 后期设置 → AI 配乐 点「生成配乐」。
#   bash deploy/09_music.sh           # 下载模型 + 建音效库目录
#   bash deploy/09_music.sh --check   # 只检查 ComfyUI 是否有 ACE-Step 节点（需要 ComfyUI 在运行）
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$APP_DIR"; [ -f .env ] || die "请先运行 deploy/05_app.sh"
CKPT_DIR=$COMFY_DIR/models/checkpoints
NAME=ace_step_v1_3.5b.safetensors
REPO=${MUSIC_REPO:-Comfy-Org/ACE-Step_ComfyUI_repackaged}

check_nodes() {
  local url; url=$(grep -E "^COMFYUI_URL=" .env | cut -d= -f2-); url=${url:-http://127.0.0.1:8188}
  curl -fsS "$url/object_info" -o /tmp/oi.json || { log "⚠ ComfyUI 没在运行，跳过节点检查"; return 0; }
  for n in TextEncodeAceStepAudio EmptyAceStepLatentAudio VAEDecodeAudio SaveAudio; do
    grep -q "\"$n\"" /tmp/oi.json && log "✓ 节点 $n" || log "⚠ ✗ 缺节点 $n：请升级 ComfyUI（git pull）"
  done
}
if [ "${1:-}" = "--check" ]; then check_nodes; exit 0; fi

mkdir -p "$CKPT_DIR"
DEST=$CKPT_DIR/$NAME
if [ -s "$DEST" ] && [ ! -f "$DEST.aria2" ] && [ "$(stat -c%s "$DEST")" -gt 7000000000 ]; then
  log "✓ 已存在 $NAME"
else
  ok=0
  for url in "https://www.modelscope.cn/models/$REPO/resolve/master/all_in_one/$NAME" "$HF_ENDPOINT/$REPO/resolve/main/all_in_one/$NAME"; do
    log "下载 $NAME (约 7.7GB) ← ${url%%/resolve*}"
    if aria2c -c -x 16 -s 16 -k 20M --file-allocation=none --console-log-level=warn --summary-interval=30 \
         --auto-file-renaming=false --allow-overwrite=true -d "$CKPT_DIR" -o "$NAME" "$url"; then ok=1; break; fi
  done
  [ $ok = 1 ] || die "下载失败：可手动下载 $REPO/all_in_one/$NAME 放到 $CKPT_DIR"
fi
grep -q "^MUSIC_CKPT=" .env || echo "MUSIC_CKPT=$NAME" >> .env

# 音效库：把 wav/mp3 放进 data/sfx，文件名即标签（如「雨声_环境.wav」「关门声.mp3」）
DATA=$(grep -E "^DATA_DIR=" .env | cut -d= -f2-); DATA=${DATA:-$BASE/data/studio}
mkdir -p "$DATA/sfx" "$DATA/voices"
log "音效库目录：$DATA/sfx（可在网页 ⑤成片 → 音效 上传）；默认旁白音色可放 $DATA/voices/narrator.wav"
check_nodes
log "完成。重启应用后生效：bash deploy/stop.sh && bash deploy/start.sh"
