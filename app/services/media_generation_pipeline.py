from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
import re
from typing import Any

from app.services.prompt_compiler import PromptCompiler, naturalize_visual_anchor
from app.services.generation_contract import GenerationContract
from app.services.visual_direction import VisualDirection


_FACE_ANCHOR_TOKENS = (
    "age", "年龄", "岁", "gender", "性别", "少年", "少女", "teen",
    "face", "facial", "脸", "面部", "脸型", "五官", "眉", "眼", "鼻", "嘴", "下颌", "轮廓",
    "hair", "发型", "发色", "黑发", "肤色", "skin", "气质", "冷峻", "清秀",
)
_COSTUME_ANCHOR_TOKENS = (
    "costume", "clothing", "outfit", "robe", "garment", "服装", "衣", "袍", "上衣", "下装",
    "shoe", "boot", "鞋", "靴", "accessory", "配饰", "配色", "颜色", "纹样", "刺绣", "材质",
    "sword", "剑", "剑鞘", "体型", "身高", "body", "build",
)


def _phase_anchor_text(text: str, phase: str) -> str:
    """Keep only facts relevant to the current character reference sub-stage.

    Face-anchor generation must not inherit scenery, action, sword poses or the
    complete costume paragraph. Costume generation should receive clothing/body
    facts, while the final turnaround keeps the full approved identity package.
    """
    raw = str(text or "").strip()
    phase = str(phase or "").strip().lower()
    if not raw or phase not in {"face_anchor", "costume"}:
        return raw
    tokens = _FACE_ANCHOR_TOKENS if phase == "face_anchor" else _COSTUME_ANCHOR_TOKENS
    parts = [part.strip(" -\t") for part in re.split(r"[;\n]+", raw) if part.strip(" -\t")]
    selected = [part for part in parts if any(token in part.lower() for token in tokens)]
    return "; ".join(dict.fromkeys(selected))


@dataclass(frozen=True)
class MediaGenerationRequest:
    """Provider independent generation request."""

    asset_kind: str
    asset_description: str
    generation_contract: Any
    visual_direction: Any


