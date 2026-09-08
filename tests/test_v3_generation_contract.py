from __future__ import annotations

import unittest

from app.v3.continuity import ContinuityRegistry
from app.v3.contracts import Capability, EntityKind, ProviderModelSpec, ProviderTransport, VisualEntity
from app.v3.generation_contract import GenerationContractCompiler, ShotContract
from app.v3.provider_gateway import ProviderRegistry, ProviderResolutionError


class XiaoduanV3GenerationContractTests(unittest.TestCase):
    def _continuity(self) -> ContinuityRegistry:
        registry = ContinuityRegistry()
        for entity in (
            VisualEntity(
                entity_id="char_hero",
                kind=EntityKind.character,
                display_name="少年",
                identity={"cultural_context": "Chinese", "face_identity": "hero-v1"},
            ),
            VisualEntity(
                entity_id="creature_guardian",
                kind=EntityKind.creature,
                display_name="守护神兽",
                identity={"species": "canonical-guardian-beast-v1"},
            ),
            VisualEntity(
                entity_id="prop_sword",
                kind=EntityKind.important_item,
                display_name="失落古剑",
                identity={"shape": "ancient-sword-v1"},
            ),
            VisualEntity(
                entity_id="loc_snow_mountain",
                kind=EntityKind.location,
                display_name="雪山",
                identity={"location_identity": "snow-mountain-world-v1"},
            ),
        ):
            registry.register(entity)
        registry.adopt_reference("char_hero", "asset_hero_v1")
        registry.adopt_reference("creature_guardian", "asset_guardian_v1")
        registry.adopt_reference("prop_sword", "asset_sword_v1")
        registry.adopt_reference("loc_snow_mountain", "asset_snow_world_v1")
        return registry

    def _provider(self, *, multi_reference: bool = True) -> ProviderRegistry:
        capabilities = {Capability.image_generation, Capability.image_reference}
        if multi_reference:
            capabilities.add(Capability.multi_reference)
        return ProviderRegistry(
            [
                ProviderModelSpec(
                    provider_id="reference-image-provider",
                    model_id="image-v1",
                    transport=ProviderTransport.local_comfyui,
                    capabilities=capabilities,
                    max_references=6 if multi_reference else None,
                )
            ]
        )

    def test_snow_story_keeps_same_canonical_entities_across_three_shots(self) -> None:
        compiler = GenerationContractCompiler(
            continuity=self._continuity(), providers=self._provider()
        )
        shots = [
            ShotContract(
                shot_id="shot-001",
                source_text="少年在雪山寻找失落古剑。",
                required_entity_ids=("char_hero", "prop_sword", "loc_snow_mountain"),
            ),
            ShotContract(
                shot_id="shot-002",
                source_text="少年在雪山途中遇到守护神兽。",
                required_entity_ids=("char_hero", "creature_guardian", "loc_snow_mountain"),
            ),
            ShotContract(
                shot_id="shot-003",
                source_text="少年最终获得守护神兽的认可。",
                required_entity_ids=("char_hero", "creature_guardian", "loc_snow_mountain"),
            ),
        ]
        compiled = [compiler.compile_image(shot) for shot in shots]
        self.assertIn("asset_hero_v1", compiled[0].reference_ids)
        self.assertIn("asset_hero_v1", compiled[1].reference_ids)
        self.assertIn("asset_hero_v1", compiled[2].reference_ids)
        self.assertIn("asset_guardian_v1", compiled[1].reference_ids)
        self.assertIn("asset_guardian_v1", compiled[2].reference_ids)
        self.assertIn("asset_snow_world_v1", compiled[0].reference_ids)
        self.assertIn("asset_snow_world_v1", compiled[1].reference_ids)
        self.assertIn("asset_snow_world_v1", compiled[2].reference_ids)

    def test_persistent_entity_without_adopted_reference_fails_closed(self) -> None:
        continuity = self._continuity()
        continuity.register(
            VisualEntity(entity_id="prop_new", kind=EntityKind.prop, display_name="新道具")
        )
        compiler = GenerationContractCompiler(continuity=continuity, providers=self._provider())
        with self.assertRaises(Exception):
            compiler.compile_image(
                ShotContract(
                    shot_id="shot-x",
                    source_text="少年拿起新道具",
                    required_entity_ids=("char_hero", "prop_new"),
                )
            )

    def test_multi_entity_shot_does_not_drop_refs_for_low_capability_provider(self) -> None:
        compiler = GenerationContractCompiler(
            continuity=self._continuity(), providers=self._provider(multi_reference=False)
        )
        with self.assertRaises(ProviderResolutionError):
            compiler.compile_image(
                ShotContract(
                    shot_id="shot-002",
                    source_text="少年在雪山途中遇到守护神兽。",
                    required_entity_ids=("char_hero", "creature_guardian", "loc_snow_mountain"),
                )
            )


if __name__ == "__main__":
    unittest.main()
