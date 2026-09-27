from __future__ import annotations

import json
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.config import get_settings
from app.legacy_snapshot import load_original_workbench_runtime
from app.v3.contracts import Capability
from app.v3.media.bgm import BGMStore
from app.v3.media.composition import CompositionRequest, FFmpegCompositionService
from app.v3.media.subtitle import proportional_cues, read_srt, write_srt
from app.v3.media.tts import TTSRequest, build_tts_adapter
from app.v3.provider_catalog import build_provider_registry


settings = get_settings()
legacy = load_original_workbench_runtime()
router = APIRouter()
providers = build_provider_registry(settings)
bgm_store = BGMStore(settings.data_dir)
composer = FFmpegCompositionService()
_ROOT = Path(settings.data_dir) / "v3" / "original-workbench-postproduction"
_ROOT.mkdir(parents=True, exist_ok=True)
_SAFE_PROJECT = re.compile(r"[A-Za-z0-9._-]{3,128}")


class TTSBody(BaseModel):
    narration: str = Field(min_length=1, max_length=20000)
    voice: str = Field(default="zh-CN-XiaoxiaoNeural", min_length=2, max_length=120)


class SubtitleGenerateBody(BaseModel):
    narration: str = Field(min_length=1, max_length=20000)
    video_asset_ids: list[str] = Field(min_length=1, max_length=200)


class SubtitleSaveBody(BaseModel):
    srt_text: str = Field(min_length=1, max_length=100000)


class ComposeBody(BaseModel):
    video_asset_ids: list[str] = Field(min_length=1, max_length=200)
    name: str = Field(default="最终成片", min_length=1, max_length=120)
    voice_volume: float = Field(default=1.0, ge=0.0, le=3.0)
    bgm_volume: float = Field(default=0.18, ge=0.0, le=1.0)


def _project_dir(project_id: str) -> Path:
    value = str(project_id or "").strip()
    if not _SAFE_PROJECT.fullmatch(value):
        raise ValueError("作品编号格式不正确")
    legacy.director.get_project(value)
    path = _ROOT / value
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(project_id: str) -> Path:
    return _project_dir(project_id) / "state.json"


def _load(project_id: str) -> dict[str, Any]:
    path = _state_path(project_id)
    if not path.is_file():
        return {"project_id": project_id, "narration": "", "voice": "zh-CN-XiaoxiaoNeural"}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"project_id": project_id}


def _save(project_id: str, **updates: Any) -> dict[str, Any]:
    data = _load(project_id)
    data.update(updates)
    path = _state_path(project_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return data


def _files_url(path: Path) -> str:
    root = Path(settings.data_dir).resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("文件不在平台数据目录")
    return "/files/" + resolved.relative_to(root).as_posix()


def _asset_path(project_id: str, asset_id: str) -> tuple[dict[str, Any], Path]:
    item = legacy.director.production.get_asset(project_id, asset_id)
    if str(item.get("asset_type") or "").upper() != "VIDEO":
        raise ValueError("只能选择已采用的视频资产")
    if str(getattr(item.get("status"), "value", item.get("status")) or "").lower() != "ready":
        raise ValueError("所选视频尚未就绪")
    if str(getattr(item.get("dependency_state"), "value", item.get("dependency_state")) or "").lower() == "stale":
        raise ValueError("所选视频已过期，请重新生成或采用")
    url = legacy.director.production.asset_url(project_id, asset_id)
    if not url:
        raise ValueError("所选视频没有可用文件")
    path = legacy.assets.resolve_asset_url(url)
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError("所选视频文件不存在")
    return item, path


def _video_assets(project_id: str) -> list[dict[str, Any]]:
    result = []
    for item in legacy.director.production.list_assets(project_id, active_only=True):
        if str(item.get("asset_type") or "").upper() != "VIDEO":
            continue
        if str(getattr(item.get("status"), "value", item.get("status")) or "").lower() != "ready":
            continue
        if str(getattr(item.get("dependency_state"), "value", item.get("dependency_state")) or "").lower() == "stale":
            continue
        role = str(item.get("asset_role") or "")
        if role not in {"shot_clip", "shot_video_processed", "clip"}:
            continue
        url = legacy.director.production.asset_url(project_id, str(item.get("asset_id") or ""))
        if not url:
            continue
        result.append({
            "asset_id": str(item.get("asset_id") or ""),
            "name": str(item.get("name") or "已采用视频"),
            "asset_role": role,
            "version": int(item.get("version") or 1),
            "url": url,
            "shot_id": str((item.get("metadata") or {}).get("shot_id") or ""),
            "order": int((item.get("metadata") or {}).get("global_order") or 0),
        })
    result.sort(key=lambda x: (x["order"], x["name"], x["version"]))
    return result


def _duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("无法读取视频时长")
    value = float((proc.stdout or "0").strip() or 0)
    if value <= 0:
        raise RuntimeError("视频时长无效")
    return value


def _public_state(project_id: str) -> dict[str, Any]:
    state = _load(project_id)
    for key in ("voice_path", "subtitle_path", "bgm_path", "final_path"):
        raw = str(state.get(key) or "").strip()
        path = Path(raw) if raw else None
        state[key.replace("_path", "_url")] = _files_url(path) if path and path.is_file() else ""
    state["videos"] = _video_assets(project_id)
    return state


@router.get("/api/v3/studio/projects/{project_id}/postproduction")
async def postproduction_state(project_id: str) -> dict[str, Any]:
    try:
        return _public_state(project_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, FileNotFoundError) else 400, detail=str(exc)) from exc


