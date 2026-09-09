from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted({_clean(item) for item in value if _clean(item)}))


def _appearance_signature(shot: dict[str, Any]) -> tuple[str, ...]:
    # New native storyboard output may use one of these names; accepting all
    # keeps older formal shots compatible while the front half migrates.
    for field in (
        "character_appearance_asset_ids",
        "character_appearance_ids",
        "appearance_version_ids",
        "appearance_ids",
    ):
        values = _ids(shot.get(field))
        if values:
            return values
    mapping = shot.get("character_appearances")
    if isinstance(mapping, dict):
        return tuple(sorted(f"{_clean(k)}={_clean(v)}" for k, v in mapping.items() if _clean(k) and _clean(v)))
    return ()


def _order(shot: dict[str, Any], index: int) -> tuple[int, int]:
    try:
        global_order = int(shot.get("global_order") or 0)
    except Exception:
        global_order = 0
    try:
        local_order = int(shot.get("order") or shot.get("shot_order") or 0)
    except Exception:
        local_order = 0
    return (global_order if global_order > 0 else 10_000_000 + index, local_order)


_TIME_JUMP = re.compile(
    r"(?:次日|翌日|第二天|数日后|几天后|多年后|几年后|数月后|几个月后|"
    r"与此同时|另一时间|回忆|闪回|梦境|清晨转为|白天转为|夜晚转为|"
    r"later that day|next day|days later|years later|flashback|time jump)",
    re.I,
)


class ShotContinuityLinker:
    """Deterministically connect adjacent shots that can safely inherit state.

    This is intentionally cheaper than an LLM audit.  It only links when scene,
    visible character set and explicit appearance-version set are compatible and
    no clear time jump exists.  It never overwrites an explicit start state.
    """

    def __init__(self, settings: Any, legacy_runtime: Any) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.production = self.director.production
        self.root = Path(settings.data_dir) / "story_continuity"
        self._original_confirm = None

    def _path(self, project_id: str) -> Path:
        self.director.get_project(project_id)
        return self.root / f"{project_id}.json"

    def _load(self, project_id: str) -> dict[str, Any]:
        path = self._path(project_id)
        if not path.is_file():
            raise FileNotFoundError("当前作品还没有正式分镜连续性数据")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("正式分镜连续性数据格式无效")
        return value

    def _save(self, project_id: str, state: dict[str, Any]) -> None:
        path = self._path(project_id)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _time_text(shot: dict[str, Any]) -> str:
        return " ".join(
            _clean(shot.get(field))
            for field in ("title", "summary", "environment", "action", "time", "time_of_day")
            if _clean(shot.get(field))
        )

    def _decision(self, previous: dict[str, Any], current: dict[str, Any]) -> tuple[bool, str]:
        explicit = current.get("continuity_break")
        if explicit is True or _clean(explicit).lower() in {"true", "yes", "1", "break"}:
            return False, "镜头明确要求断开连续链"

        prev_scene = _clean(previous.get("scene_id"))
        curr_scene = _clean(current.get("scene_id"))
        if prev_scene and curr_scene and prev_scene != curr_scene:
            return False, "地点/场次发生变化"

        prev_chars = _ids(previous.get("character_entity_ids"))
        curr_chars = _ids(current.get("character_entity_ids"))
        if prev_chars != curr_chars:
            return False, "可见人物集合发生变化"

        prev_looks = _appearance_signature(previous)
        curr_looks = _appearance_signature(current)
        if prev_looks and curr_looks and prev_looks != curr_looks:
            return False, "角色形象版本发生变化"

        if _TIME_JUMP.search(self._time_text(current)):
            return False, "检测到明确时间跳跃"

        return True, "同地点、同人物集合、无明确时间/形象断点"

    def link(self, project_id: str) -> dict[str, Any]:
        state = self._load(project_id)
        shots = [row for row in state.get("shots") or [] if isinstance(row, dict) and not bool(row.get("provisional"))]
        shots.sort(key=lambda pair: _order(pair, shots.index(pair) if pair in shots else 0))
        linked = 0
        breaks = 0
        inherited = 0
        records: list[dict[str, Any]] = []

        previous: dict[str, Any] | None = None
        for shot in shots:
            shot_id = _clean(shot.get("shot_id"))
            if previous is None:
                shot["continuity_link"] = {
                    "mode": "root",
                    "reason": "连续镜头链起点",
                }
                previous = shot
                continue

            can_link, reason = self._decision(previous, shot)
            prev_id = _clean(previous.get("shot_id"))
            if can_link:
                end_state = _clean(previous.get("video_end_state"))
                start_state = _clean(shot.get("video_start_state"))
                if not start_state and end_state:
                    shot["video_start_state"] = end_state
                    inherited += 1
                shot["continuity_link"] = {
                    "mode": "inherit",
                    "from_shot_id": prev_id,
                    "reason": reason,
                    "inherited_previous_end_state": bool(not start_state and end_state),
                    "previous_end_state": end_state,
                }
                linked += 1
            else:
                shot["continuity_link"] = {
                    "mode": "break",
                    "from_shot_id": prev_id,
                    "reason": reason,
                }
                breaks += 1
            records.append({
                "shot_id": shot_id,
                "previous_shot_id": prev_id,
                "mode": shot["continuity_link"]["mode"],
                "reason": reason,
            })
            previous = shot

        state["shot_continuity_links"] = records
        state["continuity_linker"] = {
            "schema_version": "xiaoduan_shot_continuity_link_v1",
            "linked_count": linked,
            "break_count": breaks,
            "inherited_start_state_count": inherited,
            "deterministic_first": True,
        }
        self._save(project_id, state)

        if records:
            self.production.create_text_asset(
                project_id,
                stage="04",
                skill="xiaoduan-shot-continuity-linker",
                logical_key="studio:stage04:shot-continuity-links",
                asset_role="shot_continuity_links",
                name="镜头连续性链",
                content=json.dumps({
                    "schema_version": "xiaoduan_shot_continuity_link_v1",
                    "records": records,
                }, ensure_ascii=False, indent=2),
                asset_type="STRUCTURED_DATA",
                extension=".json",
                source={"type": "deterministic_shot_continuity_linker"},
                metadata={"deterministic_audit": True},
            )
        return {
            "project_id": project_id,
            "linked_count": linked,
            "break_count": breaks,
            "inherited_start_state_count": inherited,
            "records": records,
        }

    def install_confirmation_hook(self) -> None:
        if getattr(self.director, "_xiaoduan_shot_continuity_linker_installed", False):
            return
        original = self.director.confirm_stage
        self._original_confirm = original

        async def wrapped(project_id: str):
            before = self.director.get_project(project_id)
            stage = _clean(before.get("current_stage"))
            result = await original(project_id)
            if stage == "04":
                self.link(project_id)
            return result

        self.director.confirm_stage = wrapped
        self.director._xiaoduan_shot_continuity_linker_installed = True


__all__ = ["ShotContinuityLinker"]
