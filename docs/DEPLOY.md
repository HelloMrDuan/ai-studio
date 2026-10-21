# AI Studio v3 部署手册（AutoDL / 算家云 等 GPU 云）

> 目标机器：AutoDL **vGPU-32GB**（32GB 显存 / 12 核 / 62GB 内存 / 系统盘 30GB / 数据盘 50GB 可扩容，驱动 595.71.05，CUDA ≤ 13.2）。
> 所有程序、环境、模型、数据都放在数据盘 **/root/autodl-tmp**。系统盘只有 30GB，**不要往系统盘装东西**。

## 其他平台（算家云等）

脚本不绑定 AutoDL，差别只有三处：

| | AutoDL | 算家云 / 其他平台 |
|---|---|---|
| 安装目录 | `/root/autodl-tmp` | 自动选**剩余空间最大**的磁盘，建 `studio-home` 目录；也可以先 `export BASE=/数据盘路径` 指定 |
| 访问 GitHub/HF | `source /etc/network_turbo` | 没有就直连（模型走魔搭，国内不受影响）；GitHub 慢可自行设置 `http_proxy` |
| 打开页面 | 「自定义服务」6006 | 实例上「开放端口 → 对外开放端口」（只开放 **8080**）。`05_app.sh` 检测到算家云会自动把网页改到 8080、文字模型挪到 8081 |

安装目录选定后会记在 `~/.studio_base`，之后所有脚本都用同一个目录。第一步 `00_system.sh` 会打印选中的目录和剩余空间，**确认是数据盘再继续**。

## 24GB 显卡（RTX 4090 / 4090D / 3090）

可以跑，安装时加 `--24g`（有卡时会按显存自动识别；**无卡模式下必须手动加**）：

```bash
bash deploy/install_all.sh --24g
```

| | 32GB 方案 | 24GB 方案 |
|---|---|---|
| H3 视频模型 | `pruned_int8_convrot` 21GB | `pruned_w6a8` 16GB（官方最小档，INT8 计算，4090/3090 都支持） |
| 文字模型 | Qwen3.6 Q4_K_M 22.3GB | Qwen3.6 **IQ4_XS** 19.7GB（质量接近，显存留出余量） |
| Qwen-Image 2.1 | 不变 | 不变（约 17GB，24GB 足够） |
| 磁盘 | 约 135GB | 约 125GB |

- H3 文本编码器 27GB 大于 24GB 显存，ComfyUI 会自动分段加载，**实例内存需要 ≥ 64GB**（AutoDL 4090/4090D 一般满足）。
- 视频建议用「草稿 480p」，单镜头 ≤ 8 秒；「成品 768p」长镜头可能显存不足。
- 显卡优先级：4090 ≈ 4090D（慢约 10%）＞ 3090（无 FP8、速度约慢一半，能跑但不推荐）。

## 0. 准备

1. **镜像**：创建实例时选 **PyTorch 2.8.0 / Python 3.12 / CUDA 12.8**（或任何 Python 3.12 + CUDA ≥ 12.8 的官方镜像）。驱动支持 CUDA 13.2，向下兼容。
2. **扩容数据盘到 200GB**（控制台 → 实例 → 更多 → 扩容数据盘）。空间账：

| 内容 | 大小 |
|---|---|
| Qwen-Image 2.1（图片：模型 7.26 + 文本编码器 9.35 + VAE 0.68） | 17.3GB |
| MiniMax H3 图生视频（模型 20.97 + 文本编码器 27.14 + 视频 VAE 2.81 + 音频 VAE 0.61 + Turbo LoRA 1.96） | 53.5GB |
| Qwen3.6-35B-A3B Q4_K_M（文字模型） | 22.3GB |
| 可选：H3 参考生视频（模型 20.97 + LoRA 1.96） | 22.9GB |
| Python 环境 + FaceFusion 模型 + llama.cpp 编译 | 约 20GB |
| 项目数据（图片/视频，按量增长） | 20GB 起 |
| **合计** | **约 135GB（含参考生视频约 158GB）→ 建议 200GB** |

3. **端口**：AutoDL 只开放 **6006** 给「自定义服务」。AI Studio 就监听 6006；ComfyUI(8188)、llama-server(8080) 只在本机内部用。

## 1. 上传代码

```bash
cd /root/autodl-tmp
source /etc/network_turbo            # AutoDL 学术加速，GitHub 更快
git clone https://github.com/HelloMrDuan/ai-studio.git ai-studio   # 或者把压缩包上传后解压到 /root/autodl-tmp/ai-studio
unset http_proxy https_proxy
```