@router.post("/api/v3/studio/projects/{project_id}/postproduction/tts")
async def postproduction_tts(project_id: str, body: TTSBody) -> dict[str, Any]:
    try:
        selected = providers.resolve({Capability.tts}, provider_id="edge-tts-gateway", model_id="edge-tts")
        adapter = build_tts_adapter(selected.spec)
        target = _project_dir(project_id) / f"voice_{secrets.token_hex(8)}.mp3"
        receipt = await adapter.synthesize(
            TTSRequest(
                text=body.narration.strip(),
                voice=body.voice.strip(),
                model=selected.spec.model_id,
                response_format="mp3",
                speed=1.0,
                instructions="",
            ),
            target,
        )
        _save(
            project_id,
            narration=body.narration.strip(),
            voice=body.voice.strip(),
            voice_path=str(receipt.output_path),
        )
        return _public_state(project_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"配音生成失败：{type(exc).__name__}: {exc}") from exc


@router.post("/api/v3/studio/projects/{project_id}/postproduction/subtitle/generate")
async def postproduction_subtitle_generate(project_id: str, body: SubtitleGenerateBody) -> dict[str, Any]:
    try:
        paths = [_asset_path(project_id, asset_id)[1] for asset_id in body.video_asset_ids]
        duration = sum(_duration(path) for path in paths)
        narration = body.narration.strip()
        pieces = [item.strip() for item in re.split(r"(?<=[。！？!?；;])", narration) if item.strip()]
        cues = proportional_cues(tuple(pieces or [narration]), duration)
        target = _project_dir(project_id) / "subtitles.srt"
        write_srt(cues, target)
        _save(
            project_id,
            narration=narration,
            subtitle_path=str(target),
            subtitle_text=target.read_text(encoding="utf-8"),
            selected_video_asset_ids=list(body.video_asset_ids),
        )
        return _public_state(project_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"字幕生成失败：{type(exc).__name__}: {exc}") from exc


@router.put("/api/v3/studio/projects/{project_id}/postproduction/subtitle")
async def postproduction_subtitle_save(project_id: str, body: SubtitleSaveBody) -> dict[str, Any]:
    try:
        target = _project_dir(project_id) / "subtitles.srt"
        temp = target.with_suffix(".tmp")
        temp.write_text(body.srt_text.replace("\r\n", "\n").strip() + "\n", encoding="utf-8")
        # Reuse the MoneyPrinterTurbo-derived parser as a validation gate before
        # replacing the active subtitle file.
        cues = read_srt(temp)
        if not cues:
            raise ValueError("字幕内容为空")
        temp.replace(target)
        _save(project_id, subtitle_path=str(target), subtitle_text=target.read_text(encoding="utf-8"))
        return _public_state(project_id)
    except Exception as exc:
        try:
            (_project_dir(project_id) / "subtitles.tmp").unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=f"字幕保存失败：{type(exc).__name__}: {exc}") from exc


@router.post("/api/v3/studio/projects/{project_id}/postproduction/bgm")
async def postproduction_bgm(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    try:
        path = bgm_store.save_upload(file.filename or "背景音乐.mp3", file.file)
        _project_dir(project_id)
        _save(project_id, bgm_path=str(path))
        return _public_state(project_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"背景音乐保存失败：{type(exc).__name__}: {exc}") from exc
    finally:
        await file.close()


@router.post("/api/v3/studio/projects/{project_id}/postproduction/compose")
async def postproduction_compose(project_id: str, body: ComposeBody) -> dict[str, Any]:
    try:
        state = _load(project_id)
        voice = Path(str(state.get("voice_path") or ""))
        subtitle = Path(str(state.get("subtitle_path") or ""))
        bgm = Path(str(state.get("bgm_path") or ""))
        missing = []
        if not voice.is_file():
            missing.append("配音")
        if not subtitle.is_file():
            missing.append("字幕")
        if not bgm.is_file():
            missing.append("背景音乐")
        if missing:
            raise ValueError("请先完成并确认：" + "、".join(missing))

        video_paths = tuple(_asset_path(project_id, asset_id)[1] for asset_id in body.video_asset_ids)
        target = _project_dir(project_id) / f"final_{secrets.token_hex(8)}.mp4"
        receipt = composer.compose(
            CompositionRequest(
                video_clips=video_paths,
                output_path=target,
                voice_path=voice,
                bgm_path=bgm,
                subtitle_path=subtitle,
                voice_volume=body.voice_volume,
                bgm_volume=body.bgm_volume,
            )
        )
        url = _files_url(receipt.output_path)
        # Register the final movie back into the original production graph so it
        # remains visible in the original "最终输出" area and keeps its lineage.
        item = legacy.director.production.register_existing_file(
            project_id,
            stage="final",
            skill="xiaoduan-v3-postproduction",
            logical_key="studio:final_cut:v3",
            asset_type="VIDEO",
            asset_role="final_cut",
            name=body.name,
            url=url,
            source={"type": "v3_postproduction", "manual_stage_editing": True},
            parent_asset_ids=list(body.video_asset_ids),
            entity_ids=[],
            metadata={
                "voice_path": str(voice),
                "subtitle_path": str(subtitle),
                "bgm_path": str(bgm),
                "voice_volume": body.voice_volume,
                "bgm_volume": body.bgm_volume,
                "video_codec": receipt.video_codec,
                "audio_codec": receipt.audio_codec,
                "burned_subtitles": receipt.burned_subtitles,
                "used_voice": receipt.used_voice,
                "used_bgm": receipt.used_bgm,
            },
        )
        _save(
            project_id,
            final_path=str(receipt.output_path),
            final_asset_id=str(item.get("asset_id") or ""),
            selected_video_asset_ids=list(body.video_asset_ids),
        )
        return _public_state(project_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"最终合成失败：{type(exc).__name__}: {exc}") from exc


__all__ = ["router"]
