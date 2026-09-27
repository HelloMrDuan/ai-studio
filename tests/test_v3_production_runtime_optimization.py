from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.v3.production_runtime_optimization import (
    PersistentLLMContentCache,
    ProductionRuntimeOptimizer,
    ProjectProductionContext,
)


class _Production:
    def __init__(self) -> None:
        self.assets = [
            {
                "asset_id": "story1",
                "asset_role": "story_bible",
                "asset_type": "STRUCTURED_DATA",
                "status": "ready",
                "dependency_state": "current",
                "active": True,
                "version": 1,
                "updated_at": "1",
                "entity_ids": [],
            },
            {
                "asset_id": "charprof",
                "asset_role": "character_profile",
                "asset_type": "STRUCTURED_DATA",
                "status": "ready",
                "dependency_state": "current",
                "active": True,
                "version": 2,
                "updated_at": "2",
                "entity_ids": ["char1"],
            },
            {
                "asset_id": "locprof",
                "asset_role": "location_profile",
                "asset_type": "STRUCTURED_DATA",
                "status": "ready",
                "dependency_state": "current",
                "active": True,
                "version": 1,
                "updated_at": "2",
                "entity_ids": ["loc1"],
            },
        ]
        self.text = {
            "story1": '{"剧情节点":[{"id":"P1","事件":"少年进入雪山"}]}',
            "charprof": '{"name":"少年","stable_design":"黑发、深蓝冬装"}',
            "locprof": '{"name":"雪山古道","anchors":["石碑","断桥","冰壁"]}',
        }
        self.entities = [
            {
                "entity_id": "char1",
                "entity_type": "character",
                "name": "少年",
                "metadata": {"continuity": {"core_profile": {"身份": "少年侠客"}}},
            },
            {
                "entity_id": "loc1",
                "entity_type": "location",
                "name": "雪山古道",
                "metadata": {"continuity": {"core_profile": {"结构": "狭窄山道"}}},
            },
        ]

    def list_assets(self, project_id: str, active_only: bool = False, **kwargs):
        rows = list(self.assets)
        return [row for row in rows if not active_only or row.get("active")]

    def list_entities(self, project_id: str, entity_type: str = ""):
        if entity_type:
            return [row for row in self.entities if row["entity_type"] == entity_type]
        return list(self.entities)

    def read_text_asset(self, project_id: str, asset_id: str, max_chars: int = 30000):
        return self.text[asset_id][:max_chars]


class _Director:
    def __init__(self, production: _Production) -> None:
        self.production = production
        self.calls = 0
        self.llm = SimpleNamespace(base_url="http://127.0.0.1:6006/v1", model="qwen")

    def _prior_handoffs(self, project, max_chars=12000):
        return "legacy-all-history"

    def _prior_asset_manifest(self, project, max_chars=5000):
        return "legacy-asset-manifest"

    async def _tracked_llm_chat(self, *, phase, messages, system_prompt, temperature, max_tokens):
        self.calls += 1
        return {"content": "稳定结果", "model": "qwen"}

    def record_phase_cache_hit(self, phase: str):
        pass


class ProductionRuntimeOptimizationTests(unittest.IsolatedAsyncioTestCase):
    def test_stage_context_is_projected_not_full_history(self):
        with tempfile.TemporaryDirectory() as raw:
            service = ProjectProductionContext(Path(raw), _Production())
            project = {
                "project_id": "a" * 24,
                "current_stage": "03",
                "confirmed_outputs": {},
            }
            stage3 = service.build(project, "03")
            self.assertIn("character_assets", stage3)
            self.assertIn("location_facts", stage3)
            self.assertNotIn("canonical_references", stage3)
            self.assertTrue(stage3["context_hash"])

            stage4 = service.build(project, "04")
            self.assertIn("reusable_assets", stage4)
            self.assertIn("canonical_references", stage4)
            self.assertNotEqual(stage3["context_hash"], stage4["context_hash"])

    def test_persistent_cache_returns_identical_llm_call_without_second_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = PersistentLLMContentCache(Path(raw), max_entries=64)
            key = cache.key(
                phase="content",
                messages=[{"role": "user", "content": "x"}],
                system_prompt="s",
                temperature=0.5,
                max_tokens=100,
                model_signature={"model": "qwen"},
            )
            self.assertIsNone(cache.get(key))
            cache.put(key, {"content": "ok"})
            self.assertEqual(cache.get(key)["content"], "ok")

    async def test_optimizer_caches_real_director_call_and_replaces_handoffs_with_context(self):
        with tempfile.TemporaryDirectory() as raw:
            production = _Production()
            director = _Director(production)
            settings = SimpleNamespace(
                data_dir=Path(raw),
                stage04_required_model_id="qwen3-32b-abliterated",
                stage04_required_model_alias="qwen3-32b",
            )
            optimizer = ProductionRuntimeOptimizer(settings, director)
            optimizer.install()
            kwargs = {
                "phase": "director_orchestrator_content",
                "messages": [{"role": "user", "content": "生成"}],
                "system_prompt": "系统",
                "temperature": 0.5,
                "max_tokens": 100,
            }
            first = await director._tracked_llm_chat(**kwargs)
            second = await director._tracked_llm_chat(**kwargs)
            self.assertEqual(first["content"], second["content"])
            self.assertEqual(director.calls, 1)
            self.assertTrue(second["persistent_content_cache_hit"])

            project = {
                "project_id": "b" * 24,
                "current_stage": "04",
                "confirmed_outputs": {},
            }
            context = director._prior_handoffs(project, max_chars=9000)
            self.assertIn("story_bible", context)
            self.assertIn("reusable_assets", context)
            self.assertNotIn("legacy-all-history", context)


if __name__ == "__main__":
    unittest.main()
