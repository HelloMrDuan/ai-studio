# V3 分镜生产链修复方案

目标是修复所有项目会遇到的空间关系错误，而不是给青云山或特定角色写坐标。当前五张参考图已进入同一个 ComfyUI 图，只证明输入传输正确；组合图里人物脚下无地面、剑的附着点错误，说明镜头的可执行空间条件尚未成立。候选不得因模型返回文件就视为可采用。

## 开源实现对照与取舍

| 项目 | 可复用的做法 | 对当前故障的边界 |
| --- | --- | --- |
| LumenX | `StoryboardFrame` 的结构化 blocking、镜头参数、分级资产解析和逐镜 TAKE | `generate_storyboard_render` 把 `composition_data` 中的参考图和 prompt 交给生成器；当前路径没有把脚下支撑面、道具附着点编译成物理约束。它还有内存中的部分任务状态，不适合替换现有 Temporal。 |
| LocalMiniDrama | 分镜时长、前后镜头连续性、首尾帧明确绑定、生成任务与参考图日志 | `layout_description` 主要作为高优先级文本插入 prompt，仍不能证明地面或附着点成立；部分路径会按提示词过滤参考图，不可照搬到正式引用。 |
| ai-short-drama（雪风） | 角色/场景/道具注册表、`@` 标签与图片输入序位的确定性绑定、多 TAKE | 解决“图片 N 是谁”，不解决“人物站在什么表面、道具挂在哪个身体部位”。 |
| ArcReel | 正式剧本与视觉层分离，按 ID 合并；未登记或无已采用资产图的引用在生成入口阻断 | 参考图注入和人工审核并非可执行几何控制。 |
| waooowaoo | 单一资源事实源；冻结 `resourceId + contentVersion + role + channel` 的有序输入；异步终态单写者 | 可借用执行身份与状态原则，不迁移它的资源系统，也不取消 ai-studio 的 Prompt Compiler。 |
| OiiOii | 公开资料描述共享场景图、锁定角色形象并由镜头选择模型 | 没有公开可核验的空间控制实现，不能把宣传描述当作可移植代码。 |

MoneyPrinterTurbo、NarratoAI、FunClip、pyVideoTrans 主要用于成片、字幕、剪辑等末端，不是此处的分镜空间故障的优先解法。Wan/HunyuanVideo 等视频底座保留为 Adapter 选项，不替换资产与任务系统。

## ai-studio 模块决定

| 处理 | 当前模块 | 决定 |
| --- | --- | --- |
| 保留 | `ProductionAssetService`、canonical Entity、Logical Asset、Asset Version | 唯一资产事实源；镜头场景底图和执行合同继续作为版本化 Asset，复用 `metadata` 与 `parent_asset_ids`，不建第二套表。 |
| 保留 | Stage04 正式分镜、Typed Story Bible、`GenerationContractCompiler`、Temporal、ComfyUI Adapter、ResourceStore | 保留已有生产链和角色参考图路线。 |
| 改造 | `shot_authoring.py` 与 `storyboard/contracts.py` | 文本模型只编写语义站位；另有可执行的 `ShotExecutionSpec`，标明相机、地面支撑区、人物脚点/姿态、道具持有人与身体附着点、遮挡层、精确资产版本。 |
| 改造 | `legacy_reference_bridge.py`、`production_legacy_bridge.py` | 正式分镜在生成前解析并冻结 canonical 引用。缺道具 ID 应回到正式分镜修正或阻断；移除按名字从文本临时补 ID 的读时兜底。 |
| 改造 | `unified_image_executor.py`、`zimage_temporal_executor.py` | 先生成或选择与本镜头机位匹配的空场景底图，再依据地面/身体锚点组合控制图；模型只在其图像条件能力足够时接单。保留逐张参考的 ID、角色、版本、哈希与 Comfy 节点绑定记录。 |
| 改造 | candidate、TaskStore、页面轮询 | 每次 submission 只读取自身 task/workflow/candidate；失败和重试不复用旧候选。生成完成与“通过空间校验、可供采用”分开。 |
| 淘汰 | 历史候选按目标资产取最新、prompt 文本推断引用、无条件参考图截断 | 兼容读取旧数据即可；新生产路径禁止使用。 |