## 2. 一键安装（约 1~2 小时，主要是下载模型）

建议在 tmux 里跑，SSH 断了也不会中断：

```bash
tmux new -s install
cd /root/autodl-tmp/ai-studio
bash deploy/install_all.sh            # 需要参考生视频再加 --r2v；文字模型用外部 API 加 --no-llm
```

脚本按顺序执行下面这些步骤，每一步都能单独重跑（已完成的部分会自动跳过）：

| 脚本 | 做什么 | 结果位置 |
|---|---|---|
| `deploy/00_system.sh` | 检查 GPU/磁盘，apt 安装 ffmpeg、中文字体、aria2、编译工具 | 系统 |
| `deploy/01_comfyui.sh` | 克隆 ComfyUI v0.42.1，建虚拟环境（复用镜像自带的 PyTorch，不够新就装 2.8+ cu128） | `/root/autodl-tmp/ComfyUI`、`envs/comfyui` |
| `deploy/download_models.sh` | 下载全部模型：**魔搭 ModelScope 优先**，失败自动切 hf-mirror；aria2 16 线程断点续传 | `ComfyUI/models/*`、`models/llm` |
| `deploy/03_llama.sh` | 编译 llama.cpp（CUDA） | `/root/autodl-tmp/llama.cpp/build/bin/llama-server` |
| `deploy/04_facefusion.sh` | 安装 FaceFusion 3.9.1 + onnxruntime-gpu + CUDA12/cuDNN9 运行库，预下载人脸模型 | `/root/autodl-tmp/facefusion`、`envs/facefusion` |
| `deploy/05_app.sh` | 安装 AI Studio，生成 `.env` | `envs/studio` |
| `deploy/start.sh` | 启动 ComfyUI + AI Studio | 日志在 `/root/autodl-tmp/logs` |

### 省钱做法：先用「无卡模式」安装

下载约 100GB 模型要 1 小时以上，不需要显卡。可以在 AutoDL 选「**无卡模式开机**」（只有很少的 CPU，价格很低）先跑：

```bash
bash deploy/install_all.sh      # 自动识别无卡模式：装系统依赖、ComfyUI、FaceFusion、AI Studio，下载全部模型
```

无卡模式会自动跳过：编译 llama.cpp（需要识别显卡架构）、启动服务、自检。之后关机，改用 GPU 模式开机，再执行一次同样的命令，它会跳过已完成的部分，只补编译 llama.cpp（约 5~10 分钟）并启动服务。

> 无卡模式 CPU 和内存都很少，pip 安装会比较慢，属于正常现象。

### 模型清单（自动下载，手动下载也可以照这个放）

| 文件 | 仓库（魔搭/HF 同名） | 放到 |
|---|---|---|
| `qwen_image_2.1_turbo_int8_convrot.safetensors` | Comfy-Org/Qwen-Image-2.1 | `ComfyUI/models/diffusion_models/` |
| `qwen3vl_8b_int8_convrot.safetensors` | Comfy-Org/Qwen-Image-2.1 | `ComfyUI/models/text_encoders/` |
| `qwen_image_2.1_vae_bf16.safetensors` | Comfy-Org/Qwen-Image-2.1 | `ComfyUI/models/vae/` |
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | Comfy-Org/MiniMax-H3 | `ComfyUI/models/diffusion_models/` |
| `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | Comfy-Org/MiniMax-H3 | `ComfyUI/models/text_encoders/` |
| `minimax_h3_video_vae_int8_convrot.safetensors` | Comfy-Org/MiniMax-H3 | `ComfyUI/models/vae/` |
| `minimax_h3_audio_vae_fp32.safetensors` | Comfy-Org/MiniMax-H3 | `ComfyUI/models/vae/` |
| `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | Comfy-Org/MiniMax-H3 | `ComfyUI/models/loras/` |
| 可选 `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | Comfy-Org/MiniMax-H3 | `ComfyUI/models/diffusion_models/` |
| 可选 `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | Comfy-Org/MiniMax-H3 | `ComfyUI/models/loras/` |
| `Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf` | bartowski/Qwen_Qwen3.6-35B-A3B-GGUF | `/root/autodl-tmp/models/llm/` |

