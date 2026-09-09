from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReferenceTemplate:
    asset_type: str
    role: str
    positive: str
    negative: str


REFERENCE_TEMPLATES = {
    "character": ReferenceTemplate(
        asset_type="CHARACTER",
        role="character_identity_reference",
        positive=(
            "character identity reference, front view, half body and full body consistency, "
            "fixed age, fixed face shape, fixed hairstyle, fixed clothing, clear identity anchor"
        ),
        negative=(
            "random model photography, "
            "identity change, different face"
        ),
    ),
    "location": ReferenceTemplate(
        asset_type="LOCATION",
        role="location_identity_reference",
        positive=(
            "location identity reference, architectural structure, spatial layout, "
            "environment details, lighting condition, world building consistency"
        ),
        negative="random background, unrelated architecture",
    ),
    "prop": ReferenceTemplate(
        asset_type="PROP",
        role="prop_identity_reference",
        positive=(
            "prop identity reference, shape, material, structure, craftsmanship details, "
            "consistent object design"
        ),
        negative="different object, wrong material, inconsistent structure",
    ),
}


def get_reference_template(asset_kind: str) -> ReferenceTemplate:
    key = str(asset_kind or "").lower()
    if key not in REFERENCE_TEMPLATES:
        raise ValueError(f"unsupported reference asset kind: {asset_kind}")
    return REFERENCE_TEMPLATES[key]