## 执行合同

每次生成先冻结同一个合同：`shot_id`、正式分镜版本、`scene_plate_asset_version_id`、`camera`、有序 `reference_manifest`、`subject_placements`、`prop_attachments`、`support_surfaces`、`compiled_prompt`、模型能力与参数、合同哈希。引用只用 canonical ID 和已采用 Asset Version ID；文字名称只供显示。

地点参考图是“这个地点长什么样”，不是“此机位可站立的底图”。空场景底图必须有可见且覆盖人物脚点的支撑面；有悬崖、楼梯、水面、桌椅等不同空间时，支撑面由底图上的多边形或深度/分割证据表示。自动视觉识别没有足够把握时停止并要求确认底图，不能把文本模型凭空给的坐标当真。

道具不再只给独立 `screen_box` 和 `behind_subject/front_of_subject`。合同必须声明 `holder_entity_id`、`attachment`（如左手、右手、背部、腰侧、地面）、朝向、相对尺寸和遮挡关系。落图坐标由人物姿态关键点推导；“背负”不得落成腰侧，“手握”不得漂浮。身体关键点无法检测、剑鞘状态与参考图矛盾、脚点不在可站立区域时，在提交 ComfyUI 前阻断或重新规划。无证据不准静默修补。

ComfyUI 仍按现有 Z-Image/Union ControlNet 或满足条件的图像编辑模型运行，但实际执行图必须绑定场景底图、人物/道具参考与空间控制；不能只把五张图编号放进 prompt。输出后检查人数和道具数、人物与地面接触、道具附着、身份与服装、画幅和镜头动作。自动判断不确定则进入人工审核；只有已采用分镜图能作为视频首帧。

## 实施顺序与验收

1. 收束当前远端未提交改动，记录 Web/Worker 同一 commit、当前镜头五参考的真实执行证据；冻结旧候选，补路由/状态回归测试。
2. 完成正式分镜引用一致性：Stage04 输出的角色/地点/道具 ID 与 manifest 必须相同；缺失时阻断并修正式数据。测试任意角色和道具名，不按词过滤。
3. 把 `ShotExecutionSpec` 和场景底图做成现有 Asset Version；生成前验证地面与身体附着点。先用青云山失败镜头回归，再用不同场景/道具组合验证通用性。
4. 将合同编译到实际 ComfyUI 执行图，保存 exact input 和 output lineage；失败需显示是哪项空间约束或哪个模型阶段未满足。
5. 用至少两个不同项目做真实逐镜生成：室外不平地面与室内平面；分别覆盖手持、背负、地面道具。每个都核对提交、Comfy 历史、候选、人工采用和采用图驱动的视频任务，检查文件可访问。CI 与 V3 Verify 通过后再推送。

验收标准不是“prompt 看起来正确”或“参考图上传五张”，而是：输入合同可追溯，错误布局在生成前被阻断；真实候选中脚有支撑、道具附着正确；采用的确切图片版本进入后续视频，任务和输出无旧状态污染。若模型做不到某类镜头，明确标记不支持该条件并给出可用模型或人工底图入口，不能宣称已修复。

## 核对的实现

