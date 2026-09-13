from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .contracts import EntityKind, VisualEntity


IMMUTABLE_FIELDS: dict[EntityKind, frozenset[str]] = {
    EntityKind.character: frozenset(
        {
            "identity",
            "face_identity",
            "age_band",
            "hair_baseline",
            "body_identity",
            "baseline_outfit",
            "signature_item",
            "cultural_context",
        }
    ),
    EntityKind.creature: frozenset(
        {"species", "anatomy", "silhouette", "surface", "palette", "eyes", "markings", "horns_mane"}
    ),
    EntityKind.prop: frozenset({"shape", "geometry", "material", "ornament", "color_identity"}),
    EntityKind.important_item: frozenset({"shape", "geometry", "material", "ornament", "color_identity"}),
    EntityKind.location: frozenset({"location_identity", "architecture", "environment_identity"}),
    EntityKind.environment: frozenset({"location_identity", "environment_identity", "visual_style"}),
    EntityKind.vehicle: frozenset({"make_identity", "geometry", "material", "color_identity"}),
}


class ContinuityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReferenceContract:
    entity_ids: tuple[str, ...]
    reference_ids: tuple[str, ...]


class ContinuityRegistry:
    """Project-scope entity and canonical-reference authority."""

    def __init__(self) -> None:
        self._entities: dict[str, VisualEntity] = {}

    def register(self, entity: VisualEntity) -> VisualEntity:
        if entity.entity_id in self._entities:
            raise ContinuityError(f"duplicate entity_id: {entity.entity_id}")
        self._entities[entity.entity_id] = entity.model_copy(deep=True)
        return self.get(entity.entity_id)

    def get(self, entity_id: str) -> VisualEntity:
        try:
            return self._entities[entity_id].model_copy(deep=True)
        except KeyError as exc:
            raise ContinuityError(f"unknown entity_id: {entity_id}") from exc

    def update_shot_state(self, entity_id: str, **changes: Any) -> VisualEntity:
        entity = self._entities.get(entity_id)
        if entity is None:
            raise ContinuityError(f"unknown entity_id: {entity_id}")
        forbidden = IMMUTABLE_FIELDS[entity.kind].intersection(changes)
        if forbidden:
            raise ContinuityError(
                f"shot state cannot mutate immutable identity fields for {entity_id}: {sorted(forbidden)}"
            )
        entity.shot_state.update(deepcopy(changes))
        entity.revision += 1
        return entity.model_copy(deep=True)

    def rebaseline_identity(self, entity_id: str, *, explicit: bool = False, **changes: Any) -> VisualEntity:
        if not explicit:
            raise ContinuityError("identity rebaseline requires explicit=True and must invalidate dependent outputs")
        entity = self._entities.get(entity_id)
        if entity is None:
            raise ContinuityError(f"unknown entity_id: {entity_id}")
        entity.identity.update(deepcopy(changes))
        entity.revision += 1
        return entity.model_copy(deep=True)

    def adopt_reference(self, entity_id: str, reference_id: str) -> VisualEntity:
        entity = self._entities.get(entity_id)
        if entity is None:
            raise ContinuityError(f"unknown entity_id: {entity_id}")
        ref = str(reference_id or "").strip()
        if not ref:
            raise ContinuityError("reference_id is required")
        if ref not in entity.canonical_reference_ids:
            entity.canonical_reference_ids.append(ref)
            entity.revision += 1
        return entity.model_copy(deep=True)

    def build_reference_contract(
        self,
        entity_ids: list[str] | tuple[str, ...],
        *,
        require_anchor: bool = True,
    ) -> ReferenceContract:
        ordered_entities = tuple(dict.fromkeys(entity_ids))
        references: list[str] = []
        for entity_id in ordered_entities:
            entity = self._entities.get(entity_id)
            if entity is None:
                raise ContinuityError(f"unknown entity_id: {entity_id}")
            if require_anchor and not entity.canonical_reference_ids:
                raise ContinuityError(f"entity has no adopted canonical reference: {entity_id}")
            references.extend(entity.canonical_reference_ids)
        return ReferenceContract(
            entity_ids=ordered_entities,
            reference_ids=tuple(dict.fromkeys(references)),
        )


def assert_required_entities_visible(
    required_entity_ids: list[str] | tuple[str, ...],
    visible_entity_ids: list[str] | tuple[str, ...],
) -> None:
    required = set(required_entity_ids)
    visible = set(visible_entity_ids)
    missing = sorted(required - visible)
    if missing:
        raise ContinuityError(f"visual audit missing required entities: {missing}")
