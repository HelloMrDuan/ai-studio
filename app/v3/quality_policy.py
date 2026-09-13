from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


QualityTier = Literal["A", "B", "C"]


@dataclass(frozen=True)
class QualityProfile:
    tier: QualityTier
    image_candidate_steps: int
    image_final_steps: int
    video_steps: int
    semantic_audit: bool
    identity_audit: bool
    candidate_count: int = 1


_PROFILES: dict[QualityTier, QualityProfile] = {
    "A": QualityProfile(
        tier="A",
        image_candidate_steps=24,
        image_final_steps=34,
        video_steps=24,
        semantic_audit=True,
        identity_audit=True,
    ),
    "B": QualityProfile(
        tier="B",
        image_candidate_steps=21,
        image_final_steps=29,
        video_steps=20,
        semantic_audit=False,
        identity_audit=True,
    ),
    "C": QualityProfile(
        tier="C",
        image_candidate_steps=18,
        image_final_steps=24,
        video_steps=16,
        semantic_audit=False,
        identity_audit=False,
    ),
}


def normalize_quality_tier(value: Any) -> QualityTier:
    tier = str(value or "").strip().upper()
    return tier if tier in _PROFILES else "B"  # type: ignore[return-value]


def infer_quality_tier(shot: dict[str, Any]) -> QualityTier:
    """Cheap deterministic routing before any semantic quality model is called."""
    explicit = str(shot.get("quality_tier") or shot.get("quality_level") or "").strip().upper()
    if explicit in _PROFILES:
        return explicit  # type: ignore[return-value]

    text = " ".join(
        str(shot.get(key) or "")
        for key in (
            "title", "summary", "shot_size", "action", "performance", "narration", "dialogue"
        )
    )
    a_markers = ("特写", "高潮", "关键", "首次", "揭示", "主角", "身份", "决战", "泪", "表情")
    c_markers = ("过渡", "建立", "空镜", "远景环境", "转场", "环境镜头")
    if any(marker in text for marker in a_markers):
        return "A"
    if any(marker in text for marker in c_markers):
        return "C"
    return "B"


def profile_for_shot(shot: dict[str, Any]) -> QualityProfile:
    return _PROFILES[infer_quality_tier(shot)]


def apply_smart_candidate_params(
    params: dict[str, Any] | None,
    *,
    shot: dict[str, Any],
    capability: str,
) -> dict[str, Any]:
    result = dict(params or {})
    profile = profile_for_shot(shot)
    result["quality_tier"] = profile.tier
    result["quality_mode"] = "smart"
    result["candidate_count"] = 1
    result["semantic_audit"] = profile.semantic_audit
    result["identity_audit"] = profile.identity_audit
    if capability == "image":
        result.setdefault("steps", profile.image_candidate_steps)
        result["final_refine_steps"] = profile.image_final_steps
    elif capability == "video":
        result.setdefault("steps", profile.video_steps)
    return result


__all__ = [
    "QualityTier",
    "QualityProfile",
    "normalize_quality_tier",
    "infer_quality_tier",
    "profile_for_shot",
    "apply_smart_candidate_params",
]
