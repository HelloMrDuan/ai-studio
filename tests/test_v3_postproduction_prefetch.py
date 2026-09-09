from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.v3.postproduction_prefetch import PostProductionPrefetch


class _Adapter:
    async def synthesize(self, request, target: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-mp3")
        return SimpleNamespace(output_path=target)


class _Providers:
    def resolve(self, *args, **kwargs):
        return SimpleNamespace(spec=SimpleNamespace(model_id="edge-tts"))


class _Director:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.project = {
            "project_id": project_id,
            "current_stage": "04",
            "status": "active",
        }
        self.calls = 0

    def get_project(self, project_id: str):
        if project_id != self.project_id:
            raise FileNotFoundError(project_id)
        return dict(self.project)

    async def confirm_stage(self, project_id: str):
        self.calls += 1
        self.project["status"] = "completed"
        return {"ok": True}


class _Legacy:
    def __init__(self, director):
        self.director = director


class PostProductionPrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_builds_voice_and_subtitle_from_confirmed_shot_narration(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a" * 24
            continuity = root / "story_continuity"
            continuity.mkdir(parents=True)
            (continuity / f"{project_id}.json").write_text(
                json.dumps(
                    {
                        "shots": [
                            {"narration": "少年踏入雪山。"},
                            {"narration": "他发现了古剑。"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            director = _Director(project_id)
            service = PostProductionPrefetch(
                SimpleNamespace(data_dir=root),
                _Legacy(director),
                adapter_factory=lambda spec: _Adapter(),
            )
            service.providers = _Providers()
            service._duration = lambda path: 6.0

            state = await service.prepare(project_id)

            self.assertEqual(state["prefetch_status"], "ready")
            self.assertTrue(Path(state["voice_path"]).is_file())
            self.assertTrue(Path(state["subtitle_path"]).is_file())
            self.assertIn("少年踏入雪山", state["subtitle_text"])
            self.assertEqual(state["prefetch_audio_duration_seconds"], 6.0)

    async def test_prepare_never_overwrites_existing_user_voice_and_subtitle(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "b" * 24
            continuity = root / "story_continuity"
            continuity.mkdir(parents=True)
            (continuity / f"{project_id}.json").write_text(
                json.dumps({"shots": [{"narration": "旁白。"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            director = _Director(project_id)
            service = PostProductionPrefetch(
                SimpleNamespace(data_dir=root),
                _Legacy(director),
                adapter_factory=lambda spec: _Adapter(),
            )
            service.providers = _Providers()
            service._duration = lambda path: 2.0
            user_voice = service._project_dir(project_id) / "user.mp3"
            user_subtitle = service._project_dir(project_id) / "user.srt"
            user_voice.write_bytes(b"user")
            user_subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n用户字幕\n", encoding="utf-8")
            service._save_state(
                project_id,
                voice_path=str(user_voice),
                subtitle_path=str(user_subtitle),
            )

            state = await service.prepare(project_id)

            self.assertEqual(state["prefetch_status"], "already_ready")
            self.assertEqual(state["voice_path"], str(user_voice))
            self.assertEqual(state["subtitle_path"], str(user_subtitle))


if __name__ == "__main__":
    unittest.main()
