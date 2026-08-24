const $ = (id) => document.getElementById(id);

const PAGE_META = {
  dashboard: ["总控台", "查看服务、任务和 GPU 工作区状态"],
  gemma: ["Gemma 助手", "通用对话、创作辅助、图片提示词与可选多模态理解"],
  image: ["图片生成", "基于 ComfyUI 的图片生成能力"],
  facefusion: ["人物与画面处理", "FaceFusion 多功能中文处理中心"],
  assets: ["素材库", "统一查看生成与处理结果"],
  tasks: ["任务记录", "查看所有任务状态、结果与错误"],
  system: ["系统状态", "检查服务、路径和 GPU 调度状态"],
};


const FALLBACK_IMAGE_OPTIONS = {
  force_4k: true,
  aspect_ratios: {
    "1:1": {label: "1:1 正方形", description: "头像、封面、方形配图", base_width: 1344, base_height: 1344, output_width: 4096, output_height: 4096},
    "2:3": {label: "2:3 社交媒体", description: "自拍、人物写真、社交平台", base_width: 1088, base_height: 1632, output_width: 2730, output_height: 4096},
    "3:4": {label: "3:4 经典比例", description: "经典拍照、人物与商品", base_width: 1152, base_height: 1536, output_width: 3072, output_height: 4096},
    "4:3": {label: "4:3 文章配图", description: "文章插图、传统照片、场景", base_width: 1536, base_height: 1152, output_width: 4096, output_height: 3072},
    "9:16": {label: "9:16 手机竖屏", description: "手机壁纸、竖屏海报、人像", base_width: 1008, base_height: 1792, output_width: 2160, output_height: 3840},
    "16:9": {label: "16:9 桌面横屏", description: "桌面壁纸、风景、电影画幅", base_width: 1792, base_height: 1008, output_width: 3840, output_height: 2160},
  },
  styles: {
    portrait_photo: {label: "人像摄影", icon: "📷", description: "自然肤质与真实镜头感", recommended_cfg: 6.5, recommended_steps: 32, sampler: "dpmpp_2m", scheduler: "karras"},
    cinematic_photo: {label: "电影写真", icon: "🎬", description: "电影光影与胶片调色", recommended_cfg: 7.0, recommended_steps: 32, sampler: "dpmpp_2m_sde", scheduler: "karras"},
    chinese_style: {label: "中国风", icon: "🏯", description: "东方审美与古典意境", recommended_cfg: 7.0, recommended_steps: 32, sampler: "dpmpp_2m", scheduler: "karras"},
    anime: {label: "动漫", icon: "✨", description: "清晰线稿与动画质感", recommended_cfg: 7.5, recommended_steps: 30, sampler: "euler_ancestral", scheduler: "normal"},
    render_3d: {label: "3D 渲染", icon: "🧊", description: "材质、灯光与空间体积", recommended_cfg: 7.0, recommended_steps: 32, sampler: "dpmpp_2m", scheduler: "karras"},
    cyberpunk: {label: "赛博朋克", icon: "🌃", description: "霓虹都市与未来科技", recommended_cfg: 7.5, recommended_steps: 32, sampler: "dpmpp_2m_sde", scheduler: "karras"},
    cg_animation: {label: "CG 动画", icon: "🎞️", description: "精致角色与动画电影感", recommended_cfg: 7.0, recommended_steps: 30, sampler: "dpmpp_2m", scheduler: "karras"},
    ink_wash: {label: "水墨画", icon: "🖌️", description: "留白、墨韵与东方笔触", recommended_cfg: 6.5, recommended_steps: 30, sampler: "euler_ancestral", scheduler: "normal"},
    oil_painting: {label: "油画", icon: "🎨", description: "厚重笔触与画布质感", recommended_cfg: 7.0, recommended_steps: 32, sampler: "dpmpp_2m", scheduler: "karras"},
    classical: {label: "古典", icon: "🏛️", description: "庄重构图与古典美学", recommended_cfg: 6.5, recommended_steps: 32, sampler: "dpmpp_2m", scheduler: "karras"},
    watercolor: {label: "水彩画", icon: "💧", description: "透明颜料与纸张晕染", recommended_cfg: 6.0, recommended_steps: 30, sampler: "euler_ancestral", scheduler: "normal"},
    cartoon: {label: "卡通", icon: "😊", description: "明快造型与轻松表达", recommended_cfg: 7.0, recommended_steps: 30, sampler: "euler_ancestral", scheduler: "normal"},
  },
  style_strengths: {
    weak: {label: "弱", description: "轻度保留风格，主体描述优先"},
    standard: {label: "标准", description: "风格与主体保持平衡"},
    strong: {label: "强", description: "明显强化所选风格"},
  },
  defaults: {aspect_ratio: "16:9", style_name: "portrait_photo", style_strength: "standard"},
};

const FALLBACK_IMAGE_MODELS = {
  default_model: "lustify",
  installed_count: 1,
  total_count: 4,
  models: [
    {key: "lustify", label: "高自由度写实", name: "Lustify SDXL v20", description: "当前已安装的高自由度 SDXL 写实模型", available: true, steps: 30, cfg: 6.0, sampler: "dpmpp_2m_sde", scheduler: "karras"},
    {key: "realvis", label: "通用写实", name: "RealVisXL V5.0", description: "通用人像、东亚人物与生活摄影", available: false, reason: "模型文件未安装", steps: 32, cfg: 5.0, sampler: "dpmpp_sde", scheduler: "karras"},
    {key: "juggernaut", label: "电影写实", name: "Juggernaut XL v9", description: "电影感、广告摄影与场景写实", available: false, reason: "模型文件未安装", steps: 35, cfg: 5.0, sampler: "dpmpp_2m", scheduler: "karras"},
    {key: "illustrious", label: "动漫插画", name: "Illustrious XL v2.0", description: "动漫、插画、卡通与 CG", available: false, reason: "模型文件未安装", steps: 30, cfg: 5.5, sampler: "dpmpp_2m", scheduler: "karras"},
  ],
};

let imageOptions = JSON.parse(JSON.stringify(FALLBACK_IMAGE_OPTIONS));
let imageModels = JSON.parse(JSON.stringify(FALLBACK_IMAGE_MODELS));
let selectedImageModelKey = "z_image_turbo";
let selectedPoseControl = "auto";
let selectedAppearanceEnhance = "auto";
let selectedAspectRatio = imageOptions.defaults.aspect_ratio;
let selectedStyleName = imageOptions.defaults.style_name;
let selectedStyleStrength = imageOptions.defaults.style_strength;

function currentImageModel() {
  return imageModels.models.find((item) => item.key === selectedImageModelKey) || null;
}

function renderImageModelOptions() {
  const grid = $("imageModelGrid");
  const installed = imageModels.models.filter((item) => item.available);
  const smartAvailable = installed.length > 0;
  const cards = [{
    key: "smart",
    label: "智能推荐",
    name: "按风格自动选择已安装模型",
    description: smartAvailable ? `当前可用 ${installed.length} 个模型` : "没有可用模型",
    available: smartAvailable,
    reason: smartAvailable ? "" : "请先安装至少一个模型",
  }, ...imageModels.models];
  grid.innerHTML = cards.map((model) => `
    <button type="button" class="choice-card model-choice ${model.key === selectedImageModelKey ? "selected" : ""} ${model.available ? "" : "unavailable"}"
            data-model-key="${escapeHtml(model.key)}" ${model.available ? "" : "disabled"}>
      <span class="model-icon">${model.key === "smart" ? "✨" : model.category === "anime" ? "🎨" : model.category === "cinematic" ? "🎬" : "◈"}</span>
      <span class="choice-copy"><strong>${escapeHtml(model.label)}</strong><small>${escapeHtml(model.name || "")}</small><em>${escapeHtml(model.available ? model.description : (model.reason || "未安装"))}</em></span>
      <span class="choice-check">✓</span>
    </button>
  `).join("");
  grid.querySelectorAll("[data-model-key]").forEach((button) => {
    button.addEventListener("click", () => selectImageModel(button.dataset.modelKey, true));
  });
}

function renderPoseControlOptions() {
  const values = {
    auto: ["自动", "由 Gemma 结构化语义编译器决定是否启用及使用哪个中性姿态模板"],
    off: ["关闭", "完全由模型自行构图"],
    light: ["轻度", "轻度约束单人姿态"],
    standard: ["标准", "明确约束人体结构"],
  };
  const group = $("imagePoseGroup");
  group.innerHTML = Object.entries(values).map(([key, value]) => `
    <button type="button" class="segment pose-segment ${key === selectedPoseControl ? "selected" : ""}" data-pose-control="${key}" title="${escapeHtml(value[1])}">${escapeHtml(value[0])}</button>
  `).join("");
  group.querySelectorAll("[data-pose-control]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedPoseControl = button.dataset.poseControl;
      document.querySelectorAll("[data-pose-control]").forEach((el) => el.classList.toggle("selected", el.dataset.poseControl === selectedPoseControl));
      syncImageSelection();
    });
  });
}

