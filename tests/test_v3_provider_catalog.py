from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.v3.contracts import Capability, ProviderTransport
from app.v3.provider_catalog import build_provider_registry, load_user_provider_specs, platform_provider_specs


class XiaoduanV3ProviderCatalogTests(unittest.TestCase):
    def test_existing_local_services_become_v3_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(_env_file=None, data_dir=Path(temp))
            specs = platform_provider_specs(settings)
        by_id = {item.provider_id: item for item in specs}
        self.assertIn("local-qwen", by_id)
        self.assertIn("local-comfyui-image", by_id)
        self.assertIn("local-h3-video", by_id)
        self.assertEqual(by_id["local-qwen"].transport, ProviderTransport.local_openai_compatible)
        self.assertIn(Capability.text, by_id["local-qwen"].capabilities)
        self.assertIn(Capability.image_generation, by_id["local-comfyui-image"].capabilities)
        self.assertIn(Capability.video_generation, by_id["local-h3-video"].capabilities)

    def test_user_provider_config_supports_remote_and_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "providers.v3.json"
            path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "provider_id": "api",
                                "model_id": "remote",
                                "transport": "remote_api",
                                "base_url": "https://example.invalid/v1",
                                "secret_ref": "API_KEY",
                                "capabilities": ["text", "structured_output"],
                            },
                            {
                                "provider_id": "local",
                                "model_id": "qwen",
                                "transport": "local_openai_compatible",
                                "base_url": "http://127.0.0.1:8000/v1",
                                "capabilities": ["text"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            specs = load_user_provider_specs(path)
        self.assertEqual(specs[0].transport, ProviderTransport.remote_api)
        self.assertEqual(specs[1].transport, ProviderTransport.local_openai_compatible)
        self.assertEqual(specs[0].secret_ref, "API_KEY")

    def test_user_provider_config_is_merged_with_platform_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            (data_dir / "providers.v3.json").write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "provider_id": "api",
                                "model_id": "remote-image",
                                "transport": "remote_api",
                                "base_url": "https://example.invalid/v1",
                                "capabilities": ["image_generation"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings(_env_file=None, data_dir=data_dir)
            registry = build_provider_registry(settings)
        self.assertGreaterEqual(len(registry.list()), 4)
        selected = registry.resolve(
            {Capability.image_generation}, provider_id="api", model_id="remote-image"
        )
        self.assertEqual(selected.spec.transport, ProviderTransport.remote_api)


if __name__ == "__main__":
    unittest.main()
