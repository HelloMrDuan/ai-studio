#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings
from app.core.gpu_orchestrator import GPUOrchestrator
from app.models import GPUOwner
from app.v3.adapters.comfyui import ComfyUIAdapter
from app.v3.contracts import Capability
from app.v3.generation_contract import GenerationContract
from app.v3.generation_executor import ReferenceAssetStore, ReferenceFirstComfyExecutor
from app.v3.provider_catalog import build_provider_registry


def _first_artifact(item: dict[str, Any]) -> dict[str, Any] | None:
    for output in (item.get("outputs") or {}).values():
        if not isinstance(output, dict):
            continue
        for field in ("images", "videos", "gifs"):
            for artifact in output.get(field, []) or []:
                if isinstance(artifact, dict) and str(artifact.get("filename") or "").strip():
                    return dict(artifact)
    return None


def _discover_references(data_dir: Path) -> list[str]:
    index = data_dir / "v3" / "reference-assets" / "index.json"
    if not index.is_file():
        return []
    data = json.loads(index.read_text(encoding="utf-8"))
    rows = [row for row in (data.get("references") or {}).values() if isinstance(row, dict)]
    rows.sort(
        key=lambda row: (
            0 if str(row.get("entity_type") or "") == "character" else 1,
            0 if "character" in str(row.get("role") or "") else 1,
            str(row.get("entity_id") or ""),
            str(row.get("reference_id") or ""),
        )
    )
    selected: list[str] = []
    used_entities: set[str] = set()
    for row in rows:
        ref = str(row.get("reference_id") or "").strip()
        entity = str(row.get("entity_id") or "").strip() or ref
        if not ref or entity in used_entities:
            continue
        used_entities.add(entity)
        selected.append(ref)
        if len(selected) >= 4:
            break
    return selected


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    data_dir = Path(args.data_dir or settings.data_dir)
    profile = data_dir / "comfyui_reference_profile.v3.json"
    if not profile.is_file():
        raise SystemExit("未安装多参考配置，请先运行 configure_comfy_reference_profile.py --profile faceid")

    refs = [item.strip() for item in (args.reference_id or []) if item.strip()]
    if not refs:
        refs = _discover_references(data_dir)
    if len(refs) < 2:
        raise SystemExit("真实多参考验收至少需要 2 个不同的已采用参考资产")
    refs = refs[:4]

    store = ReferenceAssetStore(data_dir)
    resolved = store.resolve_many(refs)
    character_refs = [item for item in resolved if item.entity_type == "character" or "character" in item.role]
    if len(character_refs) < 2 and not args.allow_non_character_pair:
        raise SystemExit(
            "当前参考资产里不足 2 个不同人物。多角色验收必须至少准备并采用两个人物参考图；"
            "如果只想验证技术上的多路注入，可加 --allow-non-character-pair。"
        )

    registry = build_provider_registry(settings)
    selected = registry.resolve(
        {Capability.image_generation, Capability.image_reference, Capability.multi_reference},
        provider_id="local-comfyui-image",
        model_id="configured-image-workflow",
    )
    registry.assert_reference_budget(selected, len(refs))
    if int(selected.spec.max_references or 0) < len(refs):
        raise SystemExit("Provider 多参考预算不足")

    contract = GenerationContract(
        shot_id="multi-reference-acceptance",
        source_text=(
            args.prompt
            or "cinematic medium-wide shot, all referenced characters appear as distinct people, preserve each identity, preserve referenced environment and important props, natural composition, detailed faces"
        ),
        entity_ids=tuple(item.entity_id for item in resolved if item.entity_id),
        reference_ids=tuple(refs),
        provider_reference_ids=tuple(refs),
        provider_id=selected.spec.provider_id,
        model_id=selected.spec.model_id,
        required_capabilities=frozenset(
            {Capability.image_generation, Capability.image_reference, Capability.multi_reference}
        ),
        width=args.width,
        height=args.height,
        steps=args.steps,
        cfg=args.cfg,
        seed=args.seed,
        sampler_name="dpmpp_2m",
        scheduler="karras",
    )

    gpu = GPUOrchestrator(settings)
    adapter = ComfyUIAdapter(selected.spec)
    async with gpu.use(GPUOwner.comfyui):
        receipt = await ReferenceFirstComfyExecutor(adapter=adapter, references=store).execute_provider_profile(contract)
        deadline = time.monotonic() + float(args.timeout)
        artifact: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            history = await adapter.history(receipt.prompt_id)
            item = history.get(receipt.prompt_id)
            if isinstance(item, dict):
                status = item.get("status") if isinstance(item.get("status"), dict) else {}
                if str(status.get("status_str") or "").lower() == "error":
                    raise RuntimeError(json.dumps(status.get("messages") or item, ensure_ascii=False)[-3000:])
                artifact = _first_artifact(item)
                if artifact:
                    break
            await asyncio.sleep(2)
        if artifact is None:
            raise TimeoutError(f"等待多参考图片超时：{receipt.prompt_id}")

        params = {
            "filename": str(artifact.get("filename") or ""),
            "subfolder": str(artifact.get("subfolder") or ""),
            "type": str(artifact.get("type") or "output"),
        }
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.get(f"{adapter.base_url}/view", params=params)
            response.raise_for_status()
            content = bytes(response.content)
        if not content:
            raise RuntimeError("ComfyUI 返回空图片")

    target_dir = data_dir / "v3" / "acceptance" / "multi-reference"
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(str(artifact.get("filename") or "output.png")).suffix or ".png"
    target = target_dir / f"multi_reference_{int(time.time())}{suffix}"
    target.write_bytes(content)

    print("MULTI_REFERENCE_ACCEPTANCE=PASS")
    print(f"prompt_id={receipt.prompt_id}")
    print(f"reference_count={len(refs)}")
    print(f"character_reference_count={len(character_refs)}")
    for index, asset in enumerate(resolved, start=1):
        print(f"reference_{index}={asset.reference_id}|{asset.entity_type}|{asset.role}|{asset.entity_id}")
    print(f"artifact={target}")
    print("请打开 artifact 肉眼确认：两个人物身份没有融合，场景/道具约束没有丢失。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 ComfyUI 多参考人物一致性验收")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--reference-id", action="append", default=[])
    parser.add_argument("--allow-non-character-pair", action="store_true")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=576)
    parser.add_argument("--steps", type=int, default=28)
    parser.add_argument("--cfg", type=float, default=5.5)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