function renderAppearanceOptions() {
  const profiles = imageModels.appearance_enhancements || {};
  const values = [
    {key: "auto", label: "自动", description: "由 Gemma 语义编译器从当前模型兼容的已安装配置中选择", enabled: true},
    {key: "off", label: "关闭", description: "不加载人物外貌 LoRA", enabled: true},
    ...Object.entries(profiles).map(([key, profile]) => {
      const supportedModels = Array.isArray(profile.supported_models) ? profile.supported_models : [];
      const modelCompatible = selectedImageModelKey === "smart" || supportedModels.includes(selectedImageModelKey);
      const enabled = profile.available !== false && modelCompatible;
      const reason = profile.available === false
        ? (profile.reason || "LoRA 不可用")
        : modelCompatible
          ? (profile.description || "任务级加载配置化人物外貌 LoRA")
          : "当前生成模型不兼容";
      return {key, label: profile.label || key, description: reason, enabled};
    }),
  ];
  const current = values.find((item) => item.key === selectedAppearanceEnhance);
  if (!current || !current.enabled) selectedAppearanceEnhance = "auto";
  const group = $("imageAppearanceGroup");
  group.innerHTML = values.map((value) => `
    <button type="button" class="segment ${value.key === selectedAppearanceEnhance ? "selected" : ""} ${value.enabled ? "" : "disabled"}"
      data-appearance-enhance="${escapeHtml(value.key)}" title="${escapeHtml(value.description)}" ${value.enabled ? "" : "disabled"}>${escapeHtml(value.label)}</button>
  `).join("");
  group.querySelectorAll("[data-appearance-enhance]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      selectedAppearanceEnhance = button.dataset.appearanceEnhance;
      const profile = imageModels.appearance_enhancements?.[selectedAppearanceEnhance];
      if (profile?.default_strength != null) $("imageAppearanceStrength").value = profile.default_strength;
      document.querySelectorAll("[data-appearance-enhance]").forEach((el) => el.classList.toggle("selected", el.dataset.appearanceEnhance === selectedAppearanceEnhance));
      syncImageSelection();
    });
  });
}

function selectImageModel(key, applyRecommendation = false) {
  if (key === "smart") {
    if (!imageModels.models.some((item) => item.available)) return;
  } else {
    const model = imageModels.models.find((item) => item.key === key);
    if (!model || !model.available) return;
  }
  selectedImageModelKey = key;
  document.querySelectorAll("[data-model-key]").forEach((el) => el.classList.toggle("selected", el.dataset.modelKey === key));
  if (applyRecommendation && key !== "smart") {
    const model = currentImageModel();
    if (model) {
      if (model.steps != null) $("imageSteps").value = model.steps;
      if (model.cfg != null) $("imageCfg").value = model.cfg;
      if (model.sampler) $("imageSampler").value = model.sampler;
      if (model.scheduler) $("imageScheduler").value = model.scheduler;
    }
  }
  renderAppearanceOptions();
  syncImageSelection();
}

async function loadImageModels() {
  try {
    const models = await api("/api/image/models");
    if (models?.models) imageModels = models;
  } catch (error) {
    toast(`模型池读取失败，已使用内置状态：${error.message}`, true);
  }
  if (!imageModels.models.some((item) => item.available)) selectedImageModelKey = "smart";
  renderImageModelOptions();
  renderPoseControlOptions();
  renderAppearanceOptions();
  syncImageSelection();
}

function currentAspectPreset() {
  return imageOptions.aspect_ratios[selectedAspectRatio] || imageOptions.aspect_ratios["16:9"];
}

function currentStylePreset() {
  return imageOptions.styles[selectedStyleName] || imageOptions.styles.portrait_photo;
}

function currentStrengthPreset() {
  return imageOptions.style_strengths[selectedStyleStrength] || imageOptions.style_strengths.standard;
}

function renderImageAspectOptions() {
  const grid = $("imageAspectGrid");
  grid.innerHTML = Object.entries(imageOptions.aspect_ratios).map(([key, preset]) => `
    <button type="button" class="choice-card ratio-choice ${key === selectedAspectRatio ? "selected" : ""}" data-aspect-ratio="${escapeHtml(key)}">
      <span class="ratio-preview-wrap"><span class="ratio-preview" style="aspect-ratio:${preset.output_width}/${preset.output_height}"></span></span>
      <span class="choice-copy"><strong>${escapeHtml(preset.label)}</strong><small>${escapeHtml(preset.description)}</small><em>${preset.output_width}×${preset.output_height}</em></span>
      <span class="choice-check">✓</span>
    </button>
  `).join("");
  grid.querySelectorAll("[data-aspect-ratio]").forEach((button) => {
    button.addEventListener("click", () => selectAspectRatio(button.dataset.aspectRatio));
  });
}

function renderImageStyleOptions() {
  const grid = $("imageStyleGrid");
  grid.innerHTML = Object.entries(imageOptions.styles).map(([key, style]) => `
    <button type="button" class="choice-card style-choice style-${escapeHtml(key)} ${key === selectedStyleName ? "selected" : ""}" data-style-name="${escapeHtml(key)}">
      <span class="style-preview"><span>${escapeHtml(style.icon || "◈")}</span></span>
      <span class="choice-copy"><strong>${escapeHtml(style.label)}</strong><small>${escapeHtml(style.description)}</small></span>
      <span class="choice-check">✓</span>
    </button>
  `).join("");
  grid.querySelectorAll("[data-style-name]").forEach((button) => {
    button.addEventListener("click", () => selectStyleName(button.dataset.styleName, true));
  });
}

function renderImageStrengthOptions() {
  const group = $("imageStrengthGroup");
  group.innerHTML = Object.entries(imageOptions.style_strengths).map(([key, strength]) => `
    <button type="button" class="segment ${key === selectedStyleStrength ? "selected" : ""}" data-style-strength="${escapeHtml(key)}">${escapeHtml(strength.label)}</button>
  `).join("");
  group.querySelectorAll("[data-style-strength]").forEach((button) => {
    button.addEventListener("click", () => selectStyleStrengthValue(button.dataset.styleStrength));
  });
}

function selectAspectRatio(key) {
  if (!imageOptions.aspect_ratios[key]) return;
  selectedAspectRatio = key;
  document.querySelectorAll("[data-aspect-ratio]").forEach((el) => el.classList.toggle("selected", el.dataset.aspectRatio === key));
  syncImageSelection();
}

function selectStyleName(key, applyRecommendation = false) {
  if (!imageOptions.styles[key]) return;
  selectedStyleName = key;
  document.querySelectorAll("[data-style-name]").forEach((el) => el.classList.toggle("selected", el.dataset.styleName === key));
  if (applyRecommendation) applyStyleRecommendation();
  syncImageSelection();
}

function selectStyleStrengthValue(key) {
  if (!imageOptions.style_strengths[key]) return;
  selectedStyleStrength = key;
  document.querySelectorAll("[data-style-strength]").forEach((el) => el.classList.toggle("selected", el.dataset.styleStrength === key));
  syncImageSelection();
}

function applyStyleRecommendation() {
  const style = currentStylePreset();
  if (style.recommended_cfg != null) $("imageCfg").value = style.recommended_cfg;
  if (style.recommended_steps != null) $("imageSteps").value = style.recommended_steps;
  if (style.sampler) $("imageSampler").value = style.sampler;
  if (style.scheduler) $("imageScheduler").value = style.scheduler;
}

function syncImageSelection() {
  const aspect = currentAspectPreset();
  const style = currentStylePreset();
  const strength = currentStrengthPreset();
  $("imageAspectSummary").textContent = aspect.label;
  $("imageBaseSize").textContent = `${aspect.base_width}×${aspect.base_height}`;
  $("imageOutputSize").textContent = `${aspect.output_width}×${aspect.output_height}`;
  $("imageStyleSummary").textContent = `${style.label} · ${strength.label}`;
  const model = currentImageModel();
  $("imageModelSummary").textContent = selectedImageModelKey === "smart" ? "智能推荐" : `${model?.label || selectedImageModelKey} · ${model?.name || ""}`;
  $("imagePoseSummary").textContent = {auto: "自动", off: "关闭", light: "轻度", standard: "标准"}[selectedPoseControl] || selectedPoseControl;
  const appearanceProfile = imageModels.appearance_enhancements?.[selectedAppearanceEnhance];
  $("imageAppearanceSummary").textContent = selectedAppearanceEnhance === "auto"
    ? "自动"
    : selectedAppearanceEnhance === "off"
      ? "关闭"
      : (appearanceProfile?.label || selectedAppearanceEnhance);
  $("imageStrengthHint").textContent = strength.description;
  $("image4kHint").textContent = `模型池动态 checkpoint → 比例专用基础采样 ${aspect.base_width}×${aspect.base_height} → AI 超分 → 强制输出 ${aspect.output_width}×${aspect.output_height}。`;
}

async function loadImageOptions() {
  try {
    const options = await api("/api/image/presets");
    if (options?.aspect_ratios && options?.styles && options?.style_strengths) {
      imageOptions = options;
      selectedAspectRatio = options.defaults?.aspect_ratio || "16:9";
      selectedStyleName = options.defaults?.style_name || "portrait_photo";
      selectedStyleStrength = options.defaults?.style_strength || "standard";
    }
  } catch (error) {
    toast(`图片比例与风格配置读取失败，已使用内置配置：${error.message}`, true);
  }
  renderImageAspectOptions();
  renderImageStyleOptions();
  renderImageStrengthOptions();
  applyStyleRecommendation();
  syncImageSelection();
}

