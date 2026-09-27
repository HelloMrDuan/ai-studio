from __future__ import annotations

import asyncio
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.v3.media.bgm import BGMStore, BGMUploadError, sanitize_upload_filename, should_use_bgm
from app.v3.media.composition import CompositionRequest, FFmpegCompositionService
from app.v3.media.pipeline import MediaProductionPipeline
from app.v3.media.subtitle import SubtitleCue, correct_against_script, read_srt, write_srt
from app.v3.media.tts import OpenAICompatibleTTSConfig, TTSReceipt, TTSRequest


class FakeFFmpeg(FFmpegCompositionService):
    def _run(self, command):
        Path(command[-1]).write_bytes(b"rendered-video")
        return subprocess.CompletedProcess(command, 0, "", "")


class V3MediaPipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_bgm_filename_and_volume_rules_from_mpt_port(self):
        self.assertTrue(should_use_bgm("custom", 0.18))
        self.assertFalse(should_use_bgm("custom", 0))
        self.assertEqual(sanitize_upload_filename("music/test.mp3"), "test.mp3")
        with self.assertRaises(BGMUploadError):
            sanitize_upload_filename("CON.mp3")
        with self.assertRaises(BGMUploadError):
            sanitize_upload_filename("evil.exe")

    def test_tts_remote_and_local_share_same_openai_compatible_endpoint_contract(self):
        remote = OpenAICompatibleTTSConfig(provider_id="remote", base_url="https://api.example/v1", secret_ref="API_KEY")
        local = OpenAICompatibleTTSConfig(provider_id="local", base_url="http://127.0.0.1:9000/v1")
        self.assertEqual(remote.endpoint(), "https://api.example/v1/audio/speech")
        self.assertEqual(local.endpoint(), "http://127.0.0.1:9000/v1/audio/speech")

    def test_subtitle_write_read_and_script_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.srt"
            original = (
                SubtitleCue(0.0, 1.0, "少年在雪山"),
                SubtitleCue(1.0, 2.0, "寻找古剑"),
            )
            write_srt(original, target)
            parsed = read_srt(target)
            self.assertEqual([item.text for item in parsed], ["少年在雪山", "寻找古剑"])
            corrected = correct_against_script(parsed, ["少年在雪山寻找古剑"], similarity_threshold=0.5)
            self.assertEqual(corrected[0].text, "少年在雪山寻找古剑")
            self.assertEqual(corrected[0].end_seconds, 2.0)

    def test_composition_builds_voice_bgm_subtitle_ffmpeg_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clip.mp4"; clip.write_bytes(b"video")
            voice = root / "voice.mp3"; voice.write_bytes(b"voice")
            bgm = root / "bgm.mp3"; bgm.write_bytes(b"bgm")
            subtitle = root / "sub.srt"; subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            output = root / "final.mp4"
            service = FakeFFmpeg()
            receipt = service.compose(CompositionRequest(
                video_clips=(clip,), output_path=output, voice_path=voice, bgm_path=bgm, subtitle_path=subtitle
            ))
            self.assertTrue(output.is_file())
            self.assertTrue(receipt.used_voice)
            self.assertTrue(receipt.used_bgm)
            self.assertTrue(receipt.burned_subtitles)
            self.assertEqual(receipt.video_codec, "libx264")

    async def test_media_stages_are_independent_from_image_video_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "adopted-shot-video.mp4"; clip.write_bytes(b"video")
            bgm = root / "music.mp3"; bgm.write_bytes(b"music")

            async def fake_tts(request, output_path):
                output_path.write_bytes(b"speech")
                return TTSReceipt(
                    output_path=output_path, provider_id="local-tts", model=request.model,
                    voice=request.voice, bytes_written=6, response_format=request.response_format,
                )

            pipeline = MediaProductionPipeline(composition=FakeFFmpeg())
            result = await pipeline.run(
                work_dir=root / "work",
                video_clips=(clip,),
                final_output=root / "final.mp4",
                tts_request=TTSRequest(text="旁白", voice="voice-a", model="local-tts-model"),
                tts_callable=fake_tts,
                subtitle_cues=(SubtitleCue(0, 1, "旁白"),),
                bgm_path=bgm,
            )
            self.assertEqual([stage.stage for stage in result.stages], ["tts", "subtitle", "bgm", "composition"])
            self.assertTrue(result.final_video.is_file())


if __name__ == "__main__":
    unittest.main()
