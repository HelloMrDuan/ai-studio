from __future__ import annotations

from collections import OrderedDict

from .contracts import Capability, CreativePlan, SkillSpec


BUILTIN_SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        skill_id="screenplay",
        version="3.0.0",
        domain="story",
        input_schema="xiaoduan.screenplay.input.v1",
        output_schema="xiaoduan.screenplay.output.v1",
        required_capabilities={Capability.text, Capability.structured_output},
        allowed_operations={"read_story", "write_screenplay"},
        owned_fields={"story_structure", "screenplay"},
    ),
    SkillSpec(
        skill_id="character",
        version="3.0.0",
        domain="visual_design",
        input_schema="xiaoduan.character.input.v1",
        output_schema="xiaoduan.character.output.v1",
        required_capabilities={Capability.text, Capability.structured_output},
        allowed_operations={"read_screenplay", "write_character"},
        owned_fields={"character_identity", "character_visual_baseline"},
    ),
    SkillSpec(
        skill_id="world",
        version="3.0.0",
        domain="visual_design",
        input_schema="xiaoduan.world.input.v1",
        output_schema="xiaoduan.world.output.v1",
        required_capabilities={Capability.text, Capability.structured_output},
        allowed_operations={"read_screenplay", "write_world"},
        owned_fields={"world_identity", "location_identity", "visual_style"},
    ),
    SkillSpec(
        skill_id="storyboard",
        version="3.0.0",
        domain="storyboard",
        input_schema="xiaoduan.storyboard.input.v1",
        output_schema="xiaoduan.storyboard.output.v1",
        required_capabilities={Capability.text, Capability.structured_output},
        allowed_operations={"read_screenplay", "read_entities", "write_shot_contract"},
        owned_fields={"beats", "evidence", "shot_contracts"},
    ),
    SkillSpec(
        skill_id="cinematography",
        version="3.0.0",
        domain="storyboard",
        input_schema="xiaoduan.cinematography.input.v1",
        output_schema="xiaoduan.cinematography.output.v1",
        required_capabilities={Capability.text, Capability.structured_output},
        allowed_operations={"read_shot_contract", "write_camera_direction"},
        owned_fields={"camera", "composition", "lens", "camera_motion"},
        optional_dependencies=("storyboard",),
    ),
    SkillSpec(
        skill_id="continuity",
        version="3.0.0",
        domain="continuity",
        input_schema="xiaoduan.continuity.input.v1",
        output_schema="xiaoduan.continuity.output.v1",
        required_capabilities={Capability.structured_output},
        allowed_operations={"read_entities", "read_resources", "write_continuity_contract"},
        owned_fields={"entity_locks", "reference_contract", "continuity_revision"},
    ),
    SkillSpec(
        skill_id="image_direction",
        version="3.0.0",
        domain="image",
        input_schema="xiaoduan.image.input.v1",
        output_schema="xiaoduan.image.output.v1",
        required_capabilities={Capability.image_generation},
        allowed_operations={"read_shot_contract", "read_references", "submit_image_task"},
        owned_fields={"image_generation_contract"},
        dependencies=("continuity",),
        optional_dependencies=("cinematography",),
    ),
    SkillSpec(
        skill_id="video_direction",
        version="3.0.0",
        domain="video",
        input_schema="xiaoduan.video.input.v1",
        output_schema="xiaoduan.video.output.v1",
        required_capabilities={Capability.video_generation},
        allowed_operations={"read_shot_contract", "read_references", "submit_video_task"},
        owned_fields={"video_generation_contract"},
        dependencies=("continuity",),
        optional_dependencies=("cinematography",),
    ),
    SkillSpec(
        skill_id="audio_direction",
        version="3.0.0",
        domain="audio",
        input_schema="xiaoduan.audio.input.v1",
        output_schema="xiaoduan.audio.output.v1",
        required_capabilities={Capability.tts},
        allowed_operations={"read_screenplay", "submit_audio_task"},
        owned_fields={"voice_plan", "sfx_plan", "music_plan", "subtitle_plan"},
    ),
    SkillSpec(
        skill_id="quality_audit",
        version="3.0.0",
        domain="quality",
        input_schema="xiaoduan.audit.input.v1",
        output_schema="xiaoduan.audit.output.v1",
        required_capabilities={Capability.structured_output},
        allowed_operations={"read_project", "write_audit_result"},
        owned_fields={"audit_result"},
    ),
)


class SkillRegistry:
    """Authoritative V3 skill registry.

    The director chooses skill IDs. The registry owns dependency expansion and
    ordering; there is deliberately no hard-coded Stage01 -> Stage04 route.
    """

    def __init__(self, skills: tuple[SkillSpec, ...] = BUILTIN_SKILLS) -> None:
        self._skills = OrderedDict((skill.skill_id, skill) for skill in skills)
        if len(self._skills) != len(skills):
            raise ValueError("duplicate skill_id in registry")
        self._validate_dependencies()

    def _validate_dependencies(self) -> None:
        known = set(self._skills)
        for skill in self._skills.values():
            unknown = set(skill.dependencies) - known
            if unknown:
                raise ValueError(f"{skill.skill_id} has unknown dependencies: {sorted(unknown)}")

    def get(self, skill_id: str) -> SkillSpec:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise KeyError(f"unknown Xiaoduan skill: {skill_id}") from exc

    def list(self) -> tuple[SkillSpec, ...]:
        return tuple(self._skills.values())

    def build_plan(self, requested_skills: list[str] | tuple[str, ...]) -> CreativePlan:
        requested = tuple(dict.fromkeys(str(item).strip() for item in requested_skills if str(item).strip()))
        if not requested:
            raise ValueError("at least one professional skill is required")
        for skill_id in requested:
            self.get(skill_id)

        visiting: set[str] = set()
        visited: set[str] = set()
        ordered: list[str] = []

        def visit(skill_id: str) -> None:
            if skill_id in visited:
                return
            if skill_id in visiting:
                raise ValueError(f"cyclic skill dependency involving {skill_id}")
            visiting.add(skill_id)
            for dependency in self.get(skill_id).dependencies:
                visit(dependency)
            visiting.remove(skill_id)
            visited.add(skill_id)
            ordered.append(skill_id)

        for skill_id in requested:
            visit(skill_id)
        return CreativePlan(requested_skills=requested, execution_order=tuple(ordered))
