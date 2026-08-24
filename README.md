# AI Studio Platform V2

## 定位

这是统一的中文 AI 创作平台，不暴露 ComfyUI 原生节点界面，也不照搬 FaceFusion 原生页面。

平台包含三个独立模块：

1. **Gemma 助手**：独立生成、扩写和优化图片提示词；
2. **图片生成**：底层使用 ComfyUI，只提供图片生成相关功能；
3. **人物与画面处理**：底层使用 FaceFusion，提供多个中文功能页面。

三者可以独立使用，也可以通过素材库和结果按钮自由衔接。

## 已实现页面

- 总控台
- Gemma 助手
- 图片生成
- 人物与画面处理
- 素材库
- 任务记录
- 系统状态

## FaceFusion 中文功能

- 人物替换 `face_swapper`
- 深度替换 `deep_swapper`
- 年龄调整 `age_modifier`
- 表情修复 `expression_restorer`
- 面部增强 `face_enhancer`
- 面部编辑 `face_editor`
- 口型同步 `lip_syncer`
- 背景移除 `background_remover`
- 画面增强 `frame_enhancer`
- 画面上色 `frame_colorizer`

平台会读取当前 FaceFusion `headless-run --help`，检查当前安装版本是否识别对应功能和参数。

## GPU 工作区切换

只有两个重型页面触发切换：

```text
进入“图片生成”页面
→ 请求激活 ComfyUI 工作区

进入“人物与画面处理”页面
→ 请求激活 FaceFusion 工作区
```

进入 Gemma、总控台、素材库、任务记录和系统状态时，不切换 GPU。

切换流程：

```text
锁定切换
→ 等待当前任务结束
→ 停止旧组件
→ 确认端口/进程退出
→ 连续检查显存释放并满足最低空闲显存
→ 启动或预检目标组件
→ CUDA/接口健康检查
→ READY 后开放“开始”按钮
```

快速连续切页时，以最后一次目标页面为准；不会并发执行两个切换流程。

任务提交前还会二次确认 GPU 所有者与页面目标一致，避免页面已经切换但任务仍提交给旧组件。

## 默认端口

```text
6006  Gemma / llama.cpp
6008  统一平台
8188  ComfyUI 内部接口
```

FaceFusion 由平台通过命令行执行，不需要对外暴露原生页面。

## 部署目录

把压缩包解压为：

```text
/root/autodl-tmp/ai-studio/platform-v2
```

安装：

```bash
cd /root/autodl-tmp/ai-studio/platform-v2
chmod +x scripts/*.sh
bash scripts/install.sh
```

检查配置：

```bash
cat /root/autodl-tmp/ai-studio/platform-v2/.env
```

重点确认：

```text
COMFYUI_START_COMMAND
COMFYUI_STOP_COMMAND
COMFYUI_CHECKPOINT
FACEFUSION_DIR
FACEFUSION_PYTHON
GPU_MIN_FREE_MB
```

启动：

```bash
bash /root/autodl-tmp/ai-studio/platform-v2/scripts/start.sh
```

检查：

```bash
bash /root/autodl-tmp/ai-studio/platform-v2/scripts/check.sh
```

停止：

```bash
bash /root/autodl-tmp/ai-studio/platform-v2/scripts/stop.sh
```

AutoDL 映射 `6008` 即可。

## ComfyUI 启停脚本

默认配置使用：

```text
/root/autodl-tmp/ai-studio/start_comfy.sh
/root/autodl-tmp/ai-studio/stop_comfy.sh
```

这两个脚本必须满足：

- 启动脚本在后台启动 ComfyUI 后返回；
- 停止脚本结束 ComfyUI 进程；
- ComfyUI API 监听 `127.0.0.1:8188` 或 `0.0.0.0:8188`。

实际路径不同，就修改 `.env`。

## 人物与画面处理说明

人物身份相关功能要求素材为本人、虚构人物或已获得明确授权的成年人。

图片输入会统一转换为 PNG 后交给 FaceFusion，解决当前 FaceFusion 对 `.jpg` 与 `.jpeg` 输出扩展名严格匹配的问题，也便于背景移除保留透明通道。

视频保持原扩展名。

## 数据目录

```text
/root/autodl-tmp/ai-studio/data/platform-v2/
├── assets/
├── pending/
└── tasks/
    └── <task_id>/
        ├── task.json
        └── outputs/
```

## 首次部署验证顺序

1. 启动统一平台；
2. 打开总控台，检查 Gemma 状态；
3. 进入“图片生成”，确认页面显示 `READY`；
4. 生成一张测试图；
5. 进入“人物与画面处理”，观察排空、释放、预检和 READY；
6. 执行“人物替换”；
7. 再返回图片生成，确认能顺利切回 ComfyUI。

不要在首次验证时同时从终端手工启动 ComfyUI 或 FaceFusion 任务，否则调度器会识别到外部占用并拒绝错误切换。

## Git 与 installer baseline 管理

- 统一仓库：`https://github.com/HelloMrDuan/ai-studio.git`
- 主分支：`main`
- `.env`、运行日志、数据目录、缓存和本地备份不得提交。
- installer 必须基于已核验的真实 runtime 快照构造，不得使用过期工作区源码猜测 baseline。
- V2.39.6.3 当前 runtime baseline 为 `v23963-current-runtime.tar.gz`，完整 SHA 约束见 `deliverables/v23963_current_runtime_baseline_sha_manifest.json`。