下载地址格式：`https://www.modelscope.cn/models/<仓库>/resolve/master/<子目录>/<文件>`（HF 镜像：`https://hf-mirror.com/<仓库>/resolve/main/<子目录>/<文件>`）。

## 3. 自检

```bash
/root/autodl-tmp/envs/studio/bin/python scripts/preflight.py          # 只检查
/root/autodl-tmp/envs/studio/bin/python scripts/preflight.py --smoke  # 实际跑：文字模型 → 文生图 → 参考图生图 → 1 秒视频
```

`--smoke` 首次会加载所有模型，大约 5~10 分钟。**把完整输出发给我**就能定位问题。

## 4. 打开界面

AutoDL 控制台 → 实例 →「自定义服务」→ 打开 6006 端口对应的网址。页面里的「系统状态」可以看到显存、服务和模型文件是否齐全。

日常启停：

```bash
bash deploy/start.sh    # 启动（关机重开后执行一次）
bash deploy/stop.sh     # 停止全部（含 llama-server）
bash deploy/status.sh   # 查看状态
tail -f /root/autodl-tmp/logs/studio.log /root/autodl-tmp/logs/comfyui.log
```

## 5. 显存怎么分配（单卡 32GB 轮流用）

- 同一时间只跑一个 GPU 任务（任务中心排队）。
- **文字模型**（剧本/提取/分镜）：自动启动 llama-server（约 23GB），用完后在出图前自动停掉。
- **出图**（Qwen-Image 2.1，约 17GB）与 **出视频**（H3，文本编码器 27GB 和视频模型 21GB 分别加载）切换时，会先让 ComfyUI 卸载上一组模型，防止 62GB 内存被缓存撑爆。
- **FaceFusion**：运行前会自动停掉文字模型并清空 ComfyUI 显存。
- 页面「系统状态 → 释放显存」可以手动清空。

> 建议工作节奏：先把所有镜头的**文字工作**做完（剧本 → 角色 → 分镜），再**批量出图**，最后**批量出视频**。这样模型切换最少，速度最快。

## 6. 可选配置（`.env`）

- **文字模型走外部 API**（不占显存、速度更快）：
  ```ini
  LLM_PROVIDER=api
  LLM_BASE_URL=https://api.deepseek.com/v1      # 或阿里百炼 https://dashscope.aliyuncs.com/compatible-mode/v1
  LLM_API_KEY=sk-xxxx
  LLM_MODEL=deepseek-chat                        # 或 qwen-plus 等
  ```
- **Blackwell 显卡**（`nvidia-smi --query-gpu=compute_cap --format=csv` 显示 12.x）：可以把 H3 文本编码器换成 `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`（15.69GB，省 11GB 显存和磁盘），手动下载后改 `VIDEO_CLIP=`。
- 修改 `.env` 后执行 `bash deploy/stop.sh && bash deploy/start.sh`。

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| 下载很慢/中断 | 直接重跑 `bash deploy/download_models.sh`，会断点续传；魔搭不通会自动切 hf-mirror |
| 系统盘满 | 检查是否有东西装到了 `/root` 下：`du -sh /root/* 2>/dev/null \| sort -h`；pip 缓存已指向数据盘 |
| ComfyUI 报缺节点 | ComfyUI 版本太旧：`COMFY_REF=master bash deploy/01_comfyui.sh` |
| 视频显存不足 (OOM) | 先用「草稿 480p」；镜头时长控制在 10 秒内；「系统状态 → 释放显存」后重试 |
| llama-server 启动失败 | 看 `logs/llama-server.log`；llama.cpp 太旧不认识 Qwen3.6 时重跑 `deploy/03_llama.sh` |
| FaceFusion 报 CUDA/cuDNN 找不到 | 重跑 `deploy/04_facefusion.sh`；确认 `envs/facefusion` 里装了 `nvidia-cudnn-cu12` |
| 中文字幕是方块 | `apt-get install -y fonts-noto-cjk` |
| 页面打不开 | `bash deploy/status.sh`；看 `logs/studio.out` |

## 8. 许可提示

- Qwen-Image 2.1、Qwen3.6：Apache-2.0。
- **MiniMax H3**：使用前请阅读 MiniMax 的模型许可，商业用途需确认授权条款。
- FaceFusion：OpenRAIL-AS，请勿用于未经授权的人脸替换。

## 访问密码 / 配音组件（v3.1）

