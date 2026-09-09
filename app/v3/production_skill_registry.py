from __future__ import annotations

from types import MethodType
from typing import Any

from app.services import director as director_module
from app.services.production_skills import (
    STAGE_PRODUCTION_SKILLS,
    WORKFLOW_PRODUCTION_SKILL,
    builtin_production_skill,
    is_builtin_production_skill,
)


class ProductionSkillRegistry:
    """Make Xiaoduan's native production Skills authoritative at runtime.

    The legacy workbench still owns routing/UI/project storage, but the content
    generator no longer reads the old monolithic workflow as its production
    source.  Built-in Skills have no external reference files, so only their
    exact checked-in definitions participate in contract compilation.
    """

    def __init__(self, director: Any) -> None:
        self.director = director
        self._original_skill_md = getattr(director, "_skill_md")
        self._original_available_files = getattr(director, "_available_files")
        self._original_read_source_file = getattr(director, "_read_source_file")
        self._original_source_status = getattr(director, "source_status")

    def install(self) -> None:
        if getattr(self.director, "_xiaoduan_native_production_skills_installed", False):
            return

        # DirectorService resolves these globals at call time, so replacing the
        # module-level registry switches the real stage runtime without forking
        # the legacy project/session implementation.
        director_module.STAGE_SKILLS = dict(STAGE_PRODUCTION_SKILLS)
        director_module.WORKFLOW_SKILL = WORKFLOW_PRODUCTION_SKILL

        original_skill_md = self._original_skill_md
        original_available_files = self._original_available_files
        original_read_source_file = self._original_read_source_file
        original_source_status = self._original_source_status

        def skill_md(instance: Any, skill_name: str) -> str:
            if is_builtin_production_skill(skill_name):
                return builtin_production_skill(skill_name)
            return original_skill_md(skill_name)

        def available_files(instance: Any, skill_name: str) -> list[str]:
            if is_builtin_production_skill(skill_name):
                return []
            return original_available_files(skill_name)

        def read_source_file(instance: Any, skill_name: str, relative: str) -> str:
            if is_builtin_production_skill(skill_name):
                raise FileNotFoundError("内置生产技能没有外部引用文件")
            return original_read_source_file(skill_name, relative)

        def source_status(instance: Any) -> dict[str, Any]:
            try:
                value = original_source_status()
            except Exception:
                value = {}
            checks = {
                WORKFLOW_PRODUCTION_SKILL: True,
                **{name: True for name in STAGE_PRODUCTION_SKILLS.values()},
            }
            runtime = dict(value.get("skill_runtime") or {}) if isinstance(value, dict) else {}
            runtime.update({
                "native_production_skills": True,
                "story_bible": True,
                "typed_stage_boundaries": True,
                "external_workflow_required": False,
            })
            return {
                "ready": True,
                "manifest": {
                    "schema_version": "xiaoduan-production-skills-v1",
                    "workflow_skill": WORKFLOW_PRODUCTION_SKILL,
                    "stage_order": [
                        {"stage": stage, "skill": skill}
                        for stage, skill in STAGE_PRODUCTION_SKILLS.items()
                    ],
                },
                "checks": checks,
                "skill_runtime": runtime,
            }

        self.director._skill_md = MethodType(skill_md, self.director)
        self.director._available_files = MethodType(available_files, self.director)
        self.director._read_source_file = MethodType(read_source_file, self.director)
        self.director.source_status = MethodType(source_status, self.director)
        self.director._xiaoduan_native_production_skills_installed = True


__all__ = ["ProductionSkillRegistry"]
