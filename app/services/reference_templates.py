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
            "character identity reference, character turnaround sheet, single canvas model sheet, "
            "one prominent front facial close-up for identity and age locking, "
            "plus three full-body orthographic views arranged side by side: front view, exact 90-degree side view, back view, "
            "same person in every view, same visible age in every view, same face structure, same hairstyle, same body proportions, "
            "same clothing, footwear, accessories and color palette across all views, neutral expression, neutral standing pose, "
            "full body visible from head to feet, clean plain background, strict age fidelity from the confirmed character profile, "
            "clear identity anchor, production-ready character turnaround reference"
        ),
        negative=(
            "random model photography, identity change, different face, different person between views, age drift, "
            "unintended aged-up appearance, unintended de-aged appearance, inconsistent hairstyle, inconsistent clothing, "
            "inconsistent body proportions, missing side view, missing back view, front-only portrait, duplicated front views, "
            "cropped feet, occluded body, action pose, cinematic scene background, other characters, text, labels, watermark"
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
