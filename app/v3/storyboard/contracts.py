from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ShotSubjectPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(min_length=1)
    screen_position: str = Field(min_length=2)
    pose_and_gaze: str = Field(min_length=2)
    body_view: Literal["front", "left_profile", "right_profile", "back"]
    screen_box: tuple[float, float, float, float]

    @model_validator(mode="after")
    def valid_screen_box(self) -> "ShotSubjectPlacement":
        left, top, right, bottom = self.screen_box
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("screen_box must be a normalized [left, top, right, bottom] rectangle")
        if right - left < 0.04 or bottom - top < 0.08:
            raise ValueError("screen_box is too small for a visible character")
        return self


class ShotPropPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(min_length=1)
    holder_entity_id: str = ""
    screen_position: str = Field(min_length=2)
    visible_state: str = Field(min_length=2)
    screen_box: tuple[float, float, float, float]
    layer: Literal["behind_subject", "front_of_subject", "world"]
    attachment: Literal["hand", "back", "waist", "world"]
    attachment_anchor: tuple[float, float] | None = None
    hand_side: Literal["left", "right"] | None = None

    @model_validator(mode="after")
    def valid_screen_box(self) -> "ShotPropPlacement":
        left, top, right, bottom = self.screen_box
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("prop screen_box must be a normalized [left, top, right, bottom] rectangle")
        if self.layer != "world" and not self.holder_entity_id:
            raise ValueError("held prop layer requires a canonical holder_entity_id")
        if self.attachment == "world":
            if self.layer != "world" or self.holder_entity_id or self.attachment_anchor is not None or self.hand_side is not None:
                raise ValueError("world prop cannot have a character attachment")
        else:
            if not self.holder_entity_id or self.attachment_anchor is None:
                raise ValueError("attached prop requires a holder and attachment_anchor")
            if self.attachment == "hand" and self.hand_side is None:
                raise ValueError("hand prop requires an anatomical hand_side")
            if self.attachment != "hand" and self.hand_side is not None:
                raise ValueError("hand_side is only valid for a hand prop")
            x, y = self.attachment_anchor
            if not (left <= x <= right and top <= y <= bottom):
                raise ValueError("attachment_anchor must lie inside prop screen_box")
            if self.attachment == "back" and self.layer not in {"behind_subject", "front_of_subject"}:
                raise ValueError("back attachment must be layered with its subject")
            if self.attachment == "hand" and self.layer != "front_of_subject":
                raise ValueError("hand attachment must be in front of the subject")
        return self


class ShotVisualPlan(BaseModel):
    """Visual-only authoring for one frozen formal shot.

    Narrative states and canonical entity IDs remain owned by Stage04.  The
    planner may describe only the single frame's visible composition.
    """

    model_config = ConfigDict(extra="forbid")

    shot_id: str = Field(min_length=1)
    location_entity_id: str = Field(min_length=1)
    frame_description: str = Field(min_length=24)
    composition: str = Field(min_length=8)
    standing_surface: str = Field(min_length=4)
    subject_positions: list[ShotSubjectPlacement] = Field(default_factory=list)
    prop_placements: list[ShotPropPlacement] = Field(default_factory=list)
    visual_exclusions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_attachment_geometry(self) -> "ShotVisualPlan":
        subject_boxes = {row.entity_id: row.screen_box for row in self.subject_positions}
        subject_views = {row.entity_id: row.body_view for row in self.subject_positions}
        for row in self.prop_placements:
            if not row.holder_entity_id:
                continue
            owner = subject_boxes.get(row.holder_entity_id)
            if owner is None or row.attachment_anchor is None:
                raise ValueError("attached prop has no visible canonical holder or anchor")
            x, y = row.attachment_anchor
            relative_x = (x - owner[0]) / (owner[2] - owner[0])
            relative_y = (y - owner[1]) / (owner[3] - owner[1])
            regions = {"back": (0.1, 0.9, 0.08, 0.42),
                       "hand": (-0.2, 1.2, 0.3, 0.85),
                       "waist": (0.05, 0.95, 0.4, 0.72)}
            xmin, xmax, ymin, ymax = regions[row.attachment]
            if row.attachment != "hand" and not (xmin <= relative_x <= xmax and ymin <= relative_y <= ymax):
                raise ValueError(f"prop {row.attachment} anchor is outside the holder body region")
            if row.attachment == "back":
                expected_layer = ("front_of_subject" if subject_views[row.holder_entity_id] == "back"
                                  else "behind_subject")
                if row.layer != expected_layer:
                    raise ValueError("back prop depth layer conflicts with the holder body view")
                if (row.screen_box[3] - row.screen_box[1]) + 1e-6 < 0.25 * (owner[3] - owner[1]):
                    raise ValueError("back prop is too small to remain visible in the shot")
            if row.attachment == "back" and (row.screen_box[1] - owner[1]) / (owner[3] - owner[1]) > 0.30:
                raise ValueError("back prop starts below the holder's upper back")
        return self


