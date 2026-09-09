from __future__ import annotations

import hashlib
import re
from typing import Any


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm_name(value: Any) -> str:
    text = _clean(value)
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    text = re.sub(r"^[\s#>*_`\-\d.、（）()]+", "", text)
    text = re.sub(r"[\s*_`：:，,。；;]+$", "", text)
    return text.strip()


def _name_key(value: Any) -> str:
    return re.sub(r"\s+", "", _norm_name(value)).casefold()


_KIND_STAGE = {"character": "02", "location": "03", "prop": "03"}
_STOP_NAMES = {
    "角色", "角色资产", "角色设定", "角色档案", "角色设计", "角色资产包",
    "地点", "地点资产", "地点设定", "场景", "场景资产", "场景设定",
    "道具", "道具资产", "道具设定", "视觉圣经", "项目视觉圣经",
    "角色参考图生成要求", "参考图生成要求", "形象版本", "形象版本列表",
    "原文事实", "设计补全", "稳定身份", "完成条件", "质量规则",
}


class StageOutputAssetMaterializer:
    """Turn a ready Stage②/③ authoring draft into durable reusable assets.

    This is deliberately local and deterministic: it consumes the already
    generated stage text and never calls an LLM. New Xiaoduan Skills are asked
    to emit explicit ``角色资产：名字`` / ``地点资产：名字`` /
    ``道具资产：名字`` headings, while older drafts are recovered through
    conservative heading/field heuristics and existing story entities.
    """

    def __init__(self, legacy_runtime: Any) -> None:
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.production = self.director.production

    @staticmethod
    def _stage_ready(project: dict[str, Any], stage: str) -> bool:
        completed = {_clean(x) for x in project.get("completed_stages") or []}
        if stage in completed:
            return True
        if _clean(project.get("current_stage")) != stage:
            return False
        state = ((project.get("stage_state") or {}).get(stage) or {})
        runtime = state.get("skill_runtime") if isinstance(state.get("skill_runtime"), dict) else {}
        completion = runtime.get("completion") if isinstance(runtime.get("completion"), dict) else {}
        return bool(state.get("stage_ready")) or bool(completion.get("ready"))

    def _stage_text(self, project: dict[str, Any], stage: str) -> str:
        getter = getattr(self.director, "_latest_stage_output", None)
        if callable(getter):
            try:
                text = _clean(getter(project, stage))
                if text:
                    return text
            except Exception:
                pass
        confirmed = ((project.get("confirmed_outputs") or {}).get(stage) or {})
        return _clean(confirmed.get("handoff"))

    @staticmethod
    def _heading_blocks(text: str) -> list[tuple[str, str]]:
        matches = list(re.finditer(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", text))
        result: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            title = _norm_name(match.group(1))
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            if title:
                result.append((title, body))
        return result

    @staticmethod
    def _explicit_kind_name(title: str) -> tuple[str, str] | None:
        rules = (
            ("character", r"^角色(?:资产|设定|档案|设计)?(?:\s*\d+)?\s*[：:·\-]\s*(.+)$"),
            ("location", r"^(?:地点|场景)(?:资产|设定|设计)?(?:\s*\d+)?\s*[：:·\-]\s*(.+)$"),
            ("prop", r"^道具(?:资产|设定|设计)?(?:\s*\d+)?\s*[：:·\-]\s*(.+)$"),
        )
        for kind, pattern in rules:
            match = re.match(pattern, title, flags=re.I)
            if match:
                return kind, _norm_name(match.group(1))
        return None

    @staticmethod
    def _simple_heading_name(title: str) -> str:
        value = _norm_name(title)
        if value in _STOP_NAMES or len(value) < 2 or len(value) > 24:
            return ""
        if any(token in value for token in ("必须", "原则", "规则", "说明", "要求", "列表", "项目")):
            return ""
        if re.search(r"[。！？!?/\\]", value):
            return ""
        return value

    @staticmethod
    def _body_score(kind: str, body: str) -> int:
        if kind == "character":
            words = ("年龄", "身份", "脸", "面部", "发型", "发色", "肤色", "体型", "服装", "鞋", "配色", "配饰", "造型", "参考图")
        elif kind == "location":
            words = ("空间", "布局", "地形", "建筑", "材质", "陈设", "前景", "中景", "后景", "锚点", "天气", "参考图")
        else:
            words = ("轮廓", "比例", "结构", "材质", "颜色", "纹样", "磨损", "尺度", "功能", "参考图")
        return sum(1 for word in words if word in body)

    @staticmethod
    def _field_names(text: str, kind: str) -> list[str]:
        if kind == "character":
            labels = ("角色名称", "姓名")
        elif kind == "location":
            labels = ("地点名称", "场景名称")
        else:
            labels = ("道具名称",)
        values: list[str] = []
        for label in labels:
            for match in re.finditer(rf"(?m)^\s*(?:[-*]\s*)?{label}\s*[：:]\s*([^\n，,；;]{{1,30}})", text):
                name = _norm_name(match.group(1))
                if name and name not in _STOP_NAMES:
                    values.append(name)
        return values

    @staticmethod
    def _context_for_name(text: str, name: str, blocks: list[tuple[str, str]]) -> str:
        key = _name_key(name)
        for title, body in blocks:
            if _name_key(title) == key or key in _name_key(title):
                return (f"{title}\n{body}" if body else title).strip()[:7000]
        pos = text.find(name)
        if pos < 0:
            return ""
        start = max(0, pos - 250)
        end = min(len(text), pos + 2200)
        return text[start:end].strip()

    def _extract(self, project_id: str, stage: str, text: str) -> list[dict[str, str]]:
        allowed = {"character"} if stage == "02" else {"location", "prop"}
        blocks = self._heading_blocks(text)
        found: dict[tuple[str, str], dict[str, str]] = {}

        for title, body in blocks:
            explicit = self._explicit_kind_name(title)
            if explicit and explicit[0] in allowed:
                kind, name = explicit
                if name and name not in _STOP_NAMES:
                    found[(kind, _name_key(name))] = {
                        "kind": kind,
                        "name": name,
                        "design": (f"{title}\n{body}" if body else title).strip()[:7000],
                    }
                    continue

            simple = self._simple_heading_name(title)
            if not simple:
                continue
            if stage == "02" and self._body_score("character", body) >= 3:
                found[("character", _name_key(simple))] = {
                    "kind": "character", "name": simple,
                    "design": (f"{title}\n{body}" if body else title).strip()[:7000],
                }
            elif stage == "03":
                location_score = self._body_score("location", body)
                prop_score = self._body_score("prop", body)
                if max(location_score, prop_score) >= 3:
                    kind = "location" if location_score >= prop_score else "prop"
                    found[(kind, _name_key(simple))] = {
                        "kind": kind, "name": simple,
                        "design": (f"{title}\n{body}" if body else title).strip()[:7000],
                    }

        for kind in allowed:
            for name in self._field_names(text, kind):
                key = (kind, _name_key(name))
                found.setdefault(key, {
                    "kind": kind,
                    "name": name,
                    "design": self._context_for_name(text, name, blocks),
                })

        # Existing story entities are useful anchors for legacy drafts that did
        # not yet use the explicit materialization headings.
        for entity in self.production.list_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            if kind == "scene" and stage == "03":
                continue
            if kind not in allowed:
                continue
            name = _norm_name(entity.get("name"))
            if not name or name not in text:
                continue
            key = (kind, _name_key(name))
            context = self._context_for_name(text, name, blocks)
            if context:
                found.setdefault(key, {"kind": kind, "name": name, "design": context})

        return [row for row in found.values() if _name_key(row.get("name"))]

    def _upsert(self, project_id: str, stage: str, row: dict[str, str]) -> dict[str, Any]:
        kind = row["kind"]
        name = _norm_name(row["name"])
        design = _clean(row.get("design"))
        existing = None
        wanted = _name_key(name)
        for entity in self.production.list_entities(project_id):
            if _clean(entity.get("entity_type")).lower() != kind:
                continue
            if _name_key(entity.get("name")) == wanted:
                existing = entity
                break

        metadata = {
            "authoring": {
                "stable_design": design,
                "source_stage": stage,
                "materialized_from_stage_output": True,
            },
            "continuity": {
                "core_profile": {"阶段正式设定": design},
                "default_state": {},
            },
        }
        if existing:
            current = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
            merged = dict(current)
            for key, value in metadata.items():
                base = merged.get(key) if isinstance(merged.get(key), dict) else {}
                base = dict(base)
                base.update(value)
                if key == "continuity":
                    old_core = base.get("core_profile") if isinstance(base.get("core_profile"), dict) else {}
                    core = dict(old_core)
                    core["阶段正式设定"] = design
                    base["core_profile"] = core
                merged[key] = base
            return self.production.update_entity(project_id, _clean(existing.get("entity_id")), {"stage": stage, "skill": "xiaoduan-stage-asset-materializer", "metadata": merged})

        suffix = hashlib.sha1(f"{kind}:{wanted}".encode("utf-8")).hexdigest()[:12]
        return self.production.create_entity(
            project_id,
            entity_type=kind,
            name=name,
            logical_key=f"xiaoduan:{kind}:{suffix}",
            stage=stage,
            skill="xiaoduan-stage-asset-materializer",
            metadata=metadata,
            evidence={"source_stage": stage, "type": "ready_stage_output"},
        )

    def materialize(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        created_or_updated: list[str] = []
        stages: list[str] = []
        for stage in ("02", "03"):
            if not self._stage_ready(project, stage):
                continue
            text = self._stage_text(project, stage)
            if not text:
                continue
            rows = self._extract(project_id, stage, text)
            if not rows:
                continue
            stages.append(stage)
            for row in rows:
                entity = self._upsert(project_id, stage, row)
                entity_id = _clean(entity.get("entity_id"))
                if entity_id:
                    created_or_updated.append(entity_id)
        return {
            "project_id": project_id,
            "materialized": bool(created_or_updated),
            "stages": stages,
            "entity_ids": list(dict.fromkeys(created_or_updated)),
            "model_calls": 0,
        }


__all__ = ["StageOutputAssetMaterializer"]
