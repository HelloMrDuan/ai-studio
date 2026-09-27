from __future__ import annotations

from typing import Any

from app.v3.character_reference_hardening import install_character_reference_hardening
from app.v3.character_reference_package import CharacterReferencePackageBootstrap
from app.v3.character_visual_continuity import install_character_visual_continuity


def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def install_character_package_integrity() -> dict[str, Any]:
    """Publish a character reference package only after every component is adopted.

    The package is a frozen downstream contract, not a progress marker. A planned
    or stale turnaround must never appear as a ready package component.

    This installation point is also the production entry for the phase-specific
    character reference hardening. Tests used to install that policy explicitly,
    while the real web process did not, which meant the verified face/costume
    prompt guards were dead in production. Install hardening first so visual
    continuity wraps the hardened prompts instead of the other way around.
    """
    reference_hardening = install_character_reference_hardening()
    visual_continuity = install_character_visual_continuity()
    current = CharacterReferencePackageBootstrap._reference_package
    if getattr(current, "_xiaoduan_package_integrity", False):
        return {
            "installed": True,
            "policy": "all_components_ready_and_fresh",
            "reference_hardening": reference_hardening,
            "visual_continuity": visual_continuity,
        }
    original = current

    def guarded(
        self: CharacterReferencePackageBootstrap,
        project_id: str,
        entity: dict[str, Any],
        *,
        face: dict[str, Any],
        costume: dict[str, Any],
        turnaround: dict[str, Any],
    ) -> dict[str, Any]:
        components = {
            "face_anchor": face,
            "costume_reference": costume,
            "turnaround": turnaround,
        }
        invalid = [
            name
            for name, asset in components.items()
            if _clean(asset.get("status")) != "ready"
            or _clean(asset.get("dependency_state")) == "stale"
        ]
        if invalid:
            # generate_candidate intentionally probes package assembly before the
            # final turnaround is adopted. Do not persist a false ready package;
            # status() will assemble it once the adopted turnaround is ready.
            return {}
        return original(
            self,
            project_id,
            entity,
            face=face,
            costume=costume,
            turnaround=turnaround,
        )

    setattr(guarded, "_xiaoduan_package_integrity", True)
    CharacterReferencePackageBootstrap._reference_package = guarded
    return {
        "installed": True,
        "policy": "all_components_ready_and_fresh",
        "reference_hardening": reference_hardening,
        "visual_continuity": visual_continuity,
    }


__all__ = ["install_character_package_integrity"]