- [LumenX 分镜模型](https://github.com/alibaba/lumenx/blob/f2a02e23171447c939e7d8e1386b24d17049bbf1/src/apps/comic_gen/models.py)、[实际分镜渲染](https://github.com/alibaba/lumenx/blob/f2a02e23171447c939e7d8e1386b24d17049bbf1/src/apps/comic_gen/pipeline.py)、[参考图收集](https://github.com/alibaba/lumenx/blob/f2a02e23171447c939e7d8e1386b24d17049bbf1/src/apps/comic_gen/storyboard.py)
- [LocalMiniDrama 空间合同注入](https://github.com/xuanyustudio/LocalMiniDrama/blob/adaecf71a38277126fbe1e5e0d79664300f855c1/backend-node/src/services/framePromptService.js)、[生成时参考图处理](https://github.com/xuanyustudio/LocalMiniDrama/blob/adaecf71a38277126fbe1e5e0d79664300f855c1/backend-node/src/services/imageService.js)、[首尾帧绑定](https://github.com/xuanyustudio/LocalMiniDrama/blob/adaecf71a38277126fbe1e5e0d79664300f855c1/backend-node/src/services/storyboardFrameBinding.js)
- [ai-short-drama 确定性引用绑定](https://github.com/oiuv/ai-short-drama/blob/4f318097c54a2e24ea34d3c9d23d30f5ec332f11/lib/storyboard-references.ts)、[镜头与资产类型](https://github.com/oiuv/ai-short-drama/blob/4f318097c54a2e24ea34d3c9d23d30f5ec332f11/lib/types.ts)
- [ArcReel 正式脚本视觉层](https://github.com/ArcReel/ArcReel/blob/9aa1e2c63ff9d0b81fe84b9fead4fd947a3f58a6/agent_runtime_profile/.claude/skills/generate-script/SKILL.md)、[资产图准入决策](https://github.com/ArcReel/ArcReel/blob/9aa1e2c63ff9d0b81fe84b9fead4fd947a3f58a6/docs/adr/0073-generation-entry-requires-registered-assets-with-sheets.md)
- [waoowaoo 单一资源与冻结输入](https://github.com/waooAI/waoowaoo/blob/dfec20e32298ed675050329e4a41477024148399/docs/architecture/modules/workspace-resource.md)、[视频参考图角色](https://github.com/waooAI/waoowaoo/blob/dfec20e32298ed675050329e4a41477024148399/src/lib/video-generation/reference-images.ts)
- [OiiOii 公开流程](https://www.oiioii.ai/how-it-works)，仅作为产品级参考。

## 2026-09-24 真实模型试验

在项目 `73fd8e8265c7b1263753a79c` 的第二镜头，直接使用生产服务器 ComfyUI 和五张正式参考图做了三步诊断，未改正式分镜、未采用候选。

1. Qwen Image Edit 2511 以正式地点参考图生成空场景底图（Comfy prompt `6971583e-54fd-4278-9995-eeea5918a4af`）。结果是一条连通石地，能够在左右站人；也证明原全景参考图不能直接充当此镜机位底图。
2. 用相对人物框的背部、右手位置构造控制图，Z-Image Turbo BF16 + Union ControlNet 从控制图 VAE latent 生成（`bfdd67fc-f6d1-446d-985d-2624c9ed985a`）。人物脚下有地面，剑柄接近肩后，但小玉佩在采样中消失。
3. Qwen Image Edit 2511 以本轮成图和已采用玉佩图做局部条件编辑（`e3ea2db0-5443-4be5-8c18-a7b92b8be301`）。玉佩重新进入苏瑶手中，人物仍在地面上；但镜头成为正面双人站位，与正式分镜的肩侧机位和互相视线不一致。

三步均由真实模型执行且产出了文件，但第三步**没有通过正式分镜验收**，不得采用或送视频。试验说明场景底图和身体锚点必要且可执行，也暴露当前前视角色参考加低降噪图像条件会锁死人物朝向。下一轮必须让合同显式选择相机方位、角色朝向及对应的已采用三视图，再考虑落入生产链；同时需要场景底图的支撑区证据与前置审核。仅增加 prompt 或后验检查不足以解决。

## 2026-09-26 连续镜头与道具附着试验

第 2 镜的正式文字要求沈川左近景、苏瑶右中景，但已采用的第 1 镜和青云山参考图在该相机下只有狭窄雪阶。SAM 脚点探测在左侧命中一小块石阶，在右侧命中山壁；这不是五张图有没有传到 ComfyUI 的问题。旧的画面规划模板还预填左右两人的坐标，模型直接沿用，迫使空场景底图扩成不属于青云山的石砖广场。

- 上一镜 + 苏瑶 + 玉佩的真实 Qwen 编辑（`4e78043d-37b4-4a1e-b015-34d896113081`）保住雪阶和沈川，但苏瑶脚点不牢，玉佩成为腰间巨型吊坠。全图二次编辑（`dd7fc3b0-8c18-45bf-84b8-aaa7ba993d59`）把玉佩移到手里，却把双人镜改成苏瑶特写，证明全图重采样不能作为可靠道具修复。
- 以同一山体生成空的覆雪岩台，再将两张已采用角色三视图按可见支撑面合成控制图。第一次苏瑶落在岩台外，调整脚点后两人均在岩面或雪面。Qwen 图像条件编辑（`0d7904df-b89e-4801-9176-8a3ae29542d7`）保住人物、机位和沈川背上的入鞘剑，但把玉佩挂在苏瑶胸前。
- 对该图的局部潜空间遮罩编辑（`961dce45-5f97-41a8-bc5b-1049518c3313`）保住了全景并让苏瑶抬手，却把玉佩删掉。将已采用玉佩的前景像素按手部锚点贴入后，审计图 `/files/v3/media/image/qa-ledge-jade-control.png` 呈现可见的手持玉佩；这是实验性控制图，并非正式 candidate，也未人工采用。

这些试验说明可行方向是 **场景机位/支撑面先确定 → 角色按真实脚点布局 → 生成时绑定已采用身份和道具 → 对遗漏的手持道具做局部图像条件或确定性附着**。当前仍缺正式的场景底图与支撑区资产、自动脚点/手部锚点绑定、候选入库前的空间校验；不能宣布第 2 镜或后续视频已验收。

## 2026-09-27 正式生产链复验

上述结论属于当时的试验状态。本轮将空场景底图及其可站立区域登记为版本化镜头资产，并在生成前用 canonical 道具持有人关系约束视觉规划；脚点和手腕锚点进入 ComfyUI 控制画面。道具归属来自故事连续性事件中的 `prop entity_id → holder entity_id`，没有按名字或词语特判。视觉规划若没有可信持有人、人物脚点不在支持区、或参考图数量/身份不完整，会在 GPU 前阻断。

项目 `73fd8e8265c7b1263753a79c` 的第 2 镜正式生成了候选 `dcand_3e6a2e67ab39694d6e9c`，采用为分镜图资产 `ast_0d0f6850cde617d81e4a`。生成作业记录五个不同 canonical 引用、一个场景底图版本和一个 Z-Image Turbo + Union ControlNet 控制图；人物双脚有可见雪/岩支撑面，古剑在沈川背上，玉佩在苏瑶右手。H3 标准档随后使用这张已采用图片的逐字节相同副本作为首帧，生成 3.75 秒、768×448 的 MP4 候选 `dcand_f315538201cd10de851e`，并采用为正式 `shot_clip` 资产 `ast_1a5412917091943a0161`。视频父资产含该首帧和正式运动 Prompt；视频文件可通过平台 HTTP 访问。

本次还发现原归档工作台的采用校验仍要求旧版“独立视频首帧”标签，和 V3 已采用分镜图直接作首帧的提交合同冲突。V3 采用桥已验证实际当前首帧 ID、正式运动 Prompt 与候选依赖，再按原工作台的正式资产发布逻辑采用，并保留 V3 的真实合同版本。底层生成作业在文件落盘后现在也持久记录 `materialized`，避免候选完成而作业仍显示 `queued`。

本轮仅对一个正式镜头做了真实图片与视频复验；这不能证明所有地形、多人遮挡、道具交接和所有五个镜头都能自动一次生成。当前场景底图的支撑区仍需可靠来源与审核。候选图和视频的视觉质量仍需逐镜把关，不能用“文件已生成”替代内容验收。
