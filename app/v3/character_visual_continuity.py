from __future__ import annotations

from typing import Any

from .character_reference_package import CharacterReferencePackageBootstrap


_MARKER = "【角色外观连续性硬约束】"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _typed_contract(entity: dict[str, Any]) -> dict[str, Any]:
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    direct = metadata.get("typed_character_contract")
    if isinstance(direct, dict) and direct:
        return direct
    stable_profile = metadata.get("stable_profile") if isinstance(metadata.get("stable_profile"), dict) else {}
    for key in ("专业角色合同", "typed_character_contract", "character_contract"):
        value = stable_profile.get(key)
        if isinstance(value, dict) and value:
            return value
    authoring = metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {}
    value = authoring.get("typed_character_contract")
    return value if isinstance(value, dict) else {}


def _continuity_clause(entity: dict[str, Any]) -> str:
    if _clean(entity.get("entity_type")).lower() != "character":
        return ""
    contract = _typed_contract(entity)
    clothing = _clean(contract.get("服装") or contract.get("clothing"))
    footwear = _clean(contract.get("鞋履") or contract.get("footwear"))
    anchors = contract.get("固定身份锚点") or contract.get("fixed_identity_anchors") or []
    if not isinstance(anchors, list):
        anchors = [anchors]
    anchors = [_clean(item) for item in anchors if _clean(item)]
    facts: list[str] = []
    if clothing:
        facts.append(f"服装={clothing}")
    if footwear:
        facts.append(f"鞋履={footwear}")
    if anchors:
        facts.append("固定身份锚点=" + "；".join(anchors[:12]))
    if not facts:
        return ""
    return (
        f"{_MARKER}\n"
        + "；".join(facts)
        + "。以上文字合同优先于任何已生成参考图中的偶然服装、配色或鞋履偏差。"
        "脸部参考只负责锁定身份、年龄、五官与发型，不得把参考图中错误的衣服颜色或款式传递到后续阶段。"
        "后续服装定装图和三视图必须继续使用同一已确认服装、配色、鞋履与固定身份锚点；"
        "不得擅自改成任何未确认颜色、现代服饰或其他造型。"
    )


def _append_clause(text: str, entity: dict[str, Any]) -> str:
    base = _clean(text)
    clause = _continuity_clause(entity)
    if not clause or _MARKER in base:
        return base
    return f"{base}\n\n{clause}" if base else clause


def install_character_visual_continuity() -> dict[str, Any]:
    cls = CharacterReferencePackageBootstrap
    if getattr(cls, "_xiaoduan_character_visual_continuity_v1", False):
        return {"installed": True, "status": "already_installed", "policy": "typed_wardrobe_over_reference_pixels"}

    original_face_prompt = cls._face_prompt
    original_costume_prompt = cls._costume_prompt
    original_reference_prompt = cls._reference_prompt
    original_ensure_target = cls._ensure_target_and_prompt

    def face_prompt(self: CharacterReferencePackageBootstrap, entity: dict[str, Any]) -> str:
        return _append_clause(original_face_prompt(self, entity), entity)

    def costume_prompt(self: CharacterReferencePackageBootstrap, entity: dict[str, Any]) -> str:
        return _append_clause(original_costume_prompt(self, entity), entity)

    def reference_prompt(self: CharacterReferencePackageBootstrap, entity: dict[str, Any]) -> str:
        return _append_clause(original_reference_prompt(self, entity), entity)

    def ensure_target_and_prompt(
        self: CharacterReferencePackageBootstrap,
        project_id: str,
        entity: dict[str, Any],
        *,
        prompt_override: str = "",
    ):
        requested = _append_clause(prompt_override, entity) if _clean(prompt_override) else ""
        return original_ensure_target(
            self,
            project_id,
            entity,
            prompt_override=requested,
        )

    cls._face_prompt = face_prompt
    cls._costume_prompt = costume_prompt
    cls._reference_prompt = reference_prompt
    cls._ensure_target_and_prompt = ensure_target_and_prompt
    cls._xiaoduan_character_visual_continuity_v1 = True
    return {
        "installed": True,
        "status": "installed",
        "policy": "typed_wardrobe_over_reference_pixels",
        "face_reference_scope": "identity_only_when_pixels_conflict_with_typed_wardrobe",
        "costume_and_turnaround": "same_confirmed_clothing_color_footwear_anchors",
        "prompt_override_preserves_contract": True,
    }


__all__ = [
    "_append_clause",
    "_continuity_clause",
    "install_character_visual_continuity",
]