class ShotSupportRegion(BaseModel):
    """An approved walkable polygon measured on a specific scene-plate image."""

    model_config = ConfigDict(extra="forbid")

    polygon: list[tuple[float, float]] = Field(min_length=3)

    @model_validator(mode="after")
    def valid_polygon(self) -> "ShotSupportRegion":
        if any(not (0 <= x <= 1 and 0 <= y <= 1) for x, y in self.polygon):
            raise ValueError("support polygon coordinates must be normalized")
        twice_area = abs(sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(self.polygon, self.polygon[1:] + self.polygon[:1])
        ))
        if twice_area < 0.02:
            raise ValueError("support polygon has too little visible area")
        return self


def _point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if ((y1 > y) != (y2 > y)) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def snap_shot_support(
    plan: ShotVisualPlan, regions: list[ShotSupportRegion],
) -> tuple[ShotVisualPlan, list[dict[str, object]]]:
    """Move a typed subject horizontally to the nearest approved ground interval."""
    data = plan.model_dump(mode="json")
    corrections: list[dict[str, object]] = []
    for subject in data["subject_positions"]:
        left, top, right, bottom = (float(value) for value in subject["screen_box"])
        foot_y = max(0.0, bottom - 0.01)
        width = right - left
        foot_left, foot_right = left + 0.35 * width, left + 0.65 * width
        if all(any(_point_in_polygon(left + fraction * width, foot_y, region.polygon)
                   for region in regions) for fraction in (0.35, 0.5, 0.65)):
            continue
        shifts: list[float] = []
        for region in regions:
            crossings: list[float] = []
            for (x1, y1), (x2, y2) in zip(region.polygon, region.polygon[1:] + region.polygon[:1]):
                if (y1 > foot_y) != (y2 > foot_y):
                    crossings.append(x1 + (x2 - x1) * (foot_y - y1) / (y2 - y1))
            crossings.sort()
            for index in range(0, len(crossings) - 1, 2):
                ground_left, ground_right = crossings[index:index + 2]
                minimum = max(ground_left + 0.02 - foot_left, -left)
                maximum = min(ground_right - 0.02 - foot_right, 1 - right)
                if minimum <= maximum:
                    shifts.append(max(minimum, min(0.0, maximum)))
        shifts.sort(key=abs)
        selected = None
        for shift in shifts:
            if not all(any(_point_in_polygon(left + shift + fraction * width, foot_y, region.polygon)
                           for region in regions) for fraction in (0.35, 0.5, 0.65)):
                continue
            moved = (left + shift, top, right + shift, bottom)
            overlaps = False
            for other in data["subject_positions"]:
                if other is subject:
                    continue
                box = other["screen_box"]
                intersection = max(0.0, min(moved[2], box[2]) - max(moved[0], box[0])) * max(
                    0.0, min(moved[3], box[3]) - max(moved[1], box[1]))
                other_area = (box[2] - box[0]) * (box[3] - box[1])
                if intersection > 0.35 * min(width * (bottom - top), other_area):
                    overlaps = True
                    break
            if not overlaps:
                selected = shift
                break
        if selected is None:
            raise ValueError(f"shot subject {subject['entity_id']} has no grounded non-overlapping placement")
        subject["screen_box"] = [round(left + selected, 4), top, round(right + selected, 4), bottom]
        for prop in data["prop_placements"]:
            if prop.get("holder_entity_id") != subject["entity_id"]:
                continue
            prop["screen_box"][0] = round(prop["screen_box"][0] + selected, 4)
            prop["screen_box"][2] = round(prop["screen_box"][2] + selected, 4)
            if prop.get("attachment_anchor") is not None:
                prop["attachment_anchor"][0] = round(prop["attachment_anchor"][0] + selected, 4)
        corrections.append({
            "entity_id": subject["entity_id"], "horizontal_shift": round(selected, 4),
            "rule": "approved_scene_plate_ground_projection",
        })
    return ShotVisualPlan.model_validate(data), corrections

def validate_shot_support(plan: ShotVisualPlan, regions: list[ShotSupportRegion]) -> None:
    """Fail before rendering when a character's feet leave approved ground."""

    if plan.subject_positions and not regions:
        raise ValueError("shot scene plate has no approved walkable support region")
    for subject in plan.subject_positions:
        left, _, right, bottom = subject.screen_box
        foot_y = max(0.0, bottom - 0.01)
        for fraction in (0.35, 0.5, 0.65):
            foot_x = left + (right - left) * fraction
            if not any(_point_in_polygon(foot_x, foot_y, region.polygon) for region in regions):
                raise ValueError(
                    f"shot subject {subject.entity_id} has an unsupported foot position "
                    f"at ({foot_x:.3f}, {foot_y:.3f})"
                )
