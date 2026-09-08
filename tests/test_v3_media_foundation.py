from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.v3.contracts import Capability, ProviderModelSpec, ProviderTransport
from app.v3.media.subtitle_utils import file_to_subtitles, similarity
from app.v3.media.task_artifacts import atomic_write_json, patch_json, read_json
from app.v3.media.tts import TTSError, build_tts_adapter


class XiaoduanV3MediaFoundationTests(unittest.TestCase):
    def test_atomic_json_write_and_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "task" / "manifest.json"
            atomic_write_json(target, {"task_id": "t1", "state": "generated"})
            self.assertEqual(read_json(target)["state"], "generated")
            patched = patch_json(target, state="candidate_ready", provider="local-comfyui")
            self.assertEqual(patched["state"], "candidate_ready")
            self.assertEqual(read_json(target)["provider"], "local-comfyui")

    def test_srt_parser_keeps_last_block_without_trailing_blank_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "sample.srt"
            target.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\n第二句",
                encoding="utf-8",
            )
            items = file_to_subtitles(target)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[-1][2], "第二句")

    def test_similarity_is_normalized(self) -> None:
        self.assertEqual(similarity("same", "same"), 1.0)
        self.assertGreater(similarity("hello world", "hello wor1d"), 0.8)

    def test_tts_adapter_accepts_local_and_remote_http_peers(self) -> None:
        local = ProviderModelSpec(
            provider_id="local-tts",
            model_id="speech-model",
            transport=ProviderTransport.local_openai_compatible,
            capabilities={Capability.tts},
            base_url="http://127.0.0.1:6010/v1",
        )
        remote = ProviderModelSpec(
            provider_id="remote-tts",
            model_id="speech-model",
            transport=ProviderTransport.remote_api,
            capabilities={Capability.tts},
            base_url="https://example.invalid/v1",
            secret_ref="TTS_API_KEY",
        )
        self.assertEqual(build_tts_adapter(local).config.endpoint(), "http://127.0.0.1:6010/v1/audio/speech")
        self.assertEqual(build_tts_adapter(remote).config.endpoint(), "https://example.invalid/v1/audio/speech")

    def test_tts_adapter_fails_closed_for_wrong_transport(self) -> None:
        spec = ProviderModelSpec(
            provider_id="bad-tts",
            model_id="speech-model",
            transport=ProviderTransport.local_comfyui,
            capabilities={Capability.tts},
            base_url="http://127.0.0.1:8188",
        )
        with self.assertRaises(TTSError):
            build_tts_adapter(spec)


if __name__ == "__main__":
    unittest.main()