- 网页访问密码：`05_app.sh` 首次运行会在 `.env` 写入随机的 `ACCESS_PASSWORD` 并打印，`start.sh` 启动时也会显示。修改后重启生效；留空表示不需要密码（不建议在开放公网端口时这样做）。
- 配音 / 对口型 / 质检 OCR：`bash deploy/06_voice.sh`（可加 `--no-lipsync`）。约占 25GB 磁盘，可在无卡模式下安装，自检需要 GPU 模式。装完重启服务，「系统状态」里会显示各组件是否就绪。

## 视频增强（P2-5，可选）

- **运镜**：默认用「方括号运镜指令 + 画面变化描述」的提示词模板（推进/拉远/摇/移/跟随/环绕/升降，可在分镜卡上选方向和速度），不需要额外模型。
- **运镜 LoRA**（可选）：把 LoRA 放到 `ComfyUI/models/loras/`，在 `data/camera_loras.json` 写映射，例如 `{"推镜": "h3_push_in.safetensors", "环绕": "h3_orbit.safetensors"}`（或环境变量 `CAMERA_LORAS`）。该运镜的镜头生成视频时自动串在模型链末端。
- **视频延长**：④视频页「延长」——取当前视频最后一帧续写 N 秒，再拼成一个长镜头（自动去掉重复帧），镜头时长同步更新。
- **动作参考**（可选）：把支持「参考视频驱动动作」的 ComfyUI 工作流（API 格式导出，如 Wan2.2-Animate / VACE）保存为 `data/workflows/motion.json`，在需要的地方写占位符：`{{IMAGE}}`（分镜图）、`{{VIDEO}}`（参考视频）、`{{PROMPT}}`、`{{WIDTH}}`、`{{HEIGHT}}`、`{{SECONDS}}`、`{{LENGTH}}`、`{{SEED}}`。配置后④视频页可上传参考视频并「按参考动作生成」。

## 视觉审片（P2-6，可选）

- **本地（推荐）**：`bash deploy/08_vlm.sh` —— 下载 Qwen3.6 的视觉投影 mmproj（审片与写剧本共用一个模型，不额外占显存）并装 faster-whisper 做台词核对，自动写入 `.env` 的 `LLM_MMPROJ`、`ASR_PYTHON`。本地模型与 ComfyUI 共享显卡，所以只做**批量审片**（④视频页「整片审片」），不在每次出图后即时审。
- **接口**：在 `.env` 设置 `VLM_BASE_URL`、`VLM_API_KEY`、`VLM_MODEL`（任何 OpenAI 兼容的视觉模型）。接口模式下出图/出视频后会**即时审**，不合格自动重抽（`VLM_AUTO=false` 可关闭）。
- 合格线：`VLM_PASS_SCORE`（默认 6 分，且没有严重问题）。审片结果在分镜卡/视频卡上展开查看，「一键返工」会把问题和修正描述写进提示词重抽。

## 声音后期（P3-5，可选）

- **旁白 / 解说**：复用 IndexTTS2（`deploy/06_voice.sh`）。台词里说话人选「旁白」，或在④分镜每个镜头填「解说词」（也可在⑤成片点「写解说词」让文字模型按 解说 / 第一人称 / 悬念 风格批量写）。旁白音色在⑤成片 → 声音里选角色音色或上传 3~15 秒样本；默认用 `data/voices/narrator.wav`。
- **音效库**：wav/mp3 放进 `data/sfx/`（或网页上传），**文件名即标签**（`雨声_环境.wav`、`关门声.mp3`）。合成时按镜头「环境音/音效」描述自动匹配，文件名带 雨/风/人群/环境 等字样的会循环铺满整个镜头。
- **AI 配乐**：`bash deploy/09_music.sh` 下载 ACE-Step v1 3.5B（约 7.7GB，可在无卡模式下运行；`--check` 检查 ComfyUI 节点）。按场次情绪曲线（紧张/悲伤/温馨/浪漫/燃/欢快/神秘/平静）分段生成、段间交叉淡化；想换工作流就把 API 格式 JSON 放到 `data/workflows/music.json`，占位符 `{{TAGS}} {{LYRICS}} {{SECONDS}} {{SEED}} {{STEPS}}`。
- **混音**：人声（对白 + 旁白）闪避 BGM（sidechaincompress），整片两遍 loudnorm 标准化到 -14 LUFS / -1.5 dBTP（可选 -16、-23）。成片历史版本里显示用了哪些声音层和目标响度。

## 画质增强（P3-1 超分 / P3-2 补帧，可选）