function nearestAspectRatio(width, height) {
  if (!width || !height) return "16:9";
  const ratio = width / height;
  return Object.entries(imageOptions.aspect_ratios).reduce((best, [key, preset]) => {
    const candidate = Number(preset.output_width) / Number(preset.output_height);
    const distance = Math.abs(candidate - ratio);
    return distance < best.distance ? {key, distance} : best;
  }, {key: "16:9", distance: Number.POSITIVE_INFINITY}).key;
}

const PHASE_PROGRESS = {
  STOPPED: 0,
  DRAINING: 20,
  RELEASING: 42,
  STARTING: 64,
  WARMING_UP: 84,
  READY: 100,
  FAILED: 100,
};

let currentPage = "dashboard";
let gpuState = null;
let capabilities = {};
let gemmaCapabilities = {chat: true, image_prompt: true, multimodal: false};
let gemmaChatMessages = [];
let latestGemmaChatReply = "";
let currentProcessor = "face_swapper";
let gpuPollTimer = null;
let activeTaskPollers = new Map();
let selectedFaceAssets = {source: null, target: null};
let assetPickerRole = null;

function toast(message, error = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast${error ? " error" : ""}`;
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data = null;
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) {
    throw new Error(data?.detail || `请求失败：${response.status}`);
  }
  return data;
}

function badgeClass(phase) {
  if (phase === "READY") return "badge ready";
  if (phase === "FAILED") return "badge failed";
  if (["DRAINING","RELEASING","STARTING","WARMING_UP"].includes(phase)) return "badge busy";
  return "badge";
}

function ownerCn(owner) {
  return {
    none: "无",
    gemma: "Gemma 助手",
    comfyui: "图片生成",
    facefusion: "人物与画面处理",
  }[owner] || owner;
}

function updateGpuUI(state) {
  gpuState = state;
  const used = state.memory_used_mb ?? 0;
  const total = state.memory_total_mb ?? 0;
  const free = state.memory_free_mb ?? 0;
  const percent = total ? Math.min(100, Math.round((used / total) * 100)) : 0;

  $("gpuOwnerMini").textContent = `GPU：${ownerCn(state.owner)}`;
  $("gpuMemoryMini").textContent = total ? `${used} / ${total} MB` : "显存未知";
  $("gpuDot").className = `dot ${state.phase === "READY" ? "ready" : state.phase === "FAILED" ? "failed" : state.phase === "STOPPED" ? "" : "busy"}`;

  $("gpuPhaseBadge").textContent = state.phase;
  $("gpuPhaseBadge").className = badgeClass(state.phase);
  $("gpuOwnerText").textContent = `当前所有者：${ownerCn(state.owner)}`;
  $("gpuMemoryText").textContent = total ? `已用 ${used} MB / 空闲 ${free} MB` : "显存：--";
  $("gpuMeterFill").style.width = `${percent}%`;
  $("gpuMessage").textContent = state.error || state.message;

  const gemmaReady = state.owner === "gemma" && state.desired_owner === "gemma" && state.phase === "READY";
  const imageReady = state.owner === "comfyui" && state.desired_owner === "comfyui" && state.phase === "READY";
  const videoReady = imageReady;
  const faceReady = state.owner === "facefusion" && state.desired_owner === "facefusion" && state.phase === "READY";

  $("gemmaWorkspaceText").textContent = gemmaReady ? "环境已就绪" : state.desired_owner === "gemma" ? state.message : "进入页面后自动激活";
  $("gemmaWorkspaceBadge").textContent = gemmaReady ? "READY" : state.desired_owner === "gemma" ? state.phase : "待激活";
  $("gemmaWorkspaceBadge").className = badgeClass(gemmaReady ? "READY" : state.desired_owner === "gemma" ? state.phase : "STOPPED");
  $("runGemmaButton").disabled = !gemmaReady;
  $("sendGemmaChatButton").disabled = !gemmaReady;

  $("imageWorkspaceText").textContent = imageReady ? "环境已就绪" : state.desired_owner === "comfyui" ? state.message : "进入页面后自动激活";
  $("imageWorkspaceBadge").textContent = imageReady ? "READY" : state.desired_owner === "comfyui" ? state.phase : "待激活";
  $("imageWorkspaceBadge").className = badgeClass(imageReady ? "READY" : state.desired_owner === "comfyui" ? state.phase : "STOPPED");
  $("generateImageButton").disabled = !imageReady;
  $("imageSubmitHint").textContent = imageReady ? "可以提交生成任务" : "GPU 工作区未就绪";

  if ($("videoWorkspaceText")) {
    $("videoWorkspaceText").textContent = videoReady ? "环境已就绪" : state.desired_owner === "comfyui" ? state.message : "进入页面后自动激活";
    $("videoWorkspaceBadge").textContent = videoReady ? "READY" : state.desired_owner === "comfyui" ? state.phase : "待激活";
    $("videoWorkspaceBadge").className = badgeClass(videoReady ? "READY" : state.desired_owner === "comfyui" ? state.phase : "STOPPED");
    $("generateVideoButton").disabled = !videoReady || !window.__h3Available;
    $("videoSubmitHint").textContent = videoReady ? (window.__h3Available ? "可以提交 H3 视频任务" : "正在检查 H3 能力") : "GPU 工作区未就绪";
  }

  $("faceWorkspaceText").textContent = faceReady ? "环境已就绪" : state.desired_owner === "facefusion" ? state.message : "进入页面后自动激活";
  $("faceWorkspaceBadge").textContent = faceReady ? "READY" : state.desired_owner === "facefusion" ? state.phase : "待激活";
  $("faceWorkspaceBadge").className = badgeClass(faceReady ? "READY" : state.desired_owner === "facefusion" ? state.phase : "STOPPED");
  $("runFaceButton").disabled = !faceReady || !capabilities[currentProcessor]?.available;
  $("faceSubmitHint").textContent = faceReady ? "可以提交处理任务" : "GPU 工作区未就绪";

  $("comfyStatus").textContent = state.owner === "comfyui" && state.phase === "READY" ? "已就绪" : state.desired_owner === "comfyui" ? state.phase : "未激活";
  $("comfyStatus").className = badgeClass(state.owner === "comfyui" && state.phase === "READY" ? "READY" : state.desired_owner === "comfyui" ? state.phase : "STOPPED");
  $("faceStatus").textContent = state.owner === "facefusion" && state.phase === "READY" ? "已就绪" : state.desired_owner === "facefusion" ? state.phase : "未激活";
  $("faceStatus").className = badgeClass(state.owner === "facefusion" && state.phase === "READY" ? "READY" : state.desired_owner === "facefusion" ? state.phase : "STOPPED");

  if (currentPage === "gemma" && state.desired_owner === "gemma") {
    updateOverlayForState(state, "Gemma 助手");
    if (gemmaReady && !window.__gemmaStatusRefreshing) {
      window.__gemmaStatusRefreshing = true;
      loadGemmaStatus().finally(() => { window.__gemmaStatusRefreshing = false; });
    }
  }
  if (currentPage === "image") {
    if (state.desired_owner === "gemma") updateOverlayForState(state, "Gemma 语义编译");
    if (state.desired_owner === "comfyui") updateOverlayForState(state, "图片生成");
  }
  if (currentPage === "video" && state.desired_owner === "comfyui") updateOverlayForState(state, "视频生成");
  if (currentPage === "facefusion" && state.desired_owner === "facefusion") updateOverlayForState(state, "人物与画面处理");
}

async function refreshGpu() {
  try {
    updateGpuUI(await api("/api/gpu/status"));
  } catch (error) {
    toast(error.message, true);
  }
}

function updateOverlayForState(state, label) {
  if (state.phase === "READY" && state.owner === state.desired_owner) {
    $("switchOverlay").classList.add("hidden");
    return;
  }
  $("switchOverlay").classList.remove("hidden");
  $("overlayTitle").textContent = `正在准备${label}环境`;
  $("overlayMessage").textContent = state.error || state.message;
  $("overlayProgress").style.width = `${PHASE_PROGRESS[state.phase] ?? 5}%`;
  if (state.phase === "FAILED") {
    $("overlayClose").classList.remove("hidden");
  } else {
    $("overlayClose").classList.add("hidden");
  }
}

async function activateWorkspace(owner) {
  $("switchOverlay").classList.remove("hidden");
  $("overlayClose").classList.add("hidden");
  $("overlayTitle").textContent = {
    gemma: "正在准备 Gemma 助手环境",
    comfyui: currentPage === "video" ? "正在准备视频生成环境" : "正在准备图片生成环境",
    facefusion: "正在准备人物与画面处理环境",
  }[owner] || "正在准备 GPU 工作区";
  $("overlayMessage").textContent = "正在提交 GPU 工作区切换请求";
  $("overlayProgress").style.width = "5%";
  try {
    const state = await api(`/api/gpu/transition/${owner}`, {method: "POST"});
    updateGpuUI(state);
  } catch (error) {
    $("overlayMessage").textContent = error.message;
    $("overlayClose").classList.remove("hidden");
    toast(error.message, true);
  }
}

PAGE_META.video = ["视频生成", "MiniMax H3 T2VA / FL2VA / REF2VA 音视频生成"];

async function navigate(page) {
  currentPage = page;
  document.querySelectorAll(".page").forEach((el) => el.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
  $(`page-${page}`).classList.add("active");
  $("pageTitle").textContent = PAGE_META[page][0];
  $("pageSubTitle").textContent = PAGE_META[page][1];
  location.hash = page;

  if (page === "gemma") {
    await activateWorkspace("gemma");
  } else if (page === "image") {
    await activateWorkspace("comfyui");
  } else if (page === "video") {
    await activateWorkspace("comfyui");
    await loadVideoCapabilities();
  } else if (page === "facefusion") {
    await activateWorkspace("facefusion");
  } else if (page === "assets") {
    await loadAssets();
  } else if (page === "tasks") {
    await loadTasks();
  } else if (page === "system") {
    await loadSystem();
  } else if (page === "dashboard") {
    await loadDashboard();
  }
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => navigate(button.dataset.page));
});
document.querySelectorAll(".jump").forEach((button) => {
  button.addEventListener("click", () => navigate(button.dataset.target));
});
$("overlayClose").addEventListener("click", () => $("switchOverlay").classList.add("hidden"));
$("refreshButton").addEventListener("click", async () => {
  await refreshGpu();
  if (currentPage === "dashboard") await loadDashboard();
  if (currentPage === "tasks") await loadTasks();
  if (currentPage === "assets") await loadAssets();
});

async function loadDashboard() {
  try {
    const [health, tasks] = await Promise.all([api("/api/health"), api("/api/tasks?limit=5")]);
    const gemmaActive = health.gpu?.owner === "gemma" && health.gpu?.phase === "READY";
    $("gemmaStatus").textContent = gemmaActive ? "已就绪" : "待切换";
    $("gemmaStatus").className = badgeClass(gemmaActive ? "READY" : "STOPPED");
    $("gemmaStatus").title = gemmaActive ? (health.gemma_detail?.message || "") : "进入 LLM 页面后自动切换 GPU";
    renderRecentTasks(tasks);
  } catch (error) {
    toast(error.message, true);
  }
}


async function loadGemmaStatus() {
  try {
    const status = await api("/api/gemma/capabilities");
    gemmaCapabilities = status;
    const ready = Boolean(status.ready) && gpuState?.owner === "gemma" && gpuState?.phase === "READY";
    $("gemmaPageStatus").textContent = ready ? "READY" : "不可用";
    $("gemmaPageStatus").className = badgeClass(ready ? "READY" : "FAILED");
    $("gemmaServiceMessage").textContent = status.message || (ready ? "LLM 已就绪" : "LLM 不可用");
    $("gemmaServiceMessage").className = `message ${ready ? "success" : "error"}`;
    $("runGemmaButton").disabled = !ready;
    $("sendGemmaChatButton").disabled = !ready;
    $("gemmaMultimodalStatus").textContent = status.multimodal_message || "";
    $("gemmaChatImage").disabled = !status.multimodal;
    $("gemmaImageUploadLabel").classList.toggle("disabled", !status.multimodal);
  } catch (error) {
    $("gemmaPageStatus").textContent = "不可用";
    $("gemmaPageStatus").className = badgeClass("FAILED");
    $("gemmaServiceMessage").textContent = error.message;
    $("gemmaServiceMessage").className = "message error";
    $("runGemmaButton").disabled = true;
    $("sendGemmaChatButton").disabled = true;
  }
}

function setGemmaPanel(mode) {
  const chat = mode === "chat";
  $("gemmaChatPanel").classList.toggle("hidden", !chat);
  $("gemmaPromptPanel").classList.toggle("hidden", chat);
  $("gemmaChatTab").classList.toggle("active", chat);
  $("gemmaPromptTab").classList.toggle("active", !chat);
}

function renderGemmaChat() {
  const history = $("gemmaChatHistory");
  if (!gemmaChatMessages.length) {
    history.className = "chat-history empty";
    history.textContent = "开始一段正常对话";
    return;
  }
  history.className = "chat-history";
  history.innerHTML = gemmaChatMessages.map((item) => `
    <div class="chat-message ${item.role}">
      <strong>${item.role === "user" ? "你" : "Gemma"}</strong>
      <div>${escapeHtml(item.content).replace(/\n/g, "<br>")}</div>
    </div>
  `).join("");
  history.scrollTop = history.scrollHeight;
}

async function sendGemmaChat() {
  const text = $("gemmaChatInput").value.trim();
  if (!text) return toast("请输入消息", true);
  const file = $("gemmaChatImage").files?.[0] || null;
  gemmaChatMessages.push({role: "user", content: text});
  renderGemmaChat();
  $("gemmaChatInput").value = "";
  $("gemmaChatRunStatus").textContent = "处理中...";
  $("sendGemmaChatButton").disabled = true;
  try {
    let result;
    if (file) {
      if (!gemmaCapabilities.multimodal) throw new Error(gemmaCapabilities.multimodal_message || "当前未配置视觉模型");
      const form = new FormData();
      form.append("messages_json", JSON.stringify(gemmaChatMessages));
      form.append("image", file);
      form.append("temperature", "0.5");
      form.append("max_tokens", "2048");
      result = await api("/api/gemma/chat/multimodal", {method: "POST", body: form});
    } else {
      result = await api("/api/gemma/chat", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({messages: gemmaChatMessages, temperature: 0.7, max_tokens: 2048}),
      });
    }
    latestGemmaChatReply = result.content || "";
    gemmaChatMessages.push({role: "assistant", content: latestGemmaChatReply});
    localStorage.setItem("aiStudioGemmaChat", JSON.stringify(gemmaChatMessages.slice(-40)));
    renderGemmaChat();
    $("gemmaChatRunStatus").textContent = `完成 · ${result.model || "gemma"}`;
    $("gemmaChatImage").value = "";
    $("gemmaChatImageName").textContent = "当前未附加图片";
  } catch (error) {
    $("gemmaChatRunStatus").textContent = "失败";
    toast(error.message, true);
  } finally {
    $("sendGemmaChatButton").disabled = !(gpuState?.owner === "gemma" && gpuState?.phase === "READY");
  }
}

function renderRecentTasks(tasks) {
  const el = $("recentTasks");
  if (!tasks.length) {
    el.className = "list empty";
    el.textContent = "暂无任务";
    return;
  }
  el.className = "list";
  el.innerHTML = tasks.map((task) => `
    <div class="task-row">
      <div><strong>${escapeHtml(task.title)}</strong><small class="muted">${escapeHtml(task.operation)}</small></div>
      <span>${statusCn(task.status)}</span>
      <span>${task.progress}%</span>
    </div>
  `).join("");
}

$("gemmaChatTab").addEventListener("click", () => setGemmaPanel("chat"));
$("gemmaPromptTab").addEventListener("click", () => setGemmaPanel("prompt"));
$("sendGemmaChatButton").addEventListener("click", sendGemmaChat);
$("gemmaChatInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) sendGemmaChat();
});
$("clearGemmaChatButton").addEventListener("click", () => {
  gemmaChatMessages = [];
  latestGemmaChatReply = "";
  localStorage.removeItem("aiStudioGemmaChat");
  renderGemmaChat();
});
$("gemmaChatImage").addEventListener("change", () => {
  const file = $("gemmaChatImage").files?.[0];
  $("gemmaChatImageName").textContent = file ? file.name : "当前未附加图片";
});
$("chatToImageButton").addEventListener("click", async () => {
  if (!latestGemmaChatReply) return toast("当前没有可发送的助手回复", true);
  $("imagePositive").value = latestGemmaChatReply;
  await navigate("image");
});

$("runGemmaButton").addEventListener("click", async () => {
  const text = $("gemmaInput").value.trim();
  if (!text) return toast("请先输入内容", true);
  $("runGemmaButton").disabled = true;
  $("gemmaRunStatus").textContent = "处理中...";
  try {
    const result = await api("/api/gemma", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        text,
        mode: $("gemmaMode").value,
        width: Number($("gemmaWidth").value),
        height: Number($("gemmaHeight").value),
      }),
    });
    $("gemmaPositive").value = result.positive_prompt || "";
    $("gemmaNegative").value = result.negative_prompt || "";
    $("gemmaNotes").textContent = result.notes || "";
    $("gemmaRunStatus").textContent = "完成";
    await loadGemmaStatus();
  } catch (error) {
    $("gemmaRunStatus").textContent = "失败";
    toast(error.message, true);
  } finally {
    $("runGemmaButton").disabled = !(gpuState?.owner === "gemma" && gpuState?.phase === "READY");
  }
});

$("sendToImageButton").addEventListener("click", async () => {
  const positive = $("gemmaPositive").value.trim();
  if (!positive) return toast("没有可发送的正向提示词", true);
  $("imagePositive").value = positive;
  $("imageNegative").value = $("gemmaNegative").value;
  selectAspectRatio(nearestAspectRatio(Number($("gemmaWidth").value), Number($("gemmaHeight").value)));
  await navigate("image");
});
$("copyPromptButton").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("gemmaPositive").value);
  toast("正向提示词已复制");
});

$("imageOptimizeButton").addEventListener("click", async () => {
  const text = $("imagePositive").value.trim();
  if (!text) return toast("请先输入图片描述或提示词", true);
  $("imageOptimizeButton").disabled = true;
  try {
    const aspect = currentAspectPreset();
    const result = await api("/api/gemma", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        text,
        mode: "optimize",
        width: Number(aspect.base_width),
        height: Number(aspect.base_height),
      }),
    });
    $("imagePositive").value = result.positive_prompt || text;
    $("imageNegative").value = result.negative_prompt || $("imageNegative").value;
    toast("Gemma 优化完成");
  } catch (error) {
    toast(error.message, true);
  } finally {
    $("imageOptimizeButton").disabled = false;
    if (currentPage === "image") await activateWorkspace("comfyui");
  }
});

$("generateImageButton").addEventListener("click", async () => {
  if (!$("imagePositive").value.trim()) return toast("正向提示词不能为空", true);
  if (!imageOptions.aspect_ratios[selectedAspectRatio]) return toast("请选择画面比例", true);
  if (!imageOptions.styles[selectedStyleName]) return toast("请选择图片风格", true);
  if (!imageOptions.style_strengths[selectedStyleStrength]) return toast("请选择风格强度", true);

  const form = new FormData();
  form.append("positive_prompt", $("imagePositive").value.trim());
  form.append("negative_prompt", $("imageNegative").value.trim());
  form.append("model_key", selectedImageModelKey);
  form.append("pose_control", selectedPoseControl);
  form.append("appearance_enhance_mode", selectedAppearanceEnhance);
  form.append("appearance_lora_strength", $("imageAppearanceStrength").value);
  form.append("aspect_ratio", selectedAspectRatio);
  form.append("style_name", selectedStyleName);
  form.append("style_strength", selectedStyleStrength);
  form.append("steps", $("imageSteps").value);
  form.append("cfg", $("imageCfg").value);
  form.append("seed", $("imageSeed").value);
  form.append("sampler", $("imageSampler").value);
  form.append("scheduler", $("imageScheduler").value);
  form.append("count", $("imageCount").value);

  $("generateImageButton").disabled = true;
  try {
    const task = await api("/api/image/tasks", {method: "POST", body: form});
    renderTaskProgress(task, $("imageResults"), "image");
    pollTask(task.task_id, $("imageResults"), "image");
  } catch (error) {
    toast(error.message, true);
    $("generateImageButton").disabled = false;
  }
});

async function loadCapabilities() {
  capabilities = await api("/api/facefusion/capabilities");
  if (capabilities._error) {
    toast(`FaceFusion 功能检查失败：${capabilities._error}`, true);
    return;
  }
  renderProcessorTabs();
  selectProcessor(currentProcessor);
}

function renderProcessorTabs() {
  const tabs = $("processorTabs");
  tabs.innerHTML = Object.entries(capabilities).map(([key, spec]) => `
    <button class="tab ${key === currentProcessor ? "active" : ""} ${spec.available ? "" : "unavailable"}"
            data-processor="${key}">
      ${escapeHtml(spec.label)}${spec.available ? "" : "（环境未识别）"}
    </button>
  `).join("");
  tabs.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => selectProcessor(button.dataset.processor));
  });
}

function selectProcessor(key) {
  currentProcessor = key;
  const spec = capabilities[key];
  if (!spec) return;
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.dataset.processor === key));
  $("processorTitle").textContent = spec.label;
  $("processorDescription").textContent = spec.description;
  const sourceVisible = Boolean(spec.source_required || spec.source_kind);
  $("sourceUploadLabel").classList.toggle("hidden", !sourceVisible);
  if (!sourceVisible) clearFaceSelection("source");
  $("sourceLabelText").textContent = spec.source_kind === "audio" ? "来源音频" : "来源人物图片";
  $("sourceHint").textContent = spec.source_required ? "此功能必须上传来源素材" : "可选";
  $("faceSource").accept = spec.source_kind === "audio" ? "audio/*" : "image/*";
  $("faceTarget").accept = (spec.target_kinds || []).includes("video") ? "image/*,video/*" : "image/*";
  $("targetHint").textContent = `支持：${(spec.target_kinds || []).map((x) => x === "image" ? "图片" : "视频").join("、")}`;
  $("authorizationLine").classList.toggle("hidden", !["face_swapper","deep_swapper","expression_restorer"].includes(key));
  renderProcessorParams(spec);
  const ready = gpuState?.owner === "facefusion" && gpuState?.phase === "READY" && gpuState?.desired_owner === "facefusion";
  $("runFaceButton").disabled = !ready || !spec.available;
  $("faceSubmitHint").textContent = spec.available ? (ready ? "可以提交处理任务" : "GPU 工作区未就绪") : "当前 FaceFusion 环境未识别该处理器";
}

function renderProcessorParams(spec) {
  const params = [...(spec.params || []), ...(spec.common_params || [])];
  $("processorParams").innerHTML = params.map((param) => fieldHtml(param)).join("");
  $("processorParams").querySelectorAll('input[type="range"]').forEach((input) => {
    const output = input.parentElement.querySelector(".range-value");
    const sync = () => output.textContent = input.value;
    input.addEventListener("input", sync);
    sync();
  });
}

function fieldHtml(param) {
  const name = escapeHtml(param.name);
  const hint = param.hint ? `<small class="muted">${escapeHtml(param.hint)}</small>` : "";
  if (param.type === "select") {
    return `<label>${escapeHtml(param.label)}
      <select data-param="${name}">
        ${(param.options || []).map((opt) => `<option value="${escapeHtml(opt)}" ${String(opt) === String(param.default) ? "selected" : ""}>${escapeHtml(opt)}</option>`).join("")}
      </select>${hint}</label>`;
  }
  if (param.type === "multiselect") {
    const defaults = Array.isArray(param.default) ? param.default : [];
    return `<label>${escapeHtml(param.label)}
      <select data-param="${name}" multiple size="${Math.min(4, (param.options || []).length)}">
        ${(param.options || []).map((opt) => `<option value="${escapeHtml(opt)}" ${defaults.includes(opt) ? "selected" : ""}>${escapeHtml(opt)}</option>`).join("")}
      </select>${hint}</label>`;
  }
  if (param.type === "range") {
    return `<label>${escapeHtml(param.label)}：<span class="range-value"></span>
      <input data-param="${name}" type="range" value="${param.default}" min="${param.min}" max="${param.max}" step="${param.step}" />${hint}</label>`;
  }
  return `<label>${escapeHtml(param.label)}
    <input data-param="${name}" type="text" value="${escapeHtml(param.default ?? "")}" />${hint}</label>`;
}

function collectProcessorParams() {
  const params = {};
  $("processorParams").querySelectorAll("[data-param]").forEach((el) => {
    if (el.multiple) {
      params[el.dataset.param] = Array.from(el.selectedOptions).map((option) => option.value);
    } else if (el.type === "range" || el.type === "number") {
      params[el.dataset.param] = Number(el.value);
    } else {
      params[el.dataset.param] = el.value;
    }
  });
  return params;
}

$("runFaceButton").addEventListener("click", async () => {
  const spec = capabilities[currentProcessor];
  const target = $("faceTarget").files[0];
  const source = $("faceSource").files[0];
  const targetAsset = selectedFaceAssets.target;
  const sourceAsset = selectedFaceAssets.source;
  if (!target && !targetAsset) return toast("请选择目标素材：本地上传或从素材库选取", true);
  if (spec.source_required && !source && !sourceAsset) return toast("此功能需要选择来源素材", true);
  if (["face_swapper","deep_swapper","expression_restorer"].includes(currentProcessor) && !$("authorizedAdult").checked) {
    return toast("请先确认人物身份素材已获得授权", true);
  }
  const form = new FormData();
  form.append("processor", currentProcessor);
  form.append("params_json", JSON.stringify(collectProcessorParams()));
  form.append("authorized_adult", String($("authorizedAdult").checked));
  if (target) form.append("target", target);
  if (source) form.append("source", source);
  if (!target && targetAsset) form.append("target_asset_url", targetAsset.url);
  if (!source && sourceAsset) form.append("source_asset_url", sourceAsset.url);

  $("runFaceButton").disabled = true;
  try {
    const task = await api("/api/facefusion/tasks", {method: "POST", body: form});
    renderTaskProgress(task, $("faceResults"), "face");
    pollTask(task.task_id, $("faceResults"), "face");
  } catch (error) {
    toast(error.message, true);
    $("runFaceButton").disabled = false;
  }
});

function renderTaskProgress(task, container, kind) {
  container.dataset.activeTaskId = task.task_id;
  container.className = kind === "image" ? "media-grid image-result-grid" : "media-grid";
  container.innerHTML = `
    <article class="media-card" style="grid-column:1/-1">
      <strong>${escapeHtml(task.title)}</strong>
      <p id="task-message-${task.task_id}">${escapeHtml(task.message)}</p>
      <div class="progress"><div id="task-progress-${task.task_id}" style="width:${task.progress}%"></div></div>
      <pre id="task-log-${task.task_id}" style="max-height:220px"></pre>
    </article>`;
}

function pollTask(taskId, container, kind) {
  if (activeTaskPollers.has(taskId)) return;
  const timer = setInterval(async () => {
    try {
      const task = await api(`/api/tasks/${taskId}`);
      const message = $(`task-message-${taskId}`);
      const bar = $(`task-progress-${taskId}`);
      const log = $(`task-log-${taskId}`);
      if (message) message.textContent = task.error || task.message;
      if (bar) bar.style.width = `${task.progress}%`;
      if (log) log.textContent = (task.logs || []).slice(-30).join("\n");

      if (task.status === "completed") {
        clearInterval(timer);
        activeTaskPollers.delete(taskId);
        if (container.dataset.activeTaskId === taskId) {
          renderOutputs(task.output_files, container, kind);
        }
        if (kind === "image") $("generateImageButton").disabled = false;
        if (kind === "face") $("runFaceButton").disabled = false;
        toast("任务完成");
      } else if (task.status === "failed") {
        clearInterval(timer);
        activeTaskPollers.delete(taskId);
        if (kind === "image") $("generateImageButton").disabled = false;
        if (kind === "face") $("runFaceButton").disabled = false;
        toast(task.error || "任务失败", true);
      }
    } catch (error) {
      clearInterval(timer);
      activeTaskPollers.delete(taskId);
      toast(error.message, true);
    }
  }, 1800);
  activeTaskPollers.set(taskId, timer);
}

function renderOutputs(urls, container, kind = "") {
  if (!urls?.length) {
    container.className = "media-grid empty";
    container.textContent = "没有输出文件";
    return;
  }
  container.className = kind === "image" ? "media-grid image-result-grid" : "media-grid";
  container.innerHTML = urls.map((url) => mediaCard(url, "", {result: true, kind})).join("");
  bindImageDimensionLabels(container);
}

function bindImageDimensionLabels(container) {
  container.querySelectorAll("img[data-dimension-target]").forEach((img) => {
    const id = img.dataset.dimensionTarget;
    const target = document.getElementById(id);
    const sync = () => {
      if (target && img.naturalWidth && img.naturalHeight) {
        target.textContent = `${img.naturalWidth}×${img.naturalHeight}`;
      }
    };
    if (img.complete) sync();
    img.addEventListener("load", sync, {once: true});
  });
}

function mediaPreview(url, name = "") {
  const lower = url.toLowerCase().split("?")[0];
  if (/\.(png|jpg|jpeg|webp|bmp|gif)$/.test(lower)) {
    return `<img src="${escapeHtml(url)}?t=${Date.now()}" alt="${escapeHtml(name)}" />`;
  }
  if (/\.(mp4|webm|mov|mkv|avi)$/.test(lower)) {
    return `<video src="${escapeHtml(url)}" controls></video>`;
  }
  if (/\.(mp3|wav|m4a|flac|aac)$/.test(lower)) {
    return `<audio src="${escapeHtml(url)}" controls></audio>`;
  }
  return `<a href="${escapeHtml(url)}" target="_blank">打开文件</a>`;
}

function mediaCard(url, name = "", options = {}) {
  const filename = name || url.split("/").pop();
  const dimensionId = `image-dim-${Math.random().toString(36).slice(2)}`;
  const lowerUrl = url.toLowerCase().split("?")[0];
  const isImage = /\.(png|jpg|jpeg|webp|bmp|gif)$/.test(lowerUrl);
  const isImageResult = Boolean(isImage && options.result && options.kind === "image");
  let actions = "";
  if (options.result) {
    actions = `<div class="media-actions">
      <button class="btn primary" data-media-action="save" data-url="${escapeHtml(url)}" data-name="${escapeHtml(filename)}">保存到素材库</button>
      <button class="btn" data-media-action="use-source" data-url="${escapeHtml(url)}" data-name="${escapeHtml(filename)}">作为来源</button>
      <button class="btn secondary" data-media-action="use-target" data-url="${escapeHtml(url)}" data-name="${escapeHtml(filename)}">作为处理目标</button>
    </div>`;
  } else if (options.library) {
    actions = `<div class="media-actions">
      <button class="btn" data-media-action="library-source" data-url="${escapeHtml(url)}" data-name="${escapeHtml(filename)}" data-mime="${escapeHtml(options.mime || "")}">作为来源</button>
      <button class="btn secondary" data-media-action="library-target" data-url="${escapeHtml(url)}" data-name="${escapeHtml(filename)}" data-mime="${escapeHtml(options.mime || "")}">作为处理目标</button>
      <button class="btn danger" data-delete-asset="1" data-url="${escapeHtml(url)}" data-name="${escapeHtml(filename)}">删除素材</button>
    </div>`;
  }
  const rawPreview = mediaPreview(url, filename);
  const preview = isImage
    ? rawPreview.replace("<img ", `<img data-dimension-target="${dimensionId}" `)
    : rawPreview;
  const dimension = isImage
    ? `<span id="${dimensionId}" class="media-dimensions">读取尺寸...</span>`
    : "";
  return `<article class="media-card ${isImageResult ? "result-image-card" : ""}">
    ${preview}
    <div class="media-meta"><span>${escapeHtml(filename)}</span>${dimension}<a href="${escapeHtml(url)}" target="_blank">打开</a></div>
    ${actions}
  </article>`;
}

async function saveToLibrary(url, name = "") {
  const form = new FormData();
  form.append("url", url);
  form.append("name", name || "");
  return await api("/api/assets/save", {method: "POST", body: form});
}

async function deleteLibraryAsset(url, name = "") {
  const displayName = name || url.split("/").pop() || "该素材";
  if (!window.confirm(`确定永久删除素材“${displayName}”吗？\n\n删除素材库副本不会删除任务记录中的原始生成结果。`)) {
    return false;
  }
  const form = new FormData();
  form.append("url", url);
  await api("/api/assets/delete", {method: "POST", body: form});

  for (const role of ["source", "target"]) {
    if (selectedFaceAssets[role]?.url === url) clearFaceSelection(role);
  }
  toast(`已删除素材：${displayName}`);
  return true;
}

async function useResultInFace(role, url, name) {
  const asset = await saveToLibrary(url, name);
  setFaceSelection(role, asset);
  toast(role === "source" ? "已保存到素材库并设为来源素材" : "已保存到素材库并设为目标素材");
  await navigate("facefusion");
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-media-action]");
  if (!button) return;
  const action = button.dataset.mediaAction;
  const url = button.dataset.url;
  const name = button.dataset.name || "";
  button.disabled = true;
  try {
    if (action === "save") {
      await saveToLibrary(url, name);
      button.textContent = "已保存";
      toast("已保存到素材库");
    } else if (action === "use-source") {
      await useResultInFace("source", url, name);
    } else if (action === "use-target") {
      await useResultInFace("target", url, name);
    } else if (action === "library-source") {
      setFaceSelection("source", {url, name, mime: button.dataset.mime || ""});
      await navigate("facefusion");
    } else if (action === "library-target") {
      setFaceSelection("target", {url, name, mime: button.dataset.mime || ""});
      await navigate("facefusion");
    }
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-asset]");
  if (!button || button.closest("#assetPickerGrid")) return;
  event.preventDefault();
  button.disabled = true;
  try {
    const deleted = await deleteLibraryAsset(button.dataset.url, button.dataset.name || "");
    if (deleted) await loadAssets();
    else button.disabled = false;
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
});

async function loadAssets() {
  try {
    const items = await api("/api/assets?limit=200");
    const grid = $("assetGrid");
    if (!items.length) {
      grid.className = "media-grid empty";
      grid.textContent = "暂无已保存素材。可从生成结果保存，或点击右上角上传素材。";
      return;
    }
    grid.className = "media-grid";
    grid.innerHTML = items.map((item) => mediaCard(item.url, item.name, {library: true, mime: item.mime})).join("");
  } catch (error) {
    toast(error.message, true);
  }
}
$("refreshAssetsButton").addEventListener("click", loadAssets);

$("assetUploadInput").addEventListener("change", async () => {
  const files = Array.from($("assetUploadInput").files || []);
  if (!files.length) return;
  try {
    for (const file of files) {
      const form = new FormData();
      form.append("file", file);
      await api("/api/assets/upload", {method: "POST", body: form});
    }
    $("assetUploadInput").value = "";
    toast(`已上传 ${files.length} 个素材`);
    await loadAssets();
  } catch (error) {
    toast(error.message, true);
  }
});

function selectionElement(role) {
  return role === "source" ? $("faceSourceSelection") : $("faceTargetSelection");
}

function fileInput(role) {
  return role === "source" ? $("faceSource") : $("faceTarget");
}

function setFaceSelection(role, asset) {
  selectedFaceAssets[role] = asset;
  fileInput(role).value = "";
  const box = selectionElement(role);
  const preview = mediaPreview(asset.url, asset.name || "");
  box.innerHTML = `<div class="selected-main">${preview}<div><b>${role === "source" ? "来源素材" : "目标素材"}</b><div class="selected-name">${escapeHtml(asset.name || asset.url.split("/").pop())}</div></div></div>
    <button type="button" class="btn" data-clear-face-asset="${role}">清除</button>`;
  box.classList.remove("hidden");
}

function showLocalSelection(role, file) {
  selectedFaceAssets[role] = null;
  const box = selectionElement(role);
  box.innerHTML = `<div class="selected-main"><div><b>${role === "source" ? "本地来源素材" : "本地目标素材"}</b><div class="selected-name">${escapeHtml(file.name)}</div></div></div>
    <button type="button" class="btn" data-clear-face-asset="${role}">清除</button>`;
  box.classList.remove("hidden");
}

function clearFaceSelection(role) {
  selectedFaceAssets[role] = null;
  fileInput(role).value = "";
  const box = selectionElement(role);
  box.innerHTML = "";
  box.classList.add("hidden");
}

$("faceSource").addEventListener("change", () => {
  const file = $("faceSource").files[0];
  if (file) showLocalSelection("source", file); else clearFaceSelection("source");
});
$("faceTarget").addEventListener("change", () => {
  const file = $("faceTarget").files[0];
  if (file) showLocalSelection("target", file); else clearFaceSelection("target");
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-clear-face-asset]");
  if (button) clearFaceSelection(button.dataset.clearFaceAsset);
});

function assetAllowedForRole(asset, role) {
  const mime = asset.mime || "";
  const lower = asset.name.toLowerCase();
  const isImage = mime.startsWith("image/") || /\.(png|jpg|jpeg|webp|bmp|gif)$/.test(lower);
  const isVideo = mime.startsWith("video/") || /\.(mp4|webm|mov|mkv|avi)$/.test(lower);
  const isAudio = mime.startsWith("audio/") || /\.(mp3|wav|m4a|flac|aac)$/.test(lower);
  const spec = capabilities[currentProcessor] || {};
  if (role === "source") return spec.source_kind === "audio" ? isAudio : isImage;
  const kinds = spec.target_kinds || ["image"];
  return (kinds.includes("image") && isImage) || (kinds.includes("video") && isVideo);
}

async function openAssetPicker(role) {
  assetPickerRole = role;
  $("assetPicker").classList.remove("hidden");
  $("assetPickerTitle").textContent = role === "source" ? "选择来源素材" : "选择目标素材";
  $("assetPickerHint").textContent = role === "source" ? "只显示当前功能支持的来源素材" : "只显示当前功能支持的目标素材";
  const grid = $("assetPickerGrid");
  grid.className = "media-grid empty";
  grid.textContent = "正在读取素材库...";
  try {
    const items = (await api("/api/assets?limit=500")).filter((item) => assetAllowedForRole(item, role));
    if (!items.length) {
      grid.className = "media-grid empty";
      grid.textContent = "素材库中没有当前功能可用的素材，请先保存生成结果或上传素材。";
      return;
    }
    grid.className = "media-grid";
    grid.innerHTML = items.map((item) => `<article class="media-card" data-pick-url="${escapeHtml(item.url)}" data-pick-name="${escapeHtml(item.name)}" data-pick-mime="${escapeHtml(item.mime || "")}">
      ${mediaPreview(item.url, item.name)}
      <div class="media-meta"><span>${escapeHtml(item.name)}</span><b>选择</b></div>
      <div class="media-actions">
        <button type="button" class="btn danger" data-delete-asset="1" data-url="${escapeHtml(item.url)}" data-name="${escapeHtml(item.name)}">删除素材</button>
      </div>
    </article>`).join("");
  } catch (error) {
    grid.className = "media-grid empty";
    grid.textContent = error.message;
  }
}

$("chooseFaceSourceAsset").addEventListener("click", () => openAssetPicker("source"));
$("chooseFaceTargetAsset").addEventListener("click", () => openAssetPicker("target"));
$("closeAssetPicker").addEventListener("click", () => $("assetPicker").classList.add("hidden"));
$("assetPickerGrid").addEventListener("click", async (event) => {
  const deleteButton = event.target.closest("[data-delete-asset]");
  if (deleteButton) {
    event.preventDefault();
    event.stopPropagation();
    deleteButton.disabled = true;
    try {
      const deleted = await deleteLibraryAsset(deleteButton.dataset.url, deleteButton.dataset.name || "");
      if (deleted && assetPickerRole) await openAssetPicker(assetPickerRole);
      else deleteButton.disabled = false;
    } catch (error) {
      toast(error.message, true);
      deleteButton.disabled = false;
    }
    return;
  }

  const card = event.target.closest("[data-pick-url]");
  if (!card || !assetPickerRole) return;
  setFaceSelection(assetPickerRole, {
    url: card.dataset.pickUrl,
    name: card.dataset.pickName,
    mime: card.dataset.pickMime,
  });
  $("assetPicker").classList.add("hidden");
  toast(assetPickerRole === "source" ? "已选择来源素材" : "已选择目标素材");
});

async function loadTasks() {
  try {
    const tasks = await api("/api/tasks?limit=200");
    const wrap = $("taskTableWrap");
    if (!tasks.length) {
      wrap.innerHTML = `<p class="muted">暂无任务</p>`;
      return;
    }
    wrap.innerHTML = `<table>
      <thead><tr><th>时间</th><th>模块</th><th>功能</th><th>状态</th><th>进度</th><th>结果/错误</th></tr></thead>
      <tbody>${tasks.map((task) => `
        <tr>
          <td>${new Date(task.created_at).toLocaleString()}</td>
          <td>${escapeHtml(task.module)}</td>
          <td>${escapeHtml(task.operation)}</td>
          <td>${statusCn(task.status)}</td>
          <td>${task.progress}%</td>
          <td>${task.error ? escapeHtml(task.error) : (task.output_files || []).map((u) => `<span class="task-result-actions"><a href="${escapeHtml(u)}" target="_blank">打开结果</a><button class="link-btn" data-media-action="save" data-url="${escapeHtml(u)}" data-name="${escapeHtml(u.split("/").pop())}">保存到素材库</button></span>`).join(" ")}</td>
        </tr>`).join("")}
      </tbody></table>`;
  } catch (error) {
    toast(error.message, true);
  }
}
$("refreshTasksButton").addEventListener("click", loadTasks);

async function loadSystem() {
  await Promise.all([loadGpuJson(), loadHealthJson()]);
}
async function loadGpuJson() {
  try { $("gpuJson").textContent = JSON.stringify(await api("/api/gpu/status"), null, 2); }
  catch (error) { $("gpuJson").textContent = error.message; }
}
async function loadHealthJson() {
  try { $("healthJson").textContent = JSON.stringify(await api("/api/health"), null, 2); }
  catch (error) { $("healthJson").textContent = error.message; }
}
$("systemGpuRefresh").addEventListener("click", loadGpuJson);
$("systemHealthRefresh").addEventListener("click", loadHealthJson);

function statusCn(status) {
  return {
    queued: "等待中",
    switching_gpu: "切换 GPU",
    running: "处理中",
    completed: "已完成",
    failed: "失败",
  }[status] || status;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function init() {
  await loadImageOptions();
  await loadImageModels();
  await loadCapabilities();
  await refreshGpu();
  await loadDashboard();
  try {
    const stored = JSON.parse(localStorage.getItem("aiStudioGemmaChat") || "[]");
    if (Array.isArray(stored)) gemmaChatMessages = stored.filter((item) => item && ["user", "assistant"].includes(item.role) && typeof item.content === "string").slice(-40);
  } catch (_) {}
  latestGemmaChatReply = [...gemmaChatMessages].reverse().find((item) => item.role === "assistant")?.content || "";
  renderGemmaChat();
  setGemmaPanel("chat");
  gpuPollTimer = setInterval(refreshGpu, 1500);
  const hash = location.hash.replace("#", "");
  if (PAGE_META[hash] && hash !== "dashboard") {
    await navigate(hash);
  }
}
init();


// V2.11 H3 VIDEO
window.__h3Available = false;

async function loadVideoCapabilities() {
  const badge = $("videoCapabilityBadge");
  const message = $("videoCapabilityMessage");
  if (!badge || !message) return;
  badge.textContent = "检查中";
  badge.className = "badge busy";
  try {
    const caps = await api("/api/video/capabilities");
    window.__h3Available = Boolean(caps.available);
    badge.textContent = caps.available ? "READY" : "不可用";
    badge.className = badgeClass(caps.available ? "READY" : "FAILED");
    message.textContent = caps.message || "";
    const ready = gpuState?.owner === "comfyui" && gpuState?.desired_owner === "comfyui" && gpuState?.phase === "READY";
    $("generateVideoButton").disabled = !(ready && window.__h3Available);
    $("videoSubmitHint").textContent = ready ? (window.__h3Available ? "可以提交 H3 视频任务" : "H3 能力不可用") : "GPU 工作区未就绪";
  } catch (error) {
    window.__h3Available = false;
    badge.textContent = "不可用";
    badge.className = badgeClass("FAILED");
    message.textContent = error.message;
    $("generateVideoButton").disabled = true;
  }
}

function renderH3Meta(meta = {}) {
  const el = $("videoResultMeta");
  if (!el) return;
  const v = meta.video || {};
  const a = meta.audio || {};
  const duration = meta.duration == null ? "--" : `${Number(meta.duration).toFixed(3)} 秒`;
  const size = meta.size == null ? "--" : `${(Number(meta.size) / 1024 / 1024).toFixed(2)} MB`;
  const elapsed = meta.elapsed_seconds == null ? "--" : `${Number(meta.elapsed_seconds).toFixed(1)} 秒`;
  const audioText = a.present ? `${a.codec || "--"} / ${a.sample_rate || "--"} Hz / ${a.channels || "--"} 声道` : "无音频轨";
  el.classList.remove("hidden");
  el.innerHTML = `<div><span>成片时长</span><strong>${duration}</strong></div><div><span>分辨率</span><strong>${v.width || meta.requested_width || "--"}×${v.height || meta.requested_height || "--"}</strong></div><div><span>视频编码</span><strong>${escapeHtml(v.codec || "--")}</strong></div><div><span>帧率</span><strong>${escapeHtml(v.fps || `${meta.fps || 24}/1`)}</strong></div><div><span>音频</span><strong>${escapeHtml(audioText)}</strong></div><div><span>文件大小</span><strong>${size}</strong></div><div><span>推理耗时</span><strong>${elapsed}</strong></div><div><span>Prompt ID</span><strong>${escapeHtml(meta.prompt_id || "--")}</strong></div>`;
}

function renderVideoTask(task) {
  if (!task) return;
  $("videoTaskStatus").textContent = statusCn(task.status);
  $("videoTaskStatus").className = task.status === "failed" ? badgeClass("FAILED") : task.status === "completed" ? badgeClass("READY") : "badge busy";
  $("videoProgressWrap").classList.remove("hidden");
  $("videoProgressFill").style.width = `${Math.max(0, Math.min(100, Number(task.progress || 0)))}%`;
  $("videoProgressText").textContent = task.error || `${task.message || ""} · ${task.progress || 0}%`;
  if (task.status === "failed") {
    $("videoResults").className = "h3-video-results";
    $("videoResults").textContent = task.error || "视频生成失败";
  }
  if (task.status === "completed" && task.output_files?.length) {
    const url = task.output_files[0];
    const results = $("videoResults");
    results.className = "h3-video-results";
    results.innerHTML = "";
    const video = document.createElement("video");
    video.controls = true; video.preload = "metadata"; video.src = url; video.className = "h3-result-video";
    results.appendChild(video);
    const link = document.createElement("a");
    link.href = url; link.target = "_blank"; link.rel = "noopener"; link.className = "btn secondary"; link.textContent = "打开成片";
    results.appendChild(link);
    renderH3Meta(task.params?.result_meta || {});
  }
}

async function pollVideoTask(taskId) {
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    try {
      const tasks = await api("/api/tasks?limit=100");
      const task = Array.isArray(tasks) ? tasks.find((item) => item.task_id === taskId) : null;
      if (!task) continue;
      renderVideoTask(task);
      if (task.status === "completed" || task.status === "failed") {
        const ready = gpuState?.owner === "comfyui" && gpuState?.desired_owner === "comfyui" && gpuState?.phase === "READY";
        $("generateVideoButton").disabled = !(ready && window.__h3Available);
        if (task.status === "completed") toast("H3 视频生成完成"); else toast(task.error || "H3 视频生成失败", true);
        return;
      }
    } catch (error) { toast(`读取视频任务失败：${error.message}`, true); }
  }
}

function updateVideoModeUI() {
  const mode = $("videoMode").value;
  const firstBox = $("videoFirstFrame")?.closest(".upload-box");
  const lastBox = $("videoLastFrame")?.closest(".upload-box");
  const refBox = $("videoReferenceBox");

  if (firstBox) firstBox.classList.toggle("hidden", mode !== "fl2va");
  if (lastBox) lastBox.classList.toggle("hidden", mode !== "fl2va");
  if (refBox) refBox.classList.toggle("hidden", mode !== "ref2va");
  if ($("videoRefImageSize")) $("videoRefImageSize").disabled = mode !== "ref2va";

  if (mode === "t2va") {
    $("videoSubmitHint").textContent = "T2VA 仅需要提示词";
  } else if (mode === "fl2va") {
    $("videoSubmitHint").textContent = "FL2VA 必须上传首帧，尾帧可选";
  } else {
    $("videoSubmitHint").textContent = "REF2VA 必须上传参考图；提示词可使用 <Picture 1>";
  }
}

if ($("videoMode")) {
  $("videoMode").addEventListener("change", updateVideoModeUI);
}

if ($("videoFirstFrame")) {
  $("videoFirstFrame").addEventListener("change", () => {
    $("videoFirstName").textContent =
      $("videoFirstFrame").files?.[0]?.name || "必须上传";
  });
}

if ($("videoLastFrame")) {
  $("videoLastFrame").addEventListener("change", () => {
    $("videoLastName").textContent =
      $("videoLastFrame").files?.[0]?.name || "可选；不上传则仅使用首帧";
  });
}

if ($("videoReferenceImage")) {
  $("videoReferenceImage").addEventListener("change", () => {
    $("videoReferenceName").textContent =
      $("videoReferenceImage").files?.[0]?.name ||
      "REF2VA 模式必须上传；提示词可使用 <Picture 1> 引用";
  });
}

if ($("generateVideoButton")) {
  $("generateVideoButton").addEventListener("click", async () => {
    const mode = $("videoMode").value;
    const prompt = $("videoPrompt").value.trim();
    const first = $("videoFirstFrame").files?.[0];
    const last = $("videoLastFrame").files?.[0];
    const reference = $("videoReferenceImage").files?.[0];

    if (!prompt) return toast("视频提示词不能为空", true);
    if (mode === "fl2va" && !first) return toast("FL2VA 请选择首帧图片", true);
    if (mode === "ref2va" && !reference) return toast("REF2VA 请选择参考图片", true);

    const width = Number($("videoWidth").value);
    const height = Number($("videoHeight").value);
    const length = Number($("videoLength").value);
    const steps = Number($("videoSteps").value);

    if (width < 256 || width > 1344 || width % 32 !== 0) {
      return toast("宽度必须为 256～1344 且是 32 的整数倍", true);
    }
    if (height < 256 || height > 1344 || height % 32 !== 0) {
      return toast("高度必须为 256～1344 且是 32 的整数倍", true);
    }
    if (length < 5 || length > 3600 || (length - 5) % 17 !== 0) {
      return toast("H3 帧数必须满足 5 + 17×N，范围 5～3600", true);
    }
    if (steps < 1 || steps > 100) {
      return toast("生成步数必须为 1～100", true);
    }

    const form = new FormData();
    form.append("mode", mode);
    form.append("prompt", prompt);
    form.append("width", String(width));
    form.append("height", String(height));
    form.append("length", String(length));
    form.append("steps", String(steps));
    form.append("seed", $("videoSeed").value);

    if (mode === "fl2va") {
      form.append("first_frame", first);
      if (last) form.append("last_frame", last);
    } else if (mode === "ref2va") {
      form.append("reference_image", reference);
      form.append("ref_image_size", $("videoRefImageSize").value);
    }

    $("generateVideoButton").disabled = true;
    $("videoResultMeta").classList.add("hidden");

    try {
      const task = await api("/api/video/tasks", {method: "POST", body: form});
      renderVideoTask(task);
      pollVideoTask(task.task_id);
    } catch (error) {
      $("generateVideoButton").disabled = false;
      toast(error.message, true);
    }
  });
}

updateVideoModeUI();


// V2.12 LLM REGISTRY
window.__llmRegistry = null;

async function loadLLMModels() {
  const select = $("llmModelSelect");
  const button = $("llmModelSwitchButton");
  const hint = $("llmModelHint");
  const message = $("llmModelMessage");
  if (!select || !button || !hint || !message) return;

  try {
    const data = await api("/api/llm/models");
    window.__llmRegistry = data;
    const models = Array.isArray(data.models) ? data.models : [];
    select.innerHTML = models.map((item) => {
      const suffix = item.installed ? "" : " · 未安装";
      const active = item.active ? " · 当前运行" : "";
      return `<option value="${escapeHtml(item.id)}" ${item.selected ? "selected" : ""} ${item.installed ? "" : "disabled"}>${escapeHtml(item.label || item.id)}${suffix}${active}</option>`;
    }).join("");

    const selected = models.find((item) => item.selected);
    const active = models.find((item) => item.active);
    button.disabled = !selected || !selected.installed;
    hint.textContent = active
      ? `当前运行：${active.label || active.id}`
      : "当前 LLM 未占用 GPU；选择会保存到下次进入 LLM 工作区";
    message.textContent = selected
      ? `已选择：${selected.label || selected.id}；Qwen3 / Gemma 共用 6006，同一时刻只加载一个模型`
      : "没有可用的 LLM 模型";
    message.className = `message ${selected?.installed ? "success" : "error"}`;
  } catch (error) {
    button.disabled = true;
    hint.textContent = "模型注册表读取失败";
    message.textContent = error.message;
    message.className = "message error";
  }
}

if ($("llmModelSwitchButton")) {
  $("llmModelSwitchButton").addEventListener("click", async () => {
    const select = $("llmModelSelect");
    const button = $("llmModelSwitchButton");
    const hint = $("llmModelHint");
    const modelId = select?.value;
    if (!modelId) return toast("请选择 LLM 模型", true);

    button.disabled = true;
    hint.textContent = "正在应用模型选择...";

    try {
      const result = await api(
        `/api/llm/select/${encodeURIComponent(modelId)}`,
        {method: "POST"}
      );
      toast(result.message || "LLM 模型选择已更新");
      await refreshGpu();
      await loadLLMModels();
    } catch (error) {
      toast(error.message, true);
      await loadLLMModels();
    }
  });
}

loadLLMModels();