class MediaGenerationPipeline:
    """Single orchestration layer before image providers.

    Existing providers (ComfyUI etc.) remain unchanged. They only receive
    compiled prompts from this layer.
    """

    def __init__(self) -> None:
        self.prompt_compiler = PromptCompiler()

    def compile_provider_payload(self, request: MediaGenerationRequest) -> dict[str, str]:
        request.generation_contract.validate()
        return asdict(self.prompt_compiler.compile(
            asset_kind=request.asset_kind,
            asset_description=request.asset_description,
            visual_direction=request.visual_direction,
            contract=request.generation_contract,
        ))

    def prepare_candidate(self, production, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Compile the actual workbench request and persist its lineage."""
        target = production.ensure_visual_context(project_id, str(payload["target_asset_id"]))
        target_metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
        incoming_params = dict(payload.get("params") or {}) if isinstance(payload.get("params"), dict) else {}
        reference_phase = str(
            incoming_params.get("reference_phase")
            or target_metadata.get("reference_phase")
            or ""
        ).strip().lower()

        source_id = str(payload.get("prompt_asset_id") or "")
        source = production.get_asset(project_id, source_id)
        if source.get("metadata", {}).get("prompt_compiler") == "v3-visual-v1":
            contract_id = source.get("contract_artifact_id")
            if contract_id and payload.get("generation_contract_id") == contract_id:
                saved = json.loads(production.read_text_asset(project_id, contract_id))
                saved_context = dict(saved.get("visual_context") or {})
                saved_phase = str(saved_context.pop("reference_phase", "") or "").strip().lower()
                target_context = dict(target["metadata"]["visual_context"])
                if (
                    saved_context == target_context
                    and saved_phase == reference_phase
                    and saved.get("positive_prompt") == incoming_params.get("positive_prompt")
                    and saved.get("negative_prompt") == incoming_params.get("negative_prompt")
                ):
                    return payload
            source_id = source["metadata"]["source_prompt_asset_id"]

        description = production.read_text_asset(project_id, source_id)
        context = dict(target["metadata"]["visual_context"])
        direction = production.get_visual_direction(project_id, context.get("visual_direction_id", ""))
        entity_ids = tuple(target.get("entity_ids") or [])
        anchors: list[str] = []
        parents = [source_id, *[
            pid for pid in target.get("parent_asset_ids", [])
            if production.get_asset(project_id, pid).get("asset_role") != "generation_contract"
        ]]
        selections = list(target["metadata"].get("character_appearances") or [])
        profiles = production.list_assets(project_id, active_only=True)
        selected = {row["character_id"]: row["appearance_version"] for row in selections}
        appearance_owners = set()

        for asset in profiles:
            if asset.get("asset_role") == "character_appearance" and asset.get("status") == "ready" and asset.get("dependency_state") != "stale":
                version = (asset.get("metadata", {}).get("visual_context") or {}).get("appearance_version") or "v1"
                appearance_owners.update(
                    eid for eid in asset.get("entity_ids") or []
                    if selected.get(eid, context.get("appearance_version") or "v1") == version
                )

        for asset in profiles:
            if asset.get("asset_role") not in {"character_profile", "location_profile", "prop_profile", "character_appearance"}:
                continue
            if asset.get("status") != "ready" or asset.get("dependency_state") == "stale":
                continue
            matched = set(entity_ids) & set(asset.get("entity_ids") or [])
            if not matched and asset["asset_id"] not in parents:
                continue
            if asset["asset_role"] == "character_appearance":
                version = (asset.get("metadata", {}).get("visual_context") or {}).get("appearance_version") or "v1"
                if not any(selected.get(eid, context.get("appearance_version") or "v1") == version for eid in matched):
                    continue

            parents.append(asset["asset_id"])
            raw = production.read_text_asset(project_id, asset["asset_id"])
            if asset["asset_role"] == "character_profile" and matched & appearance_owners:
                try:
                    profile = json.loads(raw)
                except ValueError:
                    profile = {}
                if isinstance(profile, dict):
                    # Keep identity facts that may carry age/face/body constraints.
                    # Previously this branch retained stable_profile only, which
                    # could silently discard stable_design when an appearance
                    # asset was present.
                    raw = json.dumps({
                        "name": profile.get("name", ""),
                        "stable_profile": profile.get("stable_profile", {}),
                        "stable_design": profile.get("stable_design", ""),
                    }, ensure_ascii=False)

            anchor = naturalize_visual_anchor(raw)
            if context.get("asset_identity_type") == "character":
                anchor = _phase_anchor_text(anchor, reference_phase)
            if anchor and anchor not in anchors:
                anchors.append(anchor)

        if context.get("asset_identity_type") == "character" and not selections:
            selections = [
                {"character_id": eid, "appearance_version": context["appearance_version"] or "v1"}
                for eid in entity_ids
            ]

        contract = GenerationContract(
            asset_id=target["asset_id"],
            asset_version=str(target["version"]),
            prompt=description,
            visual_direction=direction,
            visual_context={**context, "reference_phase": reference_phase},
            entity_ids=entity_ids,
            character_appearances=tuple(selections),
            identity_anchors="\n".join(anchors),
        )
        reference = target.get("asset_role") in {
            "character_reference",
            "character_turnaround",
            "location_reference",
            "scene_reference",
            "prop_reference",
        }
        params = dict(incoming_params)
        compiled = self.prompt_compiler.compile(
            asset_kind=context.get("asset_identity_type", ""),
            asset_description=description,
            visual_direction=VisualDirection(**{
                f.name: direction[f.name] for f in fields(VisualDirection) if f.name in direction
            }),
            contract=contract,
            reference=reference,
            provider_negative=str(params.get("negative_prompt") or ""),
        )

        contract_asset = production.create_text_asset(
            project_id,
            stage=target["stage"],
            skill="v3-prompt-compiler",
            logical_key=f"{target['logical_key']}:generation-contract",
            asset_role="generation_contract",
            name="视觉生成合同",
            content=json.dumps({**asdict(contract), **asdict(compiled)}, ensure_ascii=False, sort_keys=True),
            asset_type="STRUCTURED_DATA",
            extension=".json",
            entity_ids=list(entity_ids),
            parent_asset_ids=list(dict.fromkeys(parents)),
            metadata={"visual_context": context, "reference_phase": reference_phase},
        )
        prompt_asset = production.create_text_asset(
            project_id,
            stage=target["stage"],
            skill="v3-prompt-compiler",
            logical_key=f"{target['logical_key']}:compiled-prompt",
            asset_role="compiled_image_prompt",
            name="已编译视觉提示词",
            content=compiled.positive_prompt,
            entity_ids=list(entity_ids),
            parent_asset_ids=[contract_asset["asset_id"]],
            contract_artifact_id=contract_asset["asset_id"],
            metadata={
                "visual_context": context,
                "prompt_compiler": "v3-visual-v1",
                "source_prompt_asset_id": source_id,
                "reference_phase": reference_phase,
            },
        )
        params.update(asdict(compiled))
        params["semantic_compile"] = "auto"
        production.bind_generation_contract(project_id, target["asset_id"], contract_asset["asset_id"])
        return {
            **payload,
            "prompt_asset_id": prompt_asset["asset_id"],
            "params": params,
            "generation_contract_id": contract_asset["asset_id"],
            "visual_context": {**context, "reference_phase": reference_phase},
            "visual_direction": direction,
            "character_appearances": selections,
        }
