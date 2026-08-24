from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.models import GPUOwner, TaskStatus


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _task_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="json")
    if isinstance(record, dict):
        return dict(record)
    if hasattr(record, "dict"):
        return record.dict()
    return {}


class SingleGenerationService:
    """Single-run candidate registry plus generic SDXL image-to-image execution.

    Candidate task outputs are intentionally NOT promoted into the material library
    or ProductionAsset graph here. Promotion is an explicit API/user action.
    """

    schema_version = "single_generation_v1"

    def __init__(self, settings, store, assets, gpu, comfyui) -> None:
        self.settings = settings
        self.store = store
        self.assets = assets
        self.gpu = gpu
        self.comfyui = comfyui
        self.root = Path(settings.data_dir) / "single_generation"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "candidates.json"
        self._lock = threading.RLock()
        self._tasks: set[asyncio.Task] = set()
        if not self.index_path.is_file():
            self._save_index({"schema_version": self.schema_version, "items": {}})

    def _load_index(self) -> dict[str, Any]:
        with self._lock:
            try:
                value = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                value = {}
            if not isinstance(value, dict):
                value = {}
            if not isinstance(value.get("items"), dict):
                value["items"] = {}
            value["schema_version"] = self.schema_version
            return value

    def _save_index(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.index_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp.replace(self.index_path)

    def register_candidate(
        self,
        task: Any,
        *,
        capability: str,
        mode: str,
        workspace_project_id: str,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = _task_dict(task)
        task_id = _clean(payload.get("task_id"))
        if not task_id:
            raise ValueError("候选任务缺少 task_id")
        now = _utcnow()
        data = self._load_index()
        item = data["items"].get(task_id)
        if not isinstance(item, dict):
            item = {
                "candidate_id": "cand_" + secrets.token_hex(10),
                "task_id": task_id,
                "created_at": now,
                "promotions": [],
            }
        item.update({
            "workspace_project_id": _clean(workspace_project_id),
            "capability": _clean(capability).lower(),
            "mode": _clean(mode).lower(),
            "inputs": dict(inputs or {}),
            "status": _clean(payload.get("status")) or "queued",
            "output_files": list(payload.get("output_files") or []),
            "updated_at": now,
            "dismissed": False,
        })
        data["items"][task_id] = item
        self._save_index(data)
        return dict(item)

    def get_candidate(self, task_id: str) -> dict[str, Any]:
        item = self._load_index()["items"].get(_clean(task_id))
        if not isinstance(item, dict):
            raise FileNotFoundError(f"单次候选任务不存在：{task_id}")
        return dict(item)

    def list_candidates(self, *, include_dismissed: bool = False) -> list[dict[str, Any]]:
        items = []
        for item in self._load_index()["items"].values():
            if not isinstance(item, dict):
                continue
            if item.get("dismissed") and not include_dismissed:
                continue
            items.append(dict(item))
        items.sort(key=lambda x: _clean(x.get("created_at")), reverse=True)
        return items

    def sync_candidate(self, task_payload: dict[str, Any]) -> dict[str, Any]:
        task_id = _clean(task_payload.get("task_id"))
        item = self.get_candidate(task_id)
        data = self._load_index()
        stored = data["items"][task_id]
        status_value = task_payload.get("status")
        if hasattr(status_value, "value"):
            status_value = status_value.value
        stored["status"] = _clean(status_value)
        stored["progress"] = int(task_payload.get("progress") or 0)
        stored["message"] = _clean(task_payload.get("message"))
        stored["error"] = _clean(task_payload.get("error"))
        stored["output_files"] = [
            _clean(x) for x in (task_payload.get("output_files") or []) if _clean(x)
        ]
        stored["updated_at"] = _utcnow()
        self._save_index(data)
        return dict(stored)

    def record_promotion(
        self,
        task_id: str,
        *,
        kind: str,
        output_index: int,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        data = self._load_index()
        item = data["items"].get(_clean(task_id))
        if not isinstance(item, dict):
            raise FileNotFoundError(f"单次候选任务不存在：{task_id}")
        item.setdefault("promotions", []).append({
            "kind": _clean(kind),
            "output_index": int(output_index),
            "result": result,
            "created_at": _utcnow(),
        })
        item["updated_at"] = _utcnow()
        self._save_index(data)
        return dict(item)

    def dismiss(self, task_id: str) -> dict[str, Any]:
        data = self._load_index()
        item = data["items"].get(_clean(task_id))
        if not isinstance(item, dict):
            raise FileNotFoundError(f"单次候选任务不存在：{task_id}")
        item["dismissed"] = True
        item["updated_at"] = _utcnow()
        self._save_index(data)
        return dict(item)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _input_default(spec: Any) -> Any:
        if not isinstance(spec, list) or not spec:
            return None
        first = spec[0]
        if isinstance(first, list):
            return first[0] if first else None
        if len(spec) > 1 and isinstance(spec[1], dict) and "default" in spec[1]:
            return spec[1]["default"]
        return None

    @classmethod
    def _node_inputs(cls, node_info: dict[str, Any], class_type: str, overrides: dict[str, Any]) -> dict[str, Any]:
        info = node_info.get(class_type)
        if not isinstance(info, dict):
            raise RuntimeError(f"ComfyUI 缺少节点：{class_type}")
        input_info = info.get("input") or {}
        required = input_info.get("required") or {}
        optional = input_info.get("optional") or {}
        names = set(required) | set(optional)
        inputs: dict[str, Any] = {}
        for name, spec in required.items():
            if name in overrides:
                continue
            value = cls._input_default(spec)
            if value is not None:
                inputs[name] = value
        for key, value in overrides.items():
            if key in names:
                inputs[key] = value
        missing = [name for name in required if name not in inputs]
        if missing:
            raise RuntimeError(
                f"节点 {class_type} 缺少必填输入：{', '.join(missing)}；当前节点版本与工作流不兼容"
            )
        return inputs

    @staticmethod
    def _choices(node_info: dict[str, Any], class_type: str, field: str) -> list[str]:
        try:
            input_info = node_info[class_type]["input"]
            spec = (input_info.get("required") or {}).get(field)
            if spec is None:
                spec = (input_info.get("optional") or {}).get(field)
            if isinstance(spec, list) and spec and isinstance(spec[0], list):
                return [str(x) for x in spec[0]]
        except Exception:
            pass
        return []

    @staticmethod
    def _choice_by_basename(choices: list[str], candidates: list[str]) -> str:
        lookup = {Path(str(x)).name.lower(): str(x) for x in choices}
        for candidate in candidates:
            value = lookup.get(Path(candidate).name.lower())
            if value:
                return value
        return ""

    async def _object_info(self) -> dict[str, Any]:
        base = self.settings.comfyui_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.get(f"{base}/object_info")
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("ComfyUI object_info 返回格式异常")
        return payload

    async def capabilities(self) -> dict[str, Any]:
        try:
            info = await self._object_info()
        except Exception as exc:
            # ComfyUI is normally stopped while the LLM owns the GPU. Do not disable
            # built-in image modes merely because the workspace is inactive; exact
            # node/model checks run again after GPU handoff inside the task.
            runtime_reason = f"ComfyUI 当前未激活；提交后切换 GPU 并做真实节点检查（{type(exc).__name__}）"
            ip_roots = [
                Path("/root/autodl-tmp/models/image/ipadapter"),
                Path("/root/autodl-tmp/ai-studio/ComfyUI/models/ipadapter"),
            ]
            clip_roots = [
                Path("/root/autodl-tmp/models/image/clip_vision"),
                Path("/root/autodl-tmp/ai-studio/ComfyUI/models/clip_vision"),
            ]
            ip_names = [
                "ip-adapter-plus_sdxl_vit-h.safetensors", "ip-adapter-plus_sdxl_vit-h.bin",
                "ip-adapter_sdxl_vit-h.safetensors", "ip-adapter_sdxl_vit-h.bin",
            ]
            clip_name = "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"
            ip_found = next((name for root in ip_roots for name in ip_names if (root / name).is_file()), "")
            clip_found = next((clip_name for root in clip_roots if (root / clip_name).is_file()), "")
            ref_available = bool(ip_found and clip_found)
            ref_reason = runtime_reason if ref_available else "参考图生成依赖未就绪：需要 SDXL IP-Adapter ViT-H + CLIP-ViT-H 模型"
            return {
                "txt2img": {"available": True, "label": "文生图", "reason": "复用现有图片接口"},
                "img2img": {"available": True, "label": "整体图生图", "reason": runtime_reason, "runtime_check": True},
                "inpaint": {"available": True, "label": "局部重绘", "reason": runtime_reason, "runtime_check": True},
                "reference": {"available": ref_available, "label": "参考图生成", "reason": ref_reason, "runtime_check": True},
            }

        upscale_choices = self._choices(info, "UpscaleModelLoader", "model_name")
        upscaler = self._choice_by_basename(upscale_choices, ["4x-UltraSharp.pth"])
        common = {
            "CheckpointLoaderSimple", "LoadImage", "ImageScale", "CLIPTextEncode", "KSampler",
            "VAEDecode", "SaveImage",
        }
        img_nodes = common | {"VAEEncode"}
        inpaint_nodes = common | {
            "LoadImageMask", "MaskToImage", "ImageToMask", "VAEEncodeForInpaint", "ImageCompositeMasked"
        }
        ref_nodes = common | {
            "IPAdapterModelLoader", "IPAdapterAdvanced", "CLIPVisionLoader", "EmptyLatentImage"
        }

        def node_status(nodes: set[str]) -> tuple[bool, str]:
            missing = sorted(x for x in nodes if x not in info)
            if missing:
                return False, "缺少 ComfyUI 节点：" + ", ".join(missing)
            return True, "READY"

        img_ok, img_reason = node_status(img_nodes)
        inp_ok, inp_reason = node_status(inpaint_nodes)
        ref_ok, ref_reason = node_status(ref_nodes)

        ip_model = ""
        clip_model = ""
        if ref_ok:
            ip_choices = self._choices(info, "IPAdapterModelLoader", "ipadapter_file")
            clip_choices = self._choices(info, "CLIPVisionLoader", "clip_name")
            ip_model = self._choice_by_basename(ip_choices, [
                "ip-adapter-plus_sdxl_vit-h.safetensors",
                "ip-adapter-plus_sdxl_vit-h.bin",
                "ip-adapter_sdxl_vit-h.safetensors",
                "ip-adapter_sdxl_vit-h.bin",
            ])
            clip_model = self._choice_by_basename(clip_choices, [
                "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
            ])
            missing_models = []
            if not ip_model:
                missing_models.append("SDXL IP-Adapter ViT-H")
            if not clip_model:
                missing_models.append("CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors")
            if missing_models:
                ref_ok = False
                ref_reason = "参考图生成依赖未就绪：" + "、".join(missing_models)

        return {
            "txt2img": {"available": True, "label": "文生图", "reason": "复用现有成熟图片生成接口"},
            "img2img": {
                "available": img_ok, "label": "整体图生图", "reason": img_reason if img_ok else img_reason,
                "workflow": "source resize -> VAEEncode -> KSampler -> VAE decode" if img_ok else "",
                "optional_ai_upscale": bool(upscaler),
                "upscaler": upscaler,
            },
            "inpaint": {
                "available": inp_ok, "label": "局部重绘", "reason": inp_reason,
                "workflow": "mask -> VAEEncodeForInpaint -> KSampler -> outside-mask composite" if inp_ok else "",
                "optional_ai_upscale": bool(upscaler),
                "upscaler": upscaler,
            },
            "reference": {
                "available": ref_ok, "label": "参考图生成", "reason": ref_reason,
                "workflow": "IPAdapterAdvanced -> txt2img latent -> VAE decode" if ref_ok else "",
                "optional_ai_upscale": bool(upscaler),
                "ipadapter_model": ip_model, "clip_vision_model": clip_model, "upscaler": upscaler,
            },
        }

    def submit_image_transform(
        self,
        *,
        mode: str,
        params: dict[str, Any],
        input_path: Path,
        mask_path: Path | None = None,
    ) -> Any:
        mode = _clean(mode).lower()
        if mode not in {"img2img", "inpaint", "reference"}:
            raise ValueError("图片转换模式必须是 img2img、inpaint 或 reference")
        if not input_path.is_file():
            raise FileNotFoundError(f"输入图片不存在：{input_path}")
        if mode == "inpaint" and (mask_path is None or not mask_path.is_file()):
            raise ValueError("局部重绘必须提供遮罩；白色区域为重绘区，黑色区域保持")
        task_id = uuid.uuid4().hex
        title = {"img2img": "整体图生图", "inpaint": "局部重绘", "reference": "参考图生成"}[mode]
        inputs = [str(input_path)] + ([str(mask_path)] if mask_path else [])
        record = self.store.create(
            task_id=task_id,
            module="image",
            operation=mode,
            title=title,
            params=dict(params),
            input_files=inputs,
        )
        self._spawn(self._run_image_transform(task_id, mode, dict(params), input_path, mask_path))
        return record

    async def _run_image_transform(
        self,
        task_id: str,
        mode: str,
        params: dict[str, Any],
        input_path: Path,
        mask_path: Path | None,
    ) -> None:
        labels = {"img2img": "整体图生图", "inpaint": "局部重绘", "reference": "参考图生成"}
        label = labels.get(mode, mode)
        try:
            self.store.update(task_id, status=TaskStatus.switching_gpu, progress=5,
                              message="正在切换到图片生成 GPU 工作区")
            await self.gpu.ensure_ready(GPUOwner.comfyui)
            self.store.update(task_id, status=TaskStatus.running, progress=15,
                              message=f"正在执行{label}")
            async with self.gpu.use(GPUOwner.comfyui):
                output_dir = self.store.task_dir(task_id) / "outputs"
                output_dir.mkdir(parents=True, exist_ok=True)
                path = output_dir / f"{mode}.png"
                await self._run_comfy_transform(
                    mode=mode,
                    input_path=input_path,
                    mask_path=mask_path,
                    output_path=path,
                    params=params,
                )
            self.store.update(task_id, status=TaskStatus.completed, progress=100,
                              message=f"{label}完成", output_files=[self.assets.url(path)])
        except Exception as exc:
            self.store.update(task_id, status=TaskStatus.failed, message=f"{label}失败",
                              error=f"{type(exc).__name__}: {exc}")

    async def _upload_image(self, path: Path, prefix: str) -> str:
        base = self.settings.comfyui_base_url.rstrip("/")
        upload_name = f"ai_studio_{prefix}_{uuid.uuid4().hex}_{path.name}"
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            with path.open("rb") as fp:
                response = await client.post(
                    f"{base}/upload/image",
                    data={"type": "input", "overwrite": "true"},
                    files={"image": (upload_name, fp, "application/octet-stream")},
                )
            if response.status_code >= 400:
                raise RuntimeError(f"ComfyUI 输入图片上传失败：{response.text[-2000:]}")
            uploaded = response.json()
        image_name = _clean(uploaded.get("name")) or upload_name
        subfolder = _clean(uploaded.get("subfolder"))
        return f"{subfolder}/{image_name}" if subfolder else image_name

    @staticmethod
    def _append_upscale(
        workflow: dict[str, Any], node_info: dict[str, Any], image_ref: list[Any],
        upscaler: str, output_width: int, output_height: int, start: int,
    ) -> list[Any]:
        a, b, c = str(start), str(start + 1), str(start + 2)
        workflow[a] = {"class_type": "UpscaleModelLoader", "inputs": SingleGenerationService._node_inputs(
            node_info, "UpscaleModelLoader", {"model_name": upscaler})}
        workflow[b] = {"class_type": "ImageUpscaleWithModel", "inputs": SingleGenerationService._node_inputs(
            node_info, "ImageUpscaleWithModel", {"upscale_model": [a, 0], "image": image_ref})}
        workflow[c] = {"class_type": "ImageScale", "inputs": SingleGenerationService._node_inputs(
            node_info, "ImageScale", {"image": [b, 0], "upscale_method": "lanczos",
                                      "width": output_width, "height": output_height, "crop": "center"})}
        return [c, 0]

    async def _run_comfy_transform(
        self,
        *,
        mode: str,
        input_path: Path,
        mask_path: Path | None,
        output_path: Path,
        params: dict[str, Any],
    ) -> None:
        positive = _clean(params.get("positive_prompt"))
        negative = _clean(params.get("negative_prompt"))
        checkpoint = _clean(params.get("checkpoint"))
        if not positive:
            raise ValueError("图片正向提示词不能为空")
        if not checkpoint:
            raise ValueError("没有解析到 checkpoint")
        base_width = max(256, min(int(params.get("base_width") or 1024), 2048))
        base_height = max(256, min(int(params.get("base_height") or 1024), 2048))
        output_width = max(256, min(int(params.get("output_width") or base_width), 4096))
        output_height = max(256, min(int(params.get("output_height") or base_height), 4096))
        steps = max(1, min(int(params.get("steps") or 32), 100))
        cfg = max(0.1, min(float(params.get("cfg") or 6.5), 30.0))
        seed = int(params.get("seed") if params.get("seed") is not None else -1)
        actual_seed = seed if seed >= 0 else secrets.randbelow(2**63 - 1)
        sampler = _clean(params.get("sampler")) or "dpmpp_2m"
        scheduler = _clean(params.get("scheduler")) or "karras"
        denoise = max(0.05, min(float(params.get("denoise") or 0.72), 1.0))
        reference_weight = max(0.05, min(float(params.get("reference_weight") or 0.80), 2.0))
        upscale_value = params.get("upscale_enabled", False)
        upscale_enabled = upscale_value is True or str(upscale_value).strip().lower() in {"1", "true", "yes", "on"}

        node_info = await self._object_info()
        caps = await self.capabilities()
        cap = caps.get(mode) or {}
        if not cap.get("available"):
            raise RuntimeError(str(cap.get("reason") or f"{mode} 当前不可用"))
        upscaler = _clean(cap.get("upscaler"))
        if upscale_enabled and not upscaler:
            raise RuntimeError("已启用 AI 超分，但当前 ComfyUI 未识别可用的 4x-UltraSharp；请关闭 AI 超分后生成基础结果")
        source_name = await self._upload_image(input_path, mode)
        mask_name = await self._upload_image(mask_path, "mask") if mask_path else ""
        prefix = f"single/{uuid.uuid4().hex}"
        workflow: dict[str, Any] = {}

        def add(n: int, class_type: str, overrides: dict[str, Any]) -> None:
            workflow[str(n)] = {"class_type": class_type,
                                "inputs": self._node_inputs(node_info, class_type, overrides)}

        if mode == "img2img":
            add(1, "CheckpointLoaderSimple", {"ckpt_name": checkpoint})
            add(2, "LoadImage", {"image": source_name})
            add(3, "ImageScale", {"image": ["2", 0], "upscale_method": "lanczos",
                                  "width": base_width, "height": base_height, "crop": "center"})
            add(4, "VAEEncode", {"pixels": ["3", 0], "vae": ["1", 2]})
            add(5, "CLIPTextEncode", {"text": positive, "clip": ["1", 1]})
            add(6, "CLIPTextEncode", {"text": negative, "clip": ["1", 1]})
            add(7, "KSampler", {"seed": actual_seed, "steps": steps, "cfg": cfg,
                                "sampler_name": sampler, "scheduler": scheduler, "denoise": denoise,
                                "model": ["1", 0], "positive": ["5", 0], "negative": ["6", 0],
                                "latent_image": ["4", 0]})
            add(8, "VAEDecode", {"samples": ["7", 0], "vae": ["1", 2]})
            final_ref = self._append_upscale(workflow, node_info, ["8", 0], upscaler,
                                             output_width, output_height, 20) if upscale_enabled else ["8", 0]

        elif mode == "inpaint":
            if not mask_name:
                raise ValueError("局部重绘缺少遮罩")
            add(1, "CheckpointLoaderSimple", {"ckpt_name": checkpoint})
            add(2, "LoadImage", {"image": source_name})
            add(3, "ImageScale", {"image": ["2", 0], "upscale_method": "lanczos",
                                  "width": base_width, "height": base_height, "crop": "center"})
            add(4, "LoadImageMask", {"image": mask_name, "channel": "red"})
            add(5, "MaskToImage", {"mask": ["4", 0]})
            add(6, "ImageScale", {"image": ["5", 0], "upscale_method": "nearest-exact",
                                  "width": base_width, "height": base_height, "crop": "center"})
            add(7, "ImageToMask", {"image": ["6", 0], "channel": "red"})
            add(8, "VAEEncodeForInpaint", {"pixels": ["3", 0], "vae": ["1", 2],
                                            "mask": ["7", 0], "grow_mask_by": 8})
            add(9, "CLIPTextEncode", {"text": positive, "clip": ["1", 1]})
            add(10, "CLIPTextEncode", {"text": negative, "clip": ["1", 1]})
            add(11, "KSampler", {"seed": actual_seed, "steps": steps, "cfg": cfg,
                                 "sampler_name": sampler, "scheduler": scheduler, "denoise": 1.0,
                                 "model": ["1", 0], "positive": ["9", 0], "negative": ["10", 0],
                                 "latent_image": ["8", 0]})
            add(12, "VAEDecode", {"samples": ["11", 0], "vae": ["1", 2]})
            add(13, "ImageCompositeMasked", {"destination": ["3", 0], "source": ["12", 0],
                                              "x": 0, "y": 0, "resize_source": True, "mask": ["7", 0]})
            final_ref = self._append_upscale(workflow, node_info, ["13", 0], upscaler,
                                             output_width, output_height, 20) if upscale_enabled else ["13", 0]

        elif mode == "reference":
            ip_model = _clean(cap.get("ipadapter_model"))
            clip_model = _clean(cap.get("clip_vision_model"))
            add(1, "CheckpointLoaderSimple", {"ckpt_name": checkpoint})
            add(2, "LoadImage", {"image": source_name})
            add(3, "IPAdapterModelLoader", {"ipadapter_file": ip_model})
            add(4, "CLIPVisionLoader", {"clip_name": clip_model})
            add(5, "IPAdapterAdvanced", {"model": ["1", 0], "ipadapter": ["3", 0],
                                          "image": ["2", 0], "clip_vision": ["4", 0],
                                          "weight": reference_weight, "start_at": 0.0, "end_at": 1.0})
            add(6, "CLIPTextEncode", {"text": positive, "clip": ["1", 1]})
            add(7, "CLIPTextEncode", {"text": negative, "clip": ["1", 1]})
            add(8, "EmptyLatentImage", {"width": base_width, "height": base_height, "batch_size": 1})
            add(9, "KSampler", {"seed": actual_seed, "steps": steps, "cfg": cfg,
                                "sampler_name": sampler, "scheduler": scheduler, "denoise": 1.0,
                                "model": ["5", 0], "positive": ["6", 0], "negative": ["7", 0],
                                "latent_image": ["8", 0]})
            add(10, "VAEDecode", {"samples": ["9", 0], "vae": ["1", 2]})
            final_ref = self._append_upscale(workflow, node_info, ["10", 0], upscaler,
                                             output_width, output_height, 20) if upscale_enabled else ["10", 0]
        else:
            raise ValueError("未知图片转换模式")

        add(30, "SaveImage", {"images": final_ref, "filename_prefix": prefix})
        base = self.settings.comfyui_base_url.rstrip("/")
        client_id = uuid.uuid4().hex
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(f"{base}/prompt", json={"prompt": workflow, "client_id": client_id})
            if response.status_code >= 400:
                raise RuntimeError(f"ComfyUI 拒绝 {mode} 工作流：{response.text[-3000:]}")
            prompt_id = _clean(response.json().get("prompt_id"))
        if not prompt_id:
            raise RuntimeError("ComfyUI 未返回 prompt_id")

        deadline = time.monotonic() + int(self.settings.comfyui_task_timeout_seconds)
        history: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.get(f"{base}/history/{prompt_id}")
                response.raise_for_status()
                payload = response.json()
            if prompt_id in payload:
                history = payload[prompt_id]
                status = history.get("status") or {}
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI {mode} 失败：{status.get('messages', [])}")
                if history.get("outputs"):
                    break
            await asyncio.sleep(2)
        if not history or not history.get("outputs"):
            raise TimeoutError(f"等待 ComfyUI {mode} 结果超时")

        image_meta: dict[str, Any] | None = None
        for output in (history.get("outputs") or {}).values():
            if not isinstance(output, dict):
                continue
            images = output.get("images") or []
            if images and isinstance(images[0], dict):
                image_meta = images[0]
                break
        if not image_meta:
            raise RuntimeError(f"ComfyUI {mode} 完成但没有返回图片")
        view_params = {"filename": image_meta.get("filename"), "subfolder": image_meta.get("subfolder", ""),
                       "type": image_meta.get("type", "output")}
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            response = await client.get(f"{base}/view", params=view_params)
            response.raise_for_status()
        output_path.write_bytes(response.content)
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError(f"ComfyUI {mode} 输出为空")

