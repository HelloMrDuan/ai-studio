from __future__ import annotations

import asyncio
import json
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any, Callable

from app.v3.contracts import Capability
from app.v3.media.subtitle import proportional_cues, write_srt
from app.v3.media.tts import TTSRequest, build_tts_adapter
from app.v3.provider_catalog import build_provider_registry


class PostProductionPrefetch:
    """Prepare audio-side artifacts while GPU visual production is running.

    Stage04 confirmation is the earliest point where narration is stable enough
    to synthesize.  TTS/subtitles use network/CPU resources, so they should not
    wait behind Comfy/H3.  The hook is non-blocking and never overwrites a file
    that the user has already generated manually.
    """

    def __init__(
        self,
        settings: Any,
        legacy_runtime: Any,
        *,
        adapter_factory: Callable[[Any], Any] = build_tts_adapter,
    ) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.providers = build_provider_registry(settings)
        self.adapter_factory = adapter_factory
        self.root = Path(settings.data_dir) / "v3" / "original-workbench-postproduction"
        self.root.mkdir(parents=True, exist_ok=True)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._original_confirm = None

    def _project_dir(self, project_id: str) -> Path:
        self.director.get_project(project_id)
        path = self.root / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _state_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "state.json"

    def _load_state(self, project_id: str) -> dict[str, Any]:
        path = self._state_path(project_id)
        if not path.is_file():
            return {"project_id": project_id}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"project_id": project_id}
        except Exception:
            return {"project_id": project_id}

    def _save_state(self, project_id: str, **updates: Any) -> dict[str, Any]:
        state = self._load_state(project_id)
        state.update(updates)
        target = self._state_path(project_id)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(target)
        return state

    def _narration(self, project_id: str) -> str:
        path = Path(self.settings.data_dir) / "story_continuity" / f"{project_id}.json"
        if path.is_file():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                lines = []
                for shot in state.get("shots") or []:
                    if not isinstance(shot, dict):
                        continue
                    text = str(shot.get("narration") or "").strip()
                    if text and text not in lines:
                        lines.append(text)
                if lines:
                    return "\n".join(lines)
            except Exception:
                pass
        return ""

    @staticmethod
    def _duration(path: Path) -> float:
        process = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError("无法读取预生成配音时长")
        value = float((process.stdout or "0").strip() or 0)
        if value <= 0:
            raise RuntimeError("预生成配音时长无效")
        return value

    async def prepare(self, project_id: str) -> dict[str, Any]:
        narration = self._narration(project_id)
        if not narration:
            return self._save_state(
                project_id,
                prefetch_status="skipped",
                prefetch_message="正式分镜没有旁白，不需要预生成配音字幕",
            )

        before = self._load_state(project_id)
        voice_existing = Path(str(before.get("voice_path") or ""))
        subtitle_existing = Path(str(before.get("subtitle_path") or ""))
        if voice_existing.is_file() and subtitle_existing.is_file():
            return self._save_state(project_id, prefetch_status="already_ready")

        try:
            selected = self.providers.resolve(
                {Capability.tts},
                provider_id="edge-tts-gateway",
                model_id="edge-tts",
            )
            adapter = self.adapter_factory(selected.spec)
            voice_path = self._project_dir(project_id) / f"voice_prefetch_{secrets.token_hex(8)}.mp3"
            receipt = await adapter.synthesize(
                TTSRequest(
                    text=narration,
                    voice=str(before.get("voice") or "zh-CN-XiaoxiaoNeural"),
                    model=selected.spec.model_id,
                    response_format="mp3",
                    speed=1.0,
                    instructions="",
                ),
                voice_path,
            )

            # A user may have manually generated audio while prefetch was in
            # flight.  Never replace that explicit choice with background work.
            current = self._load_state(project_id)
            current_voice = Path(str(current.get("voice_path") or ""))
            if current_voice.is_file() and current_voice.resolve() != Path(receipt.output_path).resolve():
                Path(receipt.output_path).unlink(missing_ok=True)
                return self._save_state(project_id, prefetch_status="superseded_by_user")

            duration = await asyncio.to_thread(self._duration, Path(receipt.output_path))
            pieces = [
                item.strip()
                for item in re.split(r"(?<=[。！？!?；;])", narration)
                if item.strip()
            ]
            cues = proportional_cues(tuple(pieces or [narration]), duration)
            subtitle_path = self._project_dir(project_id) / "subtitles_prefetch.srt"
            write_srt(cues, subtitle_path)

            current = self._load_state(project_id)
            current_subtitle = Path(str(current.get("subtitle_path") or ""))
            if current_subtitle.is_file() and current_subtitle.resolve() != subtitle_path.resolve():
                subtitle_path.unlink(missing_ok=True)
                Path(receipt.output_path).unlink(missing_ok=True)
                return self._save_state(project_id, prefetch_status="superseded_by_user")

            return self._save_state(
                project_id,
                narration=narration,
                voice=str(before.get("voice") or "zh-CN-XiaoxiaoNeural"),
                voice_path=str(receipt.output_path),
                subtitle_path=str(subtitle_path),
                subtitle_text=subtitle_path.read_text(encoding="utf-8"),
                prefetch_status="ready",
                prefetch_message="配音与字幕已在画面/视频制作期间并行准备完成",
                prefetch_audio_duration_seconds=duration,
            )
        except Exception as exc:
            # Background acceleration must never block the production chain.
            return self._save_state(
                project_id,
                prefetch_status="failed",
                prefetch_message=f"后台配音字幕预生成失败：{exc}",
            )

    def install_confirmation_hook(self) -> None:
        if getattr(self.director, "_xiaoduan_postproduction_prefetch_installed", False):
            return
        original = self.director.confirm_stage
        self._original_confirm = original

        async def wrapped(project_id: str):
            before = self.director.get_project(project_id)
            stage = str(before.get("current_stage") or "").strip()
            result = await original(project_id)
            if stage == "04":
                task = asyncio.create_task(self.prepare(project_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return result

        self.director.confirm_stage = wrapped
        self.director._xiaoduan_postproduction_prefetch_installed = True


__all__ = ["PostProductionPrefetch"]
