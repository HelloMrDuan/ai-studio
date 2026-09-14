from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TemporalMode(str, Enum):
    observable_transition = "observable_transition"
    static_outcome = "static_outcome"
    insufficient_visual_evidence = "insufficient_visual_evidence"


class EvidenceSpan(BaseModel):
    evidence_id: str = Field(min_length=1)
    beat_order: int = Field(ge=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    text: str = Field(min_length=1)


class BeatContract(BaseModel):
    order: int = Field(ge=1)
    summary: str = Field(min_length=1)
    state_change: str = ""
    evidence: tuple[EvidenceSpan, ...]
    character_entity_ids: tuple[str, ...] = ()
    prop_entity_ids: tuple[str, ...] = ()
    creature_entity_ids: tuple[str, ...] = ()
    location_entity_ids: tuple[str, ...] = ()

    @property
    def allowed_evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.evidence if item.beat_order == self.order)

    @property
    def allowed_entity_ids(self) -> frozenset[str]:
        return frozenset(
            (*self.character_entity_ids, *self.prop_entity_ids, *self.creature_entity_ids, *self.location_entity_ids)
        )


class StoryboardShot(BaseModel):
    shot_id: str = Field(min_length=1)
    beat_order: int = Field(ge=1)
    title: str = ""
    summary: str = Field(min_length=1)
    source_fact: str = Field(min_length=1)
    temporal_mode: TemporalMode
    temporal_mode_reason: str = Field(min_length=1)
    temporal_mode_evidence_ids: tuple[str, ...]
    source_evidence_ids: tuple[str, ...]
    covered_beat_orders: tuple[int, ...]
    entity_ids: tuple[str, ...] = ()
    narrative_start_state: str = ""
    narrative_state: str = ""
    narrative_end_state: str = ""
    visual_start_frame: str = ""
    representative_frame: str = ""
    visual_end_frame: str = ""
    realization_scope: str = "narrative"
