import asyncio
import json
import logging
from contextlib import nullcontext
from pathlib import Path
from typing import TypedDict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.core.gpu_orchestrator import GPUOrchestrator
from app.core.task_store import TaskStore
from app.models import (
    ChatMessage, ChatRequest, ChatResponse, GPUOwner, GPUState,
    PromptRequest, PromptResponse, TaskRecord,
)
from app.services.assets import AssetService, safe_name
from app.services.comfyui import (
    ASPECT_RATIO_PRESETS,
    LEGACY_PRESET_MAP,
    STYLE_PRESETS,
    STYLE_STRENGTHS,
    ComfyUIService,
    build_styled_prompts,
    public_image_options,
)
from app.services.facefusion import FaceFusionService
from app.services.gemma import GemmaService
from app.services.h3_video import H3VideoService
from app.services.llm_registry import LLMRegistryService
from app.services.director import DirectorMessageRequest, DirectorProjectCreate, DirectorService
from app.services.task_runner import TaskRunner


settings = get_settings()
assets = AssetService(settings.data_dir, settings.max_upload_mb)
store = TaskStore(settings.data_dir)
gpu = GPUOrchestrator(settings)
llm_registry = LLMRegistryService(settings)
gemma = GemmaService(settings)
director = DirectorService(settings, gemma)
comfyui = ComfyUIService(settings)
h3_video = H3VideoService(settings)
facefusion = FaceFusionService(settings)
runner = TaskRunner(store, assets, gpu, comfyui, h3_video, facefusion)
logger = logging.getLogger("ai-studio")
_llm_activation_lock = asyncio.Lock()


class StageProgress(TypedDict):
    stage_id: str
    stage_name: str
    status: str
    current_step: int
    total_steps: int
    percent: int
    completed_items: list[str]
    current_item: str
    eta_seconds: int | None
    source: str


async def _llm_status_payload() -> dict:
    legacy = await gemma.status()
    selected = llm_registry.selected_model()
    state = await gpu.snapshot()
    return {
        "ready": bool(legacy.get("ready")),
        "selected_model": selected,
        "active_alias": legacy.get("resolved_model") or "",
        "active_models": legacy.get("models") or [],
        "matches_selection": bool(
            legacy.get("ready")
            and legacy.get("resolved_model") == selected.get("alias")
        ),
        "gpu": state.model_dump(mode="json"),
        "legacy_gemma_status": legacy,
    }


async def _ensure_selected_llm_loaded() -> dict:
    async with _llm_activation_lock:
        state = await gpu.snapshot()
        if state.owner != GPUOwner.gemma:
            return await _llm_status_payload()

        phase = getattr(state.phase, "value", state.phase)
        if str(phase) != "READY":
            raise RuntimeError(f"LLM GPU 工作区当前状态不是 READY：{phase}")

        active_tasks = state.active_tasks or {}
        busy = (
            active_tasks.get(GPUOwner.gemma, 0)
            or active_tasks.get("gemma", 0)
        )
        if busy:
            raise RuntimeError("LLM 当前有活动任务，不能切换模型")

        status = await _llm_status_payload()
        if status["matches_selection"]:
            return status

        await gpu.reload_owner(GPUOwner.gemma)

        status = await _llm_status_payload()
        if not status["matches_selection"]:
            raise RuntimeError(
                "LLM 重载完成但活动模型与选择不一致："
                f"selected={status['selected_model'].get('alias')} "
                f"active={status.get('active_alias')}"
            )
        return status

app = FastAPI(title="AI Studio Platform V2", version="2.39.6.3-stage04-full-pipeline-preflight")
app.mount("/files", StaticFiles(directory=settings.data_dir), name="files")
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)


async def _activate_default_workspace() -> None:
    try:
        owner = GPUOwner(str(settings.gpu_default_owner).strip().lower())
    except ValueError:
        owner = GPUOwner.gemma
    if owner == GPUOwner.none:
        owner = GPUOwner.gemma
    try:
        await gpu.ensure_ready(owner)
        if owner == GPUOwner.gemma:
            await _ensure_selected_llm_loaded()
    except Exception:
        logger.exception("默认 GPU 工作区启动失败：%s", owner.value)


@app.on_event("startup")
async def platform_startup() -> None:
    asyncio.create_task(_activate_default_workspace())


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict:
    model_status = await comfyui.public_models()
    gemma_status = await gemma.status()
    return {
        "platform": True,
        "version": '2.39.6.3-stage04-full-pipeline-preflight',
        "llm": await _llm_status_payload(),
        "director": director.source_status(),
        "gemma": bool(gemma_status.get("ready")),
        "gemma_detail": gemma_status,
        "assistant": await gemma.capabilities(),
        "gpu": (await gpu.snapshot()).model_dump(mode="json"),
        "image": {
            "force_4k": False,
            "mandatory_ai_upscale": False,
            "all_ratio_quality_profile": True,
            "model_pool": True,
            "z_image_turbo": True,
            "workflow_by_model": True,
            "work_resolution": True,
            "model_total": model_status["total_count"],
            "model_installed": model_status["installed_count"],
            "dynamic_checkpoint": True,
            "optional_openpose": True,
            "appearance_lora": True,
            "appearance_lora_task_level": True,
            "semantic_compiler": True,
            "business_keyword_hardcoding": False,
            "raw_prompt_fallback": True,
            "compiler_schema_version": "1.0",
            "compiler_cache": True,
            "tri_state_gpu": True,
            "synchronous_workspace_handoff": True,
            "face_detailer": True,
            "portrait_visibility_gate": False,
            "semantic_auto_retry": False,
            "aspect_ratio_count": len(ASPECT_RATIO_PRESETS),
            "style_count": len(STYLE_PRESETS),
        },
        "paths": {
            "data": str(settings.data_dir),
            "facefusion": str(settings.facefusion_dir),
            "workflow": str(settings.comfyui_workflow_path),
            "image_models": str(comfyui.model_registry.config_path),
        },
    }


@app.get("/api/director/status")
async def director_status() -> dict:
    return director.source_status()


@app.get("/api/director/projects")
async def director_projects() -> list[dict]:
    return director.list_projects()


@app.post("/api/director/projects")
async def director_create(request: DirectorProjectCreate) -> dict:
    try:
        return director.create_project(request.title)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/director/projects/{project_id}")
async def director_project(project_id: str) -> dict:
    try:
        return director.get_project(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/message")
async def director_message(
    project_id: str,
    request: DirectorMessageRequest,
) -> dict:
    try:
        async with gpu.use(GPUOwner.gemma):
            return await director.message(project_id, request.text)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/confirm-stage")
async def director_confirm_stage(project_id: str) -> dict:
    try:
        if "_sync_project_production_tasks" in globals():
            _sync_project_production_tasks(project_id)
        return await director.confirm_stage(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/llm/models")
async def llm_models() -> dict:
    legacy = await gemma.status()
    return llm_registry.list_models(
        active_alias=str(legacy.get("resolved_model") or "")
    )


@app.get("/api/llm/status")
async def llm_status() -> dict:
    return await _llm_status_payload()


@app.post("/api/llm/select/{model_id}")
async def llm_select(model_id: str) -> dict:
    previous = llm_registry.selected_model()
    state = await gpu.snapshot()
    phase = str(getattr(state.phase, "value", state.phase))

    if state.owner == GPUOwner.gemma and phase != "READY":
        raise HTTPException(
            status_code=409,
            detail=f"LLM 工作区正在切换，当前状态：{phase}",
        )

    active_tasks = state.active_tasks or {}
    busy = (
        active_tasks.get(GPUOwner.gemma, 0)
        or active_tasks.get("gemma", 0)
    )
    if state.owner == GPUOwner.gemma and busy:
        raise HTTPException(
            status_code=409,
            detail="LLM 当前有活动任务，请等待任务结束后再切换模型",
        )

    try:
        selected = await llm_registry.select(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    reload_now = state.owner == GPUOwner.gemma
    if not reload_now:
        return {
            **(await _llm_status_payload()),
            "selection_changed": previous.get("id") != selected.get("id"),
            "reloaded": False,
            "message": "LLM 模型选择已保存；进入 LLM 工作区时加载",
        }

    try:
        status = await _ensure_selected_llm_loaded()
        return {
            **status,
            "selection_changed": previous.get("id") != selected.get("id"),
            "reloaded": True,
            "message": "LLM 模型已切换并进入 READY",
        }
    except Exception as exc:
        rollback_error = ""
        try:
            await llm_registry.select(str(previous.get("id")))
            await gpu.reload_owner(GPUOwner.gemma)
        except Exception as rollback_exc:
            rollback_error = f"；回滚也失败：{rollback_exc}"
        raise HTTPException(
            status_code=500,
            detail=f"LLM 模型切换失败，已尝试回滚：{exc}{rollback_error}",
        ) from exc


@app.post("/api/llm/chat", response_model=ChatResponse)
async def llm_chat(request: ChatRequest) -> ChatResponse:
    try:
        async with gpu.use(GPUOwner.gemma):
            messages, system_prompt, budget = await _context_prepare_chat(
                [item.model_dump() for item in request.messages],
                request.system_prompt,
                requested_output_tokens=request.max_tokens,
            )
            result = await gemma.chat(
                messages=messages,
                system_prompt=system_prompt,
                temperature=request.temperature,
                max_tokens=budget["output_tokens"],
            )
        return ChatResponse(**result)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/gemma/status")
async def gemma_status() -> dict:
    return await gemma.status()


@app.get("/api/gemma/capabilities")
async def gemma_capabilities() -> dict:
    return await gemma.capabilities()


@app.get("/api/gpu/status", response_model=GPUState)
async def gpu_status() -> GPUState:
    return await gpu.snapshot()


@app.post("/api/gpu/activate/{owner}", response_model=GPUState)
async def gpu_activate(owner: GPUOwner) -> GPUState:
    if owner not in {GPUOwner.gemma, GPUOwner.comfyui, GPUOwner.facefusion}:
        raise HTTPException(status_code=400, detail="不支持的 GPU 工作区")
    try:
        return await gpu.request(owner)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/gpu/transition/{owner}", response_model=GPUState)
async def gpu_transition(owner: GPUOwner) -> GPUState:
    if owner not in {GPUOwner.gemma, GPUOwner.comfyui, GPUOwner.facefusion}:
        raise HTTPException(status_code=400, detail="不支持的 GPU 工作区")
    try:
        return await gpu.transition(owner)
    except Exception as exc:
        state = await gpu.snapshot()
        detail = state.error or str(exc)
        raise HTTPException(status_code=500, detail=detail) from exc

# ===== V2.32 GLOBAL CONTEXT BUDGET =====
async def _context_reduce_text(
    text: str,
    *,
    label: str,
    target_chars: int = 4200,
) -> str:
    """Hierarchically compact long free-text before a model call."""
    raw = str(text or "")
    if len(raw) <= target_chars:
        return raw

    summarizer_system = """你是上下文压缩器，不执行用户任务。
把 INPUT_PART 压缩成可供后续模型继续工作的事实/要求摘要。
必须保留明确事实、名称、数字、时间、否定条件、约束、用户要求和未解决问题。
不要回答任务，不补造事实，不使用业务关键词表。
只输出压缩后的事实文本。"""

    def chunks(value: str, size: int = 4800) -> list[str]:
        out = []
        start = 0
        while start < len(value):
            hard = min(len(value), start + size)
            end = hard
            if hard < len(value):
                floor = start + size // 2
                cut = max(
                    value.rfind("\n", floor, hard),
                    value.rfind("。", floor, hard),
                    value.rfind("！", floor, hard),
                    value.rfind("？", floor, hard),
                )
                if cut >= floor:
                    end = cut + 1
            out.append(value[start:end])
            start = end
        return out

    level = [
        f"[{label} part {i + 1}/{len(chunks(raw))}]\n{part}"
        for i, part in enumerate(chunks(raw))
    ]
    while True:
        summaries: list[str] = []
        for i, part in enumerate(level):
            result = await gemma.chat(
                messages=[{
                    "role": "user",
                    "content": (
                        f"LABEL={label}\n"
                        f"PART={i + 1}/{len(level)}\n"
                        "=== INPUT_PART ===\n"
                        + part
                    ),
                }],
                system_prompt=summarizer_system,
                temperature=0.0,
                max_tokens=420,
            )
            summary = str(result.get("content") or "").strip()
            if summary:
                summaries.append(summary[:1800])
        merged = "\n\n".join(summaries)
        if len(merged) <= target_chars or len(summaries) <= 1:
            return merged[:target_chars]
        # Reduce summaries again in bounded groups.
        grouped: list[str] = []
        current = ""
        for item in summaries:
            if current and len(current) + len(item) + 2 > 4800:
                grouped.append(current)
                current = item
            else:
                current = item if not current else current + "\n\n" + item
        if current:
            grouped.append(current)
        level = grouped


async def _context_prepare_chat(
    messages: list[dict],
    system_prompt: str,
    *,
    requested_output_tokens: int,
    safety_tokens: int = 192,
) -> tuple[list[dict], str, dict]:
    """Return a model-safe chat packet using the live Director token budget."""
    system = str(system_prompt or "")
    if len(system) > 3200:
        system = await _context_reduce_text(
            system, label="SYSTEM_PROMPT", target_chars=2600
        )

    packed: list[dict] = []
    for index, item in enumerate(messages):
        role = str(item.get("role") or "user")
        content = str(item.get("content") or "")
        if len(content) > 5200:
            content = await _context_reduce_text(
                content,
                label=f"{role.upper()}_{index + 1}",
                target_chars=3600,
            )
        packed.append({"role": role, "content": content})

    try:
        budget = await director._llm_call_budget(
            phase="global_chat_context_budget",
            system_prompt=system,
            messages=packed,
            requested_output_tokens=requested_output_tokens,
            minimum_output_tokens=min(
                160, max(64, int(requested_output_tokens))
            ),
            safety_tokens=safety_tokens,
        )
        return packed, system, budget
    except RuntimeError:
        pass

    # The individual messages fit, but accumulated history does not. Preserve
    # the newest user request separately and hierarchically reduce older turns.
    latest_user = ""
    latest_index = -1
    for index in range(len(packed) - 1, -1, -1):
        if packed[index].get("role") == "user":
            latest_user = str(packed[index].get("content") or "")
            latest_index = index
            break
    history = "\n\n".join(
        f"{x.get('role','user')}: {x.get('content','')}"
        for i, x in enumerate(packed)
        if i != latest_index
    )
    history_summary = (
        await _context_reduce_text(
            history, label="CONVERSATION_HISTORY", target_chars=3000
        )
        if history else ""
    )
    if len(latest_user) > 3200:
        latest_user = await _context_reduce_text(
            latest_user,
            label="LATEST_USER_REQUEST",
            target_chars=3000,
        )
    final_content = (
        "=== BOUNDED_HISTORY_FACTS ===\n"
        + (history_summary or "<none>")
        + "\n\n=== CURRENT_USER_REQUEST ===\n"
        + latest_user
    )
    final_messages = [{"role": "user", "content": final_content}]
    system = (
        system
        + "\n\n[上下文预算说明] 较长历史已压缩为事实摘要；"
        "只能依据摘要和当前请求回答，不得假装看到被压缩掉的原文。"
    )
    budget = await director._llm_call_budget(
        phase="global_chat_context_budget_compacted",
        system_prompt=system,
        messages=final_messages,
        requested_output_tokens=requested_output_tokens,
        minimum_output_tokens=min(
            128, max(64, int(requested_output_tokens))
        ),
        safety_tokens=safety_tokens,
    )
    return final_messages, system, budget
# ===== /V2.32 GLOBAL CONTEXT BUDGET =====

@app.post("/api/gemma", response_model=PromptResponse)
async def run_gemma(request: PromptRequest) -> PromptResponse:
    try:
        async with gpu.use(GPUOwner.gemma):
            text = str(request.text or "")
            if len(text) > 6000:
                text = await _context_reduce_text(
                    text,
                    label="PROMPT_OPTIMIZATION_INPUT",
                    target_chars=4200,
                )
            result = await gemma.run(
                text, request.mode, request.width, request.height
            )
        return PromptResponse(**result)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/gemma/chat", response_model=ChatResponse)
async def gemma_chat(request: ChatRequest) -> ChatResponse:
    try:
        async with gpu.use(GPUOwner.gemma):
            messages, system_prompt, budget = await _context_prepare_chat(
                [item.model_dump() for item in request.messages],
                request.system_prompt,
                requested_output_tokens=request.max_tokens,
            )
            result = await gemma.chat(
                messages=messages,
                system_prompt=system_prompt,
                temperature=request.temperature,
                max_tokens=budget["output_tokens"],
            )
        return ChatResponse(**result)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/gemma/chat/multimodal", response_model=ChatResponse)
async def gemma_multimodal_chat(
    messages_json: str = Form(...),
    system_prompt: str = Form(default=""),
    temperature: float = Form(default=0.5),
    max_tokens: int = Form(default=2048),
    image: UploadFile = File(...),
) -> ChatResponse:
    capabilities = await gemma.capabilities()
    if not capabilities.get("multimodal"):
        raise HTTPException(
            status_code=409,
            detail=capabilities.get("multimodal_message"),
        )
    try:
        raw = json.loads(messages_json)
        if not isinstance(raw, list):
            raise ValueError("messages 必须是数组")
        messages = [
            ChatMessage.model_validate(item).model_dump()
            for item in raw
        ]
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"对话消息格式错误：{exc}"
        ) from exc
    content_type = image.content_type or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="多模态输入必须是图片")
    limit = max(1, settings.gemma_multimodal_max_mb) * 1024 * 1024
    image_bytes = await image.read(limit + 1)
    if len(image_bytes) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"图片不能超过 {settings.gemma_multimodal_max_mb} MB",
        )
    try:
        async with gpu.use(GPUOwner.gemma):
            packed, packed_system, budget = await _context_prepare_chat(
                messages,
                system_prompt,
                requested_output_tokens=max_tokens,
                # Reserve additional context for multimodal image tokens.
                safety_tokens=1600,
            )
            result = await gemma.multimodal_chat(
                messages=packed,
                image_bytes=image_bytes,
                image_mime=content_type,
                system_prompt=packed_system,
                temperature=temperature,
                max_tokens=budget["output_tokens"],
            )
        return ChatResponse(**result)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/image/presets")
async def image_presets() -> dict:
    return public_image_options()


@app.get("/api/image/models")
async def image_models() -> dict:
    status = await comfyui.public_models()
    rows = status.get("models")
    if not isinstance(rows, list):
        rows = status.get("items") if isinstance(status.get("items"), list) else []
    z = {
        "key": "z_image_turbo",
        "id": "z_image_turbo",
        "name": "Z-Image-Turbo",
        "label": "Z-Image-Turbo",
        "description": "Z-Image-Turbo · 官方 ComfyUI 工作流 · 默认",
        "installed": True,
        "available": True,
        "selected": True,
        "default": True,
        "checkpoint": "z_image_turbo_bf16.safetensors",
        "resolved_checkpoint": "z_image_turbo_bf16.safetensors",
        "prompt_adapter": "generic",
        "face_detailer": False,
        "face_detailer_denoise": 0.0,
        "upscaler": "",
    }
    clean = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("id") or "")
        if key == "z_image_turbo":
            continue
        item = dict(item)
        item["selected"] = False
        item["default"] = False
        clean.append(item)
    status["models"] = [z] + clean
    status["default_model_key"] = "z_image_turbo"
    status["selected_model_key"] = "z_image_turbo"
    status["total_count"] = len(status["models"])
    status["installed_count"] = sum(1 for x in status["models"] if x.get("installed") is not False)
    return status


@app.post("/api/image/tasks", response_model=TaskRecord)
async def image_task(
    positive_prompt: str = Form(...),
    negative_prompt: str = Form(default=""),
    model_key: str = Form(default="z_image_turbo"),
    pose_control: str = Form(default="auto"),
    appearance_enhance_mode: str = Form(default="auto"),
    appearance_lora_strength: float = Form(default=0.30),
    aspect_ratio: str = Form(default="16:9"),
    style_name: str = Form(default="portrait_photo"),
    style_strength: str = Form(default="standard"),
    output_preset: str = Form(default=""),
    steps: int = Form(default=32),
    cfg: float = Form(default=6.5),
    seed: int = Form(default=-1),
    sampler: str = Form(default="dpmpp_2m"),
    scheduler: str = Form(default="karras"),
    count: int = Form(default=1),
    semantic_compile: str = Form(default="auto"),
) -> TaskRecord:
    user_positive = positive_prompt.strip()
    user_negative = negative_prompt.strip()
    if not user_positive:
        raise HTTPException(status_code=400, detail="正向提示词不能为空")

    semantic_mode = str(semantic_compile or "auto").strip().lower()
    if semantic_mode not in {"auto", "locked"}:
        raise HTTPException(
            status_code=400,
            detail="semantic_compile 只能是 auto 或 locked",
        )
    if semantic_mode == "locked":
        pose_control = "off"
        appearance_enhance_mode = "off"

    if output_preset and aspect_ratio == "16:9":
        aspect_ratio = LEGACY_PRESET_MAP.get(output_preset, aspect_ratio)

    preset = ASPECT_RATIO_PRESETS.get(aspect_ratio)
    style = STYLE_PRESETS.get(style_name)
    strength = STYLE_STRENGTHS.get(style_strength)
    if preset is None:
        raise HTTPException(status_code=400, detail="不支持的画面比例")
    if style is None:
        raise HTTPException(status_code=400, detail="不支持的图片风格")
    if strength is None:
        raise HTTPException(status_code=400, detail="不支持的风格强度")
    if pose_control not in {"auto", "off", "light", "standard"}:
        raise HTTPException(status_code=400, detail="不支持的姿态控制模式")

    profile_keys = comfyui.appearance_profile_keys()
    if appearance_enhance_mode not in {"auto", "off"} | profile_keys:
        raise HTTPException(status_code=400, detail="不支持的人物外貌增强模式")
    if not 0.0 <= appearance_lora_strength <= 1.0:
        raise HTTPException(status_code=400, detail="人物外貌增强强度必须为 0～1")
    if not 1 <= count <= 4:
        raise HTTPException(status_code=400, detail="单次生成数量为 1～4")
    if not 1 <= steps <= 100:
        raise HTTPException(status_code=400, detail="生成步数必须为 1～100")
    if not 1.0 <= cfg <= 30.0:
        raise HTTPException(status_code=400, detail="CFG 必须为 1～30")

    required_model_keys: set[str] | None = None
    if appearance_enhance_mode not in {"auto", "off"}:
        profile = comfyui.model_registry.get_appearance_profile(appearance_enhance_mode)
        if profile is None:
            raise HTTPException(status_code=400, detail="人物外貌增强配置不存在")
        required_model_keys = set(profile.get("supported_models", []))

    try:
        model = await comfyui.resolve_model(
            model_key,
            style_name,
            required_model_keys=required_model_keys,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    compatible_profiles = await comfyui.compatible_appearance_profiles(model["key"])
    compiler_profiles = [
        {
            "key": item["key"],
            "label": item["label"],
            "description": item["description"],
            "supported_models": item["supported_models"],
        }
        for item in compatible_profiles
    ]
    compiler_kwargs = {
        "positive_prompt": user_positive,
        "negative_prompt": user_negative,
        "model": model,
        "aspect": preset,
        "style": style,
        "style_strength": strength,
        "requested_pose_control": pose_control,
        "requested_appearance_mode": appearance_enhance_mode,
        "appearance_profiles": compiler_profiles,
    }
    compiler_error = ""
    if semantic_mode == "locked":
        compiler_status = "locked_passthrough"
        compilation = {
            "schema_version": "stage04-locked-v1",
            "semantic": {
                "subject": {"raw": user_positive, "is_human": False, "count": None},
                "composition": {"specified": False, "raw": "", "framing": "unspecified"},
                "clothing": {"specified": False, "raw": ""},
                "pose": {"specified": False, "raw": ""},
                "environment": {"specified": False, "raw": ""},
                "lighting": {"specified": False, "raw": ""},
                "camera": {"specified": False, "raw": ""},
                "style": {"specified": False, "raw": ""},
                "must_preserve": [user_positive],
            },
            "compiled": {
                "positive_prompt": user_positive,
                "negative_prompt": user_negative,
            },
            "controls": {
                "pose_control": "off",
                "pose_template": "off",
                "appearance_profile": "off",
            },
            "notes": (
                "正式制作锁定模式：直接消费已确认 Prompt；"
                "图片接口未再次调用文本模型改写剧情语义。"
            ),
        }
    else:
        compiler_status = "llm_auto"
        try:
            compilation = await gemma.get_cached_image_compilation(**compiler_kwargs)
            if compilation is not None:
                compiler_status = "cache"
            else:
                async with gpu.use(GPUOwner.gemma):
                    compilation = await gemma.compile_image_request(**compiler_kwargs)
        except Exception as exc:
            compiler_status = "raw_fallback"
            compiler_error = str(exc)
            final_positive, final_negative = build_styled_prompts(
                positive_prompt=user_positive,
                negative_prompt=user_negative,
                aspect_ratio=aspect_ratio,
                style_name=style_name,
                style_strength=style_strength,
                model_key=model["key"],
            )
            compilation = {
                "schema_version": "1.0",
                "semantic": {
                    "subject": {"raw": user_positive, "is_human": False, "count": None},
                    "composition": {"specified": False, "raw": "", "framing": "unspecified"},
                    "clothing": {"specified": False, "raw": ""},
                    "pose": {"specified": False, "raw": ""},
                    "environment": {"specified": False, "raw": ""},
                    "lighting": {"specified": False, "raw": ""},
                    "camera": {"specified": False, "raw": ""},
                    "style": {"specified": False, "raw": ""},
                    "must_preserve": [user_positive],
                },
                "compiled": {
                    "positive_prompt": final_positive,
                    "negative_prompt": final_negative,
                },
                "controls": {
                    "pose_control": "off",
                    "pose_template": "off",
                    "appearance_profile": "off",
                },
                "notes": "LLM 图片语义编译不可用，已执行原提示词回退。",
            }

    compiled = compilation["compiled"]
    controls = compilation["controls"]
    semantic = compilation["semantic"]
    final_positive = str(compiled["positive_prompt"]).strip()
    final_negative = str(compiled.get("negative_prompt", "")).strip()
    if user_negative and user_negative.lower() not in final_negative.lower():
        final_negative = ", ".join(part for part in (user_negative, final_negative) if part)

    resolved_pose_control = (
        str(controls.get("pose_control", "off"))
        if pose_control == "auto"
        else pose_control
    )
    pose_template = str(controls.get("pose_template", "off"))
    if resolved_pose_control == "off":
        pose_template = "off"
    elif pose_template == "off":
        framing = str(semantic.get("composition", {}).get("framing", "unspecified"))
        pose_template = "neutral_full_body" if framing == "full_body" else "neutral_upper_body"

    try:
        appearance = await comfyui.resolve_appearance_enhancement(
            model_key=model["key"],
            requested_mode=appearance_enhance_mode,
            compiler_profile=str(controls.get("appearance_profile", "off")),
            strength=appearance_lora_strength,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    subject_is_human = bool(semantic.get("subject", {}).get("is_human", False))
    if appearance["enabled"] or resolved_pose_control != "off":
        subject_is_human = True

    return runner.submit_image(
        params={
            "user_positive_prompt": user_positive,
            "user_negative_prompt": user_negative,
            "positive_prompt": final_positive,
            "negative_prompt": final_negative,
            "prompt_compiler_status": compiler_status,
            "semantic_compile": semantic_mode,
            "prompt_compiler_error": compiler_error,
            "prompt_compiler_schema": compilation.get("schema_version", "1.0"),
            "prompt_semantic": semantic,
            "prompt_compiler_notes": compilation.get("notes", ""),
            "prompt_compiler_cache": compilation.get("cache", {}),
            "requested_model_key": model_key,
            "model_key": model["key"],
            "model_label": model["label"],
            "model_name": model["name"],
            "checkpoint": model["resolved_checkpoint"],
            "prompt_adapter": model["prompt_adapter"],
            "face_detailer": model["face_detailer"],
            "face_detailer_denoise": model["face_detailer_denoise"],
            "upscaler": model["upscaler"],
            "smart_fallback": model.get("smart_fallback", False),
            "subject_is_human": subject_is_human,
            "requested_pose_control": pose_control,
            "pose_control": resolved_pose_control,
            "pose_template": pose_template,
            "appearance_enhance_mode": appearance["requested_mode"],
            "appearance_resolved_mode": appearance["resolved_mode"],
            "appearance_enabled": appearance["enabled"],
            "appearance_label": appearance["label"],
            "appearance_lora_name": appearance["lora_name"],
            "appearance_lora_trigger": appearance["trigger"],
            "appearance_lora_trigger_weight": appearance["trigger_weight"],
            "appearance_lora_strength_model": appearance["strength_model"],
            "appearance_lora_strength_clip": appearance["strength_clip"],
            "aspect_ratio": aspect_ratio,
            "aspect_label": str(preset["label"]),
            "style_name": style_name,
            "style_label": str(style["label"]),
            "style_strength": style_strength,
            "style_strength_label": str(strength["label"]),
            "base_width": int(preset["base_width"]),
            "base_height": int(preset["base_height"]),
            "output_width": int(preset["output_width"]),
            "output_height": int(preset["output_height"]),
            "steps": steps,
            "cfg": cfg,
            "seed": seed,
            "sampler": sampler,
            "scheduler": scheduler,
            "count": count,
        }
    )


@app.get("/api/video/capabilities")
async def video_capabilities() -> dict:
    return await h3_video.capabilities()


@app.post("/api/video/tasks", response_model=TaskRecord)
async def video_task(
    mode: str = Form(default="fl2va"),
    video_profile: str = Form(default="standard"),
    prompt: str = Form(...),
    width: int = Form(default=768),
    height: int = Form(default=448),
    length: int = Form(default=124),
    steps: int = Form(default=20),
    seed: int = Form(default=-1),
    ref_image_size: str = Form(default="match"),
    first_frame: UploadFile | None = File(default=None),
    last_frame: UploadFile | None = File(default=None),
    reference_image: UploadFile | None = File(default=None),
) -> TaskRecord:
    mode = mode.strip().lower()
    video_profile = video_profile.strip().lower()
    if mode not in {"t2va", "fl2va", "ref2va"}:
        raise HTTPException(status_code=400, detail="视频模式必须是 t2va、fl2va 或 ref2va")
    if video_profile not in {"standard", "turbo"}:
        raise HTTPException(status_code=400, detail="video_profile 只能是 standard 或 turbo")
    prompt = prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="视频提示词不能为空")
    if width < 256 or width > 1344 or width % 32 != 0:
        raise HTTPException(status_code=400, detail="视频宽度必须为 256～1344 且是 32 的整数倍")
    if height < 256 or height > 1344 or height % 32 != 0:
        raise HTTPException(status_code=400, detail="视频高度必须为 256～1344 且是 32 的整数倍")
    if length < 5 or length > 3600 or (length - 5) % 17 != 0:
        raise HTTPException(status_code=400, detail="H3 帧数必须满足 5 + 17×N，范围 5～3600")
    if video_profile == "turbo":
        steps = 4
    if not 1 <= steps <= 100:
        raise HTTPException(status_code=400, detail="生成步数必须为 1～100")
    if ref_image_size not in {"match", "max"}:
        raise HTTPException(status_code=400, detail="ref_image_size 只能是 match 或 max")

    pending = settings.data_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    import uuid
    token = uuid.uuid4().hex

    async def save_image(upload: UploadFile | None, role: str) -> Path | None:
        if upload is None or not upload.filename:
            return None
        content_type = upload.content_type or ""
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"{role}必须为图片")
        path = pending / f"{token}_{role}_{safe_name(upload.filename)}"
        await assets.save_upload(upload, path)
        return path

    first_path = await save_image(first_frame, "h3_first")
    last_path = await save_image(last_frame, "h3_last")
    reference_path = await save_image(reference_image, "h3_reference")

    if mode == "fl2va" and first_path is None:
        raise HTTPException(status_code=400, detail="FL2VA 必须上传首帧图片")
    if mode == "ref2va" and reference_path is None:
        raise HTTPException(status_code=400, detail="REF2VA 必须上传参考图片")

    return runner.submit_video(
        params={
            "mode": mode,
            "video_profile": video_profile,
            "prompt": prompt,
            "width": width,
            "height": height,
            "length": length,
            "fps": 24,
            "steps": steps,
            "seed": seed,
            "ref_image_size": ref_image_size,
        },
        first_frame=first_path,
        last_frame=last_path,
        reference_image=reference_path,
    )


@app.get("/api/facefusion/capabilities")
async def facefusion_capabilities() -> dict:
    try:
        return await facefusion.capabilities()
    except Exception as exc:
        return {"_error": str(exc)}


@app.post("/api/facefusion/tasks", response_model=TaskRecord)
async def facefusion_task(
    processor: str = Form(...),
    params_json: str = Form(default="{}"),
    authorized_adult: bool = Form(default=False),
    target_asset_url: str = Form(default=""),
    source_asset_url: str = Form(default=""),
    target: UploadFile | None = File(default=None),
    source: UploadFile | None = File(default=None),
) -> TaskRecord:
    if processor in {"face_swapper", "deep_swapper", "expression_restorer"}:
        if not authorized_adult:
            raise HTTPException(
                status_code=400,
                detail="人物身份处理素材必须为本人、虚构人物或已获授权的成年人",
            )
    try:
        params = json.loads(params_json)
        if not isinstance(params, dict):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="处理参数格式错误")

    pending = settings.data_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    import uuid
    token = uuid.uuid4().hex

    if target is not None and target.filename:
        target_path = pending / f"{token}_target_{safe_name(target.filename)}"
        await assets.save_upload(target, target_path)
    elif target_asset_url.strip():
        try:
            target_path = assets.resolve_asset_url(target_asset_url.strip())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=400, detail="请选择目标素材或上传本地文件")

    source_path = None
    if source is not None and source.filename:
        source_path = pending / f"{token}_source_{safe_name(source.filename)}"
        await assets.save_upload(source, source_path)
    elif source_asset_url.strip():
        try:
            source_path = assets.resolve_asset_url(source_asset_url.strip())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if processor in {"face_swapper", "deep_swapper", "expression_restorer", "lip_syncer"} and source_path is None:
        raise HTTPException(status_code=400, detail="当前功能必须选择来源素材")

    return runner.submit_facefusion(
        processor=processor,
        params=params,
        source_path=source_path,
        target_path=target_path,
    )


@app.get("/api/tasks", response_model=list[TaskRecord])
async def list_tasks(limit: int = 100) -> list[TaskRecord]:
    return store.list(max(1, min(limit, 300)))


@app.get("/api/tasks/{task_id}", response_model=TaskRecord)
async def get_task(task_id: str) -> TaskRecord:
    task = store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.get("/api/assets")
async def list_assets(limit: int = 200) -> list[dict]:
    return assets.list_assets(max(1, min(limit, 500)))


@app.post("/api/assets/save")
async def save_asset(
    url: str = Form(...),
    name: str = Form(default=""),
) -> dict:
    try:
        return assets.save_existing_url(url.strip(), name.strip())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/assets/upload")
async def upload_asset(file: UploadFile = File(...)) -> dict:
    try:
        return await assets.upload_to_library(file)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/assets/delete")
async def delete_asset(url: str = Form(...)) -> dict:
    try:
        return assets.delete_asset_url(url.strip())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

# ===== V2.24 Production Asset Runtime APIs =====
import json as _production_json
import mimetypes as _production_mimetypes
import httpx as _production_httpx

@app.get("/production")
async def director_production_page() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "production.html")


def _production_task_payload(task_id: str) -> dict:
    def convert(record):
        if record is None:
            return None
        if hasattr(record, "model_dump"):
            return record.model_dump(mode="json")
        if isinstance(record, dict):
            return record
        if hasattr(record, "dict"):
            return record.dict()
        return None

    getter = getattr(store, "get", None)
    if callable(getter):
        try:
            data = convert(getter(task_id))
            if data:
                return data
        except Exception:
            pass
    for name in ("list", "list_recent", "all"):
        lister = getattr(store, name, None)
        if not callable(lister):
            continue
        for kwargs in ({"limit": 5000}, {}):
            try:
                rows = lister(**kwargs)
            except TypeError:
                continue
            except Exception:
                rows = []
            for row in rows or []:
                data = convert(row)
                if data and str(data.get("task_id") or "") == task_id:
                    return data
            if rows is not None:
                break
    raise FileNotFoundError(f"任务不存在：{task_id}")


def _sync_project_production_tasks(project_id: str) -> None:
    graph = director.production.ensure_project(project_id)
    for item in list((graph.get("assets") or {}).values()):
        if not item.get("active"):
            continue
        task_id = str((item.get("source") or {}).get("task_id") or "").strip()
        if not task_id:
            continue
        try:
            director.production.bind_task(
                project_id,
                item["asset_id"],
                _production_task_payload(task_id),
            )
        except FileNotFoundError:
            continue
    director.refresh_production_completion(project_id)


@app.get("/api/director/skills/{skill_name}/contract")
async def director_skill_contract(skill_name: str) -> dict:
    try:
        async with gpu.use(GPUOwner.gemma):
            return await director.get_skill_contract(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/director/projects/{project_id}/skill-runtime")
async def director_project_skill_runtime(project_id: str) -> dict:
    try:
        director.refresh_production_completion(project_id)
        return director.project_skill_runtime(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/director/projects/{project_id}/production")
async def director_project_production(
    project_id: str,
    sync_tasks: bool = True,
) -> dict:
    try:
        if sync_tasks:
            _sync_project_production_tasks(project_id)
        return director.project_production(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/director/projects/{project_id}/production/assets")
async def director_production_assets(
    project_id: str,
    stage: str = "",
    asset_type: str = "",
    asset_role: str = "",
    active_only: bool = False,
) -> list[dict]:
    try:
        return director.production.list_assets(
            project_id,
            stage=stage,
            asset_type=asset_type,
            asset_role=asset_role,
            active_only=active_only,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/director/projects/{project_id}/production/assets/{asset_id}")
async def director_production_asset(project_id: str, asset_id: str) -> dict:
    try:
        return director.production.get_asset(project_id, asset_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/assets/declare")
async def director_declare_production_asset(project_id: str, payload: dict) -> dict:
    try:
        item = director.production.declare_asset(
            project_id,
            stage=str(payload.get("stage") or ""),
            skill=str(payload.get("skill") or ""),
            logical_key=str(payload.get("logical_key") or ""),
            asset_type=str(payload.get("asset_type") or "FILE"),
            asset_role=str(payload.get("asset_role") or "project_asset"),
            name=str(payload.get("name") or "项目资产"),
            status=str(payload.get("status") or "planned"),
            source=payload.get("source") if isinstance(payload.get("source"), dict) else {},
            parent_asset_ids=payload.get("parent_asset_ids") if isinstance(payload.get("parent_asset_ids"), list) else [],
            entity_ids=payload.get("entity_ids") if isinstance(payload.get("entity_ids"), list) else [],
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            contract_artifact_id=str(payload.get("contract_artifact_id") or ""),
        )
        director.refresh_production_completion(project_id)
        return item
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/assets/register-file")
async def director_register_production_file(project_id: str, payload: dict) -> dict:
    try:
        asset_url = str(payload.get("url") or "").strip()
        assets.resolve_asset_url(asset_url)
        item = director.production.register_existing_file(
            project_id,
            stage=str(payload.get("stage") or ""),
            skill=str(payload.get("skill") or ""),
            logical_key=str(payload.get("logical_key") or ""),
            asset_type=str(payload.get("asset_type") or "FILE"),
            asset_role=str(payload.get("asset_role") or "project_asset"),
            name=str(payload.get("name") or "项目素材"),
            url=asset_url,
            source=payload.get("source") if isinstance(payload.get("source"), dict) else {"type":"existing_platform_file"},
            parent_asset_ids=payload.get("parent_asset_ids") if isinstance(payload.get("parent_asset_ids"), list) else [],
            entity_ids=payload.get("entity_ids") if isinstance(payload.get("entity_ids"), list) else [],
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            contract_artifact_id=str(payload.get("contract_artifact_id") or ""),
        )
        director.refresh_production_completion(project_id)
        return item
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/assets/{asset_id}/bind-task/{task_id}")
async def director_bind_production_task(project_id: str, asset_id: str, task_id: str) -> dict:
    try:
        item = director.production.bind_task(
            project_id, asset_id, _production_task_payload(task_id)
        )
        state = director.refresh_production_completion(project_id)
        return {"asset": item, "completion": state}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _production_form_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


async def _production_submit_existing_api(
    path: str,
    *,
    data: dict,
    files: dict | None = None,
) -> dict:
    async with _production_httpx.AsyncClient(
        base_url="http://127.0.0.1:6008",
        timeout=90.0,
        trust_env=False,
    ) as client:
        response = await client.post(path, data=data, files=files)
    if response.status_code >= 400:
        raise ValueError(
            f"现有平台生产接口返回 {response.status_code}: {response.text[-4000:]}"
        )
    body = response.json()
    if not isinstance(body, dict) or not str(body.get("task_id") or "").strip():
        raise RuntimeError("现有平台生产接口未返回有效 task_id")
    return body


def _production_asset_file(
    project_id: str,
    asset_id: str,
    field_name: str,
) -> tuple[str, tuple[str, bytes, str]]:
    url = director.production.asset_url(project_id, asset_id)
    if not url:
        raise ValueError(f"输入资产没有可用文件：{asset_id}")
    path = assets.resolve_asset_url(url)
    if not path.is_file():
        raise FileNotFoundError(f"输入资产文件不存在：{asset_id}")
    mime = _production_mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return asset_id, (path.name, path.read_bytes(), mime)


@app.post("/api/director/projects/{project_id}/production/assets/{asset_id}/execute")
async def director_execute_production_asset(
    project_id: str,
    asset_id: str,
    payload: dict,
) -> dict:
    raise HTTPException(status_code=409, detail="V2.25 已禁用生产结果自动绑定；请到 /single 生成候选结果，再手动加入项目资产")
    """Bridge a project asset to the platform's existing production APIs.

    The bridge does not duplicate ComfyUI/H3/FaceFusion logic. It submits to
    the existing endpoints, binds the returned TaskStore task to the versioned
    project asset, and records exact upstream asset dependencies.
    """
    try:
        target = director.production.get_asset(project_id, asset_id)
        current_status = str(target.get("status") or "").strip().lower()
        if current_status in {"queued", "generating"}:
            raise RuntimeError("当前项目资产已有运行中的生产任务")
        if current_status in {"ready", "failed", "superseded", "archived"}:
            target = director.production.fork_asset_version(
                project_id,
                asset_id,
                status="planned",
                source={"type": "capability_bridge_rerun"},
            )
            asset_id = str(target["asset_id"])

        capability = str(payload.get("capability") or "").strip().lower()
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        dependencies: list[str] = []
        prompt_asset_id = str(payload.get("prompt_asset_id") or "").strip()
        prompt_text = ""
        if prompt_asset_id:
            prompt_text = director.production.read_text_asset(project_id, prompt_asset_id)
            dependencies.append(prompt_asset_id)

        if capability == "image":
            allowed = {
                "positive_prompt", "negative_prompt", "model_key", "pose_control",
                "appearance_enhance_mode", "appearance_lora_strength", "aspect_ratio",
                "style_name", "style_strength", "output_preset", "steps", "cfg",
                "seed", "sampler", "scheduler", "count",
            }
            form = {
                k: _production_form_value(v)
                for k, v in params.items()
                if k in allowed
            }
            if not str(form.get("positive_prompt") or "").strip():
                form["positive_prompt"] = prompt_text
            if not str(form.get("positive_prompt") or "").strip():
                raise ValueError("图片生产需要 params.positive_prompt 或 prompt_asset_id")
            task = await _production_submit_existing_api(
                "/api/image/tasks", data=form
            )

        elif capability == "video":
            allowed = {
                "mode", "prompt", "width", "height", "length", "steps", "seed",
                "ref_image_size",
            }
            form = {
                k: _production_form_value(v)
                for k, v in params.items()
                if k in allowed
            }
            if not str(form.get("prompt") or "").strip():
                form["prompt"] = prompt_text
            if not str(form.get("prompt") or "").strip():
                raise ValueError("视频生产需要 params.prompt 或 prompt_asset_id")
            files = {}
            first_id = str(payload.get("first_frame_asset_id") or "").strip()
            last_id = str(payload.get("last_frame_asset_id") or "").strip()
            ref_id = str(payload.get("reference_image_asset_id") or "").strip()
            if first_id:
                dep, file_tuple = _production_asset_file(project_id, first_id, "first_frame")
                dependencies.append(dep)
                files["first_frame"] = file_tuple
            if last_id:
                dep, file_tuple = _production_asset_file(project_id, last_id, "last_frame")
                dependencies.append(dep)
                files["last_frame"] = file_tuple
            if ref_id:
                dep, file_tuple = _production_asset_file(project_id, ref_id, "reference_image")
                dependencies.append(dep)
                files["reference_image"] = file_tuple
            task = await _production_submit_existing_api(
                "/api/video/tasks", data=form, files=files or None
            )

        elif capability == "facefusion":
            processor = str(payload.get("processor") or "").strip()
            if not processor:
                raise ValueError("FaceFusion 生产需要 processor")
            target_input = str(payload.get("target_input_asset_id") or "").strip()
            source_input = str(payload.get("source_input_asset_id") or "").strip()
            if not target_input:
                raise ValueError("FaceFusion 生产需要 target_input_asset_id")
            target_url = director.production.asset_url(project_id, target_input)
            if not target_url:
                raise ValueError("FaceFusion 目标项目资产没有可用文件")
            assets.resolve_asset_url(target_url)
            dependencies.append(target_input)
            source_url = ""
            if source_input:
                source_url = director.production.asset_url(project_id, source_input)
                if not source_url:
                    raise ValueError("FaceFusion 来源项目资产没有可用文件")
                assets.resolve_asset_url(source_url)
                dependencies.append(source_input)
            form = {
                "processor": processor,
                "params_json": _production_json.dumps(params, ensure_ascii=False),
                "authorized_adult": "true" if bool(payload.get("authorized_adult")) else "false",
                "target_asset_url": target_url,
                "source_asset_url": source_url,
            }
            task = await _production_submit_existing_api(
                "/api/facefusion/tasks", data=form
            )
        else:
            raise ValueError("capability 必须是 image、video 或 facefusion")

        dependencies = list(dict.fromkeys(x for x in dependencies if x))
        director.production.set_asset_dependencies(
            project_id, asset_id, dependencies, merge=True
        )
        for parent_id in dependencies:
            director.production.add_relation(
                project_id,
                source_id=parent_id,
                target_id=asset_id,
                relation_type="input_to",
                metadata={"capability": capability},
            )
        item = director.production.bind_task(project_id, asset_id, task)
        completion = director.refresh_production_completion(project_id)
        return {
            "asset": item,
            "task": task,
            "completion": completion,
            "capability": capability,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/assets/{asset_id}/sync-task")
async def director_sync_production_task(project_id: str, asset_id: str) -> dict:
    try:
        item = director.production.get_asset(project_id, asset_id)
        task_id = str((item.get("source") or {}).get("task_id") or "").strip()
        if not task_id:
            raise ValueError("当前资产尚未绑定现有平台任务")
        item = director.production.bind_task(
            project_id, asset_id, _production_task_payload(task_id)
        )
        state = director.refresh_production_completion(project_id)
        return {"asset": item, "completion": state}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/assets/{asset_id}/activate")
async def director_activate_production_asset(project_id: str, asset_id: str) -> dict:
    try:
        item = director.production.set_active_version(project_id, asset_id)
        director.refresh_production_completion(project_id)
        return item
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/assets/{asset_id}/archive")
async def director_archive_production_asset(project_id: str, asset_id: str) -> dict:
    try:
        item = director.production.archive_asset(project_id, asset_id)
        director.refresh_production_completion(project_id)
        return item
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/director/projects/{project_id}/production/assets/{asset_id}/impact")
async def director_production_asset_impact(project_id: str, asset_id: str) -> dict:
    try:
        return director.production.impact(project_id, asset_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/director/projects/{project_id}/production/entities")
async def director_production_entities(project_id: str, entity_type: str = "") -> list[dict]:
    try:
        return director.production.list_entities(project_id, entity_type=entity_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/entities")
async def director_create_production_entity(project_id: str, payload: dict) -> dict:
    try:
        return director.production.create_entity(
            project_id,
            entity_type=str(payload.get("entity_type") or "generic"),
            name=str(payload.get("name") or ""),
            logical_key=str(payload.get("logical_key") or ""),
            stage=str(payload.get("stage") or ""),
            skill=str(payload.get("skill") or ""),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/entities/{entity_id}")
async def director_update_production_entity(project_id: str, entity_id: str, payload: dict) -> dict:
    try:
        return director.production.update_entity(project_id, entity_id, payload)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/relations")
async def director_create_production_relation(project_id: str, payload: dict) -> dict:
    try:
        return director.production.add_relation(
            project_id,
            source_id=str(payload.get("source_id") or ""),
            target_id=str(payload.get("target_id") or ""),
            relation_type=str(payload.get("relation_type") or "related_to"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/projects/{project_id}/production/refresh")
async def director_refresh_production(project_id: str) -> dict:
    try:
        _sync_project_production_tasks(project_id)
        return director.project_production(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
# ===== /V2.24 Production Asset Runtime APIs =====

# ===== V2.25 Single Generation Workbench APIs =====
import asyncio as _single_asyncio
import json as _single_json
import mimetypes as _single_mimetypes
import re as _single_re
import uuid as _single_uuid
from urllib.parse import urlparse as _single_urlparse, unquote as _single_unquote
from pathlib import Path as _SinglePath
import httpx as _single_httpx
from app.services.single_generation import SingleGenerationService as _SingleGenerationService

single_generation = _SingleGenerationService(settings, store, assets, gpu, comfyui)
_SINGLE_WORKSPACE_TITLE = "单次生成"
_SINGLE_ENTITY_TYPES = ["character", "scene", "prop", "item", "location", "chapter", "shot", "clip", "generic"]
_SINGLE_ASSET_ROLE_PRESETS = [
    "selected_output", "character_reference", "character_turnaround", "scene_reference",
    "prop_reference", "item_reference", "location_reference", "keyframe", "image_prompt",
    "video_prompt", "clip", "chapter_output", "final_video",
]


@app.get("/single")
async def single_generation_page() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "single.html")


def _single_workspace_project() -> dict:
    projects = director.list_projects()
    for item in projects:
        if str(item.get("title") or "").strip() == _SINGLE_WORKSPACE_TITLE:
            try:
                return director.get_project(str(item.get("project_id") or ""))
            except Exception:
                continue
    return director.create_project(_SINGLE_WORKSPACE_TITLE)


def _single_task_payload(task_id: str) -> dict:
    return _production_task_payload(task_id)


def _single_status_text(value) -> str:
    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip().lower()


def _single_completed_task(task_id: str) -> dict:
    task = _single_task_payload(task_id)
    if _single_status_text(task.get("status")) != "completed":
        raise RuntimeError("候选任务尚未完成，不能保存或加入项目资产")
    outputs = [str(x or "").strip() for x in task.get("output_files") or [] if str(x or "").strip()]
    if not outputs:
        raise RuntimeError("候选任务没有可用输出")
    return task


def _single_output(task_id: str, output_index: int) -> tuple[dict, str, _SinglePath]:
    task = _single_completed_task(task_id)
    outputs = list(task.get("output_files") or [])
    if output_index < 0 or output_index >= len(outputs):
        raise ValueError("output_index 超出候选结果范围")
    url = str(outputs[output_index]).strip()
    path = _single_resolve_input_url(url)
    if not path.is_file():
        raise FileNotFoundError(f"候选结果文件不存在：{url}")
    return task, url, path


async def _single_internal_post(path: str, *, data: dict, files: dict | None = None) -> dict:
    async with _single_httpx.AsyncClient(
        base_url="http://127.0.0.1:6008", timeout=120.0, trust_env=False
    ) as client:
        response = await client.post(path, data=data, files=files)
    if response.status_code >= 400:
        raise ValueError(f"现有平台接口 {path} 返回 {response.status_code}: {response.text[-3000:]}")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"现有平台接口 {path} 返回格式异常")
    return body


async def _single_save_library_file(path: _SinglePath) -> dict:
    mime = _single_mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    async with _single_httpx.AsyncClient(
        base_url="http://127.0.0.1:6008", timeout=600.0, trust_env=False
    ) as client:
        with path.open("rb") as fp:
            response = await client.post(
                "/api/assets/upload",
                files={"file": (path.name, fp, mime)},
            )
    if response.status_code >= 400:
        raise ValueError(f"保存到素材库失败：{response.status_code}: {response.text[-3000:]}")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("素材库接口返回格式异常")
    return body


def _single_resolve_input_url(url: str) -> _SinglePath:
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("输入素材 URL 为空")
    parsed = _single_urlparse(raw)
    path_text = parsed.path if (parsed.scheme or parsed.netloc) else raw.split("?", 1)[0].split("#", 1)[0]
    if path_text.startswith("/files/"):
        relative = _single_unquote(path_text[len("/files/"):]).lstrip("/")
        if not relative:
            raise ValueError("输入素材路径为空")
        root = _SinglePath(settings.data_dir).resolve()
        path = (root / relative).resolve()
        if path != root and root not in path.parents:
            raise ValueError("输入素材路径越界")
    else:
        try:
            path = assets.resolve_asset_url(raw).resolve()
        except Exception as exc:
            raise ValueError("只能选择本平台候选结果或素材库中的文件") from exc
    if not path.is_file():
        raise FileNotFoundError(f"输入素材不存在：{raw}")
    return path


def _single_path_kind(path: _SinglePath) -> str:
    mime = _single_mimetypes.guess_type(path.name)[0] or ""
    lower = path.name.lower()
    if mime.startswith("image/") or _single_re.search(r"\.(png|jpe?g|webp|bmp|gif)$", lower):
        return "image"
    if mime.startswith("video/") or _single_re.search(r"\.(mp4|webm|mov|mkv|avi)$", lower):
        return "video"
    if mime.startswith("audio/") or _single_re.search(r"\.(mp3|wav|m4a|flac|aac)$", lower):
        return "audio"
    return "file"


def _single_require_kind(path: _SinglePath, allowed: set[str], label: str) -> None:
    kind = _single_path_kind(path)
    if kind not in allowed:
        raise ValueError(f"{label}类型不正确：当前为 {kind}，允许 {','.join(sorted(allowed))}")


def _single_file_tuple(url: str, field_name: str, *, allowed: set[str] | None = None) -> tuple[str, tuple[str, bytes, str]]:
    path = _single_resolve_input_url(url)
    if allowed:
        _single_require_kind(path, allowed, field_name)
    mime = _single_mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return field_name, (path.name, path.read_bytes(), mime)


def _single_asset_type(path: _SinglePath) -> str:
    mime = _single_mimetypes.guess_type(path.name)[0] or ""
    if mime.startswith("image/"):
        return "IMAGE"
    if mime.startswith("video/"):
        return "VIDEO"
    if mime.startswith("audio/"):
        return "AUDIO"
    return "FILE"


def _single_slug(value: str) -> str:
    value = str(value or "").strip().lower()
    value = _single_re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-._")[:80] or "asset"


def _single_find_model_file(filename: str) -> str:
    name = str(filename or "").strip()
    if not name:
        return ""
    roots = [
        Path("/root/autodl-tmp/models/image"),
        Path("/root/autodl-tmp/ai-studio/ComfyUI/models"),
    ]
    for root in roots:
        if not root.is_dir():
            continue
        direct = list(root.glob("*/" + name)) + list(root.glob("*/*/" + name))
        for path in direct:
            if path.is_file():
                return str(path)
        try:
            for path in root.rglob(name):
                if path.is_file():
                    return str(path)
        except Exception:
            pass
    return ""


async def _single_h3_capabilities() -> dict:
    raw = {}
    try:
        raw = await h3_video.capabilities()
    except Exception as exc:
        raw = {"available": False, "message": f"H3 运行时能力检查暂不可用：{type(exc).__name__}: {exc}", "modes": {}}
    modes = raw.get("modes") if isinstance(raw, dict) else None
    if isinstance(modes, dict) and any(isinstance(v, dict) and v.get("enabled") is True for v in modes.values()):
        return raw
    # H3 belongs to the ComfyUI GPU workspace. When that workspace is stopped,
    # /object_info-based checks may report false negatives. Detect installed models
    # without switching GPU; the original /api/video/tasks still performs exact
    # runtime node/model validation after handoff.
    names = {
        "fl2va": str(getattr(settings, "h3_fl2va_model", "")),
        "ref2va": str(getattr(settings, "h3_ref2va_model", "")),
        "text_encoder": str(getattr(settings, "h3_text_encoder", "")),
        "video_vae": str(getattr(settings, "h3_video_vae", "")),
        "audio_vae": str(getattr(settings, "h3_audio_vae", "")),
    }
    found = {key: _single_find_model_file(value) for key, value in names.items()}
    common = bool(found["text_encoder"] and found["video_vae"] and found["audio_vae"])
    t2 = bool(common and found["fl2va"] and hasattr(h3_video, "generate_t2va"))
    fl = bool(common and found["fl2va"] and hasattr(h3_video, "generate_fl2va"))
    ref = bool(common and found["ref2va"] and hasattr(h3_video, "generate_ref2va"))
    return {
        "available": bool(t2 or fl or ref),
        "message": "H3 模型已安装；提交任务时自动切换 ComfyUI 并执行真实运行时检查" if (t2 or fl or ref) else str(raw.get("message") or "H3 模型依赖不完整"),
        "runtime_check": True,
        "modes": {
            "t2va": {"enabled": t2, "label": "文本生成视频", "runtime_check": True},
            "fl2va": {"enabled": fl, "label": "首尾帧生成视频", "runtime_check": True},
            "ref2va": {"enabled": ref, "label": "参考图生成视频", "runtime_check": True, "reference_types": ["image"]},
        },
        "models": names,
        "installed_files": found,
        "native_runtime": raw,
    }


@app.get("/api/single/catalog")
async def single_generation_catalog() -> dict:
    image_caps = await single_generation.capabilities()
    video_caps = await _single_h3_capabilities()
    try:
        face_caps = await facefusion.capabilities()
    except Exception as exc:
        face_caps = {"_error": f"FaceFusion 能力检查失败：{type(exc).__name__}: {exc}"}
    return {
        "workspace_title": _SINGLE_WORKSPACE_TITLE,
        "image_modes": ["txt2img", "img2img", "inpaint", "reference"],
        "image_capabilities": image_caps,
        "video_modes": ["t2va", "fl2va", "ref2va"],
        "video_capabilities": video_caps,
        "facefusion_capabilities": face_caps,
        "entity_types": _SINGLE_ENTITY_TYPES,
        "asset_role_presets": _SINGLE_ASSET_ROLE_PRESETS,
        "persistence_policy": "manual_promotion_only",
        "workflow_policy": "upstream_native_standard_workflows",
        "input_contracts": {
            "image": {
                "txt2img": {"required": ["prompt"], "optional": [], "ignored": ["input_image", "mask_image"]},
                "img2img": {"required": ["prompt", "input_image"], "optional": [], "ignored": ["mask_image"]},
                "inpaint": {"required": ["prompt", "input_image", "mask_image"], "optional": [], "ignored": []},
                "reference": {"required": ["prompt", "input_image"], "optional": [], "ignored": ["mask_image"]},
            },
            "video": {
                "t2va": {"required": ["prompt"], "optional": [], "ignored": ["first_frame", "last_frame", "reference_image"]},
                "fl2va": {"required": ["prompt", "first_frame"], "optional": ["last_frame"], "ignored": ["reference_image"]},
                "ref2va": {"required": ["prompt", "reference_image"], "optional": [], "ignored": ["first_frame", "last_frame"]},
            },
        },
    }


@app.get("/api/single/workspace")
async def single_generation_workspace() -> dict:
    project = _single_workspace_project()
    return {
        "project_id": project["project_id"],
        "title": project["title"],
        "candidate_count": len(single_generation.list_candidates()),
        "formal_asset_count": len(director.production.list_assets(project["project_id"])),
        "policy": {
            "task_output_is_candidate": True,
            "auto_save_library": False,
            "auto_add_project_asset": False,
            "auto_bind_entity": False,
        },
    }


@app.get("/api/single/tasks")
async def single_generation_tasks(include_dismissed: bool = False) -> list[dict]:
    result = []
    for item in single_generation.list_candidates(include_dismissed=include_dismissed):
        try:
            task = _single_task_payload(str(item.get("task_id") or ""))
            item = single_generation.sync_candidate(task)
            item["task"] = task
        except FileNotFoundError:
            item["task_missing"] = True
        result.append(item)
    return result


@app.post("/api/single/image/tasks")
async def single_generation_image_task(
    mode: str = Form(default="txt2img"),
    positive_prompt: str = Form(...),
    negative_prompt: str = Form(default=""),
    model_key: str = Form(default="smart"),
    aspect_ratio: str = Form(default="16:9"),
    style_name: str = Form(default="portrait_photo"),
    style_strength: str = Form(default="standard"),
    steps: int = Form(default=32),
    cfg: float = Form(default=6.5),
    seed: int = Form(default=-1),
    sampler: str = Form(default="dpmpp_2m"),
    scheduler: str = Form(default="karras"),
    count: int = Form(default=1),
    pose_control: str = Form(default="off"),
    appearance_enhance_mode: str = Form(default="off"),
    appearance_lora_strength: float = Form(default=0.30),
    denoise: float = Form(default=0.72),
    reference_weight: float = Form(default=0.80),
    upscale_enabled: bool = Form(default=False),
    input_asset_url: str = Form(default=""),
    mask_asset_url: str = Form(default=""),
    input_image: UploadFile | None = File(default=None),
    mask_image: UploadFile | None = File(default=None),
) -> dict:
    mode = mode.strip().lower()
    if mode not in {"txt2img", "img2img", "inpaint", "reference"}:
        raise HTTPException(status_code=400, detail="图片模式必须是 txt2img、img2img、inpaint 或 reference")
    if not positive_prompt.strip():
        raise HTTPException(status_code=400, detail="正向提示词不能为空")
    workspace = _single_workspace_project()
    try:
        if mode == "txt2img":
            form = {
                "positive_prompt": positive_prompt.strip(), "negative_prompt": negative_prompt.strip(),
                "model_key": model_key, "aspect_ratio": aspect_ratio, "style_name": style_name,
                "style_strength": style_strength, "steps": str(steps), "cfg": str(cfg), "seed": str(seed),
                "sampler": sampler, "scheduler": scheduler, "count": str(count),
                "pose_control": pose_control, "appearance_enhance_mode": appearance_enhance_mode,
                "appearance_lora_strength": str(appearance_lora_strength),
            }
            task = await _single_internal_post("/api/image/tasks", data=form)
            input_meta = {}
        else:
            caps = await single_generation.capabilities()
            cap = caps.get(mode) or {}
            if not cap.get("available"):
                raise ValueError(str(cap.get("reason") or f"{mode} 当前不可用"))
            pending = settings.data_dir / "pending"
            pending.mkdir(parents=True, exist_ok=True)
            if input_image is not None and input_image.filename:
                if not (input_image.content_type or "").startswith("image/"):
                    raise ValueError("输入必须是图片")
                input_path = pending / f"single_{_single_uuid.uuid4().hex}_{safe_name(input_image.filename)}"
                await assets.save_upload(input_image, input_path)
                input_meta = {"input": {"kind": "upload", "name": input_image.filename}}
            elif input_asset_url.strip():
                input_path = _single_resolve_input_url(input_asset_url.strip())
                _single_require_kind(input_path, {"image"}, "输入图片")
                input_meta = {"input": {"kind": "asset_url", "url": input_asset_url.strip()}}
            else:
                raise ValueError("当前图片模式必须上传输入图片或选择已有素材")

            mask_path = None
            if mode == "inpaint":
                if mask_image is not None and mask_image.filename:
                    if not (mask_image.content_type or "").startswith("image/"):
                        raise ValueError("局部重绘遮罩必须是图片")
                    mask_path = pending / f"single_mask_{_single_uuid.uuid4().hex}_{safe_name(mask_image.filename)}"
                    await assets.save_upload(mask_image, mask_path)
                    input_meta["mask"] = {"kind": "upload", "name": mask_image.filename}
                elif mask_asset_url.strip():
                    mask_path = _single_resolve_input_url(mask_asset_url.strip())
                    _single_require_kind(mask_path, {"image"}, "局部重绘遮罩")
                    input_meta["mask"] = {"kind": "asset_url", "url": mask_asset_url.strip()}
                else:
                    raise ValueError("局部重绘必须提供遮罩；白色区域重绘、黑色区域保持")

            preset = ASPECT_RATIO_PRESETS.get(aspect_ratio)
            if preset is None:
                raise ValueError("不支持的图片比例")
            if style_name not in STYLE_PRESETS or style_strength not in STYLE_STRENGTHS:
                raise ValueError("不支持的图片风格或风格强度")
            model = await comfyui.resolve_model(model_key, style_name)
            final_positive, final_negative = build_styled_prompts(
                positive_prompt=positive_prompt.strip(), negative_prompt=negative_prompt.strip(),
                aspect_ratio=aspect_ratio, style_name=style_name, style_strength=style_strength,
                model_key=model["key"],
            )
            params = {
                "positive_prompt": final_positive, "negative_prompt": final_negative,
                "model_key": model["key"], "model_label": model.get("label", model["key"]),
                "checkpoint": model["checkpoint"], "aspect_ratio": aspect_ratio,
                "base_width": int(preset["base_width"]), "base_height": int(preset["base_height"]),
                "output_width": int(preset["output_width"]), "output_height": int(preset["output_height"]),
                "steps": steps, "cfg": cfg, "seed": seed, "sampler": sampler, "scheduler": scheduler,
                "denoise": max(0.05, min(float(denoise), 1.0)),
                "reference_weight": max(0.05, min(float(reference_weight), 2.0)),
                "upscale_enabled": bool(upscale_enabled),
                "workflow_version": "single_image_workflows_v3",
            }
            task_obj = single_generation.submit_image_transform(
                mode=mode, params=params, input_path=input_path, mask_path=mask_path,
            )
            task = _single_task_payload(task_obj.task_id)
        candidate = single_generation.register_candidate(
            task, capability="image", mode=mode,
            workspace_project_id=workspace["project_id"], inputs=input_meta,
        )
        return {"task": task, "candidate": candidate, "auto_persisted": False}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/single/video/tasks")
async def single_generation_video_task(
    mode: str = Form(default="fl2va"),
    video_profile: str = Form(default="standard"),
    prompt: str = Form(...),
    width: int = Form(default=768),
    height: int = Form(default=448),
    length: int = Form(default=124),
    steps: int = Form(default=20),
    seed: int = Form(default=-1),
    ref_image_size: str = Form(default="match"),
    first_asset_url: str = Form(default=""),
    last_asset_url: str = Form(default=""),
    reference_asset_url: str = Form(default=""),
    first_frame: UploadFile | None = File(default=None),
    last_frame: UploadFile | None = File(default=None),
    reference_image: UploadFile | None = File(default=None),
) -> dict:
    mode = mode.strip().lower()
    video_profile = video_profile.strip().lower()
    if video_profile not in {"standard", "turbo"}:
        raise HTTPException(status_code=400, detail="video_profile 只能是 standard 或 turbo")
    if mode not in {"t2va", "fl2va", "ref2va"}:
        raise HTTPException(status_code=400, detail="视频模式必须是 t2va、fl2va 或 ref2va")
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="视频提示词不能为空")
    workspace = _single_workspace_project()
    try:
        form = {
            "mode": mode, "video_profile": video_profile, "prompt": prompt.strip(), "width": str(width),
            "height": str(height), "length": str(length), "steps": str(4 if video_profile == "turbo" else steps),
            "seed": str(seed), "ref_image_size": ref_image_size,
        }
        send_files: dict = {}
        input_meta: dict[str, Any] = {}

        async def add_image_input(field: str, upload: UploadFile | None, url: str, *, required: bool) -> None:
            if upload is not None and upload.filename:
                raw = await upload.read()
                if not (upload.content_type or "").startswith("image/"):
                    raise ValueError(f"{field} 必须是图片")
                send_files[field] = (upload.filename, raw, upload.content_type or "image/png")
                input_meta[field] = {"kind": "upload", "name": upload.filename}
                return
            if str(url or "").strip():
                _, file_tuple = _single_file_tuple(str(url).strip(), field, allowed={"image"})
                send_files[field] = file_tuple
                input_meta[field] = {"kind": "candidate_or_library", "url": str(url).strip()}
                return
            if required:
                raise ValueError(f"{field} 为当前模式必需输入")

        # Mode contract: only active fields are ever read. Hidden/stale values from
        # another video mode are intentionally ignored server-side.
        if mode == "t2va":
            pass
        elif mode == "fl2va":
            await add_image_input("first_frame", first_frame, first_asset_url, required=True)
            await add_image_input("last_frame", last_frame, last_asset_url, required=False)
        elif mode == "ref2va":
            await add_image_input("reference_image", reference_image, reference_asset_url, required=True)

        task = await _single_internal_post("/api/video/tasks", data=form, files=send_files or None)
        candidate = single_generation.register_candidate(
            task, capability="video", mode=mode,
            workspace_project_id=workspace["project_id"], inputs=input_meta,
        )
        return {"task": task, "candidate": candidate, "auto_persisted": False, "input_contract": mode}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/single/facefusion/tasks")
async def single_generation_facefusion_task(
    processor: str = Form(...),
    params_json: str = Form(default="{}"),
    authorized_adult: bool = Form(default=False),
    target_asset_url: str = Form(default=""),
    source_asset_url: str = Form(default=""),
    target: UploadFile | None = File(default=None),
    source: UploadFile | None = File(default=None),
) -> dict:
    workspace = _single_workspace_project()
    try:
        processor = processor.strip()
        caps = await facefusion.capabilities()
        spec = caps.get(processor) if isinstance(caps, dict) else None
        if not isinstance(spec, dict):
            raise ValueError(f"FaceFusion 未识别处理器：{processor}")
        if spec.get("available") is False:
            raise ValueError(str(spec.get("message") or spec.get("reason") or f"{processor} 当前环境不可用"))

        send_files: dict = {}
        input_meta: dict[str, Any] = {}
        target_kinds = set(str(x).strip().lower() for x in (spec.get("target_kinds") or ["image"]) if str(x).strip())
        source_kind = str(spec.get("source_kind") or "").strip().lower()
        source_required = bool(spec.get("source_required"))
        source_supported = bool(source_kind or source_required)

        async def add_target() -> None:
            if target is not None and target.filename:
                raw = await target.read()
                mime = target.content_type or "application/octet-stream"
                kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else "file"
                if kind not in target_kinds:
                    raise ValueError(f"当前处理器目标只支持：{','.join(sorted(target_kinds))}")
                send_files["target"] = (target.filename, raw, mime)
                input_meta["target"] = {"kind": "upload", "name": target.filename}
                return
            if target_asset_url.strip():
                path = _single_resolve_input_url(target_asset_url.strip())
                _single_require_kind(path, target_kinds, "目标素材")
                mime = _single_mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                send_files["target"] = (path.name, path.read_bytes(), mime)
                input_meta["target"] = {"kind": "candidate_or_library", "url": target_asset_url.strip()}
                return
            raise ValueError("请选择目标素材")

        async def add_source() -> None:
            if not source_supported:
                return
            allowed = {source_kind or "image"}
            if source is not None and source.filename:
                raw = await source.read()
                mime = source.content_type or "application/octet-stream"
                kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else "file"
                if kind not in allowed:
                    raise ValueError(f"当前处理器来源必须是：{next(iter(allowed))}")
                send_files["source"] = (source.filename, raw, mime)
                input_meta["source"] = {"kind": "upload", "name": source.filename}
                return
            if source_asset_url.strip():
                path = _single_resolve_input_url(source_asset_url.strip())
                _single_require_kind(path, allowed, "来源素材")
                mime = _single_mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                send_files["source"] = (path.name, path.read_bytes(), mime)
                input_meta["source"] = {"kind": "candidate_or_library", "url": source_asset_url.strip()}
                return
            if source_required:
                raise ValueError("当前处理器必须提供来源素材")

        await add_target()
        await add_source()
        form = {
            "processor": processor, "params_json": params_json,
            "authorized_adult": "true" if authorized_adult else "false",
            # Candidate/library selections are materialized into upload tuples above;
            # do not pass stale asset URLs to the underlying library-only resolver.
            "target_asset_url": "", "source_asset_url": "",
        }
        task = await _single_internal_post("/api/facefusion/tasks", data=form, files=send_files or None)
        candidate = single_generation.register_candidate(
            task, capability="facefusion", mode=processor.lower(),
            workspace_project_id=workspace["project_id"], inputs=input_meta,
        )
        return {"task": task, "candidate": candidate, "auto_persisted": False}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/single/tasks/{task_id}/save-library")
async def single_generation_save_library(task_id: str, payload: dict) -> dict:
    try:
        output_index = int(payload.get("output_index") or 0)
        task, _, path = _single_output(task_id, output_index)
        saved = await _single_save_library_file(path)
        single_generation.record_promotion(
            task_id, kind="material_library", output_index=output_index, result=saved
        )
        return {"task_id": task_id, "output_index": output_index, "saved": saved}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/single/tasks/{task_id}/add-project-asset")
async def single_generation_add_project_asset(task_id: str, payload: dict) -> dict:
    try:
        output_index = int(payload.get("output_index") or 0)
        task, task_url, path = _single_output(task_id, output_index)
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            project_id = _single_workspace_project()["project_id"]
        project = director.get_project(project_id)
        source_url = task_url
        library_saved = None
        if bool(payload.get("save_library_first")):
            library_saved = await _single_save_library_file(path)
            maybe_url = str(library_saved.get("url") or "").strip()
            if maybe_url:
                source_url = maybe_url
        entity_ids = [str(x).strip() for x in payload.get("entity_ids") or [] if str(x).strip()]
        entity_name = str(payload.get("entity_name") or "").strip()
        entity_type = str(payload.get("entity_type") or "").strip().lower()
        if entity_name:
            if entity_type not in _SINGLE_ENTITY_TYPES:
                entity_type = "generic"
            entity = director.production.create_entity(
                project_id,
                entity_type=entity_type,
                name=entity_name,
                logical_key=str(payload.get("entity_logical_key") or "").strip(),
                stage=str(payload.get("stage") or "").strip(),
                skill="",
                metadata={
                    "chapter_id": str(payload.get("chapter_id") or "").strip(),
                    "scene_id": str(payload.get("scene_id") or "").strip(),
                    "shot_id": str(payload.get("shot_id") or "").strip(),
                    "manual": True,
                },
            )
            if entity["entity_id"] not in entity_ids:
                entity_ids.append(entity["entity_id"])
        asset_role = str(payload.get("asset_role") or "selected_output").strip()
        asset_name = str(payload.get("name") or path.stem).strip()
        logical_key = str(payload.get("logical_key") or "").strip()
        if not logical_key:
            logical_key = f"manual:{_single_slug(entity_type or 'generic')}:{_single_slug(asset_role)}:{_single_uuid.uuid4().hex[:12]}"
        metadata = {
            "chapter_id": str(payload.get("chapter_id") or "").strip(),
            "scene_id": str(payload.get("scene_id") or "").strip(),
            "shot_id": str(payload.get("shot_id") or "").strip(),
            "single_task_id": task_id,
            "single_mode": single_generation.get_candidate(task_id).get("mode", ""),
            "manually_promoted": True,
        }
        item = director.production.register_existing_file(
            project_id,
            stage=str(payload.get("stage") or project.get("current_stage") or "").strip(),
            skill="",
            logical_key=logical_key,
            asset_type=str(payload.get("asset_type") or _single_asset_type(path)).strip().upper(),
            asset_role=asset_role,
            name=asset_name,
            url=source_url,
            source={
                "type": "manual_single_generation_promotion",
                "task_id": task_id,
                "module": str(task.get("module") or ""),
                "operation": str(task.get("operation") or ""),
            },
            parent_asset_ids=[str(x).strip() for x in payload.get("parent_asset_ids") or [] if str(x).strip()],
            entity_ids=entity_ids,
            metadata=metadata,
        )
        director.refresh_production_completion(project_id)
        result = {"asset": item, "library_saved": library_saved}
        single_generation.record_promotion(
            task_id, kind="project_asset", output_index=output_index, result=result
        )
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/single/tasks/{task_id}/dismiss")
async def single_generation_dismiss(task_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    try:
        deleted = []
        if bool(payload.get("delete_files")):
            task = _single_task_payload(task_id)
            task_root = store.task_dir(task_id).resolve()
            for url in task.get("output_files") or []:
                try:
                    path = _single_resolve_input_url(str(url)).resolve()
                    if task_root == path or task_root in path.parents:
                        if path.is_file():
                            path.unlink()
                            deleted.append(str(url))
                except Exception:
                    continue
        item = single_generation.dismiss(task_id)
        return {"candidate": item, "deleted_files": deleted}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

# ===== /V2.25 Single Generation Workbench APIs =====

# ===== V2.27 COMPLETE DIRECTOR WORKBENCH APIs =====
import json as _wb_json
import mimetypes as _wb_mimetypes
import secrets as _wb_secrets
from datetime import datetime as _wb_datetime, timezone as _wb_timezone

_WB_STAGE_SKILLS = {
    "01": "chuanzhang-chuangzuo-v1",
    "02": 'ai-studio-character-design',
    "03": 'ai-studio-visual-design',
    "04": "chuanzhang-fenjing-biaoqing",
}
_WB_CANDIDATE_ROOT = settings.data_dir / "director_workbench_candidates"
_WB_CANDIDATE_ROOT.mkdir(parents=True, exist_ok=True)


def _wb_now() -> str:
    return _wb_datetime.now(_wb_timezone.utc).isoformat()


def _wb_candidate_path(project_id: str) -> Path:
    director.get_project(project_id)
    return _WB_CANDIDATE_ROOT / f"{project_id}.json"


def _wb_load_candidates(project_id: str) -> list[dict]:
    path = _wb_candidate_path(project_id)
    if not path.is_file():
        return []
    try:
        data = _wb_json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _wb_save_candidates(project_id: str, rows: list[dict]) -> None:
    path = _wb_candidate_path(project_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        _wb_json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _wb_task_status(task: dict) -> str:
    value = task.get("status")
    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip().lower()


def _wb_sync_candidate_record(project_id: str, row: dict) -> dict:
    if row.get("confirmed_asset_id") or row.get("status") == "rejected":
        return row
    try:
        task = _production_task_payload(str(row.get("task_id") or ""))
    except FileNotFoundError:
        return row
    row["status"] = _wb_task_status(task) or row.get("status") or "queued"
    row["progress"] = int(task.get("progress") or 0)
    row["message"] = str(task.get("message") or "")
    row["error"] = str(task.get("error") or "")
    row["output_files"] = [
        str(x) for x in (task.get("output_files") or []) if str(x).strip()
    ]
    row["updated_at"] = _wb_now()
    return row


def _wb_sync_candidates(project_id: str) -> list[dict]:
    rows = _wb_load_candidates(project_id)
    changed = False
    for row in rows:
        before = _wb_json.dumps(row, sort_keys=True, ensure_ascii=False)
        _wb_sync_candidate_record(project_id, row)
        after = _wb_json.dumps(row, sort_keys=True, ensure_ascii=False)
        changed = changed or before != after
    if changed:
        _wb_save_candidates(project_id, rows)
    return rows


def _wb_find_candidate(project_id: str, candidate_id: str) -> tuple[list[dict], dict]:
    rows = _wb_load_candidates(project_id)
    for row in rows:
        if str(row.get("candidate_id") or "") == candidate_id:
            return rows, row
    raise FileNotFoundError(f"Director 候选不存在：{candidate_id}")


def _wb_asset_kind(project_id: str, asset_id: str) -> str:
    item = director.production.get_asset(project_id, asset_id)
    return str(item.get("asset_type") or "").strip().upper()


def _wb_validate_media_asset(project_id: str, asset_id: str, allowed: set[str], label: str) -> None:
    if not asset_id:
        raise ValueError(f"{label}不能为空")
    item = director.production.get_asset(project_id, asset_id)
    if str(item.get("status") or "").strip().lower() != "ready":
        raise ValueError(f"{label}必须是 READY 项目资产")
    if str(item.get("dependency_state") or "").strip().lower() == "stale":
        raise ValueError(f"{label}已 STALE，请先更新")
    kind = str(item.get("asset_type") or "").strip().upper()
    if kind not in allowed:
        raise ValueError(f"{label}类型必须是：{','.join(sorted(allowed))}")
    url = director.production.asset_url(project_id, asset_id)
    if not url:
        raise ValueError(f"{label}没有可用文件")
    path = assets.resolve_asset_url(url)
    if not path.is_file():
        raise FileNotFoundError(f"{label}文件不存在")


@app.get("/director-workbench")
async def director_workbench_page() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "director_workbench.html")


@app.get("/api/director/workbench/projects/{project_id}/snapshot")
async def director_workbench_snapshot(
    project_id: str,
    sync_candidates: bool = True,
) -> dict:
    try:
        project = director.get_project(project_id)
        director.refresh_production_completion(project_id)
        runtime = director.project_skill_runtime(project_id)
        graph = director.production.ensure_project(
            project_id, str(project.get("title") or "")
        )
        candidates = (
            _wb_sync_candidates(project_id)
            if sync_candidates else _wb_load_candidates(project_id)
        )
        try:
            face_caps = await facefusion.capabilities()
        except Exception as exc:
            face_caps = {"_error": {"available": False, "message": str(exc)}}
        return {
            "project": director.get_project(project_id),
            "skill_runtime": runtime,
            "assets": director.production.list_assets(project_id),
            "entities": director.production.list_entities(project_id),
            "relations": list(graph.get("relations") or []),
            "candidates": candidates,
            "facefusion_capabilities": face_caps,
            "stage_status": {
                stage: director.production.stage_status(project_id, stage)
                for stage in ("01", "02", "03", "04")
            },
            "policy": {
                "skill_is_business_source_of_truth": True,
                "media_candidate_requires_manual_confirmation": True,
                "candidate_does_not_complete_skill": True,
                "reuse_existing_production_backends": True,
            },
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/workbench/projects/{project_id}/structure")
async def director_workbench_create_structure(project_id: str, payload: dict) -> dict:
    try:
        project = director.get_project(project_id)
        stage = str(project.get("current_stage") or "")
        entity = director.production.create_entity(
            project_id,
            entity_type=str(payload.get("entity_type") or "generic"),
            name=str(payload.get("name") or ""),
            logical_key=str(payload.get("logical_key") or ""),
            stage=stage,
            skill=_WB_STAGE_SKILLS.get(stage, ""),
            metadata=payload.get("metadata")
            if isinstance(payload.get("metadata"), dict)
            else {},
        )
        parent = str(payload.get("parent_entity_id") or "").strip()
        if parent:
            valid = {
                item["entity_id"]
                for item in director.production.list_entities(project_id)
            }
            if parent not in valid:
                raise ValueError("父实体不存在或不属于当前项目")
            director.production.add_relation(
                project_id,
                source_id=parent,
                target_id=entity["entity_id"],
                relation_type="contains",
                metadata={"source": "director_workbench"},
            )
        return entity
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/director/workbench/projects/{project_id}/candidates")
async def director_workbench_candidates(project_id: str, sync: bool = True) -> list[dict]:
    try:
        return _wb_sync_candidates(project_id) if sync else _wb_load_candidates(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/director/workbench/projects/{project_id}/execute-candidate")
async def director_workbench_execute_candidate(project_id: str, payload: dict) -> dict:
    """Submit real production without completing the formal project asset.

    The TaskStore result is a candidate until the user explicitly confirms it.
    """
    try:
        project = director.get_project(project_id)
        target_asset_id = str(payload.get("target_asset_id") or "").strip()
        if not target_asset_id:
            raise ValueError("target_asset_id 不能为空")
        target = director.production.get_asset(project_id, target_asset_id)
        target_status = str(target.get("status") or "").strip().lower()
        if target_status in {"queued", "generating", "archived", "superseded"}:
            raise ValueError(f"当前目标资产状态不能提交候选：{target_status}")

        capability = str(payload.get("capability") or "").strip().lower()
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        prompt_asset_id = str(payload.get("prompt_asset_id") or "").strip()
        prompt_text = ""
        dependencies: list[str] = []
        if prompt_asset_id:
            prompt_asset = director.production.get_asset(project_id, prompt_asset_id)
            if not _studio_asset_is_current(prompt_asset):
                raise ValueError("Prompt asset is stale, failed, superseded or not READY")
            prompt_text = director.production.read_text_asset(project_id, prompt_asset_id)
            dependencies.append(prompt_asset_id)

        mode = str(payload.get("mode") or params.get("mode") or "").strip().lower()
        processor = str(payload.get("processor") or "").strip()

        if capability == "image":
            if str(target.get("asset_type") or "").upper() != "IMAGE":
                raise ValueError("图片候选必须对应 IMAGE 项目资产")
            if mode and mode != "txt2img":
                raise ValueError("Director 正式资产桥当前图片模式必须是 txt2img；编辑类操作在单次工作台完成后可手动加入项目")
            allowed = {
                "positive_prompt", "negative_prompt", "model_key", "pose_control",
                "appearance_enhance_mode", "appearance_lora_strength", "aspect_ratio",
                "style_name", "style_strength", "output_preset", "steps", "cfg",
                "seed", "sampler", "scheduler", "count", "semantic_compile",
            }
            form = {
                k: _production_form_value(v)
                for k, v in params.items() if k in allowed
            }
            if not str(form.get("positive_prompt") or "").strip():
                form["positive_prompt"] = prompt_text
            if not str(form.get("positive_prompt") or "").strip():
                raise ValueError("图片生产需要提示词项目资产或 positive_prompt")
            task = await _production_submit_existing_api("/api/image/tasks", data=form)
            mode = "txt2img"
            output_asset_type = "IMAGE"

        elif capability == "video":
            if str(target.get("asset_type") or "").upper() != "VIDEO":
                raise ValueError("视频候选必须对应 VIDEO 项目资产")
            if mode not in {"t2va", "fl2va", "ref2va"}:
                raise ValueError("视频模式必须是 t2va、fl2va 或 ref2va")
            allowed = {
                "mode", "prompt", "width", "height", "length", "steps", "seed",
                "ref_image_size", "video_profile",
            }
            form = {
                k: _production_form_value(v)
                for k, v in params.items() if k in allowed
            }
            form["mode"] = mode
            profile = str(form.get("video_profile") or "standard").strip().lower()
            if profile not in {"standard", "turbo"}:
                raise ValueError("video_profile 只能是 standard 或 turbo")
            form["video_profile"] = profile
            if profile == "turbo":
                form["steps"] = "4"
            if not str(form.get("prompt") or "").strip():
                form["prompt"] = prompt_text
            if not str(form.get("prompt") or "").strip():
                raise ValueError("视频生产需要提示词项目资产或 prompt")

            # Exact mode contract: inactive fields are never read.
            files = {}
            if mode == "fl2va":
                first_id = str(payload.get("first_frame_asset_id") or "").strip()
                last_id = str(payload.get("last_frame_asset_id") or "").strip()
                _wb_validate_media_asset(project_id, first_id, {"IMAGE"}, "FL2VA 首帧")
                dep, tup = _production_asset_file(project_id, first_id, "first_frame")
                dependencies.append(dep)
                files["first_frame"] = tup
                if last_id:
                    _wb_validate_media_asset(project_id, last_id, {"IMAGE"}, "FL2VA 尾帧")
                    dep, tup = _production_asset_file(project_id, last_id, "last_frame")
                    dependencies.append(dep)
                    files["last_frame"] = tup
            elif mode == "ref2va":
                ref_id = str(payload.get("reference_image_asset_id") or "").strip()
                _wb_validate_media_asset(project_id, ref_id, {"IMAGE"}, "REF2VA 参考图")
                dep, tup = _production_asset_file(project_id, ref_id, "reference_image")
                dependencies.append(dep)
                files["reference_image"] = tup
            # t2va intentionally reads no media fields.
            task = await _production_submit_existing_api(
                "/api/video/tasks", data=form, files=files or None
            )
            output_asset_type = "VIDEO"

        elif capability == "facefusion":
            if not processor:
                raise ValueError("FaceFusion 需要 processor")
            caps = await facefusion.capabilities()
            spec = caps.get(processor) if isinstance(caps, dict) else None
            if not isinstance(spec, dict):
                raise ValueError(f"FaceFusion 未识别处理器：{processor}")
            if spec.get("available") is False:
                raise ValueError(str(spec.get("message") or spec.get("reason") or "当前处理器不可用"))

            target_input = str(payload.get("target_input_asset_id") or "").strip()
            source_input = str(payload.get("source_input_asset_id") or "").strip()
            target_kinds = {
                str(x).strip().lower()
                for x in (spec.get("target_kinds") or ["image"])
                if str(x).strip()
            }
            target_allowed = {
                "IMAGE" if x == "image" else
                "VIDEO" if x == "video" else
                "AUDIO" if x == "audio" else "FILE"
                for x in target_kinds
            }
            _wb_validate_media_asset(project_id, target_input, target_allowed, "FaceFusion 目标")
            dep, target_file = _production_asset_file(project_id, target_input, "target")
            dependencies.append(dep)
            files = {"target": target_file}

            source_kind = str(spec.get("source_kind") or "").strip().lower()
            source_required = bool(spec.get("source_required"))
            source_supported = bool(source_kind or source_required)
            if source_supported:
                if source_input:
                    source_allowed = {
                        "IMAGE" if source_kind in {"", "image"} else
                        "VIDEO" if source_kind == "video" else
                        "AUDIO" if source_kind == "audio" else "FILE"
                    }
                    _wb_validate_media_asset(project_id, source_input, source_allowed, "FaceFusion 来源")
                    dep, source_file = _production_asset_file(project_id, source_input, "source")
                    dependencies.append(dep)
                    files["source"] = source_file
                elif source_required:
                    raise ValueError("当前 FaceFusion Processor 必须提供来源项目资产")

            form = {
                "processor": processor,
                "params_json": _production_json.dumps(params, ensure_ascii=False),
                "authorized_adult": "true" if bool(payload.get("authorized_adult")) else "false",
                "target_asset_url": "",
                "source_asset_url": "",
            }
            task = await _production_submit_existing_api(
                "/api/facefusion/tasks", data=form, files=files
            )
            mode = processor
            output_asset_type = str(target.get("asset_type") or "IMAGE").upper()
        else:
            raise ValueError("capability 必须是 image、video 或 facefusion")

        dependencies = list(dict.fromkeys(x for x in dependencies if x))
        candidate = {
            "candidate_id": "dcand_" + _wb_secrets.token_hex(10),
            "project_id": project_id,
            "target_asset_id": target_asset_id,
            "target_logical_key": str(target.get("logical_key") or ""),
            "target_contract_artifact_id": str(target.get("contract_artifact_id") or ""),
            "capability": capability,
            "mode": mode,
            "processor": processor,
            "task_id": str(task.get("task_id") or ""),
            "status": _wb_task_status(task) or "queued",
            "progress": int(task.get("progress") or 0),
            "message": str(task.get("message") or ""),
            "error": str(task.get("error") or ""),
            "output_files": [str(x) for x in (task.get("output_files") or []) if str(x).strip()],
            "output_asset_type": output_asset_type,
            "dependency_asset_ids": dependencies,
            "prompt_asset_id": prompt_asset_id,
            "params": params,
            "confirmed_asset_id": "",
            "created_at": _wb_now(),
            "updated_at": _wb_now(),
        }
        rows = _wb_load_candidates(project_id)
        rows.append(candidate)
        _wb_save_candidates(project_id, rows)
        return {
            "candidate": candidate,
            "formal_asset_unchanged": True,
            "manual_confirmation_required": True,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/director/workbench/projects/{project_id}/candidates/{candidate_id}/sync")
async def director_workbench_sync_candidate(project_id: str, candidate_id: str) -> dict:
    try:
        rows, row = _wb_find_candidate(project_id, candidate_id)
        _wb_sync_candidate_record(project_id, row)
        _wb_save_candidates(project_id, rows)
        return row
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/director/workbench/projects/{project_id}/candidates/{candidate_id}/confirm")
async def director_workbench_confirm_candidate(
    project_id: str,
    candidate_id: str,
    payload: dict,
) -> dict:
    try:
        rows, row = _wb_find_candidate(project_id, candidate_id)
        if row.get("confirmed_asset_id"):
            return {
                "candidate": row,
                "asset": director.production.get_asset(
                    project_id, str(row["confirmed_asset_id"])
                ),
                "already_confirmed": True,
            }
        if row.get("status") == "rejected":
            raise ValueError("该候选已丢弃")
        _wb_sync_candidate_record(project_id, row)
        if row.get("status") != "completed":
            raise ValueError(f"候选任务尚未完成：{row.get('status')}")
        task = _production_task_payload(str(row.get("task_id") or ""))
        outputs = [str(x) for x in (task.get("output_files") or []) if str(x).strip()]
        if not outputs:
            raise ValueError("候选任务没有输出文件")
        output_index = int(payload.get("output_index") or 0)
        if output_index < 0 or output_index >= len(outputs):
            raise ValueError("output_index 超出候选输出范围")
        selected = outputs[output_index]
        task = dict(task)
        task["output_files"] = [selected] + [x for i, x in enumerate(outputs) if i != output_index]

        target_id = str(row.get("target_asset_id") or "")
        target = director.production.get_asset(project_id, target_id)

        # V2.36.1: Shot candidates stay ephemeral until explicit adoption.
        # On adoption, publish the selected file into the stable canonical slot.
        if _studio_shot_canonical_key_for_target(target):
            return _studio_publish_confirmed_shot_candidate(
                project_id=project_id,
                candidate_id=candidate_id,
                rows=rows,
                row=row,
                target=target,
                task=task,
                selected=selected,
            )
        target_status = str(target.get("status") or "").strip().lower()
        if target_status in {"ready", "superseded", "archived"} or not target.get("active", True):
            target = director.production.fork_asset_version(
                project_id,
                target_id,
                status="planned",
                source={
                    "type": "director_candidate_confirm",
                    "candidate_id": candidate_id,
                },
            )
            target_id = str(target["asset_id"])

        deps = [
            str(x) for x in (row.get("dependency_asset_ids") or [])
            if str(x).strip()
        ]
        director.production.set_asset_dependencies(
            project_id, target_id, deps, merge=True
        )
        for parent_id in deps:
            director.production.add_relation(
                project_id,
                source_id=parent_id,
                target_id=target_id,
                relation_type="input_to",
                metadata={
                    "capability": row.get("capability"),
                    "candidate_id": candidate_id,
                },
            )
        item = director.production.bind_task(project_id, target_id, task)
        row["status"] = "confirmed"
        row["confirmed_asset_id"] = target_id
        row["confirmed_output_url"] = selected
        row["confirmed_at"] = _wb_now()
        row["updated_at"] = _wb_now()
        _wb_save_candidates(project_id, rows)
        completion = director.refresh_production_completion(project_id)
        return {
            "candidate": row,
            "asset": item,
            "completion": completion,
            "manual_confirmation_applied": True,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/director/workbench/projects/{project_id}/candidates/{candidate_id}/reject")
async def director_workbench_reject_candidate(
    project_id: str,
    candidate_id: str,
    payload: dict,
) -> dict:
    try:
        rows, row = _wb_find_candidate(project_id, candidate_id)
        if row.get("confirmed_asset_id"):
            raise ValueError("已经确认进入正式项目资产的候选不能再丢弃")
        row["status"] = "rejected"
        row["rejected_at"] = _wb_now()
        row["updated_at"] = _wb_now()
        _wb_save_candidates(project_id, rows)
        return {
            "candidate_id": candidate_id,
            "status": "rejected",
            "formal_asset_unchanged": True,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/director/workbench/projects/{project_id}/finalize-and-confirm-stage")
async def director_workbench_finalize_and_confirm_stage(project_id: str) -> dict:
    """Close the current stage through the real Skill before confirmation.

    If media became READY after the last LLM turn, refresh_production_completion
    may make completion READY without having generated the stage handoff yet.
    This route never fabricates a handoff. It executes one final current-Skill
    turn to perform the Skill's own stage close, then calls the existing guarded
    confirm_stage.
    """
    try:
        director.refresh_production_completion(project_id)
        project = director.get_project(project_id)
        if project.get("status") != "active":
            raise RuntimeError("导演项目已经完成")
        stage = str(project.get("current_stage") or "")
        state = (project.get("stage_state") or {}).get(stage, {}) or {}
        if not bool(state.get("stage_ready")):
            completion = ((state.get("skill_runtime") or {}).get("completion") or {})
            raise RuntimeError(
                "当前 Stage 尚未满足 Skill completion："
                + str(completion.get("reason") or "仍有必需产物未完成")
            )

        if not str(state.get("handoff") or "").strip():
            async with gpu.use(GPUOwner.gemma):
                await director.message(
                    project_id,
                    "当前生产 Skill 所要求的真实项目资产已经完成。"
                    "请严格按当前 Skill 原流程执行阶段收口："
                    "只基于已经确认的文本与 READY 项目资产完成最终检查和真实 handoff；"
                    "不要补造不存在的输入，不要提前执行下一阶段。",
                )
            director.refresh_production_completion(project_id)
            project = director.get_project(project_id)
            state = (project.get("stage_state") or {}).get(stage, {}) or {}
            if not bool(state.get("stage_ready")):
                raise RuntimeError("Skill 收口后当前 Stage 不再满足 completion，拒绝确认")
            if not str(state.get("handoff") or "").strip():
                raise RuntimeError("当前 Skill 收口后仍未产生真实 handoff，拒绝确认")

        result = await director.confirm_stage(project_id)
        return {
            "confirmed_stage": stage,
            "project": result,
            "used_existing_guarded_confirm_stage": True,
            "handoff_generated_by_current_skill": True,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

# ===== /V2.27 COMPLETE DIRECTOR WORKBENCH APIs =====

# ===== V2.28 MANJU STUDIO PRODUCT APIs =====
import asyncio as _studio_asyncio
import json as _studio_json
import mimetypes as _studio_mimetypes
import re as _studio_re
import secrets as _studio_secrets
import shutil as _studio_shutil
import subprocess as _studio_subprocess
from datetime import datetime as _studio_datetime, timezone as _studio_timezone
from pathlib import Path as _StudioPath

_STUDIO_JOB_ROOT = settings.data_dir / "studio_jobs"
_STUDIO_JOB_ROOT.mkdir(parents=True, exist_ok=True)
_STUDIO_TASKS: dict[str, _studio_asyncio.Task] = {}
_STUDIO_VIDEO_EDIT_ROOT = settings.data_dir / "studio_video_edit_jobs"
_STUDIO_VIDEO_EDIT_ROOT.mkdir(parents=True, exist_ok=True)
_STUDIO_VIDEO_EDIT_TASKS: dict[str, _studio_asyncio.Task] = {}
_STUDIO_STAGE_LABELS = {
    "01": "剧本",
    "02": "角色",
    "03": "视觉",
    "04": "分镜",
}
_STUDIO_STAGE_SKILLS = {
    "01": "chuanzhang-chuangzuo-v1",
    "02": 'ai-studio-character-design',
    "03": 'ai-studio-visual-design',
    "04": "chuanzhang-fenjing-biaoqing",
}


from app.services.story_continuity import StoryContinuityService as _StoryContinuityService

story_continuity = _StoryContinuityService(settings, director)
_STUDIO_CONTINUITY_TASKS: dict[str, _studio_asyncio.Task] = {}


async def _studio_continuity_analyze_job(project_id: str) -> None:
    try:
        await story_continuity.analyze_project(project_id)
    finally:
        _STUDIO_CONTINUITY_TASKS.pop(project_id, None)


def _studio_schedule_continuity(project_id: str, force: bool = False) -> bool:
    active = _STUDIO_CONTINUITY_TASKS.get(project_id)
    if active is not None and not active.done():
        return False
    if not force and not story_continuity.needs_analysis(project_id):
        return False
    task = _studio_asyncio.create_task(_studio_continuity_analyze_job(project_id))
    _STUDIO_CONTINUITY_TASKS[project_id] = task
    return True


def _studio_source_profile(project_id: str) -> dict:
    assets = [
        a for a in director.production.list_assets(project_id, active_only=True)
        if str(a.get("asset_role") or "") == "source_full"
        and str(a.get("status") or "").lower() == "ready"
    ]
    if not assets:
        return {"char_count": 0, "longform": False, "mode": "direct"}
    asset = assets[-1]
    meta = asset.get("metadata") or {}
    try:
        char_count = int(meta.get("char_count") or 0)
    except Exception:
        char_count = 0
    if char_count <= 0:
        try:
            char_count = len(director.production.read_text_asset(project_id, asset["asset_id"], max_chars=2_000_001))
        except Exception:
            char_count = 0
    return {
        "asset_id": asset.get("asset_id"),
        "char_count": char_count,
        "longform": char_count > 12000,
        "mode": "chunked_continuity" if char_count > 12000 else "direct",
    }


async def _studio_prepare_stage01_context(project_id: str, job: dict) -> dict:
    profile = _studio_source_profile(project_id)
    if not profile.get("longform"):
        return profile
    _studio_schedule_continuity(project_id)
    deadline = _studio_asyncio.get_running_loop().time() + 1800
    while True:
        snapshot = story_continuity.compact_snapshot(project_id)
        analysis = snapshot.get("analysis") or {}
        status = str(analysis.get("status") or "idle")
        done = int(analysis.get("chunks_done") or 0)
        total = int(analysis.get("chunks_total") or 0)
        job.update({
            "status": "running",
            "message": f"长章节正在分片建立上下文：{done}/{total or '?'}",
            "longform_context": {
                "char_count": profile.get("char_count") or 0,
                "chunks_done": done,
                "chunks_total": total,
                "analysis_status": status,
            },
            "updated_at": _studio_now(),
        })
        _studio_save_job(job)
        if status == "ready":
            return profile
        if status == "failed":
            raise RuntimeError(
                "长章节分片解析失败，未把超长原文直接塞进模型："
                + str(analysis.get("error") or analysis.get("message") or "unknown")
            )
        if _studio_asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("长章节分片解析超过等待上限；原文已保存，可重新执行连续性分析后继续")
        await _studio_asyncio.sleep(1.5)


def _studio_now() -> str:
    return _studio_datetime.now(_studio_timezone.utc).isoformat()


def _studio_job_path(job_id: str) -> _StudioPath:
    if not job_id.startswith("stjob_"):
        raise ValueError("非法 studio job_id")
    return _STUDIO_JOB_ROOT / f"{job_id}.json"


def _studio_save_job(job: dict) -> dict:
    path = _studio_job_path(str(job["job_id"]))
    temp = path.with_suffix(".tmp")
    temp.write_text(
        _studio_json.dumps(job, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)
    return job


def _studio_load_job(job_id: str) -> dict:
    path = _studio_job_path(job_id)
    if not path.is_file():
        raise FileNotFoundError(f"Studio 任务不存在：{job_id}")
    return _studio_json.loads(path.read_text(encoding="utf-8"))


def _studio_active_job(project_id: str) -> dict | None:
    rows: list[dict] = []
    for path in _STUDIO_JOB_ROOT.glob("stjob_*.json"):
        try:
            row = _studio_json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(row.get("project_id") or "") == project_id:
            rows.append(row)
    rows.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    for row in rows:
        if row.get("status") in {"queued", "running"}:
            task = _STUDIO_TASKS.get(str(row.get("job_id") or ""))
            if task is None or task.done():
                row["status"] = "interrupted"
                row["message"] = "平台曾重启；该后台阶段任务已中断，可直接重新点击生成继续。"
                row["updated_at"] = _studio_now()
                _studio_save_job(row)
            else:
                return row
    return rows[0] if rows else None


def _studio_contract_missing_media(result: dict) -> list[dict]:
    runtime = result.get("skill_runtime") or {}
    completion = runtime.get("completion") or {}
    missing = set(completion.get("missing_artifact_ids") or [])
    if not missing:
        return []
    contract = result.get("skill_contract") or {}
    found: list[dict] = []
    for group in contract.get("output_groups") or []:
        for spec in group.get("artifacts") or []:
            aid = str(spec.get("artifact_id") or "")
            kind = str(spec.get("asset_type") or "").upper()
            if aid in missing and kind in {"IMAGE", "VIDEO", "AUDIO", "FILE"}:
                found.append({
                    "artifact_id": aid,
                    "asset_type": kind,
                    "name": str(spec.get("name") or aid),
                    "asset_role": str(spec.get("asset_role") or "skill_artifact"),
                })
    return found


async def _studio_character_role_mode(project_id: str) -> dict:
    """Resolve whether Stage02 truly needs an identity reference image.

    This is semantic routing with a fixed schema.  It does not use character-name
    keyword tables and it does not assume that a human-looking role is a real
    person.  Reference media is a hard requirement only when the user's project
    facts explicitly require preserving/matching a specific person's identity.
    """
    project = director.get_project(project_id)
    confirmed = (project.get("confirmed_outputs") or {}).get("01") or {}
    handoff = str(confirmed.get("handoff") or "").strip()

    continuity = ""
    try:
        continuity = story_continuity.episode_context(project_id, max_chars=2600)
    except Exception:
        continuity = ""

    source = ""
    try:
        source_assets = [
            a for a in director.production.list_assets(project_id, stage="01", active_only=True)
            if str(a.get("asset_role") or "") in {"source_full", "source_brief"}
            and str(a.get("status") or "").lower() == "ready"
        ]
        if source_assets:
            source = director.production.read_text_asset(
                project_id,
                source_assets[-1]["asset_id"],
                max_chars=1450,
            )
    except Exception:
        source = ""

    # Keep this classifier compact even for long-form projects.  Stage01 itself
    # already owns the long-context pipeline; Stage02 only needs enough evidence
    # to choose the identity-preservation branch.
    context = (
        f"PROJECT_TITLE={str(project.get('title') or '')[:160]}\n\n"
        f"=== CONFIRMED_STAGE01 ===\n{handoff[:3000] or '<none>'}\n\n"
        f"=== CONTINUITY_FACTS ===\n{continuity[:2600] or '<none>'}\n\n"
        f"=== SOURCE_EXCERPT ===\n{source[:1800] or '<none>'}"
    )

    system_prompt = """你是角色生产模式解析器，只负责判断当前角色阶段是否必须提供人物身份参考图，不执行角色创作。
必须只依据项目已确认事实和用户原始要求判断，不得因为角色是人形、写实风格或需要高质量肖像就自动要求真人照片。

固定规则：
1. 只有用户明确要求保留/匹配某个特定现实人物的身份、脸部或本人长相，或者任务明确依赖某张人物身份参考图时，identity_preservation_required 才为 true。
2. 虚构、原创、文学/神话/漫画/动画/游戏等角色，默认不需要真人身份保持；即使最终要生成写实画面，参考图也只能是可选增强。
3. 用户没有明确要求身份保持时，不得把“缺少真人照片”升级为流程阻断条件。
4. 不得根据人物姓名关键词表判断；按完整语义判断。

返回严格 JSON，不要 Markdown：
{"character_source":"fictional|original|real_person|reference_based|unknown","identity_preservation_required":false,"reference_image_required":false,"reference_image_optional":true,"reason":"一句话"}"""

    try:
        _, parsed, _ = await director._structured_json_call(
            phase="studio_stage02_character_role_mode",
            messages=[{"role": "user", "content": context}],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=260,
            contract='{"character_source":"fictional|original|real_person|reference_based|unknown","identity_preservation_required":false,"reference_image_required":false,"reference_image_optional":true,"reason":"一句话"}',
        )
        def _as_bool(value) -> bool:
            if value is True:
                return True
            if value is False or value is None:
                return False
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        source_kind = str(parsed.get("character_source") or "unknown").strip().lower()
        if source_kind not in {"fictional", "original", "real_person", "reference_based", "unknown"}:
            source_kind = "unknown"
        identity_required = _as_bool(parsed.get("identity_preservation_required"))
        reference_required = _as_bool(parsed.get("reference_image_required")) and identity_required
        return {
            "schema_version": "character_role_mode_v1",
            "character_source": source_kind,
            "identity_preservation_required": identity_required,
            "reference_image_required": reference_required,
            "reference_image_optional": not reference_required,
            "reason": str(parsed.get("reason") or "").strip(),
            "decision_mode": "structured_semantic",
        }
    except Exception as exc:
        # The safe product fallback is optional reference media.  Requiring a
        # photo by default is precisely the failure this resolver prevents.
        return {
            "schema_version": "character_role_mode_v1",
            "character_source": "unknown",
            "identity_preservation_required": False,
            "reference_image_required": False,
            "reference_image_optional": True,
            "reason": "角色模式解析暂不可用；参考图按可选增强处理，不阻断角色设计",
            "decision_mode": "optional_reference_fallback",
            "resolver_error": type(exc).__name__,
        }

async def _studio_progress_decision(
    *,
    skill_name: str,
    result: dict,
    character_mode: dict | None = None,
) -> dict:
    state = result.get("control") or {}
    content = str(result.get("content") or "")
    next_expected = str(state.get("next_expected_action") or "")

    control_text = (next_expected + "\n" + content[-1600:]).strip()
    required_files = [
        str(x).strip() for x in (result.get("required_files") or [])
        if str(x).strip()
    ]
    approval_signal = bool(_studio_re.search(
        r"(?:请|可)?(?:直接)?回复.{0,10}(?:通过|确认|继续)"
        r"|(?:通过|确认).{0,16}(?:进入|继续).{0,10}(?:下一步|下一阶段)"
        r"|(?:确认无误|检查无误).{0,12}(?:继续|下一步|下一阶段)",
        control_text,
        flags=_studio_re.I | _studio_re.S,
    ))
    new_input_signal = bool(_studio_re.search(
        r"(?:请|需要|必须).{0,10}(?:补充|提供|上传|选择|指定).{0,16}"
        r"(?:事实|信息|素材|文件|图片|照片|视频|音频|方案|方向|参数|身份)",
        control_text,
        flags=_studio_re.I | _studio_re.S,
    ))
    if approval_signal and not new_input_signal and not required_files:
        return {
            "action": "auto_continue",
            "reason": "当前只要求批准/确认已有成果；产品工作流自动继续",
            "decision_mode": "deterministic_control",
        }

    mode_json = _studio_json.dumps(character_mode or {}, ensure_ascii=False)
    system_prompt = """你是创作工作流的交互决策器，不执行创作。
用户已经点击“生成当前阶段”，这代表普通内部步骤完成后可自动继续，不需要用户逐步回复“通过”。
只有以下情况才 needs_user：
- 下一步必须获得用户尚未提供的新事实或真正必需的外部素材；
- 存在会显著改变作品方向的多个方案，必须由用户选择；
- 当前 Skill 明确要求由用户做不可替代的最终选择。
如果只是要求确认当前已完成内部步骤、继续、检查或进入下一内部步骤，返回 auto_continue。

如果提供 CHARACTER_MODE：
- reference_image_required=false 时，人物参考图不是当前角色阶段的硬前置条件；不得仅因为输出中提出上传人物/真人参考图就判 needs_user，应该继续基于剧本和上游事实完成角色设计。
- reference_image_required=true 时，确实缺少身份参考素材可以 needs_user。
CHARACTER_MODE 是上游固定 JSON Schema 的语义解析结果，不得自行改写。

不要使用人物名关键词表；按完整语义判断。
返回严格 JSON：{"action":"auto_continue|needs_user","reason":"一句话"}"""
    prompt = f"""CURRENT_SKILL={skill_name}
CHARACTER_MODE={mode_json}

=== CURRENT OUTPUT ===
{content[-5000:]}

=== NEXT EXPECTED ACTION ===
{next_expected}
"""
    try:
        _, parsed, _ = await director._structured_json_call(
            phase="studio_progress_decision",
            messages=[{"role": "user", "content": prompt}],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=180,
            contract='{"action":"auto_continue|needs_user","reason":"一句话"}',
        )
        action = str(parsed.get("action") or "").strip()
        if action not in {"auto_continue", "needs_user"}:
            action = "needs_user"
        return {
            "action": action,
            "reason": str(parsed.get("reason") or ""),
            "decision_mode": "semantic",
        }
    except Exception as exc:
        if character_mode and character_mode.get("reference_image_required") is False:
            return {
                "action": "auto_continue",
                "reason": "角色参考图为可选增强；交互判断失败时继续非身份保持分支",
                "decision_mode": "character_mode_fallback",
            }
        return {
            "action": "needs_user",
            "reason": "交互判断不可用，已安全暂停，避免替用户编造输入：" + type(exc).__name__,
            "decision_mode": "safe_fallback",
        }


# ===== V2.35.7B DETAILED STORYBOARD PIPELINE =====

def _studio_stage04_scope(state: dict) -> tuple[list[dict], str]:
    scenes = list(state.get("scenes") or [])
    active_episode = str(state.get("active_episode_id") or "").strip()
    if active_episode:
        scoped = [x for x in scenes if str(x.get("episode_id") or "") == active_episode]
        if scoped:
            scenes = scoped
    scenes.sort(key=lambda x: (
        int(x.get("sequence") or 0),
        int(x.get("order") or 0),
        str(x.get("scene_id") or ""),
    ))
    return scenes, active_episode


def _studio_stage04_full_source(project_id: str) -> tuple[str, str]:
    profile = _studio_source_profile(project_id)
    asset_id = str(profile.get("asset_id") or "")
    if not asset_id:
        return "", ""
    return asset_id, director.production.read_text_asset(
        project_id, asset_id, max_chars=2_000_001
    )


def _studio_stage04_upstream(project: dict) -> dict:
    confirmed = project.get("confirmed_outputs") or {}

    def cut(stage: str, limit: int) -> str:
        value = str((confirmed.get(stage) or {}).get("handoff") or "").strip()
        if len(value) <= limit:
            return value
        head = limit * 2 // 3
        return value[:head] + "\n...[上游成果按场景预算节选]...\n" + value[-(limit-head):]

    return {
        "character_bible": cut("02", 320),
        "visual_bible": cut("03", 480),
    }


def _studio_stage04_scene_source(scene: dict, source_text: str) -> str:
    try:
        start = max(0, int(scene.get("source_start") or 0))
        end = max(start, int(scene.get("source_end") or start))
    except Exception:
        start, end = 0, 0
    if source_text and end > start:
        lo = max(0, start - 220)
        hi = min(len(source_text), end + 220)
        return source_text[lo:hi][:12000]
    return str(scene.get("source_excerpt") or "")[:6000]


def _studio_stage04_source_window(
    source: str,
    batch_index: int,
    batch_total: int,
    max_chars: int = 1800,
) -> str:
    text = str(source or "")
    if len(text) <= max_chars:
        return text
    total = max(1, int(batch_total))
    index = max(0, min(total - 1, int(batch_index)))
    if total == 1:
        head = max_chars * 2 // 3
        return (
            text[:head]
            + "\n...[场景原文中段按上下文预算省略]...\n"
            + text[-(max_chars-head):]
        )
    span = max_chars
    max_start = max(0, len(text) - span)
    start = int(round(max_start * index / max(1, total - 1)))
    return text[start:start + span]


def _studio_stage04_scene_beats(state: dict, scene_id: str) -> list[dict]:
    rows = []
    for shot in state.get("shots") or []:
        if str(shot.get("scene_id") or "") != scene_id:
            continue
        if not bool(shot.get("provisional")):
            continue
        summary = str(shot.get("summary") or "").strip()
        if not summary:
            continue
        rows.append({
            "order": int(shot.get("order") or len(rows) + 1),
            "summary": summary[:700],
            "character_entity_ids": list(shot.get("character_entity_ids") or []),
            "prop_entity_ids": list(shot.get("prop_entity_ids") or []),
        })
    rows.sort(key=lambda x: x["order"])
    return rows


def _studio_stage04_allowed_ids(scene: dict, resolved: dict) -> tuple[set[str], set[str]]:
    chars = {str(x) for x in (scene.get("character_entity_ids") or []) if str(x)}
    props = {str(x) for x in (scene.get("prop_entity_ids") or []) if str(x)}
    for row in resolved.get("characters") or []:
        eid = str(row.get("entity_id") or "")
        if eid:
            chars.add(eid)
    for row in resolved.get("props") or []:
        eid = str(row.get("entity_id") or "")
        if eid:
            props.add(eid)
    return chars, props


def _studio_stage04_clean_ids(values, allowed: set[str]) -> list[str]:
    result = []
    for value in values or []:
        eid = str(value or "").strip()
        if eid and eid in allowed and eid not in result:
            result.append(eid)
    return result


async def _studio_stage04_wait_continuity(project_id: str, job: dict) -> dict:
    _studio_schedule_continuity(project_id)
    deadline = _studio_asyncio.get_running_loop().time() + 1800
    while True:
        snap = story_continuity.compact_snapshot(project_id)
        analysis = snap.get("analysis") or {}
        status = str(analysis.get("status") or "idle")
        done = int(analysis.get("chunks_done") or 0)
        total = int(analysis.get("chunks_total") or 0)
        if status == "ready":
            return story_continuity.load(project_id)
        if status == "failed":
            raise RuntimeError(
                "连续性分片失败，不能生成详细分镜："
                + str(analysis.get("error") or analysis.get("message") or "unknown")
            )
        job.update({
            "status": "running",
            "message": f"正在准备长章场景事实：{done}/{total or '?'}",
            "updated_at": _studio_now(),
        })
        _studio_save_job(job)
        if _studio_asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("等待连续性分片超过上限；未使用截断整章继续生成")
        await _studio_asyncio.sleep(1)


# ===== V2.35.8 STAGE04 RESILIENT BATCH PARSING =====

def _studio_stage04_find_shot_list(value, depth: int = 0) -> list[dict]:
    if depth > 4:
        return []

    if isinstance(value, list):
        dict_rows = [x for x in value if isinstance(x, dict)]
        if not dict_rows:
            return []
        shot_fields = {
            "summary", "action", "composition", "shot_size", "camera",
            "camera_move", "image_prompt", "video_prompt", "duration_seconds",
            "title", "dialogue", "narration", "performance", "environment",
        }
        score = sum(
            1 for row in dict_rows[:4]
            if any(key in row for key in shot_fields)
        )
        if score:
            return dict_rows
        for item in dict_rows:
            nested = _studio_stage04_find_shot_list(item, depth + 1)
            if nested:
                return nested
        return []

    if isinstance(value, dict):
        direct = value.get("shots")
        if isinstance(direct, list):
            rows = [x for x in direct if isinstance(x, dict)]
            if rows:
                return rows
        for nested_value in value.values():
            if isinstance(nested_value, (dict, list)):
                nested = _studio_stage04_find_shot_list(
                    nested_value, depth + 1
                )
                if nested:
                    return nested
    return []


def _studio_stage04_json_candidates(raw_text: str) -> list[object]:
    text = str(raw_text or "").strip()
    if not text:
        return []

    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl >= 0:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()

    candidates = []
    try:
        candidates.append(_studio_json.loads(text))
    except Exception:
        pass

    decoder = _studio_json.JSONDecoder()
    starts = [
        idx for idx, char in enumerate(text)
        if char in "[{"
    ][:160]
    for idx in starts:
        try:
            value, _end = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        candidates.append(value)
        if len(candidates) >= 12:
            break
    return candidates


def _studio_stage04_extract_shots(parsed: object, raw_text: str) -> list[dict]:
    rows = _studio_stage04_find_shot_list(parsed)
    if rows:
        return rows
    for candidate in _studio_stage04_json_candidates(raw_text):
        rows = _studio_stage04_find_shot_list(candidate)
        if rows:
            return rows
    return []


async def _studio_stage04_call_batch_resilient(
    *,
    system_prompt: str,
    prompt: str,
    scene_index: int,
    scene_total: int,
    batch_index: int,
    batch_total: int,
) -> list[dict]:
    attempts = (
        {
            "temperature": 0.20,
            "max_tokens": 2400,
            "suffix": "",
        },
        {
            "temperature": 0.05,
            "max_tokens": 2400,
            "suffix": (
                "\n\nSTRICT_RETRY:\n"
                "上一轮没有形成可解析的合同 JSON。"
                "本轮禁止解释、禁止 Markdown、禁止代码围栏。"
                "只返回一个 JSON object，顶层唯一必需数组为 shots。"
                "每个字段保持简洁，但不得省略实际剧情镜头。"
            ),
        },
    )

    diagnostics = []
    for attempt_no, cfg in enumerate(attempts, 1):
        raw_text = ""
        try:
            raw_text, parsed, _meta = await director._structured_json_call(
                phase="studio_stage04_scene_storyboard_batch",
                messages=[{
                    "role": "user",
                    "content": prompt + cfg["suffix"],
                }],
                system_prompt=system_prompt,
                temperature=float(cfg["temperature"]),
                max_tokens=int(cfg["max_tokens"]),
                contract='{"shots":[{"title":"","duration_seconds":3.0,"summary":"","composition":"","shot_size":"","camera":"","camera_move":"","action":"","performance":"","environment":"","dialogue":"","narration":"","sound":"","music":"","continuity":"","image_prompt":"","video_prompt":"","covered_beat_orders":[],"character_entity_ids":[],"prop_entity_ids":[]}]}',
            )
            rows = _studio_stage04_extract_shots(
                parsed, str(raw_text or "")
            )
            if rows:
                return rows
            diagnostics.append(
                f"attempt={attempt_no}: parsed_without_shots "
                f"raw_chars={len(str(raw_text or ''))}"
            )
        except Exception as exc:
            diagnostics.append(
                f"attempt={attempt_no}: "
                f"{type(exc).__name__}: {str(exc)[:280]}"
            )

    raise RuntimeError(
        f"场景 {scene_index}/{scene_total} 的批次 "
        f"{batch_index + 1}/{batch_total} 两次结构化生成均失败；"
        + " | ".join(diagnostics)
    )

# ===== /V2.35.8 STAGE04 RESILIENT BATCH PARSING =====


# ===== V2.37.1 STRICT STAGE04 CONTRACT =====
import asyncio as _studio_v2371_asyncio
import hashlib as _studio_v2371_hashlib
import re as _studio_v2371_re
import secrets as _studio_v2371_secrets

def _studio_v2371_cut(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit]

def _studio_v2371_evidence_anchors(source: str) -> list[dict]:
    source = str(source or "").strip()
    if not source:
        return []
    pieces, last = [], 0
    for m in _studio_v2371_re.finditer(r"[。！？!?；;]+|\n+", source):
        end = m.end()
        part = source[last:end].strip()
        if part:
            pieces.append(part)
        last = end
    tail = source[last:].strip()
    if tail:
        pieces.append(tail)

    anchors = []
    for part in pieces:
        has_terminal = bool(_studio_v2371_re.search(r"[。！？!?；;]$", part))
        # Generic title/header filter: do not use short non-sentence fragments
        # as story evidence.
        if len(part) <= 48 and not has_terminal:
            continue
        chunks = [part] if len(part) <= 190 else [
            part[i:i+160].strip()
            for i in range(0, len(part), 120)
            if part[i:i+160].strip()
        ]
        for chunk in chunks:
            anchors.append({"id": f"E{len(anchors)+1:03d}", "text": chunk})
            if len(anchors) >= 28:
                return anchors

    if anchors:
        return anchors

    # Punctuation-poor source fallback: still exact contiguous source text.
    for i in range(0, len(source), 120):
        chunk = source[i:i+150].strip()
        if chunk:
            anchors.append({"id": f"E{len(anchors)+1:03d}", "text": chunk})
        if len(anchors) >= 20 or i + 150 >= len(source):
            break
    return anchors

def _studio_v2371_anchor_map(anchors: list[dict]) -> dict[str, str]:
    return {
        str(x.get("id") or ""): str(x.get("text") or "")
        for x in (anchors or [])
        if isinstance(x, dict)
        and str(x.get("id") or "")
        and str(x.get("text") or "")
    }

def _studio_v2371_clean_ids(value: object, allowed: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        key = str(item or "").strip()
        if key and key in allowed and key not in result:
            result.append(key)
    return result

def _studio_v2371_contract_payload(shot: dict) -> dict:
    keys = (
        "shot_id","scene_id","global_order","title","summary","duration_seconds",
        "composition","shot_size","camera","camera_move","action","performance",
        "environment","continuity","representative_state","video_start_state",
        "video_end_state","image_prompt","video_start_prompt","video_prompt",
        "covered_beat_orders","source_provenance","character_entity_ids",
        "prop_entity_ids","stage04_contract_version",
    )
    return {key: shot.get(key) for key in keys}

def _studio_shot_contract_fingerprint(shot: dict) -> str:
    raw = _studio_json.dumps(
        _studio_v2371_contract_payload(shot),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return _studio_v2371_hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _studio_v2371_require_strict_shot(shot: dict) -> None:
    if str(shot.get("stage04_contract_version") or "") != "strict-shot-v2":
        raise ValueError("当前镜头仍是旧④分镜合同；请先重建④正式分镜（Qwen3-32B）")
    required = (
        "representative_state","video_start_state","video_end_state",
        "image_prompt","video_start_prompt","video_prompt",
    )
    missing = [k for k in required if not str(shot.get(k) or "").strip()]
    if missing:
        raise ValueError("当前镜头严格制作字段不完整：" + ", ".join(missing))
    if str(shot.get("runtime_version") or "") != "2.39.6.3-stage04-full-pipeline-preflight":
        raise ValueError("当前镜头来自旧 Stage04 runtime；请先执行 V2.39.6.3 rebuild")
    if str(shot.get("text_model_policy") or "") != "qwen3-32b":
        raise ValueError("当前镜头没有绑定 qwen3-32b semantic authority")
    representative = str(shot.get("representative_state") or "").strip()
    video_start = str(shot.get("video_start_state") or "").strip()
    video_end = str(shot.get("video_end_state") or "").strip()
    if str(shot.get("image_prompt") or "").strip() != representative:
        raise ValueError("image_prompt 与 representative_state 不闭合")
    if str(shot.get("video_start_prompt") or "").strip() != video_start:
        raise ValueError("video_start_prompt 与 video_start_state 不闭合")
    if str(shot.get("video_prompt") or "").strip() != f"起始状态：{video_start}\n结束状态：{video_end}":
        raise ValueError("video_prompt 与 start→end 状态不闭合")
    provenance = shot.get("source_provenance") or {}
    if not isinstance(provenance, dict) or not provenance.get("source_evidence"):
        raise ValueError("当前镜头缺少 Shot 级小说原文依据")
    for field in ("batch_audit", "narrative_audit", "scene_global_audit", "forward_overlap_audit"):
        audit = shot.get(field) or {}
        if not isinstance(audit, dict) or audit.get("valid") is not True:
            raise ValueError(f"当前镜头缺少持久化审计闭环：{field}")

def _studio_v2371_prompt_asset(project_id: str, shot: dict, kind: str) -> dict:
    _studio_v2371_require_strict_shot(shot)
    shot_id = str(shot.get("shot_id") or "")
    order = int(shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0)
    fingerprint = _studio_shot_contract_fingerprint(shot)
    provenance = shot.get("source_provenance") or {}
    specs = {
        "image": ("image_prompt", f"studio:shot:{shot_id}:stage04-image-prompt-v2",
                  "shot_image_prompt", f"镜头 {order:03d} · ④严格分镜代表画面 Prompt"),
        "video_start": ("video_start_prompt", f"studio:shot:{shot_id}:stage04-video-start-prompt-v2",
                        "shot_video_start_prompt", f"镜头 {order:03d} · ④严格分镜视频首帧 Prompt"),
        "video_motion": ("video_prompt", f"studio:shot:{shot_id}:stage04-video-motion-prompt-v2",
                         "shot_video_prompt", f"镜头 {order:03d} · ④严格分镜视频运动 Prompt"),
    }
    if kind not in specs:
        raise ValueError("未知 Stage04 Prompt 类型：" + str(kind))
    field, logical_key, role, name = specs[kind]
    content = str(shot.get(field) or "").strip()
    if not content:
        raise ValueError(f"镜头 {order:03d} 缺少 {field}")

    for current in director.production.list_assets(project_id, active_only=True):
        meta = current.get("metadata") or {}
        if (
            str(current.get("logical_key") or "") == logical_key
            and str(current.get("status") or "").lower() == "ready"
            and str(meta.get("shot_contract_fingerprint") or "") == fingerprint
        ):
            try:
                if director.production.read_text_asset(project_id, current["asset_id"]) == content:
                    return current
            except Exception:
                pass

    return director.production.create_text_asset(
        project_id, stage="make", skill="manju-studio-stage04-strict-contract",
        logical_key=logical_key, asset_role=role, name=name, content=content,
        asset_type="TEXT", extension=".txt",
        source={"type":"stage04_strict_contract","shot_id":shot_id,"kind":kind},
        parent_asset_ids=[],
        entity_ids=[str(shot.get("entity_id") or "")] if str(shot.get("entity_id") or "") else [],
        metadata={
            "creator_ui":"shot_driven","shot_id":shot_id,
            "scene_id":str(shot.get("scene_id") or ""),"global_order":order,
            "stage04_contract_version":"strict-shot-v2",
            "shot_contract_fingerprint":fingerprint,
            "source_evidence_ids":list(provenance.get("source_evidence_ids") or []),
            "source_evidence":list(provenance.get("source_evidence") or []),
        },
    )

def _studio_v2371_batch_schema() -> str:
    return (
        '{"shots":[{"title":"","duration_seconds":3.0,"summary":"",'
        '"composition":"","shot_size":"","camera":"","camera_move":"",'
        '"action":"","performance":"","environment":"","dialogue":"",'
        '"narration":"","sound":"","music":"","continuity":"",'
        '"representative_state":"","video_start_state":"","video_end_state":"",'
        '"image_prompt":"","video_start_prompt":"","video_prompt":"",'
        '"covered_beat_orders":[1],"source_evidence_ids":["E001"],'
        '"character_entity_ids":[],"prop_entity_ids":[]}]}'
    )

async def _studio_v2371_generate_batch(
    *, system_prompt: str, prompt: str,
    scene_index: int, scene_total: int, batch_index: int, batch_total: int,
) -> list[dict]:
    diagnostics = []
    attempts = (
        (0.15, ""),
        (0.03, "\n\nSTRICT_RETRY: 只返回合同 JSON；所有制作字段、Beat 映射和证据锚点必须显式填写。"),
    )
    for attempt, (temp, suffix) in enumerate(attempts, 1):
        try:
            raw, parsed, _ = await director._structured_json_call(
                phase="studio_stage04_strict_contract_qwen32b",
                messages=[{"role":"user","content":prompt + suffix}],
                system_prompt=system_prompt, temperature=temp, max_tokens=2200,
                contract=_studio_v2371_batch_schema(),
            )
            rows = parsed.get("shots") if isinstance(parsed, dict) else None
            if isinstance(rows, list) and rows:
                return rows
            diagnostics.append(f"attempt={attempt}: no shots raw_chars={len(str(raw or ''))}")
        except Exception as exc:
            diagnostics.append(f"attempt={attempt}: {type(exc).__name__}: {str(exc)[:260]}")
    raise RuntimeError(
        f"场景 {scene_index}/{scene_total} 批次 {batch_index+1}/{batch_total} 严格合同生成失败；"
        + " | ".join(diagnostics)
    )

def _studio_v2371_validate_rows(
    *, raw_rows: list[dict], compact_beats: list[dict],
    allowed_chars: set[str], allowed_props: set[str],
    anchors: list[dict], scene_id: str, episode_id: str,
) -> list[dict]:
    expected = {int(x.get("order") or 0) for x in compact_beats if int(x.get("order") or 0) > 0}
    amap = _studio_v2371_anchor_map(anchors)
    cleaned, covered = [], set()
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        summary, action = str(row.get("summary") or "").strip(), str(row.get("action") or "").strip()
        if not summary and not action:
            continue

        beat_orders = []
        for value in row.get("covered_beat_orders") or []:
            try:
                n = int(value)
            except Exception:
                continue
            if n > 0 and n not in beat_orders:
                beat_orders.append(n)
        if expected:
            if not beat_orders:
                raise RuntimeError("严格 Stage04：存在未显式绑定 Beat 的 Shot；拒绝数量兜底")
            illegal = set(beat_orders) - expected
            if illegal:
                raise RuntimeError("严格 Stage04：Shot 引用了当前批次以外 Beat：" + repr(sorted(illegal)))
            covered.update(beat_orders)

        evidence_ids, evidence_text = [], []
        for value in row.get("source_evidence_ids") or []:
            key = str(value or "").strip()
            if key and key in amap and key not in evidence_ids:
                evidence_ids.append(key)
                evidence_text.append(amap[key])
            if len(evidence_ids) >= 4:
                break
        if not evidence_ids:
            raise RuntimeError("严格 Stage04：Shot 未选择有效小说正文证据锚点")

        required = (
            "representative_state","video_start_state","video_end_state",
            "image_prompt","video_start_prompt","video_prompt",
        )
        missing = [k for k in required if not str(row.get(k) or "").strip()]
        if missing:
            raise RuntimeError("严格 Stage04：Shot 缺少制作状态/Prompt：" + ", ".join(missing))

        try:
            duration = float(row.get("duration_seconds") or 3.0)
        except Exception:
            duration = 3.0
        duration = max(0.8, min(20.0, duration))

        cleaned.append({
            "scene_id":scene_id,"episode_id":episode_id,
            "title":str(row.get("title") or "").strip(),
            "duration_seconds":duration,"summary":summary,
            "composition":str(row.get("composition") or "").strip(),
            "shot_size":str(row.get("shot_size") or "").strip(),
            "camera":str(row.get("camera") or "").strip(),
            "camera_move":str(row.get("camera_move") or "").strip(),
            "action":action,"performance":str(row.get("performance") or "").strip(),
            "environment":str(row.get("environment") or "").strip(),
            "dialogue":str(row.get("dialogue") or "").strip(),
            "narration":str(row.get("narration") or "").strip(),
            "sound":str(row.get("sound") or "").strip(),
            "music":str(row.get("music") or "").strip(),
            "continuity":str(row.get("continuity") or "").strip(),
            "representative_state":str(row.get("representative_state") or "").strip(),
            "video_start_state":str(row.get("video_start_state") or "").strip(),
            "video_end_state":str(row.get("video_end_state") or "").strip(),
            "image_prompt":str(row.get("image_prompt") or "").strip(),
            "video_start_prompt":str(row.get("video_start_prompt") or "").strip(),
            "video_prompt":str(row.get("video_prompt") or "").strip(),
            "covered_beat_orders":beat_orders,
            "source_evidence_ids":evidence_ids,"source_evidence":evidence_text,
            "character_entity_ids":_studio_v2371_clean_ids(row.get("character_entity_ids"), allowed_chars),
            "prop_entity_ids":_studio_v2371_clean_ids(row.get("prop_entity_ids"), allowed_props),
            "stage04_contract_version":"strict-shot-v2",
        })

    if not cleaned:
        raise RuntimeError("严格 Stage04：当前批次没有有效 Shot")
    if expected and covered != expected:
        raise RuntimeError("严格 Stage04：Beat 显式覆盖不完整；missing=" + repr(sorted(expected - covered)))
    return cleaned

async def _studio_v2371_audit_batch(
    *, source_window: str, compact_beats: list[dict], shots: list[dict],
) -> dict:
    audit_rows = [{
        "index":i+1,"title":row.get("title"),
        "covered_beat_orders":row.get("covered_beat_orders"),
        "summary":row.get("summary"),"action":row.get("action"),
        "representative_state":row.get("representative_state"),
        "video_start_state":row.get("video_start_state"),
        "video_end_state":row.get("video_end_state"),
        "source_evidence":row.get("source_evidence"),
        "character_entity_ids":row.get("character_entity_ids"),
        "prop_entity_ids":row.get("prop_entity_ids"),
    } for i,row in enumerate(shots)]
    system_prompt = (
        "你是正式分镜时间边界审计器，只审计不改写。检查：Beat 显式覆盖；"
        "镜头按原文时间单调前进；不提前消费后续 Beat；拆分镜头不重复播放已完成结果；"
        "video_start_state→representative_state→video_end_state 因果成立；"
        "representative_state 具有当前 Shot 的叙事信息，不能无依据退化为通用人物/物体肖像；"
        "人物和道具只在当前 Shot 实际可见时出现。只返回严格 JSON。"
    )
    prompt = (
        "=== ORIGINAL_SOURCE_WINDOW ===\n" + source_window
        + "\n\n=== BEATS ===\n" + _studio_json.dumps(compact_beats,ensure_ascii=False,separators=(",",":"))
        + "\n\n=== SHOTS ===\n" + _studio_json.dumps(audit_rows,ensure_ascii=False,separators=(",",":"))
    )
    _, audit, _ = await director._structured_json_call(
        phase="studio_stage04_strict_temporal_audit_qwen32b",
        messages=[{"role":"user","content":prompt}], system_prompt=system_prompt,
        temperature=0.0, max_tokens=700,
        contract=(
            '{"valid":true,"beat_coverage_ok":true,"temporal_monotonic":true,'
            '"no_future_event_preconsumption":true,"no_result_duplication":true,'
            '"state_order_valid":true,"entity_visibility_valid":true,"issues":[]}'
        ),
    )
    return audit if isinstance(audit, dict) else {}

def _studio_v2371_audit_ok(audit: dict) -> bool:
    keys = (
        "valid","beat_coverage_ok","temporal_monotonic",
        "no_future_event_preconsumption","no_result_duplication",
        "state_order_valid","entity_visibility_valid",
    )
    return isinstance(audit, dict) and all(audit.get(k) is True for k in keys)

_STUDIO_V2371_REBUILD_TASKS: dict[str, dict] = {}
_STUDIO_V23963_REBUILD_LOCKS: dict[str, asyncio.Lock] = {}
_STUDIO_V23963_ACTIVE_REBUILD_STATES = {
    "starting", "warming", "queued", "running", "repairing", "auditing", "persisting",
}


def _studio_v23963_stage04_task_path(project_id: str) -> Path:
    value = str(project_id or "").strip()
    if not value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in value):
        raise ValueError("invalid project_id for Stage04 task journal")
    return settings.data_dir / "stage04_rebuild_tasks" / f"{value}.json"


def _studio_v23963_persist_stage04_task(task: dict) -> None:
    path = _studio_v23963_stage04_task_path(str(task.get("project_id") or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _studio_v23963_load_stage04_task(project_id: str) -> dict:
    path = _studio_v23963_stage04_task_path(project_id)
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or str(value.get("project_id") or "") != str(project_id):
        raise RuntimeError("Stage04 task journal identity mismatch")
    return value


def _studio_v23963_recover_project(project_id: str) -> bool:
    runtime = globals().get("_stage04_v238_runtime")
    if runtime is None:
        return False
    return bool(runtime.recover_project_transaction(globals(), project_id))


def _studio_v23963_current_stage04_task(project_id: str, *, recover_orphan: bool = True) -> dict:
    memory = _STUDIO_V2371_REBUILD_TASKS.get(project_id) or {}
    durable = _studio_v23963_load_stage04_task(project_id)
    row = memory or durable
    status = str(row.get("status") or "").lower()
    if recover_orphan and status in _STUDIO_V23963_ACTIVE_REBUILD_STATES and not memory:
        recovered = _studio_v23963_recover_project(project_id)
        row = dict(row)
        row.update({
            "status": "failed",
            "message": "平台重启中断 Stage04 rebuild；canonical transaction 已恢复" if recovered else "平台重启中断 Stage04 rebuild",
            "error": "orphan Stage04 rebuild recovered after process restart",
            "updated_at": _studio_now(),
        })
        _studio_v23963_persist_stage04_task(row)
    return row


def _studio_v23963_assert_no_active_rebuild(project_id: str) -> None:
    row = _studio_v23963_current_stage04_task(project_id)
    if str(row.get("status") or "").lower() in _STUDIO_V23963_ACTIVE_REBUILD_STATES:
        raise RuntimeError("Stage04 canonical switch is active; production reads are temporarily blocked")

async def _studio_v2371_rebuild_stage04(project_id: str, task_id: str) -> None:
    task = _STUDIO_V2371_REBUILD_TASKS[project_id]
    try:
        project = director.get_project(project_id)
        state = story_continuity.load(project_id)
        scenes, active_episode = _studio_stage04_scope(state)
        if not scenes:
            raise RuntimeError("没有可用于重建正式分镜的 Scene")
        source_asset_id, source_text = _studio_stage04_full_source(project_id)
        upstream = _studio_stage04_upstream(project)
        all_shots, scene_stats = [], []

        for index, scene in enumerate(scenes, 1):
            task.update({
                "status":"running",
                "message":f"Qwen3-32B 正在重建严格分镜：场景 {index}/{len(scenes)}",
                "scene_done":index-1,"scene_total":len(scenes),
                "shots_done":len(all_shots),"updated_at":_studio_now(),
            })
            rows = await _studio_stage04_scene_shots(
                project_id=project_id,scene=scene,state=state,source_text=source_text,
                upstream=upstream,
                user_input="按 strict-shot-v2 重新建立正式制作合同；禁止沿用旧 Shot 的剧情压缩或结果态偏移。",
                scene_index=index,scene_total=len(scenes),
            )
            all_shots.extend(rows)
            scene_stats.append({
                "scene_id":str(scene.get("scene_id") or ""),"title":str(scene.get("title") or ""),
                "shot_count":len(rows),
                "beat_count":len(_studio_stage04_scene_beats(state,str(scene.get("scene_id") or ""))),
            })

        final_text = _studio_stage04_markdown(project, scenes, all_shots)
        if not final_text.strip():
            raise RuntimeError("严格详细分镜为空")

        entity_ids = []
        for shot in all_shots:
            for eid in [*(shot.get("character_entity_ids") or []),*(shot.get("prop_entity_ids") or [])]:
                if eid and eid not in entity_ids:
                    entity_ids.append(eid)

        asset = director.production.create_text_asset(
            project_id,stage="04",skill=_STUDIO_STAGE_SKILLS["04"],
            logical_key="studio:stage04:detailed-storyboard",asset_role="storyboard_master",
            name="完整详细分镜表 · strict-shot-v2",content=final_text,
            asset_type="TEXT",extension=".md",
            source={"type":"studio_stage04_strict_contract_rebuild","mode":"scene_by_scene","text_model_policy":"qwen3-32b"},
            parent_asset_ids=[source_asset_id] if source_asset_id else [],
            entity_ids=entity_ids,
            metadata={
                "studio_stage04_detailed":True,"stage04_contract_version":"strict-shot-v2",
                "text_model_policy":"qwen3-32b","scene_count":len(scenes),
                "shot_count":len(all_shots),"active_episode_id":active_episode,"scene_stats":scene_stats,
            },
        )

        formal_count = _studio_stage04_replace_formal_shots(project_id,state,scenes,all_shots)
        if formal_count != len(all_shots):
            raise RuntimeError(f"严格正式镜头写入不完整：generated={len(all_shots)}, formal={formal_count}")

        final_sha = _studio_v2371_hashlib.sha256(final_text.encode("utf-8")).hexdigest()
        state["storyboard_source_sha256"] = final_sha
        story_continuity.save(project_id,state)

        project = director.get_project(project_id)
        stage_state = project.setdefault("stage_state",{}).setdefault("04",{})
        pipeline = {
            "schema_version":"studio_stage04_strict_v2","stage04_contract_version":"strict-shot-v2",
            "text_model_policy":"qwen3-32b","ready":True,"coverage_ok":True,"confirmed":True,
            "asset_id":asset["asset_id"],"asset_sha256":final_sha,
            "scene_count":len(scenes),"covered_scene_count":len(scene_stats),
            "shot_count":len(all_shots),"formal_shot_count":formal_count,
            "active_episode_id":active_episode,"generated_at":_studio_now(),
            "confirmed_at":_studio_now(),"scene_stats":scene_stats,
        }
        handoff = final_text[:12000]
        completion = {
            "ready":True,"reason":"studio_stage04_strict_contract_complete",
            "missing_artifact_ids":[],"missing_requirement_ids":[],
            "required_artifact_ids":["studio_stage04_detailed_storyboard"],
        }
        stage_state["studio_stage04_pipeline"] = pipeline
        stage_state["handoff"] = handoff
        stage_state["stage_ready"] = True
        stage_state.setdefault("skill_runtime",{})["completion"] = completion
        project.setdefault("confirmed_outputs",{})["04"] = {
            "skill":_STUDIO_STAGE_SKILLS["04"],"handoff":handoff,
            "handoff_audit":{
                "valid":True,"provenance_verified":True,"contract_version":"strict-shot-v2",
                "source":"studio_stage04_strict_contract_rebuild","source_asset_id":asset["asset_id"],
                "source_sha256":final_sha,"scene_count":len(scenes),"shot_count":formal_count,
            },
            "completion":completion,"production_asset_ids":[asset["asset_id"]],
            "production_stage_status":director.production.stage_status(project_id,"04"),
            "studio_stage04_pipeline":pipeline,"confirmed_at":_studio_now(),
        }
        project["updated_at"] = _studio_now()
        director._save_project(project)

        task.update({
            "status":"completed",
            "message":f"严格分镜重建完成：{len(scenes)} 场 / {formal_count} 个正式镜头",
            "scene_done":len(scenes),"scene_total":len(scenes),
            "shots_done":formal_count,"formal_shots":formal_count,
            "asset_id":asset["asset_id"],"updated_at":_studio_now(),
        })
    except Exception as exc:
        task.update({
            "status":"failed","message":str(exc),
            "error":f"{type(exc).__name__}: {exc}","updated_at":_studio_now(),
        })

@app.post("/api/studio/projects/{project_id}/stage04/rebuild-production")
async def studio_rebuild_stage04_production(project_id: str, payload: dict | None = None) -> dict:
    lock = _STUDIO_V23963_REBUILD_LOCKS.setdefault(project_id, asyncio.Lock())
    async with lock:
        director.get_project(project_id)
        _studio_v23963_recover_project(project_id)
        current = _studio_v23963_current_stage04_task(project_id)
        if str(current.get("status") or "").lower() in _STUDIO_V23963_ACTIVE_REBUILD_STATES:
            raise HTTPException(status_code=409, detail="当前项目已有④严格分镜重建任务正在运行")
        active_candidates = [
            row for row in _wb_sync_candidates(project_id)
            if str(row.get("status") or "").lower() in {"queued", "running", "generating", "switching_gpu"}
        ]
        if active_candidates:
            raise HTTPException(status_code=409, detail="当前项目有图片/视频候选任务正在运行，不能切换 Stage04 canonical")
        task_id = "s04rebuild_" + _studio_v2371_secrets.token_hex(8)
        row = {
            "task_id": task_id, "project_id": project_id, "status": "starting",
            "message": "④严格分镜重建已保留执行权", "text_model_policy": "qwen3-32b",
            "stage04_contract_version": "strict-shot-v2",
            "runtime_version": "2.39.6.3-stage04-full-pipeline-preflight",
            "created_at": _studio_now(), "updated_at": _studio_now(),
        }
        _STUDIO_V2371_REBUILD_TASKS[project_id] = row
        _studio_v23963_persist_stage04_task(row)
        row.update({"status": "warming", "message": "正在验证 Qwen3-32B workspace", "updated_at": _studio_now()})
        _studio_v23963_persist_stage04_task(row)
        try:
            preflight = await _studio_v2396_prepare_stage04_qwen()
        except Exception as exc:
            row.update({
                "status": "failed", "message": f"Stage04 Qwen3-32B 运行时预检失败：{exc}",
                "error": f"{type(exc).__name__}: {exc}", "updated_at": _studio_now(),
            })
            _studio_v23963_persist_stage04_task(row)
            raise HTTPException(status_code=503, detail=row["message"]) from exc
        row.update({
            "status": "queued", "message": "④严格分镜重建已排队",
            "performance": dict(preflight.get("performance") or {}), "updated_at": _studio_now(),
            "runtime_contract": {
                key: preflight.get(key)
                for key in ("selected_model_id", "resolved_model", "models", "response_model")
            },
        })
        _studio_v23963_persist_stage04_task(row)
        _studio_v2371_asyncio.create_task(_studio_v2371_rebuild_stage04(project_id, task_id))
        return dict(row)

@app.get("/api/studio/projects/{project_id}/stage04/rebuild-production/status")
async def studio_rebuild_stage04_production_status(project_id: str) -> dict:
    director.get_project(project_id)
    return dict(_studio_v23963_current_stage04_task(project_id) or {
        "project_id":project_id,"status":"idle","message":"当前没有④严格分镜重建任务"
    })
# ===== /V2.37.1 STRICT STAGE04 CONTRACT =====


async def _studio_stage04_scene_shots(
    *, project_id: str, scene: dict, state: dict, source_text: str,
    upstream: dict, user_input: str, scene_index: int, scene_total: int,
) -> list[dict]:
    scene_id = str(scene.get("scene_id") or "")
    resolved = story_continuity.resolve_scene(project_id, scene_id)
    beats = _studio_stage04_scene_beats(state, scene_id)
    source = _studio_stage04_scene_source(scene, source_text)
    allowed_chars, allowed_props = _studio_stage04_allowed_ids(scene, resolved)

    entities = {
        str(x.get("entity_id") or ""):{
            "entity_id":str(x.get("entity_id") or ""),
            "entity_type":str(x.get("entity_type") or ""),
            "name":str(x.get("name") or ""),
        }
        for x in director.production.list_entities(project_id)
        if str(x.get("entity_id") or "")
    }
    scene_entities = [
        entities[eid] for eid in [*sorted(allowed_chars),*sorted(allowed_props)]
        if eid in entities
    ]
    entity_text = _studio_v2371_cut(_studio_json.dumps(scene_entities,ensure_ascii=False),700)
    resolved_text = _studio_v2371_cut(_studio_json.dumps({
        "location":resolved.get("location"),
        "characters":resolved.get("characters"),
        "props":resolved.get("props"),
        "scene_state":resolved.get("scene_state"),
    },ensure_ascii=False),760)
    character_anchor = _studio_v2371_cut(upstream.get("character_bible"),1100)
    visual_anchor = _studio_v2371_cut(upstream.get("visual_bible"),900)

    if beats:
        beat_batches = [beats[i:i+3] for i in range(0,len(beats),3)]
    else:
        estimated = max(1,(len(source)+1399)//1400) if source else 1
        beat_batches = [[] for _ in range(min(8,estimated))]

    system_prompt = (
        "你是正式短视频分镜导演。默认文本模型为 Qwen3-32B。"
        "小说正文和 Beat 是最高优先级事实。必须返回可直接进入制作的 Shot 合同。"
        "有 BEAT 时，每个 Shot 的 covered_beat_orders 必须非空且只能引用当前批次，"
        "禁止用镜头数量代替 Beat 显式映射。source_evidence_ids 必须来自正文锚点。"
        "人物/道具只填写当前 Shot 当前画面真实可见实体；不确定就留空，禁止 Scene 全量兜底。"
        "representative_state 是当前 Shot 最具叙事信息的单帧；过程 Shot 优先关键变化态，"
        "不得无依据退化为结果人物/物体通用肖像。"
        "video_start_state 是第一可见动作发生前或刚开始，video_end_state 是本 Shot 自己结束态；"
        "不得提前消费后续 Beat 或后续 Shot 的主要事件。"
        "image_prompt 只描述 representative_state；video_start_prompt 只描述 video_start_state；"
        "video_prompt 描述 video_start_state 到 video_end_state 的前向动作。"
        "同一事件拆成多个 Shot 时必须继续推进，不能重复播放已完成结果。"
        "只依据正文、Scene Fact、允许实体及已确认角色/视觉锚点；不得新增剧情事实。只返回严格 JSON。"
    )

    all_rows, seen = [], set()
    for batch_index,batch in enumerate(beat_batches):
        source_window = _studio_stage04_source_window(
            source,batch_index,len(beat_batches),max_chars=1500
        )
        anchors = _studio_v2371_evidence_anchors(source_window)
        if not anchors:
            raise RuntimeError(
                f"场景 {scene_index}/{scene_total} 批次 {batch_index+1} 无法建立正文证据锚点"
            )
        compact_beats = [{
            "order":int(x.get("order") or 0),
            "summary":str(x.get("summary") or "")[:260],
            "character_entity_ids":list(x.get("character_entity_ids") or []),
            "prop_entity_ids":list(x.get("prop_entity_ids") or []),
        } for x in batch]
        target = max(1,len(compact_beats)*2 if compact_beats else 3)
        base_prompt = (
            f"SCENE_PROGRESS={scene_index}/{scene_total}\n"
            f"BATCH_PROGRESS={batch_index+1}/{len(beat_batches)}\n"
            f"SCENE_ID={scene_id}\n"
            f"SCENE_TITLE={str(scene.get('title') or '')[:160]}\n"
            f"SCENE_SUMMARY={str(scene.get('summary') or '')[:340]}\n"
            f"TARGET_SHOTS≈{target}；按真实动作复杂度决定，不得合并掉独立剧情变化。\n\n"
            "=== ORIGINAL_SCENE_SOURCE_WINDOW ===\n" + (source_window or "<none>")
            + "\n\n=== SOURCE_EVIDENCE_ANCHORS ===\n"
            + _studio_json.dumps(anchors,ensure_ascii=False,separators=(",",":"))
            + "\n\n=== BEATS_THIS_BATCH ===\n"
            + _studio_json.dumps(compact_beats,ensure_ascii=False,separators=(",",":"))
            + "\n\n=== CONTINUITY ===\n" + resolved_text
            + "\n\n=== ALLOWED_ENTITIES ===\n" + entity_text
            + "\n\n=== CHARACTER_ANCHOR ===\n" + (character_anchor or "<none>")
            + "\n\n=== VISUAL_ANCHOR ===\n" + (visual_anchor or "<none>")
            + "\n\n=== USER_REQUIREMENT ===\n" + _studio_v2371_cut(user_input,300)
        )

        accepted, repair_issues, final_audit = None, "", None
        for _round in range(2):
            prompt = base_prompt
            if repair_issues:
                prompt += "\n\n=== PREVIOUS_AUDIT_ISSUES ===\n" + repair_issues + "\n请重新生成整个批次。"
            raw_rows = await _studio_v2371_generate_batch(
                system_prompt=system_prompt,prompt=prompt,
                scene_index=scene_index,scene_total=scene_total,
                batch_index=batch_index,batch_total=len(beat_batches),
            )
            rows = _studio_v2371_validate_rows(
                raw_rows=raw_rows,compact_beats=compact_beats,
                allowed_chars=allowed_chars,allowed_props=allowed_props,
                anchors=anchors,scene_id=scene_id,
                episode_id=str(scene.get("episode_id") or ""),
            )
            audit = await _studio_v2371_audit_batch(
                source_window=source_window,compact_beats=compact_beats,shots=rows
            )
            if _studio_v2371_audit_ok(audit):
                accepted, final_audit = rows, audit
                break
            repair_issues = _studio_json.dumps(audit.get("issues") or audit,ensure_ascii=False)

        if accepted is None:
            raise RuntimeError(
                f"场景 {scene_index}/{scene_total} 批次 {batch_index+1} "
                "两轮生成后仍未通过时间边界审计：" + repair_issues[:800]
            )

        for row in accepted:
            row["source_batch_index"] = batch_index + 1
            row["source_audit"] = final_audit or {}
            fp = _studio_v2371_re.sub(
                r"\s+","",
                (str(row.get("representative_state") or "")+"|"
                 +str(row.get("video_start_state") or "")+"|"
                 +str(row.get("video_end_state") or "")).lower()
            )[:700]
            if fp and fp in seen:
                raise RuntimeError("严格 Stage04：检测到重复状态 Shot；拒绝用重复镜头充当剧情覆盖")
            if fp:
                seen.add(fp)
            all_rows.append(row)

    if not all_rows:
        raise RuntimeError(f"场景 {scene_index}/{scene_total} 没有生成正式镜头")

    expected_all = {int(x.get("order") or 0) for x in beats if int(x.get("order") or 0)>0}
    covered_all = {
        int(order) for row in all_rows for order in (row.get("covered_beat_orders") or [])
        if int(order)>0
    }
    if expected_all and covered_all != expected_all:
        raise RuntimeError(
            f"场景 {scene_index}/{scene_total} Beat 显式覆盖不完整："
            f"missing={sorted(expected_all-covered_all)} unexpected={sorted(covered_all-expected_all)}"
        )

    for idx,row in enumerate(all_rows,1):
        row["local_order"] = idx
        if not str(row.get("title") or "").strip():
            row["title"] = f"{scene.get('title') or '场景'} · 镜头{idx}"
    return all_rows

def _studio_stage04_markdown(project: dict, scenes: list[dict], all_shots: list[dict]) -> str:
    by_scene = {}
    for shot in all_shots:
        by_scene.setdefault(str(shot.get("scene_id") or ""),[]).append(shot)
    total = sum(float(x.get("duration_seconds") or 0) for x in all_shots)
    lines = [
        "# 完整详细分镜表 · strict-shot-v2","",
        f"- 作品：{str(project.get('title') or '')}",
        f"- 场景数：{len(scenes)}",f"- 镜头数：{len(all_shots)}",
        f"- 镜头建议总时长：约 {total:.1f} 秒",
        "- 文本模型策略：Qwen3-32B",
        "- 合同：Beat 显式映射 + Shot 级小说证据 + representative/video_start/video_end 三状态","",
    ]
    global_no = 0
    for scene_index,scene in enumerate(scenes,1):
        scene_id = str(scene.get("scene_id") or "")
        rows = by_scene.get(scene_id) or []
        lines.extend([
            f"## 场景 {scene_index}：{str(scene.get('title') or scene_id)}","",
            "剧情摘要："+str(scene.get("summary") or ""),f"本场镜头：{len(rows)}","",
        ])
        for shot in rows:
            global_no += 1
            shot["global_order"] = global_no
            evidence = list(shot.get("source_evidence") or [])
            lines.extend([
                f"### 镜头 {global_no:03d}｜{shot.get('title') or ''}｜{float(shot.get('duration_seconds') or 0):.1f}s",
                "- Covered Beats："+_studio_json.dumps(shot.get("covered_beat_orders") or [],ensure_ascii=False),
                "- 画面动作："+str(shot.get("summary") or ""),
                "- 代表状态："+str(shot.get("representative_state") or ""),
                "- 视频起始状态："+str(shot.get("video_start_state") or ""),
                "- 视频结束状态："+str(shot.get("video_end_state") or ""),
                "- 构图："+str(shot.get("composition") or ""),
                "- 景别："+str(shot.get("shot_size") or ""),
                "- 机位："+str(shot.get("camera") or ""),
                "- 镜头运动："+str(shot.get("camera_move") or ""),
                "- 人物动作："+str(shot.get("action") or ""),
                "- 表演/表情："+str(shot.get("performance") or ""),
                "- 环境/光线："+str(shot.get("environment") or ""),
                "- 连续性："+str(shot.get("continuity") or ""),
                "- ④分镜代表画面 Prompt："+str(shot.get("image_prompt") or ""),
                "- ④视频首帧 Prompt："+str(shot.get("video_start_prompt") or ""),
                "- ④视频运动 Prompt："+str(shot.get("video_prompt") or ""),
                "- 原文依据："+(" / ".join(evidence) if evidence else "缺失"),"",
            ])
    return "\n".join(lines).strip()+"\n"

def _studio_stage04_replace_formal_shots(
    project_id: str, state: dict, scenes: list[dict], all_shots: list[dict],
) -> int:
    import secrets as _s04_secrets
    scope_ids = {str(x.get("scene_id") or "") for x in scenes}
    preserved = [x for x in (state.get("shots") or []) if str(x.get("scene_id") or "") not in scope_ids]
    scene_map = {str(x.get("scene_id") or ""):x for x in scenes}
    formal, local_counts = [], {}

    for global_index,shot in enumerate(all_shots,1):
        scene_id = str(shot.get("scene_id") or "")
        scene = scene_map.get(scene_id)
        if not scene:
            continue
        if str(shot.get("stage04_contract_version") or "") != "strict-shot-v2":
            raise RuntimeError("拒绝写入非 strict-shot-v2 的正式 Shot")
        if not list(shot.get("source_evidence") or []):
            raise RuntimeError("拒绝写入缺少 Shot 级小说原文依据的正式 Shot")

        local_order = local_counts.get(scene_id,0)+1
        local_counts[scene_id] = local_order
        scene_sequence = int(scene.get("sequence") or 0)
        sequence = scene_sequence*1000 + local_order
        logical_key = f"continuity:shot:{scene_sequence:06d}:{local_order:04d}"
        provenance = {
            "contract_version":"strict-shot-v2","text_model_policy":"qwen3-32b",
            "scene_id":scene_id,"source_start":scene.get("source_start"),
            "source_end":scene.get("source_end"),
            "source_batch_index":int(shot.get("source_batch_index") or 0),
            "covered_beat_orders":list(shot.get("covered_beat_orders") or []),
            "source_evidence_ids":list(shot.get("source_evidence_ids") or []),
            "source_evidence":list(shot.get("source_evidence") or []),
        }
        continuity_meta = {
            "scene_id":scene_id,"order":local_order,"global_order":global_index,
            "duration_seconds":shot.get("duration_seconds"),"shot_size":shot.get("shot_size"),
            "camera":shot.get("camera"),"camera_move":shot.get("camera_move"),
            "action":shot.get("action"),"performance":shot.get("performance"),
            "dialogue":shot.get("dialogue"),"continuity":shot.get("continuity"),
            "representative_state":shot.get("representative_state"),
            "video_start_state":shot.get("video_start_state"),
            "video_end_state":shot.get("video_end_state"),
            "image_prompt":shot.get("image_prompt"),
            "video_start_prompt":shot.get("video_start_prompt"),
            "video_prompt":shot.get("video_prompt"),
            "covered_beat_orders":list(shot.get("covered_beat_orders") or []),
            "source_provenance":provenance,
            "stage04_contract_version":"strict-shot-v2",
        }
        entity = director.production.create_entity(
            project_id,entity_type="shot",
            name=f"镜头 {global_index:03d} · {shot.get('title') or scene.get('title') or ''}",
            logical_key=logical_key,stage="04",skill=_STUDIO_STAGE_SKILLS["04"],
            metadata={"continuity":continuity_meta},
        )
        try:
            director.production.add_relation(
                project_id,source_id=str(scene.get("entity_id") or ""),
                target_id=entity["entity_id"],relation_type="contains",
                metadata={"source":"studio_stage04_strict_v2"},
            )
        except Exception:
            pass

        for eid in [*(shot.get("character_entity_ids") or []),*(shot.get("prop_entity_ids") or [])]:
            try:
                director.production.add_relation(
                    project_id,source_id=str(eid),target_id=entity["entity_id"],
                    relation_type="appears_in",metadata={"source":"studio_stage04_strict_v2"},
                )
            except Exception:
                pass

        formal.append({
            "shot_id":"shot_"+_s04_secrets.token_hex(8),
            "entity_id":entity["entity_id"],"scene_id":scene_id,
            "episode_id":str(scene.get("episode_id") or ""),
            "title":str(shot.get("title") or ""),"order":local_order,
            "global_order":global_index,"sequence":sequence,
            "duration_seconds":shot.get("duration_seconds"),
            "summary":str(shot.get("summary") or ""),
            "composition":str(shot.get("composition") or ""),
            "shot_size":str(shot.get("shot_size") or ""),
            "camera":str(shot.get("camera") or ""),
            "camera_move":str(shot.get("camera_move") or ""),
            "action":str(shot.get("action") or ""),
            "performance":str(shot.get("performance") or ""),
            "environment":str(shot.get("environment") or ""),
            "dialogue":str(shot.get("dialogue") or ""),
            "narration":str(shot.get("narration") or ""),
            "sound":str(shot.get("sound") or ""),
            "music":str(shot.get("music") or ""),
            "continuity":str(shot.get("continuity") or ""),
            "representative_state":str(shot.get("representative_state") or ""),
            "video_start_state":str(shot.get("video_start_state") or ""),
            "video_end_state":str(shot.get("video_end_state") or ""),
            "image_prompt":str(shot.get("image_prompt") or ""),
            "video_start_prompt":str(shot.get("video_start_prompt") or ""),
            "video_prompt":str(shot.get("video_prompt") or ""),
            "covered_beat_orders":list(shot.get("covered_beat_orders") or []),
            "source_provenance":provenance,
            "character_entity_ids":list(shot.get("character_entity_ids") or []),
            "prop_entity_ids":list(shot.get("prop_entity_ids") or []),
            "stage04_contract_version":"strict-shot-v2",
            "text_model_policy":"qwen3-32b","provisional":False,
        })
    state["shots"] = preserved + formal
    return len(formal)

async def _studio_stage04_generate_detailed(project_id: str, user_input: str, job: dict) -> None:
    import hashlib as _s04_hash
    project = director.get_project(project_id)
    if project.get("status") != "active" or str(project.get("current_stage") or "") != "04":
        raise RuntimeError("当前项目不在分镜阶段")

    state = await _studio_stage04_wait_continuity(project_id, job)
    scenes, active_episode = _studio_stage04_scope(state)
    if not scenes:
        raise RuntimeError("连续性引擎没有得到可用于分镜的 Scene；不能用粗略整章摘要代替")

    source_asset_id, source_text = _studio_stage04_full_source(project_id)
    upstream = _studio_stage04_upstream(project)
    all_shots = []
    scene_stats = []
    for index, scene in enumerate(scenes, 1):
        job.update({
            "status": "running",
            "message": f"正在生成详细分镜：场景 {index}/{len(scenes)} · {str(scene.get('title') or '')}",
            "storyboard_progress": {"scene_done": index - 1, "scene_total": len(scenes), "shots_done": len(all_shots)},
            "updated_at": _studio_now(),
        })
        _studio_save_job(job)
        rows = await _studio_stage04_scene_shots(
            project_id=project_id, scene=scene, state=state, source_text=source_text,
            upstream=upstream, user_input=user_input, scene_index=index, scene_total=len(scenes),
        )
        all_shots.extend(rows)
        scene_stats.append({
            "scene_id": str(scene.get("scene_id") or ""),
            "title": str(scene.get("title") or ""),
            "shot_count": len(rows),
            "beat_count": len(_studio_stage04_scene_beats(state, str(scene.get("scene_id") or ""))),
        })

    if len(scene_stats) != len(scenes) or any(int(x.get("shot_count") or 0) <= 0 for x in scene_stats):
        raise RuntimeError("详细分镜覆盖校验失败：存在未生成镜头的场景")

    final_text = _studio_stage04_markdown(project, scenes, all_shots)
    if not final_text.strip():
        raise RuntimeError("完整详细分镜为空")

    for asset in director.production.list_assets(project_id, stage="04", active_only=True):
        if str(asset.get("logical_key") or "") == "studio:stage04:detailed-storyboard":
            continue
        if str(asset.get("asset_type") or "").upper() not in {"TEXT", "STRUCTURED_DATA", "COLLECTION"}:
            continue
        try:
            director.production.archive_asset(project_id, asset["asset_id"])
        except Exception:
            pass

    parent_ids = [source_asset_id] if source_asset_id else []
    entity_ids = []
    for shot in all_shots:
        for eid in [*(shot.get("character_entity_ids") or []), *(shot.get("prop_entity_ids") or [])]:
            if eid and eid not in entity_ids:
                entity_ids.append(eid)

    asset = director.production.create_text_asset(
        project_id, stage="04", skill=_STUDIO_STAGE_SKILLS["04"],
        logical_key="studio:stage04:detailed-storyboard", asset_role="storyboard_master",
        name="完整详细分镜表", content=final_text, asset_type="TEXT", extension=".md",
        source={"type": "studio_stage04_detailed_pipeline", "mode": "scene_by_scene"},
        parent_asset_ids=parent_ids, entity_ids=entity_ids,
        metadata={
            "studio_stage04_detailed": True, "scene_count": len(scenes),
            "covered_scene_count": len(scene_stats), "shot_count": len(all_shots),
            "active_episode_id": active_episode, "scene_stats": scene_stats,
        },
    )

    formal_count = _studio_stage04_replace_formal_shots(project_id, state, scenes, all_shots)
    if formal_count != len(all_shots):
        raise RuntimeError(f"正式镜头写入不完整：generated={len(all_shots)}, formal={formal_count}")

    final_sha = _s04_hash.sha256(final_text.encode("utf-8")).hexdigest()
    state["storyboard_source_sha256"] = final_sha
    story_continuity.save(project_id, state)

    project = director.get_project(project_id)
    stage_state = project.setdefault("stage_state", {}).setdefault("04", {})
    stage_state["studio_stage04_pipeline"] = {
        "schema_version": "studio_stage04_detailed_v1", "ready": True, "coverage_ok": True,
        "asset_id": asset["asset_id"], "asset_sha256": final_sha,
        "scene_count": len(scenes), "covered_scene_count": len(scene_stats),
        "shot_count": len(all_shots), "formal_shot_count": formal_count,
        "active_episode_id": active_episode, "generated_at": _studio_now(), "scene_stats": scene_stats,
    }
    stage_state["stage_ready"] = False
    stage_state["handoff"] = ""
    project["updated_at"] = _studio_now()
    director._save_project(project)

    job.update({
        "status": "review",
        "message": f"详细分镜已生成：{len(scenes)} 场 / {len(all_shots)} 镜头。检查后确认进入制作。",
        "storyboard_progress": {"scene_done": len(scenes), "scene_total": len(scenes), "shots_done": len(all_shots), "complete": True},
        "storyboard_asset_id": asset["asset_id"], "updated_at": _studio_now(),
    })
    _studio_save_job(job)


async def _studio_stage04_finalize(project_id: str, job: dict) -> None:
    import hashlib as _s04_hash
    project = director.get_project(project_id)
    if project.get("status") == "completed":
        job.update({"status": "advanced", "message": "分镜已经确认，当前已进入制作。", "auto_confirmed": True, "updated_at": _studio_now()})
        _studio_save_job(job)
        return
    if str(project.get("current_stage") or "") != "04":
        raise RuntimeError("当前项目不在可确认的分镜阶段")

    stage_state = (project.get("stage_state") or {}).get("04") or {}
    pipeline = stage_state.get("studio_stage04_pipeline") or {}
    if not (pipeline.get("ready") is True and pipeline.get("coverage_ok") is True):
        raise RuntimeError("详细分镜尚未通过场景覆盖校验，请先生成详细分镜")
    if (
        str(pipeline.get("stage04_contract_version") or "") != "strict-shot-v2"
        or str(pipeline.get("runtime_version") or "") != "2.39.6.3-stage04-full-pipeline-preflight"
        or str(pipeline.get("text_model_policy") or "") != "qwen3-32b"
    ):
        raise RuntimeError("当前 Stage04 canonical 不是 V2.39.6.3 Qwen strict-shot-v2 合同；请使用专用 rebuild")

    scene_count = int(pipeline.get("scene_count") or 0)
    covered = int(pipeline.get("covered_scene_count") or 0)
    shot_count = int(pipeline.get("shot_count") or 0)
    formal_count = int(pipeline.get("formal_shot_count") or 0)
    if scene_count <= 0 or covered != scene_count:
        raise RuntimeError(f"不能确认：场景覆盖不完整 {covered}/{scene_count}")
    if shot_count <= 0 or formal_count != shot_count:
        raise RuntimeError(f"不能确认：正式镜头写入不完整 {formal_count}/{shot_count}")

    asset_id = str(pipeline.get("asset_id") or "")
    asset = director.production.get_asset(project_id, asset_id)
    if asset.get("active") is False or str(asset.get("status") or "").lower() != "ready" or str(asset.get("dependency_state") or "") == "stale":
        raise RuntimeError("不能确认：完整详细分镜资产不是当前 READY 版本")

    storyboard = director.production.read_text_asset(project_id, asset_id, max_chars=2_000_000)
    actual_sha = _s04_hash.sha256(storyboard.encode("utf-8")).hexdigest()
    expected_sha = str(pipeline.get("asset_sha256") or "")
    if expected_sha and actual_sha != expected_sha:
        raise RuntimeError("不能确认：详细分镜资产内容校验发生变化")

    handoff = storyboard[:12000]
    audit = {
        "valid": True, "provenance_verified": True, "contract_version": "verbatim_evidence_v1",
        "source": "studio_stage04_detailed_pipeline", "source_asset_id": asset_id,
        "source_sha256": actual_sha, "evidence_chars": len(handoff),
        "scene_count": scene_count, "shot_count": shot_count,
    }
    completion = {
        "ready": True, "reason": "studio_stage04_detailed_pipeline_complete",
        "missing_artifact_ids": [], "missing_requirement_ids": [],
        "required_artifact_ids": ["studio_stage04_detailed_storyboard"],
    }
    stage_state["handoff"] = handoff
    stage_state["last_handoff_audit"] = audit
    stage_state["stage_ready"] = True
    stage_state.setdefault("skill_runtime", {})["completion"] = completion
    stage_state["studio_stage04_pipeline"] = {**pipeline, "confirmed": True, "confirmed_at": _studio_now()}

    project.setdefault("confirmed_outputs", {})["04"] = {
        "skill": _STUDIO_STAGE_SKILLS["04"], "handoff": handoff,
        "handoff_audit": audit, "completion": completion,
        "production_asset_ids": [asset_id],
        "production_stage_status": director.production.stage_status(project_id, "04"),
        "studio_stage04_pipeline": stage_state["studio_stage04_pipeline"],
        "confirmed_at": _studio_now(),
    }
    completed = project.setdefault("completed_stages", [])
    if "04" not in completed:
        completed.append("04")
    project["status"] = "completed"
    project["updated_at"] = _studio_now()
    director._save_project(project)

    job.update({
        "status": "advanced",
        "message": f"分镜已确认：{scene_count} 场 / {shot_count} 镜头。现在进入制作。",
        "confirmed_stage": "04", "next_stage": "make", "auto_confirmed": True,
        "updated_at": _studio_now(),
    })
    _studio_save_job(job)

# ===== /V2.35.7 DETAILED STORYBOARD PIPELINE =====


async def _studio_run_stage_job(
    *,
    job_id: str,
    project_id: str,
    user_input: str,
    max_turns: int,
) -> None:
    job = _studio_load_job(job_id)
    job.update({"status": "running", "updated_at": _studio_now(), "message": "正在执行当前创作阶段"})
    _studio_save_job(job)
    ready_hit = False
    explicit_approval = False
    stage = ""
    character_mode: dict = {}
    try:
        project = director.get_project(project_id)
        if project.get("status") != "active":
            job.update({"status": "review", "message": "核心 Skill 阶段已完成", "updated_at": _studio_now()})
            _studio_save_job(job)
            return
        stage = str(project.get("current_stage") or "")
        skill_name = _STUDIO_STAGE_SKILLS.get(stage, "")
        if not skill_name:
            raise RuntimeError("当前项目阶段无对应生产 Skill")

        raw_input = str(user_input or "").strip()
        normalized_input = _studio_re.sub(
            r"[\s，。！？!?、；;：:（）()【】\[\]<>《》“”‘’]+",
            "",
            raw_input,
        ).lower()
        explicit_approval = normalized_input in {
            "通过", "确认", "继续", "下一步", "确认通过",
            "通过继续", "继续下一步", "ok", "okay",
        }

        # V2.39.6.3: non-approval Stage04 work must use the dedicated rebuild
        # endpoint.  The historical generator bypasses current lineage/audits.
        if stage == "04":
            if explicit_approval:
                await _studio_stage04_finalize(project_id, job)
            else:
                raise RuntimeError("Stage04 generation is available only through /stage04/rebuild-production")
            return

        if stage == "02":
            # Stage02 now has its own character-design Skill.  The semantic
            # identity classifier is only a UI/product hint and must never be
            # allowed to block the real production call if llama.cpp has a
            # transient error.
            try:
                async with gpu.use(GPUOwner.gemma):
                    character_mode = await _studio_character_role_mode(project_id)
            except Exception as classifier_exc:
                character_mode = {
                    "schema_version": "character_role_mode_v1",
                    "character_source": "unknown",
                    "identity_preservation_required": False,
                    "reference_image_required": False,
                    "reference_image_optional": True,
                    "reason": "身份路由器临时不可用；由角色设计 Skill 根据项目事实自行判断必要输入",
                    "decision_mode": "classifier_unavailable_non_blocking",
                    "classifier_error": repr(classifier_exc)[:800],
                }
            job["character_mode"] = character_mode
            job["message"] = (
                "正在生成角色；人物身份参考图为必要输入"
                if character_mode.get("reference_image_required")
                else "正在生成角色；角色设定将直接依据已确认剧本生成"
            )
            job["updated_at"] = _studio_now()
            _studio_save_job(job)

        text = raw_input
        longform_profile = {}
        if stage == "01":
            longform_profile = await _studio_prepare_stage01_context(project_id, job)

        if stage == "02":
            # Keep the route metadata compact.  The Stage02 Skill itself owns
            # character production and identity-reference policy.
            mode_json = _studio_json.dumps(character_mode, ensure_ascii=False)
            mode_instruction = f"""STAGE02_PRODUCT_ROUTE={mode_json}

当前阶段是通用角色设计，不是真人写真工具。
直接依据已经确认的剧本、角色实体和连续性事实执行 ai-studio-character-design。
若项目事实未明确要求保持某个现实人物身份，则参考图不得成为阻断条件。
"""
            if not text or explicit_approval:
                text = mode_instruction + "\n继续当前角色设计阶段。"
            else:
                text = mode_instruction + "\n\nUSER_SUPPLEMENT:\n" + text

        if not text:
            source_assets = [
                a for a in director.production.list_assets(project_id, stage="01", active_only=True)
                if str(a.get("asset_role") or "") == "source_brief"
                and str(a.get("status") or "").lower() == "ready"
            ]
            if stage == "01" and source_assets:
                continuity_text = ""
                try:
                    continuity_text = story_continuity.episode_context(project_id, max_chars=9000)
                except Exception:
                    continuity_text = ""
                if continuity_text:
                    text = (
                        "这是当前作品中由连续性引擎解析出的当前章节/集事实。"
                        "严格基于这些事实执行当前 Skill，不推断未提供的后续剧情。\n\n"
                        + continuity_text
                    )
                else:
                    if longform_profile.get("longform"):
                        raise RuntimeError("长章节连续性已准备但章节事实包为空；停止执行，避免用截断原文生成")
                    text = director.production.read_text_asset(
                        project_id, source_assets[-1]["asset_id"], max_chars=12000
                    )
            else:
                text = "基于当前项目已经确认的上游成果，直接执行当前生产 Skill 的当前阶段。"

        turns: list[dict] = list(job.get("turns") or [])

        # Stage02 is deliberately click-bounded: one model production call per
        # user action.  This prevents a character bible from being appended to
        # history repeatedly by an automatic loop.  The first click produces
        # the bible; an explicit approval click closes the one-step stage.
        turn_limit = 1 if stage in {"02", "03", "04"} else max_turns

        last_fingerprint = ""
        repeated = 0
        for turn_index in range(turn_limit):
            current = director.get_project(project_id)
            if str(current.get("current_stage") or "") != stage:
                job.update({"status": "review", "message": "阶段已经变化，后台任务停止", "updated_at": _studio_now()})
                _studio_save_job(job)
                break

            async with gpu.use(GPUOwner.gemma):
                result = await director.message(project_id, text)
                decision = None
                state = result.get("control") or {}
                completion = (result.get("skill_runtime") or {}).get("completion") or {}
                media_missing = _studio_contract_missing_media(result)
                content = str(result.get("content") or "")
                turns.append({
                    "index": len(turns) + 1,
                    "stage": stage,
                    "content": content,
                    "internal_step": str(state.get("internal_step") or ""),
                    "next_expected_action": str(state.get("next_expected_action") or ""),
                    "completion_ready": bool(completion.get("ready")),
                    "control_action": str((result.get("control_event") or {}).get("action") or ""),
                    "character_mode": character_mode if stage == "02" else {},
                    "created_at": _studio_now(),
                })
                job["turns"] = turns
                job["turn_count"] = len(turns)
                job["last_content"] = content
                job["updated_at"] = _studio_now()
                _studio_save_job(job)

                if bool(completion.get("ready")) or bool(state.get("stage_ready")):
                    ready_hit = True
                    job.update({
                        "status": "review",
                        "message": "本阶段已生成完成，请检查结果后确认进入下一阶段。",
                        "reason": str(completion.get("reason") or ""),
                        "updated_at": _studio_now(),
                    })
                    _studio_save_job(job)
                    break

                if media_missing:
                    if not (stage == "02" and character_mode.get("reference_image_required") is False):
                        job.update({
                            "status": "media_required",
                            "message": "当前 Skill 需要真实媒体资产后才能完成。请到当前作品制作区补充必要媒体。",
                            "missing_media": media_missing,
                            "updated_at": _studio_now(),
                        })
                        _studio_save_job(job)
                        break

                # Stage02/03 are explicit review stages. One user action may
                # perform at most one production call. After the artifact is
                # produced, the UI offers one approval action instead of
                # invoking the same producer again.
                if stage in {"02", "03", "04"}:
                    if stage == "02":
                        review_message = (
                            "角色阶段尚未完成，请继续生成；完成后才能进入视觉。"
                            if not explicit_approval
                            else "已执行角色阶段确认，请检查阶段完成状态。"
                        )
                    elif stage == "03":
                        review_message = (
                            "视觉阶段尚未完成，请继续生成；完成后才能进入分镜。"
                            if not explicit_approval
                            else "已执行视觉阶段确认，请检查阶段完成状态。"
                        )
                    else:
                        review_message = (
                            "完整分镜已生成，请检查；确认后进入制作。"
                            if not explicit_approval
                            else "已执行分镜阶段确认，请检查核心创作完成状态。"
                        )
                    job.update({
                        "status": "review",
                        "message": review_message,
                        "reason": str(completion.get("reason") or ""),
                        "updated_at": _studio_now(),
                    })
                    _studio_save_job(job)
                    break

                if explicit_approval:
                    decision = {
                        "action": "auto_continue",
                        "reason": "用户已明确批准当前成果",
                        "decision_mode": "explicit_approval",
                    }
                else:
                    decision = await _studio_progress_decision(
                        skill_name=skill_name,
                        result=result,
                        character_mode=None,
                    )

            if decision.get("action") == "needs_user":
                job.update({
                    "status": "input_required",
                    "message": "这里确实需要你的新信息或创作选择。",
                    "reason": str(decision.get("reason") or ""),
                    "updated_at": _studio_now(),
                })
                _studio_save_job(job)
                break

            fingerprint = (
                str(state.get("internal_step") or "") + "\n" + content[-900:]
            ).strip()
            if fingerprint and fingerprint == last_fingerprint:
                repeated += 1
            else:
                repeated = 0
            last_fingerprint = fingerprint
            if repeated >= 2:
                job.update({
                    "status": "review_attention",
                    "message": "当前 Skill 连续返回同一内部步骤；已停止重复执行，没有强行越级。",
                    "reason": "stage_transition_repeat_guard",
                    "updated_at": _studio_now(),
                })
                _studio_save_job(job)
                break
            text = "通过"
        else:
            if stage not in {"02", "03", "04"}:
                job.update({
                    "status": "review_attention",
                    "message": f"已连续执行 {max_turns} 个内部步骤，为避免无限循环已暂停，请检查当前结果后继续。",
                    "updated_at": _studio_now(),
                })
                _studio_save_job(job)

        if ready_hit and explicit_approval:
            try:
                confirmed = await studio_confirm_stage(project_id)
                after = confirmed.get("project") if isinstance(confirmed, dict) else {}
                after = after if isinstance(after, dict) else {}
                next_stage = str(after.get("current_stage") or "")
                if after.get("status") == "completed":
                    message = "已确认分镜阶段，核心创作流程完成；下一步进入制作。"
                else:
                    label = _STUDIO_STAGE_LABELS.get(next_stage, next_stage or "下一阶段")
                    message = f"已确认当前阶段，已自动进入{label}。"
                job.update({
                    "status": "advanced",
                    "message": message,
                    "confirmed_stage": stage,
                    "next_stage": next_stage,
                    "auto_confirmed": True,
                    "updated_at": _studio_now(),
                })
                _studio_save_job(job)
            except Exception as exc:
                job.update({
                    "status": "review",
                    "message": "当前阶段已经完成，但自动确认未成功；请使用页面主按钮确认进入下一阶段。",
                    "auto_confirmed": False,
                    "auto_confirm_error": repr(exc),
                    "updated_at": _studio_now(),
                })
                _studio_save_job(job)
    except Exception as exc:
        job.update({
            "status": "failed",
            "message": "当前阶段执行失败",
            "error": repr(exc),
            "updated_at": _studio_now(),
        })
        _studio_save_job(job)
    finally:
        _STUDIO_TASKS.pop(job_id, None)




def _studio_video_edit_job_path(job_id: str) -> _StudioPath:
    if not str(job_id).startswith("vedit_"):
        raise ValueError("非法 video edit job_id")
    return _STUDIO_VIDEO_EDIT_ROOT / f"{job_id}.json"


def _studio_save_video_edit_job(job: dict) -> dict:
    path = _studio_video_edit_job_path(str(job["job_id"]))
    temp = path.with_suffix(".tmp")
    temp.write_text(_studio_json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return job


def _studio_load_video_edit_job(job_id: str) -> dict:
    path = _studio_video_edit_job_path(job_id)
    if not path.is_file():
        raise FileNotFoundError(f"视频编辑任务不存在：{job_id}")
    return _studio_json.loads(path.read_text(encoding="utf-8"))


def _studio_latest_video_edit_job(project_id: str) -> dict | None:
    rows: list[dict] = []
    for path in _STUDIO_VIDEO_EDIT_ROOT.glob("vedit_*.json"):
        try:
            row = _studio_json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(row.get("project_id") or "") == project_id:
            rows.append(row)
    rows.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    if not rows:
        return None
    row = rows[0]
    if row.get("status") in {"queued", "running"}:
        task = _STUDIO_VIDEO_EDIT_TASKS.get(str(row.get("job_id") or ""))
        if task is None or task.done():
            row["status"] = "interrupted"
            row["message"] = "平台曾重启；视频编辑任务已中断，可重新提交。"
            row["updated_at"] = _studio_now()
            _studio_save_video_edit_job(row)
    return row


def _studio_asset_media(
    project_id: str,
    asset_id: str,
    allowed: set[str],
    label: str,
) -> tuple[dict, str, _StudioPath]:
    _wb_validate_media_asset(project_id, asset_id, allowed, label)
    item = director.production.get_asset(project_id, asset_id)
    url = director.production.asset_url(project_id, asset_id)
    if not url:
        raise ValueError(f"{label}没有可用文件")
    path = assets.resolve_asset_url(url)
    if not path.is_file():
        raise FileNotFoundError(f"{label}文件不存在：{url}")
    return item, url, path


def _studio_task_output(task: dict) -> tuple[str, _StudioPath]:
    outputs = [str(x) for x in (task.get("output_files") or []) if str(x).strip()]
    if not outputs:
        raise RuntimeError("生产任务没有输出文件")
    url = outputs[0]
    try:
        path = assets.resolve_asset_url(url)
    except Exception:
        path = _StudioPath(url)
    if not path.is_file():
        raise FileNotFoundError(f"生产任务输出文件不存在：{url}")
    return url, path


async def _studio_wait_task(task: dict, *, timeout_seconds: int = 7200) -> dict:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("生产任务缺少 task_id")
    deadline = _studio_asyncio.get_running_loop().time() + max(60, int(timeout_seconds))
    while _studio_asyncio.get_running_loop().time() < deadline:
        current = _production_task_payload(task_id)
        status = _wb_task_status(current)
        if status == "completed":
            return current
        if status in {"failed", "cancelled", "canceled"}:
            raise RuntimeError(str(current.get("error") or current.get("message") or f"任务失败：{status}"))
        await _studio_asyncio.sleep(2)
    raise RuntimeError(f"生产任务超时：{task_id}")


async def _studio_run_facefusion_raw(
    *,
    processor: str,
    target_url: str,
    source_url: str = "",
    params: dict | None = None,
    authorized_adult: bool = False,
) -> tuple[dict, str, _StudioPath]:
    caps = await facefusion.capabilities()
    spec = caps.get(processor) if isinstance(caps, dict) else None
    if not isinstance(spec, dict) or spec.get("available") is False:
        raise RuntimeError(f"FaceFusion 处理器不可用：{processor}")
    form = {
        "processor": processor,
        "params_json": _studio_json.dumps(params or {}, ensure_ascii=False),
        "authorized_adult": "true" if authorized_adult else "false",
        "target_asset_url": target_url,
        "source_asset_url": source_url,
    }
    task = await _production_submit_existing_api("/api/facefusion/tasks", data=form)
    task = await _studio_wait_task(task)
    output_url, output_path = _studio_task_output(task)
    return task, output_url, output_path


def _studio_ffprobe_video(path: _StudioPath) -> dict:
    if not _studio_shutil.which("ffprobe"):
        raise RuntimeError("系统未检测到 ffprobe")
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    p = _studio_subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffprobe 读取视频失败：" + (p.stderr or p.stdout)[-1200:])
    body = _studio_json.loads(p.stdout or "{}")
    stream = (body.get("streams") or [{}])[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    duration = float((body.get("format") or {}).get("duration") or 0.0)
    if width <= 0 or height <= 0 or duration <= 0:
        raise RuntimeError("无法读取目标视频尺寸或时长")
    return {"width": width, "height": height, "duration": duration}


def _studio_compose_background(
    *,
    foreground: _StudioPath,
    background: _StudioPath,
    original_video: _StudioPath,
    output: _StudioPath,
    background_type: str,
    key_color: str = "0xFF00FF",
    similarity: float = 0.12,
    blend: float = 0.04,
) -> None:
    if not _studio_shutil.which("ffmpeg"):
        raise RuntimeError("系统未检测到 ffmpeg")
    info = _studio_ffprobe_video(foreground)
    w, h, duration = info["width"], info["height"], info["duration"]
    cmd = ["ffmpeg", "-y", "-i", str(foreground)]
    if background_type == "IMAGE":
        cmd += ["-loop", "1", "-i", str(background)]
    else:
        cmd += ["-stream_loop", "-1", "-i", str(background)]
    cmd += ["-i", str(original_video)]
    filt = (
        f"[0:v]chromakey={key_color}:{similarity:.4f}:{blend:.4f},format=rgba[fg];"
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}[bg];"
        "[bg][fg]overlay=0:0:format=auto[v]"
    )
    cmd += [
        "-filter_complex", filt,
        "-map", "[v]", "-map", "2:a?",
        "-t", f"{duration:.6f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(output),
    ]
    p = _studio_subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not output.is_file() or output.stat().st_size < 1024:
        raise RuntimeError("背景合成失败：" + (p.stderr or p.stdout)[-2200:])


def _studio_remux_original_audio(
    *,
    edited_video: Path,
    original_video: Path,
    output: Path,
) -> None:
    if not _studio_shutil.which("ffmpeg"):
        raise RuntimeError("系统未检测到 ffmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    attempts = [
        [
            "ffmpeg", "-y", "-i", str(edited_video), "-i", str(original_video),
            "-map", "0:v:0", "-map", "1:a?", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-shortest",
            "-movflags", "+faststart", str(output),
        ],
        [
            "ffmpeg", "-y", "-i", str(edited_video), "-i", str(original_video),
            "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264",
            "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest",
            "-movflags", "+faststart", str(output),
        ],
    ]
    last = ""
    for cmd in attempts:
        p = _studio_subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0 and output.is_file() and output.stat().st_size >= 1024:
            return
        last = (p.stderr or p.stdout)[-2200:]
        try:
            output.unlink(missing_ok=True)
        except Exception:
            pass
    raise RuntimeError("原音轨合成失败：" + last)


async def _studio_video_edit_job(job_id: str) -> None:
    job = _studio_load_video_edit_job(job_id)
    job.update({"status": "running", "message": "正在处理视频", "updated_at": _studio_now()})
    _studio_save_video_edit_job(job)
    try:
        project_id = str(job["project_id"])
        mode = str(job["mode"])
        target_id = str(job["target_asset_id"])
        target_asset, target_url, target_path = _studio_asset_media(
            project_id, target_id, {"VIDEO"}, "目标视频"
        )
        current_url = target_url
        current_path = target_path
        task_ids: list[str] = []

        if mode in {"person", "person_background"}:
            source_id = str(job.get("person_source_asset_id") or "")
            _, source_url, _ = _studio_asset_media(
                project_id, source_id, {"IMAGE"}, "人物参考"
            )
            person_params = job.get("person_params") if isinstance(job.get("person_params"), dict) else {}
            _, current_url, current_path = await _studio_run_facefusion_raw(
                processor="face_swapper",
                target_url=current_url,
                source_url=source_url,
                params=person_params,
                authorized_adult=True,
            )
            if mode == "person":
                out_dir = settings.data_dir / "studio_video_edits" / project_id
                out_dir.mkdir(parents=True, exist_ok=True)
                audio_output = out_dir / ("person_" + _studio_datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + _studio_secrets.token_hex(4) + ".mp4")
                await _studio_asyncio.to_thread(
                    _studio_remux_original_audio,
                    edited_video=current_path,
                    original_video=target_path,
                    output=audio_output,
                )
                current_path = audio_output
                current_url = "/files/" + audio_output.relative_to(settings.data_dir).as_posix()
            job["message"] = "人物替换完成" if mode == "person" else "人物替换完成，正在分离背景"
            job["updated_at"] = _studio_now()
            _studio_save_video_edit_job(job)

        if mode in {"background", "person_background"}:
            remover_params = job.get("background_remover_params") if isinstance(job.get("background_remover_params"), dict) else {}
            _, keyed_url, keyed_path = await _studio_run_facefusion_raw(
                processor="background_remover",
                target_url=current_url,
                params=remover_params,
                authorized_adult=False,
            )
            job["message"] = "前景分离完成，正在合成新背景"
            job["updated_at"] = _studio_now()
            _studio_save_video_edit_job(job)
            bg_id = str(job.get("background_asset_id") or "")
            bg_asset, _, bg_path = _studio_asset_media(
                project_id, bg_id, {"IMAGE", "VIDEO"}, "背景素材"
            )
            out_dir = settings.data_dir / "studio_video_edits" / project_id
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / ("edit_" + _studio_datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + _studio_secrets.token_hex(4) + ".mp4")
            await _studio_asyncio.to_thread(
                _studio_compose_background,
                foreground=keyed_path,
                background=bg_path,
                original_video=target_path,
                output=output_path,
                background_type=str(bg_asset.get("asset_type") or "IMAGE").upper(),
                key_color=str(job.get("key_color") or "0xFF00FF"),
                similarity=float(job.get("key_similarity") or 0.12),
                blend=float(job.get("key_blend") or 0.04),
            )
            current_path = output_path
            current_url = "/files/" + output_path.relative_to(settings.data_dir).as_posix()

        parent_ids = [target_id]
        if mode in {"person", "person_background"}:
            parent_ids.append(str(job.get("person_source_asset_id") or ""))
        if mode in {"background", "person_background"}:
            parent_ids.append(str(job.get("background_asset_id") or ""))
        parent_ids = list(dict.fromkeys(x for x in parent_ids if x))

        result = director.production.register_existing_file(
            project_id,
            stage="edit",
            skill="manju-studio-video-edit",
            logical_key="studio:video_edit:" + _studio_secrets.token_hex(10),
            asset_type="VIDEO",
            asset_role="video_edit_result",
            name=str(job.get("name") or "视频编辑结果"),
            url=current_url,
            source={"type": "studio_video_edit", "job_id": job_id, "mode": mode},
            parent_asset_ids=parent_ids,
            entity_ids=[],
            metadata={
                "edit_mode": mode,
                "review_status": "pending",
                "person_processor": "face_swapper" if mode in {"person", "person_background"} else "",
                "background_processor": "background_remover" if mode in {"background", "person_background"} else "",
            },
        )
        for parent_id in parent_ids:
            director.production.add_relation(
                project_id,
                source_id=parent_id,
                target_id=result["asset_id"],
                relation_type="input_to",
                metadata={"operation": "studio_video_edit", "mode": mode},
            )
        job.update({
            "status": "completed",
            "message": "视频编辑完成，请预览后采用或丢弃",
            "review_status": "pending",
            "result_asset_id": result["asset_id"],
            "result_url": current_url,
            "updated_at": _studio_now(),
        })
        _studio_save_video_edit_job(job)
    except Exception as exc:
        job.update({
            "status": "failed",
            "message": "视频编辑失败",
            "error": f"{type(exc).__name__}: {exc}",
            "updated_at": _studio_now(),
        })
        _studio_save_video_edit_job(job)
    finally:
        _STUDIO_VIDEO_EDIT_TASKS.pop(job_id, None)


def _studio_text_assets(project_id: str) -> list[dict]:
    return [
        a for a in director.production.list_assets(project_id, active_only=True)
        if str(a.get("asset_type") or "").upper() in {"TEXT", "STRUCTURED_DATA", "FILE"}
        and str(a.get("status") or "").lower() == "ready"
        and str(a.get("dependency_state") or "").lower() != "stale"
        and director.production.asset_url(project_id, a["asset_id"])
    ]


@app.get("/studio")
async def manju_studio_page() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "studio.html")


@app.get("/api/studio/catalog")
async def studio_catalog() -> dict:
    return {
        "version": "2.35.7b-detailed-storyboard-pipeline",
        "stages": _STUDIO_STAGE_LABELS,
        "skills_hidden_from_creator_ui": True,
        "internal_stage_autopilot": True,
        "manual_stage_confirmation": True,
        "media_candidate_confirmation": True,
        "entity_identity_resolution": "deterministic_alias_then_structured_semantic",
        "video_edit_modes": ["person", "background", "person_background"],
        "ffmpeg": bool(_studio_shutil.which("ffmpeg")),
        "ffprobe": bool(_studio_shutil.which("ffprobe")),
        "runtime": {
            "native_plan": "soft_fallback",
            "context_budget": "global_hierarchical_v1",
        },
    }


_STUDIO_PROGRESS_LABELS = {
    "01": "剧本", "02": "角色", "03": "视觉",
    "04": "分镜", "05": "制作", "06": "成片",
}


def _studio_progress_eta(created_at: str, percent: int) -> int | None:
    if not created_at or percent <= 0 or percent >= 100:
        return None
    try:
        started = _studio_datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=_studio_timezone.utc)
        elapsed = max(0.0, (_studio_datetime.now(_studio_timezone.utc) - started).total_seconds())
        if elapsed < 2:
            return None
        remaining = elapsed * (100 - percent) / percent
        return max(1, min(int(remaining), 7 * 24 * 3600))
    except Exception:
        return None


def _studio_progress_row(
    stage_id: str, *, status: str = "pending", current_step: int = 0,
    total_steps: int = 1, percent: int = 0,
    completed_items: list[str] | None = None, current_item: str = "尚未开始",
    eta_seconds: int | None = None, source: str = "derived",
) -> StageProgress:
    total = max(1, int(total_steps or 1))
    step = max(0, min(int(current_step or 0), total))
    return {
        "stage_id": stage_id,
        "stage_name": _STUDIO_PROGRESS_LABELS[stage_id],
        "status": str(status or "pending"),
        "current_step": step,
        "total_steps": total,
        "percent": max(0, min(100, int(percent or 0))),
        "completed_items": [str(x) for x in (completed_items or []) if str(x).strip()],
        "current_item": str(current_item or "处理中"),
        "eta_seconds": eta_seconds,
        "source": str(source or "derived"),
    }


def _studio_core_stage_progress(
    project: dict, stage_id: str, current_job: dict | None,
) -> StageProgress:
    completed_stages = {str(x) for x in (project.get("completed_stages") or [])}
    confirmed = (project.get("confirmed_outputs") or {}).get(stage_id) or {}
    state = (project.get("stage_state") or {}).get(stage_id) or {}
    runtime = state.get("skill_runtime") or {}
    completion = runtime.get("completion") or {}
    artifact_registry = runtime.get("artifact_registry") or {}
    requirement_registry = runtime.get("requirement_registry") or {}
    required_artifacts = [str(x) for x in (completion.get("required_artifact_ids") or []) if str(x)]
    missing_artifacts = {str(x) for x in (completion.get("missing_artifact_ids") or [])}
    active_requirements = [str(x) for x in (completion.get("active_requirement_ids") or []) if str(x)]
    missing_requirements = {str(x) for x in (completion.get("missing_requirement_ids") or [])}
    completed_items = [
        "产物 · " + aid for aid in required_artifacts
        if aid not in missing_artifacts or bool((artifact_registry.get(aid) or {}).get("verified"))
    ]
    completed_items.extend(
        "规则 · " + rid for rid in active_requirements
        if rid not in missing_requirements or bool((requirement_registry.get(rid) or {}).get("verified"))
    )
    native_done = bool(completion.get("native_terminal"))
    if native_done:
        completed_items.append("Skill 原生流程")

    if stage_id in completed_stages:
        if not completed_items:
            completed_items = ["阶段成果已确认"]
        return _studio_progress_row(
            stage_id, status="completed", current_step=len(completed_items),
            total_steps=len(completed_items), percent=100,
            completed_items=completed_items, current_item="阶段已完成",
            source="confirmed_outputs" if confirmed else "completed_stages",
        )

    current_stage = str(project.get("current_stage") or "")
    if current_stage != stage_id:
        return _studio_progress_row(stage_id)

    job = current_job if str((current_job or {}).get("stage") or "") == stage_id else {}
    job_status = str(job.get("status") or "").lower()
    total = len(required_artifacts) + len(active_requirements) + 1
    done = len(completed_items)
    if total <= 1 and not required_artifacts and not active_requirements:
        turns = max(0, int(job.get("turn_count") or 0))
        done = turns
        total = max(1, turns + (0 if completion.get("ready") else 1))
        completed_items = [f"模型步骤 {index}" for index in range(1, turns + 1)]
    if completion.get("ready") is True:
        done = total
    percent = 100 if completion.get("ready") is True else int(done * 100 / max(total, 1))
    if job_status in {"queued", "running"} and percent == 0:
        percent = 5
    status = "ready" if completion.get("ready") is True else (
        "running" if job_status in {"queued", "running"} else
        "blocked" if job_status in {"input_required", "media_required", "failed"} else
        "waiting"
    )
    current_item = (
        str(job.get("message") or "").strip()
        or str(state.get("next_expected_action") or "").strip()
        or str(state.get("internal_step") or "").strip()
        or str(completion.get("reason") or "").strip()
        or "处理中"
    )
    current_step = total if completion.get("ready") is True else min(total, done + 1)
    return _studio_progress_row(
        stage_id, status=status, current_step=current_step, total_steps=total,
        percent=percent, completed_items=completed_items, current_item=current_item,
        eta_seconds=_studio_progress_eta(str(job.get("created_at") or ""), percent)
        if job_status in {"queued", "running"} else None,
        source="job+skill_runtime",
    )


def _studio_stage04_progress(
    project: dict, current_job: dict | None,
) -> StageProgress:
    base = _studio_core_stage_progress(project, "04", current_job)
    if base["status"] == "completed":
        return base
    task = _studio_v23963_current_stage04_task(
        str(project.get("project_id") or ""), recover_orphan=False,
    )
    task_status = str(task.get("status") or "").lower()
    if task_status not in {"starting", "warming", "queued", "running", "completed", "failed"}:
        pipeline = ((project.get("stage_state") or {}).get("04") or {}).get("studio_stage04_pipeline") or {}
        if pipeline.get("ready") is True:
            base.update({
                "status": "ready", "percent": 95,
                "current_item": "分镜已生成，等待确认进入制作",
                "source": "stage04_pipeline",
            })
        return base
    total = max(1, int(task.get("scene_total") or 1))
    done = max(0, min(int(task.get("scene_done") or 0), total))
    if task_status == "completed":
        done = total
    percent = 100 if task_status == "completed" else int(done * 100 / total)
    if task_status in {"starting", "warming", "queued", "running"} and percent == 0:
        percent = 5
    return _studio_progress_row(
        "04", status="ready" if task_status == "completed" else (
            "failed" if task_status == "failed" else "running"
        ),
        current_step=total if task_status == "completed" else min(total, done + 1),
        total_steps=total, percent=percent,
        completed_items=[f"场景 {index}" for index in range(1, done + 1)],
        current_item="分镜生成完成，等待确认进入制作" if task_status == "completed"
        else str(task.get("message") or "正在处理分镜"),
        eta_seconds=_studio_progress_eta(str(task.get("created_at") or ""), percent)
        if task_status in {"starting", "warming", "queued", "running"} else None,
        source="stage04_rebuild_task",
    )


def _studio_stage05_progress(
    project_id: str, assets_snapshot: list[dict], candidates: list[dict],
) -> StageProgress:
    try:
        continuity = story_continuity.load(project_id)
    except Exception:
        continuity = {}
    shots = [
        row for row in (continuity.get("shots") or [])
        if not bool(row.get("provisional")) and str(row.get("shot_id") or "")
    ]
    shots.sort(key=lambda row: int(row.get("global_order") or row.get("sequence") or row.get("order") or 0))
    shot_ids = {str(row.get("shot_id")) for row in shots}
    ready_video_ids: set[str] = set()
    assets_by_id = {str(row.get("asset_id") or ""): row for row in assets_snapshot}
    for item in assets_snapshot:
        if not _studio_asset_is_current(item):
            continue
        if str(item.get("asset_role") or "") not in {"shot_clip", "shot_video_processed"}:
            continue
        meta = item.get("metadata") or {}
        source = item.get("source") or {}
        shot_id = str(meta.get("shot_id") or source.get("shot_id") or "")
        if shot_id in shot_ids:
            ready_video_ids.add(shot_id)
    def candidate_shot_id(row: dict) -> str:
        target = assets_by_id.get(str(row.get("target_asset_id") or "")) or {}
        return str(
            (target.get("metadata") or {}).get("shot_id")
            or (target.get("source") or {}).get("shot_id")
            or ""
        )

    active_statuses = {"queued", "switching_gpu", "running", "generating"}
    active_candidates = [
        row for row in candidates
        if str(row.get("status") or "").lower() in active_statuses
        and candidate_shot_id(row) in shot_ids
    ]
    pending_review = [
        row for row in candidates
        if str(row.get("status") or "").lower() == "completed"
        and not row.get("confirmed_asset_id")
        and candidate_shot_id(row) in shot_ids
    ]
    total = max(1, len(shots))
    done = len(ready_video_ids)
    active = active_candidates[0] if active_candidates else {}
    task_percent = max(0, min(100, int(active.get("progress") or 0)))
    percent = 100 if shots and done >= len(shots) else int(
        min(total, done + task_percent / 100) * 100 / total
    )
    completed_items = []
    for index, shot in enumerate(shots, 1):
        if str(shot.get("shot_id") or "") in ready_video_ids:
            completed_items.append(f"镜头 {index:03d} · 视频已采用")
    if shots and done >= len(shots):
        status, current_item = "completed", "全部正式镜头视频已采用"
    elif active:
        target = assets_by_id.get(str(active.get("target_asset_id") or "")) or {}
        target_shot = (target.get("metadata") or {}).get("shot_id") or (target.get("source") or {}).get("shot_id")
        status = "running"
        current_item = str(active.get("message") or "").strip() or (
            f"正在制作镜头 {target_shot}" if target_shot else "正在制作镜头素材"
        )
    elif pending_review:
        status, current_item = "waiting", f"{len(pending_review)} 个制作候选等待采用"
    elif shots:
        status, current_item = "waiting", f"等待制作剩余 {len(shots) - done} 个镜头视频"
    else:
        status, current_item = "pending", "等待正式分镜"
    return _studio_progress_row(
        "05", status=status, current_step=total if percent == 100 else min(total, done + 1),
        total_steps=total, percent=percent, completed_items=completed_items,
        current_item=current_item,
        eta_seconds=_studio_progress_eta(str(active.get("created_at") or ""), percent)
        if active else None,
        source="production_assets+candidates",
    )


def _studio_stage06_progress(
    assets_snapshot: list[dict], stage05: StageProgress,
) -> StageProgress:
    finals = [
        row for row in assets_snapshot
        if _studio_asset_is_current(row) and str(row.get("asset_role") or "") == "final_cut"
    ]
    if finals:
        latest = sorted(finals, key=lambda row: str(row.get("updated_at") or ""))[-1]
        return _studio_progress_row(
            "06", status="completed", current_step=1, total_steps=1, percent=100,
            completed_items=[str(latest.get("name") or "最终成片")],
            current_item="成片已生成", source="final_cut_asset",
        )
    ready = stage05["percent"] == 100
    return _studio_progress_row(
        "06", status="ready" if ready else "pending", current_step=1 if ready else 0,
        total_steps=1, percent=0, completed_items=[],
        current_item="等待生成成片" if ready else "等待制作完成",
        source="final_cut_asset",
    )


def _studio_stage_progress_snapshot(
    project: dict, current_job: dict | None,
    assets_snapshot: list[dict], candidates: list[dict],
) -> dict:
    stages: list[StageProgress] = [
        _studio_core_stage_progress(project, stage_id, current_job)
        for stage_id in ("01", "02", "03")
    ]
    stages.append(_studio_stage04_progress(project, current_job))
    stage05 = _studio_stage05_progress(str(project.get("project_id") or ""), assets_snapshot, candidates)
    stages.append(stage05)
    stage06 = _studio_stage06_progress(assets_snapshot, stage05)
    stages.append(stage06)
    if str(project.get("status") or "") == "active":
        current_stage = str(project.get("current_stage") or "01")
    elif stage06["status"] == "completed" or stage05["status"] == "completed":
        current_stage = "06"
    else:
        current_stage = "05"
    return {
        "schema_version": "stage-progress-v1",
        "current_stage": current_stage,
        "stages": stages,
    }


def _studio_stage_progress_fallback(project: dict) -> dict:
    completed = {str(x) for x in (project.get("completed_stages") or [])}
    current_stage = str(project.get("current_stage") or "01")
    if str(project.get("status") or "") != "active":
        current_stage = "05"
    stages = []
    for stage_id in ("01", "02", "03", "04", "05", "06"):
        is_completed = stage_id in completed
        is_current = stage_id == current_stage
        stages.append(_studio_progress_row(
            stage_id,
            status="completed" if is_completed else ("running" if is_current else "pending"),
            current_step=1 if is_completed or is_current else 0,
            total_steps=1,
            percent=100 if is_completed else 0,
            completed_items=["阶段已完成"] if is_completed else [],
            current_item="阶段已完成" if is_completed else ("处理中" if is_current else "尚未开始"),
            source="fallback",
        ))
    return {
        "schema_version": "stage-progress-v1",
        "current_stage": current_stage,
        "stages": stages,
    }


@app.get("/api/studio/projects")
async def studio_projects() -> list[dict]:
    rows = director.list_projects()
    out: list[dict] = []
    for p in rows:
        stage = str(p.get("current_stage") or "")
        out.append({
            "project_id": p.get("project_id"),
            "title": p.get("title"),
            "status": p.get("status"),
            "current_stage": stage,
            "current_label": _STUDIO_STAGE_LABELS.get(stage, "成片制作"),
            "completed_stages": p.get("completed_stages") or [],
            "updated_at": p.get("updated_at"),
        })
    return out


@app.post("/api/studio/projects")
async def studio_create_project(payload: dict) -> dict:
    title = str(payload.get("title") or "").strip()
    source_text = str(payload.get("source_text") or "").strip()
    source_type = str(payload.get("source_type") or "idea").strip().lower()
    if not title:
        raise HTTPException(status_code=400, detail="作品名称不能为空")
    if not source_text:
        raise HTTPException(status_code=400, detail="创作内容不能为空")
    if len(source_text) > 2_000_000:
        raise HTTPException(status_code=400, detail="单个作品源文本最多 200 万字符")
    project = director.create_project(title)
    project_id = project["project_id"]

    director.production.create_text_asset(
        project_id,
        stage="source",
        skill="manju-studio",
        logical_key="studio:source:full",
        asset_role="source_full",
        name="作品完整源文本",
        content=source_text,
        asset_type="TEXT",
        extension=".md",
        source={"type": "studio_source", "source_type": source_type},
        metadata={
            "source_type": source_type,
            "creator_ui": "manju_studio",
            "char_count": len(source_text),
            "full_source": True,
        },
    )

    if len(source_text) <= 12000:
        brief = source_text
    else:
        brief = (
            f"这是一个长篇作品项目，完整源文本已经作为项目正式资产保存，"
            f"共 {len(source_text)} 字符。系统不会把整章直接塞进一次模型提示词；"
            "会先按文本片段解析成章节、场景、人物、地点和道具，再生成有界章节事实包。"
            "创作阶段只消费已经完成的分片事实，不得把未读取部分当成已知事实。"
        )
    director.production.create_text_asset(
        project_id,
        stage="01",
        skill=_STUDIO_STAGE_SKILLS["01"],
        logical_key="studio:source:brief",
        asset_role="source_brief",
        name="当前创作入口",
        content=brief,
        asset_type="TEXT",
        extension=".md",
        source={"type": "studio_source", "source_type": source_type},
        metadata={
            "source_type": source_type,
            "creator_ui": "manju_studio",
            "full_source_asset": "studio:source:full",
        },
    )
    _studio_schedule_continuity(project_id, force=True)
    return director.get_project(project_id)


@app.get("/api/studio/projects/{project_id}")
async def studio_project_snapshot(project_id: str) -> dict:
    try:
        _studio_v23963_recover_project(project_id)
        project = director.get_project(project_id)
        try:
            director.refresh_production_completion(project_id)
        except Exception:
            pass
        graph = director.production.ensure_project(project_id, str(project.get("title") or ""))
        candidates = _wb_sync_candidates(project_id)
        assets_snapshot = director.production.list_assets(project_id)
        try:
            face_caps = await facefusion.capabilities()
        except Exception as exc:
            face_caps = {"_error": {"available": False, "message": str(exc)}}
        project = director.get_project(project_id)
        stage = str(project.get("current_stage") or "")
        last_result = ""
        for row in reversed(project.get("history") or []):
            if row.get("role") == "assistant" and str(row.get("stage") or "") == stage:
                last_result = str(row.get("content") or "")
                break
        current_job = _studio_active_job(project_id)
        if (
            current_job
            and project.get("status") == "active"
            and str(current_job.get("stage") or "") != stage
        ):
            current_job = None
        try:
            stage_progress = _studio_stage_progress_snapshot(
                project, current_job, assets_snapshot, candidates,
            )
        except Exception:
            logger.exception("StageProgress display mapping failed: %s", project_id)
            stage_progress = _studio_stage_progress_fallback(project)
        try:
            _studio_schedule_continuity(project_id)
            continuity_snapshot = story_continuity.compact_snapshot(project_id)
        except Exception as exc:
            continuity_snapshot = {
                "analysis": {
                    "status": "failed",
                    "message": "连续性状态读取失败",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                "episodes": [], "scenes": [], "shots": [], "entities": [],
                "overrides": [], "active_episode_id": "",
            }
        return {
            "project": project,
            "assets": assets_snapshot,
            "entities": director.production.list_entities(project_id),
            "relations": list(graph.get("relations") or []),
            "candidates": candidates,
            "active_job": current_job,
            "stage_progress": stage_progress,
            "video_edit_job": _studio_latest_video_edit_job(project_id),
            "facefusion_capabilities": face_caps,
            "last_result": last_result,
            "continuity": continuity_snapshot,
            "source_profile": _studio_source_profile(project_id),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/studio/projects/{project_id}/run-stage")
async def studio_run_stage(project_id: str, payload: dict) -> dict:
    try:
        project = director.get_project(project_id)
        if project.get("status") != "active":
            raise ValueError("核心 Skill 阶段已经完成，可继续制作画面、视频和成片")
        stage = str(project.get("current_stage") or "")
        raw = str(payload.get("input") or "").strip()
        normalized = _studio_re.sub(
            r"[\s，。！？!?、；;：:（）()【】\[\]<>《》“”‘’]+", "", raw
        ).lower()
        explicit_approval = normalized in {
            "通过", "确认", "继续", "下一步", "确认通过",
            "通过继续", "继续下一步", "ok", "okay",
        }
        if stage in {"02", "03"} and explicit_approval:
            return await studio_confirm_stage(project_id)
        if stage == "04":
            if not explicit_approval:
                raise RuntimeError("Stage04 generation is available only through /stage04/rebuild-production")
        active = _studio_active_job(project_id)
        if active and active.get("status") in {"queued", "running"}:
            raise RuntimeError("当前阶段已有后台任务正在执行")
        max_turns = max(1, min(24, int(payload.get("max_turns") or 16)))
        job_id = "stjob_" + _studio_secrets.token_hex(10)
        job = {
            "job_id": job_id,
            "project_id": project_id,
            "stage": str(project.get("current_stage") or ""),
            "status": "queued",
            "turn_count": 0,
            "turns": [],
            "message": "等待执行",
            "created_at": _studio_now(),
            "updated_at": _studio_now(),
        }
        _studio_save_job(job)
        task = _studio_asyncio.create_task(_studio_run_stage_job(
            job_id=job_id,
            project_id=project_id,
            user_input=str(payload.get("input") or ""),
            max_turns=max_turns,
        ))
        _STUDIO_TASKS[job_id] = task
        return {"job": job, "background": True}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/studio/jobs/{job_id}")
async def studio_job(job_id: str) -> dict:
    try:
        return _studio_load_job(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/studio/projects/{project_id}/confirm-stage")
async def studio_confirm_stage(project_id: str) -> dict:
    try:
        director.refresh_production_completion(project_id)
        project = director.get_project(project_id)
        if project.get("status") != "active":
            return {"project": project, "already_complete": True}
        stage = str(project.get("current_stage") or "")
        state = (project.get("stage_state") or {}).get(stage, {}) or {}
        completion = ((state.get("skill_runtime") or {}).get("completion") or {})
        if completion.get("ready") is not True:
            raise RuntimeError("当前阶段还没有完成，请先生成本阶段或补齐真实媒体资产：" + str(completion.get("reason") or ""))
        if stage not in {"02", "03"} and not str(state.get("handoff") or "").strip():
            async with gpu.use(GPUOwner.gemma):
                await director.message(
                    project_id,
                    "当前阶段已经完成所有真实交付。请严格按当前生产 Skill 做最终收口和 handoff；只引用已经实际形成并确认的成果，不补造资产，然后结束本阶段。",
                )
            director.refresh_production_completion(project_id)
        result = await director.confirm_stage(project_id)
        return {"project": result, "confirmed_stage": stage}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _studio_new_target(
    project_id: str,
    *,
    asset_type: str,
    name: str,
    role: str,
    stage: str,
    skill: str,
    parent_asset_ids: list[str],
) -> dict:
    logical = "studio:" + asset_type.lower() + ":" + _studio_secrets.token_hex(8)
    return director.production.declare_asset(
        project_id,
        stage=stage,
        skill=skill,
        logical_key=logical,
        asset_type=asset_type,
        asset_role=role,
        name=name,
        status="planned",
        source={"type": "studio_product"},
        parent_asset_ids=parent_asset_ids,
        entity_ids=[],
        metadata={"creator_ui": "manju_studio"},
    )


# ===== V2.36.1 E2E SHOT CHAIN =====

_STUDIO_SHOT_CANONICAL_ROLES = {
    "shot_keyframe": "keyframe",
    "shot_video_start_frame": "video-start-frame",
    "shot_clip": "video",
    "shot_image_processed": "keyframe-processed",
    "shot_video_processed": "video-processed",
}


def _studio_shot_asset_id(asset: dict) -> str:
    meta = asset.get("metadata") or {}
    source = asset.get("source") or {}
    return str(meta.get("shot_id") or source.get("shot_id") or "")


def _studio_shot_canonical_key(shot_id: str, asset_role: str) -> str:
    suffix = _STUDIO_SHOT_CANONICAL_ROLES.get(str(asset_role or ""), "")
    return f"studio:shot:{shot_id}:{suffix}" if shot_id and suffix else ""


def _studio_shot_canonical_key_for_target(target: dict) -> str:
    meta = target.get("metadata") or {}
    explicit = str(meta.get("canonical_logical_key") or "").strip()
    if explicit:
        return explicit
    return _studio_shot_canonical_key(
        _studio_shot_asset_id(target), str(target.get("asset_role") or "")
    )


def _studio_asset_is_current(asset: dict | None) -> bool:
    if not isinstance(asset, dict):
        return False
    return (
        bool(asset.get("active", True))
        and str(asset.get("status") or "").lower() == "ready"
        and str(asset.get("dependency_state") or "").lower() != "stale"
    )


def _studio_asset_ancestors(project_id: str, asset_id: str, limit: int = 300) -> set[str]:
    rows = director.production.list_assets(project_id)
    by_id = {str(a.get("asset_id") or ""): a for a in rows}
    seen: set[str] = set()
    stack = [str(asset_id or "")]
    steps = 0
    while stack and steps < limit:
        steps += 1
        current = stack.pop()
        if not current or current in seen:
            continue
        seen.add(current)
        item = by_id.get(current) or {}
        for parent in item.get("parent_asset_ids") or []:
            parent = str(parent or "").strip()
            if parent and parent not in seen:
                stack.append(parent)
    seen.discard(str(asset_id or ""))
    return seen


def _studio_current_role_asset(project_id: str, shot_id: str, role: str) -> dict | None:
    rows = [
        a for a in director.production.list_assets(project_id, active_only=True)
        if _studio_asset_is_current(a)
        and _studio_shot_asset_id(a) == str(shot_id or "")
        and str(a.get("asset_role") or "") == role
    ]
    if not rows:
        return None
    canonical = _studio_shot_canonical_key(shot_id, role)
    canonical_rows = [a for a in rows if str(a.get("logical_key") or "") == canonical]
    pool = canonical_rows or rows
    pool.sort(key=lambda a: (int(a.get("version") or 0), str(a.get("updated_at") or "")))
    return pool[-1]


def _studio_rep_keyframe_valid(asset: dict | None) -> bool:
    if not _studio_asset_is_current(asset):
        return False
    meta = asset.get("metadata") or {}
    return (
        str(meta.get("production_contract_version") or "") == "stage04-production-v2"
        and str(meta.get("prompt_source") or "") == "stage04.image_prompt"
        and str(meta.get("semantic_compile") or "") == "locked"
        and bool(str(meta.get("shot_contract_fingerprint") or "").strip())
    )


def _studio_video_start_valid(asset: dict | None) -> bool:
    if not _studio_asset_is_current(asset):
        return False
    meta = asset.get("metadata") or {}
    return (
        str(meta.get("video_start_contract_version") or "") == "stage04-video-start-v2"
        and bool(str(meta.get("shot_contract_fingerprint") or "").strip())
        and bool(str(meta.get("video_motion_prompt_asset_id") or "").strip())
    )


def _studio_current_video_start(project_id: str, shot_id: str) -> dict | None:
    item = _studio_current_role_asset(project_id, shot_id, "shot_video_start_frame")
    if not _studio_video_start_valid(item):
        return None
    shot = _studio_formal_shot(project_id, shot_id)
    expected = _studio_shot_contract_fingerprint(shot)
    return item if str((item.get("metadata") or {}).get("shot_contract_fingerprint") or "") == expected else None


def _studio_latest_shot_asset(project_id: str, shot_id: str, asset_type: str) -> dict | None:
    kind = str(asset_type or "").upper()
    formal = _studio_formal_shot(project_id, shot_id)
    expected_fingerprint = _studio_shot_contract_fingerprint(formal)

    def same_contract(item: dict | None) -> bool:
        return bool(
            isinstance(item, dict)
            and str((item.get("metadata") or {}).get("shot_contract_fingerprint") or "")
            == expected_fingerprint
        )

    if kind == "IMAGE":
        base = _studio_current_role_asset(project_id, shot_id, "shot_keyframe")
        if not _studio_rep_keyframe_valid(base) or not same_contract(base):
            return None
        processed = _studio_current_role_asset(
            project_id, shot_id, "shot_image_processed"
        )
        if processed is not None:
            ancestors = _studio_asset_ancestors(
                project_id, str(processed.get("asset_id") or "")
            )
            if str(base.get("asset_id") or "") in ancestors and same_contract(processed):
                return processed
        return base

    if kind in {"VIDEO_START", "VIDEO_START_FRAME"}:
        return _studio_current_video_start(project_id, shot_id)

    if kind == "VIDEO":
        start_frame = _studio_current_video_start(project_id, shot_id)
        if start_frame is None:
            return None
        base = _studio_current_role_asset(project_id, shot_id, "shot_clip")
        if base is None or not same_contract(base):
            return None
        meta = base.get("metadata") or {}
        if str(meta.get("video_contract_version") or "") != "h3-start-frame-lineage-v2":
            return None
        ancestors = _studio_asset_ancestors(
            project_id, str(base.get("asset_id") or "")
        )
        if str(start_frame.get("asset_id") or "") not in ancestors:
            return None
        processed = _studio_current_role_asset(
            project_id, shot_id, "shot_video_processed"
        )
        if processed is not None:
            processed_ancestors = _studio_asset_ancestors(
                project_id, str(processed.get("asset_id") or "")
            )
            if str(base.get("asset_id") or "") in processed_ancestors and same_contract(processed):
                return processed
        return base
    return None


def _studio_formal_shot(project_id: str, shot_id: str) -> dict:
    _studio_v23963_recover_project(project_id)
    _studio_v23963_assert_no_active_rebuild(project_id)
    state = story_continuity.load(project_id)
    for row in state.get("shots") or []:
        if str(row.get("shot_id") or "") != str(shot_id or ""):
            continue
        if bool(row.get("provisional")):
            raise ValueError("剧情镜头种子不能直接进入制作，请先完成正式分镜")
        # Stage04 detailed storyboard stores the full production fields in the
        # continuity Shot and also mirrors core fields on the Shot entity.
        # Hydrate only missing values; never invent or semantically guess data.
        result = dict(row)
        entity_id = str(result.get("entity_id") or "")
        if entity_id:
            entity = next((
                e for e in director.production.list_entities(project_id)
                if str(e.get("entity_id") or "") == entity_id
            ), None)
            continuity = ((entity or {}).get("metadata") or {}).get("continuity") or {}
            if isinstance(continuity, dict):
                for key, value in continuity.items():
                    current = result.get(key)
                    if current is None or current == "" or (isinstance(current, list) and not current):
                        result[key] = value
        return result
    raise FileNotFoundError("正式分镜镜头不存在：" + str(shot_id))


def _studio_shot_prompt_asset(project_id: str, shot: dict, kind: str) -> dict:
    shot_id = str(shot.get("shot_id") or "")
    entity_id = str(shot.get("entity_id") or "")
    order = int(shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0)
    if kind == "image":
        content = str(shot.get("image_prompt") or "").strip()
        role, name = "shot_image_prompt", f"镜头 {order:03d} · 画面提示"
    else:
        content = str(shot.get("video_prompt") or "").strip()
        role, name = "shot_video_prompt", f"镜头 {order:03d} · 视频提示"
    if not content:
        raise ValueError(
            f"镜头 {order:03d} 缺少 {kind} Prompt；请先检查④分镜真实数据，不允许用空提示词制作"
        )
    logical_key = f"studio:shot:{shot_id}:{kind}-prompt"
    for current in director.production.list_assets(project_id, active_only=True):
        if (
            str(current.get("logical_key") or "") == logical_key
            and str(current.get("status") or "").lower() == "ready"
        ):
            try:
                if director.production.read_text_asset(project_id, current["asset_id"]) == content:
                    return current
            except Exception:
                pass
    return director.production.create_text_asset(
        project_id,
        stage="make",
        skill="manju-studio-shot-production",
        logical_key=logical_key,
        asset_role=role,
        name=name,
        content=content,
        asset_type="TEXT",
        extension=".txt",
        source={"type": "studio_shot_production", "shot_id": shot_id, "kind": kind},
        parent_asset_ids=[],
        entity_ids=[entity_id] if entity_id else [],
        metadata={
            "creator_ui": "shot_driven",
            "shot_id": shot_id,
            "shot_entity_id": entity_id,
            "scene_id": str(shot.get("scene_id") or ""),
            "global_order": order,
        },
    )


def _studio_shot_target(
    project_id: str,
    shot: dict,
    *,
    asset_type: str,
    asset_role: str,
    name: str,
    parent_asset_ids: list[str],
    extra_metadata: dict | None = None,
) -> dict:
    shot_id = str(shot.get("shot_id") or "")
    entity_id = str(shot.get("entity_id") or "")
    order = int(shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0)
    canonical = _studio_shot_canonical_key(shot_id, asset_role)
    metadata = {
        "creator_ui": "shot_driven",
        "candidate_target": True,
        "canonical_logical_key": canonical,
        "shot_id": shot_id,
        "shot_entity_id": entity_id,
        "scene_id": str(shot.get("scene_id") or ""),
        "global_order": order,
        "duration_seconds": shot.get("duration_seconds"),
        "shot_contract_fingerprint": _studio_shot_contract_fingerprint(shot),
    }
    if isinstance(extra_metadata, dict):
        metadata.update(extra_metadata)
    return director.production.declare_asset(
        project_id,
        stage="make",
        skill="manju-studio-shot-production",
        # Candidate targets are deliberately ephemeral. They MUST NOT replace
        # the formal slot until the user explicitly adopts the candidate.
        logical_key=(
            f"studio:candidate:{shot_id}:{asset_role}:"
            f"{_studio_secrets.token_hex(6)}"
        ),
        asset_type=asset_type,
        asset_role=asset_role,
        name=name,
        status="planned",
        source={"type": "studio_shot_candidate", "shot_id": shot_id},
        parent_asset_ids=parent_asset_ids,
        entity_ids=[entity_id] if entity_id else [],
        metadata=metadata,
    )


def _studio_shot_video_dimensions(aspect_ratio: str) -> tuple[int, int]:
    value = str(aspect_ratio or "16:9").strip()
    mapping = {
        "16:9": (768, 448),
        "9:16": (448, 768),
        "1:1": (640, 640),
        "4:3": (768, 576),
        "3:4": (576, 768),
        "21:9": (896, 384),
    }
    return mapping.get(value, mapping["16:9"])


def _studio_h3_length(duration_seconds: object) -> tuple[float, int, float]:
    try:
        intended = float(duration_seconds)
    except Exception as exc:
        raise ValueError("当前正式镜头缺少 duration_seconds；请先检查④分镜数据") from exc
    if not (0.1 < intended <= 150.0):
        raise ValueError(f"镜头时长超出 H3 可用范围：{intended}s")
    target_frames = max(5, min(3600, int(round(intended * 24.0))))
    n = int(round((target_frames - 5) / 17.0))
    max_n = (3600 - 5) // 17
    n = max(0, min(max_n, n))
    length = 5 + 17 * n
    return intended, length, length / 24.0


def _studio_mark_descendants_stale(project_id: str, root_asset_ids: list[str]) -> list[str]:
    roots = {str(x or "").strip() for x in root_asset_ids if str(x or "").strip()}
    if not roots:
        return []
    graph = director.production.get_graph(project_id)
    rows = graph.get("assets") or {}
    affected = set(roots)
    changed = False
    while True:
        grew = False
        for aid, item in rows.items():
            if aid in affected or not bool(item.get("active", True)):
                continue
            parents = {str(x or "").strip() for x in (item.get("parent_asset_ids") or [])}
            hits = parents & affected
            if not hits:
                continue
            item["dependency_state"] = "stale"
            stale = item.setdefault("stale_parent_asset_ids", [])
            for parent_id in sorted(hits):
                if parent_id not in stale:
                    stale.append(parent_id)
            item["updated_at"] = _studio_now()
            affected.add(str(aid))
            changed = True
            grew = True
        if not grew:
            break
    if changed:
        director.production._save(graph)
    return sorted(affected)


def _studio_publish_confirmed_shot_candidate(
    *,
    project_id: str,
    candidate_id: str,
    rows: list[dict],
    row: dict,
    target: dict,
    task: dict,
    selected: str,
) -> dict:
    shot_id = _studio_shot_asset_id(target)
    role = str(target.get("asset_role") or "")
    target_meta = target.get("metadata") or {}

    formal_shot = _studio_formal_shot(project_id, shot_id)
    expected_fingerprint = _studio_shot_contract_fingerprint(formal_shot)
    if str(target_meta.get("shot_contract_fingerprint") or "") != expected_fingerprint:
        raise ValueError("候选绑定的是旧分镜合同；当前 Stage04 canonical 已变化，请重新生成")

    if role == "shot_keyframe":
        if not (
            str(target_meta.get("production_contract_version") or "")
            == "stage04-production-v2"
            and str(target_meta.get("prompt_source") or "")
            == "stage04.image_prompt"
            and str(target_meta.get("semantic_compile") or "") == "locked"
            and bool(str(target_meta.get("shot_contract_fingerprint") or "").strip())
        ):
            raise ValueError(
                "这是旧的/非④分镜锁定画面候选，不能作为正式代表画面采用；请重新生成"
            )

    elif role == "shot_video_start_frame":
        if not (
            str(target_meta.get("video_start_contract_version") or "")
            == "stage04-video-start-v2"
            and str(target_meta.get("text_model_policy") or "") == "qwen3-32b"
            and bool(str(target_meta.get("video_motion_prompt_asset_id") or "").strip())
            and bool(str(target_meta.get("shot_contract_fingerprint") or "").strip())
        ):
            raise ValueError(
                "这是旧的/未绑定 Qwen3-32B 时序合同的视频首帧候选，不能采用；请重新生成"
            )

    elif role == "shot_clip":
        if not (
            str(target_meta.get("video_contract_version") or "")
            == "h3-start-frame-lineage-v2"
            and bool(str(target_meta.get("video_start_frame_asset_id") or "").strip())
            and bool(str(target_meta.get("shot_contract_fingerprint") or "").strip())
        ):
            raise ValueError(
                "这是旧的视频候选，未绑定独立 H3 视频首帧血缘，不能采用；请重新生成"
            )

    canonical = _studio_shot_canonical_key_for_target(target)
    if not shot_id or not canonical:
        raise ValueError("镜头候选缺少正式资产槽信息")

    graph_before = director.production.get_graph(project_id)
    previous = str(
        ((graph_before.get("logical_assets") or {}).get(canonical) or {}).get("active_asset_id")
        or ""
    )
    legacy_roots = []
    for old in director.production.list_assets(project_id, active_only=True):
        if (
            _studio_shot_asset_id(old) == shot_id
            and str(old.get("asset_role") or "") == role
            and str(old.get("status") or "").lower() == "ready"
            and str(old.get("logical_key") or "") != canonical
        ):
            legacy_roots.append(str(old.get("asset_id") or ""))

    deps = [
        str(x) for x in (row.get("dependency_asset_ids") or [])
        if str(x).strip()
    ]
    meta = _studio_json.loads(_studio_json.dumps(target.get("metadata") or {}, ensure_ascii=False))
    meta.update({
        "candidate_target": False,
        "canonical_logical_key": canonical,
        "confirmed_candidate_id": candidate_id,
        "task_params": task.get("params") or {},
        "confirmed_output_url": selected,
        "replaced_asset_ids": [x for x in [previous, *legacy_roots] if x],
    })
    source = dict(target.get("source") or {})
    source.update({
        "type": "director_candidate_confirm",
        "candidate_id": candidate_id,
        "task_id": str(task.get("task_id") or ""),
        "module": str(task.get("module") or ""),
        "operation": str(task.get("operation") or ""),
    })
    item = director.production.register_existing_file(
        project_id,
        stage=str(target.get("stage") or "make"),
        skill=str(target.get("skill") or "manju-studio-shot-production"),
        logical_key=canonical,
        asset_type=str(target.get("asset_type") or "FILE"),
        asset_role=role,
        name=str(target.get("name") or "镜头正式资产"),
        url=selected,
        source=source,
        parent_asset_ids=deps,
        entity_ids=list(target.get("entity_ids") or []),
        metadata=meta,
        contract_artifact_id=str(target.get("contract_artifact_id") or ""),
    )

    stale_roots = [x for x in [previous, *legacy_roots] if x and x != item["asset_id"]]
    _studio_mark_descendants_stale(project_id, stale_roots)
    for old_id in legacy_roots:
        try:
            director.production.archive_asset(project_id, old_id)
        except Exception:
            pass
    try:
        if str(target.get("asset_id") or "") != str(item.get("asset_id") or ""):
            director.production.archive_asset(project_id, str(target.get("asset_id") or ""))
    except Exception:
        pass

    for parent_id in deps:
        try:
            director.production.add_relation(
                project_id,
                source_id=parent_id,
                target_id=item["asset_id"],
                relation_type="input_to",
                metadata={"capability": row.get("capability"), "candidate_id": candidate_id},
            )
        except Exception:
            pass

    row["status"] = "confirmed"
    row["confirmed_asset_id"] = str(item["asset_id"])
    row["confirmed_output_url"] = selected
    row["confirmed_at"] = _wb_now()
    row["updated_at"] = _wb_now()
    _wb_save_candidates(project_id, rows)
    completion = director.refresh_production_completion(project_id)
    return {
        "candidate": row,
        "asset": item,
        "completion": completion,
        "manual_confirmation_applied": True,
        "shot_canonical_publish": True,
        "canonical_logical_key": canonical,
        "recursive_stale_propagation": True,
    }


def _studio_probe_video_file(path: _StudioPath) -> dict:
    exe = _studio_shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("系统未检测到 ffprobe")
    proc = _studio_subprocess.run(
        [
            exe, "-v", "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,sample_rate,channels:format=duration,format_name",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("ffprobe 失败：" + (proc.stderr or proc.stdout)[-1200:])
    data = _studio_json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    video = next((x for x in streams if x.get("codec_type") == "video"), {})
    audio = next((x for x in streams if x.get("codec_type") == "audio"), {})
    fmt = data.get("format") or {}
    def _fps(value: object) -> float:
        raw = str(value or "")
        if "/" in raw:
            a, b = raw.split("/", 1)
            try:
                return float(a) / float(b) if float(b) else 0.0
            except Exception:
                return 0.0
        try:
            return float(raw)
        except Exception:
            return 0.0
    try:
        duration = float(fmt.get("duration") or 0.0)
    except Exception:
        duration = 0.0
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "duration": duration,
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or "") if audio else "",
        "has_audio": bool(audio),
        "sample_rate": str(audio.get("sample_rate") or "") if audio else "",
        "channels": int(audio.get("channels") or 0) if audio else 0,
        "format": str(fmt.get("format_name") or ""),
    }


def _studio_final_dimensions(aspect_ratio: str, export_profile: str) -> tuple[int, int]:
    aspect = str(aspect_ratio or "16:9").strip()
    profile = str(export_profile or "working").strip().lower()
    working = {
        "16:9": (768, 448), "9:16": (448, 768), "1:1": (640, 640),
        "4:3": (768, 576), "3:4": (576, 768), "21:9": (896, 384),
    }
    hd = {
        "16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080),
        "4:3": (1440, 1080), "3:4": (1080, 1440), "21:9": (1920, 824),
    }
    uhd = {
        "16:9": (3840, 2160), "9:16": (2160, 3840), "1:1": (2160, 2160),
        "4:3": (2880, 2160), "3:4": (2160, 2880), "21:9": (3840, 1646),
    }
    table = uhd if profile == "4k" else hd if profile == "1080p" else working
    return table.get(aspect, table["16:9"])


# ===== V2.37.0 STAGE04 PRODUCTION CONTRACT / QWEN32B VIDEO START =====
import hashlib as _studio_v237_hashlib
import re as _studio_v237_re


def _studio_v237_cut(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if limit < 16:
        return text[:limit]
    head = max(1, (limit * 2) // 3)
    tail = max(1, limit - head - 5)
    return text[:head] + " ... " + text[-tail:]


def _studio_shot_contract_payload(shot: dict) -> dict:
    return {
        "shot_id": str(shot.get("shot_id") or ""),
        "scene_id": str(shot.get("scene_id") or ""),
        "episode_id": str(shot.get("episode_id") or ""),
        "global_order": int(
            shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0
        ),
        "title": _studio_v237_cut(shot.get("title"), 120),
        "summary": _studio_v237_cut(shot.get("summary"), 420),
        "duration_seconds": shot.get("duration_seconds"),
        "composition": _studio_v237_cut(shot.get("composition"), 240),
        "shot_size": _studio_v237_cut(shot.get("shot_size"), 100),
        "camera": _studio_v237_cut(shot.get("camera"), 140),
        "camera_move": _studio_v237_cut(shot.get("camera_move"), 180),
        "action": _studio_v237_cut(shot.get("action"), 420),
        "performance": _studio_v237_cut(shot.get("performance"), 240),
        "environment": _studio_v237_cut(shot.get("environment"), 300),
        "dialogue": _studio_v237_cut(shot.get("dialogue"), 220),
        "narration": _studio_v237_cut(shot.get("narration"), 220),
        "continuity": _studio_v237_cut(shot.get("continuity"), 320),
        "sound": _studio_v237_cut(shot.get("sound"), 220),
        "music": _studio_v237_cut(shot.get("music"), 220),
        "representative_state": _studio_v237_cut(shot.get("representative_state"), 680),
        "video_start_state": _studio_v237_cut(shot.get("video_start_state"), 680),
        "video_end_state": _studio_v237_cut(shot.get("video_end_state"), 680),
        "image_prompt": _studio_v237_cut(shot.get("image_prompt"), 680),
        "video_start_prompt": _studio_v237_cut(shot.get("video_start_prompt"), 680),
        "video_prompt": _studio_v237_cut(shot.get("video_prompt"), 680),
        "covered_beat_orders": list(shot.get("covered_beat_orders") or []),
        "source_provenance": shot.get("source_provenance") or {},
        "character_entity_ids": list(shot.get("character_entity_ids") or []),
        "prop_entity_ids": list(shot.get("prop_entity_ids") or []),
        "stage04_contract_version": str(shot.get("stage04_contract_version") or ""),
        "text_model_policy": str(shot.get("text_model_policy") or ""),
        "runtime_version": str(shot.get("runtime_version") or ""),
    }


def _studio_shot_contract_fingerprint(shot: dict) -> str:
    raw = _studio_json.dumps(
        _studio_shot_contract_payload(shot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _studio_v237_hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _studio_v237_compact_neighbor(shot: dict | None) -> dict:
    if not isinstance(shot, dict):
        return {}
    return {
        "shot_id": str(shot.get("shot_id") or ""),
        "order": int(
            shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0
        ),
        "title": _studio_v237_cut(shot.get("title"), 90),
        "summary": _studio_v237_cut(shot.get("summary"), 230),
        "action": _studio_v237_cut(shot.get("action"), 180),
        "continuity": _studio_v237_cut(shot.get("continuity"), 160),
    }


def _studio_v237_scene_source(project_id: str, shot: dict) -> tuple[str, str, dict, dict]:
    state = story_continuity.load(project_id)
    scene_id = str(shot.get("scene_id") or "")
    scene = next(
        (
            row for row in (state.get("scenes") or [])
            if str(row.get("scene_id") or "") == scene_id
        ),
        None,
    )
    if not scene:
        raise ValueError(f"找不到镜头所属 Scene：{scene_id}")

    source_asset_id, full_source = _studio_stage04_full_source(project_id)
    source = ""
    try:
        start = max(0, int(scene.get("source_start") or 0))
        end = max(start, int(scene.get("source_end") or start))
    except Exception:
        start, end = 0, 0

    if full_source and end > start:
        source = str(full_source[start:end]).strip()
    if not source:
        source = str(scene.get("source_excerpt") or "").strip()
    if not source:
        # Last-resort use of the Stage04 helper preserves source grounding;
        # it is only used when the scene has no exact source offsets.
        source = _studio_stage04_scene_source(scene, full_source).strip()
    if not source:
        raise ValueError("当前 Scene 没有可回溯小说正文，拒绝编译视频首帧")

    return source_asset_id, source, scene, state


def _studio_v237_ngrams(text: str, size: int = 2) -> set[str]:
    compact = _studio_v237_re.sub(
        r"[\s\W_]+",
        "",
        str(text or "").lower(),
        flags=_studio_v237_re.UNICODE,
    )
    if not compact:
        return set()
    if len(compact) <= size:
        return {compact}
    return {compact[i:i + size] for i in range(len(compact) - size + 1)}


def _studio_v237_relevant_window(source: str, shot: dict, budget: int) -> str:
    source = str(source or "").strip()
    if len(source) <= budget:
        return source
    query = "\n".join(
        str(shot.get(key) or "")
        for key in (
            "title", "summary", "action", "dialogue", "narration",
            "continuity", "image_prompt", "video_prompt",
        )
    )
    qgrams = _studio_v237_ngrams(query, 2)
    if not qgrams:
        return source[:budget]

    probe = min(max(480, budget // 2), 820)
    stride = max(200, probe // 2)
    best = None
    for start in range(0, len(source), stride):
        end = min(len(source), start + probe)
        chunk = source[start:end]
        grams = _studio_v237_ngrams(chunk, 2)
        overlap = len(qgrams & grams)
        density = overlap / max(1, len(grams))
        row = (overlap, density, -start, start, end)
        if best is None or row > best:
            best = row
        if end >= len(source):
            break
    if best is None:
        return source[:budget]
    center = (best[3] + best[4]) // 2
    start = max(0, center - budget // 2)
    end = min(len(source), start + budget)
    start = max(0, end - budget)
    return source[start:end]


def _studio_v237_evidence_anchors(source: str) -> list[dict]:
    source = str(source or "").strip()
    if not source:
        return []

    parts = []
    last = 0
    for match in _studio_v237_re.finditer(r"[。！？!?；;\n]+", source):
        end = match.end()
        text = source[last:end].strip()
        if text:
            parts.append(text)
        last = end
    tail = source[last:].strip()
    if tail:
        parts.append(tail)

    normalized = []
    for text in parts:
        visible = _studio_v237_re.sub(r"\s+", "", text)
        # Structural heading filter: a short punctuation-free line is not
        # sufficiently evidential for a production shot.
        has_sentence_mark = bool(_studio_v237_re.search(r"[。！？!?；;]", text))
        if len(visible) < 18 and not has_sentence_mark:
            continue
        if len(text) <= 170:
            normalized.append(text)
        else:
            pos = 0
            while pos < len(text):
                chunk = text[pos:pos + 145].strip()
                if len(_studio_v237_re.sub(r"\s+", "", chunk)) >= 18:
                    normalized.append(chunk)
                if pos + 145 >= len(text):
                    break
                pos += 105

    return [
        {"id": f"E{i:03d}", "text": text}
        for i, text in enumerate(normalized[:36], 1)
    ]


def _studio_v237_scene_fact(project_id: str, scene_id: str) -> dict:
    resolved = story_continuity.resolve_scene(project_id, scene_id)

    def rows(values):
        out = []
        for row in values or []:
            if not isinstance(row, dict):
                continue
            out.append({
                "entity_id": str(row.get("entity_id") or ""),
                "name": _studio_v237_cut(row.get("name"), 90),
                "state": _studio_v237_cut(
                    row.get("state") or row.get("status") or row.get("description"),
                    160,
                ),
            })
            if len(out) >= 10:
                break
        return out

    return {
        "location": _studio_v237_cut(resolved.get("location"), 240),
        "characters": rows(resolved.get("characters")),
        "props": rows(resolved.get("props")),
        "scene_state": _studio_v237_cut(
            _studio_json.dumps(
                resolved.get("scene_state") or {}, ensure_ascii=False
            ),
            620,
        ),
    }


def _studio_v237_neighbor_shots(state: dict, shot: dict) -> tuple[dict, dict]:
    scene_id = str(shot.get("scene_id") or "")
    rows = [
        row for row in (state.get("shots") or [])
        if str(row.get("scene_id") or "") == scene_id and not bool(row.get("provisional"))
    ]
    rows.sort(
        key=lambda row: (
            int(row.get("global_order") or row.get("sequence") or row.get("order") or 0),
            str(row.get("shot_id") or ""),
        )
    )
    idx = next(
        (
            i for i, row in enumerate(rows)
            if str(row.get("shot_id") or "") == str(shot.get("shot_id") or "")
        ),
        -1,
    )
    previous = rows[idx - 1] if idx > 0 else None
    following = rows[idx + 1] if idx >= 0 and idx + 1 < len(rows) else None
    return _studio_v237_compact_neighbor(previous), _studio_v237_compact_neighbor(following)


def _studio_v237_resolve_evidence(contract: dict, anchors: list[dict]) -> tuple[list[str], list[str]]:
    amap = {
        str(row.get("id") or ""): str(row.get("text") or "")
        for row in anchors
        if isinstance(row, dict) and str(row.get("id") or "") and str(row.get("text") or "")
    }
    ids = []
    texts = []
    for value in contract.get("source_evidence_ids") or []:
        key = str(value or "").strip()
        if not key or key in ids or key not in amap:
            continue
        ids.append(key)
        texts.append(amap[key])
        if len(ids) >= 4:
            break
    return ids, texts


async def _studio_v2396_qwen_runtime_contract(
    *, verify_chat_response: bool = False
) -> dict:
    # GPUOwner.gemma is a historical workspace enum. The actual model policy
    # for all semantic text work in this product is Qwen3-32B.
    required_id = str(settings.stage04_required_model_id).strip()
    required_alias = str(settings.stage04_required_model_alias).strip()
    selected = llm_registry.selected_model()
    selected_id = str(selected.get("id") or "").strip()
    selected_alias = str(selected.get("alias") or "").strip()
    if selected_id != required_id:
        raise RuntimeError(
            "Stage04 已选择模型不是要求的 Qwen3-32B："
            f"selected={selected_id or '<empty>'} required={required_id}"
        )
    if selected_alias != required_alias:
        raise RuntimeError(
            "Stage04 Qwen registry alias 不一致："
            f"selected={selected_alias or '<empty>'} required={required_alias}"
        )
    if not bool(selected.get("installed")):
        raise RuntimeError(
            "Stage04 Qwen GGUF 不存在："
            f"{selected.get('path') or '<empty>'}"
        )

    status = await gemma.status()
    if not bool(status.get("ready")):
        raise RuntimeError(
            str(status.get("message") or "Stage04 LLM 服务未 READY")
        )
    resolved = str(status.get("resolved_model") or "").strip()
    models = [
        str(item or "").strip()
        for item in (status.get("models") or [])
        if str(item or "").strip()
    ]
    if resolved != required_alias:
        raise RuntimeError(
            "Stage04 resolved model 不匹配："
            f"resolved={resolved or '<empty>'} required={required_alias}"
        )
    if models != [required_alias]:
        raise RuntimeError(
            "Stage04 /v1/models 必须且只能暴露要求的 Qwen alias："
            f"models={models!r} required={[required_alias]!r}"
        )
    result = None
    if verify_chat_response:
        result = await gemma.chat(
            messages=[{"role": "user", "content": "Reply exactly QWEN_OK"}],
            system_prompt=(
                "This is a Stage04 runtime identity probe. "
                "Reply with exactly QWEN_OK and no other text."
            ),
            temperature=0.0,
            max_tokens=16,
        )
        response_model = str(result.get("model") or "").strip()
        if response_model != required_alias:
            raise RuntimeError(
                "Stage04 chat response model 不匹配："
                f"response={response_model or '<empty>'} required={required_alias}"
            )
        if str(result.get("content") or "").strip() != "QWEN_OK":
            raise RuntimeError("Stage04 Qwen identity probe 未返回 QWEN_OK")
    return {
        "selected_model_id": selected_id,
        "resolved_model": resolved,
        "models": models,
        "response_model": (
            str(result.get("model") or "").strip() if result else ""
        ),
    }


async def _studio_v2396_prepare_stage04_qwen() -> dict:
    # This completes before the Stage04 background task is created, so the
    # startup activator cannot race a newly incremented LLM active-task count.
    loop = asyncio.get_running_loop()
    workspace_started = loop.time()
    await gpu.ensure_ready(GPUOwner.gemma)
    workspace_seconds = loop.time() - workspace_started
    qwen_ready_started = loop.time()
    await _ensure_selected_llm_loaded()
    contract = await _studio_v2396_qwen_runtime_contract(
        verify_chat_response=True
    )
    qwen_ready_seconds = loop.time() - qwen_ready_started
    return {
        **contract,
        "performance": {
            "schema_version": "stage04-perf-v1",
            "workspace_start_seconds": round(workspace_seconds, 6),
            "qwen_ready_wait_seconds": round(qwen_ready_seconds, 6),
            "qwen_contract_verified": True,
        },
    }


async def _studio_v237_require_qwen32b() -> None:
    contract_cached = globals().get(
        "_studio_v2396_qwen_contract_cached"
    )
    if callable(contract_cached) and bool(contract_cached()):
        # Rebuild-wide GPU guard keeps the preflight-verified workspace active.
        # Every request still checks its actual response.model value.
        return
    await _studio_v2396_qwen_runtime_contract()


def _studio_v237_contract_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    required = ("start_state", "transition_state", "result_state",
                "start_frame_prompt", "forward_motion_prompt")
    return all(bool(str(value.get(key) or "").strip()) for key in required)


def _studio_v237_contract_prompt(pack: dict) -> str:
    return (
        "=== ORIGINAL_SCENE_SOURCE ===\n"
        + str(pack.get("source_window") or "")
        + "\n\n=== SOURCE_EVIDENCE_ANCHORS ===\n"
        + _studio_json.dumps(pack.get("anchors") or [], ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== SCENE_FACT ===\n"
        + _studio_json.dumps(pack.get("scene_fact") or {}, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== PREVIOUS_SHOT ===\n"
        + _studio_json.dumps(pack.get("previous_shot") or {}, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== CURRENT_FORMAL_SHOT ===\n"
        + _studio_json.dumps(pack.get("shot") or {}, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== NEXT_SHOT ===\n"
        + _studio_json.dumps(pack.get("next_shot") or {}, ensure_ascii=False, separators=(",", ":"))
    )


async def _studio_v237_audit_video_contract(pack: dict, contract: dict) -> dict:
    system_prompt = (
        "你是小说改编图生视频的时序合同审计器。"
        "只依据 ORIGINAL_SCENE_SOURCE、证据锚点、Scene Fact 和已确认 Formal Shot。"
        "检查：start_state 是否确实早于 result_state；start_frame_prompt 是否只表现起始态"
        "或变化刚开始的状态；不能把结果态主体完整提前到首帧；"
        "forward_motion_prompt 是否只沿剧情时间向前；不得逆向/倒放；"
        "不得添加原文和已确认实体没有支持的剧情事实。只输出严格 JSON。"
    )
    prompt = (
        _studio_v237_contract_prompt(pack)
        + "\n\n=== CANDIDATE_VIDEO_START_CONTRACT ===\n"
        + _studio_json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
    )
    async with gpu.use(GPUOwner.gemma):
        await _studio_v237_require_qwen32b()
        _, audit, _ = await director._structured_json_call(
            phase="studio_v237_qwen32b_video_start_audit",
            messages=[{"role": "user", "content": prompt}],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=700,
            contract=(
                '{"source_faithful":true,"start_precedes_result":true,'
                '"start_frame_not_result_state":true,"forward_time_only":true,'
                '"no_unsupported_visual_facts":true,"evidence_supported":true,'
                '"issues":[]}'
            ),
        )
    return audit if isinstance(audit, dict) else {}


def _studio_v237_audit_pass(audit: dict) -> bool:
    return isinstance(audit, dict) and all(
        audit.get(key) is True
        for key in (
            "source_faithful",
            "start_precedes_result",
            "start_frame_not_result_state",
            "forward_time_only",
            "no_unsupported_visual_facts",
            "evidence_supported",
        )
    )


async def _studio_compile_video_start_contract(project_id: str, shot: dict) -> dict:
    shot_id = str(shot.get("shot_id") or "")
    order = int(
        shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0
    )
    shot_fingerprint = _studio_shot_contract_fingerprint(shot)
    logical_key = f"studio:shot:{shot_id}:video-start-contract"

    source_asset_id, full_scene_source, scene, state = _studio_v237_scene_source(
        project_id, shot
    )
    previous, following = _studio_v237_neighbor_shots(state, shot)

    system_prompt = (
        "你是 AI 漫剧 I2V 视频首帧编译器，默认文本模型为 Qwen3-32B。"
        "Formal Shot 已由④分镜确认，你不能重新改写剧情。"
        "你只负责把这个已确认 Shot 拆成视频时间上的 start_state、transition_state、"
        "result_state，并编译 H3 的 start_frame_prompt 与 forward_motion_prompt。"
        "start_state 必须是该镜头首个可见变化发生前或刚开始；"
        "若结果是某人物/物体出现、破裂完成、动作完成，不能在 start_frame_prompt 中"
        "提前完整表现结果态。forward_motion_prompt 必须从 start_state 单向推进到"
        "result_state，禁止倒放、返回更早状态。"
        "所有剧情事实必须有原文/Scene Fact/Formal Shot 支持。"
        "证据只能返回 SOURCE_EVIDENCE_ANCHORS 中的 ID，不得自行抄写原文。"
        "只输出严格 JSON。"
    )
    schema = (
        '{"start_state":"","transition_state":"","result_state":"",'
        '"start_frame_prompt":"","forward_motion_prompt":"","end_state":"",'
        '"camera_motion":"","continuity_constraints":"",'
        '"reverse_motion_constraints":"","source_evidence_ids":["E001"],'
        '"unsupported_visual_facts":[],"reason":""}'
    )

    last_error = None
    for source_budget in (1400, 1000, 700):
        source_window = _studio_v237_relevant_window(
            full_scene_source, shot, source_budget
        )
        anchors = _studio_v237_evidence_anchors(source_window)
        if not anchors:
            continue
        pack = {
            "source_window": source_window,
            "anchors": anchors,
            "scene_fact": _studio_v237_scene_fact(
                project_id, str(shot.get("scene_id") or "")
            ),
            "previous_shot": previous,
            "shot": _studio_shot_contract_payload(shot),
            "next_shot": following,
        }
        source_fingerprint = _studio_v237_hashlib.sha256(
            _studio_json.dumps(
                {
                    "shot_fingerprint": shot_fingerprint,
                    "source_asset_id": source_asset_id,
                    "source_window": source_window,
                    "anchors": anchors,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        for current in director.production.list_assets(project_id, active_only=True):
            meta = current.get("metadata") or {}
            if (
                str(current.get("logical_key") or "") == logical_key
                and str(current.get("status") or "").lower() == "ready"
                and str(meta.get("video_start_contract_version") or "")
                    == "stage04-video-start-v2"
                and str(meta.get("shot_contract_fingerprint") or "")
                    == shot_fingerprint
                and str(meta.get("source_fingerprint") or "") == source_fingerprint
            ):
                try:
                    cached = _studio_json.loads(
                        director.production.read_text_asset(
                            project_id, current["asset_id"]
                        )
                    )
                    ids, texts = _studio_v237_resolve_evidence(cached, anchors)
                    if _studio_v237_contract_valid(cached) and ids:
                        cached["source_evidence_ids"] = ids
                        cached["source_evidence"] = texts
                        cached["_asset"] = current
                        cached["shot_contract_fingerprint"] = shot_fingerprint
                        cached["source_fingerprint"] = source_fingerprint
                        return cached
                except Exception:
                    pass

        prompt = _studio_v237_contract_prompt(pack)
        try:
            async with gpu.use(GPUOwner.gemma):
                await _studio_v237_require_qwen32b()
                _, contract, _ = await director._structured_json_call(
                    phase="studio_v237_qwen32b_video_start",
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt=system_prompt,
                    temperature=0.08,
                    max_tokens=1100,
                    contract=schema,
                )
        except Exception as exc:
            last_error = exc
            message = str(exc)
            if (
                "上下文预算不足" in message
                or "context_window" in message
                or "prompt_tokens" in message
            ):
                continue
            raise ValueError(
                f"镜头 {order:03d} Qwen3-32B 视频首帧编译失败："
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not _studio_v237_contract_valid(contract):
            raise ValueError(
                f"镜头 {order:03d} Qwen3-32B 没有返回完整的起始/过渡/结果时序合同"
            )
        contract = dict(contract)
        ids, texts = _studio_v237_resolve_evidence(contract, anchors)
        if not ids:
            raise ValueError(
                f"镜头 {order:03d} 没有选择有效小说正文证据锚点；拒绝生成视频首帧"
            )
        contract["source_evidence_ids"] = ids
        contract["source_evidence"] = texts

        audit = await _studio_v237_audit_video_contract(pack, contract)
        if not _studio_v237_audit_pass(audit):
            raise ValueError(
                f"镜头 {order:03d} 视频首帧时序审计未通过："
                + _studio_json.dumps(audit.get("issues") or [], ensure_ascii=False)
            )

        contract["video_start_contract_version"] = "stage04-video-start-v2"
        contract["text_model_policy"] = "qwen3-32b"
        contract["shot_contract_fingerprint"] = shot_fingerprint
        contract["source_fingerprint"] = source_fingerprint
        contract["source_asset_id"] = source_asset_id
        contract["source_budget_chars"] = source_budget
        contract["audit"] = audit

        entity_id = str(shot.get("entity_id") or "")
        asset = director.production.create_text_asset(
            project_id,
            stage="make",
            skill="manju-studio-qwen32b-video-start",
            logical_key=logical_key,
            asset_role="shot_video_start_contract",
            name=f"镜头 {order:03d} · Qwen3-32B 视频首帧合同",
            content=_studio_json.dumps(contract, ensure_ascii=False, indent=2),
            asset_type="TEXT",
            extension=".json",
            source={
                "type": "studio_v237_qwen32b_video_start",
                "shot_id": shot_id,
                "source_asset_id": source_asset_id,
            },
            parent_asset_ids=[],
            entity_ids=[entity_id] if entity_id else [],
            metadata={
                "creator_ui": "shot_driven",
                "shot_id": shot_id,
                "scene_id": str(shot.get("scene_id") or ""),
                "global_order": order,
                "video_start_contract_version": "stage04-video-start-v2",
                "text_model_policy": "qwen3-32b",
                "shot_contract_fingerprint": shot_fingerprint,
                "source_fingerprint": source_fingerprint,
                "source_evidence_ids": ids,
                "source_evidence": texts,
            },
        )
        contract["_asset"] = asset
        return contract

    raise ValueError(
        f"镜头 {order:03d} 在预算化原文窗口下仍无法完成 Qwen3-32B 视频首帧编译"
        + (
            f"；最后错误：{type(last_error).__name__}: {last_error}"
            if last_error else ""
        )
    )


def _studio_video_contract_prompt_asset(
    project_id: str,
    shot: dict,
    contract: dict,
    kind: str,
) -> dict:
    shot_id = str(shot.get("shot_id") or "")
    order = int(
        shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0
    )
    if kind == "start":
        content = str(contract.get("start_frame_prompt") or "").strip()
        logical_key = f"studio:shot:{shot_id}:video-start-prompt"
        role = "shot_video_start_prompt"
        name = f"镜头 {order:03d} · H3 视频首帧 Prompt"
    elif kind == "motion":
        parts = [str(contract.get("forward_motion_prompt") or "").strip()]
        camera = str(contract.get("camera_motion") or "").strip()
        continuity = str(contract.get("continuity_constraints") or "").strip()
        reverse = str(contract.get("reverse_motion_constraints") or "").strip()
        if camera:
            parts.append("镜头运动：" + camera)
        if continuity:
            parts.append("连续性约束：" + continuity)
        if reverse:
            parts.append("时序约束：" + reverse)
        parts.append("时间方向：只从首帧向结果态推进，不倒放、不回到更早剧情状态。")
        content = "\n".join(x for x in parts if x).strip()
        logical_key = f"studio:shot:{shot_id}:video-motion-prompt"
        role = "shot_video_prompt"
        name = f"镜头 {order:03d} · H3 前向动作 Prompt"
    else:
        raise ValueError("未知视频 Prompt 类型")

    if not content:
        raise ValueError(f"镜头 {order:03d} 视频 {kind} Prompt 为空")

    fingerprint = str(contract.get("shot_contract_fingerprint") or "")
    contract_asset = contract.get("_asset") or {}
    for current in director.production.list_assets(project_id, active_only=True):
        meta = current.get("metadata") or {}
        if (
            str(current.get("logical_key") or "") == logical_key
            and str(current.get("status") or "").lower() == "ready"
            and str(meta.get("shot_contract_fingerprint") or "") == fingerprint
        ):
            try:
                if director.production.read_text_asset(
                    project_id, current["asset_id"]
                ) == content:
                    return current
            except Exception:
                pass

    return director.production.create_text_asset(
        project_id,
        stage="make",
        skill="manju-studio-qwen32b-video-start",
        logical_key=logical_key,
        asset_role=role,
        name=name,
        content=content,
        asset_type="TEXT",
        extension=".txt",
        source={
            "type": "studio_v237_qwen32b_video_start",
            "shot_id": shot_id,
            "kind": kind,
        },
        parent_asset_ids=[
            str(contract_asset.get("asset_id") or "")
        ] if str(contract_asset.get("asset_id") or "") else [],
        entity_ids=[str(shot.get("entity_id") or "")] if str(shot.get("entity_id") or "") else [],
        metadata={
            "creator_ui": "shot_driven",
            "shot_id": shot_id,
            "scene_id": str(shot.get("scene_id") or ""),
            "global_order": order,
            "video_start_contract_version": "stage04-video-start-v2",
            "text_model_policy": "qwen3-32b",
            "shot_contract_fingerprint": fingerprint,
            "source_evidence_ids": list(contract.get("source_evidence_ids") or []),
            "source_evidence": list(contract.get("source_evidence") or []),
        },
    )
# ===== /V2.37.0 STAGE04 PRODUCTION CONTRACT / QWEN32B VIDEO START =====

@app.post("/api/studio/projects/{project_id}/shots/{shot_id}/generate-image")
async def studio_generate_shot_image(project_id: str, shot_id: str, payload: dict) -> dict:
    try:
        director.get_project(project_id)
        shot = _studio_formal_shot(project_id,shot_id)
        _studio_v2371_require_strict_shot(shot)
        prompt_asset = _studio_v2371_prompt_asset(project_id,shot,"image")
        fingerprint = _studio_shot_contract_fingerprint(shot)
        order = int(shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0)
        provenance = shot.get("source_provenance") or {}
        target = _studio_shot_target(
            project_id,shot,asset_type="IMAGE",asset_role="shot_keyframe",
            name=f"镜头 {order:03d} · 分镜代表画面",
            parent_asset_ids=[prompt_asset["asset_id"]],
            extra_metadata={
                "production_contract_version":"stage04-production-v2",
                "stage04_contract_version":"strict-shot-v2",
                "prompt_source":"stage04.image_prompt",
                "prompt_asset_id":str(prompt_asset.get("asset_id") or ""),
                "semantic_compile":"locked","shot_contract_fingerprint":fingerprint,
                "representative_image":True,"video_start_frame":False,
                "representative_state":str(shot.get("representative_state") or ""),
                "source_evidence_ids":list(provenance.get("source_evidence_ids") or []),
                "source_evidence":list(provenance.get("source_evidence") or []),
            },
        )
        model_key = str(payload.get("model_key") or "z_image_turbo").strip()
        is_z = model_key == "z_image_turbo"
        params = {
            "aspect_ratio":str(payload.get("aspect_ratio") or "16:9"),
            "steps":int(payload.get("steps") or (9 if is_z else 32)),
            "model_key":model_key,
            "style_name":str(payload.get("style_name") or "portrait_photo"),
            "style_strength":str(payload.get("style_strength") or "standard"),
            "cfg":float(payload.get("cfg") if payload.get("cfg") is not None else (1.0 if is_z else 6.5)),
            "seed":int(payload.get("seed") if payload.get("seed") is not None else -1),
            "sampler":str(payload.get("sampler") or ("euler" if is_z else "dpmpp_2m")),
            "scheduler":str(payload.get("scheduler") or ("simple" if is_z else "karras")),
            "count":1,"semantic_compile":"locked","pose_control":"off",
            "appearance_enhance_mode":"off",
        }
        return await director_workbench_execute_candidate(
            project_id,{"target_asset_id":target["asset_id"],"capability":"image",
                        "mode":"txt2img","prompt_asset_id":prompt_asset["asset_id"],"params":params}
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except (ValueError,RuntimeError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc

@app.post("/api/studio/projects/{project_id}/shots/{shot_id}/generate-video-start")
async def studio_generate_shot_video_start(project_id: str, shot_id: str, payload: dict) -> dict:
    try:
        director.get_project(project_id)
        shot = _studio_formal_shot(project_id,shot_id)
        _studio_v2371_require_strict_shot(shot)
        prompt_asset = _studio_v2371_prompt_asset(project_id,shot,"video_start")
        motion_asset = _studio_v2371_prompt_asset(project_id,shot,"video_motion")
        fingerprint = _studio_shot_contract_fingerprint(shot)
        provenance = shot.get("source_provenance") or {}
        order = int(shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0)
        target = _studio_shot_target(
            project_id,shot,asset_type="IMAGE",asset_role="shot_video_start_frame",
            name=f"镜头 {order:03d} · H3 视频首帧",parent_asset_ids=[prompt_asset["asset_id"]],
            extra_metadata={
                "video_start_contract_version":"stage04-video-start-v2",
                "stage04_contract_version":"strict-shot-v2",
                "text_model_policy":"qwen3-32b",
                "shot_contract_fingerprint":fingerprint,
                "video_start_prompt_asset_id":str(prompt_asset.get("asset_id") or ""),
                "video_motion_prompt_asset_id":str(motion_asset.get("asset_id") or ""),
                "semantic_compile":"locked",
                "video_start_state":str(shot.get("video_start_state") or ""),
                "video_end_state":str(shot.get("video_end_state") or ""),
                "source_evidence_ids":list(provenance.get("source_evidence_ids") or []),
                "source_evidence":list(provenance.get("source_evidence") or []),
            },
        )
        model_key = str(payload.get("model_key") or "z_image_turbo").strip()
        is_z = model_key == "z_image_turbo"
        params = {
            "aspect_ratio":str(payload.get("aspect_ratio") or "16:9"),
            "steps":int(payload.get("steps") or (9 if is_z else 32)),
            "model_key":model_key,
            "style_name":str(payload.get("style_name") or "portrait_photo"),
            "style_strength":str(payload.get("style_strength") or "standard"),
            "cfg":float(payload.get("cfg") if payload.get("cfg") is not None else (1.0 if is_z else 6.5)),
            "seed":int(payload.get("seed") if payload.get("seed") is not None else -1),
            "sampler":str(payload.get("sampler") or ("euler" if is_z else "dpmpp_2m")),
            "scheduler":str(payload.get("scheduler") or ("simple" if is_z else "karras")),
            "count":1,"semantic_compile":"locked","pose_control":"off",
            "appearance_enhance_mode":"off",
        }
        return await director_workbench_execute_candidate(
            project_id,{"target_asset_id":target["asset_id"],"capability":"image",
                        "mode":"txt2img","prompt_asset_id":prompt_asset["asset_id"],"params":params}
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except (ValueError,RuntimeError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc

@app.post("/api/studio/projects/{project_id}/shots/{shot_id}/generate-video")
async def studio_generate_shot_video(project_id: str, shot_id: str, payload: dict) -> dict:
    try:
        director.get_project(project_id)
        shot = _studio_formal_shot(project_id,shot_id)
        _studio_v2371_require_strict_shot(shot)
        fingerprint = _studio_shot_contract_fingerprint(shot)
        start_frame = _studio_current_video_start(project_id,shot_id)
        if start_frame is None:
            raise ValueError("当前镜头还没有已采用的严格④ H3 视频首帧")
        start_meta = start_frame.get("metadata") or {}
        if str(start_meta.get("shot_contract_fingerprint") or "") != fingerprint:
            raise ValueError("当前 H3 视频首帧对应旧分镜合同；请重新生成并采用")
        motion_asset_id = str(start_meta.get("video_motion_prompt_asset_id") or "").strip()
        if not motion_asset_id:
            raise ValueError("当前视频首帧没有绑定④视频运动 Prompt")
        motion_asset = director.production.get_asset(project_id,motion_asset_id)
        if not _studio_asset_is_current(motion_asset):
            raise ValueError("当前视频运动 Prompt 已失效；请重新生成视频首帧")

        intended,length,effective = _studio_h3_length(shot.get("duration_seconds"))
        order = int(shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0)
        profile = str(payload.get("video_profile") or "standard").strip().lower()
        if profile not in {"standard","turbo"}:
            profile = "standard"
        aspect = str(payload.get("aspect_ratio") or "16:9")
        width,height = _studio_shot_video_dimensions(aspect)
        target = _studio_shot_target(
            project_id,shot,asset_type="VIDEO",asset_role="shot_clip",
            name=f"镜头 {order:03d} · H3 视频",
            parent_asset_ids=[motion_asset_id,start_frame["asset_id"]],
            extra_metadata={
                "video_contract_version":"h3-start-frame-lineage-v2",
                "stage04_contract_version":"strict-shot-v2",
                "shot_contract_fingerprint":fingerprint,
                "video_start_frame_asset_id":str(start_frame.get("asset_id") or ""),
                "video_motion_prompt_asset_id":motion_asset_id,
                "video_start_contract_version":"stage04-video-start-v2",
                "text_model_policy":"qwen3-32b",
                "aspect_ratio":aspect,"intended_duration_seconds":intended,
                "h3_length":length,"effective_duration_seconds":effective,
                "fps":24,"video_profile":profile,
            },
        )
        return await director_workbench_execute_candidate(
            project_id,{"target_asset_id":target["asset_id"],"capability":"video",
                        "mode":"fl2va","prompt_asset_id":motion_asset_id,
                        "first_frame_asset_id":start_frame["asset_id"],"last_frame_asset_id":"",
                        "params":{"mode":"fl2va","prompt":"","width":width,"height":height,
                                  "length":length,"steps":4 if profile=="turbo" else int(payload.get("steps") or 20),
                                  "seed":-1,"ref_image_size":"match","video_profile":profile}}
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except (ValueError,RuntimeError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc

@app.post("/api/studio/projects/{project_id}/shots/{shot_id}/facefusion")
async def studio_facefusion_shot(project_id: str, shot_id: str, payload: dict) -> dict:
    try:
        director.get_project(project_id)
        shot = _studio_formal_shot(project_id, shot_id)
        target_kind = str(payload.get("target_kind") or "video").strip().lower()
        target_asset = _studio_latest_shot_asset(
            project_id, shot_id, "VIDEO" if target_kind == "video" else "IMAGE"
        )
        if target_asset is None and target_kind == "video":
            target_asset = _studio_latest_shot_asset(project_id, shot_id, "IMAGE")
        if target_asset is None:
            raise ValueError("当前镜头还没有当前有效的可处理图片或视频")
        processor = str(payload.get("processor") or "").strip()
        if not processor:
            raise ValueError("请选择人物处理方式")
        source_id = str(payload.get("source_asset_id") or "").strip()
        parents = [target_asset["asset_id"]]
        if source_id:
            director.production.get_asset(project_id, source_id)
            parents.append(source_id)
        out_type = str(target_asset.get("asset_type") or "").upper()
        role = "shot_video_processed" if out_type == "VIDEO" else "shot_image_processed"
        order = int(shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0)
        target = _studio_shot_target(
            project_id, shot, asset_type=out_type, asset_role=role,
            name=f"镜头 {order:03d} · 人物处理", parent_asset_ids=parents,
        )
        return await director_workbench_execute_candidate(project_id, {
            "target_asset_id": target["asset_id"], "capability": "facefusion",
            "processor": processor, "target_input_asset_id": target_asset["asset_id"],
            "source_input_asset_id": source_id,
            "authorized_adult": bool(payload.get("authorized_adult", False)),
            "params": payload.get("params") if isinstance(payload.get("params"), dict) else {},
        })
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

# ===== /V2.36.1 E2E SHOT CHAIN =====



@app.post("/api/studio/projects/{project_id}/generate-image")
async def studio_generate_image(project_id: str, payload: dict) -> dict:
    prompt_id = str(payload.get("prompt_asset_id") or "").strip()
    if not prompt_id:
        raise HTTPException(status_code=400, detail="请选择提示词项目资产")
    try:
        prompt_asset = director.production.get_asset(project_id, prompt_id)
        if str(prompt_asset.get("status") or "").lower() != "ready":
            raise ValueError("提示词资产必须 READY")
        stage = str(prompt_asset.get("stage") or "03")
        target = _studio_new_target(
            project_id,
            asset_type="IMAGE",
            name=str(payload.get("name") or "关键画面"),
            role="keyframe",
            stage=stage,
            skill=str(prompt_asset.get("skill") or _STUDIO_STAGE_SKILLS.get(stage, "")),
            parent_asset_ids=[prompt_id],
        )
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        return await director_workbench_execute_candidate(project_id, {
            "target_asset_id": target["asset_id"],
            "capability": "image",
            "mode": "txt2img",
            "prompt_asset_id": prompt_id,
            "params": params,
        })
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/studio/projects/{project_id}/generate-video")
async def studio_generate_video(project_id: str, payload: dict) -> dict:
    prompt_id = str(payload.get("prompt_asset_id") or "").strip()
    if not prompt_id:
        raise HTTPException(status_code=400, detail="请选择视频提示词项目资产")
    try:
        prompt_asset = director.production.get_asset(project_id, prompt_id)
        if str(prompt_asset.get("status") or "").lower() != "ready":
            raise ValueError("提示词资产必须 READY")
        stage = str(prompt_asset.get("stage") or "04")
        mode = str(payload.get("mode") or "t2va").lower()
        profile = str(payload.get("video_profile") or "standard").lower()
        target = _studio_new_target(
            project_id,
            asset_type="VIDEO",
            name=str(payload.get("name") or "镜头视频"),
            role="shot_clip",
            stage=stage,
            skill=str(prompt_asset.get("skill") or _STUDIO_STAGE_SKILLS.get(stage, "")),
            parent_asset_ids=[prompt_id],
        )
        req = {
            "target_asset_id": target["asset_id"],
            "capability": "video",
            "mode": mode,
            "prompt_asset_id": prompt_id,
            "params": {
                "mode": mode,
                "prompt": "",
                "width": 768,
                "height": 448,
                "length": 124,
                "steps": 4 if profile == "turbo" else int(payload.get("steps") or 20),
                "seed": -1,
                "ref_image_size": "match",
                "video_profile": profile,
            },
        }
        if mode == "fl2va":
            req["first_frame_asset_id"] = str(payload.get("first_frame_asset_id") or "")
            req["last_frame_asset_id"] = str(payload.get("last_frame_asset_id") or "")
        elif mode == "ref2va":
            req["reference_image_asset_id"] = str(payload.get("reference_image_asset_id") or "")
        return await director_workbench_execute_candidate(project_id, req)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/studio/projects/{project_id}/facefusion")
async def studio_facefusion(project_id: str, payload: dict) -> dict:
    processor = str(payload.get("processor") or "").strip()
    target_input = str(payload.get("target_asset_id") or "").strip()
    source_input = str(payload.get("source_asset_id") or "").strip()
    if not processor or not target_input:
        raise HTTPException(status_code=400, detail="请选择处理方式和目标素材")
    try:
        target_asset = director.production.get_asset(project_id, target_input)
        output_type = str(target_asset.get("asset_type") or "IMAGE").upper()
        if output_type not in {"IMAGE", "VIDEO"}:
            raise ValueError("FaceFusion 目标必须是图片或视频")
        stage = str(target_asset.get("stage") or "04")
        parents = [target_input] + ([source_input] if source_input else [])
        target = _studio_new_target(
            project_id,
            asset_type=output_type,
            name=str(payload.get("name") or "人物处理结果"),
            role="character_consistency",
            stage=stage,
            skill="facefusion",
            parent_asset_ids=parents,
        )
        return await director_workbench_execute_candidate(project_id, {
            "target_asset_id": target["asset_id"],
            "capability": "facefusion",
            "processor": processor,
            "target_input_asset_id": target_input,
            "source_input_asset_id": source_input,
            "authorized_adult": True,
            "params": payload.get("params") if isinstance(payload.get("params"), dict) else {},
        })
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc



@app.post("/api/studio/projects/{project_id}/import-media")
async def studio_import_media(
    project_id: str,
    file: UploadFile = File(...),
    role: str = Form(default="project_media"),
    name: str = Form(default=""),
) -> dict:
    try:
        director.get_project(project_id)
        filename = _StudioPath(str(file.filename or "upload.bin")).name
        safe = _studio_re.sub(r"[^0-9A-Za-z._-]+", "_", filename).strip("._") or "upload.bin"
        out_dir = settings.data_dir / "studio_imports" / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / (_studio_secrets.token_hex(8) + "_" + safe[:120])
        size = 0
        with output.open("wb") as fp:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 8 * 1024 * 1024 * 1024:
                    raise ValueError("单个导入素材不能超过 8GB")
                fp.write(chunk)
        mime = str(file.content_type or _studio_mimetypes.guess_type(output.name)[0] or "application/octet-stream")
        if mime.startswith("video/"):
            asset_type = "VIDEO"
        elif mime.startswith("image/"):
            asset_type = "IMAGE"
        elif mime.startswith("audio/"):
            asset_type = "AUDIO"
        else:
            asset_type = "FILE"
        url = "/files/" + output.relative_to(settings.data_dir).as_posix()
        item = director.production.register_existing_file(
            project_id,
            stage="edit",
            skill="manju-studio",
            logical_key="studio:import:" + _studio_secrets.token_hex(10),
            asset_type=asset_type,
            asset_role=str(role or "project_media")[:80],
            name=str(name or filename)[:200],
            url=url,
            source={"type": "studio_upload", "original_name": filename},
            parent_asset_ids=[],
            entity_ids=[],
            metadata={"mime_type": mime, "size_bytes": size, "creator_ui": "manju_studio"},
        )
        return {"asset": item, "url": url}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/studio/projects/{project_id}/video-edit")
async def studio_video_edit(project_id: str, payload: dict) -> dict:
    try:
        director.get_project(project_id)
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in {"person", "background", "person_background"}:
            raise ValueError("视频编辑模式必须是 person、background 或 person_background")
        target_id = str(payload.get("target_asset_id") or "").strip()
        _studio_asset_media(project_id, target_id, {"VIDEO"}, "目标视频")
        person_id = str(payload.get("person_source_asset_id") or "").strip()
        background_id = str(payload.get("background_asset_id") or "").strip()
        if mode in {"person", "person_background"}:
            if payload.get("authorized_adult") is not True:
                raise ValueError("人物替换需要确认素材为本人、虚构人物或已获明确授权的成年人")
            _studio_asset_media(project_id, person_id, {"IMAGE"}, "人物参考")
        if mode in {"background", "person_background"}:
            _studio_asset_media(project_id, background_id, {"IMAGE", "VIDEO"}, "背景素材")
            caps = await facefusion.capabilities()
            br = caps.get("background_remover") if isinstance(caps, dict) else None
            if not isinstance(br, dict) or br.get("available") is False or "video" not in [str(x).lower() for x in (br.get("target_kinds") or [])]:
                raise RuntimeError("当前 FaceFusion background_remover 未提供视频背景分离能力")
            if not _studio_shutil.which("ffmpeg") or not _studio_shutil.which("ffprobe"):
                raise RuntimeError("背景替换需要 ffmpeg + ffprobe")
        latest = _studio_latest_video_edit_job(project_id)
        if latest and latest.get("status") in {"queued", "running"}:
            raise RuntimeError("当前项目已有视频编辑任务正在运行")
        if latest and latest.get("status") == "completed" and str(latest.get("review_status") or "pending") == "pending":
            raise RuntimeError("上一条视频改造结果尚未采用或丢弃，请先完成确认")

        person_params = payload.get("person_params") if isinstance(payload.get("person_params"), dict) else {}
        if not person_params:
            person_params = {
                "face_swapper_model": "hyperswap_1a_256",
                "face_swapper_pixel_boost": "512x512",
                "face_swapper_weight": 1.0,
                "face_selector_mode": "one",
                "face_mask_types": ["box"],
                "output_quality": 95,
            }
        remover_params = payload.get("background_remover_params") if isinstance(payload.get("background_remover_params"), dict) else {}
        if not remover_params:
            remover_params = {
                "background_remover_model": "rmbg_2.0",
                "background_remover_color": "255 0 255 255",
                "output_quality": 95,
            }
        job_id = "vedit_" + _studio_secrets.token_hex(10)
        job = {
            "job_id": job_id,
            "project_id": project_id,
            "mode": mode,
            "target_asset_id": target_id,
            "person_source_asset_id": person_id,
            "background_asset_id": background_id,
            "authorized_adult": bool(payload.get("authorized_adult")),
            "person_params": person_params,
            "background_remover_params": remover_params,
            "key_color": str(payload.get("key_color") or "0xFF00FF"),
            "key_similarity": float(payload.get("key_similarity") or 0.12),
            "key_blend": float(payload.get("key_blend") or 0.04),
            "name": str(payload.get("name") or "视频编辑结果"),
            "status": "queued",
            "message": "等待执行",
            "created_at": _studio_now(),
            "updated_at": _studio_now(),
        }
        _studio_save_video_edit_job(job)
        task = _studio_asyncio.create_task(_studio_video_edit_job(job_id))
        _STUDIO_VIDEO_EDIT_TASKS[job_id] = task
        return {"job": job, "background": True}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/studio/video-edit/jobs/{job_id}")
async def studio_video_edit_job(job_id: str) -> dict:
    try:
        return _studio_load_video_edit_job(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/studio/projects/{project_id}/video-edit/jobs/{job_id}/review")
async def studio_video_edit_review(project_id: str, job_id: str, payload: dict) -> dict:
    try:
        job = _studio_load_video_edit_job(job_id)
        if str(job.get("project_id") or "") != project_id:
            raise FileNotFoundError("视频编辑任务不属于当前项目")
        if str(job.get("status") or "") != "completed":
            raise ValueError("只有已完成的视频编辑结果可以审核")
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"accept", "reject"}:
            raise ValueError("action 必须是 accept 或 reject")
        asset_id = str(job.get("result_asset_id") or "").strip()
        if not asset_id:
            raise ValueError("视频编辑任务没有结果资产")
        if action == "accept":
            director.production.get_asset(project_id, asset_id)
            job["review_status"] = "accepted"
            job["message"] = "视频编辑结果已采用"
        else:
            try:
                director.production.archive_asset(project_id, asset_id)
            except FileNotFoundError:
                pass
            job["review_status"] = "rejected"
            job["message"] = "视频编辑结果已丢弃"
        job["updated_at"] = _studio_now()
        _studio_save_video_edit_job(job)
        return job
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/studio/projects/{project_id}/assemble")
async def studio_assemble(project_id: str, payload: dict) -> dict:
    if not _studio_shutil.which("ffmpeg"):
        raise HTTPException(status_code=409, detail="系统未检测到 ffmpeg")
    if not _studio_shutil.which("ffprobe"):
        raise HTTPException(status_code=409, detail="系统未检测到 ffprobe")
    try:
        _studio_v23963_recover_project(project_id)
        _studio_v23963_assert_no_active_rebuild(project_id)
        state = story_continuity.load(project_id)
        shots = [
            x for x in (state.get("shots") or [])
            if not bool(x.get("provisional")) and str(x.get("shot_id") or "")
        ]
        shots.sort(key=lambda x: int(x.get("global_order") or x.get("sequence") or x.get("order") or 0))
        if not shots:
            raise ValueError("没有正式分镜镜头，不能生成成片")

        expected_assets = []
        for shot in shots:
            sid = str(shot.get("shot_id") or "")
            video = _studio_latest_shot_asset(project_id, sid, "VIDEO")
            if video is None:
                order = int(shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0)
                raise ValueError(f"镜头 {order:03d} 没有当前有效视频；可能缺失、stale 或与当前画面血缘不一致")
            expected_assets.append(video)
        expected_ids = [str(x.get("asset_id") or "") for x in expected_assets]
        requested = [str(x or "").strip() for x in (payload.get("asset_ids") or []) if str(x or "").strip()]
        if requested != expected_ids:
            raise ValueError("成片输入不是当前正式镜头的有效视频版本，已拒绝旧版本/乱序输入；请刷新⑥成片页面")
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError("多个正式镜头错误映射到同一视频资产，拒绝合成")

        aspect = str(payload.get("aspect_ratio") or "16:9").strip()
        if aspect not in {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}:
            aspect = "16:9"
        export_profile = str(payload.get("export_profile") or "working").strip().lower()
        if export_profile not in {"working", "1080p", "4k"}:
            export_profile = "working"
        width, height = _studio_final_dimensions(aspect, export_profile)

        paths: list[_StudioPath] = []
        input_manifest = []
        for shot, item in zip(shots, expected_assets):
            url = director.production.asset_url(project_id, item["asset_id"])
            path = assets.resolve_asset_url(url)
            if not path.is_file():
                raise FileNotFoundError(f"视频文件不存在：{url}")
            probe = await _studio_asyncio.to_thread(_studio_probe_video_file, path)
            if not probe.get("width") or not probe.get("height") or probe.get("duration", 0) <= 0:
                raise RuntimeError(f"视频媒体信息异常：{item['asset_id']}")
            paths.append(path)
            input_manifest.append({
                "shot_id": str(shot.get("shot_id") or ""),
                "global_order": int(shot.get("global_order") or shot.get("sequence") or shot.get("order") or 0),
                "asset_id": str(item.get("asset_id") or ""),
                "probe": probe,
            })

        out_dir = settings.data_dir / "studio_finals" / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _studio_datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output = out_dir / f"final_{stamp}.mp4"
        temp_dir = out_dir / f".assemble_{stamp}"
        temp_dir.mkdir(parents=True, exist_ok=False)
        normalized = []
        try:
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                "fps=24,setsar=1,format=yuv420p"
            )
            for idx, (path, manifest) in enumerate(zip(paths, input_manifest), 1):
                norm = temp_dir / f"clip_{idx:04d}.mp4"
                has_audio = bool((manifest.get("probe") or {}).get("has_audio"))
                cmd = ["ffmpeg", "-y", "-i", str(path)]
                if has_audio:
                    cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
                else:
                    cmd += [
                        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-map", "0:v:0", "-map", "1:a:0",
                    ]
                cmd += [
                    "-vf", vf,
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                    "-movflags", "+faststart",
                ]
                if not has_audio:
                    cmd += ["-shortest"]
                cmd += [str(norm)]
                proc = await _studio_asyncio.to_thread(
                    _studio_subprocess.run, cmd, capture_output=True, text=True
                )
                if proc.returncode != 0 or not norm.is_file() or norm.stat().st_size < 1024:
                    raise RuntimeError(
                        f"镜头 {manifest['global_order']:03d} 标准化失败："
                        + (proc.stderr or proc.stdout)[-1800:]
                    )
                normalized.append(norm)

            concat_file = temp_dir / "concat.txt"
            def _q(path: _StudioPath) -> str:
                return str(path).replace("'", "'\\''")
            concat_file.write_text(
                "\n".join(f"file '{_q(p)}'" for p in normalized) + "\n",
                encoding="utf-8",
            )
            proc = await _studio_asyncio.to_thread(
                _studio_subprocess.run,
                [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_file), "-c", "copy", "-movflags", "+faststart", str(output),
                ],
                capture_output=True, text=True,
            )
            if proc.returncode != 0 or not output.is_file() or output.stat().st_size < 1024:
                raise RuntimeError("标准化视频 concat 失败：" + (proc.stderr or proc.stdout)[-1800:])
        finally:
            _studio_shutil.rmtree(temp_dir, ignore_errors=True)

        final_probe = await _studio_asyncio.to_thread(_studio_probe_video_file, output)
        if (
            int(final_probe.get("width") or 0) != width
            or int(final_probe.get("height") or 0) != height
            or abs(float(final_probe.get("fps") or 0.0) - 24.0) > 0.2
        ):
            raise RuntimeError("成片输出媒体参数验收失败：" + _studio_json.dumps(final_probe, ensure_ascii=False))

        url = "/files/" + output.relative_to(settings.data_dir).as_posix()
        item = director.production.register_existing_file(
            project_id,
            stage="final", skill="manju-studio", logical_key="studio:final_cut",
            asset_type="VIDEO", asset_role="final_cut",
            name=str(payload.get("name") or "最终成片"), url=url,
            source={"type": "studio_assembly", "mode": "normalized_concat"},
            parent_asset_ids=expected_ids, entity_ids=[],
            metadata={
                "assembly_mode": "normalized_concat",
                "clip_count": len(expected_ids),
                "aspect_ratio": aspect,
                "export_profile": export_profile,
                "target_width": width,
                "target_height": height,
                "fps": 24,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate": 48000,
                "input_manifest": input_manifest,
                "final_probe": final_probe,
                "upscale_method": "ffmpeg_lanczos_non_ai" if export_profile in {"1080p", "4k"} else "working_resolution_normalization",
            },
        )
        for parent in expected_ids:
            director.production.add_relation(
                project_id, source_id=parent, target_id=item["asset_id"],
                relation_type="input_to", metadata={"operation": "final_assembly"},
            )
        return {
            "asset": item, "url": url, "mode": "normalized_concat",
            "profile": export_profile, "aspect_ratio": aspect,
            "width": width, "height": height, "fps": 24,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

# ===== /V2.28 MANJU STUDIO PRODUCT APIs =====

# ===== V2.36.1A PROJECT MEDIA INPUT RESOLVER =====
from pathlib import Path as _StudioMediaPath
from urllib.parse import urlparse as _studio_media_urlparse, unquote as _studio_media_unquote
import mimetypes as _studio_media_mimetypes


def _studio_resolve_platform_media_url(url: str) -> _StudioMediaPath:
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("输入项目资产没有可用文件 URL")

    parsed = _studio_media_urlparse(raw)
    path_text = (
        parsed.path
        if (parsed.scheme or parsed.netloc)
        else raw.split("?", 1)[0].split("#", 1)[0]
    )

    if path_text.startswith("/files/"):
        relative = _studio_media_unquote(path_text[len("/files/"):]).lstrip("/")
        if not relative:
            raise ValueError("输入项目资产文件路径为空")

        root = _StudioMediaPath(settings.data_dir).resolve()
        path = (root / relative).resolve()

        if path != root and root not in path.parents:
            raise ValueError("输入项目资产文件路径越界")
    else:
        path = assets.resolve_asset_url(raw).resolve()

    if not path.is_file():
        raise FileNotFoundError(f"输入项目资产文件不存在：{raw}")

    return path


def _wb_validate_media_asset(
    project_id: str,
    asset_id: str,
    allowed: set[str],
    label: str,
) -> None:
    if not asset_id:
        raise ValueError(f"{label}不能为空")

    item = director.production.get_asset(project_id, asset_id)

    if str(item.get("status") or "").strip().lower() != "ready":
        raise ValueError(f"{label}必须是 READY 项目资产")

    if str(item.get("dependency_state") or "").strip().lower() == "stale":
        raise ValueError(f"{label}已 STALE，请先更新")

    kind = str(item.get("asset_type") or "").strip().upper()
    if kind not in allowed:
        raise ValueError(f"{label}类型必须是：{','.join(sorted(allowed))}")

    url = director.production.asset_url(project_id, asset_id)
    if not url:
        raise ValueError(f"{label}没有可用文件")

    _studio_resolve_platform_media_url(url)


def _production_asset_file(
    project_id: str,
    asset_id: str,
    field_name: str,
) -> tuple[str, tuple[str, bytes, str]]:
    url = director.production.asset_url(project_id, asset_id)
    if not url:
        raise ValueError(f"输入资产没有可用文件：{asset_id}")

    path = _studio_resolve_platform_media_url(url)
    mime = _studio_media_mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    return asset_id, (path.name, path.read_bytes(), mime)
# ===== /V2.36.1A PROJECT MEDIA INPUT RESOLVER =====


# ===== V2.37.1A STAGE04 QWEN RUNTIME + STRUCTURED OUTPUT RESILIENCE =====
import json as _studio_v2371a_json
import re as _studio_v2371a_re


def _studio_v2371a_collect_texts(value: object, depth: int = 0) -> list[str]:
    if depth > 7:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        result = []
        priority = (
            "content", "text", "response", "output",
            "message", "raw", "result", "body", "data",
        )
        seen = set()
        for key in priority:
            if key in value:
                seen.add(key)
                result.extend(
                    _studio_v2371a_collect_texts(
                        value.get(key), depth + 1
                    )
                )
        for key, item in value.items():
            if key in seen:
                continue
            result.extend(
                _studio_v2371a_collect_texts(item, depth + 1)
            )
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(
                _studio_v2371a_collect_texts(item, depth + 1)
            )
        return result
    return []


def _studio_v2371a_balanced_json(text: str) -> list[str]:
    text = str(text or "")
    candidates = []

    # Markdown JSON fences first.
    for match in _studio_v2371a_re.finditer(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        flags=_studio_v2371a_re.S | _studio_v2371a_re.I,
    ):
        candidates.append(match.group(1))

    # Balanced JSON object extraction, string/escape aware.
    starts = [
        idx for idx, char in enumerate(text)
        if char == "{"
    ]
    for start in starts[:12]:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:idx + 1])
                    break
                if depth < 0:
                    break

    result = []
    seen = set()
    for item in candidates:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _studio_v2371a_extract_shots(value: object) -> list[dict]:
    if isinstance(value, dict):
        rows = value.get("shots")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]

    for text in _studio_v2371a_collect_texts(value):
        if '"shots"' not in text and "'shots'" not in text:
            continue

        direct = text.strip()
        if direct.startswith("{") and direct.endswith("}"):
            candidates = [direct]
        else:
            candidates = []
        candidates.extend(
            _studio_v2371a_balanced_json(text)
        )

        for candidate in candidates:
            try:
                parsed = _studio_v2371a_json.loads(candidate)
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue
            rows = parsed.get("shots")
            if isinstance(rows, list):
                clean = [
                    row for row in rows
                    if isinstance(row, dict)
                ]
                if clean:
                    return clean
    return []


def _studio_v2371a_row_completeness(row: dict) -> tuple[bool, list[str]]:
    if not isinstance(row, dict):
        return False, ["not_object"]

    required = (
        "representative_state",
        "video_start_state",
        "video_end_state",
        "image_prompt",
        "video_start_prompt",
        "video_prompt",
    )
    missing = [
        key for key in required
        if not str(row.get(key) or "").strip()
    ]
    if not (
        str(row.get("summary") or "").strip()
        or str(row.get("action") or "").strip()
    ):
        missing.append("summary_or_action")

    beat_value = row.get("covered_beat_orders")
    if not isinstance(beat_value, list):
        missing.append("covered_beat_orders_type")

    evidence_value = row.get("source_evidence_ids")
    if not isinstance(evidence_value, list):
        missing.append("source_evidence_ids_type")

    return not missing, missing


def _studio_v2371a_rows_usable(rows: object) -> tuple[bool, str]:
    if not isinstance(rows, list) or not rows:
        return False, "shots 为空"

    failures = []
    valid_count = 0
    for idx, row in enumerate(rows, 1):
        ok, missing = _studio_v2371a_row_completeness(row)
        if ok:
            valid_count += 1
        else:
            failures.append(
                f"shot#{idx} missing={','.join(missing)}"
            )

    if valid_count == len(rows):
        return True, f"{valid_count}/{len(rows)} 完整"
    return (
        False,
        f"完整={valid_count}/{len(rows)}；"
        + " | ".join(failures[:4]),
    )


async def _studio_v2371_generate_batch(
    *,
    system_prompt: str,
    prompt: str,
    scene_index: int,
    scene_total: int,
    batch_index: int,
    batch_total: int,
) -> list[dict]:
    diagnostics = []

    attempts = (
        (
            0.10,
            "",
        ),
        (
            0.02,
            "\n\nSTRICT_OUTPUT_RETRY:\n"
            "上一轮结构输出不可用。只输出一个 JSON 对象，不要 Markdown、"
            "不要解释、不要前后缀。根对象必须只有 shots。"
            "shots 中每一项必须显式填写 summary 或 action，并填写："
            "representative_state、video_start_state、video_end_state、"
            "image_prompt、video_start_prompt、video_prompt、"
            "covered_beat_orders、source_evidence_ids。"
            "绝不能返回空字符串模板。",
        ),
        (
            0.0,
            "\n\nFINAL_JSON_RETRY:\n"
            "返回可直接 json.loads 的严格 JSON。"
            "禁止输出字段模板或示例；必须填写真实当前分镜内容。"
            "若当前批次有 Beat，每个 Shot 必须显式绑定 Beat。"
            "若无法满足，不要返回空 Shot。",
        ),
    )

    for attempt, (temperature, suffix) in enumerate(
        attempts, 1
    ):
        raw = None
        parsed = None
        try:
            raw, parsed, meta = await _studio_v2371a_qwen_call(
                phase="studio_stage04_strict_contract_qwen32b",
                messages=[{
                    "role": "user",
                    "content": prompt + suffix,
                }],
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=2400,
                contract=_studio_v2371_batch_schema(),
            )

            # 1. Trust parsed only if semantically complete.
            parsed_rows = (
                parsed.get("shots")
                if isinstance(parsed, dict)
                else None
            )
            usable, reason = _studio_v2371a_rows_usable(
                parsed_rows
            )
            if usable:
                return parsed_rows

            # 2. _structured_json_call may have returned its empty
            # contract skeleton after a parser/format recovery.
            # Recover actual JSON from the raw model response instead.
            raw_rows = _studio_v2371a_extract_shots(raw)
            raw_usable, raw_reason = (
                _studio_v2371a_rows_usable(raw_rows)
            )
            if raw_usable:
                return raw_rows

            raw_preview = ""
            texts = _studio_v2371a_collect_texts(raw)
            if texts:
                raw_preview = _studio_v2371a_re.sub(
                    r"\s+",
                    " ",
                    max(texts, key=len),
                )[:360]

            diagnostics.append(
                f"attempt={attempt}: "
                f"parsed=({reason}); raw=({raw_reason}); "
                f"raw_preview={raw_preview!r}"
            )
        except Exception as exc:
            diagnostics.append(
                f"attempt={attempt}: "
                f"{type(exc).__name__}: {str(exc)[:420]}"
            )

    raise RuntimeError(
        f"场景 {scene_index}/{scene_total} 批次 "
        f"{batch_index + 1}/{batch_total} "
        "Qwen3-32B 未返回可验证的严格 Shot 合同；"
        + " || ".join(diagnostics)
    )


async def _studio_v2371_audit_batch(
    *,
    source_window: str,
    compact_beats: list[dict],
    shots: list[dict],
) -> dict:
    audit_rows = [{
        "index": i + 1,
        "title": row.get("title"),
        "covered_beat_orders": row.get("covered_beat_orders"),
        "summary": row.get("summary"),
        "action": row.get("action"),
        "representative_state": row.get("representative_state"),
        "video_start_state": row.get("video_start_state"),
        "video_end_state": row.get("video_end_state"),
        "source_evidence": row.get("source_evidence"),
        "character_entity_ids": row.get("character_entity_ids"),
        "prop_entity_ids": row.get("prop_entity_ids"),
    } for i, row in enumerate(shots)]

    system_prompt = (
        "你是正式分镜时间边界审计器，只审计不改写。"
        "检查 Beat 显式覆盖、镜头按原文时间单调前进、"
        "不提前消费后续 Beat、拆分镜头不重复已完成结果、"
        "video_start_state→representative_state→video_end_state 因果成立、"
        "representative_state 具有当前 Shot 叙事信息而不是通用肖像、"
        "人物和道具只在当前 Shot 实际可见时出现。"
        "只返回严格 JSON。"
    )
    prompt = (
        "=== ORIGINAL_SOURCE_WINDOW ===\n"
        + source_window
        + "\n\n=== BEATS ===\n"
        + _studio_json.dumps(
            compact_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SHOTS ===\n"
        + _studio_json.dumps(
            audit_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    _, audit, _ = await _studio_v2371a_qwen_call(
        phase="studio_stage04_strict_temporal_audit_qwen32b",
        messages=[{"role": "user", "content": prompt}],
        system_prompt=system_prompt,
        temperature=0.0,
        max_tokens=700,
        contract=(
            '{"valid":true,"beat_coverage_ok":true,'
            '"temporal_monotonic":true,'
            '"no_future_event_preconsumption":true,'
            '"no_result_duplication":true,'
            '"state_order_valid":true,'
            '"entity_visibility_valid":true,"issues":[]}'
        ),
    )
    return audit if isinstance(audit, dict) else {}


_ORIGINAL_V2371_VALIDATE_ROWS = _studio_v2371_validate_rows


def _studio_v2371_validate_rows(
    *,
    raw_rows: list[dict],
    compact_beats: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    anchors: list[dict],
    scene_id: str,
    episode_id: str,
) -> list[dict]:
    # Fail descriptively instead of silently filtering an all-empty
    # structured-output skeleton.
    if not isinstance(raw_rows, list) or not raw_rows:
        raise RuntimeError(
            "严格 Stage04：Qwen3-32B 返回的 shots 为空"
        )

    diagnostics = []
    for idx, row in enumerate(raw_rows, 1):
        ok, missing = _studio_v2371a_row_completeness(
            row if isinstance(row, dict) else {}
        )
        if not ok:
            diagnostics.append(
                f"shot#{idx} missing={','.join(missing)}"
            )

    if len(diagnostics) == len(raw_rows):
        raise RuntimeError(
            "严格 Stage04：Qwen3-32B 返回了 Shot 容器，"
            "但所有 Shot 都是空模板/不完整结构；"
            + " | ".join(diagnostics[:6])
        )

    return _ORIGINAL_V2371_VALIDATE_ROWS(
        raw_rows=raw_rows,
        compact_beats=compact_beats,
        allowed_chars=allowed_chars,
        allowed_props=allowed_props,
        anchors=anchors,
        scene_id=scene_id,
        episode_id=episode_id,
    )
# ===== /V2.37.1A STAGE04 QWEN RUNTIME + STRUCTURED OUTPUT RESILIENCE =====


# ===== V2.37.1B STAGE04 SOURCE BEAT RECOVERY + AUDIT NORMALIZATION =====
import json as _studio_v2371b_json
import re as _studio_v2371b_re


def _studio_v2371b_extract_json_objects(value: object) -> list[dict]:
    result = []
    seen = set()

    if isinstance(value, dict):
        result.append(value)

    texts = []
    collect = globals().get("_studio_v2371a_collect_texts")
    if collect is not None:
        texts.extend(collect(value))
    elif isinstance(value, str):
        texts.append(value)

    balanced = globals().get("_studio_v2371a_balanced_json")
    for text in texts:
        candidates = []
        raw = str(text or "").strip()
        if raw.startswith("{") and raw.endswith("}"):
            candidates.append(raw)
        if balanced is not None:
            candidates.extend(balanced(raw))

        for candidate in candidates:
            try:
                parsed = _studio_v2371b_json.loads(candidate)
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue
            key = _studio_json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(parsed)

    return result


def _studio_v2371b_extract_beats(value: object) -> list[dict]:
    for parsed in _studio_v2371b_extract_json_objects(value):
        rows = parsed.get("beats")
        if isinstance(rows, list):
            clean = [
                row for row in rows
                if isinstance(row, dict)
            ]
            if clean:
                return clean
    return []


def _studio_v2371b_source_chunks(
    source: str,
    *,
    max_chars: int = 1800,
    overlap: int = 160,
) -> list[str]:
    text = str(source or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    result = []
    start = 0
    while start < len(text):
        tentative_end = min(len(text), start + max_chars)
        end = tentative_end

        if tentative_end < len(text):
            region = text[
                max(start + max_chars // 2, tentative_end - 260):
                tentative_end
            ]
            matches = list(
                _studio_v2371b_re.finditer(
                    r"[。！？!?；;\n]",
                    region,
                )
            )
            if matches:
                base = max(
                    start + max_chars // 2,
                    tentative_end - 260,
                )
                end = base + matches[-1].end()

        chunk = text[start:end].strip()
        if chunk:
            result.append(chunk)

        if end >= len(text):
            break
        start = max(start + 1, end - max(0, overlap))

        if len(result) >= 12:
            break

    return result


def _studio_v2371b_normalize_summary(text: str) -> str:
    return _studio_v2371b_re.sub(
        r"[\s\W_]+",
        "",
        str(text or "").lower(),
        flags=_studio_v2371b_re.UNICODE,
    )


async def _studio_v2371b_generate_beats_for_chunk(
    *,
    chunk: str,
    chunk_index: int,
    chunk_total: int,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> list[dict]:
    anchors = _studio_v2371_evidence_anchors(chunk)
    if not anchors:
        return []
    anchor_map = _studio_v2371_anchor_map(anchors)

    allowed_entity_text = _studio_json.dumps(
        entity_rows,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    allowed_entity_text = _studio_v2371_cut(
        allowed_entity_text,
        700,
    )

    system_prompt = (
        "你是小说正文的原子剧情 Beat 提取器，文本模型为 Qwen3-32B。"
        "这里只做事实拆解，不做分镜、不添加剧情。"
        "按 ORIGINAL_SOURCE_CHUNK 的原始时间顺序提取可被后续镜头消费的原子 Beat。"
        "每个 Beat 表示一个明确的状态变化、动作推进、信息揭示或角色行为；"
        "不要把相隔较远的事件压缩成一个 Beat，也不要提前总结后续结果。"
        "source_evidence_ids 必须从给定 SOURCE_EVIDENCE_ANCHORS 选择。"
        "character_entity_ids / prop_entity_ids 只填写该 Beat 明确涉及且在 ALLOWED_ENTITIES 中存在的实体；"
        "无法确认就留空，禁止整个 Scene 兜底。"
        "不要输出章节标题或目录文字作为 Beat。"
        "只输出严格 JSON。"
    )
    prompt = (
        f"CHUNK_PROGRESS={chunk_index}/{chunk_total}\n"
        "=== ORIGINAL_SOURCE_CHUNK ===\n"
        + chunk
        + "\n\n=== SOURCE_EVIDENCE_ANCHORS ===\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + allowed_entity_text
    )
    schema = (
        '{"beats":[{'
        '"summary":"",'
        '"source_evidence_ids":["E001"],'
        '"character_entity_ids":[],'
        '"prop_entity_ids":[]'
        '}]}'
    )

    diagnostics = []
    for attempt, suffix in enumerate((
        "",
        "\n\nRETRY：只输出真实 Beat JSON；禁止空 beats、空 summary、示例模板或解释。",
    ), 1):
        raw = None
        parsed = None
        try:
            raw, parsed, _ = await _studio_v2371a_qwen_call(
                phase="studio_stage04_source_beat_extraction_qwen32b",
                messages=[{
                    "role": "user",
                    "content": prompt + suffix,
                }],
                system_prompt=system_prompt,
                temperature=0.08 if attempt == 1 else 0.0,
                max_tokens=1300,
                contract=schema,
            )

            rows = (
                parsed.get("beats")
                if isinstance(parsed, dict)
                else None
            )
            if not isinstance(rows, list) or not rows:
                rows = _studio_v2371b_extract_beats(raw)

            cleaned = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                summary = str(
                    row.get("summary") or ""
                ).strip()
                if not summary:
                    continue

                ids = []
                evidence_text = []
                for value in (
                    row.get("source_evidence_ids") or []
                ):
                    key = str(value or "").strip()
                    if (
                        key
                        and key in anchor_map
                        and key not in ids
                    ):
                        ids.append(key)
                        evidence_text.append(
                            anchor_map[key]
                        )
                    if len(ids) >= 3:
                        break
                if not ids:
                    continue

                cleaned.append({
                    "summary": summary[:500],
                    "source_evidence_ids": ids,
                    "source_evidence": evidence_text,
                    "character_entity_ids":
                        _studio_v2371_clean_ids(
                            row.get(
                                "character_entity_ids"
                            ),
                            allowed_chars,
                        ),
                    "prop_entity_ids":
                        _studio_v2371_clean_ids(
                            row.get(
                                "prop_entity_ids"
                            ),
                            allowed_props,
                        ),
                })

            if cleaned:
                return cleaned

            diagnostics.append(
                f"attempt={attempt}: no usable beats"
            )
        except Exception as exc:
            diagnostics.append(
                f"attempt={attempt}: "
                f"{type(exc).__name__}: {str(exc)[:260]}"
            )

    raise RuntimeError(
        f"正文 Beat 提取失败（chunk "
        f"{chunk_index}/{chunk_total}）："
        + " | ".join(diagnostics)
    )


async def _studio_v2371b_ensure_scene_beats(
    *,
    project_id: str,
    scene: dict,
    state: dict,
    source: str,
    allowed_chars: set[str],
    allowed_props: set[str],
) -> tuple[list[dict], str]:
    scene_id = str(scene.get("scene_id") or "")
    existing = _studio_stage04_scene_beats(
        state,
        scene_id,
    )
    if existing:
        return existing, "continuity-provisional"

    if not str(source or "").strip():
        raise RuntimeError(
            "当前 Scene 既没有 provisional Beats，"
            "也没有可用于重新提取 Beats 的小说正文"
        )

    entities = {
        str(row.get("entity_id") or ""): {
            "entity_id": str(row.get("entity_id") or ""),
            "entity_type": str(
                row.get("entity_type") or ""
            ),
            "name": str(row.get("name") or ""),
        }
        for row in director.production.list_entities(
            project_id
        )
        if str(row.get("entity_id") or "")
    }
    visible_ids = [
        *sorted(allowed_chars),
        *sorted(allowed_props),
    ]
    entity_rows = [
        entities[eid]
        for eid in visible_ids
        if eid in entities
    ]

    chunks = _studio_v2371b_source_chunks(
        source,
        max_chars=1800,
        overlap=160,
    )
    if not chunks:
        raise RuntimeError(
            "小说 Scene 正文无法建立 Beat 提取窗口"
        )

    gathered = []
    seen = set()

    for index, chunk in enumerate(chunks, 1):
        rows = await _studio_v2371b_generate_beats_for_chunk(
            chunk=chunk,
            chunk_index=index,
            chunk_total=len(chunks),
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
            entity_rows=entity_rows,
        )

        for row in rows:
            fingerprint = (
                _studio_v2371b_normalize_summary(
                    row.get("summary") or ""
                )
            )
            if not fingerprint:
                continue

            # Chunk overlap can reproduce one boundary event.
            # Exact normalized duplicates are ignored; no semantic
            # keyword rules or story-specific heuristics are used.
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            gathered.append(row)

    if not gathered:
        raise RuntimeError(
            "Qwen3-32B 没有从当前 Scene 正文提取出有效 Beats"
        )

    beats = []
    for index, row in enumerate(gathered, 1):
        beats.append({
            "order": index,
            "summary": str(
                row.get("summary") or ""
            )[:700],
            "character_entity_ids": list(
                row.get("character_entity_ids") or []
            ),
            "prop_entity_ids": list(
                row.get("prop_entity_ids") or []
            ),
            "source_evidence_ids": list(
                row.get("source_evidence_ids") or []
            ),
            "source_evidence": list(
                row.get("source_evidence") or []
            ),
            "beat_source": "qwen3-32b-source-derived",
        })

    # Insert only into this in-memory rebuild state.
    # If rebuild fails, nothing is persisted. If rebuild succeeds,
    # _studio_stage04_replace_formal_shots replaces all in-scope
    # provisional rows with strict formal shots before state is saved.
    state_rows = state.setdefault("shots", [])
    for beat in beats:
        state_rows.append({
            "shot_id": (
                "beat_runtime_"
                + str(scene_id)
                + "_"
                + str(beat["order"])
            ),
            "scene_id": scene_id,
            "episode_id": str(
                scene.get("episode_id") or ""
            ),
            "order": int(beat["order"]),
            "sequence": int(
                scene.get("sequence") or 0
            ) * 1000 + int(beat["order"]),
            "summary": beat["summary"],
            "character_entity_ids": list(
                beat.get("character_entity_ids") or []
            ),
            "prop_entity_ids": list(
                beat.get("prop_entity_ids") or []
            ),
            "source_evidence_ids": list(
                beat.get("source_evidence_ids") or []
            ),
            "source_evidence": list(
                beat.get("source_evidence") or []
            ),
            "beat_source": "qwen3-32b-source-derived",
            "provisional": True,
        })

    return beats, "qwen3-32b-source-derived"


def _studio_v2371_audit_ok(audit: dict) -> bool:
    if not isinstance(audit, dict):
        return False

    strict_keys = (
        "valid",
        "beat_coverage_ok",
        "temporal_monotonic",
        "no_future_event_preconsumption",
        "no_result_duplication",
        "state_order_valid",
        "entity_visibility_valid",
    )
    if all(audit.get(key) is True for key in strict_keys):
        return True

    # Qwen3-32B sometimes returns the same audit decision using
    # aggregate fields despite the requested schema.
    # Accept only an explicit pass with zero violations.
    if audit.get("audit_passed") is True:
        violations = audit.get("violations")
        if violations in (None, [], {}):
            return True

    return False


async def _studio_stage04_scene_shots(
    *, project_id: str, scene: dict, state: dict, source_text: str,
    upstream: dict, user_input: str, scene_index: int, scene_total: int,
) -> list[dict]:
    scene_id = str(scene.get("scene_id") or "")
    resolved = story_continuity.resolve_scene(
        project_id,
        scene_id,
    )
    source = _studio_stage04_scene_source(
        scene,
        source_text,
    )
    allowed_chars, allowed_props = (
        _studio_stage04_allowed_ids(
            scene,
            resolved,
        )
    )

    beats, beat_source = (
        await _studio_v2371b_ensure_scene_beats(
            project_id=project_id,
            scene=scene,
            state=state,
            source=source,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
        )
    )
    if not beats:
        raise RuntimeError(
            "严格 Stage04：当前 Scene 没有 Beats；"
            "拒绝无 Beat 直接生成正式 Shot"
        )

    entities = {
        str(x.get("entity_id") or ""): {
            "entity_id": str(
                x.get("entity_id") or ""
            ),
            "entity_type": str(
                x.get("entity_type") or ""
            ),
            "name": str(x.get("name") or ""),
        }
        for x in director.production.list_entities(
            project_id
        )
        if str(x.get("entity_id") or "")
    }
    scene_entities = [
        entities[eid]
        for eid in [
            *sorted(allowed_chars),
            *sorted(allowed_props),
        ]
        if eid in entities
    ]
    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            scene_entities,
            ensure_ascii=False,
        ),
        700,
    )

    resolved_compact = {
        "location": resolved.get("location"),
        "characters": resolved.get("characters"),
        "props": resolved.get("props"),
        "scene_state": resolved.get("scene_state"),
    }
    resolved_text = _studio_v2371_cut(
        _studio_json.dumps(
            resolved_compact,
            ensure_ascii=False,
        ),
        760,
    )
    character_anchor = _studio_v2371_cut(
        upstream.get("character_bible"),
        1100,
    )
    visual_anchor = _studio_v2371_cut(
        upstream.get("visual_bible"),
        900,
    )

    beat_batches = [
        beats[i:i + 3]
        for i in range(0, len(beats), 3)
    ]

    system_prompt = (
        "你是正式短视频分镜导演，运行文本模型为 Qwen3-32B。"
        "小说正文和明确 Beats 是最高优先级事实。"
        "当前必须生成可直接进入图片/视频制作的 strict-shot-v2 合同。"
        "硬规则："
        "1. 每个 Shot 的 covered_beat_orders 必须非空，"
        "且只能引用当前 BEATS_THIS_BATCH；"
        "2. 每个当前 Beat 必须至少被一个 Shot 显式覆盖；"
        "禁止镜头数量兜底；"
        "3. source_evidence_ids 必须选择当前正文证据锚点；"
        "4. character_entity_ids / prop_entity_ids 只填写画面真实可见实体，"
        "不确定就留空，禁止整个 Scene 兜底；"
        "5. representative_state 是当前 Shot 最有叙事信息的单帧，"
        "不得退化成无因果信息的通用人物/物体肖像；"
        "6. video_start_state 是当前 Shot 第一动作发生前或刚开始；"
        "video_end_state 是当前 Shot 自己结束状态；"
        "7. 不得提前消费后续 Beat 的主要事件；"
        "8. image_prompt 只描述 representative_state；"
        "video_start_prompt 只描述 video_start_state；"
        "video_prompt 只描述 video_start_state 到 video_end_state 的前向变化；"
        "9. 只依据当前原文、Beat、Scene Fact、允许实体及已确认角色/视觉锚点。"
        "只输出严格 JSON。"
    )

    all_rows = []
    seen_fingerprints = set()

    for batch_index, batch in enumerate(
        beat_batches
    ):
        source_window = _studio_stage04_source_window(
            source,
            batch_index,
            len(beat_batches),
            max_chars=1500,
        )
        anchors = _studio_v2371_evidence_anchors(
            source_window
        )
        if not anchors:
            raise RuntimeError(
                f"场景 {scene_index}/{scene_total} "
                f"批次 {batch_index + 1} 无正文证据锚点"
            )

        compact_beats = [{
            "order": int(row.get("order") or 0),
            "summary": str(
                row.get("summary") or ""
            )[:260],
            "character_entity_ids": list(
                row.get("character_entity_ids") or []
            ),
            "prop_entity_ids": list(
                row.get("prop_entity_ids") or []
            ),
        } for row in batch]

        batch_target = max(
            1,
            len(compact_beats) * 2,
        )

        base_prompt = (
            f"SCENE_PROGRESS={scene_index}/{scene_total}\n"
            f"BATCH_PROGRESS="
            f"{batch_index + 1}/{len(beat_batches)}\n"
            f"BEAT_SOURCE={beat_source}\n"
            f"SCENE_ID={scene_id}\n"
            f"SCENE_TITLE="
            f"{str(scene.get('title') or '')[:160]}\n"
            f"SCENE_SUMMARY="
            f"{str(scene.get('summary') or '')[:340]}\n"
            f"TARGET_SHOTS≈{batch_target}；"
            "按实际视觉变化决定，不得合并掉独立状态变化。\n\n"
            "=== ORIGINAL_SCENE_SOURCE_WINDOW ===\n"
            + source_window
            + "\n\n=== SOURCE_EVIDENCE_ANCHORS ===\n"
            + _studio_json.dumps(
                anchors,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== BEATS_THIS_BATCH ===\n"
            + _studio_json.dumps(
                compact_beats,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== CONTINUITY ===\n"
            + resolved_text
            + "\n\n=== ALLOWED_ENTITIES ===\n"
            + entity_text
            + "\n\n=== CHARACTER_ANCHOR ===\n"
            + (character_anchor or "<none>")
            + "\n\n=== VISUAL_ANCHOR ===\n"
            + (visual_anchor or "<none>")
            + "\n\n=== USER_REQUIREMENT ===\n"
            + _studio_v2371_cut(
                user_input,
                300,
            )
        )

        accepted = None
        final_audit = None
        repair_issues = ""

        for round_index in range(2):
            prompt = base_prompt
            if repair_issues:
                prompt += (
                    "\n\n=== PREVIOUS_AUDIT_ISSUES ===\n"
                    + repair_issues
                    + "\n重新生成整个当前批次，"
                    "不要仅修改说明文字。"
                )

            raw_rows = await _studio_v2371_generate_batch(
                system_prompt=system_prompt,
                prompt=prompt,
                scene_index=scene_index,
                scene_total=scene_total,
                batch_index=batch_index,
                batch_total=len(beat_batches),
            )

            rows = _studio_v2371_validate_rows(
                raw_rows=raw_rows,
                compact_beats=compact_beats,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                anchors=anchors,
                scene_id=scene_id,
                episode_id=str(
                    scene.get("episode_id") or ""
                ),
            )

            audit = await _studio_v2371_audit_batch(
                source_window=source_window,
                compact_beats=compact_beats,
                shots=rows,
            )
            if _studio_v2371_audit_ok(audit):
                accepted = rows
                final_audit = audit
                break

            repair_issues = _studio_json.dumps(
                audit.get("issues")
                or audit.get("violations")
                or audit,
                ensure_ascii=False,
            )

        if accepted is None:
            raise RuntimeError(
                f"场景 {scene_index}/{scene_total} "
                f"批次 {batch_index + 1} "
                "两轮生成后仍未通过时间边界审计："
                + repair_issues[:900]
            )

        for row in accepted:
            row["source_batch_index"] = (
                batch_index + 1
            )
            row["source_audit"] = (
                final_audit or {}
            )
            row["beat_source"] = beat_source

            fingerprint = _studio_v2371b_re.sub(
                r"\s+",
                "",
                (
                    str(
                        row.get(
                            "representative_state"
                        )
                        or ""
                    )
                    + "|"
                    + str(
                        row.get(
                            "video_start_state"
                        )
                        or ""
                    )
                    + "|"
                    + str(
                        row.get(
                            "video_end_state"
                        )
                        or ""
                    )
                ).lower(),
            )[:700]

            if (
                fingerprint
                and fingerprint
                in seen_fingerprints
            ):
                raise RuntimeError(
                    "严格 Stage04：检测到重复状态 Shot；"
                    "拒绝用重复镜头充当剧情覆盖"
                )
            if fingerprint:
                seen_fingerprints.add(
                    fingerprint
                )
            all_rows.append(row)

    if not all_rows:
        raise RuntimeError(
            f"场景 {scene_index}/{scene_total} "
            "没有生成正式镜头"
        )

    expected_all = {
        int(row.get("order") or 0)
        for row in beats
        if int(row.get("order") or 0) > 0
    }
    covered_all = {
        int(order)
        for row in all_rows
        for order in (
            row.get("covered_beat_orders")
            or []
        )
        if int(order) > 0
    }
    if covered_all != expected_all:
        raise RuntimeError(
            f"场景 {scene_index}/{scene_total} "
            "Beat 显式覆盖不完整："
            f"missing="
            f"{sorted(expected_all-covered_all)} "
            f"unexpected="
            f"{sorted(covered_all-expected_all)}"
        )

    for index, row in enumerate(
        all_rows,
        1,
    ):
        row["local_order"] = index
        if not str(
            row.get("title") or ""
        ).strip():
            row["title"] = (
                f"{scene.get('title') or '场景'}"
                f" · 镜头{index}"
            )

    return all_rows
# ===== /V2.37.1B STAGE04 SOURCE BEAT RECOVERY + AUDIT NORMALIZATION =====


# ===== V2.37.1C DIRECT QWEN + LOCAL STRUCTURED PARSER =====
import ast as _studio_v2371c_ast
import json as _studio_v2371c_json
import re as _studio_v2371c_re


def _studio_v2371c_strip_think(text: str) -> str:
    raw = str(text or "")
    raw = _studio_v2371c_re.sub(
        r"<think>.*?</think>",
        "",
        raw,
        flags=_studio_v2371c_re.S | _studio_v2371c_re.I,
    )
    raw = _studio_v2371c_re.sub(
        r"```(?:json|JSON)?\s*",
        "",
        raw,
    )
    raw = raw.replace("```", "")
    return raw.strip()


def _studio_v2371c_balanced_objects(text: str) -> list[str]:
    raw = str(text or "")
    out = []
    seen = set()

    for start, char in enumerate(raw):
        if char != "{":
            continue

        depth = 0
        in_string = False
        quote = ""
        escape = False

        for idx in range(start, len(raw)):
            ch = raw[idx]

            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                continue

            if ch in ('"', "'"):
                in_string = True
                quote = ch
                continue

            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    value = raw[start:idx + 1].strip()
                    if value and value not in seen:
                        seen.add(value)
                        out.append(value)
                    break
                if depth < 0:
                    break

        if len(out) >= 20:
            break

    return out


def _studio_v2371c_json_variants(text: str) -> list[str]:
    raw = _studio_v2371c_strip_think(text)
    variants = []

    def add(value: str):
        value = str(value or "").strip()
        if value and value not in variants:
            variants.append(value)

    add(raw)

    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        add(raw[first:last + 1])

    for item in _studio_v2371c_balanced_objects(raw):
        add(item)

    # Deterministic syntax-only normalizations.
    for base in list(variants):
        add(
            _studio_v2371c_re.sub(
                r",\s*([}\]])",
                r"\1",
                base,
            )
        )
        add(
            base.replace("“", '"')
            .replace("”", '"')
            .replace("‘", "'")
            .replace("’", "'")
        )

    return variants


def _studio_v2371c_parse_object(
    text: str,
    *,
    preferred_keys: tuple[str, ...] = (),
) -> dict:
    candidates = []

    for value in _studio_v2371c_json_variants(text):
        parsed = None
        try:
            parsed = _studio_v2371c_json.loads(value)
        except Exception:
            try:
                obj = _studio_v2371c_ast.literal_eval(value)
                parsed = obj if isinstance(obj, dict) else None
            except Exception:
                parsed = None

        if not isinstance(parsed, dict):
            continue

        score = sum(
            1 for key in preferred_keys
            if key in parsed
        )
        candidates.append((score, parsed))

    if not candidates:
        return {}

    candidates.sort(
        key=lambda row: row[0],
        reverse=True,
    )
    return candidates[0][1]


def _studio_v2371c_result_content(result: object) -> str:
    if isinstance(result, dict):
        return str(result.get("content") or "").strip()
    return str(result or "").strip()


async def _studio_v2371a_qwen_call(
    *,
    phase: str,
    messages: list[dict],
    system_prompt: str,
    temperature: float,
    max_tokens: int,
    contract: str,
):
    """
    V2.37.1c: do NOT call director._structured_json_call here.

    The old director JSON-repair path may discard usable business content
    after a second malformed repair response. Stage04 calls Qwen3-32B
    directly and performs deterministic local syntax parsing.
    """
    perf_started = asyncio.get_running_loop().time()
    contract_cached = globals().get(
        "_studio_v2396_qwen_contract_cached"
    )
    reuse_workspace = (
        bool(contract_cached())
        if callable(contract_cached)
        else False
    )
    async with (
        nullcontext()
        if reuse_workspace
        else gpu.use(GPUOwner.gemma)
    ):
        require = globals().get(
            "_studio_v237_require_qwen32b"
        )
        if require is not None:
            await require()

        output_tokens = int(max_tokens)
        budget_fn = getattr(
            director,
            "_llm_call_budget",
            None,
        )
        if budget_fn is not None:
            try:
                budget = await budget_fn(
                    phase=phase,
                    system_prompt=system_prompt,
                    messages=messages,
                    requested_output_tokens=max_tokens,
                    minimum_output_tokens=160,
                )
                output_tokens = int(
                    budget.get("output_tokens")
                    or output_tokens
                )
            except Exception:
                pass

        # V2.39.6_STAGE04_OUTPUT_BUDGET_CLAMP
        # Stage04 已按具体语义任务定义 max_tokens。
        # 通用 _llm_call_budget 只能缩小预算，绝不能把 Stage04 输出预算再次放大。
        requested_output_tokens = max(1, int(max_tokens))
        budget_output_tokens = max(1, int(output_tokens))
        output_tokens = min(
            requested_output_tokens,
            budget_output_tokens,
        )

        # V2.39.7_STAGE04_PHASE_CAPS
        # 保留完整 Stage04 语义链，仅限制不同语义任务的最大输出长度。
        # 所有 Phase Cap 只能缩小输出，不改变 Prompt、证据、Beat/Shot 绑定或审计逻辑。
        stage04_phase_caps = {
            # V2.39.8_STAGE04_EARLY_PHASE_CAPS
            # 前半段 Anchor / Beat 是当前主要耗时来源。
            # 只限制 JSON 输出长度，不改变语义 Prompt、证据、分组和审计规则。
            "studio_stage04_batched_anchor_classification_qwen32b": 420,
            "studio_stage04_batched_beat_grouping_qwen32b": 1200,
            "studio_stage04_beat_membership_repair_qwen32b": 450,
            "studio_stage04_adaptive_beat_grouping_qwen32b": 1000,
            "studio_stage04_narrative_beat_audit_qwen32b": 420,
            "studio_stage04_narrative_beat_audit_schema_completion_qwen32b": 360,
            "studio_stage04_v23910_scene_narrative_audit_qwen32b": 600,
            "studio_stage04_v239103_compact_scene_audit_qwen32b": 360,

            "studio_stage04_v2395_adjacent_beat_relation_qwen32b": 180,
            "studio_stage04_v2395_forward_overlap_projection_qwen32b": 480,
            "studio_stage04_v239105_forward_overlap_projection_retry_qwen32b": 760,
            "studio_stage04_v2395_forward_overlap_projection_audit_qwen32b": 300,
            "studio_stage04_v2394_same_unit_synthesis_qwen32b": 320,

            "studio_stage04_v2392_direct_shot_generation_qwen32b": 1600,
            "studio_stage04_v2392_missing_beat_completion_qwen32b": 900,

            "studio_stage04_v2391_targeted_duration_planner_qwen32b": 140,

            "studio_stage04_strict_evidence_temporal_audit_qwen32b": 480,
            "studio_stage04_strict_shot_audit_schema_completion_qwen32b": 420,
            "studio_stage04_cross_batch_boundary_audit_qwen32b": 320,

            "studio_stage04_v2383_evidence_locked_repair_qwen32b": 850,

            "v2390_boundary_evidence_locked_repair_qwen32b": 750,
            "v2390_scene_evidence_locked_repair_qwen32b": 850,

            # 部分当前源码 phase 带 studio_stage04_ 前缀。
            "studio_stage04_v2390_boundary_evidence_locked_repair_qwen32b": 750,
            "studio_stage04_v2390_scene_evidence_locked_repair_qwen32b": 850,
            "studio_stage04_anchor_classification_repair_qwen32b": 480,
            "studio_stage04_v239104_adjacent_beat_mini_batch_qwen32b": 420,
        }

        phase_cap = stage04_phase_caps.get(str(phase))

        if phase_cap is not None:
            output_tokens = min(
                int(output_tokens),
                int(phase_cap),
            )

        print(
            "[V2.39.7][Stage04][PhaseCap] "
            f"phase={phase} "
            f"requested={requested_output_tokens} "
            f"budget={budget_output_tokens} "
            f"cap={phase_cap if phase_cap is not None else 'none'} "
            f"effective={output_tokens}",
            flush=True,
        )

        print(
            "[V2.39.6][Stage04][QwenBudget] "
            f"phase={phase} "
            f"requested={requested_output_tokens} "
            f"budget={budget_output_tokens} "
            f"effective={output_tokens}",
            flush=True,
        )

        try:
            result = await director.llm.chat(
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=output_tokens,
                verified_model=(
                    str(settings.stage04_required_model_alias)
                    if reuse_workspace
                    else ""
                ),
            )
        except Exception as exc:
            perf_recorder = globals().get(
                "_studio_v2396_perf_record_llm"
            )
            if callable(perf_recorder):
                perf_recorder(
                    phase=phase,
                    seconds=(
                        asyncio.get_running_loop().time()
                        - perf_started
                    ),
                    error=exc,
                )
            raise
        perf_recorder = globals().get(
            "_studio_v2396_perf_record_llm"
        )
        if callable(perf_recorder):
            perf_recorder(
                phase=phase,
                seconds=(
                    asyncio.get_running_loop().time()
                    - perf_started
                ),
                result=result,
            )
        required_alias = str(
            settings.stage04_required_model_alias
        ).strip()
        response_model = str(
            result.get("model") if isinstance(result, dict) else ""
        ).strip()
        if response_model != required_alias:
            raise RuntimeError(
                "Stage04 chat response model 不匹配："
                f"response={response_model or '<empty>'} "
                f"required={required_alias}"
            )

    raw = _studio_v2371c_result_content(result)

    preferred = ()
    if '"shots"' in contract:
        preferred = ("shots",)
    elif '"beats"' in contract:
        preferred = ("beats",)
    elif '"audit_passed"' in contract:
        preferred = (
            "audit_passed",
            "violations",
        )
    elif '"valid"' in contract:
        preferred = (
            "valid",
            "issues",
        )

    parsed = _studio_v2371c_parse_object(
        raw,
        preferred_keys=preferred,
    )

    merged = (
        dict(result)
        if isinstance(result, dict)
        else {"content": raw}
    )
    merged["stage04_direct_qwen"] = True
    merged["stage04_local_parse"] = bool(parsed)
    merged["stage04_phase"] = phase
    merged["stage04_requested_max_tokens"] = int(max_tokens)
    merged["stage04_effective_max_tokens"] = int(output_tokens)
    return merged, parsed, False

def _studio_v2371b_extract_json_objects(
    value: object,
) -> list[dict]:
    objects = []
    seen = set()

    if isinstance(value, dict):
        content = str(value.get("content") or "")
        if content:
            parsed = _studio_v2371c_parse_object(
                content,
                preferred_keys=(
                    "beats",
                    "shots",
                    "valid",
                    "audit_passed",
                ),
            )
            if parsed:
                objects.append(parsed)
        # Do not treat transport metadata itself as business JSON.
    elif isinstance(value, str):
        parsed = _studio_v2371c_parse_object(
            value,
            preferred_keys=(
                "beats",
                "shots",
                "valid",
                "audit_passed",
            ),
        )
        if parsed:
            objects.append(parsed)

    collect = globals().get(
        "_studio_v2371a_collect_texts"
    )
    if collect is not None:
        for text in collect(value):
            parsed = _studio_v2371c_parse_object(
                text,
                preferred_keys=(
                    "beats",
                    "shots",
                    "valid",
                    "audit_passed",
                ),
            )
            if parsed:
                objects.append(parsed)

    clean = []
    for obj in objects:
        key = _studio_json.dumps(
            obj,
            ensure_ascii=False,
            sort_keys=True,
        )
        if key in seen:
            continue
        seen.add(key)
        clean.append(obj)
    return clean


def _studio_v2371a_extract_shots(
    value: object,
) -> list[dict]:
    for parsed in _studio_v2371b_extract_json_objects(
        value
    ):
        rows = parsed.get("shots")
        if isinstance(rows, list):
            clean = [
                row for row in rows
                if isinstance(row, dict)
            ]
            if clean:
                return clean
    return []


def _studio_v2371b_extract_beats(
    value: object,
) -> list[dict]:
    for parsed in _studio_v2371b_extract_json_objects(
        value
    ):
        rows = parsed.get("beats")
        if isinstance(rows, list):
            clean = [
                row for row in rows
                if isinstance(row, dict)
            ]
            if clean:
                return clean
    return []


def _studio_v2371c_parse_beat_lines(
    text: str,
    *,
    anchor_map: dict[str, str],
    allowed_chars: set[str],
    allowed_props: set[str],
) -> list[dict]:
    """
    Deterministic non-JSON fallback protocol:
    BEAT<TAB>summary<TAB>E001,E002<TAB>char_ids<TAB>prop_ids
    """
    result = []
    seen = set()

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("BEAT\t"):
            continue

        parts = line.split("\t")
        if len(parts) < 3:
            continue

        summary = str(parts[1] or "").strip()
        if not summary:
            continue

        evidence_ids = []
        evidence_text = []
        for token in str(parts[2] or "").split(","):
            key = token.strip()
            if (
                key
                and key in anchor_map
                and key not in evidence_ids
            ):
                evidence_ids.append(key)
                evidence_text.append(
                    anchor_map[key]
                )
        if not evidence_ids:
            continue

        chars = []
        if len(parts) >= 4:
            for token in str(parts[3] or "").split(","):
                key = token.strip()
                if (
                    key
                    and key in allowed_chars
                    and key not in chars
                ):
                    chars.append(key)

        props = []
        if len(parts) >= 5:
            for token in str(parts[4] or "").split(","):
                key = token.strip()
                if (
                    key
                    and key in allowed_props
                    and key not in props
                ):
                    props.append(key)

        fingerprint = _studio_v2371b_normalize_summary(
            summary
        )
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)

        result.append({
            "summary": summary[:500],
            "source_evidence_ids": evidence_ids[:3],
            "source_evidence": evidence_text[:3],
            "character_entity_ids": chars,
            "prop_entity_ids": props,
        })

    return result


async def _studio_v2371b_generate_beats_for_chunk(
    *,
    chunk: str,
    chunk_index: int,
    chunk_total: int,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> list[dict]:
    anchors = _studio_v2371_evidence_anchors(
        chunk
    )
    if not anchors:
        return []

    anchor_map = _studio_v2371_anchor_map(
        anchors
    )
    allowed_entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        700,
    )

    json_system = (
        "你是小说正文原子剧情 Beat 提取器，运行模型为 Qwen3-32B。"
        "只做事实拆解，不做分镜，不添加剧情。"
        "按 ORIGINAL_SOURCE_CHUNK 的原始时间顺序提取原子 Beat。"
        "每个 Beat 是明确的状态变化、动作推进、信息揭示或角色行为。"
        "不要把相隔较远的事件压缩成一个 Beat，不提前总结后续结果。"
        "source_evidence_ids 必须从 SOURCE_EVIDENCE_ANCHORS 选择。"
        "实体只填写当前 Beat 明确涉及且在 ALLOWED_ENTITIES 中存在者；"
        "无法确认留空。只输出 JSON。"
    )
    json_prompt = (
        f"CHUNK_PROGRESS={chunk_index}/{chunk_total}\n"
        "=== ORIGINAL_SOURCE_CHUNK ===\n"
        + chunk
        + "\n\n=== SOURCE_EVIDENCE_ANCHORS ===\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + allowed_entity_text
    )
    schema = (
        '{"beats":[{'
        '"summary":"",'
        '"source_evidence_ids":["E001"],'
        '"character_entity_ids":[],'
        '"prop_entity_ids":[]'
        '}]}'
    )

    diagnostics = []

    for attempt, suffix in enumerate(
        (
            "",
            "\n\n只返回一个 JSON 对象；根字段必须是 beats；"
            "不要 Markdown、不要解释、不要尾注。",
        ),
        1,
    ):
        try:
            raw_result, parsed, _ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "source_beat_extraction_qwen32b"
                    ),
                    messages=[{
                        "role": "user",
                        "content": json_prompt + suffix,
                    }],
                    system_prompt=json_system,
                    temperature=(
                        0.05 if attempt == 1 else 0.0
                    ),
                    max_tokens=1250,
                    contract=schema,
                )
            )

            rows = (
                parsed.get("beats")
                if isinstance(parsed, dict)
                else None
            )
            if not isinstance(rows, list) or not rows:
                rows = _studio_v2371b_extract_beats(
                    raw_result
                )

            cleaned = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue

                summary = str(
                    row.get("summary") or ""
                ).strip()
                if not summary:
                    continue

                evidence_ids = []
                evidence_text = []
                for token in (
                    row.get("source_evidence_ids")
                    or []
                ):
                    key = str(token or "").strip()
                    if (
                        key
                        and key in anchor_map
                        and key not in evidence_ids
                    ):
                        evidence_ids.append(key)
                        evidence_text.append(
                            anchor_map[key]
                        )
                    if len(evidence_ids) >= 3:
                        break
                if not evidence_ids:
                    continue

                cleaned.append({
                    "summary": summary[:500],
                    "source_evidence_ids":
                        evidence_ids,
                    "source_evidence":
                        evidence_text,
                    "character_entity_ids":
                        _studio_v2371_clean_ids(
                            row.get(
                                "character_entity_ids"
                            ),
                            allowed_chars,
                        ),
                    "prop_entity_ids":
                        _studio_v2371_clean_ids(
                            row.get(
                                "prop_entity_ids"
                            ),
                            allowed_props,
                        ),
                })

            if cleaned:
                return cleaned

            diagnostics.append(
                f"json_attempt={attempt}: "
                "no usable beats"
            )
        except Exception as exc:
            diagnostics.append(
                f"json_attempt={attempt}: "
                f"{type(exc).__name__}: "
                f"{str(exc)[:260]}"
            )

    # Final fallback is deliberately NOT JSON.
    # This avoids making Stage04 depend on one serialization syntax.
    line_system = (
        "你是小说正文原子 Beat 提取器。"
        "不要输出 JSON。每个 Beat 只占一行，严格格式："
        "BEAT<TAB>summary<TAB>evidence_ids<TAB>character_ids<TAB>prop_ids。"
        "summary 内禁止制表符。"
        "evidence_ids 使用 E001,E002 形式。"
        "实体 ID 只能来自 ALLOWED_ENTITIES；没有就留空。"
        "只输出 BEAT 行，不要标题、解释或 Markdown。"
    )
    line_prompt = (
        f"CHUNK_PROGRESS={chunk_index}/{chunk_total}\n"
        "=== ORIGINAL_SOURCE_CHUNK ===\n"
        + chunk
        + "\n\n=== SOURCE_EVIDENCE_ANCHORS ===\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + allowed_entity_text
    )

    try:
        async with gpu.use(GPUOwner.gemma):
            require = globals().get(
                "_studio_v237_require_qwen32b"
            )
            if require is not None:
                await require()

            result = await director.llm.chat(
                messages=[{
                    "role": "user",
                    "content": line_prompt,
                }],
                system_prompt=line_system,
                temperature=0.0,
                max_tokens=1200,
            )

        line_rows = _studio_v2371c_parse_beat_lines(
            _studio_v2371c_result_content(
                result
            ),
            anchor_map=anchor_map,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
        )
        if line_rows:
            return line_rows

        diagnostics.append(
            "line_fallback: no usable BEAT rows"
        )
    except Exception as exc:
        diagnostics.append(
            "line_fallback: "
            f"{type(exc).__name__}: "
            f"{str(exc)[:260]}"
        )

    raise RuntimeError(
        f"正文 Beat 提取失败（chunk "
        f"{chunk_index}/{chunk_total}）："
        + " | ".join(diagnostics)
    )
# ===== /V2.37.1C DIRECT QWEN + LOCAL STRUCTURED PARSER =====


# ===== V2.37.1D STAGE04 AUDIT RESULT NORMALIZATION =====
def _studio_v2371_audit_ok(audit: dict) -> bool:
    """
    Accept only an explicit positive audit with no violations.

    Supported Qwen3-32B response shapes:
    1) full strict schema:
       valid=true + every strict boolean=true
    2) aggregate schema:
       audit_passed=true + violations=[]
    3) compact aggregate schema observed at runtime:
       valid=true + violations=[] + reasons=[...]

    No negative/unknown result is converted into a pass.
    """
    if not isinstance(audit, dict):
        return False

    violations = audit.get("violations")
    no_violations = violations in (None, [], {})

    strict_keys = (
        "valid",
        "beat_coverage_ok",
        "temporal_monotonic",
        "no_future_event_preconsumption",
        "no_result_duplication",
        "state_order_valid",
        "entity_visibility_valid",
    )

    # Full requested contract.
    if all(audit.get(key) is True for key in strict_keys):
        return no_violations

    # Aggregate alternate schema.
    if audit.get("audit_passed") is True and no_violations:
        return True

    # Qwen3-32B compact schema actually observed in Stage04 runtime:
    # {"valid": true, "reasons": [...], "violations": []}
    if audit.get("valid") is True and no_violations:
        return True

    return False
# ===== /V2.37.1D STAGE04 AUDIT RESULT NORMALIZATION =====


# ===== V2.37.1E STAGE04 BEAT EVIDENCE LINEAGE =====
import re as _studio_v2371e_re


def _studio_v2371e_batch_evidence(
    *,
    source: str,
    batch: list[dict],
    max_context_chars: int = 1900,
) -> tuple[str, list[dict], dict[int, list[str]]]:
    """
    Build Shot evidence directly from the exact source evidence already
    attached to each Beat.

    This prevents the old bug:
      Beat extraction window E001/E002
          !=
      later Shot batch window E001/E002
    even though both IDs happened to use the same labels.
    """
    source = str(source or "")
    anchors = []
    beat_to_anchor_ids: dict[int, list[str]] = {}
    text_to_id: dict[str, str] = {}
    positions = []

    for beat in batch:
        try:
            order = int(beat.get("order") or 0)
        except Exception:
            order = 0
        if order <= 0:
            raise RuntimeError(
                "严格 Stage04：Beat 缺少有效 order，"
                "无法建立证据血缘"
            )

        evidence_rows = [
            str(x or "").strip()
            for x in (
                beat.get("source_evidence") or []
            )
            if str(x or "").strip()
        ]
        if not evidence_rows:
            raise RuntimeError(
                f"严格 Stage04：Beat {order} "
                "缺少小说正文 source_evidence；"
                "拒绝生成脱离原文证据的 Shot"
            )

        beat_ids = []
        for evidence_text in evidence_rows:
            pos = source.find(evidence_text)
            if pos < 0:
                raise RuntimeError(
                    f"严格 Stage04：Beat {order} 的原文证据"
                    "无法在当前 Scene 正文逐字定位；"
                    f" evidence={evidence_text[:120]!r}"
                )

            anchor_id = text_to_id.get(evidence_text)
            if not anchor_id:
                anchor_id = f"E{len(anchors) + 1:03d}"
                text_to_id[evidence_text] = anchor_id
                anchors.append({
                    "id": anchor_id,
                    "text": evidence_text,
                    "beat_order": order,
                })

            if anchor_id not in beat_ids:
                beat_ids.append(anchor_id)

            positions.append(
                (pos, pos + len(evidence_text))
            )

        beat_to_anchor_ids[order] = beat_ids

    if not anchors:
        raise RuntimeError(
            "严格 Stage04：当前 Beat 批次没有可用原文证据"
        )

    # Build an audit/source context around the exact Beat evidence.
    # The evidence anchors themselves remain the authoritative exact text.
    lo = min(x[0] for x in positions)
    hi = max(x[1] for x in positions)

    if hi - lo <= max_context_chars:
        spare = max_context_chars - (hi - lo)
        left = max(0, lo - spare // 2)
        right = min(
            len(source),
            hi + (spare - (lo - left)),
        )
        left = max(0, right - max_context_chars)
        source_context = source[left:right].strip()
    else:
        # Consecutive Beats should normally be close. If a legacy/source
        # anomaly makes the range very large, never drop evidence just to
        # satisfy a window length: concatenate the exact evidence instead.
        source_context = "\n".join(
            f"[{row['id']}|Beat {row['beat_order']}] "
            f"{row['text']}"
            for row in anchors
        )

    return (
        source_context,
        anchors,
        beat_to_anchor_ids,
    )


_V2371E_PREVIOUS_VALIDATE_ROWS = _studio_v2371_validate_rows


def _studio_v2371_validate_rows(
    *,
    raw_rows: list[dict],
    compact_beats: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    anchors: list[dict],
    scene_id: str,
    episode_id: str,
) -> list[dict]:
    rows = _V2371E_PREVIOUS_VALIDATE_ROWS(
        raw_rows=raw_rows,
        compact_beats=compact_beats,
        allowed_chars=allowed_chars,
        allowed_props=allowed_props,
        anchors=anchors,
        scene_id=scene_id,
        episode_id=episode_id,
    )

    beat_allowed: dict[int, set[str]] = {}
    for beat in compact_beats:
        try:
            order = int(beat.get("order") or 0)
        except Exception:
            continue
        beat_allowed[order] = {
            str(x or "").strip()
            for x in (
                beat.get("source_evidence_ids") or []
            )
            if str(x or "").strip()
        }

    for index, row in enumerate(rows, 1):
        covered = {
            int(x)
            for x in (
                row.get("covered_beat_orders") or []
            )
            if str(x).strip()
        }
        evidence_ids = {
            str(x or "").strip()
            for x in (
                row.get("source_evidence_ids") or []
            )
            if str(x or "").strip()
        }

        allowed = set()
        for order in covered:
            allowed.update(
                beat_allowed.get(order) or set()
            )

        if not allowed:
            raise RuntimeError(
                f"严格 Stage04：Shot {index} 覆盖的 Beat "
                "没有可继承的小说正文证据"
            )

        if not evidence_ids:
            raise RuntimeError(
                f"严格 Stage04：Shot {index} "
                "没有选择任何 Beat 原文证据"
            )

        illegal = evidence_ids - allowed
        if illegal:
            raise RuntimeError(
                f"严格 Stage04：Shot {index} 引用了不属于 "
                f"covered_beat_orders 的证据锚点："
                f"{sorted(illegal)}；允许={sorted(allowed)}"
            )

    return rows


async def _studio_stage04_scene_shots(
    *, project_id: str, scene: dict, state: dict, source_text: str,
    upstream: dict, user_input: str, scene_index: int, scene_total: int,
) -> list[dict]:
    scene_id = str(scene.get("scene_id") or "")
    resolved = story_continuity.resolve_scene(
        project_id,
        scene_id,
    )
    source = _studio_stage04_scene_source(
        scene,
        source_text,
    )
    allowed_chars, allowed_props = (
        _studio_stage04_allowed_ids(
            scene,
            resolved,
        )
    )

    beats, beat_source = (
        await _studio_v2371b_ensure_scene_beats(
            project_id=project_id,
            scene=scene,
            state=state,
            source=source,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
        )
    )
    if not beats:
        raise RuntimeError(
            "严格 Stage04：当前 Scene 没有 Beats；"
            "拒绝无 Beat 直接生成正式 Shot"
        )

    entities = {
        str(x.get("entity_id") or ""): {
            "entity_id": str(
                x.get("entity_id") or ""
            ),
            "entity_type": str(
                x.get("entity_type") or ""
            ),
            "name": str(x.get("name") or ""),
        }
        for x in director.production.list_entities(
            project_id
        )
        if str(x.get("entity_id") or "")
    }

    scene_entities = [
        entities[eid]
        for eid in [
            *sorted(allowed_chars),
            *sorted(allowed_props),
        ]
        if eid in entities
    ]
    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            scene_entities,
            ensure_ascii=False,
        ),
        700,
    )

    resolved_compact = {
        "location": resolved.get("location"),
        "characters": resolved.get("characters"),
        "props": resolved.get("props"),
        "scene_state": resolved.get("scene_state"),
    }
    resolved_text = _studio_v2371_cut(
        _studio_json.dumps(
            resolved_compact,
            ensure_ascii=False,
        ),
        760,
    )
    character_anchor = _studio_v2371_cut(
        upstream.get("character_bible"),
        1100,
    )
    visual_anchor = _studio_v2371_cut(
        upstream.get("visual_bible"),
        900,
    )

    beat_batches = [
        beats[i:i + 3]
        for i in range(0, len(beats), 3)
    ]

    system_prompt = (
        "你是正式短视频分镜导演，运行文本模型为 Qwen3-32B。"
        "小说正文和明确 Beats 是最高优先级事实。"
        "必须生成 strict-shot-v2 制作合同。"
        "硬规则："
        "1. 每个 Shot 的 covered_beat_orders 必须非空，"
        "且只能引用当前 BEATS_THIS_BATCH；"
        "2. 每个 Beat 必须至少被一个 Shot 显式覆盖；"
        "3. 每个 Beat 自带 allowed_source_evidence_ids，"
        "Shot 的 source_evidence_ids 只能从自己 covered_beat_orders "
        "对应 Beat 的 allowed_source_evidence_ids 中选择；"
        "4. 人物/道具只填写当前画面真实可见实体，"
        "不确定留空，禁止整个 Scene 兜底；"
        "5. representative_state 是当前 Shot 最有叙事信息的单帧，"
        "不得退化为无因果信息的通用肖像；"
        "6. video_start_state 是当前 Shot 第一动作发生前或刚开始；"
        "video_end_state 是该 Shot 自己结束状态；"
        "7. 不得提前消费后续 Beat 的主要事件；"
        "8. image_prompt 只描述 representative_state；"
        "video_start_prompt 只描述 video_start_state；"
        "video_prompt 只描述 video_start_state 到 video_end_state "
        "的前向变化；"
        "9. 只依据当前原文、Beat、Scene Fact、允许实体及确认视觉锚点。"
        "只输出严格 JSON。"
    )

    all_rows = []
    seen_fingerprints = set()

    for batch_index, batch in enumerate(
        beat_batches
    ):
        (
            source_window,
            anchors,
            beat_to_anchor_ids,
        ) = _studio_v2371e_batch_evidence(
            source=source,
            batch=batch,
            max_context_chars=1900,
        )

        compact_beats = []
        for row in batch:
            order = int(row.get("order") or 0)
            allowed_evidence = list(
                beat_to_anchor_ids.get(order) or []
            )
            if not allowed_evidence:
                raise RuntimeError(
                    f"严格 Stage04：Beat {order} "
                    "没有可传递给 Shot 的证据锚点"
                )
            compact_beats.append({
                "order": order,
                "summary": str(
                    row.get("summary") or ""
                )[:320],
                "allowed_source_evidence_ids":
                    allowed_evidence,
                # Validator reads this field to enforce lineage.
                "source_evidence_ids":
                    allowed_evidence,
                "source_evidence": list(
                    row.get("source_evidence") or []
                ),
                "character_entity_ids": list(
                    row.get("character_entity_ids") or []
                ),
                "prop_entity_ids": list(
                    row.get("prop_entity_ids") or []
                ),
            })

        # One Beat normally maps to one production Shot.
        # Split further only when Qwen determines the Beat contains an
        # independently filmable state change; do not expand merely to hit
        # a quantity target. This keeps strict contracts inside output budget.
        batch_target = max(
            1,
            len(compact_beats),
        )

        base_prompt = (
            f"SCENE_PROGRESS={scene_index}/{scene_total}\n"
            f"BATCH_PROGRESS="
            f"{batch_index + 1}/{len(beat_batches)}\n"
            f"BEAT_SOURCE={beat_source}\n"
            f"SCENE_ID={scene_id}\n"
            f"SCENE_TITLE="
            f"{str(scene.get('title') or '')[:160]}\n"
            f"SCENE_SUMMARY="
            f"{str(scene.get('summary') or '')[:340]}\n"
            f"TARGET_SHOTS≈{batch_target}；"
            "按实际视觉变化决定，不得合并掉独立状态变化。\n\n"
            "=== ORIGINAL_SOURCE_CONTEXT ===\n"
            + source_window
            + "\n\n=== SOURCE_EVIDENCE_ANCHORS ===\n"
            + _studio_json.dumps(
                anchors,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== BEATS_THIS_BATCH ===\n"
            + _studio_json.dumps(
                compact_beats,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== CONTINUITY ===\n"
            + resolved_text
            + "\n\n=== ALLOWED_ENTITIES ===\n"
            + entity_text
            + "\n\n=== CHARACTER_ANCHOR ===\n"
            + (character_anchor or "<none>")
            + "\n\n=== VISUAL_ANCHOR ===\n"
            + (visual_anchor or "<none>")
            + "\n\n=== USER_REQUIREMENT ===\n"
            + _studio_v2371_cut(
                user_input,
                300,
            )
        )

        accepted = None
        final_audit = None
        repair_issues = ""

        for round_index in range(2):
            prompt = base_prompt
            if repair_issues:
                prompt += (
                    "\n\n=== PREVIOUS_AUDIT_ISSUES ===\n"
                    + repair_issues
                    + "\n重新生成整个当前批次，"
                    "不要仅修改说明文字。"
                )

            raw_rows = await _studio_v2371_generate_batch(
                system_prompt=system_prompt,
                prompt=prompt,
                scene_index=scene_index,
                scene_total=scene_total,
                batch_index=batch_index,
                batch_total=len(beat_batches),
            )

            try:
                rows = _studio_v2371_validate_rows(
                    raw_rows=raw_rows,
                    compact_beats=compact_beats,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    anchors=anchors,
                    scene_id=scene_id,
                    episode_id=str(
                        scene.get("episode_id") or ""
                    ),
                )
            except RuntimeError as exc:
                repair_issues = (
                    "STRICT_VALIDATION_ERROR: "
                    + str(exc)
                )
                if round_index == 0:
                    continue
                raise RuntimeError(
                    f"场景 {scene_index}/{scene_total} "
                    f"批次 {batch_index + 1} "
                    "两轮生成后仍未通过严格字段校验："
                    + str(exc)
                ) from exc

            audit = await _studio_v2371_audit_batch(
                source_window=source_window,
                compact_beats=compact_beats,
                shots=rows,
            )

            if _studio_v2371_audit_ok(audit):
                accepted = rows
                final_audit = audit
                break

            repair_issues = _studio_json.dumps(
                audit.get("issues")
                or audit.get("violations")
                or audit,
                ensure_ascii=False,
            )

        if accepted is None:
            raise RuntimeError(
                f"场景 {scene_index}/{scene_total} "
                f"批次 {batch_index + 1} "
                "两轮生成后仍未通过时间边界审计："
                + repair_issues[:900]
            )

        for row in accepted:
            row["source_batch_index"] = (
                batch_index + 1
            )
            row["source_audit"] = (
                final_audit or {}
            )
            row["beat_source"] = beat_source
            row[
                "evidence_lineage_version"
            ] = "beat-to-shot-v1"

            # Duplicate identity must include narrative scope.
            # Similar visual states across different Beats can be legitimate;
            # the temporal audit decides whether they are narratively redundant.
            covered_scope = ",".join(
                str(int(x))
                for x in sorted(
                    {
                        int(v)
                        for v in (
                            row.get("covered_beat_orders") or []
                        )
                        if str(v).strip()
                    }
                )
            )
            evidence_scope = ",".join(
                sorted(
                    {
                        str(v or "").strip()
                        for v in (
                            row.get("source_evidence_ids") or []
                        )
                        if str(v or "").strip()
                    }
                )
            )
            state_scope = _studio_v2371e_re.sub(
                r"\s+",
                "",
                (
                    str(
                        row.get(
                            "representative_state"
                        )
                        or ""
                    )
                    + "|"
                    + str(
                        row.get(
                            "video_start_state"
                        )
                        or ""
                    )
                    + "|"
                    + str(
                        row.get(
                            "video_end_state"
                        )
                        or ""
                    )
                ).lower(),
            )[:700]

            duplicate_identity = (
                covered_scope
                + "||"
                + evidence_scope
                + "||"
                + state_scope
            )

            if (
                covered_scope
                and evidence_scope
                and state_scope
                and duplicate_identity in seen_fingerprints
            ):
                raise RuntimeError(
                    "严格 Stage04：检测到同一 Beat/同一原文证据/"
                    "同一三状态的真正重复 Shot；"
                    "拒绝用完全重复镜头充当剧情覆盖"
                )

            if (
                covered_scope
                and evidence_scope
                and state_scope
            ):
                seen_fingerprints.add(
                    duplicate_identity
                )
            all_rows.append(row)

    if not all_rows:
        raise RuntimeError(
            f"场景 {scene_index}/{scene_total} "
            "没有生成正式镜头"
        )

    expected_all = {
        int(row.get("order") or 0)
        for row in beats
        if int(row.get("order") or 0) > 0
    }
    covered_all = {
        int(order)
        for row in all_rows
        for order in (
            row.get("covered_beat_orders")
            or []
        )
        if int(order) > 0
    }
    if covered_all != expected_all:
        raise RuntimeError(
            f"场景 {scene_index}/{scene_total} "
            "Beat 显式覆盖不完整："
            f"missing="
            f"{sorted(expected_all-covered_all)} "
            f"unexpected="
            f"{sorted(covered_all-expected_all)}"
        )

    for index, row in enumerate(
        all_rows,
        1,
    ):
        row["local_order"] = index
        if not str(
            row.get("title") or ""
        ).strip():
            row["title"] = (
                f"{scene.get('title') or '场景'}"
                f" · 镜头{index}"
            )

    return all_rows
# ===== /V2.37.1E STAGE04 BEAT EVIDENCE LINEAGE =====

# ===== V2.37.1F-R1 ACTIVE-FUNCTION DUPLICATE SCOPE FIX =====


# ===== V2.37.1G EVIDENCE-BASED BEAT BINDING REPAIR =====
import copy as _studio_v2371g_copy


def _studio_v2371g_int_orders(value: object) -> list[int]:
    result = []
    for item in value or []:
        try:
            order = int(item)
        except Exception:
            continue
        if order > 0 and order not in result:
            result.append(order)
    return result


def _studio_v2371g_text_ids(value: object) -> list[str]:
    result = []
    for item in value or []:
        key = str(item or "").strip()
        if key and key not in result:
            result.append(key)
    return result


def _studio_v2371g_evidence_owners(
    compact_beats: list[dict],
) -> dict[str, set[int]]:
    owners: dict[str, set[int]] = {}
    for beat in compact_beats or []:
        try:
            order = int(beat.get("order") or 0)
        except Exception:
            continue
        if order <= 0:
            continue
        evidence_ids = _studio_v2371g_text_ids(
            beat.get("source_evidence_ids")
            or beat.get("allowed_source_evidence_ids")
            or []
        )
        for evidence_id in evidence_ids:
            owners.setdefault(
                evidence_id,
                set(),
            ).add(order)
    return owners


def _studio_v2371g_repair_beat_binding(
    *,
    raw_rows: list[dict],
    compact_beats: list[dict],
) -> list[dict]:
    """
    Recover ONLY a missing covered_beat_orders field from the Shot's
    own selected source_evidence_ids.

    This is not quantity/order fallback:
    - no evidence -> reject;
    - unknown evidence -> reject;
    - evidence owned by multiple Beats -> reject;
    - otherwise persist the uniquely implied Beat order(s).
    """
    expected = {
        int(beat.get("order") or 0)
        for beat in compact_beats or []
        if int(beat.get("order") or 0) > 0
    }
    owners = _studio_v2371g_evidence_owners(
        compact_beats
    )

    repaired = []
    for index, original in enumerate(
        raw_rows or [],
        1,
    ):
        if not isinstance(original, dict):
            repaired.append(original)
            continue

        row = _studio_v2371g_copy.deepcopy(
            original
        )
        existing = _studio_v2371g_int_orders(
            row.get("covered_beat_orders")
        )
        if existing:
            # Existing explicit model binding remains authoritative.
            repaired.append(row)
            continue

        # Ignore structurally empty template rows here; the downstream
        # validator already handles/removes them.
        summary = str(
            row.get("summary") or ""
        ).strip()
        action = str(
            row.get("action") or ""
        ).strip()
        if not summary and not action:
            repaired.append(row)
            continue

        evidence_ids = _studio_v2371g_text_ids(
            row.get("source_evidence_ids")
        )
        if not evidence_ids:
            raise RuntimeError(
                f"Shot {index} 未填写 covered_beat_orders，"
                "且没有 source_evidence_ids 可用于确定性恢复"
            )

        derived = set()
        unknown = []
        ambiguous = {}

        for evidence_id in evidence_ids:
            beat_owners = set(
                owners.get(evidence_id) or set()
            )
            if not beat_owners:
                unknown.append(evidence_id)
                continue
            if len(beat_owners) != 1:
                ambiguous[evidence_id] = sorted(
                    beat_owners
                )
                continue
            derived.update(beat_owners)

        if unknown:
            raise RuntimeError(
                f"Shot {index} 未填写 covered_beat_orders；"
                "其 source_evidence_ids 含当前 Beat 批次未知证据："
                + repr(unknown)
            )

        if ambiguous:
            raise RuntimeError(
                f"Shot {index} 未填写 covered_beat_orders；"
                "证据归属多个 Beat，不能自动猜测："
                + repr(ambiguous)
            )

        if not derived:
            raise RuntimeError(
                f"Shot {index} 未填写 covered_beat_orders；"
                "无法从原文证据得到唯一 Beat 绑定"
            )

        illegal = derived - expected
        if illegal:
            raise RuntimeError(
                f"Shot {index} 从证据推导出了当前批次外 Beat："
                + repr(sorted(illegal))
            )

        row["covered_beat_orders"] = sorted(
            derived
        )
        row[
            "beat_binding_origin"
        ] = "derived-from-source-evidence"
        repaired.append(row)

    return repaired


_V2371G_PREVIOUS_VALIDATE_ROWS = (
    _studio_v2371_validate_rows
)


def _studio_v2371_validate_rows(
    *,
    raw_rows: list[dict],
    compact_beats: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    anchors: list[dict],
    scene_id: str,
    episode_id: str,
) -> list[dict]:
    repaired_rows = (
        _studio_v2371g_repair_beat_binding(
            raw_rows=raw_rows,
            compact_beats=compact_beats,
        )
    )

    rows = _V2371G_PREVIOUS_VALIDATE_ROWS(
        raw_rows=repaired_rows,
        compact_beats=compact_beats,
        allowed_chars=allowed_chars,
        allowed_props=allowed_props,
        anchors=anchors,
        scene_id=scene_id,
        episode_id=episode_id,
    )

    # Carry audit provenance for any deterministic binding recovery.
    origins = []
    for raw in repaired_rows:
        if not isinstance(raw, dict):
            continue
        if (
            raw.get("beat_binding_origin")
            == "derived-from-source-evidence"
        ):
            origins.append({
                "title": str(
                    raw.get("title") or ""
                ),
                "summary": str(
                    raw.get("summary") or ""
                )[:180],
                "covered_beat_orders": list(
                    raw.get(
                        "covered_beat_orders"
                    ) or []
                ),
                "source_evidence_ids": list(
                    raw.get(
                        "source_evidence_ids"
                    ) or []
                ),
            })

    if origins:
        # Match normalized rows back by stable content fields.
        for row in rows:
            if row.get("covered_beat_orders"):
                for origin in origins:
                    if (
                        list(
                            row.get(
                                "covered_beat_orders"
                            ) or []
                        )
                        == origin[
                            "covered_beat_orders"
                        ]
                        and set(
                            row.get(
                                "source_evidence_ids"
                            ) or []
                        )
                        == set(
                            origin[
                                "source_evidence_ids"
                            ]
                        )
                    ):
                        row[
                            "beat_binding_origin"
                        ] = (
                            "derived-from-source-evidence"
                        )
                        break

    return rows
# ===== /V2.37.1G EVIDENCE-BASED BEAT BINDING REPAIR =====


# ===== V2.37.1H STAGE04 SHOT OUTPUT RESILIENCE =====
import ast as _studio_v2371h_ast
import json as _studio_v2371h_json
import re as _studio_v2371h_re


def _studio_v2371h_strip_fences(text: str) -> str:
    raw = str(text or "").strip()
    raw = _studio_v2371h_re.sub(
        r"<think>.*?</think>",
        "",
        raw,
        flags=_studio_v2371h_re.S | _studio_v2371h_re.I,
    ).strip()
    raw = _studio_v2371h_re.sub(
        r"^\s*```(?:json|JSON)?\s*",
        "",
        raw,
    )
    raw = _studio_v2371h_re.sub(
        r"\s*```\s*$",
        "",
        raw,
    )
    return raw.strip()


def _studio_v2371h_parse_jsonish(value: str):
    raw = _studio_v2371h_strip_fences(
        value
    )
    if not raw:
        return None

    variants = [raw]
    variants.append(
        _studio_v2371h_re.sub(
            r",\s*([}\]])",
            r"\1",
            raw,
        )
    )

    for item in variants:
        try:
            return _studio_v2371h_json.loads(
                item
            )
        except Exception:
            pass
        try:
            return _studio_v2371h_ast.literal_eval(
                item
            )
        except Exception:
            pass
    return None


def _studio_v2371h_balanced_objects_from_array(
    text: str,
    array_start: int,
    *,
    limit: int = 12,
) -> list[dict]:
    """
    Recover complete object elements from a JSON array even if the model
    response was truncated before the closing ] / outer }.
    """
    raw = str(text or "")
    if (
        array_start < 0
        or array_start >= len(raw)
        or raw[array_start] != "["
    ):
        return []

    result = []
    idx = array_start + 1

    while idx < len(raw) and len(result) < limit:
        while (
            idx < len(raw)
            and raw[idx] in " \t\r\n,"
        ):
            idx += 1

        if idx >= len(raw) or raw[idx] == "]":
            break

        if raw[idx] != "{":
            # Skip non-object material until next plausible element.
            nxt = raw.find("{", idx + 1)
            if nxt < 0:
                break
            idx = nxt

        start = idx
        depth = 0
        in_string = False
        quote = ""
        escape = False
        end = -1

        for pos in range(start, len(raw)):
            ch = raw[pos]

            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                continue

            if ch in ('"', "'"):
                in_string = True
                quote = ch
                continue

            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break
                if depth < 0:
                    break

        if end < 0:
            # Last element itself is truncated; preserve earlier complete
            # elements and stop.
            break

        obj = _studio_v2371h_parse_jsonish(
            raw[start:end]
        )
        if isinstance(obj, dict):
            result.append(obj)

        idx = end

    return result


def _studio_v2371h_extract_shots_from_text(
    text: str,
) -> list[dict]:
    raw = _studio_v2371h_strip_fences(
        text
    )
    if not raw:
        return []

    # 1) Fully valid top-level object or top-level array.
    parsed = _studio_v2371h_parse_jsonish(
        raw
    )
    if isinstance(parsed, dict):
        rows = parsed.get("shots")
        if isinstance(rows, list):
            clean = [
                row for row in rows
                if isinstance(row, dict)
            ]
            if clean:
                return clean
    elif isinstance(parsed, list):
        clean = [
            row for row in parsed
            if isinstance(row, dict)
        ]
        if clean:
            return clean

    # 2) Existing deterministic object parser may recover an object
    # embedded in prose/fences.
    parse_object = globals().get(
        "_studio_v2371c_parse_object"
    )
    if parse_object is not None:
        try:
            obj = parse_object(
                raw,
                preferred_keys=("shots",),
            )
        except Exception:
            obj = {}
        if isinstance(obj, dict):
            rows = obj.get("shots")
            if isinstance(rows, list):
                clean = [
                    row for row in rows
                    if isinstance(row, dict)
                ]
                if clean:
                    return clean

    # 3) Outer response may be truncated. Locate the "shots": [ array
    # and recover every complete object element before truncation.
    match = _studio_v2371h_re.search(
        r'["\']shots["\']\s*:\s*\[',
        raw,
        flags=_studio_v2371h_re.I,
    )
    if match:
        array_start = raw.find(
            "[",
            match.start(),
        )
        recovered = (
            _studio_v2371h_balanced_objects_from_array(
                raw,
                array_start,
            )
        )
        if recovered:
            return recovered

    # 4) Model sometimes returns a bare array after explanatory whitespace.
    first_array = raw.find("[")
    if first_array >= 0:
        recovered = (
            _studio_v2371h_balanced_objects_from_array(
                raw,
                first_array,
            )
        )
        if recovered:
            return recovered

    return []


def _studio_v2371h_extract_shots_any(
    value: object,
) -> list[dict]:
    if isinstance(value, dict):
        rows = value.get("shots")
        if isinstance(rows, list):
            clean = [
                row for row in rows
                if isinstance(row, dict)
            ]
            if clean:
                return clean

        content = str(
            value.get("content") or ""
        ).strip()
        if content:
            rows = (
                _studio_v2371h_extract_shots_from_text(
                    content
                )
            )
            if rows:
                return rows

    if isinstance(value, list):
        clean = [
            row for row in value
            if isinstance(row, dict)
        ]
        if clean:
            return clean

    collect = globals().get(
        "_studio_v2371a_collect_texts"
    )
    texts = (
        collect(value)
        if collect is not None
        else [str(value or "")]
    )

    for text in sorted(
        texts,
        key=len,
        reverse=True,
    ):
        rows = (
            _studio_v2371h_extract_shots_from_text(
                text
            )
        )
        if rows:
            return rows

    return []


def _studio_v2371h_prompt_beats(
    prompt: str,
) -> list[dict]:
    raw = str(prompt or "")
    marker = "=== BEATS_THIS_BATCH ==="
    start = raw.find(marker)
    if start < 0:
        return []

    start += len(marker)
    tail = raw[start:]

    next_markers = (
        "=== CONTINUITY ===",
        "=== ALLOWED_ENTITIES ===",
        "=== CHARACTER_ANCHOR ===",
    )
    end = len(tail)
    for item in next_markers:
        pos = tail.find(item)
        if pos >= 0:
            end = min(end, pos)

    parsed = _studio_v2371h_parse_jsonish(
        tail[:end].strip()
    )
    if not isinstance(parsed, list):
        return []

    return [
        row for row in parsed
        if isinstance(row, dict)
    ]


def _studio_v2371h_state_text(
    value: object,
) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return str(value or "").strip()

    for key in (
        "description",
        "state",
        "summary",
        "visual_prompt",
        "prompt",
    ):
        text = str(
            value.get(key) or ""
        ).strip()
        if text:
            return text

    # Last-resort structural preservation, not semantic invention.
    return _studio_json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _studio_v2371h_nested_prompt(
    value: object,
    keys: tuple[str, ...],
) -> str:
    if not isinstance(value, dict):
        return ""
    for key in keys:
        text = str(
            value.get(key) or ""
        ).strip()
        if text:
            return text
    return ""


def _studio_v2371h_normalize_shot(
    row: dict,
    *,
    compact_beats: list[dict],
) -> dict:
    if not isinstance(row, dict):
        return {}

    out = dict(row)

    # Common schema drift: Beat binding nested under "beats".
    nested_beats = out.get("beats")
    if isinstance(nested_beats, list):
        nested_orders = []
        nested_evidence = []
        for item in nested_beats:
            if not isinstance(item, dict):
                continue

            for value in (
                item.get("covered_beat_orders")
                or [item.get("beat_order")]
            ):
                try:
                    order = int(value)
                except Exception:
                    continue
                if (
                    order > 0
                    and order not in nested_orders
                ):
                    nested_orders.append(order)

            for value in (
                item.get("source_evidence_ids")
                or []
            ):
                key = str(value or "").strip()
                if (
                    key
                    and key not in nested_evidence
                ):
                    nested_evidence.append(key)

        if (
            not isinstance(
                out.get("covered_beat_orders"),
                list,
            )
            or not out.get("covered_beat_orders")
        ) and nested_orders:
            out[
                "covered_beat_orders"
            ] = nested_orders

        if (
            not isinstance(
                out.get("source_evidence_ids"),
                list,
            )
            or not out.get("source_evidence_ids")
        ) and nested_evidence:
            out[
                "source_evidence_ids"
            ] = nested_evidence

    rep_raw = out.get(
        "representative_state"
    )
    start_raw = out.get(
        "video_start_state"
    )
    end_raw = out.get(
        "video_end_state"
    )

    out["representative_state"] = (
        _studio_v2371h_state_text(
            rep_raw
        )
    )
    out["video_start_state"] = (
        _studio_v2371h_state_text(
            start_raw
        )
    )
    out["video_end_state"] = (
        _studio_v2371h_state_text(
            end_raw
        )
    )

    # Preserve prompts the model already supplied inside structured
    # state objects.
    if not str(
        out.get("image_prompt") or ""
    ).strip():
        prompt = _studio_v2371h_nested_prompt(
            rep_raw,
            (
                "visual_prompt",
                "image_prompt",
                "prompt",
            ),
        )
        if prompt:
            out["image_prompt"] = prompt

    if not str(
        out.get("video_start_prompt") or ""
    ).strip():
        prompt = _studio_v2371h_nested_prompt(
            start_raw,
            (
                "visual_prompt",
                "video_start_prompt",
                "prompt",
            ),
        )
        if prompt:
            out[
                "video_start_prompt"
            ] = prompt

    if not str(
        out.get("video_prompt") or ""
    ).strip():
        for candidate in (
            out.get("motion_prompt"),
            out.get("transition_prompt"),
            _studio_v2371h_nested_prompt(
                end_raw,
                (
                    "video_prompt",
                    "motion_prompt",
                    "transition_prompt",
                ),
            ),
        ):
            text = str(
                candidate or ""
            ).strip()
            if text:
                out["video_prompt"] = text
                break

    # If summary/action alone was omitted, recover summary ONLY from the
    # explicitly covered Beat text already present in this same prompt.
    # This does not invent new story semantics.
    if not (
        str(out.get("summary") or "").strip()
        or str(out.get("action") or "").strip()
    ):
        orders = []
        for value in (
            out.get("covered_beat_orders")
            or []
        ):
            try:
                order = int(value)
            except Exception:
                continue
            if (
                order > 0
                and order not in orders
            ):
                orders.append(order)

        beat_map = {
            int(beat.get("order") or 0):
                str(
                    beat.get("summary") or ""
                ).strip()
            for beat in compact_beats
            if isinstance(beat, dict)
            and int(beat.get("order") or 0) > 0
        }
        summaries = [
            beat_map[order]
            for order in orders
            if beat_map.get(order)
        ]
        if summaries:
            out["summary"] = "；".join(
                summaries
            )[:700]
            out[
                "summary_origin"
            ] = "covered-beat-summary"

    # Common entity aliases inside structured state objects.
    if not isinstance(
        out.get("character_entity_ids"),
        list,
    ):
        out["character_entity_ids"] = []
    if not isinstance(
        out.get("prop_entity_ids"),
        list,
    ):
        out["prop_entity_ids"] = []

    for state in (
        rep_raw,
        start_raw,
        end_raw,
    ):
        if not isinstance(state, dict):
            continue

        for key in (
            "character_entity_ids",
            "character_entities",
        ):
            values = state.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                entity_id = str(
                    value.get("entity_id")
                    if isinstance(value, dict)
                    else value
                ).strip()
                if (
                    entity_id
                    and entity_id
                    not in out[
                        "character_entity_ids"
                    ]
                ):
                    out[
                        "character_entity_ids"
                    ].append(entity_id)

        for key in (
            "prop_entity_ids",
            "prop_entities",
        ):
            values = state.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                entity_id = str(
                    value.get("entity_id")
                    if isinstance(value, dict)
                    else value
                ).strip()
                if (
                    entity_id
                    and entity_id
                    not in out[
                        "prop_entity_ids"
                    ]
                ):
                    out[
                        "prop_entity_ids"
                    ].append(entity_id)

    return out


def _studio_v2371h_normalize_rows(
    rows: list[dict],
    *,
    compact_beats: list[dict],
) -> list[dict]:
    result = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        normalized = (
            _studio_v2371h_normalize_shot(
                row,
                compact_beats=compact_beats,
            )
        )
        if normalized:
            result.append(normalized)
    return result


async def _studio_v2371_generate_batch(
    *,
    system_prompt: str,
    prompt: str,
    scene_index: int,
    scene_total: int,
    batch_index: int,
    batch_total: int,
) -> list[dict]:
    compact_beats = (
        _studio_v2371h_prompt_beats(
            prompt
        )
    )
    diagnostics = []

    attempts = (
        (
            0.08,
            "",
            3000,
        ),
        (
            0.0,
            "\n\nSTRICT_OUTPUT_RETRY:\n"
            "只输出一个 JSON 对象，根对象只能有 shots。"
            "不要回显 scene、BEATS_THIS_BATCH 或输入材料。"
            "默认一个 Beat 对应一个 Shot；只有同一 Beat 内确有"
            "不可在一个镜头表达的独立视觉状态变化才拆分。"
            "每个 Shot 必须填写 summary 或 action、三状态、三个 Prompt、"
            "covered_beat_orders、source_evidence_ids。"
            "状态字段请输出纯字符串，不要嵌套对象。",
            3000,
        ),
        (
            0.0,
            "\n\nFINAL_FORMAT_RETRY:\n"
            "返回严格 JSON：{\"shots\":[...]}。"
            "不要 Markdown，不要解释，不要输入回显。"
            "优先完整输出较少 Shot，禁止为了数量扩镜头。"
            "绝不能在 JSON 未闭合前继续增加 Shot。",
            2800,
        ),
    )

    for attempt, (
        temperature,
        suffix,
        max_tokens,
    ) in enumerate(attempts, 1):
        raw = None
        parsed = None

        try:
            raw, parsed, _ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "strict_contract_qwen32b"
                    ),
                    messages=[{
                        "role": "user",
                        "content": prompt + suffix,
                    }],
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    contract=(
                        _studio_v2371_batch_schema()
                    ),
                )
            )

            candidates = []

            parsed_rows = (
                parsed.get("shots")
                if isinstance(parsed, dict)
                else (
                    parsed
                    if isinstance(parsed, list)
                    else None
                )
            )
            if isinstance(parsed_rows, list):
                candidates.append(
                    (
                        "parsed",
                        parsed_rows,
                    )
                )

            raw_rows = (
                _studio_v2371h_extract_shots_any(
                    raw
                )
            )
            if raw_rows:
                candidates.append(
                    (
                        "raw_recovered",
                        raw_rows,
                    )
                )

            candidate_diags = []

            for source_name, rows in candidates:
                normalized = (
                    _studio_v2371h_normalize_rows(
                        rows,
                        compact_beats=compact_beats,
                    )
                )
                usable, reason = (
                    _studio_v2371a_rows_usable(
                        normalized
                    )
                )
                if usable:
                    return normalized

                candidate_diags.append(
                    f"{source_name}=({reason})"
                )

            if not candidate_diags:
                candidate_diags.append(
                    "no_shot_candidates"
                )

            raw_preview = ""
            collect = globals().get(
                "_studio_v2371a_collect_texts"
            )
            texts = (
                collect(raw)
                if collect is not None
                else [str(raw or "")]
            )
            if texts:
                raw_preview = (
                    _studio_v2371h_re.sub(
                        r"\s+",
                        " ",
                        max(
                            texts,
                            key=len,
                        ),
                    )[:520]
                )

            diagnostics.append(
                f"attempt={attempt}: "
                + " | ".join(
                    candidate_diags
                )
                + f"; raw_preview="
                + repr(raw_preview)
            )

        except Exception as exc:
            diagnostics.append(
                f"attempt={attempt}: "
                f"{type(exc).__name__}: "
                f"{str(exc)[:520]}"
            )

    raise RuntimeError(
        f"场景 {scene_index}/{scene_total} "
        f"批次 {batch_index + 1}/{batch_total} "
        "Qwen3-32B 未返回可验证的严格 Shot 合同；"
        + " || ".join(diagnostics)
    )
# ===== /V2.37.1H STAGE04 SHOT OUTPUT RESILIENCE =====


# ===== V2.37.2 R1 STAGE04 NARRATIVE BACKBONE =====
import copy as _studio_v2372_copy
import json as _studio_v2372_json
import re as _studio_v2372_re


# ---------------------------------------------------------------------
# 1. Scene source authority: ONLY the exact source_start:source_end span
#    is eligible for Beat evidence. Historical +/-220 context padding
#    must never become production Beat evidence.
# ---------------------------------------------------------------------
def _studio_stage04_scene_source(
    scene: dict,
    source_text: str,
) -> str:
    try:
        start = max(
            0,
            int(scene.get("source_start") or 0),
        )
        end = max(
            start,
            int(scene.get("source_end") or start),
        )
    except Exception:
        start, end = 0, 0

    source_text = str(source_text or "")
    if source_text and end > start:
        return source_text[
            start:min(end, len(source_text))
        ]

    return str(
        scene.get("source_excerpt") or ""
    ).strip()


def _studio_v2372_scene_range_guard(
    *,
    scene: dict,
    state: dict,
) -> None:
    try:
        current_start = int(
            scene.get("source_start") or 0
        )
        current_end = int(
            scene.get("source_end") or 0
        )
    except Exception:
        return

    if (
        current_start < 0
        or current_end <= current_start
    ):
        return

    current_id = str(
        scene.get("scene_id") or ""
    )

    rows = []
    for item in state.get("scenes") or []:
        if not isinstance(item, dict):
            continue
        try:
            start = int(
                item.get("source_start") or 0
            )
            end = int(
                item.get("source_end") or 0
            )
        except Exception:
            continue
        if end <= start:
            continue
        rows.append((
            start,
            end,
            str(item.get("scene_id") or ""),
            str(item.get("title") or ""),
        ))

    rows.sort(
        key=lambda x: (
            x[0],
            x[1],
            x[2],
        )
    )

    for index, row in enumerate(rows):
        if row[2] != current_id:
            continue

        for neighbor_index in (
            index - 1,
            index + 1,
        ):
            if not (
                0 <= neighbor_index < len(rows)
            ):
                continue
            other = rows[neighbor_index]
            overlap = max(
                0,
                min(current_end, other[1])
                - max(current_start, other[0]),
            )
            if overlap > 0:
                raise RuntimeError(
                    "严格 Stage04：Scene 小说正文范围发生重叠；"
                    "拒绝让同一段正文重复生成跨 Scene Beat。"
                    f" current={current_id}"
                    f"[{current_start},{current_end})"
                    f" other={other[2]}"
                    f"[{other[0]},{other[1]})"
                    f" overlap_chars={overlap}"
                )
        break


# ---------------------------------------------------------------------
# 2. Non-overlapping Scene-core chunks with stable absolute offsets.
# ---------------------------------------------------------------------
def _studio_v2372_source_chunks(
    source: str,
    *,
    max_chars: int = 1500,
) -> list[dict]:
    text = str(source or "")
    if not text.strip():
        return []

    result = []
    start = 0

    while start < len(text):
        hard_end = min(
            len(text),
            start + max_chars,
        )
        end = hard_end

        if hard_end < len(text):
            search_start = max(
                start + max_chars // 2,
                hard_end - 260,
            )
            region = text[
                search_start:hard_end
            ]
            matches = list(
                _studio_v2372_re.finditer(
                    r"[。！？!?；;\n]",
                    region,
                )
            )
            if matches:
                end = (
                    search_start
                    + matches[-1].end()
                )

        if end <= start:
            end = hard_end

        chunk_text = text[start:end]
        if chunk_text.strip():
            result.append({
                "index": len(result) + 1,
                "start": start,
                "end": end,
                "text": chunk_text,
                "context_before": text[
                    max(0, start - 180):start
                ],
                "context_after": text[
                    end:min(len(text), end + 180)
                ],
            })

        start = end

        if len(result) >= 20:
            if start < len(text):
                raise RuntimeError(
                    "严格 Stage04：单 Scene 正文超过 "
                    "Narrative Backbone 安全分块上限"
                )
            break

    return result


def _studio_v2372_chunk_anchors(
    chunk: dict,
) -> list[dict]:
    text = str(chunk.get("text") or "")
    chunk_start = int(
        chunk.get("start") or 0
    )
    chunk_index = int(
        chunk.get("index") or 1
    )

    pieces = []
    last = 0

    for match in _studio_v2372_re.finditer(
        r"[。！？!?；;]+|\n+",
        text,
    ):
        end = match.end()
        raw = text[last:end]
        stripped = raw.strip()

        if stripped:
            left_trim = len(raw) - len(
                raw.lstrip()
            )
            right_trim = len(raw.rstrip())

            local_start = last + left_trim
            local_end = last + right_trim

            pieces.append((
                stripped,
                local_start,
                local_end,
            ))

        last = end

    tail = text[last:]
    stripped = tail.strip()
    if stripped:
        left_trim = len(tail) - len(
            tail.lstrip()
        )
        right_trim = len(tail.rstrip())
        pieces.append((
            stripped,
            last + left_trim,
            last + right_trim,
        ))

    anchors = []

    for part, local_start, local_end in pieces:
        has_terminal = bool(
            _studio_v2372_re.search(
                r"[。！？!?；;]$",
                part,
            )
        )

        # Generic heading/filter only. No business keywords.
        if len(part) <= 36 and not has_terminal:
            continue

        if len(part) <= 190:
            spans = [(
                part,
                local_start,
                local_end,
            )]
        else:
            spans = []
            offset = 0
            while offset < len(part):
                segment = part[
                    offset:offset + 160
                ].strip()
                if segment:
                    raw_segment = part[
                        offset:offset + 160
                    ]
                    seg_left = (
                        len(raw_segment)
                        - len(raw_segment.lstrip())
                    )
                    seg_right = len(
                        raw_segment.rstrip()
                    )
                    spans.append((
                        segment,
                        local_start
                        + offset
                        + seg_left,
                        local_start
                        + offset
                        + seg_right,
                    ))
                offset += 140

        for segment, seg_start, seg_end in spans:
            anchor_id = (
                f"C{chunk_index:02d}"
                f"E{len(anchors)+1:03d}"
            )
            anchors.append({
                "id": anchor_id,
                "text": segment,
                "start": (
                    chunk_start + seg_start
                ),
                "end": (
                    chunk_start + seg_end
                ),
            })

            if len(anchors) >= 96:
                raise RuntimeError(
                    "严格 Stage04：单正文分块证据锚点过多；"
                    "拒绝静默截断正文覆盖"
                )

    if not anchors:
        raw = text.strip()
        if raw:
            pos = text.find(raw)
            anchors.append({
                "id": (
                    f"C{chunk_index:02d}E001"
                ),
                "text": raw,
                "start": (
                    chunk_start + max(0, pos)
                ),
                "end": (
                    chunk_start
                    + max(0, pos)
                    + len(raw)
                ),
            })

    return anchors


def _studio_v2372_extract_object(
    raw: object,
    parsed: object,
) -> dict:
    if isinstance(parsed, dict):
        return parsed

    parser = globals().get(
        "_studio_v2371c_parse_object"
    )
    if parser is not None:
        try:
            value = parser(
                str(raw or ""),
                preferred_keys=(
                    "beats",
                    "support_evidence_ids",
                    "valid",
                ),
            )
        except Exception:
            value = {}
        if isinstance(value, dict):
            return value

    return {}


def _studio_v2372_clean_entity_ids(
    values: object,
    *,
    allowed: set[str],
) -> list[str]:
    result = []
    for value in values or []:
        if isinstance(value, dict):
            key = str(
                value.get("entity_id") or ""
            ).strip()
        else:
            key = str(value or "").strip()

        if (
            key
            and key in allowed
            and key not in result
        ):
            result.append(key)

    return result


def _studio_v2372_exact_name_bindings(
    *,
    text: str,
    entity_rows: list[dict],
    entity_type: str,
    allowed: set[str],
) -> list[str]:
    result = []
    body = str(text or "")

    for entity in entity_rows or []:
        if not isinstance(entity, dict):
            continue

        if (
            str(
                entity.get("entity_type") or ""
            ).strip().lower()
            != entity_type
        ):
            continue

        entity_id = str(
            entity.get("entity_id") or ""
        ).strip()
        name = str(
            entity.get("name") or ""
        ).strip()

        if (
            entity_id
            and entity_id in allowed
            and name
            and name in body
            and entity_id not in result
        ):
            result.append(entity_id)

    return result


def _studio_v2372_normalize_support(
    value: object,
) -> list[str]:
    result = []

    for item in value or []:
        if isinstance(item, dict):
            values = (
                item.get("source_evidence_ids")
                or item.get("evidence_ids")
                or []
            )
        else:
            values = [item]

        for current in values:
            key = str(current or "").strip()
            if key and key not in result:
                result.append(key)

    return result


def _studio_v2372_validate_extraction(
    *,
    payload: dict,
    anchors: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[list[dict], list[str]]:
    anchor_map = {
        str(row["id"]): row
        for row in anchors
    }
    expected = set(anchor_map)

    raw_beats = payload.get("beats")
    if not isinstance(raw_beats, list):
        raw_beats = []

    support_ids = (
        _studio_v2372_normalize_support(
            payload.get(
                "support_evidence_ids"
            )
            or payload.get("support")
            or []
        )
    )

    cleaned = []
    used = set()

    for index, raw in enumerate(
        raw_beats,
        1,
    ):
        if not isinstance(raw, dict):
            continue

        summary = str(
            raw.get("summary") or ""
        ).strip()

        if not summary:
            raise RuntimeError(
                f"Beat#{index} summary 为空"
            )

        # E001 / C01E001 is a structural anchor id,
        # never a narrative summary.
        if _studio_v2372_re.fullmatch(
            r"(?:C\d{2})?E\d{3}",
            summary,
            flags=_studio_v2372_re.I,
        ):
            raise RuntimeError(
                f"Beat#{index} summary "
                "错误回显证据锚点 ID"
            )

        ids = []
        for value in (
            raw.get("source_evidence_ids")
            or []
        ):
            key = str(value or "").strip()
            if (
                key
                and key in anchor_map
                and key not in ids
            ):
                ids.append(key)

        if not ids:
            raise RuntimeError(
                f"Beat#{index} 没有有效小说正文证据"
            )

        duplicate = used.intersection(ids)
        if duplicate:
            raise RuntimeError(
                f"Beat#{index} 重复消费其他 Beat "
                "已经占用的正文证据："
                + repr(sorted(duplicate))
            )

        used.update(ids)

        evidence_text = [
            str(anchor_map[key]["text"])
            for key in ids
        ]
        spans = [{
            "id": key,
            "start": int(
                anchor_map[key]["start"]
            ),
            "end": int(
                anchor_map[key]["end"]
            ),
            "text": str(
                anchor_map[key]["text"]
            ),
        } for key in ids]

        combined = (
            summary
            + "\n"
            + "\n".join(evidence_text)
        )

        char_ids = (
            _studio_v2372_clean_entity_ids(
                raw.get(
                    "character_entity_ids"
                ),
                allowed=allowed_chars,
            )
        )
        prop_ids = (
            _studio_v2372_clean_entity_ids(
                raw.get("prop_entity_ids"),
                allowed=allowed_props,
            )
        )

        for key in (
            _studio_v2372_exact_name_bindings(
                text=combined,
                entity_rows=entity_rows,
                entity_type="character",
                allowed=allowed_chars,
            )
        ):
            if key not in char_ids:
                char_ids.append(key)

        for key in (
            _studio_v2372_exact_name_bindings(
                text=combined,
                entity_rows=entity_rows,
                entity_type="prop",
                allowed=allowed_props,
            )
        ):
            if key not in prop_ids:
                prop_ids.append(key)

        cleaned.append({
            "summary": summary[:700],
            "state_change": str(
                raw.get("state_change") or ""
            ).strip()[:500],
            "source_evidence_ids": ids,
            "source_evidence": evidence_text,
            "source_evidence_spans": spans,
            "character_entity_ids": char_ids,
            "prop_entity_ids": prop_ids,
        })

    support_set = {
        key
        for key in support_ids
        if key in anchor_map
    }

    overlap = used.intersection(
        support_set
    )
    if overlap:
        raise RuntimeError(
            "同一正文证据不能同时属于 Beat 和 support："
            + repr(sorted(overlap))
        )

    accounted = used.union(
        support_set
    )

    if accounted != expected:
        raise RuntimeError(
            "当前正文分块存在未分类证据；"
            f"missing={sorted(expected-accounted)} "
            f"unexpected={sorted(accounted-expected)}"
        )

    return cleaned, sorted(
        support_set,
        key=lambda key: (
            int(anchor_map[key]["start"]),
            key,
        ),
    )


async def _studio_v2372_audit_extraction(
    *,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
) -> dict:
    audit_beats = [{
        "index": index + 1,
        "summary": row.get("summary"),
        "state_change": row.get("state_change"),
        "source_evidence_ids":
            row.get("source_evidence_ids"),
        "source_evidence":
            row.get("source_evidence"),
    } for index, row in enumerate(beats)]

    system_prompt = (
        "你是小说 Narrative Beat 质量审计器，只审计不改写。"
        "正文锚点必须全部被分类，但不是每句话都应成为制作 Beat。"
        "生产 Beat 必须至少满足一种：改变角色/世界状态、推进因果动作、"
        "形成决定、发现、转折、到达新阶段，或提供会改变后续行为的重要信息。"
        "不得依据固定关键词、文本类别、题材类型或预设示例进行分类；"
        "不改变因果状态的修辞细节，应归为 support，而不是独立 Beat。"
        "同时检查：每个 Beat 的 summary/state_change 必须被它自己的"
        "source_evidence 直接支持；不得借邻近上下文补剧情；不得遗漏正文中"
        "真正会改变剧情状态的事件；Beat 顺序必须与原文一致。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== SOURCE_ANCHORS ===\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== PROPOSED_BEATS ===\n"
        + _studio_json.dumps(
            audit_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SUPPORT_EVIDENCE_IDS ===\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
    )

    raw, parsed, _ = (
        await _studio_v2371a_qwen_call(
            phase=(
                "studio_stage04_"
                "narrative_beat_audit_qwen32b"
            ),
            messages=[{
                "role": "user",
                "content": prompt,
            }],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=900,
            contract=(
                '{"valid":true,'
                '"event_coverage_ok":true,'
                '"granularity_ok":true,'
                '"evidence_entailment_ok":true,'
                '"temporal_order_ok":true,'
                '"support_classification_ok":true,'
                '"violations":[]}'
            ),
        )
    )

    audit = (
        parsed
        if isinstance(parsed, dict)
        else _studio_v2372_extract_object(
            raw,
            parsed,
        )
    )

    if not isinstance(audit, dict):
        return {
            "valid": False,
            "violations": [
                "Beat audit 未返回 JSON 对象"
            ],
        }

    required = (
        "event_coverage_ok",
        "granularity_ok",
        "evidence_entailment_ok",
        "temporal_order_ok",
        "support_classification_ok",
    )

    violations = audit.get(
        "violations"
    )
    if not isinstance(violations, list):
        violations = []

    if not all(
        audit.get(key) is True
        for key in required
    ):
        audit["valid"] = False

    if violations:
        audit["valid"] = False

    audit["violations"] = violations
    return audit


async def _studio_v2372_generate_chunk_beats(
    *,
    chunk: dict,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[list[dict], list[str]]:
    anchors = (
        _studio_v2372_chunk_anchors(
            chunk
        )
    )
    if not anchors:
        return [], []

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    previous_issues = ""

    for attempt in range(2):
        system_prompt = (
            "你是小说正文 Narrative Beat 提取器，运行 Qwen3-32B。"
            "只处理 CORE_SOURCE_CHUNK，不把前后 context 当可消费正文。"
            "目标不是逐句拆分，而是建立后续分镜需要的剧情骨架。"
            "Beat 必须代表真实的状态变化、因果推进、角色行为/决定、发现、"
            "转折、阶段到达或会改变后续行为的重要信息。"
            "不得依据固定关键词、文本类别、题材类型或预设示例进行分类；"
            "不改变剧情状态的修辞细节放到 support_evidence_ids，"
            "禁止单独制造 Beat。"
            "每个 SOURCE_ANCHOR 必须且只能被分类一次："
            "要么属于某个 Beat 的 source_evidence_ids，"
            "要么属于 support_evidence_ids。"
            "Beat summary/state_change 必须被它自己的证据直接支持，"
            "不得引用邻近 context 补写未发生事件。"
            "character_entity_ids / prop_entity_ids 只使用 ALLOWED_ENTITIES "
            "中的真实 ID；不确定留空。"
            "只输出严格 JSON。"
        )

        prompt = (
            f"CHUNK_PROGRESS="
            f"{chunk.get('index')}\n"
            "=== NON_ANCHOR_CONTEXT_BEFORE ===\n"
            + str(
                chunk.get(
                    "context_before"
                ) or ""
            )
            + "\n\n=== CORE_SOURCE_CHUNK ===\n"
            + str(chunk.get("text") or "")
            + "\n\n=== NON_ANCHOR_CONTEXT_AFTER ===\n"
            + str(
                chunk.get(
                    "context_after"
                ) or ""
            )
            + "\n\n=== SOURCE_ANCHORS ===\n"
            + _studio_json.dumps(
                anchors,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== ALLOWED_ENTITIES ===\n"
            + entity_text
        )

        if previous_issues:
            prompt += (
                "\n\n=== PREVIOUS_AUDIT_ISSUES ===\n"
                + previous_issues
                + "\n重新分类整个 CORE_SOURCE_CHUNK；"
                "不要仅修改说明。"
            )

        raw, parsed, _ = (
            await _studio_v2371a_qwen_call(
                phase=(
                    "studio_stage04_"
                    "narrative_beat_extraction_qwen32b"
                ),
                messages=[{
                    "role": "user",
                    "content": prompt,
                }],
                system_prompt=system_prompt,
                temperature=(
                    0.06 if attempt == 0
                    else 0.0
                ),
                max_tokens=1800,
                contract=(
                    '{"beats":[{'
                    '"summary":"",'
                    '"state_change":"",'
                    '"source_evidence_ids":["C01E001"],'
                    '"character_entity_ids":[],'
                    '"prop_entity_ids":[]'
                    '}],'
                    '"support_evidence_ids":["C01E002"]}'
                ),
            )
        )

        payload = (
            parsed
            if isinstance(parsed, dict)
            else _studio_v2372_extract_object(
                raw,
                parsed,
            )
        )

        try:
            beats, support_ids = (
                _studio_v2372_validate_extraction(
                    payload=payload,
                    anchors=anchors,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except RuntimeError as exc:
            previous_issues = (
                "DETERMINISTIC_EXTRACTION_ERROR: "
                + str(exc)
            )
            continue

        audit = (
            await _studio_v2372_audit_extraction(
                chunk=chunk,
                anchors=anchors,
                beats=beats,
                support_ids=support_ids,
            )
        )

        if (
            audit.get("valid") is True
            and not (
                audit.get("violations")
                or []
            )
        ):
            return beats, support_ids

        previous_issues = (
            _studio_json.dumps(
                audit.get("violations")
                or audit,
                ensure_ascii=False,
            )
        )

    raise RuntimeError(
        "严格 Stage04：Narrative Beat 提取两轮后"
        "仍未通过正文覆盖/粒度/证据蕴含审计："
        + previous_issues[:1200]
    )


# ---------------------------------------------------------------------
# 3. Replace old Beat recovery. No provisional-shot dependency, no
#    overlap context as evidence, no silent source-anchor omission.
# ---------------------------------------------------------------------
async def _studio_v2371b_ensure_scene_beats(
    *,
    project_id: str,
    scene: dict,
    state: dict,
    source: str,
    allowed_chars: set[str],
    allowed_props: set[str],
) -> tuple[list[dict], str]:
    _studio_v2372_scene_range_guard(
        scene=scene,
        state=state,
    )

    source = str(source or "")
    if not source.strip():
        raise RuntimeError(
            "严格 Stage04：当前 Scene 没有核心小说正文"
        )

    entities = {
        str(row.get("entity_id") or ""): {
            "entity_id": str(
                row.get("entity_id") or ""
            ),
            "entity_type": str(
                row.get("entity_type") or ""
            ),
            "name": str(
                row.get("name") or ""
            ),
        }
        for row in (
            director.production.list_entities(
                project_id
            )
        )
        if str(row.get("entity_id") or "")
    }

    visible_ids = [
        *sorted(allowed_chars),
        *sorted(allowed_props),
    ]
    entity_rows = [
        entities[eid]
        for eid in visible_ids
        if eid in entities
    ]

    chunks = _studio_v2372_source_chunks(
        source,
        max_chars=1500,
    )

    if not chunks:
        raise RuntimeError(
            "严格 Stage04：Scene 核心正文无法建立"
            " Narrative Backbone 分块"
        )

    gathered = []

    for chunk in chunks:
        rows, _support = (
            await _studio_v2372_generate_chunk_beats(
                chunk=chunk,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                entity_rows=entity_rows,
            )
        )

        for row in rows:
            gathered.append(row)

    if not gathered:
        raise RuntimeError(
            "严格 Stage04：Scene 正文经过 Narrative "
            "Backbone 审计后没有任何可制作剧情 Beat"
        )

    # Exact source positions are the authoritative time order.
    gathered.sort(
        key=lambda row: (
            min(
                [
                    int(span.get("start") or 0)
                    for span in (
                        row.get(
                            "source_evidence_spans"
                        )
                        or []
                    )
                ]
                or [0]
            ),
            max(
                [
                    int(span.get("end") or 0)
                    for span in (
                        row.get(
                            "source_evidence_spans"
                        )
                        or []
                    )
                ]
                or [0]
            ),
            str(row.get("summary") or ""),
        )
    )

    beats = []

    for order, row in enumerate(
        gathered,
        1,
    ):
        beat = {
            "order": order,
            "summary": str(
                row.get("summary") or ""
            )[:700],
            "state_change": str(
                row.get("state_change") or ""
            )[:500],
            "character_entity_ids": list(
                row.get(
                    "character_entity_ids"
                ) or []
            ),
            "prop_entity_ids": list(
                row.get(
                    "prop_entity_ids"
                ) or []
            ),
            "source_evidence_ids": list(
                row.get(
                    "source_evidence_ids"
                ) or []
            ),
            "source_evidence": list(
                row.get(
                    "source_evidence"
                ) or []
            ),
            "source_evidence_spans": list(
                row.get(
                    "source_evidence_spans"
                ) or []
            ),
            "beat_source": (
                "qwen3-32b-narrative-backbone"
            ),
        }
        beats.append(beat)

    scene_id = str(
        scene.get("scene_id") or ""
    )
    episode_id = str(
        scene.get("episode_id") or ""
    )

    # In-memory only. Successful formal replacement removes these.
    state_rows = state.setdefault(
        "shots",
        [],
    )

    for beat in beats:
        state_rows.append({
            "shot_id": (
                "beat_runtime_v2372_"
                + scene_id
                + "_"
                + str(beat["order"])
            ),
            "scene_id": scene_id,
            "episode_id": episode_id,
            "order": int(
                beat["order"]
            ),
            "sequence": (
                int(
                    scene.get("sequence")
                    or 0
                )
                * 1000
                + int(beat["order"])
            ),
            "summary": beat["summary"],
            "state_change":
                beat["state_change"],
            "character_entity_ids": list(
                beat.get(
                    "character_entity_ids"
                ) or []
            ),
            "prop_entity_ids": list(
                beat.get(
                    "prop_entity_ids"
                ) or []
            ),
            "source_evidence_ids": list(
                beat.get(
                    "source_evidence_ids"
                ) or []
            ),
            "source_evidence": list(
                beat.get(
                    "source_evidence"
                ) or []
            ),
            "source_evidence_spans": list(
                beat.get(
                    "source_evidence_spans"
                ) or []
            ),
            "beat_source": (
                "qwen3-32b-narrative-backbone"
            ),
            "provisional": True,
        })

    return (
        beats,
        "qwen3-32b-narrative-backbone",
    )


# ---------------------------------------------------------------------
# 4. Evidence lineage: use exact source offsets when available so
#    repeated identical sentences cannot bind to an earlier occurrence.
# ---------------------------------------------------------------------
def _studio_v2371e_batch_evidence(
    *,
    source: str,
    batch: list[dict],
    max_context_chars: int = 1900,
) -> tuple[
    str,
    list[dict],
    dict[int, list[str]],
]:
    source = str(source or "")
    anchors = []
    beat_to_anchor_ids = {}
    span_to_id = {}
    positions = []

    for beat in batch:
        order = int(
            beat.get("order") or 0
        )
        if order <= 0:
            raise RuntimeError(
                "严格 Stage04：Beat 缺少有效 order"
            )

        spans = [
            span
            for span in (
                beat.get(
                    "source_evidence_spans"
                )
                or []
            )
            if isinstance(span, dict)
        ]

        if not spans:
            # Legacy/provisional fallback only.
            spans = []
            cursor = 0
            for text in (
                beat.get("source_evidence")
                or []
            ):
                value = str(
                    text or ""
                ).strip()
                if not value:
                    continue
                pos = source.find(
                    value,
                    cursor,
                )
                if pos < 0:
                    pos = source.find(value)
                if pos < 0:
                    raise RuntimeError(
                        f"严格 Stage04：Beat {order} "
                        "原文证据无法定位"
                    )
                spans.append({
                    "start": pos,
                    "end": pos + len(value),
                    "text": value,
                })
                cursor = pos + len(value)

        beat_ids = []

        for span in spans:
            start = int(
                span.get("start") or 0
            )
            end = int(
                span.get("end") or 0
            )
            text = str(
                span.get("text") or ""
            )

            if (
                start < 0
                or end <= start
                or end > len(source)
                or source[start:end] != text
            ):
                raise RuntimeError(
                    f"严格 Stage04：Beat {order} "
                    "证据 offset 与 Scene 核心正文不一致"
                )

            key = (
                start,
                end,
                text,
            )

            anchor_id = span_to_id.get(
                key
            )

            if not anchor_id:
                anchor_id = (
                    f"E{len(anchors)+1:03d}"
                )
                span_to_id[key] = anchor_id
                anchors.append({
                    "id": anchor_id,
                    "text": text,
                    "beat_order": order,
                    "source_start": start,
                    "source_end": end,
                })

            if anchor_id not in beat_ids:
                beat_ids.append(
                    anchor_id
                )

            positions.append(
                (start, end)
            )

        if not beat_ids:
            raise RuntimeError(
                f"严格 Stage04：Beat {order} "
                "没有可用于 Shot 的核心正文证据"
            )

        beat_to_anchor_ids[
            order
        ] = beat_ids

    if not anchors:
        raise RuntimeError(
            "严格 Stage04：当前 Beat 批次没有核心正文证据"
        )

    lo = min(
        x[0] for x in positions
    )
    hi = max(
        x[1] for x in positions
    )

    if hi - lo <= max_context_chars:
        spare = max_context_chars - (
            hi - lo
        )
        left = max(
            0,
            lo - spare // 2,
        )
        right = min(
            len(source),
            hi
            + (
                spare
                - (lo - left)
            ),
        )
        left = max(
            0,
            right - max_context_chars,
        )
        context = source[
            left:right
        ].strip()
    else:
        context = "\n".join(
            (
                f"[{row['id']}|"
                f"Beat {row['beat_order']}] "
                f"{row['text']}"
            )
            for row in anchors
        )

    return (
        context,
        anchors,
        beat_to_anchor_ids,
    )


# ---------------------------------------------------------------------
# 5. Normalize more Qwen state schemas. "characters"/"props" are common
#    and were not handled in V2.37.1h.
# ---------------------------------------------------------------------
_V2372_R1_PREVIOUS_NORMALIZE_SHOT = (
    _studio_v2371h_normalize_shot
)


def _studio_v2371h_normalize_shot(
    row: dict,
    *,
    compact_beats: list[dict],
) -> dict:
    out = (
        _V2372_R1_PREVIOUS_NORMALIZE_SHOT(
            row,
            compact_beats=compact_beats,
        )
    )

    if not isinstance(out, dict):
        return out

    char_ids = list(
        out.get(
            "character_entity_ids"
        )
        or []
    )
    prop_ids = list(
        out.get("prop_entity_ids")
        or []
    )

    for state_key in (
        "representative_state",
        "video_start_state",
        "video_end_state",
    ):
        state = (
            row.get(state_key)
            if isinstance(row, dict)
            else None
        )
        if not isinstance(state, dict):
            continue

        for key in (
            "characters",
            "character_entities",
            "character_entity_ids",
        ):
            values = state.get(key)
            if not isinstance(values, list):
                continue

            for value in values:
                entity_id = str(
                    value.get("entity_id")
                    if isinstance(value, dict)
                    else value
                ).strip()

                if (
                    entity_id
                    and entity_id
                    not in char_ids
                ):
                    char_ids.append(
                        entity_id
                    )

        for key in (
            "props",
            "prop_entities",
            "prop_entity_ids",
        ):
            values = state.get(key)
            if not isinstance(values, list):
                continue

            for value in values:
                entity_id = str(
                    value.get("entity_id")
                    if isinstance(value, dict)
                    else value
                ).strip()

                if (
                    entity_id
                    and entity_id
                    not in prop_ids
                ):
                    prop_ids.append(
                        entity_id
                    )

    out[
        "character_entity_ids"
    ] = char_ids
    out[
        "prop_entity_ids"
    ] = prop_ids
    return out


# ---------------------------------------------------------------------
# 6. Shot pre-validation recovery is deterministic only:
#    - anchor-id summary -> covered Beat summary
#    - empty top-level entities -> covered Beat entities (Beat-level, never
#      Scene-wide)
# ---------------------------------------------------------------------
_V2372_R1_PREVIOUS_VALIDATE_ROWS = (
    _studio_v2371_validate_rows
)


def _studio_v2372_orders(
    value: object,
) -> list[int]:
    result = []
    for item in value or []:
        try:
            order = int(item)
        except Exception:
            continue
        if (
            order > 0
            and order not in result
        ):
            result.append(order)
    return result


def _studio_v2371_validate_rows(
    *,
    raw_rows: list[dict],
    compact_beats: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    anchors: list[dict],
    scene_id: str,
    episode_id: str,
) -> list[dict]:
    beat_map = {
        int(row.get("order") or 0): row
        for row in (
            compact_beats or []
        )
        if isinstance(row, dict)
        and int(row.get("order") or 0) > 0
    }

    prepared = []

    for original in (
        raw_rows or []
    ):
        if not isinstance(
            original,
            dict,
        ):
            prepared.append(original)
            continue

        row = _studio_v2372_copy.deepcopy(
            original
        )

        orders = (
            _studio_v2372_orders(
                row.get(
                    "covered_beat_orders"
                )
            )
        )

        summary = str(
            row.get("summary") or ""
        ).strip()

        evidence_ids = {
            str(x or "").strip()
            for x in (
                row.get(
                    "source_evidence_ids"
                )
                or []
            )
            if str(x or "").strip()
        }

        bad_summary = bool(
            _studio_v2372_re.fullmatch(
                r"(?:C\d{2})?E\d{3}",
                summary,
                flags=_studio_v2372_re.I,
            )
        ) or (
            summary
            and summary in evidence_ids
        )

        if (
            (not summary or bad_summary)
            and orders
        ):
            beat_summaries = [
                str(
                    beat_map[order].get(
                        "summary"
                    ) or ""
                ).strip()
                for order in orders
                if order in beat_map
                and str(
                    beat_map[order].get(
                        "summary"
                    ) or ""
                ).strip()
            ]
            if beat_summaries:
                row["summary"] = "；".join(
                    beat_summaries
                )[:700]
                row[
                    "summary_origin"
                ] = (
                    "covered-beat-summary"
                )

        if orders:
            if not (
                row.get(
                    "character_entity_ids"
                )
                or []
            ):
                inherited = []
                for order in orders:
                    beat = beat_map.get(
                        order
                    ) or {}
                    for entity_id in (
                        beat.get(
                            "character_entity_ids"
                        )
                        or []
                    ):
                        key = str(
                            entity_id or ""
                        ).strip()
                        if (
                            key
                            and key in allowed_chars
                            and key
                            not in inherited
                        ):
                            inherited.append(
                                key
                            )
                if inherited:
                    row[
                        "character_entity_ids"
                    ] = inherited

            if not (
                row.get(
                    "prop_entity_ids"
                )
                or []
            ):
                inherited = []
                for order in orders:
                    beat = beat_map.get(
                        order
                    ) or {}
                    for entity_id in (
                        beat.get(
                            "prop_entity_ids"
                        )
                        or []
                    ):
                        key = str(
                            entity_id or ""
                        ).strip()
                        if (
                            key
                            and key in allowed_props
                            and key
                            not in inherited
                        ):
                            inherited.append(
                                key
                            )
                if inherited:
                    row[
                        "prop_entity_ids"
                    ] = inherited

        prepared.append(row)

    rows = _V2372_R1_PREVIOUS_VALIDATE_ROWS(
        raw_rows=prepared,
        compact_beats=compact_beats,
        allowed_chars=allowed_chars,
        allowed_props=allowed_props,
        anchors=anchors,
        scene_id=scene_id,
        episode_id=episode_id,
    )

    anchor_ids = {
        str(anchor.get("id") or "")
        for anchor in anchors or []
        if isinstance(anchor, dict)
    }

    for index, row in enumerate(
        rows,
        1,
    ):
        summary = str(
            row.get("summary") or ""
        ).strip()

        if (
            not summary
            or _studio_v2372_re.fullmatch(
                r"(?:C\d{2})?E\d{3}",
                summary,
                flags=_studio_v2372_re.I,
            )
            or summary in anchor_ids
        ):
            raise RuntimeError(
                f"严格 Stage04：Shot {index} "
                "summary 仍是证据锚点/空值，拒绝写入"
            )

    return rows


# ---------------------------------------------------------------------
# 7. Shot audit: exact selected evidence is authoritative. Wider context
#    may explain continuity but can NEVER justify an event absent from the
#    selected evidence / covered Beat.
# ---------------------------------------------------------------------
async def _studio_v2371_audit_batch(
    *,
    source_window: str,
    compact_beats: list[dict],
    shots: list[dict],
) -> dict:
    audit_rows = [{
        "index": index + 1,
        "title": row.get("title"),
        "covered_beat_orders":
            row.get("covered_beat_orders"),
        "summary": row.get("summary"),
        "action": row.get("action"),
        "representative_state":
            row.get("representative_state"),
        "video_start_state":
            row.get("video_start_state"),
        "video_end_state":
            row.get("video_end_state"),
        "image_prompt":
            row.get("image_prompt"),
        "video_start_prompt":
            row.get("video_start_prompt"),
        "video_prompt":
            row.get("video_prompt"),
        "source_evidence_ids":
            row.get("source_evidence_ids"),
        "source_evidence":
            row.get("source_evidence"),
        "character_entity_ids":
            row.get("character_entity_ids"),
        "prop_entity_ids":
            row.get("prop_entity_ids"),
    } for index, row in enumerate(shots)]

    system_prompt = (
        "你是 strict-shot-v2 制作合同审计器，只审计不改写。"
        "每个 Shot 自己的 source_evidence 是叙事事实最高权威；"
        "covered Beat 只能概括这些证据。较宽 source context "
        "只能帮助理解前后关系，绝不能用来替 Shot 补一个其已选择证据中"
        "不存在的事件。"
        "必须检查："
        "1. evidence_entailment：summary/action/三状态/三个 Prompt "
        "均被该 Shot 自己的 source_evidence 和 covered Beat 直接支持；"
        "2. Beat 显式覆盖；"
        "3. 时间单调，不提前消费后续事件；"
        "4. 不重复播放已经完成的结果；"
        "5. video_start→representative→video_end 因果顺序成立；"
        "6. representative 是当前 Beat 的信息帧；"
        "7. 角色/道具 ID 只表示该 Shot 真实涉及的实体。"
        "任意一项不满足必须 valid=false，并写入 violations。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== CONTEXT_ONLY_NOT_EVIDENCE ===\n"
        + str(source_window or "")
        + "\n\n=== COVERED_BEATS ===\n"
        + _studio_json.dumps(
            compact_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SHOTS_WITH_EXACT_EVIDENCE ===\n"
        + _studio_json.dumps(
            audit_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    raw, parsed, _ = (
        await _studio_v2371a_qwen_call(
            phase=(
                "studio_stage04_"
                "strict_evidence_temporal_audit_qwen32b"
            ),
            messages=[{
                "role": "user",
                "content": prompt,
            }],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=1000,
            contract=(
                '{"valid":true,'
                '"evidence_entailment_ok":true,'
                '"beat_coverage_ok":true,'
                '"temporal_monotonic":true,'
                '"no_future_event_preconsumption":true,'
                '"no_result_duplication":true,'
                '"state_order_valid":true,'
                '"entity_visibility_valid":true,'
                '"violations":[]}'
            ),
        )
    )

    audit = (
        parsed
        if isinstance(parsed, dict)
        else _studio_v2372_extract_object(
            raw,
            parsed,
        )
    )

    if not isinstance(audit, dict):
        return {
            "valid": False,
            "violations": [
                "Shot evidence audit "
                "未返回 JSON 对象"
            ],
        }

    required = (
        "evidence_entailment_ok",
        "beat_coverage_ok",
        "temporal_monotonic",
        "no_future_event_preconsumption",
        "no_result_duplication",
        "state_order_valid",
        "entity_visibility_valid",
    )

    violations = audit.get(
        "violations"
    )
    if not isinstance(violations, list):
        violations = []

    missing_or_false = [
        key for key in required
        if audit.get(key) is not True
    ]

    if missing_or_false:
        audit["valid"] = False
        if not violations:
            violations.append(
                "audit fields not true: "
                + ", ".join(
                    missing_or_false
                )
            )

    if violations:
        audit["valid"] = False

    audit["violations"] = violations
    return audit


# ---------------------------------------------------------------------
# 8. Strict Shot prompt is refined generically; no story keywords.
# ---------------------------------------------------------------------
_V2372_R1_PREVIOUS_GENERATE_BATCH = (
    _studio_v2371_generate_batch
)


async def _studio_v2371_generate_batch(
    *,
    system_prompt: str,
    prompt: str,
    scene_index: int,
    scene_total: int,
    batch_index: int,
    batch_total: int,
) -> list[dict]:
    hardened_system = (
        str(system_prompt or "")
        + "\n新增硬约束："
        "每个 Shot 的 summary/action/representative_state/"
        "video_start_state/video_end_state/image_prompt/"
        "video_start_prompt/video_prompt 必须只描述该 Shot "
        "实际选择的 source_evidence_ids 与 covered Beat "
        "能够直接支持的事实。"
        "不得从同一 source context 的其他未选择句子借用事件。"
        "若当前 Beat 主要是描述性 support，而不是剧情状态变化，"
        "不应凭空扩写成新事件。"
        "character_entity_ids / prop_entity_ids 必须使用"
        " ALLOWED_ENTITIES 中的真实 ID；禁止编造近似 ID。"
    )

    return await _V2372_R1_PREVIOUS_GENERATE_BATCH(
        system_prompt=hardened_system,
        prompt=prompt,
        scene_index=scene_index,
        scene_total=scene_total,
        batch_index=batch_index,
        batch_total=batch_total,
    )


# ===== /V2.37.2 STAGE04 NARRATIVE BACKBONE =====


# ===== V2.37.2 R2 STAGE04 MODEL-DRIVEN NARRATIVE BACKBONE =====
import copy as _studio_v2372_copy
import json as _studio_v2372_json
import re as _studio_v2372_re


# ---------------------------------------------------------------------
# 1. Scene source authority: ONLY the exact source_start:source_end span
#    is eligible for Beat evidence. Historical +/-220 context padding
#    must never become production Beat evidence.
# ---------------------------------------------------------------------
def _studio_stage04_scene_source(
    scene: dict,
    source_text: str,
) -> str:
    try:
        start = max(
            0,
            int(scene.get("source_start") or 0),
        )
        end = max(
            start,
            int(scene.get("source_end") or start),
        )
    except Exception:
        start, end = 0, 0

    source_text = str(source_text or "")
    if source_text and end > start:
        return source_text[
            start:min(end, len(source_text))
        ]

    return str(
        scene.get("source_excerpt") or ""
    ).strip()


def _studio_v2372_scene_range_guard(
    *,
    scene: dict,
    state: dict,
) -> None:
    try:
        current_start = int(
            scene.get("source_start") or 0
        )
        current_end = int(
            scene.get("source_end") or 0
        )
    except Exception:
        return

    if (
        current_start < 0
        or current_end <= current_start
    ):
        return

    current_id = str(
        scene.get("scene_id") or ""
    )

    rows = []
    for item in state.get("scenes") or []:
        if not isinstance(item, dict):
            continue
        try:
            start = int(
                item.get("source_start") or 0
            )
            end = int(
                item.get("source_end") or 0
            )
        except Exception:
            continue
        if end <= start:
            continue
        rows.append((
            start,
            end,
            str(item.get("scene_id") or ""),
            str(item.get("title") or ""),
        ))

    rows.sort(
        key=lambda x: (
            x[0],
            x[1],
            x[2],
        )
    )

    for index, row in enumerate(rows):
        if row[2] != current_id:
            continue

        for neighbor_index in (
            index - 1,
            index + 1,
        ):
            if not (
                0 <= neighbor_index < len(rows)
            ):
                continue
            other = rows[neighbor_index]
            overlap = max(
                0,
                min(current_end, other[1])
                - max(current_start, other[0]),
            )
            if overlap > 0:
                raise RuntimeError(
                    "严格 Stage04：Scene 小说正文范围发生重叠；"
                    "拒绝让同一段正文重复生成跨 Scene Beat。"
                    f" current={current_id}"
                    f"[{current_start},{current_end})"
                    f" other={other[2]}"
                    f"[{other[0]},{other[1]})"
                    f" overlap_chars={overlap}"
                )
        break


# ---------------------------------------------------------------------
# 2. Non-overlapping Scene-core chunks with stable absolute offsets.
# ---------------------------------------------------------------------
def _studio_v2372_source_chunks(
    source: str,
    *,
    max_chars: int = 1500,
) -> list[dict]:
    text = str(source or "")
    if not text.strip():
        return []

    result = []
    start = 0

    while start < len(text):
        hard_end = min(
            len(text),
            start + max_chars,
        )
        end = hard_end

        if hard_end < len(text):
            search_start = max(
                start + max_chars // 2,
                hard_end - 260,
            )
            region = text[
                search_start:hard_end
            ]
            matches = list(
                _studio_v2372_re.finditer(
                    r"[。！？!?；;\n]",
                    region,
                )
            )
            if matches:
                end = (
                    search_start
                    + matches[-1].end()
                )

        if end <= start:
            end = hard_end

        chunk_text = text[start:end]
        if chunk_text.strip():
            result.append({
                "index": len(result) + 1,
                "start": start,
                "end": end,
                "text": chunk_text,
                "context_before": text[
                    max(0, start - 180):start
                ],
                "context_after": text[
                    end:min(len(text), end + 180)
                ],
            })

        start = end

        if len(result) >= 20:
            if start < len(text):
                raise RuntimeError(
                    "严格 Stage04：单 Scene 正文超过 "
                    "Narrative Backbone 安全分块上限"
                )
            break

    return result


def _studio_v2372_chunk_anchors(
    chunk: dict,
) -> list[dict]:
    text = str(chunk.get("text") or "")
    chunk_start = int(
        chunk.get("start") or 0
    )
    chunk_index = int(
        chunk.get("index") or 1
    )

    pieces = []
    last = 0

    for match in _studio_v2372_re.finditer(
        r"[。！？!?；;]+|\n+",
        text,
    ):
        end = match.end()
        raw = text[last:end]
        stripped = raw.strip()

        if stripped:
            left_trim = len(raw) - len(
                raw.lstrip()
            )
            right_trim = len(raw.rstrip())

            local_start = last + left_trim
            local_end = last + right_trim

            pieces.append((
                stripped,
                local_start,
                local_end,
            ))

        last = end

    tail = text[last:]
    stripped = tail.strip()
    if stripped:
        left_trim = len(tail) - len(
            tail.lstrip()
        )
        right_trim = len(tail.rstrip())
        pieces.append((
            stripped,
            last + left_trim,
            last + right_trim,
        ))

    anchors = []

    for part, local_start, local_end in pieces:
        has_terminal = bool(
            _studio_v2372_re.search(
                r"[。！？!?；;]$",
                part,
            )
        )

        # Generic heading/filter only. No business keywords.
        if len(part) <= 36 and not has_terminal:
            continue

        if len(part) <= 190:
            spans = [(
                part,
                local_start,
                local_end,
            )]
        else:
            spans = []
            offset = 0
            while offset < len(part):
                segment = part[
                    offset:offset + 160
                ].strip()
                if segment:
                    raw_segment = part[
                        offset:offset + 160
                    ]
                    seg_left = (
                        len(raw_segment)
                        - len(raw_segment.lstrip())
                    )
                    seg_right = len(
                        raw_segment.rstrip()
                    )
                    spans.append((
                        segment,
                        local_start
                        + offset
                        + seg_left,
                        local_start
                        + offset
                        + seg_right,
                    ))
                offset += 140

        for segment, seg_start, seg_end in spans:
            anchor_id = (
                f"C{chunk_index:02d}"
                f"E{len(anchors)+1:03d}"
            )
            anchors.append({
                "id": anchor_id,
                "text": segment,
                "start": (
                    chunk_start + seg_start
                ),
                "end": (
                    chunk_start + seg_end
                ),
            })

            if len(anchors) >= 96:
                raise RuntimeError(
                    "严格 Stage04：单正文分块证据锚点过多；"
                    "拒绝静默截断正文覆盖"
                )

    if not anchors:
        raw = text.strip()
        if raw:
            pos = text.find(raw)
            anchors.append({
                "id": (
                    f"C{chunk_index:02d}E001"
                ),
                "text": raw,
                "start": (
                    chunk_start + max(0, pos)
                ),
                "end": (
                    chunk_start
                    + max(0, pos)
                    + len(raw)
                ),
            })

    return anchors


def _studio_v2372_extract_object(
    raw: object,
    parsed: object,
) -> dict:
    if isinstance(parsed, dict):
        return parsed

    parser = globals().get(
        "_studio_v2371c_parse_object"
    )
    if parser is not None:
        try:
            value = parser(
                str(raw or ""),
                preferred_keys=(
                    "beats",
                    "support_evidence_ids",
                    "valid",
                ),
            )
        except Exception:
            value = {}
        if isinstance(value, dict):
            return value

    return {}


def _studio_v2372_clean_entity_ids(
    values: object,
    *,
    allowed: set[str],
) -> list[str]:
    result = []
    for value in values or []:
        if isinstance(value, dict):
            key = str(
                value.get("entity_id") or ""
            ).strip()
        else:
            key = str(value or "").strip()

        if (
            key
            and key in allowed
            and key not in result
        ):
            result.append(key)

    return result


def _studio_v2372_exact_name_bindings(
    *,
    text: str,
    entity_rows: list[dict],
    entity_type: str,
    allowed: set[str],
) -> list[str]:
    result = []
    body = str(text or "")

    for entity in entity_rows or []:
        if not isinstance(entity, dict):
            continue

        if (
            str(
                entity.get("entity_type") or ""
            ).strip().lower()
            != entity_type
        ):
            continue

        entity_id = str(
            entity.get("entity_id") or ""
        ).strip()
        name = str(
            entity.get("name") or ""
        ).strip()

        if (
            entity_id
            and entity_id in allowed
            and name
            and name in body
            and entity_id not in result
        ):
            result.append(entity_id)

    return result


def _studio_v2372_normalize_support(
    value: object,
) -> list[str]:
    result = []

    for item in value or []:
        if isinstance(item, dict):
            values = (
                item.get("source_evidence_ids")
                or item.get("evidence_ids")
                or []
            )
        else:
            values = [item]

        for current in values:
            key = str(current or "").strip()
            if key and key not in result:
                result.append(key)

    return result


def _studio_v2372_validate_extraction(
    *,
    payload: dict,
    anchors: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[list[dict], list[str]]:
    anchor_map = {
        str(row["id"]): row
        for row in anchors
    }
    expected = set(anchor_map)

    raw_beats = payload.get("beats")
    if not isinstance(raw_beats, list):
        raw_beats = []

    support_ids = (
        _studio_v2372_normalize_support(
            payload.get(
                "support_evidence_ids"
            )
            or payload.get("support")
            or []
        )
    )

    cleaned = []
    used = set()

    for index, raw in enumerate(
        raw_beats,
        1,
    ):
        if not isinstance(raw, dict):
            continue

        summary = str(
            raw.get("summary") or ""
        ).strip()

        if not summary:
            raise RuntimeError(
                f"Beat#{index} summary 为空"
            )

        # E001 / C01E001 is a structural anchor id,
        # never a narrative summary.
        if _studio_v2372_re.fullmatch(
            r"(?:C\d{2})?E\d{3}",
            summary,
            flags=_studio_v2372_re.I,
        ):
            raise RuntimeError(
                f"Beat#{index} summary "
                "错误回显证据锚点 ID"
            )

        ids = []
        for value in (
            raw.get("source_evidence_ids")
            or []
        ):
            key = str(value or "").strip()
            if (
                key
                and key in anchor_map
                and key not in ids
            ):
                ids.append(key)

        if not ids:
            raise RuntimeError(
                f"Beat#{index} 没有有效小说正文证据"
            )

        duplicate = used.intersection(ids)
        if duplicate:
            raise RuntimeError(
                f"Beat#{index} 重复消费其他 Beat "
                "已经占用的正文证据："
                + repr(sorted(duplicate))
            )

        used.update(ids)

        evidence_text = [
            str(anchor_map[key]["text"])
            for key in ids
        ]
        spans = [{
            "id": key,
            "start": int(
                anchor_map[key]["start"]
            ),
            "end": int(
                anchor_map[key]["end"]
            ),
            "text": str(
                anchor_map[key]["text"]
            ),
        } for key in ids]

        combined = (
            summary
            + "\n"
            + "\n".join(evidence_text)
        )

        char_ids = (
            _studio_v2372_clean_entity_ids(
                raw.get(
                    "character_entity_ids"
                ),
                allowed=allowed_chars,
            )
        )
        prop_ids = (
            _studio_v2372_clean_entity_ids(
                raw.get("prop_entity_ids"),
                allowed=allowed_props,
            )
        )

        for key in (
            _studio_v2372_exact_name_bindings(
                text=combined,
                entity_rows=entity_rows,
                entity_type="character",
                allowed=allowed_chars,
            )
        ):
            if key not in char_ids:
                char_ids.append(key)

        for key in (
            _studio_v2372_exact_name_bindings(
                text=combined,
                entity_rows=entity_rows,
                entity_type="prop",
                allowed=allowed_props,
            )
        ):
            if key not in prop_ids:
                prop_ids.append(key)

        cleaned.append({
            "summary": summary[:700],
            "state_change": str(
                raw.get("state_change") or ""
            ).strip()[:500],
            "source_evidence_ids": ids,
            "source_evidence": evidence_text,
            "source_evidence_spans": spans,
            "character_entity_ids": char_ids,
            "prop_entity_ids": prop_ids,
        })

    support_set = {
        key
        for key in support_ids
        if key in anchor_map
    }

    overlap = used.intersection(
        support_set
    )
    if overlap:
        raise RuntimeError(
            "同一正文证据不能同时属于 Beat 和 support："
            + repr(sorted(overlap))
        )

    accounted = used.union(
        support_set
    )

    if accounted != expected:
        raise RuntimeError(
            "当前正文分块存在未分类证据；"
            f"missing={sorted(expected-accounted)} "
            f"unexpected={sorted(accounted-expected)}"
        )

    return cleaned, sorted(
        support_set,
        key=lambda key: (
            int(anchor_map[key]["start"]),
            key,
        ),
    )


async def _studio_v2372_audit_extraction(
    *,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
) -> dict:
    audit_beats = [{
        "index": index + 1,
        "summary": row.get("summary"),
        "state_change": row.get("state_change"),
        "source_evidence_ids":
            row.get("source_evidence_ids"),
        "source_evidence":
            row.get("source_evidence"),
    } for index, row in enumerate(beats)]

    system_prompt = (
        "你是小说 Narrative Beat 质量审计器，只审计不改写。"
        "正文锚点必须全部被分类，但分类不能依赖固定关键词、文本类别或预设题材规则。"
        "对每个候选单元，判断：如果从当前 Scene 的最小有序叙事状态图中移除它，"
        "是否会改变后续可重建的状态、因果关系或必要上下文依赖。"
        "会改变则作为 Beat；不会改变则作为 support。这个判断必须由当前正文上下文得出，"
        "不得使用预定义业务词表或题材枚举。"
        "同时检查：每个 Beat 的 summary/state_change 必须被它自己的"
        "source_evidence 直接支持；不得借邻近上下文补剧情；不得遗漏正文中"
        "真正会改变剧情状态的事件；Beat 顺序必须与原文一致。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== SOURCE_ANCHORS ===\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== PROPOSED_BEATS ===\n"
        + _studio_json.dumps(
            audit_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SUPPORT_EVIDENCE_IDS ===\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
    )

    raw, parsed, _ = (
        await _studio_v2371a_qwen_call(
            phase=(
                "studio_stage04_"
                "narrative_beat_audit_qwen32b"
            ),
            messages=[{
                "role": "user",
                "content": prompt,
            }],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=900,
            contract=(
                '{"valid":true,'
                '"event_coverage_ok":true,'
                '"granularity_ok":true,'
                '"evidence_entailment_ok":true,'
                '"temporal_order_ok":true,'
                '"support_classification_ok":true,'
                '"violations":[]}'
            ),
        )
    )

    audit = (
        parsed
        if isinstance(parsed, dict)
        else _studio_v2372_extract_object(
            raw,
            parsed,
        )
    )

    if not isinstance(audit, dict):
        return {
            "valid": False,
            "violations": [
                "Beat audit 未返回 JSON 对象"
            ],
        }

    required = (
        "event_coverage_ok",
        "granularity_ok",
        "evidence_entailment_ok",
        "temporal_order_ok",
        "support_classification_ok",
    )

    violations = audit.get(
        "violations"
    )
    if not isinstance(violations, list):
        violations = []

    if not all(
        audit.get(key) is True
        for key in required
    ):
        audit["valid"] = False

    if violations:
        audit["valid"] = False

    audit["violations"] = violations
    return audit


async def _studio_v2372_generate_chunk_beats(
    *,
    chunk: dict,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[list[dict], list[str]]:
    anchors = (
        _studio_v2372_chunk_anchors(
            chunk
        )
    )
    if not anchors:
        return [], []

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    previous_issues = ""

    for attempt in range(2):
        system_prompt = (
            "你是小说正文 Narrative Beat 提取器，运行 Qwen3-32B。"
            "只处理 CORE_SOURCE_CHUNK，不把前后 context 当可消费正文。"
            "目标不是逐句拆分，而是由模型建立当前 Scene 的最小有序叙事状态图。"
            "每个证据单元是否成为 Beat，只依据它对该状态图的必要性判断："
            "移除后会改变后续状态、因果关系或必要上下文依赖，则归入 Beat；"
            "移除后不改变该状态图，则归入 support_evidence_ids。"
            "不得依据固定关键词、文本类别、题材类型或预设示例进行分类。"
            "每个 SOURCE_ANCHOR 必须且只能被分类一次："
            "要么属于某个 Beat 的 source_evidence_ids，"
            "要么属于 support_evidence_ids。"
            "Beat summary/state_change 必须被它自己的证据直接支持，"
            "不得引用邻近 context 补写未发生事件。"
            "character_entity_ids / prop_entity_ids 只使用 ALLOWED_ENTITIES "
            "中的真实 ID；不确定留空。"
            "只输出严格 JSON。"
        )

        prompt = (
            f"CHUNK_PROGRESS="
            f"{chunk.get('index')}\n"
            "=== NON_ANCHOR_CONTEXT_BEFORE ===\n"
            + str(
                chunk.get(
                    "context_before"
                ) or ""
            )
            + "\n\n=== CORE_SOURCE_CHUNK ===\n"
            + str(chunk.get("text") or "")
            + "\n\n=== NON_ANCHOR_CONTEXT_AFTER ===\n"
            + str(
                chunk.get(
                    "context_after"
                ) or ""
            )
            + "\n\n=== SOURCE_ANCHORS ===\n"
            + _studio_json.dumps(
                anchors,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== ALLOWED_ENTITIES ===\n"
            + entity_text
        )

        if previous_issues:
            prompt += (
                "\n\n=== PREVIOUS_AUDIT_ISSUES ===\n"
                + previous_issues
                + "\n重新分类整个 CORE_SOURCE_CHUNK；"
                "不要仅修改说明。"
            )

        raw, parsed, _ = (
            await _studio_v2371a_qwen_call(
                phase=(
                    "studio_stage04_"
                    "narrative_beat_extraction_qwen32b"
                ),
                messages=[{
                    "role": "user",
                    "content": prompt,
                }],
                system_prompt=system_prompt,
                temperature=(
                    0.06 if attempt == 0
                    else 0.0
                ),
                max_tokens=1800,
                contract=(
                    '{"beats":[{'
                    '"summary":"",'
                    '"state_change":"",'
                    '"source_evidence_ids":["C01E001"],'
                    '"character_entity_ids":[],'
                    '"prop_entity_ids":[]'
                    '}],'
                    '"support_evidence_ids":["C01E002"]}'
                ),
            )
        )

        payload = (
            parsed
            if isinstance(parsed, dict)
            else _studio_v2372_extract_object(
                raw,
                parsed,
            )
        )

        try:
            beats, support_ids = (
                _studio_v2372_validate_extraction(
                    payload=payload,
                    anchors=anchors,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except RuntimeError as exc:
            previous_issues = (
                "DETERMINISTIC_EXTRACTION_ERROR: "
                + str(exc)
            )
            continue

        audit = (
            await _studio_v2372_audit_extraction(
                chunk=chunk,
                anchors=anchors,
                beats=beats,
                support_ids=support_ids,
            )
        )

        if (
            audit.get("valid") is True
            and not (
                audit.get("violations")
                or []
            )
        ):
            return beats, support_ids

        previous_issues = (
            _studio_json.dumps(
                audit.get("violations")
                or audit,
                ensure_ascii=False,
            )
        )

    raise RuntimeError(
        "严格 Stage04：Narrative Beat 提取两轮后"
        "仍未通过正文覆盖/粒度/证据蕴含审计："
        + previous_issues[:1200]
    )


# ---------------------------------------------------------------------
# 3. Replace old Beat recovery. No provisional-shot dependency, no
#    overlap context as evidence, no silent source-anchor omission.
# ---------------------------------------------------------------------
async def _studio_v2371b_ensure_scene_beats(
    *,
    project_id: str,
    scene: dict,
    state: dict,
    source: str,
    allowed_chars: set[str],
    allowed_props: set[str],
) -> tuple[list[dict], str]:
    # V2.39.10_STAGE04_SCENE_BATCHED_NARRATIVE
    _studio_v2372_scene_range_guard(
        scene=scene,
        state=state,
    )

    source = str(source or "")

    if not source.strip():
        raise RuntimeError(
            "严格 Stage04：当前 Scene 没有核心小说正文"
        )

    entities = {
        str(
            row.get("entity_id")
            or ""
        ): {
            "entity_id":
                str(
                    row.get("entity_id")
                    or ""
                ),
            "entity_type":
                str(
                    row.get("entity_type")
                    or ""
                ),
            "name":
                str(
                    row.get("name")
                    or ""
                ),
        }
        for row in (
            director.production.list_entities(
                project_id
            )
        )
        if str(
            row.get("entity_id")
            or ""
        )
    }

    visible_ids = [
        *sorted(allowed_chars),
        *sorted(allowed_props),
    ]

    entity_rows = [
        entities[eid]
        for eid in visible_ids
        if eid in entities
    ]

    # ----------------------------------------------------------
    # V2.39.10:
    # 1500 chars micro-chunk
    #     ->
    # 3600 chars non-overlapping Scene superchunk
    #
    # Evidence offsets remain Scene-relative and authoritative.
    # ----------------------------------------------------------
    # V2.39.10.1_STAGE04_ADAPTIVE_SUPERCHUNK
    #
    # 不再固定使用 3600 字。
    # 目标：
    # - 尽量减少 microchunk 数量；
    # - 同时保证单块 anchor 数量不会逼近旧 96-anchor
    #   安全上限；
    # - 不静默丢弃任何原文证据。
    #
    # 64 是性能/上下文折中，不是语义阈值。
    # 只控制批大小，不参与剧情分类。
    # V2.39.10.2_STAGE04_ADAPTIVE_SUPERCHUNK_FIX
    #
    # 继续优先较大 superchunk；
    # 对高密度正文允许继续缩小，避免为了性能而撞旧 96-anchor 硬保护。
    candidate_sizes = (
        3000,
        2700,
        2400,
        2200,
        2000,
        1800,
        1600,
        1500,
        1400,
        1200,
        1000,
        900,
        800,
    )

    chunks = None
    selected_chunk_chars = None
    selected_anchor_counts = None

    perf_observer = globals().get(
        "_studio_v2396_perf_observe"
    )

    for candidate_chars in candidate_sizes:
        candidate_chunks = (
            _studio_v2372_source_chunks(
                source,
                max_chars=candidate_chars,
            )
        )

        if not candidate_chunks:
            continue

        anchor_counts = []
        candidate_ok = True

        for candidate_chunk in candidate_chunks:
            anchor_started = (
                _studio_asyncio.get_running_loop().time()
            )
            candidate_anchors = []
            try:
                candidate_anchors = (
                    _studio_v2372_chunk_anchors(
                        candidate_chunk
                    )
                )
            except RuntimeError as exc:
                # 旧保护仍然保留。
                if "证据锚点过多" in str(exc):
                    candidate_ok = False
                    break
                raise
            finally:
                if callable(perf_observer):
                    perf_observer(
                        "anchor_extraction",
                        _studio_asyncio.get_running_loop().time()
                        - anchor_started,
                        kind="superchunk_probe",
                        candidate_chars=candidate_chars,
                        anchor_count=len(candidate_anchors),
                    )

            anchor_count = len(
                candidate_anchors
            )

            anchor_counts.append(
                anchor_count
            )

            # 原实现真正硬保护为 96。
            # 这里仅留少量余量，不能像 V2.39.10.1 那样用 64
            # 过早拒绝正常的高密度正文。
            if anchor_count > 88:
                candidate_ok = False
                break

        print(
            "[V2.39.10.2][Stage04][SuperchunkProbe] "
            f"candidate_chars={candidate_chars} "
            f"chunks={len(candidate_chunks)} "
            f"anchor_counts={anchor_counts} "
            f"accepted={candidate_ok}",
            flush=True,
        )

        if candidate_ok:
            chunks = candidate_chunks
            selected_chunk_chars = (
                candidate_chars
            )
            selected_anchor_counts = (
                anchor_counts
            )
            break

    if not chunks:
        raise RuntimeError(
            "严格 Stage04：Scene 正文缩小到 800 字分块后"
            "仍无法控制证据锚点规模；拒绝静默截断正文覆盖"
        )

    print(
        "[V2.39.10.2][Stage04][Superchunk] "
        f"source_chars={len(source)} "
        f"selected_max_chars={selected_chunk_chars} "
        f"chunks={len(chunks)} "
        f"anchor_counts={selected_anchor_counts}",
        flush=True,
    )

    # Classification 只输出 ID partition，适合大 batch。
    # Grouping JSON 较重，因此控制在 20 anchors。
    globals()[
        "_STUDIO_V2374_CLASSIFY_BATCH_SIZE"
    ] = 40

    globals()[
        "_STUDIO_V2374_GROUP_BATCH_SIZE"
    ] = 20

    globals()[
        "_STUDIO_V2374_REPAIR_BATCH_SIZE"
    ] = 8

    gathered: list[dict] = []

    scene_anchors: list[dict] = []

    scene_support_ids: list[str] = []

    # V2.39.10.3_STAGE04_SHARDED_SEMANTIC_AUDIT
    # 每个 superchunk 独立语义审计，避免把整个 Scene
    # 的全部 evidence JSON 一次塞进 8192 context。
    scene_semantic_jobs = []

    print(
        "[V2.39.10][Stage04][Narrative] "
        f"scene={scene.get('scene_id') or ''} "
        f"source_chars={len(source)} "
        f"superchunks={len(chunks)}",
        flush=True,
    )

    # ==========================================================
    # Superchunk:
    # Qwen classification
    #   -> Qwen grouping
    #   -> deterministic validation
    #
    # 不再每个 chunk 单独做 semantic audit。
    # ==========================================================
    for (
        chunk_index,
        chunk,
    ) in enumerate(
        chunks,
        1,
    ):
        anchor_started = (
            _studio_asyncio.get_running_loop().time()
        )
        anchors = (
            _studio_v2372_chunk_anchors(
                chunk
            )
        )
        if callable(perf_observer):
            perf_observer(
                "anchor_extraction",
                _studio_asyncio.get_running_loop().time()
                - anchor_started,
                kind="selected_superchunk",
                anchor_count=len(anchors),
                chunk_index=chunk_index,
            )

        if not anchors:
            continue

        print(
            "[V2.39.10][Stage04][Narrative] "
            f"superchunk={chunk_index}/{len(chunks)} "
            f"phase=anchor_classification "
            f"anchors={len(anchors)}",
            flush=True,
        )

        (
            beat_ids,
            support_ids,
        ) = (
            await _studio_v2374_classify_all(
                chunk=chunk,
                anchors=anchors,
            )
        )

        if not beat_ids:
            raise RuntimeError(
                "V2.39.10: 当前 superchunk "
                "classification 没有产生任何 beat_ids；"
                f"chunk={chunk_index}/{len(chunks)}"
            )

        print(
            "[V2.39.10][Stage04][Narrative] "
            f"superchunk={chunk_index}/{len(chunks)} "
            f"phase=beat_grouping "
            f"beat_anchor_ids={len(beat_ids)}",
            flush=True,
        )

        grouped_rows = (
            await _studio_v2374_group_all(
                chunk=chunk,
                anchors=anchors,
                beat_ids=beat_ids,
                allowed_chars=
                    allowed_chars,
                allowed_props=
                    allowed_props,
                entity_rows=
                    entity_rows,
            )
        )

        payload = {
            "beats":
                grouped_rows,
            "support_evidence_ids":
                support_ids,
        }

        (
            beats,
            validated_support,
            missing_ids,
        ) = (
            _studio_v2372c_validate_partial(
                payload=payload,
                anchors=anchors,
                allowed_chars=
                    allowed_chars,
                allowed_props=
                    allowed_props,
                entity_rows=
                    entity_rows,
            )
        )

        # 只有真正漏项才重新进 Qwen。
        if missing_ids:
            print(
                "[V2.39.10][Stage04][Narrative] "
                f"superchunk={chunk_index}/{len(chunks)} "
                "phase=targeted_missing_completion "
                f"missing={len(missing_ids)}",
                flush=True,
            )

            (
                beats,
                validated_support,
            ) = (
                await _studio_v2372c_complete_missing(
                    chunk=chunk,
                    anchors=anchors,
                    beats=beats,
                    support_ids=
                        validated_support,
                    missing_ids=
                        missing_ids,
                    allowed_chars=
                        allowed_chars,
                    allowed_props=
                        allowed_props,
                    entity_rows=
                        entity_rows,
                )
            )

        # V2.39.6.2: source offsets are the deterministic temporal substrate.
        # Rebuild evidence projections from IDs, keeping each Beat's semantic
        # description attached to its own evidence closure.
        beats = _studio_v23962_close_validated_beats(
            beats,
            anchors=anchors,
        )

        # ------------------------------------------------------
        # Python deterministic exactly-once partition
        # ------------------------------------------------------
        expected = {
            str(
                row.get("id")
                or ""
            )
            for row in anchors
            if str(
                row.get("id")
                or ""
            )
        }

        used = {
            str(
                evidence_id
                or ""
            ).strip()
            for beat in beats
            for evidence_id in (
                beat.get(
                    "source_evidence_ids"
                )
                or []
            )
            if str(
                evidence_id
                or ""
            ).strip()
        }

        support = {
            str(
                evidence_id
                or ""
            ).strip()
            for evidence_id
            in validated_support
            if str(
                evidence_id
                or ""
            ).strip()
        }

        overlap = (
            used
            & support
        )

        accounted = (
            used
            | support
        )

        if (
            overlap
            or accounted != expected
        ):
            raise RuntimeError(
                "V2.39.10: superchunk "
                "deterministic partition failed；"
                f"chunk={chunk_index}/{len(chunks)} "
                f"overlap={sorted(overlap)} "
                f"missing={sorted(expected-accounted)} "
                f"unexpected={sorted(accounted-expected)}"
            )

        scene_anchors.extend(
            anchors
        )

        gathered.extend(
            beats
        )

        for key in validated_support:
            if key not in scene_support_ids:
                scene_support_ids.append(
                    key
                )

        scene_semantic_jobs.append({
            "chunk_index":
                chunk_index,
            "chunk":
                dict(chunk),
            "anchors":
                [dict(x) for x in anchors],
            "beats":
                [dict(x) for x in beats],
            "support_ids":
                list(validated_support),
        })

        print(
            "[V2.39.10][Stage04][Narrative] "
            f"superchunk={chunk_index}/{len(chunks)} "
            "phase=deterministic_pass "
            f"beats={len(beats)} "
            f"support={len(validated_support)}",
            flush=True,
        )

    if not gathered:
        raise RuntimeError(
            "严格 Stage04：Scene 正文经过 "
            "Narrative Backbone 后没有任何可制作 Beat"
        )

    # ==========================================================
    # Scene-wide deterministic lineage validation
    # ==========================================================
    all_anchor_ids = {
        str(
            row.get("id")
            or ""
        )
        for row in scene_anchors
        if str(
            row.get("id")
            or ""
        )
    }

    all_used_ids = {
        str(
            evidence_id
            or ""
        ).strip()
        for beat in gathered
        for evidence_id in (
            beat.get(
                "source_evidence_ids"
            )
            or []
        )
        if str(
            evidence_id
            or ""
        ).strip()
    }

    all_support_ids = {
        str(
            evidence_id
            or ""
        ).strip()
        for evidence_id
        in scene_support_ids
        if str(
            evidence_id
            or ""
        ).strip()
    }

    overlap = (
        all_used_ids
        & all_support_ids
    )

    accounted = (
        all_used_ids
        | all_support_ids
    )

    if (
        overlap
        or accounted
        != all_anchor_ids
    ):
        raise RuntimeError(
            "V2.39.10: Scene deterministic "
            "anchor partition failed；"
            f"overlap={sorted(overlap)} "
            f"missing="
            f"{sorted(all_anchor_ids-accounted)} "
            f"unexpected="
            f"{sorted(accounted-all_anchor_ids)}"
        )

    # Exact offset lock.
    for (
        beat_index,
        beat,
    ) in enumerate(
        gathered,
        1,
    ):
        spans = [
            span
            for span in (
                beat.get(
                    "source_evidence_spans"
                )
                or []
            )
            if isinstance(
                span,
                dict,
            )
        ]

        if not spans:
            raise RuntimeError(
                f"V2.39.10: Beat#{beat_index} "
                "缺少 source_evidence_spans"
            )

        for span in spans:
            start = int(
                span.get("start")
                or 0
            )

            end = int(
                span.get("end")
                or 0
            )

            evidence_text = str(
                span.get("text")
                or ""
            )

            if (
                start < 0
                or end <= start
                or end > len(source)
                or source[
                    start:end
                ] != evidence_text
            ):
                raise RuntimeError(
                    f"V2.39.10: Beat#{beat_index} "
                    "evidence offset 与 Scene 正文不一致"
                )

    # 正文 offset 为唯一时间顺序。
    gathered.sort(
        key=lambda row: (
            min(
                [
                    int(
                        span.get(
                            "start"
                        )
                        or 0
                    )
                    for span in (
                        row.get(
                            "source_evidence_spans"
                        )
                        or []
                    )
                    if isinstance(
                        span,
                        dict,
                    )
                ]
                or [10**18]
            ),
            max(
                [
                    int(
                        span.get(
                            "end"
                        )
                        or 0
                    )
                    for span in (
                        row.get(
                            "source_evidence_spans"
                        )
                        or []
                    )
                    if isinstance(
                        span,
                        dict,
                    )
                ]
                or [10**18]
            ),
            str(
                row.get(
                    "summary"
                )
                or ""
            ),
        )
    )

    # ==========================================================
    # V2.39.10.3 SHARDED SEMANTIC AUDIT
    #
    # 语义审计按 superchunk 执行。
    # 每个 chunk 仍检查：
    # - event coverage
    # - granularity
    # - evidence entailment
    # - temporal order
    # - support classification
    #
    # Scene 全局只再检查 Beat 边界/时间/重复，
    # 不重复发送 191 个 anchor 的完整正文。
    # ==========================================================

    chunk_audits = []

    for (
        audit_job_index,
        audit_job,
    ) in enumerate(
        scene_semantic_jobs,
        1,
    ):
        print(
            "[V2.39.10.3][Stage04][Audit] "
            f"superchunk="
            f"{audit_job_index}/{len(scene_semantic_jobs)} "
            f"phase=semantic_audit "
            f"anchors={len(audit_job['anchors'])} "
            f"beats={len(audit_job['beats'])}",
            flush=True,
        )

        audit = (
            await _studio_v2372_audit_extraction(
                chunk=
                    audit_job["chunk"],
                anchors=
                    audit_job["anchors"],
                beats=
                    audit_job["beats"],
                support_ids=
                    audit_job["support_ids"],
            )
        )

        violations = (
            audit.get("violations")
            if isinstance(
                audit,
                dict,
            )
            else [
                "semantic audit "
                "did not return object"
            ]
        )

        if not isinstance(
            violations,
            list,
        ):
            violations = []

        if (
            not isinstance(
                audit,
                dict,
            )
            or audit.get(
                "valid"
            ) is not True
            or violations
        ):
            raise RuntimeError(
                "V2.39.10.3 "
                "SUPERCHUNK_NARRATIVE_AUDIT failed："
                + _studio_json.dumps(
                    audit,
                    ensure_ascii=False,
                )[:2200]
            )

        chunk_audits.append(
            audit
        )

        print(
            "[V2.39.10.3][Stage04][Audit] "
            f"superchunk="
            f"{audit_job_index}/{len(scene_semantic_jobs)} "
            "phase=semantic_pass",
            flush=True,
        )

    # ----------------------------------------------------------
    # Compact Scene-global audit
    #
    # 不发送完整 anchors。
    # 这里只验证跨 superchunk 边界：
    # - Beat 时间是否递增
    # - 是否发生跨 chunk 重复
    # - 是否错误拆分/合并
    # ----------------------------------------------------------
    compact_scene_beats = []

    for (
        beat_index,
        beat,
    ) in enumerate(
        gathered,
        1,
    ):
        spans = [
            span
            for span in (
                beat.get(
                    "source_evidence_spans"
                )
                or []
            )
            if isinstance(
                span,
                dict,
            )
        ]

        starts = [
            int(
                span.get("start")
                or 0
            )
            for span in spans
        ]

        ends = [
            int(
                span.get("end")
                or 0
            )
            for span in spans
        ]

        compact_scene_beats.append({
            "beat_index":
                beat_index,
            "summary":
                str(
                    beat.get(
                        "summary"
                    )
                    or ""
                )[:240],
            "state_change":
                str(
                    beat.get(
                        "state_change"
                    )
                    or ""
                )[:200],
            "evidence_start":
                min(starts)
                if starts
                else None,
            "evidence_end":
                max(ends)
                if ends
                else None,
        })

    global_system = (
        "你是 Scene Narrative Beat 跨分块边界审计器。"
        "每个 superchunk 内部已经分别通过正文覆盖、"
        "证据蕴含、support classification 和粒度审计。"
        "你只检查当前 Scene 的 Beat 序列跨分块边界是否正确："
        "1. temporal_order_ok：正文 offset 与剧情时间推进不存在倒置；"
        "2. cross_chunk_granularity_ok：分块边界没有把同一个"
        "不可分割状态变化错误切成两个 Beat，也没有错误合并独立结果；"
        "3. no_cross_chunk_duplication：相邻分块没有重复消费"
        "同一剧情结果。"
        "不得根据固定关键词、题材类型或预设故事模板判断。"
        "只返回严格 JSON。"
    )

    global_prompt = (
        "=== ORDERED_SCENE_BEATS ===\n"
        + _studio_json.dumps(
            compact_scene_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    required_global = (
        "temporal_order_ok",
        "cross_chunk_granularity_ok",
        "no_cross_chunk_duplication",
    )

    global_audit = None

    print(
        "[V2.39.10.3][Stage04][Audit] "
        "phase=compact_scene_global_audit "
        f"beats={len(compact_scene_beats)}",
        flush=True,
    )

    for global_attempt in range(2):
        suffix = ""

        if global_attempt:
            suffix = (
                "\n\nSTRICT_SCHEMA_RETRY："
                "必须显式返回三个 boolean 和 violations；"
                "禁止省略字段。"
            )

        (
            raw,
            parsed,
            _,
        ) = (
            await _studio_v2371a_qwen_call(
                phase=(
                    "studio_stage04_"
                    "v239103_compact_scene_audit_qwen32b"
                ),
                messages=[{
                    "role":
                        "user",
                    "content":
                        global_prompt
                        + suffix,
                }],
                system_prompt=
                    global_system,
                temperature=0.0,
                max_tokens=360,
                contract=(
                    '{"valid":true,'
                    '"temporal_order_ok":true,'
                    '"cross_chunk_granularity_ok":true,'
                    '"no_cross_chunk_duplication":true,'
                    '"violations":[]}'
                ),
            )
        )

        candidate = (
            parsed
            if isinstance(
                parsed,
                dict,
            )
            else (
                _studio_v2372_extract_object(
                    raw,
                    parsed,
                )
            )
        )

        if not isinstance(
            candidate,
            dict,
        ):
            candidate = {}

        violations = (
            candidate.get(
                "violations"
            )
        )

        if not isinstance(
            violations,
            list,
        ):
            violations = []

        false_fields = [
            key
            for key
            in required_global
            if candidate.get(
                key
            ) is False
        ]

        if (
            false_fields
            or violations
        ):
            raise RuntimeError(
                "V2.39.10.3 "
                "COMPACT_SCENE_AUDIT failed："
                + _studio_json.dumps(
                    {
                        "false_fields":
                            false_fields,
                        "violations":
                            violations,
                    },
                    ensure_ascii=False,
                )[:1800]
            )

        missing = [
            key
            for key
            in required_global
            if candidate.get(
                key
            ) is not True
        ]

        if not missing:
            global_audit = (
                candidate
            )
            global_audit[
                "valid"
            ] = True
            global_audit[
                "violations"
            ] = []
            break

        if global_attempt == 1:
            raise RuntimeError(
                "V2.39.10.3 "
                "COMPACT_SCENE_AUDIT "
                "schema incomplete："
                + repr(missing)
            )

    if global_audit is None:
        raise RuntimeError(
            "V2.39.10.3 "
            "COMPACT_SCENE_AUDIT "
            "没有显式通过"
        )

    final_audit = {
        "valid":
            True,
        "strategy":
            "v23910.3-sharded-semantic-audit",
        "superchunk_audit_count":
            len(chunk_audits),
        "cross_chunk":
            global_audit,
        "violations":
            [],
    }

    # ==========================================================
    # Formal ordered Beats
    # ==========================================================
    beats: list[dict] = []

    for (
        order,
        row,
    ) in enumerate(
        gathered,
        1,
    ):
        beats.append({
            "order":
                order,
            "summary":
                str(
                    row.get(
                        "summary"
                    )
                    or ""
                )[:700],
            "state_change":
                str(
                    row.get(
                        "state_change"
                    )
                    or ""
                )[:500],
            "character_entity_ids":
                list(
                    row.get(
                        "character_entity_ids"
                    )
                    or []
                ),
            "prop_entity_ids":
                list(
                    row.get(
                        "prop_entity_ids"
                    )
                    or []
                ),
            "source_evidence_ids":
                list(
                    row.get(
                        "source_evidence_ids"
                    )
                    or []
                ),
            "source_evidence":
                list(
                    row.get(
                        "source_evidence"
                    )
                    or []
                ),
            "source_evidence_spans":
                list(
                    row.get(
                        "source_evidence_spans"
                    )
                    or []
                ),
            "beat_source":
                (
                    "qwen3-32b-"
                    "narrative-backbone-"
                    "v23910-scene-batched"
                ),
            "scene_narrative_audit":
                final_audit,
        })

    scene_id = str(
        scene.get(
            "scene_id"
        )
        or ""
    )

    episode_id = str(
        scene.get(
            "episode_id"
        )
        or ""
    )

    state_rows = state.setdefault(
        "shots",
        [],
    )

    for beat in beats:
        state_rows.append({
            "shot_id":
                (
                    "beat_runtime_v23910_"
                    + scene_id
                    + "_"
                    + str(
                        beat[
                            "order"
                        ]
                    )
                ),
            "scene_id":
                scene_id,
            "episode_id":
                episode_id,
            "order":
                int(
                    beat[
                        "order"
                    ]
                ),
            "sequence":
                (
                    int(
                        scene.get(
                            "sequence"
                        )
                        or 0
                    )
                    * 1000
                    + int(
                        beat[
                            "order"
                        ]
                    )
                ),
            "summary":
                beat[
                    "summary"
                ],
            "state_change":
                beat[
                    "state_change"
                ],
            "character_entity_ids":
                list(
                    beat.get(
                        "character_entity_ids"
                    )
                    or []
                ),
            "prop_entity_ids":
                list(
                    beat.get(
                        "prop_entity_ids"
                    )
                    or []
                ),
            "source_evidence_ids":
                list(
                    beat.get(
                        "source_evidence_ids"
                    )
                    or []
                ),
            "source_evidence":
                list(
                    beat.get(
                        "source_evidence"
                    )
                    or []
                ),
            "source_evidence_spans":
                list(
                    beat.get(
                        "source_evidence_spans"
                    )
                    or []
                ),
            "beat_source":
                (
                    "qwen3-32b-"
                    "narrative-backbone-"
                    "v23910-scene-batched"
                ),
            "scene_narrative_audit":
                final_audit,
            "provisional":
                True,
        })

    print(
        "[V2.39.10][Stage04][Narrative] "
        f"scene_complete "
        f"beats={len(beats)} "
        f"anchors={len(scene_anchors)} "
        f"support={len(scene_support_ids)}",
        flush=True,
    )

    return (
        beats,
        (
            "qwen3-32b-"
            "narrative-backbone-"
            "v23910-scene-batched"
        ),
    )




# ---------------------------------------------------------------------
# 4. Evidence lineage: use exact source offsets when available so
#    repeated identical sentences cannot bind to an earlier occurrence.
# ---------------------------------------------------------------------
def _studio_v2371e_batch_evidence(
    *,
    source: str,
    batch: list[dict],
    max_context_chars: int = 1900,
) -> tuple[
    str,
    list[dict],
    dict[int, list[str]],
]:
    source = str(source or "")
    anchors = []
    beat_to_anchor_ids = {}
    span_to_id = {}
    positions = []

    for beat in batch:
        order = int(
            beat.get("order") or 0
        )
        if order <= 0:
            raise RuntimeError(
                "严格 Stage04：Beat 缺少有效 order"
            )

        spans = [
            span
            for span in (
                beat.get(
                    "source_evidence_spans"
                )
                or []
            )
            if isinstance(span, dict)
        ]

        if not spans:
            # Legacy/provisional fallback only.
            spans = []
            cursor = 0
            for text in (
                beat.get("source_evidence")
                or []
            ):
                value = str(
                    text or ""
                ).strip()
                if not value:
                    continue
                pos = source.find(
                    value,
                    cursor,
                )
                if pos < 0:
                    pos = source.find(value)
                if pos < 0:
                    raise RuntimeError(
                        f"严格 Stage04：Beat {order} "
                        "原文证据无法定位"
                    )
                spans.append({
                    "start": pos,
                    "end": pos + len(value),
                    "text": value,
                })
                cursor = pos + len(value)

        beat_ids = []

        for span in spans:
            start = int(
                span.get("start") or 0
            )
            end = int(
                span.get("end") or 0
            )
            text = str(
                span.get("text") or ""
            )

            if (
                start < 0
                or end <= start
                or end > len(source)
                or source[start:end] != text
            ):
                raise RuntimeError(
                    f"严格 Stage04：Beat {order} "
                    "证据 offset 与 Scene 核心正文不一致"
                )

            key = (
                start,
                end,
                text,
            )

            anchor_id = span_to_id.get(
                key
            )

            if not anchor_id:
                anchor_id = (
                    f"E{len(anchors)+1:03d}"
                )
                span_to_id[key] = anchor_id
                anchors.append({
                    "id": anchor_id,
                    "text": text,
                    "beat_order": order,
                    "source_start": start,
                    "source_end": end,
                })

            if anchor_id not in beat_ids:
                beat_ids.append(
                    anchor_id
                )

            positions.append(
                (start, end)
            )

        if not beat_ids:
            raise RuntimeError(
                f"严格 Stage04：Beat {order} "
                "没有可用于 Shot 的核心正文证据"
            )

        beat_to_anchor_ids[
            order
        ] = beat_ids

    if not anchors:
        raise RuntimeError(
            "严格 Stage04：当前 Beat 批次没有核心正文证据"
        )

    lo = min(
        x[0] for x in positions
    )
    hi = max(
        x[1] for x in positions
    )

    if hi - lo <= max_context_chars:
        spare = max_context_chars - (
            hi - lo
        )
        left = max(
            0,
            lo - spare // 2,
        )
        right = min(
            len(source),
            hi
            + (
                spare
                - (lo - left)
            ),
        )
        left = max(
            0,
            right - max_context_chars,
        )
        context = source[
            left:right
        ].strip()
    else:
        context = "\n".join(
            (
                f"[{row['id']}|"
                f"Beat {row['beat_order']}] "
                f"{row['text']}"
            )
            for row in anchors
        )

    return (
        context,
        anchors,
        beat_to_anchor_ids,
    )


# ---------------------------------------------------------------------
# 5. Normalize more Qwen state schemas. "characters"/"props" are common
#    and were not handled in V2.37.1h.
# ---------------------------------------------------------------------
_V2372_R2_PREVIOUS_NORMALIZE_SHOT = (
    _studio_v2371h_normalize_shot
)


def _studio_v2371h_normalize_shot(
    row: dict,
    *,
    compact_beats: list[dict],
) -> dict:
    out = (
        _V2372_R2_PREVIOUS_NORMALIZE_SHOT(
            row,
            compact_beats=compact_beats,
        )
    )

    if not isinstance(out, dict):
        return out

    char_ids = list(
        out.get(
            "character_entity_ids"
        )
        or []
    )
    prop_ids = list(
        out.get("prop_entity_ids")
        or []
    )

    for state_key in (
        "representative_state",
        "video_start_state",
        "video_end_state",
    ):
        state = (
            row.get(state_key)
            if isinstance(row, dict)
            else None
        )
        if not isinstance(state, dict):
            continue

        for key in (
            "characters",
            "character_entities",
            "character_entity_ids",
        ):
            values = state.get(key)
            if not isinstance(values, list):
                continue

            for value in values:
                entity_id = str(
                    value.get("entity_id")
                    if isinstance(value, dict)
                    else value
                ).strip()

                if (
                    entity_id
                    and entity_id
                    not in char_ids
                ):
                    char_ids.append(
                        entity_id
                    )

        for key in (
            "props",
            "prop_entities",
            "prop_entity_ids",
        ):
            values = state.get(key)
            if not isinstance(values, list):
                continue

            for value in values:
                entity_id = str(
                    value.get("entity_id")
                    if isinstance(value, dict)
                    else value
                ).strip()

                if (
                    entity_id
                    and entity_id
                    not in prop_ids
                ):
                    prop_ids.append(
                        entity_id
                    )

    out[
        "character_entity_ids"
    ] = char_ids
    out[
        "prop_entity_ids"
    ] = prop_ids
    return out


# ---------------------------------------------------------------------
# 6. Shot pre-validation recovery is deterministic only:
#    - anchor-id summary -> covered Beat summary
#    - empty top-level entities -> covered Beat entities (Beat-level, never
#      Scene-wide)
# ---------------------------------------------------------------------
_V2372_R2_PREVIOUS_VALIDATE_ROWS = (
    _studio_v2371_validate_rows
)


def _studio_v2372_orders(
    value: object,
) -> list[int]:
    result = []
    for item in value or []:
        try:
            order = int(item)
        except Exception:
            continue
        if (
            order > 0
            and order not in result
        ):
            result.append(order)
    return result


def _studio_v2371_validate_rows(
    *,
    raw_rows: list[dict],
    compact_beats: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    anchors: list[dict],
    scene_id: str,
    episode_id: str,
) -> list[dict]:
    beat_map = {
        int(row.get("order") or 0): row
        for row in (
            compact_beats or []
        )
        if isinstance(row, dict)
        and int(row.get("order") or 0) > 0
    }

    prepared = []

    for original in (
        raw_rows or []
    ):
        if not isinstance(
            original,
            dict,
        ):
            prepared.append(original)
            continue

        row = _studio_v2372_copy.deepcopy(
            original
        )

        orders = (
            _studio_v2372_orders(
                row.get(
                    "covered_beat_orders"
                )
            )
        )

        summary = str(
            row.get("summary") or ""
        ).strip()

        evidence_ids = {
            str(x or "").strip()
            for x in (
                row.get(
                    "source_evidence_ids"
                )
                or []
            )
            if str(x or "").strip()
        }

        bad_summary = bool(
            _studio_v2372_re.fullmatch(
                r"(?:C\d{2})?E\d{3}",
                summary,
                flags=_studio_v2372_re.I,
            )
        ) or (
            summary
            and summary in evidence_ids
        )

        if (
            (not summary or bad_summary)
            and orders
        ):
            beat_summaries = [
                str(
                    beat_map[order].get(
                        "summary"
                    ) or ""
                ).strip()
                for order in orders
                if order in beat_map
                and str(
                    beat_map[order].get(
                        "summary"
                    ) or ""
                ).strip()
            ]
            if beat_summaries:
                row["summary"] = "；".join(
                    beat_summaries
                )[:700]
                row[
                    "summary_origin"
                ] = (
                    "covered-beat-summary"
                )

        if orders:
            if not (
                row.get(
                    "character_entity_ids"
                )
                or []
            ):
                inherited = []
                for order in orders:
                    beat = beat_map.get(
                        order
                    ) or {}
                    for entity_id in (
                        beat.get(
                            "character_entity_ids"
                        )
                        or []
                    ):
                        key = str(
                            entity_id or ""
                        ).strip()
                        if (
                            key
                            and key in allowed_chars
                            and key
                            not in inherited
                        ):
                            inherited.append(
                                key
                            )
                if inherited:
                    row[
                        "character_entity_ids"
                    ] = inherited

            if not (
                row.get(
                    "prop_entity_ids"
                )
                or []
            ):
                inherited = []
                for order in orders:
                    beat = beat_map.get(
                        order
                    ) or {}
                    for entity_id in (
                        beat.get(
                            "prop_entity_ids"
                        )
                        or []
                    ):
                        key = str(
                            entity_id or ""
                        ).strip()
                        if (
                            key
                            and key in allowed_props
                            and key
                            not in inherited
                        ):
                            inherited.append(
                                key
                            )
                if inherited:
                    row[
                        "prop_entity_ids"
                    ] = inherited

        prepared.append(row)

    rows = _V2372_R2_PREVIOUS_VALIDATE_ROWS(
        raw_rows=prepared,
        compact_beats=compact_beats,
        allowed_chars=allowed_chars,
        allowed_props=allowed_props,
        anchors=anchors,
        scene_id=scene_id,
        episode_id=episode_id,
    )

    anchor_ids = {
        str(anchor.get("id") or "")
        for anchor in anchors or []
        if isinstance(anchor, dict)
    }

    for index, row in enumerate(
        rows,
        1,
    ):
        summary = str(
            row.get("summary") or ""
        ).strip()

        if (
            not summary
            or _studio_v2372_re.fullmatch(
                r"(?:C\d{2})?E\d{3}",
                summary,
                flags=_studio_v2372_re.I,
            )
            or summary in anchor_ids
        ):
            raise RuntimeError(
                f"严格 Stage04：Shot {index} "
                "summary 仍是证据锚点/空值，拒绝写入"
            )

    return rows


# ---------------------------------------------------------------------
# 7. Shot audit: exact selected evidence is authoritative. Wider context
#    may explain continuity but can NEVER justify an event absent from the
#    selected evidence / covered Beat.
# ---------------------------------------------------------------------
def _studio_v2377_shot_audit_decision(
    audit: object,
    *,
    required: tuple[str, ...],
) -> tuple[
    bool | None,
    list[str],
    list[str],
]:
    """
    True  = structurally complete semantic pass
    False = structurally complete semantic fail
    None  = incomplete schema; must be re-audited, never treated as semantic fail
    """
    if not isinstance(audit, dict):
        return (
            None,
            [],
            list(required),
        )

    violations = audit.get(
        "violations"
    )

    if not isinstance(
        violations,
        list,
    ):
        violations = []

    missing = [
        key
        for key in required
        if key not in audit
        or not isinstance(
            audit.get(key),
            bool,
        )
    ]

    if missing:
        return (
            None,
            violations,
            missing,
        )

    failed = [
        key
        for key in required
        if audit.get(key)
        is not True
    ]

    if failed:
        if not violations:
            violations = [
                "Shot 审计维度未通过："
                + ", ".join(failed)
            ]
        return (
            False,
            violations,
            [],
        )

    if audit.get("valid") is False:
        if not violations:
            violations = [
                "Shot 审计七个维度全部为 true，"
                "但 valid=false；审计结果自相矛盾"
            ]
        return (
            False,
            violations,
            [],
        )

    if violations:
        return (
            False,
            violations,
            [],
        )

    return (
        True,
        [],
        [],
    )


def _studio_v2377_shot_audit_rows(
    shots: list[dict],
) -> list[dict]:
    return [{
        "index": index + 1,
        "title": row.get("title"),
        "covered_beat_orders":
            row.get(
                "covered_beat_orders"
            ),
        "summary":
            row.get("summary"),
        "action":
            row.get("action"),
        "representative_state":
            row.get(
                "representative_state"
            ),
        "video_start_state":
            row.get(
                "video_start_state"
            ),
        "video_end_state":
            row.get(
                "video_end_state"
            ),
        "image_prompt":
            row.get("image_prompt"),
        "video_start_prompt":
            row.get(
                "video_start_prompt"
            ),
        "video_prompt":
            row.get("video_prompt"),
        "source_evidence_ids":
            row.get(
                "source_evidence_ids"
            ),
        "source_evidence":
            row.get(
                "source_evidence"
            ),
        "character_entity_ids":
            row.get(
                "character_entity_ids"
            ),
        "prop_entity_ids":
            row.get(
                "prop_entity_ids"
            ),
    } for index,row in enumerate(
        shots
    )]


async def _studio_v2377_complete_shot_audit_schema(
    *,
    source_window: str,
    compact_beats: list[dict],
    audit_rows: list[dict],
    prior_audit: object,
    prior_missing: list[str],
) -> dict:
    required = (
        "evidence_entailment_ok",
        "beat_coverage_ok",
        "temporal_monotonic",
        "no_future_event_preconsumption",
        "no_result_duplication",
        "state_order_valid",
        "entity_visibility_valid",
    )

    system_prompt = (
        "你是 strict-shot-v2 七维审计器。"
        "前一次审计返回了不完整 schema；"
        "你必须重新独立审计，不能仅把 prior_audit 缺失字段机械补 true/false。"
        "每个 Shot 自己的 source_evidence 是叙事事实最高权威；"
        "covered Beat 只能概括这些证据；"
        "较宽 context 只能理解连续性，不能替 Shot 补充未选证据中的事件。"
        "必须逐项检查并显式返回七个 boolean："
        "evidence_entailment_ok、beat_coverage_ok、temporal_monotonic、"
        "no_future_event_preconsumption、no_result_duplication、"
        "state_order_valid、entity_visibility_valid。"
        "如果任一项为 false，violations 必须写出具体 Shot/Beat/证据和原因；"
        "如果七项全部 true，violations 必须为空数组。"
        "valid 必须等于七项全部 true 且 violations 为空。"
        "禁止省略字段，禁止只返回 valid/reasons。只输出严格 JSON。"
    )

    prompt = (
        "=== CONTEXT_ONLY_NOT_EVIDENCE ===\n"
        + str(
            source_window or ""
        )
        + "\n\n=== COVERED_BEATS ===\n"
        + _studio_json.dumps(
            compact_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SHOTS_WITH_EXACT_EVIDENCE ===\n"
        + _studio_json.dumps(
            audit_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== PRIOR_AUDIT_ONLY_FOR_SCHEMA_DIAGNOSTIC ===\n"
        + _studio_json.dumps(
            prior_audit
            if isinstance(
                prior_audit,
                dict,
            )
            else {},
            ensure_ascii=False,
        )
        + "\nMISSING_FIELDS="
        + _studio_json.dumps(
            prior_missing,
            ensure_ascii=False,
        )
    )

    diagnostics = []

    for attempt in range(2):
        raw,parsed,_ = (
            await _studio_v2371a_qwen_call(
                phase=(
                    "studio_stage04_"
                    "strict_shot_audit_schema_completion_qwen32b"
                ),
                messages=[{
                    "role":"user",
                    "content":
                        prompt
                        + (
                            ""
                            if attempt == 0
                            else (
                                "\n\nSTRICT_SCHEMA_RETRY："
                                "必须完整返回 valid + 七个 *_ok + violations；"
                                "不得返回 reasons 替代七个 boolean。"
                            )
                        ),
                }],
                system_prompt=
                    system_prompt,
                temperature=0.0,
                max_tokens=1200,
                contract=(
                    '{"valid":true,'
                    '"evidence_entailment_ok":true,'
                    '"beat_coverage_ok":true,'
                    '"temporal_monotonic":true,'
                    '"no_future_event_preconsumption":true,'
                    '"no_result_duplication":true,'
                    '"state_order_valid":true,'
                    '"entity_visibility_valid":true,'
                    '"violations":[]}'
                ),
            )
        )

        audit = (
            parsed
            if isinstance(
                parsed,
                dict,
            )
            else _studio_v2372_extract_object(
                raw,
                parsed,
            )
        )

        (
            decision,
            violations,
            missing,
        ) = (
            _studio_v2377_shot_audit_decision(
                audit,
                required=required,
            )
        )

        if decision is True:
            result = dict(audit)
            result["valid"] = True
            result[
                "violations"
            ] = []
            result[
                "audit_schema_origin"
            ] = (
                "shot-schema-completion"
            )
            return result

        if decision is False:
            result = (
                dict(audit)
                if isinstance(
                    audit,
                    dict,
                )
                else {}
            )
            result["valid"] = False
            result[
                "violations"
            ] = violations
            result[
                "audit_schema_origin"
            ] = (
                "shot-schema-completion"
            )
            return result

        diagnostics.append(
            "attempt="
            + str(attempt + 1)
            + " missing="
            + repr(missing)
            + " keys="
            + repr(
                sorted(
                    audit.keys()
                    if isinstance(
                        audit,
                        dict,
                    )
                    else []
                )
            )
        )

    return {
        "valid": False,
        "violations": [
            "Shot 七维审计连续返回不完整 schema；"
            + " | ".join(
                diagnostics
            )
        ],
        "audit_schema_origin":
            "shot-schema-completion-failed",
    }


async def _studio_v2371_audit_batch(
    *,
    source_window: str,
    compact_beats: list[dict],
    shots: list[dict],
) -> dict:
    required = (
        "evidence_entailment_ok",
        "beat_coverage_ok",
        "temporal_monotonic",
        "no_future_event_preconsumption",
        "no_result_duplication",
        "state_order_valid",
        "entity_visibility_valid",
    )

    audit_rows = (
        _studio_v2377_shot_audit_rows(
            shots
        )
    )

    system_prompt = (
        "你是 strict-shot-v2 制作合同审计器，只审计不改写。"
        "每个 Shot 自己的 source_evidence 是叙事事实最高权威；"
        "covered Beat 只能概括这些证据。较宽 source context "
        "只能帮助理解前后关系，绝不能用来替 Shot 补一个其已选择证据中"
        "不存在的事件。"
        "必须检查："
        "1. evidence_entailment：summary/action/三状态/三个 Prompt "
        "均被该 Shot 自己的 source_evidence 和 covered Beat 直接支持；"
        "2. Beat 显式覆盖；"
        "3. 时间单调，不提前消费后续事件；"
        "4. 不重复播放已经完成的结果；"
        "5. video_start→representative→video_end 因果顺序成立；"
        "6. representative 是当前 Beat 的信息帧；"
        "7. 角色/道具 ID 只表示该 Shot 真实涉及的实体。"
        "必须显式返回七个 *_ok boolean 和 violations；"
        "任意一项不满足必须 valid=false，并写出具体 violations。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== CONTEXT_ONLY_NOT_EVIDENCE ===\n"
        + str(
            source_window or ""
        )
        + "\n\n=== COVERED_BEATS ===\n"
        + _studio_json.dumps(
            compact_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SHOTS_WITH_EXACT_EVIDENCE ===\n"
        + _studio_json.dumps(
            audit_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    raw,parsed,_ = (
        await _studio_v2371a_qwen_call(
            phase=(
                "studio_stage04_"
                "strict_evidence_temporal_audit_qwen32b"
            ),
            messages=[{
                "role":"user",
                "content":prompt,
            }],
            system_prompt=
                system_prompt,
            temperature=0.0,
            max_tokens=1100,
            contract=(
                '{"valid":true,'
                '"evidence_entailment_ok":true,'
                '"beat_coverage_ok":true,'
                '"temporal_monotonic":true,'
                '"no_future_event_preconsumption":true,'
                '"no_result_duplication":true,'
                '"state_order_valid":true,'
                '"entity_visibility_valid":true,'
                '"violations":[]}'
            ),
        )
    )

    audit = (
        parsed
        if isinstance(
            parsed,
            dict,
        )
        else _studio_v2372_extract_object(
            raw,
            parsed,
        )
    )

    (
        decision,
        violations,
        missing,
    ) = (
        _studio_v2377_shot_audit_decision(
            audit,
            required=required,
        )
    )

    if decision is True:
        result = dict(audit)
        result["valid"] = True
        result[
            "violations"
        ] = []
        result[
            "audit_schema_origin"
        ] = (
            "shot-primary-complete"
        )
        return result

    if decision is False:
        result = (
            dict(audit)
            if isinstance(
                audit,
                dict,
            )
            else {}
        )
        result["valid"] = False
        result[
            "violations"
        ] = violations
        result[
            "audit_schema_origin"
        ] = (
            "shot-primary-complete"
        )
        return result

    # Missing fields are a serialization/schema problem, not evidence that
    # all semantic dimensions failed. Re-audit with a strict complete schema.
    return (
        await _studio_v2377_complete_shot_audit_schema(
            source_window=
                source_window,
            compact_beats=
                compact_beats,
            audit_rows=
                audit_rows,
            prior_audit=
                audit,
            prior_missing=
                missing,
        )
    )



# ---------------------------------------------------------------------
# 8. Strict Shot prompt is refined generically; no story keywords.
# ---------------------------------------------------------------------
_V2372_R2_PREVIOUS_GENERATE_BATCH = (
    _studio_v2371_generate_batch
)


async def _studio_v2371_generate_batch(
    *,
    system_prompt: str,
    prompt: str,
    scene_index: int,
    scene_total: int,
    batch_index: int,
    batch_total: int,
) -> list[dict]:
    hardened_system = (
        str(system_prompt or "")
        + "\n新增硬约束："
        "每个 Shot 的 summary/action/representative_state/"
        "video_start_state/video_end_state/image_prompt/"
        "video_start_prompt/video_prompt 必须只描述该 Shot "
        "实际选择的 source_evidence_ids 与 covered Beat "
        "能够直接支持的事实。"
        "不得从同一 source context 的其他未选择句子借用事件。"
        "不得把未被 covered Beat 与所选 source_evidence 直接支持的信息扩写成新事件。"
        "character_entity_ids / prop_entity_ids 必须使用"
        " ALLOWED_ENTITIES 中的真实 ID；禁止编造近似 ID。"
    )

    return await _V2372_R2_PREVIOUS_GENERATE_BATCH(
        system_prompt=hardened_system,
        prompt=prompt,
        scene_index=scene_index,
        scene_total=scene_total,
        batch_index=batch_index,
        batch_total=batch_total,
    )


# ===== /V2.37.2 STAGE04 NARRATIVE BACKBONE =====


# ===== V2.37.2A STAGE04 NON-DESTRUCTIVE SCENE OVERLAP PARTITION =====
import copy as _studio_v2372a_copy


_V2372A_PREVIOUS_SCOPE = _studio_stage04_scope


def _studio_v2372a_raw_range(scene: dict) -> tuple[int, int] | None:
    try:
        start = int(scene.get("source_start") or 0)
        end = int(scene.get("source_end") or 0)
    except Exception:
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def _studio_v2372a_effective_range(scene: dict) -> tuple[int, int] | None:
    raw = _studio_v2372a_raw_range(scene)
    if raw is None:
        return None
    start = int(
        scene.get("_stage04_effective_source_start")
        if scene.get("_stage04_effective_source_start") is not None
        else raw[0]
    )
    end = int(
        scene.get("_stage04_effective_source_end")
        if scene.get("_stage04_effective_source_end") is not None
        else raw[1]
    )
    if start < 0 or end <= start:
        return None
    return start, end


def _studio_v2372a_partition_scene_ranges(
    scenes: list[dict],
) -> list[dict]:
    """
    Build non-overlapping effective Scene source ranges for this rebuild only.

    Raw source_start/source_end are preserved. Ordinary partial overlap between
    adjacent Scenes is partitioned at the midpoint of the overlap so every
    source character is consumed by at most one Scene.

    Pathological mappings (backward order, equal starts, containment/nesting)
    remain hard failures because there is no unambiguous local partition.
    """
    result = [
        _studio_v2372a_copy.deepcopy(scene)
        for scene in (scenes or [])
    ]

    valid = []
    for index, scene in enumerate(result):
        raw = _studio_v2372a_raw_range(scene)
        if raw is None:
            continue

        start, end = raw
        scene["_stage04_effective_source_start"] = start
        scene["_stage04_effective_source_end"] = end
        scene["_stage04_source_range_policy"] = (
            "raw-no-overlap-adjustment"
        )
        valid.append((index, start, end))

    for pos in range(len(valid) - 1):
        left_index, left_start, left_end = valid[pos]
        right_index, right_start, right_end = valid[pos + 1]

        left_scene = result[left_index]
        right_scene = result[right_index]

        # Scene order must agree with source order.
        if right_start < left_start:
            raise RuntimeError(
                "严格 Stage04：Scene 剧情顺序与小说正文起点倒序；"
                f" left={left_scene.get('scene_id')}"
                f"[{left_start},{left_end})"
                f" right={right_scene.get('scene_id')}"
                f"[{right_start},{right_end})"
            )

        if right_start == left_start:
            raise RuntimeError(
                "严格 Stage04：相邻 Scene 使用相同小说正文起点，"
                "无法无损确定正文归属；"
                f" left={left_scene.get('scene_id')}"
                f"[{left_start},{left_end})"
                f" right={right_scene.get('scene_id')}"
                f"[{right_start},{right_end})"
            )

        if left_end <= right_start:
            continue

        # Containment/nesting is not an ordinary boundary overlap.
        if left_end >= right_end:
            raise RuntimeError(
                "严格 Stage04：Scene 小说正文范围发生包含/嵌套，"
                "无法用相邻边界分区安全修复；"
                f" left={left_scene.get('scene_id')}"
                f"[{left_start},{left_end})"
                f" right={right_scene.get('scene_id')}"
                f"[{right_start},{right_end})"
            )

        overlap_start = right_start
        overlap_end = left_end
        overlap_chars = overlap_end - overlap_start

        split = overlap_start + overlap_chars // 2

        # Guarantee both effective ranges remain non-empty.
        if split <= left_start:
            split = left_start + 1
        if split >= right_end:
            split = right_end - 1

        current_left_end = int(
            left_scene.get("_stage04_effective_source_end")
            or left_end
        )
        current_right_start = int(
            right_scene.get("_stage04_effective_source_start")
            if right_scene.get("_stage04_effective_source_start") is not None
            else right_start
        )

        left_scene["_stage04_effective_source_end"] = min(
            current_left_end,
            split,
        )
        right_scene["_stage04_effective_source_start"] = max(
            current_right_start,
            split,
        )

        partition_meta = {
            "policy": "adjacent-overlap-midpoint-partition-v1",
            "raw_overlap_start": overlap_start,
            "raw_overlap_end": overlap_end,
            "raw_overlap_chars": overlap_chars,
            "split": split,
        }

        left_scene["_stage04_overlap_partition_next"] = dict(
            partition_meta
        )
        right_scene["_stage04_overlap_partition_prev"] = dict(
            partition_meta
        )
        left_scene["_stage04_source_range_policy"] = (
            "effective-partitioned"
        )
        right_scene["_stage04_source_range_policy"] = (
            "effective-partitioned"
        )

    # Final effective ranges must be ordered and non-overlapping.
    previous_end = None
    previous_scene = None

    for scene in result:
        effective = _studio_v2372a_effective_range(scene)
        if effective is None:
            continue

        start, end = effective

        if end <= start:
            raise RuntimeError(
                "严格 Stage04：Scene 有效正文范围为空；"
                f" scene={scene.get('scene_id')}"
                f"[{start},{end})"
            )

        if previous_end is not None and start < previous_end:
            raise RuntimeError(
                "严格 Stage04：Scene 有效正文分区后仍有重叠；"
                f" previous={previous_scene}"
                f" end={previous_end}"
                f" current={scene.get('scene_id')}"
                f" start={start}"
            )

        previous_end = end
        previous_scene = str(
            scene.get("scene_id") or ""
        )

    return result


def _studio_stage04_scope(
    state: dict,
) -> tuple[list[dict], str]:
    scenes, active_episode = (
        _V2372A_PREVIOUS_SCOPE(state)
    )
    return (
        _studio_v2372a_partition_scene_ranges(
            scenes
        ),
        active_episode,
    )


def _studio_stage04_scene_source(
    scene: dict,
    source_text: str,
) -> str:
    effective = _studio_v2372a_effective_range(
        scene
    )
    source_text = str(source_text or "")

    if effective is not None and source_text:
        start, end = effective

        if start >= len(source_text):
            raise RuntimeError(
                "严格 Stage04：Scene 有效正文起点超过小说正文长度；"
                f" scene={scene.get('scene_id')}"
                f" start={start}"
                f" source_len={len(source_text)}"
            )

        end = min(
            end,
            len(source_text),
        )
        if end <= start:
            raise RuntimeError(
                "严格 Stage04：Scene 有效正文范围裁剪后为空；"
                f" scene={scene.get('scene_id')}"
                f"[{start},{end})"
            )

        return source_text[start:end]

    # No valid source offsets: preserve source_excerpt fallback.
    return str(
        scene.get("source_excerpt") or ""
    ).strip()


def _studio_v2372_scene_range_guard(
    *,
    scene: dict,
    state: dict,
) -> None:
    """
    V2.37.2A: ordinary adjacent raw overlap is already resolved by
    _studio_stage04_scope. Validate the effective range instead of rejecting
    the historical raw overlap.
    """
    raw = _studio_v2372a_raw_range(scene)
    effective = _studio_v2372a_effective_range(
        scene
    )

    if raw is None:
        return

    if effective is None:
        raise RuntimeError(
            "严格 Stage04：Scene 有原始正文范围但没有有效分区范围；"
            f" scene={scene.get('scene_id')}"
        )

    start, end = effective
    if end <= start:
        raise RuntimeError(
            "严格 Stage04：Scene 有效正文范围无效；"
            f" scene={scene.get('scene_id')}"
            f"[{start},{end})"
        )
# ===== /V2.37.2A STAGE04 NON-DESTRUCTIVE SCENE OVERLAP PARTITION =====


# ===== V2.37.2B STAGE04 AUDIT SCHEMA COMPLETION =====

def _studio_v2372b_audit_violations(
    audit: object,
    *,
    required: tuple[str, ...],
) -> tuple[bool | None, list[str], list[str]]:
    """
    Returns:
      decision:
        True  = complete strict pass
        False = complete strict fail
        None  = structurally incomplete; requires schema-completion audit
      violations
      missing_fields

    Aggregate valid/audit_passed alone is intentionally NOT accepted.
    """
    if not isinstance(audit, dict):
        return (
            None,
            ["audit 不是 JSON object"],
            list(required),
        )

    raw_violations = audit.get("violations")
    violations = (
        [
            str(item or "").strip()
            for item in raw_violations
            if str(item or "").strip()
        ]
        if isinstance(raw_violations, list)
        else []
    )

    missing = [
        key
        for key in required
        if not isinstance(
            audit.get(key),
            bool,
        )
    ]

    if missing:
        return None, violations, missing

    failed = [
        key
        for key in required
        if audit.get(key) is not True
    ]

    explicit_valid = audit.get("valid")

    if failed:
        if not violations:
            violations = [
                "审计维度未通过："
                + ", ".join(failed)
            ]
        return False, violations, []

    if explicit_valid is False:
        if not violations:
            violations = [
                "所有维度为 true，"
                "但 audit.valid=false；"
                "审计结果自相矛盾"
            ]
        return False, violations, []

    if violations:
        return False, violations, []

    return True, [], []


def _studio_v23962_audit_beats(
    beats: list[dict],
) -> list[dict]:
    """Build an offset-visible audit payload without splitting its closure."""
    rows = []
    for beat in beats or []:
        if not isinstance(beat, dict):
            continue
        rows.append({
            "summary": beat.get("summary"),
            "state_change": beat.get("state_change"),
            "source_evidence_ids": list(
                beat.get("source_evidence_ids") or []
            ),
            "source_evidence": list(
                beat.get("source_evidence") or []
            ),
            "source_evidence_spans": [
                dict(span)
                for span in (
                    beat.get("source_evidence_spans") or []
                )
                if isinstance(span, dict)
            ],
        })
    rows.sort(
        key=lambda row: (
            min(
                [
                    int(span.get("start") or 0)
                    for span in row["source_evidence_spans"]
                ]
                or [10**18]
            ),
            str(row.get("summary") or ""),
        )
    )
    for index, row in enumerate(rows, 1):
        row["index"] = index
    return rows


async def _studio_v2372b_complete_audit_schema(
    *,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
    prior_audit: object,
    prior_missing: list[str],
) -> dict:
    required = (
        "event_coverage_ok",
        "granularity_ok",
        "evidence_entailment_ok",
        "temporal_order_ok",
        "support_classification_ok",
    )

    audit_beats = _studio_v23962_audit_beats(beats)

    system_prompt = (
        "你是 Narrative Beat 审计结果结构补全器。"
        "你仍然必须独立审计正文和 Beat，不能沿用 prior_audit 的结论。"
        "分类只能基于当前 Scene 的最小有序叙事状态图和证据依赖，"
        "不得使用固定关键词、文本类别、题材类型或预设业务词表。"
        "必须逐项输出以下五个 boolean："
        "event_coverage_ok、granularity_ok、evidence_entailment_ok、"
        "temporal_order_ok、support_classification_ok。"
        "如果任何一项为 false，violations 必须至少写出一条具体原因；"
        "如果全部为 true，violations 必须为空数组。"
        "valid 必须等于上述五项全部为 true 且 violations 为空。"
        "禁止省略字段，禁止只返回 valid。只输出严格 JSON。"
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== SOURCE_ANCHORS ===\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== PROPOSED_BEATS ===\n"
        + _studio_json.dumps(
            audit_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SUPPORT_EVIDENCE_IDS ===\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
        + "\n\n=== PRIOR_AUDIT_ONLY_FOR_SCHEMA_DIAGNOSTIC ===\n"
        + _studio_json.dumps(
            prior_audit
            if isinstance(prior_audit, dict)
            else {},
            ensure_ascii=False,
        )
        + "\nMISSING_FIELDS="
        + _studio_json.dumps(
            prior_missing,
            ensure_ascii=False,
        )
    )

    diagnostics = []

    for attempt in range(2):
        raw, parsed, _ = (
            await _studio_v2371a_qwen_call(
                phase=(
                    "studio_stage04_"
                    "narrative_beat_audit_schema_completion_qwen32b"
                ),
                messages=[{
                    "role": "user",
                    "content": prompt + (
                        ""
                        if attempt == 0
                        else (
                            "\n\nSTRICT_SCHEMA_RETRY："
                            "六个顶层字段 valid + 五个 *_ok "
                            "以及 violations 必须全部显式返回；"
                            "不得输出 reasons 代替这些字段。"
                        )
                    ),
                }],
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=850,
                contract=(
                    '{"valid":true,'
                    '"event_coverage_ok":true,'
                    '"granularity_ok":true,'
                    '"evidence_entailment_ok":true,'
                    '"temporal_order_ok":true,'
                    '"support_classification_ok":true,'
                    '"violations":[]}'
                ),
            )
        )

        audit = (
            parsed
            if isinstance(parsed, dict)
            else _studio_v2372_extract_object(
                raw,
                parsed,
            )
        )

        decision, violations, missing = (
            _studio_v2372b_audit_violations(
                audit,
                required=required,
            )
        )

        if decision is True:
            result = dict(audit)
            result["valid"] = True
            result["violations"] = []
            result[
                "audit_schema_origin"
            ] = "schema-completion"
            return result

        if decision is False:
            result = (
                dict(audit)
                if isinstance(audit, dict)
                else {}
            )
            result["valid"] = False
            result["violations"] = violations
            result[
                "audit_schema_origin"
            ] = "schema-completion"
            return result

        diagnostics.append(
            "attempt="
            + str(attempt + 1)
            + " missing="
            + repr(missing)
            + " keys="
            + repr(
                sorted(
                    audit.keys()
                    if isinstance(audit, dict)
                    else []
                )
            )
        )

    return {
        "valid": False,
        "event_coverage_ok": False,
        "granularity_ok": False,
        "evidence_entailment_ok": False,
        "temporal_order_ok": False,
        "support_classification_ok": False,
        "violations": [
            "Narrative Beat 审计连续返回不完整 schema；"
            + " | ".join(diagnostics)
        ],
        "audit_schema_origin":
            "schema-completion-failed",
    }


async def _studio_v2372_audit_extraction(
    *,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
) -> dict:
    required = (
        "event_coverage_ok",
        "granularity_ok",
        "evidence_entailment_ok",
        "temporal_order_ok",
        "support_classification_ok",
    )

    audit_beats = _studio_v23962_audit_beats(beats)

    system_prompt = (
        "你是小说 Narrative Beat 质量审计器，只审计不改写。"
        "正文锚点必须全部被分类，但分类不能依赖固定关键词、"
        "文本类别或预设题材规则。"
        "对每个候选单元，判断它对当前 Scene 的最小有序叙事状态图"
        "是否构成必要状态、因果关系或上下文依赖。"
        "同时检查每个 Beat 的 summary/state_change 是否被自己的"
        "source_evidence 直接支持、是否遗漏必要叙事状态、"
        "顺序是否与正文一致。"
        "必须显式返回五个 *_ok boolean 和 violations。"
        "只输出严格 JSON。"
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== SOURCE_ANCHORS ===\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== PROPOSED_BEATS ===\n"
        + _studio_json.dumps(
            audit_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SUPPORT_EVIDENCE_IDS ===\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
    )

    raw, parsed, _ = (
        await _studio_v2371a_qwen_call(
            phase=(
                "studio_stage04_"
                "narrative_beat_audit_qwen32b"
            ),
            messages=[{
                "role": "user",
                "content": prompt,
            }],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=900,
            contract=(
                '{"valid":true,'
                '"event_coverage_ok":true,'
                '"granularity_ok":true,'
                '"evidence_entailment_ok":true,'
                '"temporal_order_ok":true,'
                '"support_classification_ok":true,'
                '"violations":[]}'
            ),
        )
    )

    audit = (
        parsed
        if isinstance(parsed, dict)
        else _studio_v2372_extract_object(
            raw,
            parsed,
        )
    )

    decision, violations, missing = (
        _studio_v2372b_audit_violations(
            audit,
            required=required,
        )
    )

    if decision is True:
        result = dict(audit)
        result["valid"] = True
        result["violations"] = []
        result[
            "audit_schema_origin"
        ] = "primary-complete"
        return result

    if decision is False:
        result = (
            dict(audit)
            if isinstance(audit, dict)
            else {}
        )
        result["valid"] = False
        result["violations"] = violations
        result[
            "audit_schema_origin"
        ] = "primary-complete"
        return result

    # Structurally incomplete audit is NOT treated as semantic failure
    # and is NOT accepted as pass. Perform one dedicated schema-completion
    # verification instead.
    return await _studio_v2372b_complete_audit_schema(
        chunk=chunk,
        anchors=anchors,
        beats=beats,
        support_ids=support_ids,
        prior_audit=audit,
        prior_missing=missing,
    )


_V2372B_PREVIOUS_GENERATE_CHUNK_BEATS = (
    _studio_v2372_generate_chunk_beats
)


async def _studio_v2372_generate_chunk_beats(
    *,
    chunk: dict,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[list[dict], list[str]]:
    try:
        return await (
            _V2372B_PREVIOUS_GENERATE_CHUNK_BEATS(
                chunk=chunk,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                entity_rows=entity_rows,
            )
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "Narrative Backbone chunk "
            + str(chunk.get("index") or "?")
            + " source_range=["
            + str(chunk.get("start") or 0)
            + ","
            + str(chunk.get("end") or 0)
            + ")："
            + str(exc)
        ) from exc

# ===== /V2.37.2B STAGE04 AUDIT SCHEMA COMPLETION =====


# ===== V2.37.2C STAGE04 ANCHOR COVERAGE COMPLETION =====
import copy as _studio_v2372c_copy
import re as _studio_v2372c_re


_V2372C_VALIDATE_EXTRACTION = (
    _studio_v2372_validate_extraction
)


def _studio_v2372c_anchor_map(
    anchors: list[dict],
) -> dict[str, dict]:
    return {
        str(row.get("id") or ""): row
        for row in (anchors or [])
        if isinstance(row, dict)
        and str(row.get("id") or "")
    }


def _studio_v2372c_payload_accounting(
    *,
    payload: dict,
    anchors: list[dict],
) -> tuple[
    set[str],
    set[str],
    list[str],
]:
    amap = _studio_v2372c_anchor_map(
        anchors
    )
    expected = set(amap)

    used = set()

    raw_beats = (
        payload.get("beats")
        if isinstance(payload, dict)
        else []
    )
    if not isinstance(raw_beats, list):
        raw_beats = []

    for row in raw_beats:
        if not isinstance(row, dict):
            continue

        for value in (
            row.get("source_evidence_ids")
            or []
        ):
            key = str(value or "").strip()
            if key in expected:
                used.add(key)

    normalize_support = globals().get(
        "_studio_v2372_normalize_support"
    )

    support_values = (
        payload.get("support_evidence_ids")
        or payload.get("support")
        or []
        if isinstance(payload, dict)
        else []
    )

    support_ids = (
        normalize_support(support_values)
        if normalize_support is not None
        else [
            str(value or "").strip()
            for value in support_values
        ]
    )

    support = {
        key
        for key in support_ids
        if key in expected
    }

    overlap = used.intersection(
        support
    )

    if overlap:
        raise RuntimeError(
            "同一正文证据同时被模型分到 Beat 和 support："
            + repr(sorted(overlap))
        )

    missing = sorted(
        expected - used - support,
        key=lambda key: (
            int(
                amap[key].get("start") or 0
            ),
            key,
        ),
    )

    return used, support, missing


def _studio_v2372c_validate_partial(
    *,
    payload: dict,
    anchors: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
    list[str],
]:
    """
    Validate every classification the model DID return without pretending
    missing anchors are semantically support.

    Missing anchors are temporarily added to the validator's support list
    only to let the existing deterministic validator validate the returned
    Beats. They are removed immediately and remain UNCLASSIFIED until the
    model-driven completion pass assigns them.
    """
    _, support, missing = (
        _studio_v2372c_payload_accounting(
            payload=payload,
            anchors=anchors,
        )
    )

    augmented = (
        _studio_v2372c_copy.deepcopy(
            payload
        )
        if isinstance(payload, dict)
        else {}
    )

    augmented[
        "support_evidence_ids"
    ] = [
        *sorted(support),
        *missing,
    ]

    beats, validator_support = (
        _V2372C_VALIDATE_EXTRACTION(
            payload=augmented,
            anchors=anchors,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
            entity_rows=entity_rows,
        )
    )

    missing_set = set(missing)

    real_support = [
        key
        for key in validator_support
        if key not in missing_set
    ]

    return (
        beats,
        real_support,
        missing,
    )


def _studio_v2372c_clean_entity_ids(
    values: object,
    *,
    allowed: set[str],
) -> list[str]:
    cleaner = globals().get(
        "_studio_v2372_clean_entity_ids"
    )

    if cleaner is not None:
        return cleaner(
            values,
            allowed=allowed,
        )

    result = []

    for value in values or []:
        key = str(
            value.get("entity_id")
            if isinstance(value, dict)
            else value
        ).strip()

        if (
            key
            and key in allowed
            and key not in result
        ):
            result.append(key)

    return result


def _studio_v2372c_anchor_span(
    anchor: dict,
) -> dict:
    return {
        "id": str(
            anchor.get("id") or ""
        ),
        "start": int(
            anchor.get("start") or 0
        ),
        "end": int(
            anchor.get("end") or 0
        ),
        "text": str(
            anchor.get("text") or ""
        ),
    }


def _studio_v2372c_sort_beats(
    beats: list[dict],
) -> list[dict]:
    def first_position(row):
        spans = [
            span
            for span in (
                row.get(
                    "source_evidence_spans"
                )
                or []
            )
            if isinstance(span, dict)
        ]

        starts = [
            int(span.get("start") or 0)
            for span in spans
        ]

        return (
            min(starts) if starts else 10**18,
            str(row.get("summary") or ""),
        )

    return sorted(
        beats,
        key=first_position,
    )


def _studio_v2372c_merge_assignments(
    *,
    beats: list[dict],
    support_ids: list[str],
    assignments: list[dict],
    requested_ids: list[str],
    anchors: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    amap = _studio_v2372c_anchor_map(
        anchors
    )

    requested = set(requested_ids)

    rows_by_id = {}
    duplicate_assignment = set()

    for assignment in (
        assignments or []
    ):
        if not isinstance(
            assignment,
            dict,
        ):
            continue

        evidence_id = str(
            assignment.get(
                "source_evidence_id"
            )
            or ""
        ).strip()

        if not evidence_id:
            continue

        if evidence_id in rows_by_id:
            duplicate_assignment.add(
                evidence_id
            )

        rows_by_id[
            evidence_id
        ] = assignment

    if duplicate_assignment:
        raise RuntimeError(
            "覆盖补全重复返回正文锚点："
            + repr(
                sorted(
                    duplicate_assignment
                )
            )
        )

    returned = set(
        rows_by_id
    )

    missing = requested - returned
    unexpected = returned - requested

    if missing or unexpected:
        raise RuntimeError(
            "覆盖补全没有逐项返回当前缺失锚点；"
            f"missing={sorted(missing)} "
            f"unexpected={sorted(unexpected)}"
        )

    merged_beats = (
        _studio_v2372c_copy.deepcopy(
            beats
        )
    )

    merged_support = list(
        support_ids or []
    )

    exact_name_bindings = globals().get(
        "_studio_v2372_exact_name_bindings"
    )

    for evidence_id in requested_ids:
        assignment = rows_by_id[
            evidence_id
        ]

        anchor = amap.get(
            evidence_id
        )

        if not anchor:
            raise RuntimeError(
                "覆盖补全引用未知正文锚点："
                + evidence_id
            )

        destination = str(
            assignment.get("destination")
            or ""
        ).strip().lower()

        if destination == "support":
            if evidence_id not in merged_support:
                merged_support.append(
                    evidence_id
                )
            continue

        if destination not in {
            "existing_beat",
            "new_beat",
        }:
            raise RuntimeError(
                "覆盖补全 destination 非法："
                + repr(destination)
            )

        evidence_text = str(
            anchor.get("text") or ""
        )
        span = (
            _studio_v2372c_anchor_span(
                anchor
            )
        )

        if destination == "existing_beat":
            try:
                beat_index = int(
                    assignment.get(
                        "target_beat_index"
                    )
                    or 0
                )
            except Exception:
                beat_index = 0

            if not (
                1
                <= beat_index
                <= len(merged_beats)
            ):
                raise RuntimeError(
                    "覆盖补全 existing_beat "
                    "target_beat_index 越界："
                    + repr(beat_index)
                )

            beat = merged_beats[
                beat_index - 1
            ]

            ids = list(
                beat.get(
                    "source_evidence_ids"
                )
                or []
            )

            if evidence_id not in ids:
                ids.append(evidence_id)

            beat[
                "source_evidence_ids"
            ] = ids

            evidence = list(
                beat.get(
                    "source_evidence"
                )
                or []
            )

            if evidence_text not in evidence:
                evidence.append(
                    evidence_text
                )

            beat[
                "source_evidence"
            ] = evidence

            spans = list(
                beat.get(
                    "source_evidence_spans"
                )
                or []
            )

            if not any(
                str(
                    current.get("id")
                    or ""
                )
                == evidence_id
                for current in spans
                if isinstance(
                    current,
                    dict,
                )
            ):
                spans.append(span)

            beat[
                "source_evidence_spans"
            ] = spans

            combined = (
                str(
                    beat.get("summary")
                    or ""
                )
                + "\n"
                + evidence_text
            )

            if exact_name_bindings is not None:
                for key in (
                    exact_name_bindings(
                        text=combined,
                        entity_rows=entity_rows,
                        entity_type="character",
                        allowed=allowed_chars,
                    )
                ):
                    current = list(
                        beat.get(
                            "character_entity_ids"
                        )
                        or []
                    )
                    if key not in current:
                        current.append(key)
                    beat[
                        "character_entity_ids"
                    ] = current

                for key in (
                    exact_name_bindings(
                        text=combined,
                        entity_rows=entity_rows,
                        entity_type="prop",
                        allowed=allowed_props,
                    )
                ):
                    current = list(
                        beat.get(
                            "prop_entity_ids"
                        )
                        or []
                    )
                    if key not in current:
                        current.append(key)
                    beat[
                        "prop_entity_ids"
                    ] = current

            continue

        summary = str(
            assignment.get("summary")
            or ""
        ).strip()

        state_change = str(
            assignment.get(
                "state_change"
            )
            or ""
        ).strip()

        if not summary:
            raise RuntimeError(
                "覆盖补全 new_beat 缺少 summary；"
                f"evidence_id={evidence_id}"
            )

        if _studio_v2372c_re.fullmatch(
            r"(?:C\d{2})?E\d{3}",
            summary,
            flags=_studio_v2372c_re.I,
        ):
            raise RuntimeError(
                "覆盖补全 new_beat summary "
                "错误回显正文锚点 ID；"
                f"evidence_id={evidence_id}"
            )

        char_ids = (
            _studio_v2372c_clean_entity_ids(
                assignment.get(
                    "character_entity_ids"
                ),
                allowed=allowed_chars,
            )
        )

        prop_ids = (
            _studio_v2372c_clean_entity_ids(
                assignment.get(
                    "prop_entity_ids"
                ),
                allowed=allowed_props,
            )
        )

        combined = (
            summary
            + "\n"
            + state_change
            + "\n"
            + evidence_text
        )

        if exact_name_bindings is not None:
            for key in (
                exact_name_bindings(
                    text=combined,
                    entity_rows=entity_rows,
                    entity_type="character",
                    allowed=allowed_chars,
                )
            ):
                if key not in char_ids:
                    char_ids.append(key)

            for key in (
                exact_name_bindings(
                    text=combined,
                    entity_rows=entity_rows,
                    entity_type="prop",
                    allowed=allowed_props,
                )
            ):
                if key not in prop_ids:
                    prop_ids.append(key)

        merged_beats.append({
            "summary": summary[:700],
            "state_change":
                state_change[:500],
            "source_evidence_ids": [
                evidence_id
            ],
            "source_evidence": [
                evidence_text
            ],
            "source_evidence_spans": [
                span
            ],
            "character_entity_ids":
                char_ids,
            "prop_entity_ids":
                prop_ids,
            "coverage_completion_origin":
                "model-new-beat",
        })

    anchor_order = {
        key: (
            int(row.get("start") or 0),
            key,
        )
        for key,row in amap.items()
    }

    merged_support = sorted(
        set(merged_support),
        key=lambda key: anchor_order.get(
            key,
            (10**18, key),
        ),
    )

    merged_beats = (
        _studio_v2372c_sort_beats(
            merged_beats
        )
    )

    return (
        merged_beats,
        merged_support,
    )


async def _studio_v2372c_complete_group(
    *,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
    requested_ids: list[str],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    amap = _studio_v2372c_anchor_map(
        anchors
    )

    requested_anchors = [
        amap[key]
        for key in requested_ids
        if key in amap
    ]

    compact_beats = [{
        "beat_index": index + 1,
        "summary": row.get("summary"),
        "state_change":
            row.get("state_change"),
        "source_evidence_ids":
            row.get("source_evidence_ids"),
        "source_evidence":
            row.get("source_evidence"),
    } for index,row in enumerate(beats)]

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    system_prompt = (
        "你是 Narrative Backbone 正文覆盖补全器，运行 Qwen3-32B。"
        "上一次提取已经产生一组有效 Beat/support，但遗漏了少量正文锚点。"
        "你只处理 MISSING_SOURCE_ANCHORS，不重写已经完成的分类。"
        "每个缺失锚点必须且只能选择一个结构目的地："
        "existing_beat、new_beat 或 support。"
        "选择依据只来自当前 Scene 的最小有序叙事状态图："
        "如果该证据属于已有 Beat 的同一必要状态/因果单元，选择 existing_beat；"
        "如果它形成独立且必要的叙事状态单元，选择 new_beat；"
        "如果移除后不改变可重建的状态、因果关系或必要上下文依赖，选择 support。"
        "不得依据固定关键词、文本类别、题材类型或预设业务词表分类。"
        "new_beat 必须填写 summary/state_change；"
        "existing_beat 必须填写 CURRENT_BEATS 中有效的 target_beat_index；"
        "support 的 target_beat_index=0。"
        "所有 requested source_evidence_id 必须逐项返回一次，不能遗漏、不能新增。"
        "只输出严格 JSON。"
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== CURRENT_BEATS ===\n"
        + _studio_json.dumps(
            compact_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== CURRENT_SUPPORT_IDS ===\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
        + "\n\n=== MISSING_SOURCE_ANCHORS ===\n"
        + _studio_json.dumps(
            requested_anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + entity_text
    )

    diagnostics = []

    for attempt in range(2):
        raw, parsed, _ = (
            await _studio_v2371a_qwen_call(
                phase=(
                    "studio_stage04_"
                    "anchor_coverage_completion_qwen32b"
                ),
                messages=[{
                    "role": "user",
                    "content": prompt + (
                        ""
                        if attempt == 0
                        else (
                            "\n\nSTRICT_RETRY："
                            "assignments 数量必须严格等于 "
                            "MISSING_SOURCE_ANCHORS 数量；"
                            "每个 source_evidence_id "
                            "必须原样返回且只返回一次。"
                        )
                    ),
                }],
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=1100,
                contract=(
                    '{"assignments":[{'
                    '"source_evidence_id":"C01E001",'
                    '"destination":"support",'
                    '"target_beat_index":0,'
                    '"summary":"",'
                    '"state_change":"",'
                    '"character_entity_ids":[],'
                    '"prop_entity_ids":[]'
                    '}]}'
                ),
            )
        )

        payload = (
            parsed
            if isinstance(parsed, dict)
            else _studio_v2372_extract_object(
                raw,
                parsed,
            )
        )

        assignments = (
            payload.get("assignments")
            if isinstance(payload, dict)
            else None
        )

        if not isinstance(
            assignments,
            list,
        ):
            diagnostics.append(
                "attempt="
                + str(attempt + 1)
                + ": assignments 非数组"
            )
            continue

        try:
            return (
                _studio_v2372c_merge_assignments(
                    beats=beats,
                    support_ids=support_ids,
                    assignments=assignments,
                    requested_ids=requested_ids,
                    anchors=anchors,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except RuntimeError as exc:
            diagnostics.append(
                "attempt="
                + str(attempt + 1)
                + ": "
                + str(exc)
            )

    raise RuntimeError(
        "正文覆盖补全失败；requested="
        + repr(requested_ids)
        + "；"
        + " | ".join(diagnostics)
    )


async def _studio_v2372c_complete_missing(
    *,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
    missing_ids: list[str],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    current_beats = (
        _studio_v2372c_copy.deepcopy(
            beats
        )
    )
    current_support = list(
        support_ids or []
    )

    group_size = 6

    groups = [
        missing_ids[
            index:index + group_size
        ]
        for index in range(
            0,
            len(missing_ids),
            group_size,
        )
    ]

    for group_index, group in enumerate(
        groups,
        1,
    ):
        try:
            current_beats, current_support = (
                await _studio_v2372c_complete_group(
                    chunk=chunk,
                    anchors=anchors,
                    beats=current_beats,
                    support_ids=current_support,
                    requested_ids=group,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except RuntimeError as exc:
            raise RuntimeError(
                "覆盖补全 group "
                + str(group_index)
                + "/"
                + str(len(groups))
                + " failed："
                + str(exc)
            ) from exc

    expected = set(
        _studio_v2372c_anchor_map(
            anchors
        )
    )

    used = {
        str(evidence_id or "").strip()
        for beat in current_beats
        for evidence_id in (
            beat.get(
                "source_evidence_ids"
            )
            or []
        )
        if str(evidence_id or "").strip()
    }

    support = {
        str(key or "").strip()
        for key in current_support
        if str(key or "").strip()
    }

    overlap = used.intersection(
        support
    )

    if overlap:
        raise RuntimeError(
            "覆盖补全完成后 Beat/support 仍重叠："
            + repr(sorted(overlap))
        )

    missing_after = (
        expected - used - support
    )

    unexpected = (
        used.union(support) - expected
    )

    if missing_after or unexpected:
        raise RuntimeError(
            "覆盖补全完成后正文仍未完整分类；"
            f"missing={sorted(missing_after)} "
            f"unexpected={sorted(unexpected)}"
        )

    return (
        _studio_v2372c_sort_beats(
            current_beats
        ),
        sorted(
            support,
            key=lambda key: (
                int(
                    _studio_v2372c_anchor_map(
                        anchors
                    )[key].get(
                        "start"
                    )
                    or 0
                ),
                key,
            ),
        ),
    )


async def _studio_v2372_generate_chunk_beats(
    *,
    chunk: dict,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    anchors = (
        _studio_v2372_chunk_anchors(
            chunk
        )
    )

    if not anchors:
        return [], []

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    previous_issues = ""

    for attempt in range(2):
        system_prompt = (
            "你是小说正文 Narrative Beat 提取器，运行 Qwen3-32B。"
            "只处理 CORE_SOURCE_CHUNK，不把前后 context 当可消费正文。"
            "目标是由模型建立当前 Scene 的最小有序叙事状态图。"
            "每个证据单元是否成为 Beat，只依据它对该状态图的必要性判断："
            "移除后会改变后续状态、因果关系或必要上下文依赖，则归入 Beat；"
            "移除后不改变该状态图，则归入 support_evidence_ids。"
            "不得依据固定关键词、文本类别、题材类型或预设示例进行分类。"
            "每个 SOURCE_ANCHOR 最终必须且只能被分类一次。"
            "Beat summary/state_change 必须被自己的证据直接支持。"
            "character_entity_ids / prop_entity_ids 只使用 "
            "ALLOWED_ENTITIES 中真实 ID；不确定留空。"
            "如果一次输出遗漏少量锚点，系统会在后续独立 coverage completion "
            "步骤继续分类；不要为了避免遗漏而牺牲 Beat 语义质量。"
            "只输出严格 JSON。"
        )

        prompt = (
            f"CHUNK_PROGRESS="
            f"{chunk.get('index')}\n"
            "=== NON_ANCHOR_CONTEXT_BEFORE ===\n"
            + str(
                chunk.get(
                    "context_before"
                ) or ""
            )
            + "\n\n=== CORE_SOURCE_CHUNK ===\n"
            + str(chunk.get("text") or "")
            + "\n\n=== NON_ANCHOR_CONTEXT_AFTER ===\n"
            + str(
                chunk.get(
                    "context_after"
                ) or ""
            )
            + "\n\n=== SOURCE_ANCHORS ===\n"
            + _studio_json.dumps(
                anchors,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== ALLOWED_ENTITIES ===\n"
            + entity_text
        )

        if previous_issues:
            prompt += (
                "\n\n=== PREVIOUS_AUDIT_ISSUES ===\n"
                + previous_issues
                + "\n重新生成当前 chunk 的 Narrative Backbone；"
                "不要仅修改说明文字。"
            )

        raw, parsed, _ = (
            await _studio_v2371a_qwen_call(
                phase=(
                    "studio_stage04_"
                    "narrative_beat_extraction_qwen32b"
                ),
                messages=[{
                    "role": "user",
                    "content": prompt,
                }],
                system_prompt=system_prompt,
                temperature=(
                    0.06
                    if attempt == 0
                    else 0.0
                ),
                max_tokens=1800,
                contract=(
                    '{"beats":[{'
                    '"summary":"",'
                    '"state_change":"",'
                    '"source_evidence_ids":["C01E001"],'
                    '"character_entity_ids":[],'
                    '"prop_entity_ids":[]'
                    '}],'
                    '"support_evidence_ids":["C01E002"]}'
                ),
            )
        )

        payload = (
            parsed
            if isinstance(parsed, dict)
            else _studio_v2372_extract_object(
                raw,
                parsed,
            )
        )

        try:
            beats, support_ids, missing_ids = (
                _studio_v2372c_validate_partial(
                    payload=payload,
                    anchors=anchors,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except RuntimeError as exc:
            previous_issues = (
                "DETERMINISTIC_PARTIAL_ERROR: "
                + str(exc)
            )
            continue

        if missing_ids:
            try:
                beats, support_ids = (
                    await _studio_v2372c_complete_missing(
                        chunk=chunk,
                        anchors=anchors,
                        beats=beats,
                        support_ids=support_ids,
                        missing_ids=missing_ids,
                        allowed_chars=allowed_chars,
                        allowed_props=allowed_props,
                        entity_rows=entity_rows,
                    )
                )
            except RuntimeError as exc:
                previous_issues = (
                    "COVERAGE_COMPLETION_ERROR: "
                    + str(exc)
                )
                continue

        audit = (
            await _studio_v2372_audit_extraction(
                chunk=chunk,
                anchors=anchors,
                beats=beats,
                support_ids=support_ids,
            )
        )

        if (
            audit.get("valid") is True
            and not (
                audit.get("violations")
                or []
            )
        ):
            return (
                beats,
                support_ids,
            )

        previous_issues = (
            _studio_json.dumps(
                audit.get("violations")
                or audit,
                ensure_ascii=False,
            )
        )

    raise RuntimeError(
        "严格 Stage04：Narrative Beat 提取/覆盖补全两轮后"
        "仍未通过正文覆盖/粒度/证据蕴含审计："
        + previous_issues[:1800]
    )

# ===== /V2.37.2C STAGE04 ANCHOR COVERAGE COMPLETION =====


# ===== V2.37.2D STAGE04 COVERAGE OUTPUT RESILIENCE =====
import ast as _studio_v2372d_ast
import json as _studio_v2372d_json
import re as _studio_v2372d_re


def _studio_v2372d_strip_wrappers(
    text: object,
) -> str:
    raw = str(text or "").strip()

    raw = _studio_v2372d_re.sub(
        r"<think>.*?</think>",
        "",
        raw,
        flags=(
            _studio_v2372d_re.S
            | _studio_v2372d_re.I
        ),
    ).strip()

    raw = _studio_v2372d_re.sub(
        r"^\s*```(?:json|JSON)?\s*",
        "",
        raw,
    )

    raw = _studio_v2372d_re.sub(
        r"\s*```\s*$",
        "",
        raw,
    )

    return raw.strip()


def _studio_v2372d_collect_texts(
    value: object,
) -> list[str]:
    existing = globals().get(
        "_studio_v2371a_collect_texts"
    )

    if existing is not None:
        try:
            texts = existing(value)
        except Exception:
            texts = []

        clean = [
            str(text or "")
            for text in texts or []
            if str(text or "").strip()
        ]

        if clean:
            return clean

    result = []

    def walk(current):
        if current is None:
            return

        if isinstance(current, str):
            if current.strip():
                result.append(current)
            return

        if isinstance(current, dict):
            preferred = (
                "content",
                "text",
                "output",
                "response",
                "raw",
                "message",
            )

            for key in preferred:
                if key in current:
                    walk(current.get(key))

            for key,value in current.items():
                if key not in preferred:
                    walk(value)

            return

        if isinstance(
            current,
            (list, tuple),
        ):
            for item in current:
                walk(item)
            return

        try:
            text = str(current)
        except Exception:
            return

        if text.strip():
            result.append(text)

    walk(value)

    unique = []
    seen = set()

    for text in result:
        key = text.strip()

        if (
            key
            and key not in seen
        ):
            seen.add(key)
            unique.append(text)

    return unique


def _studio_v2372d_parse_jsonish(
    text: object,
):
    raw = _studio_v2372d_strip_wrappers(
        text
    )

    if not raw:
        return None

    variants = [
        raw,
        _studio_v2372d_re.sub(
            r",\s*([}\]])",
            r"\1",
            raw,
        ),
    ]

    for candidate in variants:
        try:
            return _studio_v2372d_json.loads(
                candidate
            )
        except Exception:
            pass

        try:
            return _studio_v2372d_ast.literal_eval(
                candidate
            )
        except Exception:
            pass

    return None


def _studio_v2372d_assignment_shape(
    row: object,
) -> bool:
    if not isinstance(row, dict):
        return False

    evidence_id = str(
        row.get("source_evidence_id")
        or ""
    ).strip()

    destination = str(
        row.get("destination")
        or ""
    ).strip()

    return bool(
        evidence_id
        and destination
    )


def _studio_v2372d_normalize_assignment(
    row: dict,
) -> dict:
    result = dict(row)

    result[
        "source_evidence_id"
    ] = str(
        result.get(
            "source_evidence_id"
        )
        or ""
    ).strip()

    result["destination"] = str(
        result.get("destination")
        or ""
    ).strip().lower()

    try:
        result[
            "target_beat_index"
        ] = int(
            result.get(
                "target_beat_index"
            )
            or 0
        )
    except Exception:
        result[
            "target_beat_index"
        ] = 0

    for key in (
        "summary",
        "state_change",
    ):
        result[key] = str(
            result.get(key)
            or ""
        ).strip()

    for key in (
        "character_entity_ids",
        "prop_entity_ids",
    ):
        values = result.get(key)

        if not isinstance(values, list):
            values = []

        result[key] = values

    return result


def _studio_v2372d_find_assignment_list(
    value: object,
    *,
    depth: int = 0,
) -> list[dict]:
    if depth > 8:
        return []

    if isinstance(value, list):
        shaped = [
            _studio_v2372d_normalize_assignment(
                row
            )
            for row in value
            if _studio_v2372d_assignment_shape(
                row
            )
        ]

        if (
            shaped
            and len(shaped)
            == len([
                row
                for row in value
                if isinstance(row, dict)
            ])
        ):
            return shaped

        for item in value:
            found = (
                _studio_v2372d_find_assignment_list(
                    item,
                    depth=depth + 1,
                )
            )

            if found:
                return found

        return []

    if isinstance(value, dict):
        direct = value.get(
            "assignments"
        )

        if isinstance(direct, str):
            parsed = (
                _studio_v2372d_parse_jsonish(
                    direct
                )
            )

            found = (
                _studio_v2372d_find_assignment_list(
                    parsed,
                    depth=depth + 1,
                )
            )

            if found:
                return found

        if isinstance(
            direct,
            (list, dict),
        ):
            found = (
                _studio_v2372d_find_assignment_list(
                    direct,
                    depth=depth + 1,
                )
            )

            if found:
                return found

        # Structural map form:
        # {
        #   "C01E001": {"destination": "...", ...},
        #   ...
        # }
        mapping_rows = []

        for key,item in value.items():
            if not isinstance(item, dict):
                mapping_rows = []
                break

            if not str(
                item.get("destination")
                or ""
            ).strip():
                mapping_rows = []
                break

            row = dict(item)

            if not str(
                row.get(
                    "source_evidence_id"
                )
                or ""
            ).strip():
                row[
                    "source_evidence_id"
                ] = str(key)

            if not (
                _studio_v2372d_assignment_shape(
                    row
                )
            ):
                mapping_rows = []
                break

            mapping_rows.append(
                _studio_v2372d_normalize_assignment(
                    row
                )
            )

        if mapping_rows:
            return mapping_rows

        for item in value.values():
            found = (
                _studio_v2372d_find_assignment_list(
                    item,
                    depth=depth + 1,
                )
            )

            if found:
                return found

    return []


def _studio_v2372d_balanced_objects_from_array(
    text: str,
    array_start: int,
    *,
    limit: int = 32,
) -> list[dict]:
    raw = str(text or "")

    if (
        array_start < 0
        or array_start >= len(raw)
        or raw[array_start] != "["
    ):
        return []

    result = []
    index = array_start + 1

    while (
        index < len(raw)
        and len(result) < limit
    ):
        while (
            index < len(raw)
            and raw[index] in " \t\r\n,"
        ):
            index += 1

        if (
            index >= len(raw)
            or raw[index] == "]"
        ):
            break

        if raw[index] != "{":
            next_object = raw.find(
                "{",
                index + 1,
            )

            if next_object < 0:
                break

            index = next_object

        start = index
        depth = 0
        in_string = False
        quote = ""
        escape = False
        end = -1

        for position in range(
            start,
            len(raw),
        ):
            char = raw[position]

            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == quote:
                    in_string = False
                continue

            if char in (
                '"',
                "'",
            ):
                in_string = True
                quote = char
                continue

            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1

                if depth == 0:
                    end = position + 1
                    break

                if depth < 0:
                    break

        if end < 0:
            break

        parsed = (
            _studio_v2372d_parse_jsonish(
                raw[start:end]
            )
        )

        if isinstance(parsed, dict):
            result.append(parsed)

        index = end

    return result


def _studio_v2372d_assignments_from_text(
    text: object,
) -> list[dict]:
    raw = _studio_v2372d_strip_wrappers(
        text
    )

    if not raw:
        return []

    parsed = _studio_v2372d_parse_jsonish(
        raw
    )

    found = (
        _studio_v2372d_find_assignment_list(
            parsed
        )
    )

    if found:
        return found

    # Truncated outer object:
    # {"assignments":[ {...}, {...}, {truncated...
    match = _studio_v2372d_re.search(
        r'["\']assignments["\']\s*:\s*\[',
        raw,
        flags=_studio_v2372d_re.I,
    )

    if match:
        array_start = raw.find(
            "[",
            match.start(),
        )

        rows = (
            _studio_v2372d_balanced_objects_from_array(
                raw,
                array_start,
            )
        )

        found = (
            _studio_v2372d_find_assignment_list(
                rows
            )
        )

        if found:
            return found

    # Bare top-level array, including truncated array.
    first_array = raw.find("[")

    if first_array >= 0:
        rows = (
            _studio_v2372d_balanced_objects_from_array(
                raw,
                first_array,
            )
        )

        found = (
            _studio_v2372d_find_assignment_list(
                rows
            )
        )

        if found:
            return found

    return []


def _studio_v2372d_extract_assignments(
    *,
    raw: object,
    parsed: object,
) -> tuple[
    list[dict],
    str,
]:
    found = (
        _studio_v2372d_find_assignment_list(
            parsed
        )
    )

    if found:
        return found, "parsed-structure"

    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )

    for text in sorted(
        texts,
        key=len,
        reverse=True,
    ):
        found = (
            _studio_v2372d_assignments_from_text(
                text
            )
        )

        if found:
            return (
                found,
                "raw-structural-recovery",
            )

    return [], "not-found"


def _studio_v2372d_assignments_from_lines(
    raw: object,
) -> list[dict]:
    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )

    result = []

    for text in sorted(
        texts,
        key=len,
        reverse=True,
    ):
        current = []

        for raw_line in str(text).splitlines():
            # Preserve trailing TAB fields because support rows may
            # legitimately have empty summary/state/entity columns.
            line = raw_line.rstrip("\r\n")

            if not line.strip():
                continue

            if not line.startswith(
                "ASSIGN\t"
            ):
                continue

            parts = line.split("\t")

            if len(parts) < 4:
                current = []
                break

            while len(parts) < 8:
                parts.append("")

            evidence_id = parts[1].strip()
            destination = parts[2].strip().lower()

            try:
                target = int(
                    parts[3].strip()
                    or 0
                )
            except Exception:
                target = 0

            summary = parts[4].strip()
            state_change = parts[5].strip()

            char_ids = [
                value.strip()
                for value in parts[6].split(",")
                if value.strip()
            ]

            prop_ids = [
                value.strip()
                for value in parts[7].split(",")
                if value.strip()
            ]

            current.append({
                "source_evidence_id":
                    evidence_id,
                "destination":
                    destination,
                "target_beat_index":
                    target,
                "summary":
                    summary,
                "state_change":
                    state_change,
                "character_entity_ids":
                    char_ids,
                "prop_entity_ids":
                    prop_ids,
            })

        if current:
            result = current
            break

    return result


async def _studio_v2372c_complete_group(
    *,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
    requested_ids: list[str],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    amap = _studio_v2372c_anchor_map(
        anchors
    )

    requested_anchors = [
        amap[key]
        for key in requested_ids
        if key in amap
    ]

    compact_beats = [{
        "beat_index": index + 1,
        "summary": row.get("summary"),
        "state_change":
            row.get("state_change"),
        "source_evidence_ids":
            row.get("source_evidence_ids"),
        "source_evidence":
            row.get("source_evidence"),
    } for index,row in enumerate(beats)]

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    system_prompt = (
        "你是 Narrative Backbone 正文覆盖补全器，运行 Qwen3-32B。"
        "上一次提取已经产生一组有效 Beat/support，但遗漏了少量正文锚点。"
        "你只处理 MISSING_SOURCE_ANCHORS，不重写已经完成的分类。"
        "每个缺失锚点必须且只能选择一个结构目的地："
        "existing_beat、new_beat 或 support。"
        "选择依据只来自当前 Scene 的最小有序叙事状态图。"
        "不得依据固定关键词、文本类别、题材类型或预设业务词表分类。"
        "new_beat 必须填写 summary/state_change；"
        "existing_beat 必须填写 CURRENT_BEATS 中有效的 target_beat_index；"
        "support 的 target_beat_index=0。"
        "所有 requested source_evidence_id 必须逐项返回一次，不能遗漏、不能新增。"
        "只输出严格 JSON。"
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== CURRENT_BEATS ===\n"
        + _studio_json.dumps(
            compact_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== CURRENT_SUPPORT_IDS ===\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
        + "\n\n=== MISSING_SOURCE_ANCHORS ===\n"
        + _studio_json.dumps(
            requested_anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + entity_text
    )

    diagnostics = []

    attempts = (
        (
            "json-primary",
            "",
            0.0,
            1400,
        ),
        (
            "json-strict",
            (
                "\n\nSTRICT_JSON_RETRY："
                "只返回一个 JSON 对象，顶层 assignments 必须是数组；"
                "assignments 数量必须严格等于 MISSING_SOURCE_ANCHORS 数量；"
                "每个 source_evidence_id 原样返回且只出现一次。"
            ),
            0.0,
            1400,
        ),
        (
            "line-protocol",
            (
                "\n\nSERIALIZATION_FALLBACK："
                "不要输出 JSON。每个缺失锚点严格输出一行，格式：\n"
                "ASSIGN<TAB>source_evidence_id<TAB>destination"
                "<TAB>target_beat_index<TAB>summary<TAB>state_change"
                "<TAB>character_entity_ids逗号分隔<TAB>prop_entity_ids逗号分隔\n"
                "不得输出其他文字；所有 requested ID 必须逐项出现一次。"
            ),
            0.0,
            1200,
        ),
    )

    for (
        attempt_name,
        suffix,
        temperature,
        max_tokens,
    ) in attempts:
        try:
            raw, parsed, _ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "anchor_coverage_completion_qwen32b"
                    ),
                    messages=[{
                        "role": "user",
                        "content": prompt + suffix,
                    }],
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    contract=(
                        '{"assignments":[{'
                        '"source_evidence_id":"C01E001",'
                        '"destination":"support",'
                        '"target_beat_index":0,'
                        '"summary":"",'
                        '"state_change":"",'
                        '"character_entity_ids":[],'
                        '"prop_entity_ids":[]'
                        '}]}'
                    ),
                )
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": qwen_call="
                + type(exc).__name__
                + ": "
                + str(exc)[:420]
            )
            continue

        if attempt_name == "line-protocol":
            assignments = (
                _studio_v2372d_assignments_from_lines(
                    raw
                )
            )
            origin = "line-protocol"
        else:
            assignments, origin = (
                _studio_v2372d_extract_assignments(
                    raw=raw,
                    parsed=parsed,
                )
            )

        if not assignments:
            parsed_type = type(parsed).__name__
            parsed_keys = (
                sorted(parsed.keys())
                if isinstance(parsed, dict)
                else []
            )

            texts = (
                _studio_v2372d_collect_texts(
                    raw
                )
            )

            preview = ""

            if texts:
                preview = (
                    _studio_v2372d_re.sub(
                        r"\s+",
                        " ",
                        max(
                            texts,
                            key=len,
                        ),
                    )[:520]
                )

            diagnostics.append(
                attempt_name
                + ": assignments_not_found"
                + " parsed_type="
                + parsed_type
                + " parsed_keys="
                + repr(parsed_keys)
                + " raw_preview="
                + repr(preview)
            )
            continue

        try:
            return (
                _studio_v2372c_merge_assignments(
                    beats=beats,
                    support_ids=support_ids,
                    assignments=assignments,
                    requested_ids=requested_ids,
                    anchors=anchors,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except RuntimeError as exc:
            diagnostics.append(
                attempt_name
                + " origin="
                + origin
                + " rows="
                + str(len(assignments))
                + ": "
                + str(exc)
            )

    raise RuntimeError(
        "正文覆盖补全失败；requested="
        + repr(requested_ids)
        + "；"
        + " | ".join(diagnostics)
    )

# ===== /V2.37.2D STAGE04 COVERAGE OUTPUT RESILIENCE =====


# ===== V2.37.2E STAGE04 ASSIGNMENT SCHEMA ALIAS RECOVERY =====


def _studio_v2372e_assignment_fields(
    row: object,
) -> tuple[str, str]:
    if not isinstance(row, dict):
        return "", ""

    evidence_id = str(
        row.get("source_evidence_id")
        or row.get("id")
        or ""
    ).strip()

    destination = str(
        row.get("destination")
        or row.get("structure_destination")
        or ""
    ).strip().lower()

    return evidence_id, destination


def _studio_v2372d_assignment_shape(
    row: object,
) -> bool:
    evidence_id, destination = (
        _studio_v2372e_assignment_fields(
            row
        )
    )

    return bool(
        evidence_id
        and destination
    )


def _studio_v2372d_normalize_assignment(
    row: dict,
) -> dict:
    result = dict(row)

    evidence_id, destination = (
        _studio_v2372e_assignment_fields(
            result
        )
    )

    result[
        "source_evidence_id"
    ] = evidence_id

    result["destination"] = destination

    try:
        result[
            "target_beat_index"
        ] = int(
            result.get(
                "target_beat_index"
            )
            or 0
        )
    except Exception:
        result[
            "target_beat_index"
        ] = 0

    for key in (
        "summary",
        "state_change",
    ):
        result[key] = str(
            result.get(key)
            or ""
        ).strip()

    for key in (
        "character_entity_ids",
        "prop_entity_ids",
    ):
        values = result.get(key)

        if not isinstance(values, list):
            values = []

        result[key] = values

    # Remove only the observed serialization aliases after canonicalization.
    result.pop(
        "structure_destination",
        None,
    )

    if (
        "id" in result
        and result.get("id")
        == evidence_id
    ):
        result.pop("id", None)

    return result


def _studio_v2372e_single_assignment(
    value: object,
) -> list[dict]:
    if not (
        _studio_v2372d_assignment_shape(
            value
        )
    ):
        return []

    return [
        _studio_v2372d_normalize_assignment(
            value
        )
    ]


_V2372E_PREVIOUS_FIND_ASSIGNMENT_LIST = (
    _studio_v2372d_find_assignment_list
)


def _studio_v2372d_find_assignment_list(
    value: object,
    *,
    depth: int = 0,
) -> list[dict]:
    if depth > 8:
        return []

    if isinstance(value, dict):
        # A lossy generic parser may return only one object from a top-level
        # array. Expose it as a candidate, but the final extractor will compare
        # it with raw-response candidates and choose the best requested-ID
        # coverage instead of blindly returning this one row.
        single = (
            _studio_v2372e_single_assignment(
                value
            )
        )

        direct = value.get(
            "assignments"
        )

        if isinstance(direct, str):
            parsed = (
                _studio_v2372d_parse_jsonish(
                    direct
                )
            )

            found = (
                _studio_v2372d_find_assignment_list(
                    parsed,
                    depth=depth + 1,
                )
            )

            if found:
                return found

        if isinstance(
            direct,
            (list, dict),
        ):
            found = (
                _studio_v2372d_find_assignment_list(
                    direct,
                    depth=depth + 1,
                )
            )

            if found:
                return found

        # Structural map form using either canonical destination or the
        # observed Qwen alias structure_destination.
        mapping_rows = []

        for key,item in value.items():
            if not isinstance(item, dict):
                mapping_rows = []
                break

            candidate = dict(item)

            if not str(
                candidate.get(
                    "source_evidence_id"
                )
                or candidate.get("id")
                or ""
            ).strip():
                candidate[
                    "source_evidence_id"
                ] = str(key)

            if not (
                _studio_v2372d_assignment_shape(
                    candidate
                )
            ):
                mapping_rows = []
                break

            mapping_rows.append(
                _studio_v2372d_normalize_assignment(
                    candidate
                )
            )

        if mapping_rows:
            return mapping_rows

        for item in value.values():
            found = (
                _studio_v2372d_find_assignment_list(
                    item,
                    depth=depth + 1,
                )
            )

            if found:
                return found

        if single:
            return single

        return []

    if isinstance(value, list):
        dict_rows = [
            row
            for row in value
            if isinstance(row, dict)
        ]

        shaped = [
            _studio_v2372d_normalize_assignment(
                row
            )
            for row in dict_rows
            if _studio_v2372d_assignment_shape(
                row
            )
        ]

        if (
            shaped
            and len(shaped)
            == len(dict_rows)
        ):
            return shaped

        for item in value:
            found = (
                _studio_v2372d_find_assignment_list(
                    item,
                    depth=depth + 1,
                )
            )

            if found:
                return found

    return []


def _studio_v2372e_candidate_ids(
    rows: list[dict],
) -> list[str]:
    return [
        str(
            row.get(
                "source_evidence_id"
            )
            or ""
        ).strip()
        for row in rows or []
        if str(
            row.get(
                "source_evidence_id"
            )
            or ""
        ).strip()
    ]


def _studio_v2372e_candidate_score(
    rows: list[dict],
    *,
    requested_ids: list[str],
) -> tuple[
    int,
    int,
    int,
    int,
]:
    requested = set(
        requested_ids or []
    )

    ids = (
        _studio_v2372e_candidate_ids(
            rows
        )
    )

    id_set = set(ids)

    duplicate_count = (
        len(ids)
        - len(id_set)
    )

    unexpected = (
        id_set - requested
    )

    covered = (
        id_set & requested
    )

    exact = int(
        bool(rows)
        and not duplicate_count
        and not unexpected
        and id_set == requested
    )

    return (
        exact,
        len(covered),
        -len(unexpected),
        -duplicate_count,
    )


def _studio_v2372e_raw_candidates(
    raw: object,
) -> list[
    tuple[str, list[dict]]
]:
    candidates = []

    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )

    for index,text in enumerate(
        sorted(
            texts,
            key=len,
            reverse=True,
        ),
        1,
    ):
        raw_text = (
            _studio_v2372d_strip_wrappers(
                text
            )
        )

        parsed = (
            _studio_v2372d_parse_jsonish(
                raw_text
            )
        )

        found = (
            _studio_v2372d_find_assignment_list(
                parsed
            )
        )

        if found:
            candidates.append((
                f"raw-json-{index}",
                found,
            ))

        # Existing robust raw parser includes truncated-array recovery.
        recovered = (
            _studio_v2372d_assignments_from_text(
                raw_text
            )
        )

        if recovered:
            candidates.append((
                f"raw-recovery-{index}",
                recovered,
            ))

    return candidates


def _studio_v2372e_extract_assignments(
    *,
    raw: object,
    parsed: object,
    requested_ids: list[str],
) -> tuple[
    list[dict],
    str,
    list[dict],
]:
    candidates = []

    parsed_rows = (
        _studio_v2372d_find_assignment_list(
            parsed
        )
    )

    if parsed_rows:
        candidates.append((
            "parsed-structure",
            parsed_rows,
        ))

    candidates.extend(
        _studio_v2372e_raw_candidates(
            raw
        )
    )

    # Deduplicate candidates by canonical IDs + destinations + targets.
    unique = []
    seen = set()

    for origin,rows in candidates:
        canonical = [
            _studio_v2372d_normalize_assignment(
                row
            )
            for row in rows or []
            if isinstance(row, dict)
        ]

        signature = tuple(
            (
                row.get(
                    "source_evidence_id"
                ),
                row.get("destination"),
                row.get(
                    "target_beat_index"
                ),
                row.get("summary"),
                row.get("state_change"),
            )
            for row in canonical
        )

        if not signature:
            continue

        if signature in seen:
            continue

        seen.add(signature)

        unique.append((
            origin,
            canonical,
        ))

    if not unique:
        return [], "not-found", []

    ranked = sorted(
        unique,
        key=lambda item: (
            _studio_v2372e_candidate_score(
                item[1],
                requested_ids=requested_ids,
            ),
            len(item[1]),
        ),
        reverse=True,
    )

    best_origin,best_rows = (
        ranked[0]
    )

    diagnostics = []

    for origin,rows in ranked:
        ids = (
            _studio_v2372e_candidate_ids(
                rows
            )
        )

        diagnostics.append({
            "origin": origin,
            "rows": len(rows),
            "ids": ids,
            "score":
                _studio_v2372e_candidate_score(
                    rows,
                    requested_ids=requested_ids,
                ),
        })

    return (
        best_rows,
        best_origin,
        diagnostics,
    )


async def _studio_v2372c_complete_group(
    *,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
    requested_ids: list[str],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    amap = _studio_v2372c_anchor_map(
        anchors
    )

    requested_anchors = [
        amap[key]
        for key in requested_ids
        if key in amap
    ]

    compact_beats = [{
        "beat_index": index + 1,
        "summary": row.get("summary"),
        "state_change":
            row.get("state_change"),
        "source_evidence_ids":
            row.get("source_evidence_ids"),
        "source_evidence":
            row.get("source_evidence"),
    } for index,row in enumerate(beats)]

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    system_prompt = (
        "你是 Narrative Backbone 正文覆盖补全器，运行 Qwen3-32B。"
        "上一次提取已经产生一组有效 Beat/support，但遗漏了少量正文锚点。"
        "你只处理 MISSING_SOURCE_ANCHORS，不重写已经完成的分类。"
        "每个缺失锚点必须且只能选择一个结构目的地："
        "existing_beat、new_beat 或 support。"
        "选择依据只来自当前 Scene 的最小有序叙事状态图。"
        "不得依据固定关键词、文本类别、题材类型或预设业务词表分类。"
        "new_beat 必须填写 summary/state_change；"
        "existing_beat 必须填写 CURRENT_BEATS 中有效的 target_beat_index；"
        "support 的 target_beat_index=0。"
        "所有 requested source_evidence_id 必须逐项返回一次，不能遗漏、不能新增。"
        "字段名必须使用 source_evidence_id、destination、target_beat_index。"
        "只输出严格 JSON。"
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== CURRENT_BEATS ===\n"
        + _studio_json.dumps(
            compact_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== CURRENT_SUPPORT_IDS ===\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
        + "\n\n=== MISSING_SOURCE_ANCHORS ===\n"
        + _studio_json.dumps(
            requested_anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + entity_text
    )

    diagnostics = []

    attempts = (
        (
            "json-primary",
            "",
            0.0,
            1400,
        ),
        (
            "json-strict",
            (
                "\n\nSTRICT_JSON_RETRY："
                "只返回一个 JSON 对象，顶层 assignments 必须是数组；"
                "数组元素字段必须严格命名为 "
                "source_evidence_id、destination、target_beat_index、"
                "summary、state_change、character_entity_ids、prop_entity_ids；"
                "assignments 数量必须严格等于 MISSING_SOURCE_ANCHORS 数量；"
                "每个 source_evidence_id 原样返回且只出现一次。"
            ),
            0.0,
            1400,
        ),
        (
            "line-protocol",
            (
                "\n\nSERIALIZATION_FALLBACK："
                "不要输出 JSON。每个缺失锚点严格输出一行，格式：\n"
                "ASSIGN<TAB>source_evidence_id<TAB>destination"
                "<TAB>target_beat_index<TAB>summary<TAB>state_change"
                "<TAB>character_entity_ids逗号分隔<TAB>prop_entity_ids逗号分隔\n"
                "不得输出其他文字；所有 requested ID 必须逐项出现一次。"
            ),
            0.0,
            1200,
        ),
    )

    for (
        attempt_name,
        suffix,
        temperature,
        max_tokens,
    ) in attempts:
        try:
            raw, parsed, _ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "anchor_coverage_completion_qwen32b"
                    ),
                    messages=[{
                        "role": "user",
                        "content": prompt + suffix,
                    }],
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    contract=(
                        '{"assignments":[{'
                        '"source_evidence_id":"C01E001",'
                        '"destination":"support",'
                        '"target_beat_index":0,'
                        '"summary":"",'
                        '"state_change":"",'
                        '"character_entity_ids":[],'
                        '"prop_entity_ids":[]'
                        '}]}'
                    ),
                )
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": qwen_call="
                + type(exc).__name__
                + ": "
                + str(exc)[:420]
            )
            continue

        if attempt_name == "line-protocol":
            assignments = (
                _studio_v2372d_assignments_from_lines(
                    raw
                )
            )
            origin = "line-protocol"
            candidate_diagnostics = []
        else:
            (
                assignments,
                origin,
                candidate_diagnostics,
            ) = (
                _studio_v2372e_extract_assignments(
                    raw=raw,
                    parsed=parsed,
                    requested_ids=requested_ids,
                )
            )

        if not assignments:
            texts = (
                _studio_v2372d_collect_texts(
                    raw
                )
            )

            preview = ""

            if texts:
                preview = (
                    _studio_v2372d_re.sub(
                        r"\s+",
                        " ",
                        max(
                            texts,
                            key=len,
                        ),
                    )[:620]
                )

            diagnostics.append(
                attempt_name
                + ": assignments_not_found"
                + " parsed_type="
                + type(parsed).__name__
                + " parsed_keys="
                + repr(
                    sorted(parsed.keys())
                    if isinstance(parsed, dict)
                    else []
                )
                + " raw_preview="
                + repr(preview)
            )
            continue

        try:
            return (
                _studio_v2372c_merge_assignments(
                    beats=beats,
                    support_ids=support_ids,
                    assignments=assignments,
                    requested_ids=requested_ids,
                    anchors=anchors,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except RuntimeError as exc:
            diagnostics.append(
                attempt_name
                + " origin="
                + origin
                + " rows="
                + str(len(assignments))
                + " ids="
                + repr(
                    _studio_v2372e_candidate_ids(
                        assignments
                    )
                )
                + " candidates="
                + repr(
                    candidate_diagnostics
                )[:900]
                + ": "
                + str(exc)
            )

    raise RuntimeError(
        "正文覆盖补全失败；requested="
        + repr(requested_ids)
        + "；"
        + " | ".join(diagnostics)
    )

# ===== /V2.37.2E STAGE04 ASSIGNMENT SCHEMA ALIAS RECOVERY =====


# ===== V2.37.2F R1 STAGE04 BEAT OUTPUT RESILIENCE =====
import copy as _studio_v2372f_copy
import re as _studio_v2372f_re


def _studio_v2372f_values(
    value: object,
) -> list[object]:
    if value is None:
        return []

    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(
                _studio_v2372f_values(item)
            )
        return result

    return [value]


def _studio_v2372f_structural_ids(
    value: object,
) -> list[str]:
    """
    Syntax-only ID extraction. No semantic inference.
    """
    result = []

    def add(candidate):
        key = str(candidate or "").strip()
        if key and key not in result:
            result.append(key)

    for item in _studio_v2372f_values(value):
        if isinstance(item, dict):
            for key in (
                "source_evidence_id",
                "evidence_id",
                "anchor_id",
                "source_id",
                "id",
            ):
                if item.get(key) is not None:
                    add(item.get(key))
            continue

        text = str(item or "").strip()
        if not text:
            continue

        # A field may serialize IDs as comma/space separated text.
        tokens = _studio_v2372f_re.findall(
            r"C\d{2}E\d{3}|E\d{3}",
            text,
            flags=_studio_v2372f_re.I,
        )

        if tokens:
            for token in tokens:
                add(token)
        else:
            add(text)

    return result


def _studio_v2372f_evidence_texts(
    value: object,
) -> list[str]:
    result = []

    def add(text):
        current = str(text or "").strip()
        if current and current not in result:
            result.append(current)

    for item in _studio_v2372f_values(value):
        if isinstance(item, dict):
            for key in (
                "text",
                "quote",
                "source_text",
                "evidence_text",
                "excerpt",
            ):
                if item.get(key) is not None:
                    add(item.get(key))
            continue

        text = str(item or "").strip()

        # Do not treat structural IDs themselves as evidence text.
        if _studio_v2372f_re.fullmatch(
            r"(?:C\d{2})?E\d{3}",
            text,
            flags=_studio_v2372f_re.I,
        ):
            continue

        add(text)

    return result


def _studio_v2372f_resolve_evidence_ids(
    row: dict,
    *,
    anchors: list[dict],
) -> list[str]:
    amap = {
        str(anchor.get("id") or ""): anchor
        for anchor in anchors or []
        if isinstance(anchor, dict)
        and str(anchor.get("id") or "")
    }

    candidates = []

    for key in (
        "source_evidence_ids",
        "evidence_ids",
        "anchor_ids",
        "source_ids",
        "source_anchor_ids",
        "source_evidence_id",
        "evidence_id",
        "anchor_id",
        "source_id",
        "anchors",
    ):
        if key in row:
            candidates.extend(
                _studio_v2372f_structural_ids(
                    row.get(key)
                )
            )

    result = []

    for key in candidates:
        if key in amap and key not in result:
            result.append(key)

    # Exact-text recovery only. If the same exact text maps to more than one
    # anchor, it is ambiguous and intentionally NOT resolved.
    text_values = []

    for key in (
        "source_evidence",
        "evidence",
        "source_quotes",
        "quotes",
        "source_quote",
        "evidence_text",
    ):
        if key in row:
            text_values.extend(
                _studio_v2372f_evidence_texts(
                    row.get(key)
                )
            )

    for text in text_values:
        matches = [
            anchor_id
            for anchor_id,anchor in amap.items()
            if str(
                anchor.get("text") or ""
            ).strip() == text
        ]

        if len(matches) == 1:
            key = matches[0]
            if key not in result:
                result.append(key)

    return _studio_v23962_order_evidence_ids(
        result,
        anchors=anchors,
    )


def _studio_v23962_order_evidence_ids(
    values: object,
    *,
    anchors: list[dict],
) -> list[str]:
    """Canonicalize evidence IDs by their authoritative source offsets."""
    amap = {
        str(anchor.get("id") or ""): anchor
        for anchor in (anchors or [])
        if isinstance(anchor, dict)
        and str(anchor.get("id") or "")
    }
    ordered = []
    for value in values or []:
        key = str(value or "").strip()
        if key in amap and key not in ordered:
            ordered.append(key)
    return sorted(
        ordered,
        key=lambda key: (
            int(amap[key].get("start") or 0),
            int(amap[key].get("end") or 0),
            key,
        ),
    )


def _studio_v23962_close_validated_beats(
    beats: list[dict],
    *,
    anchors: list[dict],
) -> list[dict]:
    """Keep Beat semantics and exact evidence lineage as one closure."""
    amap = {
        str(anchor.get("id") or ""): anchor
        for anchor in (anchors or [])
        if isinstance(anchor, dict)
        and str(anchor.get("id") or "")
    }
    closed = []
    consumed = set()
    for index, beat in enumerate(beats or [], 1):
        if not isinstance(beat, dict):
            continue
        raw_ids = [
            str(value or "").strip()
            for value in (beat.get("source_evidence_ids") or [])
            if str(value or "").strip()
        ]
        unknown = sorted(set(raw_ids) - set(amap))
        if unknown:
            raise RuntimeError(
                f"V2.39.6.2: Beat#{index} 引用未知 evidence IDs：{unknown}"
            )
        ids = _studio_v23962_order_evidence_ids(
            raw_ids,
            anchors=anchors,
        )
        if not ids:
            raise RuntimeError(
                f"V2.39.6.2: Beat#{index} evidence lineage 为空"
            )
        overlap = consumed.intersection(ids)
        if overlap:
            raise RuntimeError(
                f"V2.39.6.2: cross-Beat evidence overlap：{sorted(overlap)}"
            )
        consumed.update(ids)
        row = dict(beat)
        row["source_evidence_ids"] = ids
        row["source_evidence"] = [
            str(amap[key].get("text") or "")
            for key in ids
        ]
        row["source_evidence_spans"] = [{
            "id": key,
            "start": int(amap[key].get("start") or 0),
            "end": int(amap[key].get("end") or 0),
            "text": str(amap[key].get("text") or ""),
        } for key in ids]
        closed.append(row)
    return sorted(
        closed,
        key=lambda row: (
            min(
                int(span.get("start") or 0)
                for span in row["source_evidence_spans"]
            ),
            max(
                int(span.get("end") or 0)
                for span in row["source_evidence_spans"]
            ),
            str(row.get("summary") or ""),
        ),
    )


def _studio_v2372f_id_list(
    row: dict,
    *,
    canonical: str,
    aliases: tuple[str, ...],
) -> list[object]:
    for key in (
        canonical,
        *aliases,
    ):
        value = row.get(key)
        if isinstance(value, list):
            return value
    return []


def _studio_v2372f_normalize_beat(
    row: dict,
    *,
    anchors: list[dict],
) -> dict:
    result = dict(row)

    summary = str(
        result.get("summary")
        or result.get("beat_summary")
        or result.get("narrative_summary")
        or result.get("description")
        or ""
    ).strip()

    state_change = str(
        result.get("state_change")
        or result.get("state_delta")
        or result.get("narrative_change")
        or result.get("change")
        or ""
    ).strip()

    result["summary"] = summary
    result["state_change"] = state_change

    result[
        "source_evidence_ids"
    ] = _studio_v2372f_resolve_evidence_ids(
        result,
        anchors=anchors,
    )

    result[
        "character_entity_ids"
    ] = _studio_v2372f_id_list(
        result,
        canonical="character_entity_ids",
        aliases=(
            "character_ids",
            "characters",
        ),
    )

    result[
        "prop_entity_ids"
    ] = _studio_v2372f_id_list(
        result,
        canonical="prop_entity_ids",
        aliases=(
            "prop_ids",
            "props",
        ),
    )

    return result


def _studio_v2372f_beat_shape(
    row: object,
    *,
    anchors: list[dict],
) -> bool:
    if not isinstance(row, dict):
        return False

    normalized = (
        _studio_v2372f_normalize_beat(
            row,
            anchors=anchors,
        )
    )

    return bool(
        str(
            normalized.get("summary")
            or ""
        ).strip()
        or normalized.get(
            "source_evidence_ids"
        )
    )


def _studio_v2372f_support_ids(
    value: object,
    *,
    anchors: list[dict],
) -> list[str]:
    amap = {
        str(anchor.get("id") or ""): anchor
        for anchor in anchors or []
        if isinstance(anchor, dict)
        and str(anchor.get("id") or "")
    }

    result = []

    for key in (
        _studio_v2372f_structural_ids(
            value
        )
    ):
        if key in amap and key not in result:
            result.append(key)

    # Exact evidence text can also identify support structurally.
    for text in (
        _studio_v2372f_evidence_texts(
            value
        )
    ):
        matches = [
            anchor_id
            for anchor_id,anchor in amap.items()
            if str(
                anchor.get("text") or ""
            ).strip() == text
        ]

        if len(matches) == 1:
            key = matches[0]
            if key not in result:
                result.append(key)

    return result


def _studio_v2372f_find_payloads(
    value: object,
    *,
    anchors: list[dict],
    depth: int = 0,
) -> list[dict]:
    if depth > 8:
        return []

    result = []

    if isinstance(value, list):
        dict_rows = [
            row
            for row in value
            if isinstance(row, dict)
        ]

        beat_rows = [
            _studio_v2372f_normalize_beat(
                row,
                anchors=anchors,
            )
            for row in dict_rows
            if _studio_v2372f_beat_shape(
                row,
                anchors=anchors,
            )
        ]

        if (
            beat_rows
            and len(beat_rows)
            == len(dict_rows)
        ):
            result.append({
                "beats": beat_rows,
                "support_evidence_ids": [],
            })

        for item in value:
            result.extend(
                _studio_v2372f_find_payloads(
                    item,
                    anchors=anchors,
                    depth=depth + 1,
                )
            )

        return result

    if not isinstance(value, dict):
        return result

    beat_keys = (
        "beats",
        "narrative_beats",
        "beat_list",
        "narrative_units",
        "units",
        "items",
    )

    support_keys = (
        "support_evidence_ids",
        "support_ids",
        "support",
        "supporting_evidence_ids",
        "context_evidence_ids",
    )

    for beat_key in beat_keys:
        rows = value.get(beat_key)

        if not isinstance(rows, list):
            continue

        normalized_rows = [
            _studio_v2372f_normalize_beat(
                row,
                anchors=anchors,
            )
            for row in rows
            if isinstance(row, dict)
            and _studio_v2372f_beat_shape(
                row,
                anchors=anchors,
            )
        ]

        if not normalized_rows and rows:
            continue

        support = []

        for support_key in support_keys:
            if support_key in value:
                support.extend(
                    _studio_v2372f_support_ids(
                        value.get(support_key),
                        anchors=anchors,
                    )
                )

        result.append({
            "beats": normalized_rows,
            "support_evidence_ids":
                list(dict.fromkeys(support)),
        })

    # A lossy parser may surface a single Beat object.
    if _studio_v2372f_beat_shape(
        value,
        anchors=anchors,
    ):
        result.append({
            "beats": [
                _studio_v2372f_normalize_beat(
                    value,
                    anchors=anchors,
                )
            ],
            "support_evidence_ids": [],
        })

    for item in value.values():
        if isinstance(
            item,
            (dict, list),
        ):
            result.extend(
                _studio_v2372f_find_payloads(
                    item,
                    anchors=anchors,
                    depth=depth + 1,
                )
            )

    return result


def _studio_v2372f_payload_score(
    payload: dict,
    *,
    anchors: list[dict],
) -> tuple[
    int,
    int,
    int,
    int,
    int,
]:
    expected = {
        str(anchor.get("id") or "")
        for anchor in anchors or []
        if isinstance(anchor, dict)
        and str(anchor.get("id") or "")
    }

    beats = (
        payload.get("beats")
        if isinstance(payload, dict)
        else []
    )

    if not isinstance(beats, list):
        beats = []

    valid_beats = 0
    used = []
    invalid = 0

    for beat in beats:
        if not isinstance(beat, dict):
            continue

        ids = [
            str(value or "").strip()
            for value in (
                beat.get(
                    "source_evidence_ids"
                )
                or []
            )
            if str(value or "").strip()
        ]

        valid_ids = [
            key
            for key in ids
            if key in expected
        ]

        invalid += (
            len(ids)
            - len(valid_ids)
        )

        if valid_ids:
            valid_beats += 1

        used.extend(valid_ids)

    support = [
        str(value or "").strip()
        for value in (
            payload.get(
                "support_evidence_ids"
            )
            or []
        )
        if str(value or "").strip()
        and str(value or "").strip()
        in expected
    ]

    all_ids = used + support
    unique = set(all_ids)
    duplicates = (
        len(all_ids)
        - len(unique)
    )

    return (
        len(unique),
        valid_beats,
        len(beats),
        -invalid,
        -duplicates,
    )


def _studio_v2372f_raw_payloads(
    raw: object,
    *,
    anchors: list[dict],
) -> list[
    tuple[str, dict]
]:
    candidates = []

    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )

    for index,text in enumerate(
        sorted(
            texts,
            key=len,
            reverse=True,
        ),
        1,
    ):
        cleaned = (
            _studio_v2372d_strip_wrappers(
                text
            )
        )

        parsed = (
            _studio_v2372d_parse_jsonish(
                cleaned
            )
        )

        for payload in (
            _studio_v2372f_find_payloads(
                parsed,
                anchors=anchors,
            )
        ):
            candidates.append((
                f"raw-json-{index}",
                payload,
            ))

        # Recover complete Beat objects from a truncated array/object.
        array_positions = []

        match = _studio_v2372d_re.search(
            r'["\'](?:beats|narrative_beats|beat_list)["\']\s*:\s*\[',
            cleaned,
            flags=_studio_v2372d_re.I,
        )

        if match:
            position = cleaned.find(
                "[",
                match.start(),
            )
            if position >= 0:
                array_positions.append(
                    position
                )

        first_array = cleaned.find("[")
        if first_array >= 0:
            array_positions.append(
                first_array
            )

        for array_position in (
            dict.fromkeys(
                array_positions
            )
        ):
            rows = (
                _studio_v2372d_balanced_objects_from_array(
                    cleaned,
                    array_position,
                    limit=64,
                )
            )

            payloads = (
                _studio_v2372f_find_payloads(
                    rows,
                    anchors=anchors,
                )
            )

            for payload in payloads:
                candidates.append((
                    f"raw-array-recovery-{index}",
                    payload,
                ))

    return candidates


def _studio_v2372f_extract_payload(
    *,
    raw: object,
    parsed: object,
    anchors: list[dict],
) -> tuple[
    dict,
    str,
    list[dict],
]:
    candidates = []

    for payload in (
        _studio_v2372f_find_payloads(
            parsed,
            anchors=anchors,
        )
    ):
        candidates.append((
            "parsed-structure",
            payload,
        ))

    candidates.extend(
        _studio_v2372f_raw_payloads(
            raw,
            anchors=anchors,
        )
    )

    unique = []
    seen = set()

    for origin,payload in candidates:
        canonical = {
            "beats": [
                _studio_v2372f_normalize_beat(
                    row,
                    anchors=anchors,
                )
                for row in (
                    payload.get("beats")
                    or []
                )
                if isinstance(row, dict)
            ],
            "support_evidence_ids":
                list(dict.fromkeys(
                    payload.get(
                        "support_evidence_ids"
                    )
                    or []
                )),
        }

        signature = _studio_json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        if signature in seen:
            continue

        seen.add(signature)
        unique.append((
            origin,
            canonical,
        ))

    if not unique:
        return {}, "not-found", []

    ranked = sorted(
        unique,
        key=lambda item: (
            _studio_v2372f_payload_score(
                item[1],
                anchors=anchors,
            ),
            len(
                _studio_json.dumps(
                    item[1],
                    ensure_ascii=False,
                )
            ),
        ),
        reverse=True,
    )

    diagnostics = [{
        "origin": origin,
        "score":
            _studio_v2372f_payload_score(
                payload,
                anchors=anchors,
            ),
        "beat_count":
            len(payload.get("beats") or []),
        "evidence_ids": sorted({
            str(evidence_id or "")
            for beat in (
                payload.get("beats")
                or []
            )
            for evidence_id in (
                beat.get(
                    "source_evidence_ids"
                )
                or []
            )
            if str(evidence_id or "")
        }),
        "support_count":
            len(
                payload.get(
                    "support_evidence_ids"
                )
                or []
            ),
    } for origin,payload in ranked[:8]]

    return (
        ranked[0][1],
        ranked[0][0],
        diagnostics,
    )


def _studio_v2372f_parse_line_payload(
    raw: object,
    *,
    anchors: list[dict],
) -> dict:
    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )

    best = {}

    for text in sorted(
        texts,
        key=len,
        reverse=True,
    ):
        beats = []
        support = []

        for raw_line in str(text).splitlines():
            line = raw_line.rstrip(
                "\r\n"
            )

            if not line.strip():
                continue

            parts = line.split("\t")
            kind = (
                parts[0].strip().upper()
                if parts
                else ""
            )

            if kind == "BEAT":
                while len(parts) < 6:
                    parts.append("")

                beat = {
                    "summary":
                        parts[1].strip(),
                    "state_change":
                        parts[2].strip(),
                    "source_evidence_ids":
                        [
                            value.strip()
                            for value in parts[3].split(",")
                            if value.strip()
                        ],
                    "character_entity_ids":
                        [
                            value.strip()
                            for value in parts[4].split(",")
                            if value.strip()
                        ],
                    "prop_entity_ids":
                        [
                            value.strip()
                            for value in parts[5].split(",")
                            if value.strip()
                        ],
                }

                beats.append(
                    _studio_v2372f_normalize_beat(
                        beat,
                        anchors=anchors,
                    )
                )

            elif kind == "SUPPORT":
                if len(parts) < 2:
                    continue

                support.extend(
                    _studio_v2372f_support_ids(
                        parts[1],
                        anchors=anchors,
                    )
                )

        payload = {
            "beats": beats,
            "support_evidence_ids":
                list(dict.fromkeys(support)),
        }

        if (
            not best
            or _studio_v2372f_payload_score(
                payload,
                anchors=anchors,
            )
            > _studio_v2372f_payload_score(
                best,
                anchors=anchors,
            )
        ):
            best = payload

    return best


async def _studio_v2372_generate_chunk_beats(
    *,
    chunk: dict,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    anchors = (
        _studio_v2372_chunk_anchors(
            chunk
        )
    )

    if not anchors:
        return [], []

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    diagnostics = []

    attempts = (
        (
            "json-primary",
            0.06,
            2100,
            "",
        ),
        (
            "json-strict",
            0.0,
            2100,
            (
                "\n\nSTRICT_SCHEMA_RETRY："
                "只返回一个 JSON 对象；"
                "顶层只能使用 beats 和 support_evidence_ids；"
                "每个 Beat 必须显式包含 summary、state_change、"
                "source_evidence_ids、character_entity_ids、prop_entity_ids；"
                "source_evidence_ids 只能从 SOURCE_ANCHORS 的 id 选择。"
            ),
        ),
        (
            "line-protocol",
            0.0,
            1800,
            (
                "\n\nSERIALIZATION_FALLBACK：不要输出 JSON。"
                "每个 Beat 严格一行：\n"
                "BEAT<TAB>summary<TAB>state_change"
                "<TAB>source_evidence_ids逗号分隔"
                "<TAB>character_entity_ids逗号分隔"
                "<TAB>prop_entity_ids逗号分隔\n"
                "所有非 Beat 正文锚点用一行：\n"
                "SUPPORT<TAB>source_evidence_ids逗号分隔\n"
                "不得输出其他文字。"
            ),
        ),
    )

    base_system_prompt = (
        "你是小说正文 Narrative Beat 提取器，运行 Qwen3-32B。"
        "只处理 CORE_SOURCE_CHUNK，不把前后 context 当可消费正文。"
        "目标是由模型建立当前 Scene 的最小有序叙事状态图。"
        "每个证据单元是否成为 Beat，只依据它对该状态图的必要性判断："
        "移除后会改变后续状态、因果关系或必要上下文依赖，则归入 Beat；"
        "移除后不改变该状态图，则归入 support_evidence_ids。"
        "不得依据固定关键词、文本类别、题材类型或预设示例进行分类。"
        "Beat summary/state_change 必须被自己的正文证据直接支持。"
        "character_entity_ids / prop_entity_ids 只使用 ALLOWED_ENTITIES "
        "中的真实 ID；不确定留空。"
    )

    base_prompt = (
        f"CHUNK_PROGRESS="
        f"{chunk.get('index')}\n"
        "=== NON_ANCHOR_CONTEXT_BEFORE ===\n"
        + str(
            chunk.get(
                "context_before"
            ) or ""
        )
        + "\n\n=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== NON_ANCHOR_CONTEXT_AFTER ===\n"
        + str(
            chunk.get(
                "context_after"
            ) or ""
        )
        + "\n\n=== SOURCE_ANCHORS ===\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + entity_text
    )

    for (
        attempt_name,
        temperature,
        max_tokens,
        suffix,
    ) in attempts:
        try:
            raw, parsed, _ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "narrative_beat_extraction_qwen32b"
                    ),
                    messages=[{
                        "role": "user",
                        "content":
                            base_prompt + suffix,
                    }],
                    system_prompt=
                        base_system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    contract=(
                        '{"beats":[{'
                        '"summary":"",'
                        '"state_change":"",'
                        '"source_evidence_ids":["C01E001"],'
                        '"character_entity_ids":[],'
                        '"prop_entity_ids":[]'
                        '}],'
                        '"support_evidence_ids":["C01E002"]}'
                    ),
                )
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": qwen_call="
                + type(exc).__name__
                + ": "
                + str(exc)[:500]
            )
            continue

        if attempt_name == "line-protocol":
            payload = (
                _studio_v2372f_parse_line_payload(
                    raw,
                    anchors=anchors,
                )
            )
            origin = "line-protocol"
            candidate_diagnostics = []
        else:
            (
                payload,
                origin,
                candidate_diagnostics,
            ) = (
                _studio_v2372f_extract_payload(
                    raw=raw,
                    parsed=parsed,
                    anchors=anchors,
                )
            )

        if not payload:
            texts = (
                _studio_v2372d_collect_texts(
                    raw
                )
            )

            preview = ""

            if texts:
                preview = (
                    _studio_v2372d_re.sub(
                        r"\s+",
                        " ",
                        max(
                            texts,
                            key=len,
                        ),
                    )[:700]
                )

            diagnostics.append(
                attempt_name
                + ": beat_payload_not_found"
                + " parsed_type="
                + type(parsed).__name__
                + " parsed_keys="
                + repr(
                    sorted(parsed.keys())
                    if isinstance(parsed, dict)
                    else []
                )
                + " raw_preview="
                + repr(preview)
            )
            continue

        try:
            (
                beats,
                support_ids,
                missing_ids,
            ) = (
                _studio_v2372c_validate_partial(
                    payload=payload,
                    anchors=anchors,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except RuntimeError as exc:
            diagnostics.append(
                attempt_name
                + " origin="
                + origin
                + " candidates="
                + repr(
                    candidate_diagnostics
                )[:1000]
                + ": DETERMINISTIC_PARTIAL_ERROR: "
                + str(exc)
            )
            continue

        if missing_ids:
            try:
                beats, support_ids = (
                    await _studio_v2372c_complete_missing(
                        chunk=chunk,
                        anchors=anchors,
                        beats=beats,
                        support_ids=support_ids,
                        missing_ids=missing_ids,
                        allowed_chars=allowed_chars,
                        allowed_props=allowed_props,
                        entity_rows=entity_rows,
                    )
                )
            except RuntimeError as exc:
                diagnostics.append(
                    attempt_name
                    + " origin="
                    + origin
                    + ": COVERAGE_COMPLETION_ERROR: "
                    + str(exc)
                )
                continue

        audit = (
            await _studio_v2372_audit_extraction(
                chunk=chunk,
                anchors=anchors,
                beats=beats,
                support_ids=support_ids,
            )
        )

        if (
            audit.get("valid") is True
            and not (
                audit.get("violations")
                or []
            )
        ):
            return (
                beats,
                support_ids,
            )

        diagnostics.append(
            attempt_name
            + " origin="
            + origin
            + ": FINAL_AUDIT="
            + _studio_json.dumps(
                audit.get("violations")
                or audit,
                ensure_ascii=False,
            )[:1200]
        )

    raise RuntimeError(
        "严格 Stage04：Narrative Beat 输出恢复/覆盖补全/最终审计失败："
        + " | ".join(diagnostics)[-3200:]
    )

# ===== /V2.37.2F STAGE04 BEAT OUTPUT RESILIENCE =====


# ===== V2.37.3 STAGE04 TWO-PHASE NARRATIVE BEATS =====
import copy as _studio_v2373_copy
import re as _studio_v2373_re


def _studio_v2373_anchor_map(
    anchors: list[dict],
) -> dict[str, dict]:
    return {
        str(row.get("id") or ""): row
        for row in (anchors or [])
        if isinstance(row, dict)
        and str(row.get("id") or "")
    }


def _studio_v2373_normalize_id_list(
    value: object,
    *,
    anchors: list[dict],
) -> list[str]:
    amap = _studio_v2373_anchor_map(
        anchors
    )

    extractor = globals().get(
        "_studio_v2372f_structural_ids"
    )

    if extractor is not None:
        candidates = extractor(value)
    else:
        if isinstance(value, list):
            candidates = [
                str(item or "").strip()
                for item in value
            ]
        else:
            candidates = [
                str(value or "").strip()
            ]

    result = []

    for candidate in candidates:
        key = str(candidate or "").strip()

        if (
            key in amap
            and key not in result
        ):
            result.append(key)

    return result


def _studio_v2373_find_classification_plans(
    value: object,
    *,
    anchors: list[dict],
    depth: int = 0,
) -> list[dict]:
    if depth > 8:
        return []

    result = []

    if isinstance(value, dict):
        beat_value = None
        support_value = None

        for key in (
            "beat_ids",
            "narrative_beat_ids",
        ):
            if key in value:
                beat_value = value.get(key)
                break

        for key in (
            "support_evidence_ids",
            "support_ids",
        ):
            if key in value:
                support_value = value.get(key)
                break

        if beat_value is not None:
            beat_ids = (
                _studio_v2373_normalize_id_list(
                    beat_value,
                    anchors=anchors,
                )
            )

            support_ids = (
                _studio_v2373_normalize_id_list(
                    support_value,
                    anchors=anchors,
                )
                if support_value is not None
                else []
            )

            if beat_ids or support_ids:
                result.append({
                    "beat_ids": beat_ids,
                    "support_evidence_ids":
                        support_ids,
                    "character_entity_ids":
                        list(
                            value.get(
                                "character_entity_ids"
                            )
                            or []
                        ),
                    "prop_entity_ids":
                        list(
                            value.get(
                                "prop_entity_ids"
                            )
                            or []
                        ),
                })

        for item in value.values():
            if isinstance(
                item,
                (dict, list),
            ):
                result.extend(
                    _studio_v2373_find_classification_plans(
                        item,
                        anchors=anchors,
                        depth=depth + 1,
                    )
                )

        return result

    if isinstance(value, list):
        for item in value:
            result.extend(
                _studio_v2373_find_classification_plans(
                    item,
                    anchors=anchors,
                    depth=depth + 1,
                )
            )

    return result


def _studio_v2373_plan_score(
    plan: dict,
    *,
    anchors: list[dict],
) -> tuple[
    int,
    int,
    int,
]:
    expected = set(
        _studio_v2373_anchor_map(
            anchors
        )
    )

    beat_ids = list(
        plan.get("beat_ids")
        or []
    )

    support_ids = list(
        plan.get(
            "support_evidence_ids"
        )
        or []
    )

    beat_set = set(beat_ids)
    support_set = set(
        support_ids
    )

    overlap = beat_set.intersection(
        support_set
    )

    accounted = beat_set.union(
        support_set
    )

    return (
        len(accounted & expected),
        -len(overlap),
        -abs(
            len(expected)
            - len(accounted & expected)
        ),
    )


def _studio_v2373_extract_classification_plan(
    *,
    raw: object,
    parsed: object,
    anchors: list[dict],
) -> tuple[
    dict,
    str,
    list[dict],
]:
    candidates = []

    for plan in (
        _studio_v2373_find_classification_plans(
            parsed,
            anchors=anchors,
        )
    ):
        candidates.append((
            "parsed-structure",
            plan,
        ))

    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )

    for index,text in enumerate(
        sorted(
            texts,
            key=len,
            reverse=True,
        ),
        1,
    ):
        cleaned = (
            _studio_v2372d_strip_wrappers(
                text
            )
        )

        parsed_raw = (
            _studio_v2372d_parse_jsonish(
                cleaned
            )
        )

        for plan in (
            _studio_v2373_find_classification_plans(
                parsed_raw,
                anchors=anchors,
            )
        ):
            candidates.append((
                f"raw-json-{index}",
                plan,
            ))

    unique = []
    seen = set()

    for origin,plan in candidates:
        signature = (
            tuple(plan.get("beat_ids") or []),
            tuple(
                plan.get(
                    "support_evidence_ids"
                )
                or []
            ),
        )

        if signature in seen:
            continue

        seen.add(signature)
        unique.append((
            origin,
            plan,
        ))

    if not unique:
        return {}, "not-found", []

    ranked = sorted(
        unique,
        key=lambda item: (
            _studio_v2373_plan_score(
                item[1],
                anchors=anchors,
            ),
            len(
                item[1].get(
                    "beat_ids"
                )
                or []
            ),
        ),
        reverse=True,
    )

    diagnostics = [{
        "origin": origin,
        "beat_count":
            len(plan.get("beat_ids") or []),
        "support_count":
            len(
                plan.get(
                    "support_evidence_ids"
                )
                or []
            ),
        "score":
            _studio_v2373_plan_score(
                plan,
                anchors=anchors,
            ),
    } for origin,plan in ranked[:8]]

    return (
        ranked[0][1],
        ranked[0][0],
        diagnostics,
    )


def _studio_v2373_validate_plan(
    *,
    plan: dict,
    anchors: list[dict],
) -> tuple[
    list[str],
    list[str],
    list[str],
]:
    expected = set(
        _studio_v2373_anchor_map(
            anchors
        )
    )

    beat_ids = [
        key
        for key in (
            plan.get("beat_ids")
            or []
        )
        if key in expected
    ]

    support_ids = [
        key
        for key in (
            plan.get(
                "support_evidence_ids"
            )
            or []
        )
        if key in expected
    ]

    beat_set = set(beat_ids)
    support_set = set(
        support_ids
    )

    overlap = (
        beat_set
        & support_set
    )

    if overlap:
        raise RuntimeError(
            "Anchor classification "
            "把同一正文锚点同时判为 Beat 和 support："
            + repr(sorted(overlap))
        )

    missing = sorted(
        expected
        - beat_set
        - support_set,
        key=lambda key: (
            int(
                _studio_v2373_anchor_map(
                    anchors
                )[key].get(
                    "start"
                )
                or 0
            ),
            key,
        ),
    )

    return (
        beat_ids,
        support_ids,
        missing,
    )


def _studio_v2373_grouping_payload_score(
    payload: dict,
    *,
    beat_ids: list[str],
) -> tuple[
    int,
    int,
    int,
]:
    expected = set(
        beat_ids or []
    )

    rows = (
        payload.get("beats")
        if isinstance(payload, dict)
        else []
    )

    if not isinstance(rows, list):
        rows = []

    consumed = []
    valid_rows = 0

    for row in rows:
        if not isinstance(row, dict):
            continue

        ids = [
            str(value or "").strip()
            for value in (
                row.get(
                    "source_evidence_ids"
                )
                or []
            )
            if str(value or "").strip()
        ]

        valid = [
            key
            for key in ids
            if key in expected
        ]

        if valid:
            valid_rows += 1

        consumed.extend(valid)

    consumed_set = set(
        consumed
    )

    duplicates = (
        len(consumed)
        - len(consumed_set)
    )

    return (
        len(
            consumed_set
            & expected
        ),
        valid_rows,
        -duplicates,
    )


def _studio_v2373_validate_grouping(
    *,
    payload: dict,
    beat_ids: list[str],
    support_ids: list[str],
    anchors: list[dict],
) -> dict:
    expected = set(
        beat_ids or []
    )

    support_set = set(
        support_ids or []
    )

    rows = (
        payload.get("beats")
        if isinstance(payload, dict)
        else None
    )

    if not isinstance(rows, list):
        raise RuntimeError(
            "Beat grouping 没有返回 beats 数组"
        )

    if not rows and expected:
        raise RuntimeError(
            "Beat grouping 返回空 Beats"
        )

    consumed = []
    cleaned = []

    for index,row in enumerate(
        rows,
        1,
    ):
        if not isinstance(row, dict):
            continue

        summary = str(
            row.get("summary")
            or ""
        ).strip()

        state_change = str(
            row.get("state_change")
            or ""
        ).strip()

        ids = [
            str(value or "").strip()
            for value in (
                row.get(
                    "source_evidence_ids"
                )
                or []
            )
            if str(value or "").strip()
        ]

        if not summary:
            raise RuntimeError(
                f"Beat grouping Beat#{index} "
                "缺少 summary"
            )

        if _studio_v2373_re.fullmatch(
            r"(?:C\d{2})?E\d{3}",
            summary,
            flags=_studio_v2373_re.I,
        ):
            raise RuntimeError(
                f"Beat grouping Beat#{index} "
                "summary 回显锚点 ID"
            )

        if not ids:
            raise RuntimeError(
                f"Beat grouping Beat#{index} "
                "没有正文锚点"
            )

        illegal = (
            set(ids) - expected
        )

        if illegal:
            raise RuntimeError(
                f"Beat grouping Beat#{index} "
                "引用非 beat_ids 正文锚点："
                + repr(sorted(illegal))
            )

        if set(ids) & support_set:
            raise RuntimeError(
                f"Beat grouping Beat#{index} "
                "错误消费 support 正文锚点："
                + repr(
                    sorted(
                        set(ids)
                        & support_set
                    )
                )
            )

        consumed.extend(ids)
        cleaned.append(row)

    duplicates = sorted({
        key
        for key in consumed
        if consumed.count(key) > 1
    })

    if duplicates:
        raise RuntimeError(
            "Beat grouping 重复消费正文锚点："
            + repr(duplicates)
        )

    missing = sorted(
        expected - set(consumed)
    )

    if missing:
        raise RuntimeError(
            "Beat grouping 没有覆盖全部 beat_ids："
            + repr(missing)
        )

    return {
        "beats": cleaned,
        "support_evidence_ids":
            list(support_ids or []),
    }


async def _studio_v2373_group_beat_ids(
    *,
    chunk: dict,
    anchors: list[dict],
    beat_ids: list[str],
    support_ids: list[str],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> dict:
    amap = (
        _studio_v2373_anchor_map(
            anchors
        )
    )

    beat_anchors = [
        amap[key]
        for key in beat_ids
        if key in amap
    ]

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    system_prompt = (
        "你是 Narrative Backbone Beat Grouping 模型，运行 Qwen3-32B。"
        "上一步模型已经完成正文锚点的 Beat/support 分类。"
        "你现在只处理 BEAT_ANCHORS；SUPPORT_IDS 绝不能进入任何 Beat。"
        "请把 BEAT_ANCHORS 按当前 Scene 的最小有序叙事状态图分组成若干 Beat。"
        "每个 beat anchor 必须且只能出现一次，禁止跨 Beat 重复消费。"
        "每个 Beat 必须填写 summary、state_change、source_evidence_ids、"
        "character_entity_ids、prop_entity_ids。"
        "summary/state_change 必须只由该 Beat 自己的 source_evidence_ids 直接支持。"
        "character_entity_ids / prop_entity_ids 只使用 ALLOWED_ENTITIES 中真实 ID。"
        "不得依据固定关键词、文本类别、题材类型或预设业务词表分组。"
        "只返回严格 JSON。"
    )

    base_prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== BEAT_ANCHORS ===\n"
        + _studio_json.dumps(
            beat_anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SUPPORT_IDS_DO_NOT_USE ===\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + entity_text
    )

    diagnostics = []
    repair_issue = ""

    attempts = (
        (
            "json-primary",
            0.04,
            1900,
            "",
        ),
        (
            "json-repair",
            0.0,
            1900,
            "",
        ),
        (
            "line-protocol",
            0.0,
            1700,
            (
                "\n\nSERIALIZATION_FALLBACK：不要输出 JSON。"
                "每个 Beat 一行："
                "BEAT<TAB>summary<TAB>state_change"
                "<TAB>source_evidence_ids逗号分隔"
                "<TAB>character_entity_ids逗号分隔"
                "<TAB>prop_entity_ids逗号分隔。"
                "不得输出 SUPPORT 行，因为 support 已由上一步确定。"
            ),
        ),
    )

    for (
        attempt_name,
        temperature,
        max_tokens,
        suffix,
    ) in attempts:
        extra = suffix

        if (
            attempt_name == "json-repair"
            and repair_issue
        ):
            extra += (
                "\n\nPREVIOUS_GROUPING_ERROR："
                + repair_issue
                + "\n重新分组全部 BEAT_ANCHORS；"
                "每个 beat anchor 必须恰好出现一次。"
            )

        try:
            raw, parsed, _ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "narrative_beat_grouping_qwen32b"
                    ),
                    messages=[{
                        "role": "user",
                        "content":
                            base_prompt + extra,
                    }],
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    contract=(
                        '{"beats":[{'
                        '"summary":"",'
                        '"state_change":"",'
                        '"source_evidence_ids":["C01E001"],'
                        '"character_entity_ids":[],'
                        '"prop_entity_ids":[]'
                        '}]}'
                    ),
                )
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": qwen_call="
                + type(exc).__name__
                + ": "
                + str(exc)[:500]
            )
            continue

        if attempt_name == "line-protocol":
            payload = (
                _studio_v2372f_parse_line_payload(
                    raw,
                    anchors=anchors,
                )
            )
            payload[
                "support_evidence_ids"
            ] = []
            origin = "line-protocol"
            candidate_diagnostics = []
        else:
            (
                payload,
                origin,
                candidate_diagnostics,
            ) = (
                _studio_v2372f_extract_payload(
                    raw=raw,
                    parsed=parsed,
                    anchors=anchors,
                )
            )

        if not payload:
            diagnostics.append(
                attempt_name
                + ": grouping_payload_not_found"
            )
            continue

        # Remove any support list the grouping model may invent. The grouping
        # step has no authority to change the classification plan.
        payload[
            "support_evidence_ids"
        ] = []

        try:
            return (
                _studio_v2373_validate_grouping(
                    payload=payload,
                    beat_ids=beat_ids,
                    support_ids=support_ids,
                    anchors=anchors,
                )
            )
        except RuntimeError as exc:
            repair_issue = str(exc)

            diagnostics.append(
                attempt_name
                + " origin="
                + origin
                + " score="
                + repr(
                    _studio_v2373_grouping_payload_score(
                        payload,
                        beat_ids=beat_ids,
                    )
                )
                + " candidates="
                + repr(
                    candidate_diagnostics
                )[:900]
                + ": "
                + repair_issue
            )

    raise RuntimeError(
        "Beat grouping 失败；"
        + " | ".join(diagnostics)[-2600:]
    )


async def _studio_v2373_finalize_candidate_payload(
    *,
    payload: dict,
    chunk: dict,
    anchors: list[dict],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    (
        beats,
        support_ids,
        missing_ids,
    ) = (
        _studio_v2372c_validate_partial(
            payload=payload,
            anchors=anchors,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
            entity_rows=entity_rows,
        )
    )

    if missing_ids:
        (
            beats,
            support_ids,
        ) = (
            await _studio_v2372c_complete_missing(
                chunk=chunk,
                anchors=anchors,
                beats=beats,
                support_ids=support_ids,
                missing_ids=missing_ids,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                entity_rows=entity_rows,
            )
        )

    audit = (
        await _studio_v2372_audit_extraction(
            chunk=chunk,
            anchors=anchors,
            beats=beats,
            support_ids=support_ids,
        )
    )

    if (
        audit.get("valid") is not True
        or (
            audit.get("violations")
            or []
        )
    ):
        raise RuntimeError(
            "FINAL_NARRATIVE_AUDIT="
            + _studio_json.dumps(
                audit.get("violations")
                or audit,
                ensure_ascii=False,
            )[:1600]
        )

    return (
        beats,
        support_ids,
    )


async def _studio_v2372_generate_chunk_beats(
    *,
    chunk: dict,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    anchors = (
        _studio_v2372_chunk_anchors(
            chunk
        )
    )

    if not anchors:
        return [], []

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    system_prompt = (
        "你是小说正文 Narrative Anchor Classifier，运行 Qwen3-32B。"
        "只处理 CORE_SOURCE_CHUNK，不把前后 context 当可消费正文。"
        "第一阶段只做正文锚点分类，不负责把 Beat 分组。"
        "对每个 SOURCE_ANCHOR，依据当前 Scene 的最小有序叙事状态图判断："
        "若该锚点对可重建的状态、因果关系或必要上下文依赖不可省略，"
        "放入 beat_ids；否则放入 support_evidence_ids。"
        "每个 SOURCE_ANCHOR 必须且只能出现在一个列表中。"
        "不得依据固定关键词、文本类别、题材类型或预设业务词表分类。"
        "只返回严格 JSON。"
    )

    prompt = (
        f"CHUNK_PROGRESS="
        f"{chunk.get('index')}\n"
        "=== NON_ANCHOR_CONTEXT_BEFORE ===\n"
        + str(
            chunk.get(
                "context_before"
            ) or ""
        )
        + "\n\n=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== NON_ANCHOR_CONTEXT_AFTER ===\n"
        + str(
            chunk.get(
                "context_after"
            ) or ""
        )
        + "\n\n=== SOURCE_ANCHORS ===\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + entity_text
    )

    diagnostics = []

    attempts = (
        (
            "classification-primary",
            0.04,
            1200,
            "",
        ),
        (
            "classification-strict",
            0.0,
            1200,
            (
                "\n\nSTRICT_SCHEMA_RETRY："
                "顶层只返回 beat_ids、support_evidence_ids、"
                "character_entity_ids、prop_entity_ids。"
                "SOURCE_ANCHORS 中每个 id 必须恰好出现一次。"
            ),
        ),
    )

    for (
        attempt_name,
        temperature,
        max_tokens,
        suffix,
    ) in attempts:
        try:
            raw, parsed, _ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "narrative_anchor_classification_qwen32b"
                    ),
                    messages=[{
                        "role": "user",
                        "content": prompt + suffix,
                    }],
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    contract=(
                        '{"beat_ids":["C01E001"],'
                        '"support_evidence_ids":["C01E002"],'
                        '"character_entity_ids":[],'
                        '"prop_entity_ids":[]}'
                    ),
                )
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": qwen_call="
                + type(exc).__name__
                + ": "
                + str(exc)[:500]
            )
            continue

        (
            plan,
            plan_origin,
            plan_diagnostics,
        ) = (
            _studio_v2373_extract_classification_plan(
                raw=raw,
                parsed=parsed,
                anchors=anchors,
            )
        )

        if plan:
            try:
                (
                    beat_ids,
                    support_ids,
                    missing_ids,
                ) = (
                    _studio_v2373_validate_plan(
                        plan=plan,
                        anchors=anchors,
                    )
                )
            except RuntimeError as exc:
                diagnostics.append(
                    attempt_name
                    + " plan_origin="
                    + plan_origin
                    + ": "
                    + str(exc)
                )
                continue

            if not beat_ids:
                diagnostics.append(
                    attempt_name
                    + ": classification returned zero beat_ids"
                )
                continue

            try:
                grouped_payload = (
                    await _studio_v2373_group_beat_ids(
                        chunk=chunk,
                        anchors=anchors,
                        beat_ids=beat_ids,
                        support_ids=support_ids,
                        allowed_chars=allowed_chars,
                        allowed_props=allowed_props,
                        entity_rows=entity_rows,
                    )
                )

                # If classification missed a few anchors, let the existing
                # model-driven coverage completion classify only those missing
                # anchors after grouping. Nothing is auto-assigned to support.
                if missing_ids:
                    grouped_payload[
                        "support_evidence_ids"
                    ] = support_ids

                return (
                    await _studio_v2373_finalize_candidate_payload(
                        payload=grouped_payload,
                        chunk=chunk,
                        anchors=anchors,
                        allowed_chars=allowed_chars,
                        allowed_props=allowed_props,
                        entity_rows=entity_rows,
                    )
                )

            except RuntimeError as exc:
                diagnostics.append(
                    attempt_name
                    + " plan_origin="
                    + plan_origin
                    + " plan="
                    + repr(
                        plan_diagnostics
                    )[:900]
                    + ": "
                    + str(exc)
                )
                continue

        # Backward-compatible fallback: if the model ignored the phase contract
        # and returned full Beats directly, use the resilient V2.37.2f parser.
        (
            payload,
            origin,
            candidate_diagnostics,
        ) = (
            _studio_v2372f_extract_payload(
                raw=raw,
                parsed=parsed,
                anchors=anchors,
            )
        )

        if payload:
            try:
                return (
                    await _studio_v2373_finalize_candidate_payload(
                        payload=payload,
                        chunk=chunk,
                        anchors=anchors,
                        allowed_chars=allowed_chars,
                        allowed_props=allowed_props,
                        entity_rows=entity_rows,
                    )
                )
            except RuntimeError as exc:
                diagnostics.append(
                    attempt_name
                    + " direct_origin="
                    + origin
                    + " candidates="
                    + repr(
                        candidate_diagnostics
                    )[:900]
                    + ": "
                    + str(exc)
                )
                continue

        diagnostics.append(
            attempt_name
            + ": neither classification plan nor Beat payload found"
        )

    raise RuntimeError(
        "严格 Stage04：两阶段 Narrative Beat "
        "分类/分组/覆盖/审计失败："
        + " | ".join(diagnostics)[-3600:]
    )

# ===== /V2.37.3 STAGE04 TWO-PHASE NARRATIVE BEATS =====


# ===== V2.37.4 STAGE04 BATCHED NARRATIVE PIPELINE =====
import copy as _studio_v2374_copy
import re as _studio_v2374_re


_STUDIO_V2374_CLASSIFY_BATCH_SIZE = 10
_STUDIO_V2374_GROUP_BATCH_SIZE = 10
_STUDIO_V2374_REPAIR_BATCH_SIZE = 5


def _studio_v2374_ordered_anchors(
    anchors: list[dict],
) -> list[dict]:
    return sorted(
        [
            row
            for row in (anchors or [])
            if isinstance(row, dict)
            and str(row.get("id") or "")
        ],
        key=lambda row: (
            int(row.get("start") or 0),
            int(row.get("end") or 0),
            str(row.get("id") or ""),
        ),
    )


def _studio_v2374_anchor_map(
    anchors: list[dict],
) -> dict[str, dict]:
    return {
        str(row.get("id") or ""): row
        for row in _studio_v2374_ordered_anchors(
            anchors
        )
    }


def _studio_v2374_chunks(
    values: list,
    size: int,
) -> list[list]:
    size = max(1, int(size or 1))
    return [
        values[index:index + size]
        for index in range(
            0,
            len(values),
            size,
        )
    ]


def _studio_v2374_bool(
    value: object,
) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    text = str(value).strip().lower()
    return text in {
        "true",
        "1",
        "yes",
        "y",
    }


def _studio_v2374_plan_parts(
    *,
    plan: dict,
    anchors: list[dict],
) -> tuple[
    list[str],
    list[str],
    list[str],
    list[str],
]:
    ordered = _studio_v2374_ordered_anchors(
        anchors
    )
    expected = [
        str(row.get("id") or "")
        for row in ordered
    ]
    expected_set = set(expected)

    beat_ids = []
    for value in (
        plan.get("beat_ids")
        if isinstance(plan, dict)
        else []
    ) or []:
        key = str(value or "").strip()
        if (
            key in expected_set
            and key not in beat_ids
        ):
            beat_ids.append(key)

    support_ids = []
    for value in (
        plan.get("support_evidence_ids")
        if isinstance(plan, dict)
        else []
    ) or []:
        key = str(value or "").strip()
        if (
            key in expected_set
            and key not in support_ids
        ):
            support_ids.append(key)

    overlap = [
        key
        for key in expected
        if (
            key in set(beat_ids)
            and key in set(support_ids)
        )
    ]

    accounted = (
        set(beat_ids)
        | set(support_ids)
    )

    missing = [
        key
        for key in expected
        if key not in accounted
    ]

    # Ambiguous IDs are unresolved; never silently choose Beat or support.
    beat_ids = [
        key
        for key in beat_ids
        if key not in set(overlap)
    ]
    support_ids = [
        key
        for key in support_ids
        if key not in set(overlap)
    ]

    return (
        beat_ids,
        support_ids,
        overlap,
        missing,
    )


def _studio_v2374_classification_line_rows(
    raw: object,
) -> list[dict]:
    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )
    best = []

    for text in sorted(
        texts,
        key=len,
        reverse=True,
    ):
        rows = []

        for raw_line in str(text).splitlines():
            line = raw_line.strip()
            if not line:
                continue

            parts = line.split("\t")
            if (
                len(parts) >= 3
                and parts[0].strip().upper()
                == "CLASSIFY"
            ):
                rows.append({
                    "source_evidence_id":
                        parts[1].strip(),
                    "destination":
                        parts[2].strip().lower(),
                    "target_beat_index": 0,
                    "summary": "",
                    "state_change": "",
                    "character_entity_ids": [],
                    "prop_entity_ids": [],
                })

        if len(rows) > len(best):
            best = rows

    return best


async def _studio_v2374_resolve_classification_ids(
    *,
    chunk: dict,
    requested_ids: list[str],
    anchors: list[dict],
) -> dict[str, str]:
    # V2.39.10.4_STAGE04_PROGRESSIVE_ANCHOR_REPAIR
    #
    # Old behavior retried the ENTIRE repair group whenever even one ID was
    # missing from Qwen output. That caused 5/8-ID groups to be resent several
    # times and created the observed repair-call explosion.
    #
    # New behavior is fail-closed but progressive:
    #   1) accept only individually valid, non-conflicting assignments;
    #   2) remove those IDs from pending;
    #   3) ask Qwen again ONLY for the unresolved IDs;
    #   4) if any ID remains unresolved after JSON/strict/line recovery, fail.
    amap = _studio_v2374_anchor_map(
        anchors
    )

    requested_ids = [
        str(key)
        for key in requested_ids
        if str(key) in amap
    ]

    requested_ids = list(
        dict.fromkeys(
            requested_ids
        )
    )

    if not requested_ids:
        return {}

    # Infrastructure-only batching limit. It never decides narrative meaning.
    # Main classification already handles 40 anchors; 24 compact repair rows
    # keeps the request comfortably bounded while avoiding tiny 5/8-ID loops.
    repair_batch_size = 24

    resolved: dict[str, str] = {}
    qwen_calls = 0
    group_count = 0

    for group in _studio_v2374_chunks(
        requested_ids,
        repair_batch_size,
    ):
        group_count += 1
        pending = list(group)
        diagnostics = []

        attempts = (
            (
                "json",
                "",
                480,
            ),
            (
                "json-strict",
                (
                    "\n\nSTRICT_RETRY："
                    "只处理本次仍未解决的 REQUESTED_ANCHORS；"
                    "只返回 assignments 数组；"
                    "字段名必须为 source_evidence_id 和 destination；"
                    "destination 只能是 beat 或 support；"
                    "每个 requested ID 最多出现一次。"
                ),
                420,
            ),
            (
                "line",
                (
                    "\n\nSERIALIZATION_FALLBACK：不要输出 JSON。"
                    "只处理本次仍未解决的 requested ID。"
                    "每个 requested ID 一行："
                    "CLASSIFY<TAB>source_evidence_id<TAB>beat|support。"
                    "不得输出其他文字。"
                ),
                320,
            ),
        )

        for attempt_name, suffix, max_tokens in attempts:
            if not pending:
                break

            pending_set = set(pending)
            requested_anchors = [
                amap[key]
                for key in pending
            ]

            system_prompt = (
                "你是 Narrative Anchor Classification 修复器。"
                "只处理 REQUESTED_ANCHORS。"
                "对每个锚点只判断 beat 或 support。"
                "判断依据只能来自当前 Scene 的最小有序叙事状态图："
                "若移除该证据会改变可重建的状态、因果关系或必要上下文依赖，则为 beat；"
                "否则为 support。"
                "不得依据固定关键词、文本类别、题材类型或预设业务词表判断。"
                "每个 requested ID 必须且只能返回一次。"
                "只输出要求的结构。"
            )

            prompt = (
                "=== CORE_SOURCE_CHUNK ===\n"
                + str(chunk.get("text") or "")
                + "\n\n=== REQUESTED_ANCHORS ===\n"
                + _studio_json.dumps(
                    requested_anchors,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

            qwen_calls += 1

            try:
                raw, parsed, _ = (
                    await _studio_v2371a_qwen_call(
                        phase=(
                            "studio_stage04_"
                            "anchor_classification_repair_qwen32b"
                        ),
                        messages=[{
                            "role": "user",
                            "content": prompt + suffix,
                        }],
                        system_prompt=system_prompt,
                        temperature=0.0,
                        max_tokens=max_tokens,
                        contract=(
                            '{"assignments":[{'
                            '"source_evidence_id":"C01E001",'
                            '"destination":"beat"'
                            '}]}'
                        ),
                    )
                )
            except Exception as exc:
                diagnostics.append(
                    attempt_name
                    + ": qwen_call="
                    + type(exc).__name__
                    + ": "
                    + str(exc)[:350]
                )
                continue

            if attempt_name == "line":
                rows = (
                    _studio_v2374_classification_line_rows(
                        raw
                    )
                )
            else:
                (
                    rows,
                    _origin,
                    _candidate_diagnostics,
                ) = (
                    _studio_v2372e_extract_assignments(
                        raw=raw,
                        parsed=parsed,
                        requested_ids=pending,
                    )
                )

            by_id: dict[str, str] = {}
            conflicted: set[str] = set()

            for row in rows or []:
                if not isinstance(row, dict):
                    continue

                evidence_id = str(
                    row.get("source_evidence_id")
                    or row.get("id")
                    or ""
                ).strip()

                destination = str(
                    row.get("destination")
                    or row.get("structure_destination")
                    or ""
                ).strip().lower()

                if (
                    evidence_id not in pending_set
                    or destination not in {
                        "beat",
                        "support",
                    }
                ):
                    continue

                previous = by_id.get(
                    evidence_id
                )

                if (
                    previous is not None
                    and previous != destination
                ):
                    conflicted.add(
                        evidence_id
                    )
                    continue

                by_id[evidence_id] = (
                    destination
                )

            for key in conflicted:
                by_id.pop(
                    key,
                    None,
                )

            accepted = [
                key
                for key in pending
                if key in by_id
            ]

            for key in accepted:
                resolved[key] = by_id[key]

            pending = [
                key
                for key in pending
                if key not in by_id
            ]

            diagnostics.append(
                attempt_name
                + ": accepted="
                + str(len(accepted))
                + " pending="
                + str(len(pending))
                + " conflicts="
                + repr(sorted(conflicted))
            )

        if pending:
            raise RuntimeError(
                "Anchor classification progressive repair failed；"
                "unresolved="
                + repr(pending)
                + "；"
                + " | ".join(diagnostics)
            )

    if set(resolved) != set(requested_ids):
        raise RuntimeError(
            "Anchor classification progressive repair coverage mismatch；"
            "missing="
            + repr(
                sorted(
                    set(requested_ids)
                    - set(resolved)
                )
            )
        )

    print(
        "[V2.39.10.4][Stage04][AnchorRepair] "
        f"requested={len(requested_ids)} "
        f"groups={group_count} "
        f"qwen_calls={qwen_calls} "
        f"resolved={len(resolved)}",
        flush=True,
    )

    return resolved



async def _studio_v2374_classify_batch(
    *,
    chunk: dict,
    batch_anchors: list[dict],
) -> tuple[
    list[str],
    list[str],
]:
    perf_started = _studio_asyncio.get_running_loop().time()
    batch_anchors = (
        _studio_v2374_ordered_anchors(
            batch_anchors
        )
    )
    expected = [
        str(row.get("id") or "")
        for row in batch_anchors
    ]

    system_prompt = (
        "你是小说 Narrative Anchor Classifier，运行 Qwen3-32B。"
        "只对 REQUESTED_ANCHORS 分类。"
        "对每个锚点依据当前 Scene 的最小有序叙事状态图判断："
        "若该证据对可重建的状态、因果关系或必要上下文依赖不可省略，"
        "放入 beat_ids；否则放入 support_evidence_ids。"
        "每个 requested ID 必须且只能出现在一个列表中。"
        "不得依据固定关键词、文本类别、题材类型或预设业务词表分类。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== REQUESTED_ANCHORS ===\n"
        + _studio_json.dumps(
            batch_anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    plan = {}

    for attempt in range(2):
        raw, parsed, _ = (
            await _studio_v2371a_qwen_call(
                phase=(
                    "studio_stage04_"
                    "batched_anchor_classification_qwen32b"
                ),
                messages=[{
                    "role": "user",
                    "content": prompt + (
                        ""
                        if attempt == 0
                        else (
                            "\n\nSTRICT_RETRY："
                            "REQUESTED_ANCHORS 中每个 id "
                            "必须恰好出现一次；"
                            "beat_ids 与 support_evidence_ids 必须互斥。"
                        )
                    ),
                }],
                system_prompt=system_prompt,
                temperature=(
                    0.03
                    if attempt == 0
                    else 0.0
                ),
                max_tokens=750,
                contract=(
                    '{"beat_ids":["C01E001"],'
                    '"support_evidence_ids":["C01E002"]}'
                ),
            )
        )

        (
            candidate,
            _origin,
            _diagnostics,
        ) = (
            _studio_v2373_extract_classification_plan(
                raw=raw,
                parsed=parsed,
                anchors=batch_anchors,
            )
        )

        if candidate:
            plan = candidate
            break

    (
        beat_ids,
        support_ids,
        overlap,
        missing,
    ) = (
        _studio_v2374_plan_parts(
            plan=plan,
            anchors=batch_anchors,
        )
    )

    unresolved = list(
        dict.fromkeys(
            overlap + missing
        )
    )

    if unresolved:
        repair = (
            await _studio_v2374_resolve_classification_ids(
                chunk=chunk,
                requested_ids=unresolved,
                anchors=batch_anchors,
            )
        )

        for key in unresolved:
            destination = repair.get(key)

            if destination == "beat":
                if key not in beat_ids:
                    beat_ids.append(key)
            elif destination == "support":
                if key not in support_ids:
                    support_ids.append(key)
            else:
                raise RuntimeError(
                    "Anchor classification repair "
                    "没有给出合法结果："
                    + repr({
                        "id": key,
                        "destination":
                            destination,
                    })
                )

    beat_set = set(beat_ids)
    support_set = set(
        support_ids
    )

    if beat_set & support_set:
        raise RuntimeError(
            "Batched classification repair 后仍有 Beat/support overlap："
            + repr(
                sorted(
                    beat_set
                    & support_set
                )
            )
        )

    accounted = (
        beat_set
        | support_set
    )

    if accounted != set(expected):
        raise RuntimeError(
            "Batched classification repair 后仍未精确覆盖 requested anchors；"
            "missing="
            + repr(
                sorted(
                    set(expected)
                    - accounted
                )
            )
            + " unexpected="
            + repr(
                sorted(
                    accounted
                    - set(expected)
                )
            )
        )

    order = {
        key:index
        for index,key in enumerate(
            expected
        )
    }

    beat_ids = sorted(
        beat_set,
        key=lambda key:order[key],
    )
    support_ids = sorted(
        support_set,
        key=lambda key:order[key],
    )

    perf_observer = globals().get(
        "_studio_v2396_perf_observe"
    )
    if callable(perf_observer):
        perf_observer(
            "anchor_classification_batch",
            _studio_asyncio.get_running_loop().time()
            - perf_started,
            anchor_count=len(batch_anchors),
        )
    return beat_ids,support_ids


async def _studio_v2374_classify_all(
    *,
    chunk: dict,
    anchors: list[dict],
) -> tuple[
    list[str],
    list[str],
]:
    ordered = (
        _studio_v2374_ordered_anchors(
            anchors
        )
    )

    beat_ids = []
    support_ids = []

    batches = (
        _studio_v2374_chunks(
            ordered,
            _STUDIO_V2374_CLASSIFY_BATCH_SIZE,
        )
    )

    for batch_index,batch in enumerate(
        batches,
        1,
    ):
        try:
            batch_beats,batch_support = (
                await _studio_v2374_classify_batch(
                    chunk=chunk,
                    batch_anchors=batch,
                )
            )
        except Exception as exc:
            raise RuntimeError(
                "Anchor classification batch "
                + str(batch_index)
                + "/"
                + str(len(batches))
                + " failed："
                + str(exc)
            ) from exc

        beat_ids.extend(
            batch_beats
        )
        support_ids.extend(
            batch_support
        )

    expected = {
        str(row.get("id") or "")
        for row in ordered
    }

    beat_set = set(beat_ids)
    support_set = set(
        support_ids
    )

    overlap = (
        beat_set
        & support_set
    )

    if overlap:
        raise RuntimeError(
            "Batched classification 全局合并后出现 overlap："
            + repr(sorted(overlap))
        )

    accounted = (
        beat_set
        | support_set
    )

    if accounted != expected:
        raise RuntimeError(
            "Batched classification 全局合并后覆盖不完整；"
            "missing="
            + repr(
                sorted(
                    expected-accounted
                )
            )
        )

    order = {
        str(row.get("id") or ""):index
        for index,row in enumerate(
            ordered
        )
    }

    return (
        sorted(
            beat_set,
            key=lambda key:order[key],
        ),
        sorted(
            support_set,
            key=lambda key:order[key],
        ),
    )


def _studio_v2374_group_rows(
    payload: dict,
    *,
    anchors: list[dict],
) -> list[dict]:
    rows = (
        payload.get("beats")
        if isinstance(payload, dict)
        else []
    )

    if not isinstance(rows,list):
        rows = []

    normalized = []

    for row in rows:
        if not isinstance(row,dict):
            continue

        item = (
            _studio_v2372f_normalize_beat(
                row,
                anchors=anchors,
            )
        )

        ids = list(
            item.get(
                "source_evidence_ids"
            )
            or []
        )

        # Empty Beat rows are serialization debris, not semantic evidence.
        if not ids:
            continue

        item[
            "merge_with_previous"
        ] = _studio_v2374_bool(
            row.get(
                "merge_with_previous"
            )
        )

        normalized.append(item)

    return normalized


def _studio_v2374_group_membership(
    rows: list[dict],
    *,
    requested_ids: list[str],
) -> tuple[
    dict[str,list[int]],
    list[str],
    list[str],
]:
    requested = set(
        requested_ids
    )

    membership = {
        key:[]
        for key in requested_ids
    }

    illegal = []

    for index,row in enumerate(
        rows,
        1,
    ):
        for evidence_id in (
            row.get(
                "source_evidence_ids"
            )
            or []
        ):
            key = str(
                evidence_id or ""
            ).strip()

            if key in requested:
                membership[key].append(
                    index
                )
            else:
                illegal.append(key)

    conflicts = [
        key
        for key,owners in (
            membership.items()
        )
        if len(owners) > 1
    ]

    missing = [
        key
        for key,owners in (
            membership.items()
        )
        if not owners
    ]

    return (
        membership,
        conflicts,
        missing,
    )


def _studio_v2374_group_repair_line_rows(
    raw: object,
) -> list[dict]:
    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )

    best = []

    for text in sorted(
        texts,
        key=len,
        reverse=True,
    ):
        rows = []

        for raw_line in str(text).splitlines():
            line = raw_line.rstrip(
                "\r\n"
            )

            if not line.strip():
                continue

            parts = line.split("\t")

            if (
                not parts
                or parts[0].strip().upper()
                != "ASSIGN"
            ):
                continue

            while len(parts) < 6:
                parts.append("")

            try:
                target = int(
                    parts[3].strip()
                    or 0
                )
            except Exception:
                target = 0

            rows.append({
                "source_evidence_id":
                    parts[1].strip(),
                "destination":
                    parts[2].strip().lower(),
                "target_beat_index":
                    target,
                "summary":
                    parts[4].strip(),
                "state_change":
                    parts[5].strip(),
                "character_entity_ids": [],
                "prop_entity_ids": [],
            })

        if len(rows) > len(best):
            best = rows

    return best


async def _studio_v2374_resolve_group_membership(
    *,
    chunk: dict,
    batch_anchors: list[dict],
    rows: list[dict],
    requested_ids: list[str],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> list[dict]:
    """V2.39.10.8_R2_PROGRESSIVE_BEAT_MEMBERSHIP_REPAIR

    Preserve every already-valid Beat grouping decision. Only unresolved
    membership IDs are sent back to Qwen. Partial valid assignments are
    accepted immediately; later rounds contain pending IDs only.

    No business keyword rules. No deterministic semantic guessing.
    """
    amap = _studio_v2374_anchor_map(batch_anchors)

    raw_requested = []
    for value in requested_ids or []:
        key = str(value or "").strip()
        if key and key not in raw_requested:
            raw_requested.append(key)

    unknown_requested = [
        key for key in raw_requested
        if key not in amap
    ]
    if unknown_requested:
        raise RuntimeError(
            "V2.39.10.8-r2: membership repair requested "
            "当前 batch 不存在的 anchor；ids="
            + repr(unknown_requested)
        )

    ordered_requested = list(raw_requested)

    if not ordered_requested:
        return _studio_v2374_copy.deepcopy(rows)

    working = _studio_v2374_copy.deepcopy(rows)

    # Unresolved IDs have no trusted owner yet. Remove only those IDs once;
    # all resolved membership and Beat semantics remain frozen.
    unresolved_set = set(ordered_requested)
    for row in working:
        row["source_evidence_ids"] = [
            str(key or "").strip()
            for key in (row.get("source_evidence_ids") or [])
            if str(key or "").strip()
            and str(key or "").strip() not in unresolved_set
        ]

    working = [
        row
        for row in working
        if isinstance(row, dict)
        and (row.get("source_evidence_ids") or [])
    ]

    batch_order_context = [
        {
            "id": str(anchor.get("id") or ""),
            "text": str(anchor.get("text") or ""),
        }
        for anchor in _studio_v2374_ordered_anchors(batch_anchors)
        if str(anchor.get("id") or "")
    ]

    def compact_beats():
        return [
            {
                "beat_index": index + 1,
                "summary": str(row.get("summary") or "")[:360],
                "state_change": str(row.get("state_change") or "")[:300],
                "source_evidence_ids": list(
                    row.get("source_evidence_ids") or []
                ),
            }
            for index, row in enumerate(working)
            if isinstance(row, dict)
        ]

    def apply_assignment(evidence_id: str, assignment: dict) -> tuple[bool, str]:
        destination = str(
            assignment.get("destination") or ""
        ).strip().lower()

        if destination == "existing_beat":
            try:
                target = int(
                    assignment.get("target_beat_index") or 0
                )
            except Exception:
                target = 0

            if not (1 <= target <= len(working)):
                return False, "existing_target_out_of_range"

            target_row = working[target - 1]
            ids = list(target_row.get("source_evidence_ids") or [])
            if evidence_id not in ids:
                ids.append(evidence_id)
            target_row["source_evidence_ids"] = ids
            return True, ""

        if destination == "new_beat":
            summary = str(assignment.get("summary") or "").strip()
            state_change = str(
                assignment.get("state_change") or ""
            ).strip()

            if not summary or not state_change:
                return False, "new_beat_semantics_incomplete"

            working.append({
                "summary": summary,
                "state_change": state_change,
                "source_evidence_ids": [evidence_id],
                "character_entity_ids": list(
                    assignment.get("character_entity_ids") or []
                ),
                "prop_entity_ids": list(
                    assignment.get("prop_entity_ids") or []
                ),
                "merge_with_previous": False,
            })
            return True, ""

        return False, "invalid_destination"

    async def ask_membership(
        pending: list[str],
        *,
        mode: str,
        max_tokens: int,
    ) -> tuple[list[dict], str]:
        requested_anchors = [
            amap[key]
            for key in pending
            if key in amap
        ]

        system_prompt = (
            "你是 Narrative Beat membership 修复器，运行 Qwen3-32B。"
            "REQUESTED_BEAT_ANCHORS 都已经确定为 Beat，不是 support。"
            "只决定每个 requested anchor 应归入哪个 CURRENT_BEATS，"
            "或形成一个 new_beat。"
            "destination 只能是 existing_beat 或 new_beat。"
            "existing_beat 必须给当前合法 target_beat_index。"
            "new_beat 必须给正文直接支持的 summary 和 state_change。"
            "只处理 REQUESTED_BEAT_ANCHORS，不得重新分组已经冻结的 evidence。"
            "不得依据固定关键词、文本类别、题材类型或预设业务词表判断。"
            "不得猜测缺失语义。"
        )

        prompt = (
            "=== CURRENT_BEATS_FROZEN ===\n"
            + _studio_json.dumps(
                compact_beats(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== BATCH_ANCHOR_ORDER_CONTEXT ===\n"
            + _studio_json.dumps(
                batch_order_context,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== REQUESTED_BEAT_ANCHORS ===\n"
            + _studio_json.dumps(
                requested_anchors,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        if mode == "strict":
            prompt += (
                "\n\nSTRICT_RETRY：只返回仍在 REQUESTED_BEAT_ANCHORS "
                "中的 assignment；每个 ID 最多一次。"
                "不要重复 CURRENT_BEATS 已冻结 evidence。"
            )
        elif mode == "line":
            prompt += (
                "\n\nSERIALIZATION_FALLBACK：不要输出 JSON。"
                "每个 requested ID 一行："
                "ASSIGN<TAB>source_evidence_id"
                "<TAB>existing_beat|new_beat"
                "<TAB>target_beat_index"
                "<TAB>summary<TAB>state_change。"
                "不得输出其他文字。"
            )

        raw, parsed, _ = await _studio_v2371a_qwen_call(
            phase="studio_stage04_beat_membership_repair_qwen32b",
            messages=[{
                "role": "user",
                "content": prompt,
            }],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=max_tokens,
            contract=(
                '{"assignments":[{'
                '"source_evidence_id":"C01E001",'
                '"destination":"existing_beat",'
                '"target_beat_index":1,'
                '"summary":"",'
                '"state_change":""'
                '}]}'
            ),
        )

        if mode == "line":
            candidate_rows = _studio_v2374_group_repair_line_rows(raw)
        else:
            (
                candidate_rows,
                _origin,
                _candidate_diagnostics,
            ) = _studio_v2372e_extract_assignments(
                raw=raw,
                parsed=parsed,
                requested_ids=pending,
            )

        return list(candidate_rows or []), str(raw or "")[:700]

    total_qwen_calls = 0
    repaired_total = 0
    repair_groups = _studio_v2374_chunks(
        ordered_requested,
        8,
    )

    for group_index, repair_group in enumerate(repair_groups, 1):
        pending = list(repair_group)

        # Progressive rounds: whatever is valid is committed immediately;
        # the next request contains unresolved IDs only.
        round_modes = (
            ("json", 450),
            ("strict", 450),
            ("line", 360),
        )

        for round_index, (mode, max_tokens) in enumerate(round_modes, 1):
            if not pending:
                break

            before = list(pending)
            diagnostics = []
            total_qwen_calls += 1

            try:
                candidate_rows, raw_preview = await ask_membership(
                    pending,
                    mode=mode,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                print(
                    "[V2.39.10.8-r2][Stage04][MembershipRepair] "
                    f"group={group_index}/{len(repair_groups)} "
                    f"round={round_index} mode={mode} "
                    f"requested={len(before)} accepted=0 "
                    f"pending={len(pending)} "
                    f"call_error={type(exc).__name__}:{str(exc)[:240]}",
                    flush=True,
                )
                continue

            candidates_by_id: dict[str, list[dict]] = {}
            pending_set = set(pending)

            for candidate in candidate_rows:
                if not isinstance(candidate, dict):
                    continue
                evidence_id = str(
                    candidate.get("source_evidence_id") or ""
                ).strip()
                if evidence_id not in pending_set:
                    continue
                candidates_by_id.setdefault(
                    evidence_id,
                    [],
                ).append(candidate)

            accepted_now = []

            for evidence_id in list(pending):
                options = candidates_by_id.get(evidence_id) or []
                if not options:
                    diagnostics.append(evidence_id + ":missing")
                    continue

                # Conflicting duplicate assignments are unresolved, not guessed.
                signatures = {
                    (
                        str(opt.get("destination") or "").strip().lower(),
                        int(opt.get("target_beat_index") or 0)
                        if str(opt.get("target_beat_index") or "").strip().lstrip("-").isdigit()
                        else 0,
                        str(opt.get("summary") or "").strip(),
                        str(opt.get("state_change") or "").strip(),
                    )
                    for opt in options
                }

                if len(signatures) != 1:
                    diagnostics.append(evidence_id + ":conflict")
                    continue

                ok, reason = apply_assignment(
                    evidence_id,
                    options[0],
                )

                if ok:
                    accepted_now.append(evidence_id)
                else:
                    diagnostics.append(
                        evidence_id + ":" + reason
                    )

            if accepted_now:
                accepted_set = set(accepted_now)
                pending = [
                    key
                    for key in pending
                    if key not in accepted_set
                ]
                repaired_total += len(accepted_now)

            print(
                "[V2.39.10.8-r2][Stage04][MembershipRepair] "
                f"group={group_index}/{len(repair_groups)} "
                f"round={round_index} mode={mode} "
                f"requested={len(before)} "
                f"accepted={len(accepted_now)} "
                f"pending={len(pending)} "
                f"diagnostics={diagnostics[:5]}",
                flush=True,
            )

        # Last-resort model-driven singleton assignment. This is bounded to
        # unresolved IDs only; never re-runs grouping for the whole batch.
        for evidence_id in list(pending):
            singleton_done = False

            for singleton_attempt, mode in enumerate(("json", "strict"), 1):
                total_qwen_calls += 1

                try:
                    candidate_rows, raw_preview = await ask_membership(
                        [evidence_id],
                        mode=mode,
                        max_tokens=320,
                    )
                except Exception as exc:
                    print(
                        "[V2.39.10.8-r2][Stage04][MembershipSingleton] "
                        f"id={evidence_id} attempt={singleton_attempt} "
                        f"call_error={type(exc).__name__}:{str(exc)[:220]}",
                        flush=True,
                    )
                    continue

                valid = []
                for candidate in candidate_rows:
                    if not isinstance(candidate, dict):
                        continue
                    if str(
                        candidate.get("source_evidence_id") or ""
                    ).strip() != evidence_id:
                        continue
                    ok, reason = apply_assignment(
                        evidence_id,
                        candidate,
                    )
                    if ok:
                        valid.append(candidate)
                        break

                if valid:
                    singleton_done = True
                    repaired_total += 1
                    print(
                        "[V2.39.10.8-r2][Stage04][MembershipSingleton] "
                        f"id={evidence_id} attempt={singleton_attempt} "
                        "accepted=1",
                        flush=True,
                    )
                    break

            if not singleton_done:
                raise RuntimeError(
                    "V2.39.10.8-r2: Beat membership progressive repair "
                    "仍无法解析单个 unresolved anchor；id="
                    + evidence_id
                )

            pending.remove(evidence_id)

    working = [
        row
        for row in working
        if isinstance(row, dict)
        and (row.get("source_evidence_ids") or [])
    ]

    requested_set = set(ordered_requested)
    requested_counts = {
        key: 0
        for key in ordered_requested
    }

    for row in working:
        if not isinstance(row, dict):
            continue
        for value in row.get("source_evidence_ids") or []:
            key = str(value or "").strip()
            if key in requested_set:
                requested_counts[key] += 1

    conflicts = sorted(
        key
        for key, count in requested_counts.items()
        if count > 1
    )
    missing = sorted(
        key
        for key, count in requested_counts.items()
        if count == 0
    )

    if conflicts or missing:
        raise RuntimeError(
            "V2.39.10.8-r2: progressive membership "
            "requested-ID exactly-once failed；"
            f"conflicts={conflicts} missing={missing}"
        )

    print(
        "[V2.39.10.8-r2][Stage04][MembershipRepair] "
        f"requested={len(ordered_requested)} "
        f"groups={len(repair_groups)} "
        f"qwen_calls={total_qwen_calls} "
        f"resolved={repaired_total}",
        flush=True,
    )

    return working

def _studio_v2374_sort_group_rows(
    rows: list[dict],
    *,
    anchors: list[dict],
) -> list[dict]:
    amap = (
        _studio_v2374_anchor_map(
            anchors
        )
    )

    def first_start(row):
        starts = [
            int(
                amap[key].get("start")
                or 0
            )
            for key in (
                row.get(
                    "source_evidence_ids"
                )
                or []
            )
            if key in amap
        ]

        return (
            min(starts)
            if starts
            else 10**18
        )

    return sorted(
        rows,
        key=first_start,
    )


async def _studio_v2374_group_batch(
    *,
    chunk: dict,
    batch_anchors: list[dict],
    batch_ids: list[str],
    previous_beat: dict | None,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> list[dict]:
    batch_anchors = (
        _studio_v2374_ordered_anchors(
            batch_anchors
        )
    )

    previous_context = (
        {
            "summary":
                previous_beat.get(
                    "summary"
                ),
            "state_change":
                previous_beat.get(
                    "state_change"
                ),
            "source_evidence_ids":
                previous_beat.get(
                    "source_evidence_ids"
                ),
            "source_evidence":
                previous_beat.get(
                    "source_evidence"
                ),
        }
        if isinstance(
            previous_beat,
            dict,
        )
        else None
    )

    entity_text = _studio_v2371_cut(
        _studio_json.dumps(
            entity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        900,
    )

    system_prompt = (
        "你是 Narrative Beat Grouping 模型，运行 Qwen3-32B。"
        "本次只处理 CURRENT_BEAT_ANCHORS。"
        "这些锚点已经确定属于 Narrative Beat，不能改判为 support。"
        "把它们按当前 Scene 的最小有序叙事状态图分组成若干 Beat。"
        "每个 current anchor 必须且只能属于一个 Beat。"
        "不得遗漏、不得跨 Beat 重复。"
        "每个 Beat 必须填写 summary、state_change、source_evidence_ids、"
        "character_entity_ids、prop_entity_ids。"
        "summary/state_change 只能描述该 Beat 自己 source_evidence_ids "
        "直接支持的事件或状态；不得从 CORE_SOURCE_CHUNK 借用未被该 Beat "
        "选择的锚点语义。Beat 内 source_evidence_ids 必须按锚点 source offset "
        "递增。Beat 重组时 summary/state_change/source_evidence_ids 必须作为闭包迁移。"
        "如果且仅如果第一组 current anchors 与 PREVIOUS_FINAL_BEAT "
        "属于同一个不可分割的叙事状态单元，第一条 Beat 设置 "
        "merge_with_previous=true；此时第一条 summary/state_change "
        "必须描述合并后的完整状态。"
        "除第一条 Beat 外，merge_with_previous 必须为 false。"
        "CURRENT_BEAT_ANCHORS 的 source_evidence_ids 不能引用前一批 ID。"
        "不得依据固定关键词、文本类别、题材类型或预设业务词表分组。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\n"
        + str(chunk.get("text") or "")
        + "\n\n=== PREVIOUS_FINAL_BEAT ===\n"
        + _studio_json.dumps(
            previous_context,
            ensure_ascii=False,
        )
        + "\n\n=== CURRENT_BEAT_ANCHORS ===\n"
        + _studio_json.dumps(
            batch_anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + entity_text
    )

    diagnostics = []
    payload = {}

    attempts = (
        (
            "json",
            "",
            1550,
        ),
        (
            "json-repair",
            (
                "\n\nSTRICT_RETRY："
                "CURRENT_BEAT_ANCHORS 中每个 id 必须恰好出现一次；"
                "禁止重复 source_evidence_ids；"
                "禁止返回空证据 Beat。"
            ),
            1550,
        ),
        (
            "line",
            (
                "\n\nSERIALIZATION_FALLBACK：不要输出 JSON。"
                "每个 Beat 一行："
                "BEAT<TAB>summary<TAB>state_change"
                "<TAB>source_evidence_ids逗号分隔"
                "<TAB>character_entity_ids逗号分隔"
                "<TAB>prop_entity_ids逗号分隔。"
                "行协议不表达 merge_with_previous；"
                "系统会保持批次边界独立并交最终 Narrative audit。"
            ),
            1350,
        ),
    )

    for attempt_name,suffix,max_tokens in attempts:
        try:
            raw,parsed,_ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "batched_beat_grouping_qwen32b"
                    ),
                    messages=[{
                        "role":"user",
                        "content":
                            prompt + suffix,
                    }],
                    system_prompt=
                        system_prompt,
                    temperature=(
                        0.03
                        if attempt_name
                        == "json"
                        else 0.0
                    ),
                    max_tokens=max_tokens,
                    contract=(
                        '{"beats":[{'
                        '"summary":"",'
                        '"state_change":"",'
                        '"source_evidence_ids":["C01E001"],'
                        '"character_entity_ids":[],'
                        '"prop_entity_ids":[],'
                        '"merge_with_previous":false'
                        '}]}'
                    ),
                )
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": qwen_call="
                + type(exc).__name__
                + ": "
                + str(exc)[:400]
            )
            continue

        if attempt_name == "line":
            candidate = (
                _studio_v2372f_parse_line_payload(
                    raw,
                    anchors=batch_anchors,
                )
            )
        else:
            (
                candidate,
                _origin,
                _candidate_diagnostics,
            ) = (
                _studio_v2372f_extract_payload(
                    raw=raw,
                    parsed=parsed,
                    anchors=batch_anchors,
                )
            )

        rows = (
            _studio_v2374_group_rows(
                candidate,
                anchors=batch_anchors,
            )
        )

        if rows:
            payload = {
                "beats":rows
            }
            break

        diagnostics.append(
            attempt_name
            + ": grouping rows empty"
        )

    if not payload:
        raise RuntimeError(
            "Batched Beat grouping 没有可恢复输出；"
            + " | ".join(diagnostics)
        )

    rows = list(
        payload.get("beats")
        or []
    )

    (
        _membership,
        conflicts,
        missing,
    ) = (
        _studio_v2374_group_membership(
            rows,
            requested_ids=batch_ids,
        )
    )

    unresolved = list(
        dict.fromkeys(
            conflicts + missing
        )
    )

    if unresolved:
        rows = (
            await _studio_v2374_resolve_group_membership(
                chunk=chunk,
                batch_anchors=batch_anchors,
                rows=rows,
                requested_ids=unresolved,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                entity_rows=entity_rows,
            )
        )

    rows = (
        _studio_v2374_sort_group_rows(
            rows,
            anchors=batch_anchors,
        )
    )

    # Only the first Beat may request a cross-batch merge.
    for index,row in enumerate(rows):
        if (
            index > 0
            and row.get(
                "merge_with_previous"
            )
        ):
            raise RuntimeError(
                "Batched grouping 非首 Beat "
                "错误设置 merge_with_previous=true"
            )

    if (
        rows
        and rows[0].get(
            "merge_with_previous"
        )
        and previous_beat is None
    ):
        raise RuntimeError(
            "首批 Beat 不允许 merge_with_previous=true"
        )

    # Final deterministic exact-cardinality validation after targeted repair.
    validation_payload = {
        "beats":rows
    }

    _studio_v2373_validate_grouping(
        payload=validation_payload,
        beat_ids=batch_ids,
        support_ids=[],
        anchors=batch_anchors,
    )

    return rows


def _studio_v2374_merge_boundary(
    *,
    accumulated: list[dict],
    current_rows: list[dict],
) -> list[dict]:
    if not current_rows:
        return accumulated

    current_rows = (
        _studio_v2374_copy.deepcopy(
            current_rows
        )
    )

    if (
        accumulated
        and current_rows[0].get(
            "merge_with_previous"
        )
    ):
        previous = accumulated[-1]
        first = current_rows.pop(0)

        previous_ids = list(
            previous.get(
                "source_evidence_ids"
            )
            or []
        )

        for key in (
            first.get(
                "source_evidence_ids"
            )
            or []
        ):
            if key not in previous_ids:
                previous_ids.append(key)

        previous[
            "source_evidence_ids"
        ] = previous_ids

        # The current first Beat was explicitly instructed to describe the
        # merged boundary state when merge_with_previous=true.
        previous["summary"] = str(
            first.get("summary")
            or previous.get("summary")
            or ""
        ).strip()

        previous["state_change"] = str(
            first.get(
                "state_change"
            )
            or previous.get(
                "state_change"
            )
            or ""
        ).strip()

        for field in (
            "character_entity_ids",
            "prop_entity_ids",
        ):
            merged = list(
                previous.get(field)
                or []
            )

            for value in (
                first.get(field)
                or []
            ):
                if value not in merged:
                    merged.append(value)

            previous[field] = merged

    for row in current_rows:
        row[
            "merge_with_previous"
        ] = False
        accumulated.append(row)

    return accumulated


async def _studio_v2374_group_all(
    *,
    chunk: dict,
    anchors: list[dict],
    beat_ids: list[str],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> list[dict]:
    amap = (
        _studio_v2374_anchor_map(
            anchors
        )
    )

    ordered_ids = [
        str(row.get("id") or "")
        for row in (
            _studio_v2374_ordered_anchors(
                anchors
            )
        )
        if str(row.get("id") or "")
        in set(beat_ids)
    ]

    batches = (
        _studio_v2374_chunks(
            ordered_ids,
            _STUDIO_V2374_GROUP_BATCH_SIZE,
        )
    )

    accumulated = []

    for batch_index,batch_ids in enumerate(
        batches,
        1,
    ):
        batch_anchors = [
            amap[key]
            for key in batch_ids
        ]

        previous = (
            accumulated[-1]
            if accumulated
            else None
        )

        try:
            rows = (
                await _studio_v2374_group_batch(
                    chunk=chunk,
                    batch_anchors=batch_anchors,
                    batch_ids=batch_ids,
                    previous_beat=previous,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except Exception as exc:
            raise RuntimeError(
                "Beat grouping batch "
                + str(batch_index)
                + "/"
                + str(len(batches))
                + " failed："
                + str(exc)
            ) from exc

        accumulated = (
            _studio_v2374_merge_boundary(
                accumulated=accumulated,
                current_rows=rows,
            )
        )

    return accumulated


async def _studio_v2372_generate_chunk_beats(
    *,
    chunk: dict,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> tuple[
    list[dict],
    list[str],
]:
    anchor_started = (
        _studio_asyncio.get_running_loop().time()
    )
    anchors = (
        _studio_v2372_chunk_anchors(
            chunk
        )
    )
    perf_observer = globals().get(
        "_studio_v2396_perf_observe"
    )
    if callable(perf_observer):
        perf_observer(
            "anchor_extraction",
            _studio_asyncio.get_running_loop().time()
            - anchor_started,
            anchor_count=len(anchors),
            chunk_index=int(
                chunk.get("index") or 0
            ),
        )

    if not anchors:
        return [], []

    beat_ids,support_ids = (
        await _studio_v2374_classify_all(
            chunk=chunk,
            anchors=anchors,
        )
    )

    if not beat_ids:
        raise RuntimeError(
            "Batched Narrative classification "
            "没有产生任何 beat_ids"
        )

    grouped_rows = (
        await _studio_v2374_group_all(
            chunk=chunk,
            anchors=anchors,
            beat_ids=beat_ids,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
            entity_rows=entity_rows,
        )
    )

    payload = {
        "beats":grouped_rows,
        "support_evidence_ids":
            support_ids,
    }

    (
        beats,
        validated_support,
        missing_ids,
    ) = (
        _studio_v2372c_validate_partial(
            payload=payload,
            anchors=anchors,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
            entity_rows=entity_rows,
        )
    )

    # This should normally be empty because classification and grouping both
    # enforce exact coverage. If not, retain the existing model-driven targeted
    # completion rather than auto-assigning semantics.
    if missing_ids:
        (
            beats,
            validated_support,
        ) = (
            await _studio_v2372c_complete_missing(
                chunk=chunk,
                anchors=anchors,
                beats=beats,
                support_ids=validated_support,
                missing_ids=missing_ids,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                entity_rows=entity_rows,
            )
        )

    audit = (
        await _studio_v2372_audit_extraction(
            chunk=chunk,
            anchors=anchors,
            beats=beats,
            support_ids=
                validated_support,
        )
    )

    if (
        audit.get("valid") is not True
        or (
            audit.get("violations")
            or []
        )
    ):
        raise RuntimeError(
            "FINAL_NARRATIVE_AUDIT="
            + _studio_json.dumps(
                audit.get("violations")
                or audit,
                ensure_ascii=False,
            )[:2200]
        )

    return beats,validated_support

# ===== /V2.37.4 STAGE04 BATCHED NARRATIVE PIPELINE =====


# ===== V2.37.5 STAGE04 ADAPTIVE GROUPING RECOVERY =====
import copy as _studio_v2375_copy
import re as _studio_v2375_re


_V2375_PREVIOUS_GROUP_BATCH = (
    _studio_v2374_group_batch
)


def _studio_v2375_raw_preview(
    raw: object,
    *,
    limit: int = 500,
) -> str:
    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )

    if not texts:
        return ""

    text = max(
        texts,
        key=len,
    )

    return _studio_v2375_re.sub(
        r"\s+",
        " ",
        str(text),
    )[:limit]


def _studio_v2375_singleton_semantics(
    *,
    raw: object,
    parsed: object,
) -> dict:
    """
    Extract only serialization fields. The evidence ID is NOT taken from model
    output; caller binds the known singleton anchor deterministically.
    """
    candidates = []

    def walk(value, depth=0):
        if depth > 8:
            return

        if isinstance(value, dict):
            summary = str(
                value.get("summary")
                or value.get("beat_summary")
                or value.get("description")
                or ""
            ).strip()

            state_change = str(
                value.get("state_change")
                or value.get("state_delta")
                or value.get("change")
                or ""
            ).strip()

            if summary and state_change:
                candidates.append({
                    "summary": summary,
                    "state_change":
                        state_change,
                    "merge_with_previous":
                        _studio_v2374_bool(
                            value.get(
                                "merge_with_previous"
                            )
                        ),
                    "character_entity_ids":
                        list(
                            value.get(
                                "character_entity_ids"
                            )
                            or []
                        ),
                    "prop_entity_ids":
                        list(
                            value.get(
                                "prop_entity_ids"
                            )
                            or []
                        ),
                })

            for item in value.values():
                if isinstance(
                    item,
                    (dict, list),
                ):
                    walk(
                        item,
                        depth + 1,
                    )

        elif isinstance(value, list):
            for item in value:
                walk(
                    item,
                    depth + 1,
                )

    walk(parsed)

    texts = (
        _studio_v2372d_collect_texts(
            raw
        )
    )

    for text in sorted(
        texts,
        key=len,
        reverse=True,
    ):
        cleaned = (
            _studio_v2372d_strip_wrappers(
                text
            )
        )

        parsed_raw = (
            _studio_v2372d_parse_jsonish(
                cleaned
            )
        )

        walk(parsed_raw)

        summary = ""
        state_change = ""
        merge = False

        for raw_line in str(
            cleaned
        ).splitlines():
            line = raw_line.rstrip(
                "\r\n"
            )

            if not line.strip():
                continue

            parts = line.split(
                "\t",
                1,
            )

            if len(parts) != 2:
                continue

            key = (
                parts[0]
                .strip()
                .upper()
            )
            value = parts[1].strip()

            if key == "SUMMARY":
                summary = value
            elif key == "STATE":
                state_change = value
            elif key == "MERGE":
                merge = (
                    _studio_v2374_bool(
                        value
                    )
                )

        if summary and state_change:
            candidates.append({
                "summary": summary,
                "state_change":
                    state_change,
                "merge_with_previous":
                    merge,
                "character_entity_ids":
                    [],
                "prop_entity_ids":
                    [],
            })

    if not candidates:
        return {}

    # Prefer the candidate carrying the most explicit semantic text.
    return max(
        candidates,
        key=lambda row: (
            len(
                str(
                    row.get(
                        "summary"
                    )
                    or ""
                )
            )
            + len(
                str(
                    row.get(
                        "state_change"
                    )
                    or ""
                )
            )
        ),
    )


async def _studio_v2375_single_anchor_beat(
    *,
    anchor: dict,
    previous_beat: dict | None,
    entity_rows: list[dict],
) -> list[dict]:
    anchor_id = str(
        anchor.get("id")
        or ""
    ).strip()

    if not anchor_id:
        raise RuntimeError(
            "Singleton Beat anchor 缺少 id"
        )

    previous_context = (
        {
            "summary":
                previous_beat.get(
                    "summary"
                ),
            "state_change":
                previous_beat.get(
                    "state_change"
                ),
            "source_evidence_ids":
                previous_beat.get(
                    "source_evidence_ids"
                ),
        }
        if isinstance(
            previous_beat,
            dict,
        )
        else None
    )

    system_prompt = (
        "你是 Narrative Beat 微型语义生成器，运行 Qwen3-32B。"
        "当前只有一个已经被上一步确定为 Narrative Beat 的正文锚点。"
        "你只负责根据这个锚点写出被正文直接支持的 summary 和 state_change。"
        "不得添加锚点正文之外的新事件。"
        "如果该状态与 PREVIOUS_FINAL_BEAT 属于同一个不可分割状态单元，"
        "merge_with_previous=true；否则为 false。"
        "不得使用固定关键词、文本类别、题材类型或预设业务词表判断。"
        "不要返回证据 ID，证据由系统锁定。"
    )

    prompt = (
        "=== PREVIOUS_FINAL_BEAT ===\n"
        + _studio_json.dumps(
            previous_context,
            ensure_ascii=False,
        )
        + "\n\n=== SINGLE_BEAT_ANCHOR ===\n"
        + _studio_json.dumps(
            anchor,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + _studio_v2371_cut(
            _studio_json.dumps(
                entity_rows,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            900,
        )
    )

    diagnostics = []

    attempts = (
        (
            "json",
            "",
            650,
        ),
        (
            "json-strict",
            (
                "\n\nSTRICT_RETRY："
                "只返回 summary、state_change、merge_with_previous、"
                "character_entity_ids、prop_entity_ids。"
            ),
            650,
        ),
        (
            "line",
            (
                "\n\nSERIALIZATION_FALLBACK：不要输出 JSON。"
                "严格只输出三行：\n"
                "SUMMARY<TAB>...\n"
                "STATE<TAB>...\n"
                "MERGE<TAB>true|false"
            ),
            500,
        ),
    )

    for (
        attempt_name,
        suffix,
        max_tokens,
    ) in attempts:
        try:
            raw,parsed,_ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "singleton_beat_qwen32b"
                    ),
                    messages=[{
                        "role":"user",
                        "content":
                            prompt + suffix,
                    }],
                    system_prompt=
                        system_prompt,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    contract=(
                        '{"summary":"",'
                        '"state_change":"",'
                        '"merge_with_previous":false,'
                        '"character_entity_ids":[],'
                        '"prop_entity_ids":[]}'
                    ),
                )
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": qwen_call="
                + type(exc).__name__
                + ": "
                + str(exc)[:400]
            )
            continue

        semantics = (
            _studio_v2375_singleton_semantics(
                raw=raw,
                parsed=parsed,
            )
        )

        if not semantics:
            diagnostics.append(
                attempt_name
                + ": semantics_not_found raw_preview="
                + repr(
                    _studio_v2375_raw_preview(
                        raw
                    )
                )
            )
            continue

        merge = bool(
            semantics.get(
                "merge_with_previous"
            )
        )

        if previous_beat is None:
            merge = False

        row = {
            "summary":
                str(
                    semantics.get(
                        "summary"
                    )
                    or ""
                ).strip(),
            "state_change":
                str(
                    semantics.get(
                        "state_change"
                    )
                    or ""
                ).strip(),
            "source_evidence_ids":[
                anchor_id
            ],
            "character_entity_ids":
                list(
                    semantics.get(
                        "character_entity_ids"
                    )
                    or []
                ),
            "prop_entity_ids":
                list(
                    semantics.get(
                        "prop_entity_ids"
                    )
                    or []
                ),
            "merge_with_previous":
                merge,
        }

        _studio_v2373_validate_grouping(
            payload={
                "beats":[row]
            },
            beat_ids=[
                anchor_id
            ],
            support_ids=[],
            anchors=[
                anchor
            ],
        )

        return [row]

    raise RuntimeError(
        "Singleton Beat 语义生成失败；"
        "anchor_id="
        + anchor_id
        + "；"
        + " | ".join(
            diagnostics
        )
    )


async def _studio_v2375_compact_group_batch(
    *,
    batch_anchors: list[dict],
    batch_ids: list[str],
    previous_beat: dict | None,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> list[dict]:
    """
    Fallback grouping deliberately omits the whole CORE_SOURCE_CHUNK.
    Current anchor texts are already exact source evidence, so this reduces
    context pressure without changing semantic authority.
    """
    previous_context = (
        {
            "summary":
                previous_beat.get(
                    "summary"
                ),
            "state_change":
                previous_beat.get(
                    "state_change"
                ),
            "source_evidence_ids":
                previous_beat.get(
                    "source_evidence_ids"
                ),
        }
        if isinstance(
            previous_beat,
            dict,
        )
        else None
    )

    system_prompt = (
        "你是 Narrative Beat 小批次 Grouping 模型，运行 Qwen3-32B。"
        "CURRENT_BEAT_ANCHORS 全部已经确定属于 Narrative Beat。"
        "只根据这些锚点的精确正文，把它们组成最小有序叙事状态图中的 Beat。"
        "每个 current anchor 必须且只能出现一次；不能漏、不能重复。"
        "每个 Beat 必须有 summary、state_change、source_evidence_ids。"
        "summary/state_change 只能由该 Beat 自己选中的 source_evidence_ids "
        "直接支持；ID 必须按 source offset 递增；"
        "Beat 重组必须连同语义与证据闭包迁移。"
        "第一条 Beat 若与 PREVIOUS_FINAL_BEAT 为同一不可分割状态单元，"
        "可设置 merge_with_previous=true；其他 Beat 必须为 false。"
        "不得依据固定关键词、文本类别、题材类型或预设业务词表分组。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== PREVIOUS_FINAL_BEAT ===\n"
        + _studio_json.dumps(
            previous_context,
            ensure_ascii=False,
        )
        + "\n\n=== CURRENT_BEAT_ANCHORS ===\n"
        + _studio_json.dumps(
            batch_anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + _studio_v2371_cut(
            _studio_json.dumps(
                entity_rows,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            900,
        )
    )

    diagnostics = []

    attempts = (
        (
            "json",
            "",
            1200,
        ),
        (
            "json-strict",
            (
                "\n\nSTRICT_RETRY："
                "每个 CURRENT_BEAT_ANCHORS id 必须恰好出现一次；"
                "禁止空 source_evidence_ids。"
            ),
            1200,
        ),
        (
            "line",
            (
                "\n\nSERIALIZATION_FALLBACK：不要输出 JSON。"
                "每个 Beat 一行："
                "BEAT<TAB>summary<TAB>state_change"
                "<TAB>source_evidence_ids逗号分隔"
                "<TAB>character_entity_ids逗号分隔"
                "<TAB>prop_entity_ids逗号分隔。"
            ),
            1000,
        ),
    )

    for (
        attempt_name,
        suffix,
        max_tokens,
    ) in attempts:
        try:
            raw,parsed,_ = (
                await _studio_v2371a_qwen_call(
                    phase=(
                        "studio_stage04_"
                        "adaptive_beat_grouping_qwen32b"
                    ),
                    messages=[{
                        "role":"user",
                        "content":
                            prompt + suffix,
                    }],
                    system_prompt=
                        system_prompt,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    contract=(
                        '{"beats":[{'
                        '"summary":"",'
                        '"state_change":"",'
                        '"source_evidence_ids":["C01E001"],'
                        '"character_entity_ids":[],'
                        '"prop_entity_ids":[],'
                        '"merge_with_previous":false'
                        '}]}'
                    ),
                )
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": qwen_call="
                + type(exc).__name__
                + ": "
                + str(exc)[:350]
            )
            continue

        if attempt_name == "line":
            candidate = (
                _studio_v2372f_parse_line_payload(
                    raw,
                    anchors=batch_anchors,
                )
            )
        else:
            (
                candidate,
                _origin,
                _candidate_diagnostics,
            ) = (
                _studio_v2372f_extract_payload(
                    raw=raw,
                    parsed=parsed,
                    anchors=batch_anchors,
                )
            )

        rows = (
            _studio_v2374_group_rows(
                candidate,
                anchors=batch_anchors,
            )
        )

        if not rows:
            diagnostics.append(
                attempt_name
                + ": grouping_rows_empty raw_preview="
                + repr(
                    _studio_v2375_raw_preview(
                        raw
                    )
                )
            )
            continue

        (
            _membership,
            conflicts,
            missing,
        ) = (
            _studio_v2374_group_membership(
                rows,
                requested_ids=batch_ids,
            )
        )

        unresolved = list(
            dict.fromkeys(
                conflicts + missing
            )
        )

        if unresolved:
            try:
                rows = (
                    await _studio_v2374_resolve_group_membership(
                        chunk={
                            "text":
                                "\n".join(
                                    str(
                                        anchor.get(
                                            "text"
                                        )
                                        or ""
                                    )
                                    for anchor in batch_anchors
                                )
                        },
                        batch_anchors=
                            batch_anchors,
                        rows=rows,
                        requested_ids=
                            unresolved,
                        allowed_chars=
                            allowed_chars,
                        allowed_props=
                            allowed_props,
                        entity_rows=
                            entity_rows,
                    )
                )
            except Exception as exc:
                diagnostics.append(
                    attempt_name
                    + ": membership_repair="
                    + str(exc)[:650]
                )
                continue

        rows = (
            _studio_v2374_sort_group_rows(
                rows,
                anchors=batch_anchors,
            )
        )

        for index,row in enumerate(
            rows
        ):
            if (
                index > 0
                and row.get(
                    "merge_with_previous"
                )
            ):
                row[
                    "merge_with_previous"
                ] = False

        if (
            rows
            and rows[0].get(
                "merge_with_previous"
            )
            and previous_beat is None
        ):
            rows[0][
                "merge_with_previous"
            ] = False

        try:
            _studio_v2373_validate_grouping(
                payload={
                    "beats":rows
                },
                beat_ids=batch_ids,
                support_ids=[],
                anchors=batch_anchors,
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": exact_cardinality="
                + str(exc)[:700]
            )
            continue

        return rows

    raise RuntimeError(
        "Compact Beat grouping 无可用输出；"
        + " | ".join(
            diagnostics
        )
    )


async def _studio_v2375_process_ids(
    *,
    accumulated: list[dict],
    batch_ids: list[str],
    amap: dict[str, dict],
    chunk: dict,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
    depth: int = 0,
) -> list[dict]:
    if not batch_ids:
        return accumulated

    batch_anchors = [
        amap[key]
        for key in batch_ids
    ]

    previous = (
        accumulated[-1]
        if accumulated
        else None
    )

    # A singleton gets a dedicated semantic call. Evidence ID is bound by code,
    # eliminating the failure mode where model forgets/renames evidence fields.
    if len(batch_ids) == 1:
        rows = (
            await _studio_v2375_single_anchor_beat(
                anchor=batch_anchors[0],
                previous_beat=previous,
                entity_rows=entity_rows,
            )
        )

        return (
            _studio_v2374_merge_boundary(
                accumulated=accumulated,
                current_rows=rows,
            )
        )

    errors = []

    # First preserve V2.37.4 behavior for batches that already work.
    try:
        rows = (
            await _V2375_PREVIOUS_GROUP_BATCH(
                chunk=chunk,
                batch_anchors=batch_anchors,
                batch_ids=batch_ids,
                previous_beat=previous,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                entity_rows=entity_rows,
            )
        )

        return (
            _studio_v2374_merge_boundary(
                accumulated=accumulated,
                current_rows=rows,
            )
        )

    except Exception as exc:
        errors.append(
            "primary="
            + type(exc).__name__
            + ": "
            + str(exc)[:900]
        )

    # Compact retry removes the full source chunk from the prompt.
    try:
        rows = (
            await _studio_v2375_compact_group_batch(
                batch_anchors=batch_anchors,
                batch_ids=batch_ids,
                previous_beat=previous,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                entity_rows=entity_rows,
            )
        )

        return (
            _studio_v2374_merge_boundary(
                accumulated=accumulated,
                current_rows=rows,
            )
        )

    except Exception as exc:
        errors.append(
            "compact="
            + type(exc).__name__
            + ": "
            + str(exc)[:900]
        )

    # Structural/model-output failure is localized instead of killing the
    # whole Scene. Split contiguous IDs, preserving source order.
    midpoint = max(
        1,
        len(batch_ids) // 2,
    )

    left = batch_ids[:midpoint]
    right = batch_ids[midpoint:]

    if not right:
        raise RuntimeError(
            "Adaptive grouping 无法继续拆分；"
            + " errors="
            + repr(errors)
        )

    accumulated = (
        await _studio_v2375_process_ids(
            accumulated=accumulated,
            batch_ids=left,
            amap=amap,
            chunk=chunk,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
            entity_rows=entity_rows,
            depth=depth + 1,
        )
    )

    accumulated = (
        await _studio_v2375_process_ids(
            accumulated=accumulated,
            batch_ids=right,
            amap=amap,
            chunk=chunk,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
            entity_rows=entity_rows,
            depth=depth + 1,
        )
    )

    return accumulated


async def _studio_v2374_group_all(
    *,
    chunk: dict,
    anchors: list[dict],
    beat_ids: list[str],
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict],
) -> list[dict]:
    amap = (
        _studio_v2374_anchor_map(
            anchors
        )
    )

    beat_set = set(
        beat_ids
    )

    ordered_ids = [
        str(row.get("id") or "")
        for row in (
            _studio_v2374_ordered_anchors(
                anchors
            )
        )
        if str(row.get("id") or "")
        in beat_set
    ]

    top_batches = (
        _studio_v2374_chunks(
            ordered_ids,
            _STUDIO_V2374_GROUP_BATCH_SIZE,
        )
    )

    accumulated = []

    for batch_index,batch_ids in enumerate(
        top_batches,
        1,
    ):
        try:
            accumulated = (
                await _studio_v2375_process_ids(
                    accumulated=accumulated,
                    batch_ids=batch_ids,
                    amap=amap,
                    chunk=chunk,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                )
            )
        except Exception as exc:
            raise RuntimeError(
                "Adaptive Beat grouping top batch "
                + str(batch_index)
                + "/"
                + str(len(top_batches))
                + " failed；ids="
                + repr(batch_ids)
                + "；"
                + str(exc)
            ) from exc

    # Global exact-once check after all adaptive splits/merges.
    consumed = [
        str(evidence_id or "").strip()
        for row in accumulated
        for evidence_id in (
            row.get(
                "source_evidence_ids"
            )
            or []
        )
        if str(evidence_id or "").strip()
    ]

    consumed_set = set(
        consumed
    )

    duplicates = sorted({
        key
        for key in consumed
        if consumed.count(key) > 1
    })

    missing = sorted(
        beat_set - consumed_set
    )

    unexpected = sorted(
        consumed_set - beat_set
    )

    if (
        duplicates
        or missing
        or unexpected
    ):
        raise RuntimeError(
            "Adaptive Beat grouping 全局 exactly-once 校验失败；"
            "duplicates="
            + repr(duplicates)
            + " missing="
            + repr(missing)
            + " unexpected="
            + repr(unexpected)
        )

    return accumulated

# ===== /V2.37.5 STAGE04 ADAPTIVE GROUPING RECOVERY =====


# ===== V2.38.0 CONSOLIDATED STAGE04 RUNTIME BRIDGE =====
from app import stage04_v238_runtime as _stage04_v238_runtime

async def _studio_v2371_rebuild_stage04(project_id: str, task_id: str) -> None:
    return await _stage04_v238_runtime.rebuild(
        globals(),
        project_id,
        task_id,
    )
# ===== /V2.38.0 CONSOLIDATED STAGE04 RUNTIME BRIDGE =====