- `bash deploy/10_enhance.sh`：下载 Real-ESRGAN（`realesr-animevideov3` 漫剧快、`RealESRGAN_x4plus` 写实）到 `ComfyUI/models/upscale_models`，安装 RIFE 补帧节点 ComfyUI-Frame-Interpolation；无卡模式可运行，开卡后 `--test` 自检。
- 超分用 ComfyUI 的 Python 逐帧流式处理（显存与视频长度无关，原音轨直接复制），运行前会让 ComfyUI 卸载模型；`--seedvr2` 另装 SeedVR2 节点，需自备工作流模板 `data/workflows/upscale_seedvr2.json`（占位符 `{{VIDEO}} {{WIDTH}} {{HEIGHT}} {{SHORT}} {{SEED}} {{FPS}}`）。
- 补帧：RIFE 按整数倍补帧后精确重采样到目标帧率（30/48/60）；没装 RIFE 时退回 ffmpeg 运动补偿插帧（CPU，慢、效果一般）。
- 增强结果是镜头视频的新版本（继承配音和字幕时间），可随时切回原版。**建议先配音、对口型，再超分/补帧**（重新配音会从未增强的原视频开始）。
- 成片输出可选 1080p / 1440p 和 24/30/48/60 帧率（⑤成片 → 画质增强）。

## 云端混合（P4-4，可选）
- 在 `.env` 填 `MINIMAX_API_KEY`（海螺，MiniMax 开放平台）和/或 `KLING_ACCESS_KEY` + `KLING_SECRET_KEY`（可灵开放平台），重启服务后「④ 视频」页出现「默认引擎」选择：本地 / 全部海螺 / 全部可灵 / 混合（按「云端占比」把靠后的镜头分给云端）；每个镜头卡片也能单独指定引擎。
- 云端任务走单独的并行通道，不占显卡，与本地出片同时进行；每个引擎同时跑 `CLOUD_CONCURRENCY`（默认 3）个。远端任务号会记录下来，服务重启或网络抖动重试时接着查询，不会重复扣费。
- 云端只能出 5/6/10 秒，结果按镜头时长自动裁剪；云端视频没有声音，有台词的镜头建议开启「固定音色配音」。
- 海外账号把 `MINIMAX_BASE_URL` 改成 `https://api.minimax.io`；模型可用 `MINIMAX_MODEL` / `KLING_MODEL` / `KLING_MODE` 调整（可灵首尾帧需要 `pro`）。

## 多比例导出（P4-3）
- 「⑤ 成片」→「多比例导出」：选目标画幅 →「分析构图」预览每个镜头的裁切框 → 合成该画幅版本（不替换当前成片，在历史版本里）。主体集中的镜头裁切并跟随主体，主体分散的用模糊垫底保留完整画面；可逐镜头改。
- 要最好画质可「复制为新画幅项目」，剧本、角色、分镜设定保留，按新画幅重新出图和视频。

## 运维与统计（P4-5）
- 「系统状态」页：近 7/14/30/90 天的任务数、成功率、各类任务平均 / P50 / P90 耗时、按模型组的累计耗时、失败原因、云端引擎成功率；各项目占用空间；备份列表（下载 / 恢复为新项目 / 删除 / 上传 zip 恢复）。
- 项目「⑤ 成片」页底部「项目维护」：预览并清理未选用的候选（可每个对象额外保留最近 N 个）、无记录的残留文件、合成中断的临时目录、旧版成片（当前和每种画幅最新一版始终保留）。正在使用的文件及其来源（如超分前的原版、配音分轨）不会被删。
- 备份：完整备份或精简备份（不含未选用候选），zip 在 `data/backups/`；恢复时导入为新项目，所有 id 和文件路径自动改写，不覆盖现有项目。

## 画幅与无限画布（v3.3）
- 画幅：新建项目或剧本设置里选 8 种画幅或「自动」；镜头可单独设画幅。21:9 / 2:3 / 3:2 等新尺寸的视频请在显卡上先各出一条验证。
- 无限画布：左侧栏「流程视图 / 无限画布」切换；布局、分组、便签保存在项目里（`projects.canvas`），「自动排列」可随时复原。
- 存档点保存在 `snapshots` 表，只记录设置和文件引用，不复制文件；存档引用的文件不会被「项目维护」清理。有任务在排队 / 运行时不能回到存档点。
- 升级后无需额外操作：启动时自动补齐新表和新字段。
