from __future__ import annotations

from typing import Any

from .stage_asset_materialization import StageOutputAssetMaterializer, _clean, _name_key, _norm_name


_LEGACY_VISUAL_KIND = {
    "location": "location",
    "place": "location",
    "scene": "location",
    "setting": "location",
    "prop": "prop",
    "artifact": "prop",
    "item": "prop",
    "object": "prop",
}

_LOCATION_MARKERS = (
    "空间", "边界", "布局", "地形", "建筑", "材质", "陈设", "前景", "中景", "后景", "锚点", "参考图",
)
_PROP_MARKERS = (
    "轮廓", "比例", "结构", "材质", "颜色", "纹样", "磨损", "尺度", "功能", "参考图",
)


def _marker_count(kind: str, text: str) -> int:
    markers = _LOCATION_MARKERS if kind == "location" else _PROP_MARKERS
    return sum(1 for marker in markers if marker in str(text or ""))


def install_stage_visual_asset_alias_recovery() -> None:
    """Recover old story entity types into Stage③ canonical visual assets.

    Older story extraction used ``scene`` for reusable places and sometimes
    ``artifact/item/object`` for props. Stage③ owns the decision whether those
    facts become durable visual assets. We therefore only promote an alias when
    the ready Stage③ draft actually mentions the entity and its nearby context
    contains enough stable visual structure. Narrative scene records with no
    reusable spatial design remain narrative records and are never promoted.
    """
    cls = StageOutputAssetMaterializer
    if getattr(cls, "_xiaoduan_visual_alias_recovery_installed", False):
        return

    original_extract = cls._extract

    def extract(self: StageOutputAssetMaterializer, project_id: str, stage: str, text: str):
        rows = [dict(row) for row in original_extract(self, project_id, stage, text)]
        if _clean(stage) != "03":
            return rows

        blocks = self._heading_blocks(text)
        found = {(str(row.get("kind") or ""), _name_key(row.get("name"))) for row in rows}

        for entity in self.production.list_entities(project_id):
            raw_kind = _clean(entity.get("entity_type")).lower()
            kind = _LEGACY_VISUAL_KIND.get(raw_kind, "")
            if kind not in {"location", "prop"}:
                continue
            name = _norm_name(entity.get("name"))
            if not name or self._looks_like_field_heading(name) or name not in str(text or ""):
                continue
            key = (kind, _name_key(name))
            if key in found:
                continue

            context = self._context_for_name(text, name, blocks)
            if not context:
                continue
            # A legacy alias is promoted only when Stage③ actually contains a
            # sufficiently rich stable design around it. This prevents a plot
            # scene title from becoming a second location merely by name match.
            if self._body_score(kind, context) < 3 or _marker_count(kind, context) < 3:
                continue

            rows.append({
                "kind": kind,
                "name": name,
                "design": context[:7000],
                "recovered_from_legacy_type": raw_kind,
            })
            found.add(key)

        return rows

    cls._extract = extract
    cls._xiaoduan_visual_alias_recovery_installed = True


__all__ = ["install_stage_visual_asset_alias_recovery"]
