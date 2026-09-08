from __future__ import annotations

import unittest

from app.v3.context_resolver import ContextResolver
from app.v3.continuity import ContinuityError, ContinuityRegistry, assert_required_entities_visible
from app.v3.contracts import (
    Capability,
    EntityKind,
    ProviderModelSpec,
    ProviderTransport,
    ResolutionSource,
    VisualEntity,
)
from app.v3.provider_gateway import ProviderRegistry, ProviderResolutionError
from app.v3.skill_registry import SkillRegistry


class XiaoduanV3FoundationTests(unittest.TestCase):
    def test_skill_plan_is_dynamic_not_fixed_stage_0104(self) -> None:
        plan = SkillRegistry().build_plan(["storyboard", "image_direction"])
        self.assertEqual(plan.requested_skills, ("storyboard", "image_direction"))
        self.assertEqual(plan.execution_order, ("storyboard", "continuity", "image_direction"))
        self.assertNotIn("screenplay", plan.execution_order)

    def test_context_default_is_chinese_only_when_unresolved(self) -> None:
        resolved = ContextResolver().resolve_cultural_context("少年在雪山寻找失落古剑")
        self.assertEqual(resolved.value, "Chinese")
        self.assertEqual(resolved.source, ResolutionSource.project_default)

    def test_context_infers_japanese_from_story_scope(self) -> None:
        resolved = ContextResolver().resolve_cultural_context("东京高中生放学后走进神社")
        self.assertEqual(resolved.value, "Japanese")
        self.assertEqual(resolved.source, ResolutionSource.context_inferred)

    def test_context_user_override_has_highest_precedence(self) -> None:
        resolved = ContextResolver().resolve_cultural_context(
            "东京高中生放学后走进神社",
            user_override="Chinese",
        )
        self.assertEqual(resolved.value, "Chinese")
        self.assertEqual(resolved.source, ResolutionSource.user_override)

    def test_locked_context_is_not_reinferred_per_shot(self) -> None:
        resolver = ContextResolver()
        locked = resolver.resolve_cultural_context("少年在雪山寻找失落古剑")
        resolved = resolver.resolve_cultural_context("下一镜头出现在东京", previous_locked=locked)
        self.assertEqual(resolved, locked)

    def test_remote_and_local_providers_are_peers(self) -> None:
        registry = ProviderRegistry(
            [
                ProviderModelSpec(
                    provider_id="remote",
                    model_id="img-api",
                    transport=ProviderTransport.remote_api,
                    capabilities={Capability.image_generation},
                    priority=50,
                ),
                ProviderModelSpec(
                    provider_id="local",
                    model_id="comfy",
                    transport=ProviderTransport.local_comfyui,
                    capabilities={Capability.image_generation, Capability.multi_reference},
                    max_references=4,
                    priority=10,
                ),
            ]
        )
        selected = registry.resolve({Capability.image_generation})
        self.assertEqual(selected.spec.provider_id, "local")

    def test_explicit_provider_never_silently_falls_back(self) -> None:
        registry = ProviderRegistry(
            [
                ProviderModelSpec(
                    provider_id="remote",
                    model_id="video",
                    transport=ProviderTransport.remote_api,
                    capabilities={Capability.video_generation},
                    healthy=False,
                ),
                ProviderModelSpec(
                    provider_id="local",
                    model_id="video",
                    transport=ProviderTransport.local_http,
                    capabilities={Capability.video_generation},
                ),
            ]
        )
        with self.assertRaises(ProviderResolutionError):
            registry.resolve(
                {Capability.video_generation},
                provider_id="remote",
                model_id="video",
            )

    def test_multi_reference_capability_fails_closed(self) -> None:
        registry = ProviderRegistry(
            [
                ProviderModelSpec(
                    provider_id="api",
                    model_id="image",
                    transport=ProviderTransport.remote_api,
                    capabilities={Capability.image_generation},
                )
            ]
        )
        selected = registry.resolve({Capability.image_generation})
        with self.assertRaises(ProviderResolutionError):
            registry.assert_reference_budget(selected, 2)

    def test_continuity_protects_identity_and_allows_shot_state(self) -> None:
        registry = ContinuityRegistry()
        registry.register(
            VisualEntity(
                entity_id="char_001",
                kind=EntityKind.character,
                display_name="少年",
                identity={"face_identity": "hero-face-v1", "baseline_outfit": "dark winter robe"},
            )
        )
        updated = registry.update_shot_state("char_001", pose="walking", expression="focused")
        self.assertEqual(updated.shot_state["pose"], "walking")
        with self.assertRaises(ContinuityError):
            registry.update_shot_state("char_001", face_identity="someone-else")

    def test_adopted_references_build_multi_entity_contract(self) -> None:
        registry = ContinuityRegistry()
        registry.register(
            VisualEntity(entity_id="char_001", kind=EntityKind.character, display_name="少年")
        )
        registry.register(
            VisualEntity(entity_id="creature_001", kind=EntityKind.creature, display_name="守护神兽")
        )
        registry.adopt_reference("char_001", "asset_hero_v1")
        registry.adopt_reference("creature_001", "asset_beast_v1")
        contract = registry.build_reference_contract(["char_001", "creature_001"])
        self.assertEqual(contract.reference_ids, ("asset_hero_v1", "asset_beast_v1"))

    def test_visual_audit_rejects_missing_required_entity(self) -> None:
        with self.assertRaises(ContinuityError):
            assert_required_entities_visible(
                ["char_001", "creature_001"],
                ["creature_001"],
            )


if __name__ == "__main__":
    unittest.main()
