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
            "character identity reference, 4-panel character turnaround sheet: large front facial close-up, "
            "full-body front view, exact 90-degree side view, full-body back view; same character and same visible age "
            "in every panel; fixed face shape, fixed hairstyle, fixed clothing, fixed body proportions, footwear, "
            "accessories and color palette; neutral pose, full body head-to-feet, plain design-sheet background"
        ),
        negative=(
            "different person between panels, age drift, missing facial close-up, missing side view, missing back view, "
            "duplicated front views, cropped feet, inconsistent face, inconsistent hairstyle, inconsistent clothing, "
            "action pose, fashion photoshoot, cinematic scene background, other characters, text, labels, watermark"
        ),
    ),
    "location": ReferenceTemplate(
        asset_type="LOCATION",
        role="location_identity_reference",
        positive=(
            "unoccupied location identity reference, completely empty environment plate, environment-only composition, "
            "architecture and terrain are the sole visual content, empty paths and empty stairs, no focal living subject, "
            "stable spatial layout, environment materials, fixed structures, pathways, foreground-midground-background "
            "relationships, lighting condition, world-building consistency, reusable static environment baseline, "
            "purely visual unlettered plate, caption-free, typography-free, graphic-overlay-free"
        ),
        negative=(
            "person, people, human figure, character, protagonist, crowd, silhouette, portrait, body, face, action, "
            "tiny distant person, distant human figure, distant traveler, pedestrian, monk, human-shaped silhouette, "
            "humanoid shape, person on stairs, figure on stairs, red-clothed figure, living subject, animal subject, "
            "held object, carried prop, random background, unrelated architecture, text, lettering, title, caption, "
            "Chinese characters, calligraphy, signboard, presentation text, metadata text, field name, key-value text, "
            "stable_profile, stable.profile, type label, design label, labels, watermark"
        ),
    ),
    "prop": ReferenceTemplate(
        asset_type="PROP",
        role="prop_identity_reference",
        positive=(
            "isolated prop identity reference, single-object product reference, exactly one physical prop total in frame, "
            "one complete assembled item, one-object-only composition, sole visual subject is the target prop itself, "
            "centered, fully visible, unobstructed, clean neutral seamless light-gray background, clear silhouette and "
            "proportions, shape, material, structure, craftsmanship details, color and pattern fidelity, consistent object "
            "design, isolated catalog product study, purely visual unlettered image, caption-free, typography-free, "
            "graphic-overlay-free"
        ),
        negative=(
            "person, people, human figure, character, hand, hands, body, face, wearing, worn, carried, held, handheld, "
            "mounted on a person, action scene, cinematic environment, landscape background, architectural background, "
            "other objects, multiple objects, two objects, second object, duplicate prop, duplicated object, companion "
            "object, supporting object, accessory object, detached component, exploded view, paired items, alternate prop, "
            "different object, wrong material, inconsistent structure, text, lettering, title, caption, Chinese characters, "
            "calligraphy, presentation text, metadata text, field name, key-value text, stable_profile, stable.profile, "
            "type label, design label, labels, watermark"
        ),
    ),
}


def get_reference_template(asset_kind: str) -> ReferenceTemplate:
    key = str(asset_kind or "").lower()
    if key not in REFERENCE_TEMPLATES:
        raise ValueError(f"unsupported reference asset kind: {asset_kind}")
    return REFERENCE_TEMPLATES[key]
