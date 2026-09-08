from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.v3.contracts import Capability, ProviderModelSpec, ProviderTransport
from app.v3.generation_contract import GenerationContract
from app.v3.generation_executor import (
    ComfyReferenceBinding,
    ReferenceAssetStore,
    ReferenceFirstComfyExecutor,
    bind_comfy_references,
)
from app.v3.adapters.h3 import H3ReferenceFirstExecutor, H3WorkflowCompiler, H3WorkflowConfig


class FakeComfyAdapter:
    def __init__(self, spec):
        self.spec = spec
        self.uploaded = []
        self.queued = []

    async def upload_reference(self, *, filename, content, overwrite=False, subfolder="xiaoduan-v3"):
        self.uploaded.append((filename, bytes(content)))
        return {"name": filename, "subfolder": subfolder}

    async def queue_workflow(self, workflow, **kwargs):
        self.queued.append(workflow)
        return {"prompt_id": "prompt-v3-001"}


class ReferenceFirstGenerationTests(unittest.IsolatedAsyncioTestCase):
    def _image_spec(self):
        return ProviderModelSpec(
            provider_id="comfy-ref", model_id="image-ref", transport=ProviderTransport.local_comfyui,
            base_url="http://127.0.0.1:8188",
            capabilities={Capability.image_generation, Capability.image_reference, Capability.multi_reference},
            max_references=4,
        )

    def _h3_spec(self):
        return ProviderModelSpec(
            provider_id="local-h3-video", model_id="minimax-h3", transport=ProviderTransport.local_comfyui,
            base_url="http://127.0.0.1:8188",
            capabilities={Capability.video_generation, Capability.image_reference, Capability.first_frame, Capability.last_frame, Capability.first_last_frame},
        )

    def _write_pngish(self, root: Path, name: str, payload: bytes) -> Path:
        path = root / name
        path.write_bytes(payload)
        return path

    async def test_comfy_executor_uploads_and_binds_every_canonical_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            refs = ReferenceAssetStore(root)
            refs.import_file("hero-v1", self._write_pngish(root, "hero.png", b"hero"), entity_id="char_hero")
            refs.import_file("beast-v1", self._write_pngish(root, "beast.png", b"beast"), entity_id="creature_guardian")
            contract = GenerationContract(
                shot_id="shot-2", source_text="少年遇到守护神兽", entity_ids=("char_hero", "creature_guardian"),
                reference_ids=("hero-v1", "beast-v1"), provider_reference_ids=("hero-v1", "beast-v1"),
                provider_id="comfy-ref", model_id="image-ref",
                required_capabilities=frozenset({Capability.image_generation, Capability.image_reference, Capability.multi_reference}),
                camera_direction="medium", action="meet", duration_seconds=3.0,
            )
            adapter = FakeComfyAdapter(self._image_spec())
            executor = ReferenceFirstComfyExecutor(adapter=adapter, references=refs)
            template = {
                "10": {"class_type": "LoadImage", "inputs": {"image": ""}},
                "11": {"class_type": "LoadImage", "inputs": {"image": ""}},
                "20": {"class_type": "ReferenceConditioning", "inputs": {"a": ["10", 0], "b": ["11", 0]}},
            }
            receipt = await executor.execute(
                contract, workflow=template,
                reference_bindings=(
                    ComfyReferenceBinding(reference_index=0, node_id="10"),
                    ComfyReferenceBinding(reference_index=1, node_id="11"),
                ),
            )
            self.assertEqual(receipt.provider_reference_ids, ("hero-v1", "beast-v1"))
            self.assertEqual(len(adapter.uploaded), 2)
            queued = adapter.queued[0]
            self.assertEqual(queued["10"]["inputs"]["image"], receipt.uploaded_reference_names[0])
            self.assertEqual(queued["11"]["inputs"]["image"], receipt.uploaded_reference_names[1])
            self.assertNotEqual(receipt.uploaded_reference_names[0], receipt.uploaded_reference_names[1])
            self.assertTrue(all(name.startswith("xiaoduan-v3/") for name in receipt.uploaded_reference_names))

    def test_comfy_binding_fails_when_one_reference_slot_is_missing(self):
        with self.assertRaises(Exception):
            bind_comfy_references(
                {"10": {"inputs": {"image": ""}}},
                ["hero.png", "beast.png"],
                [ComfyReferenceBinding(reference_index=0, node_id="10")],
            )

    async def test_h3_receives_adopted_first_frame_in_actual_loadimage_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            refs = ReferenceAssetStore(root)
            refs.import_file("shot2-first-frame", self._write_pngish(root, "first.webp", b"frame"))
            contract = GenerationContract(
                shot_id="shot-2", source_text="少年遇到守护神兽", entity_ids=("char_hero", "creature_guardian"),
                reference_ids=("hero-v1", "beast-v1"), provider_reference_ids=("shot2-first-frame",),
                provider_id="local-h3-video", model_id="minimax-h3",
                required_capabilities=frozenset({Capability.video_generation, Capability.image_reference, Capability.first_frame}),
                camera_direction="", action="", duration_seconds=5.0,
            )
            adapter = FakeComfyAdapter(self._h3_spec())
            compiler = H3WorkflowCompiler(H3WorkflowConfig(
                fl2va_model="h3-fl2va.safetensors", ref2va_model="h3-ref2va.safetensors",
                text_encoder="qwen3vl.safetensors", video_vae="video-vae.safetensors", audio_vae="audio-vae.safetensors",
            ))
            receipt = await H3ReferenceFirstExecutor(adapter=adapter, references=refs, compiler=compiler).execute_first_frame(
                contract, prompt="same hero and guardian beast in snow mountain", seed=42
            )
            self.assertEqual(receipt.provider_reference_ids, ("shot2-first-frame",))
            workflow = adapter.queued[0]
            self.assertEqual(workflow["7"]["class_type"], "MiniMaxH3ImageToVideo")
            self.assertEqual(workflow["7"]["inputs"]["first_frame"], ["5", 0])
            self.assertEqual(workflow["5"]["inputs"]["image"], receipt.uploaded_reference_names[0])
            self.assertTrue(receipt.uploaded_reference_names[0].startswith("xiaoduan-v3/"))

    def test_h3_ref2va_uses_real_reference_to_video_node(self):
        compiler = H3WorkflowCompiler(H3WorkflowConfig(
            fl2va_model="fl", ref2va_model="ref", text_encoder="clip", video_vae="vvae", audio_vae="avae"
        ))
        workflow = compiler.reference_to_video(prompt="test", reference_name="xiaoduan-v3/ref.png")
        self.assertEqual(workflow["7"]["class_type"], "MiniMaxH3ReferenceToVideo")
        self.assertEqual(workflow["7"]["inputs"]["ref_images"], {"ref_image_0": ["6", 0]})
        self.assertEqual(workflow["6"]["inputs"]["image"], "xiaoduan-v3/ref.png")


if __name__ == "__main__":
    unittest.main()
