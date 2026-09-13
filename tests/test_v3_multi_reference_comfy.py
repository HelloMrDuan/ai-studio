from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.v3.generation_executor import (
    ReferenceAsset,
    ReferenceAssetStore,
    compile_role_aware_reference_chain,
)


ROOT = Path(__file__).resolve().parents[1]


class MultiReferenceComfyTests(unittest.TestCase):
    def test_faceid_profile_is_role_aware_and_supports_four_references(self) -> None:
        profile = json.loads(
            (ROOT / "config" / "comfyui_reference_profile.v3.sdxl-faceid.json").read_text(encoding="utf-8")
        )
        self.assertEqual(profile["multi_reference_mode"], "role_aware_chain")
        self.assertEqual(profile["max_references"], 4)
        self.assertTrue(profile["identity_reference"])
        self.assertTrue(profile["ip_adapter"])
        self.assertIn("xiaoduan_multi_reference_faceid_api.json", profile["reference_workflow_path"])

    def test_role_aware_compiler_consumes_two_characters_location_and_prop(self) -> None:
        workflow = json.loads(
            (ROOT / "workflows" / "xiaoduan_multi_reference_faceid_api.json").read_text(encoding="utf-8")
        )
        refs = (
            ReferenceAsset("ref-a", Path("a.png"), "sha-a", "image/png", "char-a", "character_reference", "character"),
            ReferenceAsset("ref-b", Path("b.png"), "sha-b", "image/png", "char-b", "character_reference", "character"),
            ReferenceAsset("ref-loc", Path("loc.png"), "sha-l", "image/png", "loc-a", "location_reference", "location"),
            ReferenceAsset("ref-prop", Path("prop.png"), "sha-p", "image/png", "prop-a", "prop_reference", "prop"),
        )
        names = ("xiaoduan/a.png", "xiaoduan/b.png", "xiaoduan/loc.png", "xiaoduan/prop.png")
        compiled = compile_role_aware_reference_chain(workflow, names, refs)
        serialized = json.dumps(compiled, ensure_ascii=False)
        for name in names:
            self.assertIn(name, serialized)
        face_nodes = [node for node in compiled.values() if node.get("class_type") == "IPAdapterFaceID"]
        generic_nodes = [node for node in compiled.values() if node.get("class_type") == "IPAdapterAdvanced"]
        self.assertEqual(len(face_nodes), 2)
        self.assertEqual(len(generic_nodes), 2)
        self.assertNotEqual(compiled["3"]["inputs"]["model"], ["4", 0])

    def test_reference_store_persists_role_and_entity_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "ref.png"
            source.write_bytes(b"not-empty")
            store = ReferenceAssetStore(Path(tmp) / "data")
            store.import_file(
                "legacy:project:asset",
                source,
                entity_id="char-1",
                role="character_reference",
                entity_type="character",
            )
            resolved = store.resolve("legacy:project:asset")
            self.assertEqual(resolved.role, "character_reference")
            self.assertEqual(resolved.entity_type, "character")
            self.assertEqual(resolved.entity_id, "char-1")


if __name__ == "__main__":
    unittest.main()
