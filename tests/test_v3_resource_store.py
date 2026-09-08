from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.v3.contracts import ResourceState
from app.v3.resource_store import ResourceStore, ResourceStoreError


class XiaoduanV3ResourceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = ResourceStore(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _candidate(self, *, logical_key: str = "shot:001:image") -> dict:
        return self.store.create_candidate(
            "project-demo",
            logical_key=logical_key,
            generation_task_id="task_001",
            provider_id="local-comfyui",
            model_id="workflow-v1",
            reference_ids=["asset_hero_v1"],
            prompt_contract_version="shot-image-v1",
            continuity_version=1,
        )

    def test_generated_result_is_not_automatically_adopted(self) -> None:
        candidate = self._candidate()
        self.assertEqual(candidate["state"], ResourceState.generated.value)
        self.assertIsNone(self.store.adopted("project-demo", "shot:001:image"))

    def test_audit_failure_cannot_be_adopted(self) -> None:
        candidate = self._candidate()
        failed = self.store.set_audit_result(
            "project-demo", candidate["resource_id"], passed=False, audit={"missing": ["char_001"]}
        )
        self.assertEqual(failed["state"], ResourceState.audit_failed.value)
        with self.assertRaises(ResourceStoreError):
            self.store.adopt("project-demo", candidate["resource_id"])

    def test_only_candidate_ready_can_be_adopted(self) -> None:
        candidate = self._candidate()
        ready = self.store.set_audit_result(
            "project-demo", candidate["resource_id"], passed=True, audit={"required_entities_ok": True}
        )
        self.assertEqual(ready["state"], ResourceState.candidate_ready.value)
        adopted = self.store.adopt("project-demo", candidate["resource_id"])
        self.assertEqual(adopted["state"], ResourceState.adopted.value)
        self.assertEqual(
            self.store.adopted("project-demo", "shot:001:image")["resource_id"],
            candidate["resource_id"],
        )

    def test_new_candidate_does_not_overwrite_previous_adopted_until_adoption(self) -> None:
        first = self._candidate()
        self.store.set_audit_result("project-demo", first["resource_id"], passed=True, audit={})
        self.store.adopt("project-demo", first["resource_id"])

        second = self.store.create_candidate(
            "project-demo",
            logical_key="shot:001:image",
            generation_task_id="task_002",
            provider_id="remote-image",
            model_id="image-v2",
        )
        current = self.store.adopted("project-demo", "shot:001:image")
        self.assertEqual(current["resource_id"], first["resource_id"])
        self.assertEqual(second["version"], 2)

        self.store.set_audit_result("project-demo", second["resource_id"], passed=True, audit={})
        self.store.adopt("project-demo", second["resource_id"])
        versions = self.store.list_versions("project-demo", "shot:001:image")
        self.assertEqual(versions[0]["state"], ResourceState.superseded.value)
        self.assertEqual(versions[1]["state"], ResourceState.adopted.value)


if __name__ == "__main__":
    unittest.main()
