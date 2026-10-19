#!/usr/bin/env bash
# 第 4 步：安装 FaceFusion 3.9（独立虚拟环境，onnxruntime-gpu + pip 版 CUDA12 运行库）
set -euo pipefail
source "$(dirname "$0")/env.sh"
FF_REF=${FF_REF:-3.9.1}
turbo_on
[ -d "$FF_DIR/.git" ] || gh_clone https://github.com/facefusion/facefusion.git "$FF_DIR" "$FF_REF" || die "克隆 FaceFusion 失败"
turbo_off
# 无学术加速时，模型下载源换成国内可用的镜像（github → gh-proxy，huggingface → hf-mirror）
if [ ! -f /etc/network_turbo ]; then
  sed -i -e "s#'https://huggingface.co'#'https://hf-mirror.com'#" \
         -e "s#'https://github.com'#'${GH_PROXY:-}https://github.com'#" "$FF_DIR/facefusion/choices.py"
  grep -n "hf-mirror\|gh-proxy" "$FF_DIR/facefusion/choices.py" | head -3 || true
fi
make_env facefusion
cd "$FF_DIR"
log "安装 FaceFusion 依赖（onnxruntime-gpu for CUDA 12）"
PATH=$ENVS/facefusion/bin:$PATH "$FF_PY" install.py cuda@12 --skip-conda   # install.py 调用 PATH 里的 pip，必须指向本环境
log "安装 CUDA 12 / cuDNN 9 运行库（pip 版，不依赖系统 CUDA）"
"$FF_PY" -m pip install nvidia-cuda-runtime-cu12 nvidia-cudnn-cu12 nvidia-cublas-cu12 nvidia-cufft-cu12 \
  nvidia-curand-cu12 nvidia-cuda-nvrtc-cu12
NV_LIBS=$("$FF_PY" -c "import glob,os,site;print(':'.join(sorted(glob.glob(os.path.join(site.getsitepackages()[0],'nvidia','*','lib')))))")
export LD_LIBRARY_PATH=$NV_LIBS:${LD_LIBRARY_PATH:-}
log "预下载 FaceFusion 模型（避免首次使用时卡住）"
turbo_on
"$FF_PY" facefusion.py force-download || echo "部分模型下载失败，首次使用时会自动补下"
turbo_off
"$FF_PY" -c "import onnxruntime as o; print('onnxruntime', o.__version__, o.get_available_providers())"
log "FaceFusion 完成：$FF_DIR"
