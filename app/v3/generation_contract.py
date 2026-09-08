from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from .continuity import ContinuityRegistry, ReferenceContract
from .contracts import Capability
from .provider_gateway import ProviderRegistry, ProviderSelection


class ShotContract(BaseModel):
    shot_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    required_entity_ids: tuple[str, ...]
    camera_direction: str = ""
    action: str = ""
    duration_seconds: float = Field(default=3.0, gt=0, le=30)


@dataclass(frozen=True)
class GenerationContract:
    shot_id: str
    source_text: str
    entity_ids: tuple[str, ...]
    # Canonical entity references are retained for lineage/continuity audit.
    reference_ids: tuple[str, ...]
    # References actually sent to the selected provider. For image generation
    # this normally equals reference_ids. For video generation it is normally
    # the adopted shot first/last frame, while entity refs remain lineage.
    provider_reference_ids: tuple[str, ...]
    provider_id: str
    model_id: str
    required_capabilities: frozenset[Capability]
    # Keep the low-level contract backwards-compatible with ShotContract.
    # Video/frame-first call sites historically omitted camera/action because
    # those values are encoded in the video prompt; they must still compile to
    # a valid GenerationContract rather than failing before provider execution.
    camera_direction: str = ""
    action: str = ""
    duration_seconds: float = 3.0


class GenerationContractCompiler:
    """Compile semantic shots into explicit provider capability contracts.

    Image generation is entity-reference-first. Video generation is frame-first:
    canonical entity references remain part of lineage, but the video provider
    receives the adopted shot frame(s). This prevents a single-reference H3
    model from silently dropping character/creature/prop references.
    """

    def __init__(self, *, continuity: ContinuityRegistry, providers: ProviderRegistry) -> None:
        self.continuity = continuity
        self.providers = providers

    def _references(self, shot: ShotContract) -> ReferenceContract:
        if not shot.required_entity_ids:
            return ReferenceContract(entity_ids=(), reference_ids=())
        return self.continuity.build_reference_contract(shot.required_entity_ids, require_anchor=True)

    def compile_image(
        self,
        shot: ShotContract,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> GenerationContract:
        references = self._references(shot)
        provider_refs = references.reference_ids
        capabilities = {Capability.image_generation}
        if provider_refs:
            capabilities.add(Capability.image_reference)
        if len(provider_refs) > 1:
            capabilities.add(Capability.multi_reference)
        selection = self.providers.resolve(capabilities, provider_id=provider_id, model_id=model_id)
        self.providers.assert_reference_budget(selection, len(provider_refs))
        return self._build(shot, references, provider_refs, selection, capabilities)

    def compile_video(
        self,
        shot: ShotContract,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        first_frame_reference_id: str | None = None,
        last_frame_reference_id: str | None = None,
        require_first_frame: bool = True,
    ) -> GenerationContract:
        references = self._references(shot)
        first = str(first_frame_reference_id or "").strip()
        last = str(last_frame_reference_id or "").strip()
        if require_first_frame and not first:
            raise ValueError("video generation requires an adopted first_frame_reference_id")
        provider_refs = tuple(dict.fromkeys(item for item in (first, last) if item))

        capabilities = {Capability.video_generation}
        if first:
            capabilities.add(Capability.first_frame)
            capabilities.add(Capability.image_reference)
        if last:
            capabilities.add(Capability.last_frame)
            capabilities.add(Capability.image_reference)
        if first and last:
            capabilities.add(Capability.first_last_frame)
        if len(provider_refs) > 1:
            capabilities.add(Capability.multi_reference)

        selection = self.providers.resolve(capabilities, provider_id=provider_id, model_id=model_id)
        self.providers.assert_reference_budget(selection, len(provider_refs))
        return self._build(shot, references, provider_refs, selection, capabilities)

    @staticmethod
    def _build(
        shot: ShotContract,
        references: ReferenceContract,
        provider_refs: tuple[str, ...],
        selection: ProviderSelection,
        capabilities: set[Capability],
    ) -> GenerationContract:
        return GenerationContract(
            shot_id=shot.shot_id,
            source_text=shot.source_text,
            entity_ids=references.entity_ids,
            reference_ids=references.reference_ids,
            provider_reference_ids=provider_refs,
            provider_id=selection.spec.provider_id,
            model_id=selection.spec.model_id,
            required_capabilities=frozenset(capabilities),
            camera_direction=shot.camera_direction,
            action=shot.action,
            duration_seconds=shot.duration_seconds,
        )
