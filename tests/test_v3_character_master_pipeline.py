from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from app.services.production_assets import ProductionAssetService
from app.v3.character_reference_hardening import install_character_reference_hardening
from app.v3.character_reference_package import CharacterReferencePackageBootstrap


class _Assets:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve_data_url(self, url: str) -> Path:
        if not str(url).startswith("/files/"):
            raise ValueError(url)
        return self.root / str(url)[len("/files/"):]


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id

    def get_project(self, project_id: str):
        if project_id != self.project_id:
            raise FileNotFoundError(project_id)
        return {"project_id": project_id, "current_stage": "04", "completed_stages": ["01", "02", "03"]}


class _Legacy:
    def __init__(self, root: Path, director: _Director) -> None:
        self.assets = _Assets(root)
        self.director = director
        self.rows: list[dict] = []

    def _wb_load_candidates(self, _project_id: str):
        return list(self.rows)

    def _wb_sync_candidates(self, _project_id: str):
        return list(self.rows)


class CharacterMasterPipelineTests(unittest.TestCase):
    def test_one_generation_adoption_derives_all_three_legacy_roles(self):
        install_character_reference_hardening()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a1" * 12
            director = _Director(root, project_id)
            director.production.create_text_asset(
                project_id,
                stage="02",
                skill="test",
                logical_key="studio:authoring:character-1:profile",
                asset_role="character_profile",
                name="角色稳定设定",
                content=json.dumps({
                    "entity_id": "character-1",
                    "name": "测试角色",
                    "stable_profile": {"年龄": "17岁", "发型": "黑色高发髻"},
                    "stable_design": "浅青色古式长裙，玉簪",
                }, ensure_ascii=False),
                asset_type="STRUCTURED_DATA",
                extension=".json",
                entity_ids=["character-1"],
            )
            captured: list[dict] = []

            async def submit(_project_id: str, payload: dict):
                captured.append(payload)
                return {"candidate": {"candidate_id": "candidate-master"}}

            legacy = _Legacy(root, director)
            service = CharacterReferencePackageBootstrap(legacy, submit_candidate=submit)
            response = asyncio.run(service.generate_candidate(project_id, "character-1"))

            self.assertEqual(response["generation_phase"], "character_master")
            self.assertEqual(len(captured), 1)
            payload = captured[0]
            self.assertEqual(payload["mode"], "txt2img")
            self.assertEqual(payload["params"]["reference_phase"], "character_master")
            self.assertEqual((payload["params"]["width"], payload["params"]["height"]), (768, 1024))
            self.assertEqual(payload["params"].get("reference_asset_ids", []), [])
            self.assertIn("canonical front source", payload["params"]["positive_prompt"])
            self.assertNotIn("4-panel character turnaround", payload["params"]["positive_prompt"])

            master_path = root / "v3" / "media" / "image" / "master.png"
            master_path.parent.mkdir(parents=True, exist_ok=True)
            image = Image.new("RGB", (2304, 1024), "#d0d2d4")
            draw = ImageDraw.Draw(image)
            colors = ("#8aa6c1", "#b9d7d2", "#9ac1a8")
            for index, color in enumerate(colors):
                center = 384 + index * 768
                draw.ellipse((center - 95, 55, center + 95, 245), fill=(30, 35, 45))
                draw.rectangle((center - 145, 245, center + 145, 1000), fill=color)
            image.save(master_path)

            target_id = payload["target_asset_id"]
            director.production.bind_task(project_id, target_id, {
                "task_id": "master-task",
                "status": "completed",
                "module": "test",
                "operation": "character-master",
                "output_files": ["/files/v3/media/image/master.png"],
            })
            row = {
                "target_asset_id": target_id,
                "output_files": ["/files/v3/media/image/master.png"],
            }
            service.validate_master_adoption(project_id, row)
            derived = service.materialize_master_derivatives(project_id, row)

            self.assertEqual(set(derived), {
                "character_face_anchor", "character_costume_reference", "character_turnaround",
            })
            self.assertTrue(all(asset["parent_asset_ids"] == [target_id] for asset in derived.values()))
            sizes = {}
            for role, asset in derived.items():
                path = legacy.assets.resolve_data_url(asset["storage"]["url"])
                with Image.open(path) as crop:
                    sizes[role] = crop.size
            self.assertEqual(sizes["character_face_anchor"], (512, 512))
            self.assertEqual(sizes["character_costume_reference"], (768, 1024))
            self.assertEqual(sizes["character_turnaround"], (2304, 1024))

            invalid_path = root / "v3" / "media" / "image" / "invalid-master.png"
            invalid = Image.new("RGB", (2304, 1024), "#d0d2d4")
            invalid_draw = ImageDraw.Draw(invalid)
            for center in (384, 1152):
                invalid_draw.ellipse((center - 95, 55, center + 95, 245), fill=(30, 35, 45))
                invalid_draw.rectangle((center - 145, 245, center + 145, 1000), fill="#8aa6c1")
            invalid.save(invalid_path)
            invalid_row = {
                "target_asset_id": target_id,
                "output_files": ["/files/v3/media/image/invalid-master.png"],
            }
            with self.assertRaisesRegex(ValueError, "当前只识别到 2 幅"):
                service.validate_master_adoption(project_id, invalid_row)

            state = service.status(project_id)
            item = state["items"][0]
            self.assertTrue(item["ready"])
            self.assertTrue(item["package_components_ready"])
            self.assertTrue(item["reference_package_asset_id"])

            director.production.create_text_asset(
                project_id,
                stage="02",
                skill="test",
                logical_key="studio:authoring:character-1:profile",
                asset_role="character_profile",
                name="角色稳定设定",
                content=json.dumps({
                    "entity_id": "character-1",
                    "name": "测试角色",
                    "stable_profile": {"年龄": "18岁", "发型": "黑色高发髻"},
                    "stable_design": "18岁，浅青色古式长裙，玉簪",
                }, ensure_ascii=False),
                asset_type="STRUCTURED_DATA",
                extension=".json",
                entity_ids=["character-1"],
            )
            regenerated = asyncio.run(service.generate_candidate(project_id, "character-1", force=True))
            self.assertTrue(regenerated["submitted"])
            self.assertEqual(len(captured), 2)
            self.assertNotEqual(captured[1]["target_asset_id"], target_id)


if __name__ == "__main__":
    unittest.main()
