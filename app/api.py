"""HTTP API。前端是 web/ 下的单页应用，所有生成操作都提交为任务，前端轮询任务进度。"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import db, facefusion, pipeline, prompts, qa, voice
from .config import get_settings
from .gpu import gpu
from .tasks import queue
from .workflows import optional_models, required_models

router = APIRouter(prefix="/api")
ASPECTS = ["16:9", "9:16", "1:1", "4:3", "3:4"]
UPLOAD_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".mp3", ".wav", ".m4a", ".aac",
                 ".flac", ".ogg"}


def _get(table: str, row_id: str, label: str) -> dict:
    row = db.get(table, row_id)
    if not row:
        raise HTTPException(404, f"{label}不存在")
    return row


def _clean(values: dict, allowed: set[str]) -> dict:
    return {k: v for k, v in values.items() if k in allowed and v is not None}


async def _save_upload(file: UploadFile, project_id: str, sub: str) -> Path:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in UPLOAD_SUFFIX:
        raise HTTPException(400, f"不支持的文件类型：{suffix or '未知'}")
    target = pipeline.project_dir(project_id, sub) / f"{db.new_id()}{suffix}"
    with target.open("wb") as fh:
        while chunk := await file.read(1024 * 1024):
            fh.write(chunk)
    return target


def _submit(type_: str, title: str, project_id: str, owner_type: str = "", owner_id: str = "",
            params: dict | None = None, dedupe: bool = True) -> dict:
    return queue.submit(type_, project_id=project_id, title=title, owner_type=owner_type, owner_id=owner_id,
                        params=params or {}, dedupe=dedupe)


# ================= 系统 =================
@router.get("/meta")
async def meta() -> dict:
    return {"styles": {k: v["label"] for k, v in prompts.STYLES.items()}, "aspects": ASPECTS,
            "shot_sizes": prompts.SHOT_SIZES, "camera_angles": prompts.CAMERA_ANGLES,
            "camera_moves": prompts.CAMERA_MOVES, "r2v": pipeline.r2v_available(), "qa": qa.available(),
            "voice": voice.status(), "auth": bool(get_settings().access_password)}


def _model_check() -> list[dict]:
    s = get_settings()
    rows = []
    for optional, group in ((False, required_models()), (True, optional_models())):
        for folder, names in group.items():
            for name in names:
                p = s.comfyui_dir / "models" / folder / name
                rows.append({"name": name, "folder": folder, "optional": optional, "ok": p.is_file(),
                             "size_gb": round(p.stat().st_size / 1e9, 2) if p.is_file() else 0})
    if s.llm_provider == "local":
        rows.append({"name": s.llm_gguf.name, "folder": "llm", "optional": False, "ok": s.llm_gguf.is_file(),
                     "size_gb": round(s.llm_gguf.stat().st_size / 1e9, 2) if s.llm_gguf.is_file() else 0})
    return rows


@router.get("/system/status")
async def system_status() -> dict:
    s = get_settings()
    disk = shutil.disk_usage(s.data_dir if s.data_dir.exists() else "/")
    return {
        "gpu": await gpu.status(),
        "models": _model_check(),
        "llm": {"provider": s.llm_provider, "model": s.llm_model, "base_url": s.llm_base_url,
                "server_bin": s.llama_server_bin.is_file()},
        "facefusion": s.facefusion_python.is_file() and (s.facefusion_dir / "facefusion.py").is_file(),
        "qa": qa.available(), "voice": voice.status(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "disk": {"free_gb": round(disk.free / 1e9, 1), "total_gb": round(disk.total / 1e9, 1)},
        "queue": {"current": queue.current,
                  "waiting": len(db.query("SELECT id FROM tasks WHERE status='queued'")),
                  "paused": len(db.query("SELECT id FROM tasks WHERE status='paused'"))},
    }


@router.post("/system/release")
async def system_release() -> dict:
    """手动释放显存：停文字模型 + 让 ComfyUI 卸载模型（有任务运行时会等待其结束）。"""
    async with gpu.lock:
        await gpu._stop_llm()
        await gpu.free_comfy()
        gpu.owner = "idle"
    return {"ok": True}


# ================= 项目 =================
class ProjectIn(BaseModel):
    title: str = "未命名项目"
    source_type: str = "idea"
    source_text: str = ""
    aspect_ratio: str = "16:9"
    style: str = "cinematic"
    style_note: str = ""
    target_seconds: int = 60
    script: str | None = None
    synopsis: str | None = None
    dub_mode: int = 0


PROJECT_FIELDS = {"title", "source_type", "source_text", "aspect_ratio", "style", "style_note", "target_seconds",
                  "script", "synopsis", "dub_mode"}


def _check_project(values: dict) -> None:
    if "aspect_ratio" in values and values["aspect_ratio"] not in ASPECTS:
        raise HTTPException(400, "不支持的画幅")
    if "style" in values and values["style"] not in prompts.STYLES:
        raise HTTPException(400, "不支持的风格")


@router.get("/projects")
async def list_projects() -> list[dict]:
    out = []
    for p in db.query("SELECT * FROM projects ORDER BY updated_at DESC"):
        shots = pipeline.shots_of(p["id"])
        cover = next((s["keyframe"] for s in shots if s["keyframe"]), "")
        if not cover:
            cover = next((c["ref_image"] for c in pipeline.characters_of(p["id"]) if c["ref_image"]), "")
        out.append({**{k: p[k] for k in ("id", "title", "aspect_ratio", "style", "synopsis", "updated_at",
                                         "final_video", "source_type")},
                    "cover": cover, "shots": len(shots),
                    "keyframes": sum(1 for s in shots if s["keyframe"]),
                    "videos": sum(1 for s in shots if s["video"])})
    return out


@router.post("/projects")
async def create_project(body: ProjectIn) -> dict:
    values = _clean(body.model_dump(), PROJECT_FIELDS)
    _check_project(values)
    values["title"] = values.get("title") or "未命名项目"
    return db.insert("projects", values)


@router.get("/projects/{project_id}")
async def project_snapshot(project_id: str) -> dict:
    project = _get("projects", project_id, "项目")
    assets: dict[str, list[dict]] = {}
    for a in db.query("SELECT * FROM assets WHERE project_id=? ORDER BY created_at DESC", [project_id]):
        assets.setdefault(a["owner_id"], []).append(a)
    tasks = db.query("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 200", [project_id])
    return {"project": project, "characters": pipeline.characters_of(project_id),
            "locations": pipeline.locations_of(project_id), "props": pipeline.props_of(project_id),
            "shots": pipeline.shots_of(project_id),
            "assets": assets, "tasks": tasks}


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, body: dict[str, Any]) -> dict:
    _get("projects", project_id, "项目")
    values = _clean(body, PROJECT_FIELDS)
    _check_project(values)
    if "dub_mode" in values:
        values["dub_mode"] = 1 if values["dub_mode"] else 0
    return db.update("projects", project_id, values)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str) -> dict:
    _get("projects", project_id, "项目")
    for t in db.query("SELECT id FROM tasks WHERE project_id=? AND status IN ('queued','running','paused')", [project_id]):
        queue.cancel(t["id"])
    db.delete_project(project_id)
    shutil.rmtree(get_settings().media_dir / project_id, ignore_errors=True)
    return {"ok": True}


@router.post("/projects/{project_id}/script/generate")
async def gen_script(project_id: str) -> dict:
    _get("projects", project_id, "项目")
    return _submit("script", "生成剧本", project_id, "project", project_id + ":script")


@router.post("/projects/{project_id}/extract")
async def gen_extract(project_id: str) -> dict:
    _get("projects", project_id, "项目")
    return _submit("extract", "提取角色与场景", project_id, "project", project_id + ":extract")


@router.post("/projects/{project_id}/storyboard/generate")
async def gen_storyboard(project_id: str) -> dict:
    _get("projects", project_id, "项目")
    return _submit("storyboard", "生成分镜表", project_id, "project", project_id + ":storyboard")


class AssembleIn(BaseModel):
    subtitles: bool = True
    bgm: str = ""
    bgm_volume: float = Field(0.25, ge=0, le=2)
    quality: str = "final"


@router.post("/projects/{project_id}/assemble")
async def gen_assemble(project_id: str, body: AssembleIn) -> dict:
    _get("projects", project_id, "项目")
    return _submit("assemble", "合成成片", project_id, "project", project_id + ":assemble", body.model_dump())


@router.post("/projects/{project_id}/upload")
async def upload_file(project_id: str, file: UploadFile = File(...)) -> dict:
    if project_id != "toolbox":
        _get("projects", project_id, "项目")
    target = await _save_upload(file, project_id, "uploads")
    return {"path": pipeline.rel(target), "name": file.filename}


# ================= 角色 =================
CHAR_FIELDS = {"name", "role", "gender", "age", "appearance", "personality", "voice", "locked", "sort"}


@router.post("/projects/{project_id}/characters")
async def create_character(project_id: str, body: dict[str, Any]) -> dict:
    _get("projects", project_id, "项目")
    values = _clean(body, CHAR_FIELDS)
    values.setdefault("name", "新角色")
    values.setdefault("sort", len(pipeline.characters_of(project_id)))
    return db.insert("characters", {**values, "project_id": project_id})


@router.patch("/characters/{char_id}")
async def update_character(char_id: str, body: dict[str, Any]) -> dict:
    _get("characters", char_id, "角色")
    values = _clean(body, CHAR_FIELDS | {"ref_image", "face_image", "voice_ref"})
    if "locked" in values:
        values["locked"] = 1 if values["locked"] else 0
    return db.update("characters", char_id, values)


@router.delete("/characters/{char_id}")
async def delete_character(char_id: str) -> dict:
    char = _get("characters", char_id, "角色")
    db.execute("DELETE FROM characters WHERE id=?", [char_id])
    for s in pipeline.shots_of(char["project_id"]):
        if char_id in s["character_ids"]:
            db.update("shots", s["id"], {"character_ids": [i for i in s["character_ids"] if i != char_id]})
    return {"ok": True}


class GenIn(BaseModel):
    count: int = Field(2, ge=1, le=8)
    prompt: str = ""
    seed: int | None = None


@router.post("/characters/{char_id}/portrait")
async def gen_portrait(char_id: str, body: GenIn) -> dict:
    char = _get("characters", char_id, "角色")
    return _submit("portrait", f"角色形象：{char['name']}", char["project_id"], "character", char_id,
                   {"character_id": char_id, **body.model_dump()})


@router.post("/characters/{char_id}/upload")
async def upload_portrait(char_id: str, file: UploadFile = File(...)) -> dict:
    char = _get("characters", char_id, "角色")
    target = await _save_upload(file, char["project_id"], "characters")
    asset = pipeline.add_asset(char["project_id"], "portrait", target, "character", char_id, {"uploaded": True})
    return db.update("characters", char_id, {"ref_image": asset["path"]})


@router.post("/characters/{char_id}/face")
async def gen_face(char_id: str, body: GenIn) -> dict:
    """根据当前定妆照派生脸部特写（近景/特写镜头和人脸修正都用它）。"""
    char = _get("characters", char_id, "角色")
    return _submit("face_ref", f"脸部特写：{char['name']}", char["project_id"], "character", char_id,
                   {"character_id": char_id, "auto_select": True, **body.model_dump()})


@router.post("/characters/{char_id}/face/upload")
async def upload_face(char_id: str, file: UploadFile = File(...)) -> dict:
    char = _get("characters", char_id, "角色")
    target = await _save_upload(file, char["project_id"], "characters")
    asset = pipeline.add_asset(char["project_id"], "face", target, "character", char_id,
                               {"uploaded": True, "from_ref": char.get("ref_image") or ""})
    return db.update("characters", char_id, {"face_image": asset["path"]})


@router.post("/characters/{char_id}/voice")
async def gen_voice(char_id: str) -> dict:
    """生成音色样本：H3 按「声音描述」说一段话 → 提取人声。之后所有台词用 IndexTTS2 克隆这个声音。"""
    char = _get("characters", char_id, "角色")
    return _submit("voice_sample", f"音色样本：{char['name']}", char["project_id"], "character", char_id,
                   {"character_id": char_id, "auto_select": True})


@router.post("/characters/{char_id}/voice/upload")
async def upload_voice(char_id: str, file: UploadFile = File(...)) -> dict:
    """上传 3~15 秒的干净人声作为音色样本（真人配音演员 / 喜欢的声音）。"""
    char = _get("characters", char_id, "角色")
    target = await _save_upload(file, char["project_id"], "voices")
    if target.suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".webm", ".mkv"}:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "请上传音频文件")
    from . import media
    wav = target.with_name(target.stem + "_voice.wav")
    await media.trim_silence(target, wav)
    target.unlink(missing_ok=True)
    asset = pipeline.add_asset(char["project_id"], "voice", wav, "character", char_id, {"uploaded": True})
    return db.update("characters", char_id, {"voice_ref": asset["path"]})


# ================= 道具 =================
PROP_FIELDS = {"name", "description", "sort"}


@router.post("/projects/{project_id}/props")
async def create_prop(project_id: str, body: dict[str, Any]) -> dict:
    _get("projects", project_id, "项目")
    values = _clean(body, PROP_FIELDS)
    values.setdefault("name", "新道具")
    values.setdefault("sort", len(pipeline.props_of(project_id)))
    return db.insert("props", {**values, "project_id": project_id})


@router.patch("/props/{prop_id}")
async def update_prop(prop_id: str, body: dict[str, Any]) -> dict:
    _get("props", prop_id, "道具")
    return db.update("props", prop_id, _clean(body, PROP_FIELDS | {"ref_image"}))


@router.delete("/props/{prop_id}")
async def delete_prop(prop_id: str) -> dict:
    prop = _get("props", prop_id, "道具")
    db.execute("DELETE FROM props WHERE id=?", [prop_id])
    for s in pipeline.shots_of(prop["project_id"]):
        if prop_id in (s.get("prop_ids") or []):
            db.update("shots", s["id"], {"prop_ids": [i for i in s["prop_ids"] if i != prop_id]})
    return {"ok": True}


@router.post("/props/{prop_id}/image")
async def gen_prop(prop_id: str, body: GenIn) -> dict:
    prop = _get("props", prop_id, "道具")
    return _submit("prop_image", f"道具图：{prop['name']}", prop["project_id"], "prop", prop_id,
                   {"prop_id": prop_id, **body.model_dump()})


@router.post("/props/{prop_id}/upload")
async def upload_prop(prop_id: str, file: UploadFile = File(...)) -> dict:
    prop = _get("props", prop_id, "道具")
    target = await _save_upload(file, prop["project_id"], "props")
    asset = pipeline.add_asset(prop["project_id"], "prop", target, "prop", prop_id, {"uploaded": True})
    return db.update("props", prop_id, {"ref_image": asset["path"]})


# ================= 场景 =================
LOC_FIELDS = {"name", "description", "sort"}


@router.post("/projects/{project_id}/locations")
async def create_location(project_id: str, body: dict[str, Any]) -> dict:
    _get("projects", project_id, "项目")
    values = _clean(body, LOC_FIELDS)
    values.setdefault("name", "新场景")
    values.setdefault("sort", len(pipeline.locations_of(project_id)))
    return db.insert("locations", {**values, "project_id": project_id})


@router.patch("/locations/{loc_id}")
async def update_location(loc_id: str, body: dict[str, Any]) -> dict:
    _get("locations", loc_id, "场景")
    return db.update("locations", loc_id, _clean(body, LOC_FIELDS | {"ref_image"}))


@router.delete("/locations/{loc_id}")
async def delete_location(loc_id: str) -> dict:
    loc = _get("locations", loc_id, "场景")
    db.execute("DELETE FROM locations WHERE id=?", [loc_id])
    db.execute("UPDATE shots SET location_id='' WHERE project_id=? AND location_id=?", [loc["project_id"], loc_id])
    return {"ok": True}


@router.post("/locations/{loc_id}/image")
async def gen_location(loc_id: str, body: GenIn) -> dict:
    loc = _get("locations", loc_id, "场景")
    return _submit("location_image", f"场景图：{loc['name']}", loc["project_id"], "location", loc_id,
                   {"location_id": loc_id, **body.model_dump()})


@router.post("/locations/{loc_id}/upload")
async def upload_location(loc_id: str, file: UploadFile = File(...)) -> dict:
    loc = _get("locations", loc_id, "场景")
    target = await _save_upload(file, loc["project_id"], "locations")
    asset = pipeline.add_asset(loc["project_id"], "location", target, "location", loc_id, {"uploaded": True})
    return db.update("locations", loc_id, {"ref_image": asset["path"]})


# ================= 分镜 =================
SHOT_FIELDS = {"scene", "location_id", "shot_size", "camera_angle", "camera_move", "description", "action",
               "character_ids", "prop_ids", "dialogue", "sound", "duration", "image_prompt", "video_prompt", "keyframe", "video"}


def _renumber(project_id: str, ids: list[str]) -> None:
    for i, sid in enumerate(ids):
        db.execute("UPDATE shots SET sort=? WHERE id=? AND project_id=?", [i, sid, project_id])


@router.post("/projects/{project_id}/shots")
async def create_shot(project_id: str, body: dict[str, Any]) -> dict:
    _get("projects", project_id, "项目")
    values = _clean(body, SHOT_FIELDS)
    values.setdefault("description", "")
    shot = db.insert("shots", {**values, "project_id": project_id, "sort": 10**6})
    ids = [s["id"] for s in pipeline.shots_of(project_id) if s["id"] != shot["id"]]
    after = body.get("after_id")
    pos = ids.index(after) + 1 if after in ids else len(ids)
    ids.insert(pos, shot["id"])
    _renumber(project_id, ids)
    return db.get("shots", shot["id"])


@router.patch("/shots/{shot_id}")
async def update_shot(shot_id: str, body: dict[str, Any]) -> dict:
    _get("shots", shot_id, "镜头")
    values = _clean(body, SHOT_FIELDS)
    if "duration" in values:
        values["duration"] = round(min(max(float(values["duration"]), 1.0), 20.0), 1)
    return db.update("shots", shot_id, values)


@router.delete("/shots/{shot_id}")
async def delete_shot(shot_id: str) -> dict:
    shot = _get("shots", shot_id, "镜头")
    db.execute("DELETE FROM shots WHERE id=?", [shot_id])
    _renumber(shot["project_id"], [s["id"] for s in pipeline.shots_of(shot["project_id"])])
    return {"ok": True}


class ReorderIn(BaseModel):
    ids: list[str]


@router.post("/projects/{project_id}/shots/reorder")
async def reorder_shots(project_id: str, body: ReorderIn) -> dict:
    existing = {s["id"] for s in pipeline.shots_of(project_id)}
    if set(body.ids) != existing:
        raise HTTPException(400, "镜头列表不一致，请刷新后重试")
    _renumber(project_id, body.ids)
    return {"ok": True}


@router.get("/shots/{shot_id}/plan")
async def shot_plan(shot_id: str) -> dict:
    """预览这一镜头实际会发给模型的提示词和参考图（所见即所得，不再有隐藏的改写）。"""
    shot = _get("shots", shot_id, "镜头")
    k = pipeline.keyframe_plan(shot)
    try:
        v = pipeline.video_plan(shot)
        video = {"mode": v["mode"], "prompt": v["prompt"], "refs": [pipeline.rel(p) for p in v["refs"]]}
    except ValueError as exc:
        video = {"mode": "", "prompt": "", "refs": [], "error": str(exc)}
    return {"keyframe": {"prompt": k["prompt"], "refs": [pipeline.rel(p) for p in k["refs"]],
                         "labels": k["ref_labels"]}, "video": video}


@router.post("/shots/{shot_id}/keyframe")
async def gen_keyframe(shot_id: str, body: GenIn) -> dict:
    shot = _get("shots", shot_id, "镜头")
    return _submit("keyframe", f"分镜图 #{shot['sort'] + 1}", shot["project_id"], "shot", shot_id,
                   {"shot_id": shot_id, **body.model_dump()})


class EditIn(BaseModel):
    instruction: str
    count: int = Field(1, ge=1, le=4)


@router.post("/shots/{shot_id}/keyframe/edit")
async def edit_keyframe(shot_id: str, body: EditIn) -> dict:
    shot = _get("shots", shot_id, "镜头")
    return _submit("keyframe_edit", f"改图 #{shot['sort'] + 1}", shot["project_id"], "shot", shot_id,
                   {"shot_id": shot_id, **body.model_dump()}, dedupe=False)


class VideoIn(BaseModel):
    mode: str = "auto"
    quality: str = "draft"
    turbo: bool = True
    prompt: str = ""
    seconds: float | None = None
    seed: int | None = None


@router.post("/shots/{shot_id}/video")
async def gen_video(shot_id: str, body: VideoIn) -> dict:
    shot = _get("shots", shot_id, "镜头")
    return _submit("video", f"镜头视频 #{shot['sort'] + 1}", shot["project_id"], "shot", shot_id,
                   {"shot_id": shot_id, **body.model_dump()})


class FaceFixItem(BaseModel):
    character_id: str
    position: str | int = "auto"   # "auto"=最大的脸；0,1,2…=从左数第 N+1 张脸；"skip"=不处理


class FaceFixIn(BaseModel):
    target: str = "video"          # video | keyframe
    items: list[FaceFixItem] | None = None
    enhance: bool = True
    swapper_model: str = "hyperswap_1a_256"


@router.post("/shots/{shot_id}/facefix")
async def face_fix(shot_id: str, body: FaceFixIn) -> dict:
    """人脸修正：用出镜角色的脸部特写对当前视频/分镜图换脸，结果作为新版本并设为当前使用（可在候选里切回）。"""
    shot = _get("shots", shot_id, "镜头")
    field = "keyframe" if body.target == "keyframe" else "video"
    if not pipeline.exists(shot.get(field)):
        raise HTTPException(400, "这个镜头还没有" + ("分镜图" if field == "keyframe" else "视频"))
    items = [i.model_dump() for i in body.items] if body.items is not None else None
    try:
        pipeline.face_fix_plan(shot, items)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _submit("face_fix", f"人脸修正 #{shot['sort'] + 1}", shot["project_id"], "shot", shot_id,
                   {"shot_id": shot_id, **body.model_dump(), "target": field, "items": items}, dedupe=False)


class DubIn(BaseModel):
    lipsync: str = "auto"      # auto：单人中近景才对口型 | on：强制 | off：只换声音


@router.post("/shots/{shot_id}/dub")
async def dub_shot(shot_id: str, body: DubIn) -> dict:
    """固定音色配音：IndexTTS2 用角色音色样本合成台词 → 去掉原视频人声 → LatentSync 对口型 → 作为新版本。"""
    shot = _get("shots", shot_id, "镜头")
    if not pipeline.exists(shot.get("video")):
        raise HTTPException(400, "这个镜头还没有视频")
    voiced, missing = pipeline.dub_lines(shot)
    if not voiced:
        raise HTTPException(400, "本镜头没有可配音的台词" + (f"：角色「{'、'.join(missing)}」还没有音色样本" if missing else ""))
    if not voice.tts_available():
        raise HTTPException(400, "未安装配音组件，请在服务器运行 bash deploy/06_voice.sh")
    return _submit("dub", f"配音 #{shot['sort'] + 1}", shot["project_id"], "shot", shot_id,
                   {"shot_id": shot_id, "lipsync": body.lipsync})


@router.post("/shots/{shot_id}/upload")
async def upload_shot_media(shot_id: str, file: UploadFile = File(...)) -> dict:
    shot = _get("shots", shot_id, "镜头")
    target = await _save_upload(file, shot["project_id"], "uploads")
    is_img = facefusion.is_image(target)
    asset = pipeline.add_asset(shot["project_id"], "keyframe" if is_img else "video", target, "shot", shot_id,
                               {"uploaded": True})
    return db.update("shots", shot_id, {"keyframe" if is_img else "video": asset["path"]})


class BatchIn(BaseModel):
    only_missing: bool = True
    count: int = Field(1, ge=1, le=8)
    quality: str = "draft"
    turbo: bool = True
    mode: str = "auto"


@router.post("/projects/{project_id}/batch/{kind}")
async def batch(project_id: str, kind: str, body: BatchIn) -> dict:
    _get("projects", project_id, "项目")
    submitted = []
    bid = f"{kind}-{db.new_id()[:6]}"           # 批量任务标记：调度时让位给手动任务，可整体暂停

    def _submit(type_: str, title: str, project_id: str, owner_type: str = "", owner_id: str = "",
                params: dict | None = None) -> dict:
        return queue.submit(type_, project_id=project_id, title=title, owner_type=owner_type, owner_id=owner_id,
                            params={**(params or {}), "_batch": bid})

    if kind == "portraits":
        for c in pipeline.characters_of(project_id):
            if not (body.only_missing and c["ref_image"]):
                submitted.append(_submit("portrait", f"角色形象：{c['name']}", project_id, "character", c["id"],
                                         {"character_id": c["id"], "count": body.count}))
            elif not c.get("face_image"):
                submitted.append(_submit("face_ref", f"脸部特写：{c['name']}", project_id, "character", c["id"],
                                         {"character_id": c["id"], "count": 1}))
        for loc in pipeline.locations_of(project_id):
            if not (body.only_missing and loc["ref_image"]):
                submitted.append(_submit("location_image", f"场景图：{loc['name']}", project_id, "location",
                                         loc["id"], {"location_id": loc["id"], "count": body.count}))
        for pr in pipeline.props_of(project_id):
            if not (body.only_missing and pr["ref_image"]):
                submitted.append(_submit("prop_image", f"道具图：{pr['name']}", project_id, "prop",
                                         pr["id"], {"prop_id": pr["id"], "count": body.count}))
    elif kind == "voices":
        for c in pipeline.characters_of(project_id):
            if body.only_missing and c.get("voice_ref"):
                continue
            if c.get("ref_image") or c.get("face_image"):
                submitted.append(_submit("voice_sample", f"音色样本：{c['name']}", project_id, "character", c["id"],
                                         {"character_id": c["id"], "auto_select": True}))
    elif kind == "dub":
        if not voice.tts_available():
            raise HTTPException(400, "未安装配音组件，请在服务器运行 bash deploy/06_voice.sh")
        for s in pipeline.shots_of(project_id):
            done = db.query("SELECT meta FROM assets WHERE path=? LIMIT 1", [s["video"]]) if s["video"] else []
            if not s["video"] or not pipeline.dub_lines(s)[0]:
                continue
            if body.only_missing and done and (done[0].get("meta") or {}).get("dub"):
                continue
            submitted.append(_submit("dub", f"配音 #{s['sort'] + 1}", project_id, "shot", s["id"],
                                     {"shot_id": s["id"], "lipsync": "auto"}))
    elif kind in {"keyframes", "videos"}:
        for s in pipeline.shots_of(project_id):
            field = "keyframe" if kind == "keyframes" else "video"
            if body.only_missing and s[field]:
                continue
            if kind == "keyframes":
                submitted.append(_submit("keyframe", f"分镜图 #{s['sort'] + 1}", project_id, "shot", s["id"],
                                         {"shot_id": s["id"], "count": body.count}))
            else:
                submitted.append(_submit("video", f"镜头视频 #{s['sort'] + 1}", project_id, "shot", s["id"],
                                         {"shot_id": s["id"], "quality": body.quality, "turbo": body.turbo,
                                          "mode": body.mode}))
    else:
        raise HTTPException(400, "未知批量类型")
    return {"submitted": len(submitted), "batch": bid}


# ================= 素材 =================
class SelectIn(BaseModel):
    asset_id: str


@router.post("/assets/select")
async def select_asset(body: SelectIn) -> dict:
    """把某个候选素材设为当前使用（角色参考图 / 场景参考图 / 分镜图 / 镜头视频）。"""
    asset = _get("assets", body.asset_id, "素材")
    if asset["owner_type"] == "character":
        field = {"face": "face_image", "voice": "voice_ref"}.get(asset["kind"], "ref_image")
        return db.update("characters", asset["owner_id"], {field: asset["path"]})
    if asset["owner_type"] == "prop":
        return db.update("props", asset["owner_id"], {"ref_image": asset["path"]})
    if asset["owner_type"] == "location":
        return db.update("locations", asset["owner_id"], {"ref_image": asset["path"]})
    if asset["owner_type"] == "shot":
        field = "keyframe" if facefusion.is_image(Path(asset["path"])) else "video"
        return db.update("shots", asset["owner_id"], {field: asset["path"]})
    if asset["owner_type"] == "project" and asset["kind"] == "final":
        return db.update("projects", asset["owner_id"], {"final_video": asset["path"]})
    raise HTTPException(400, "该素材不能设为当前使用")


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: str) -> dict:
    asset = _get("assets", asset_id, "素材")
    db.execute("DELETE FROM assets WHERE id=?", [asset_id])
    for table, field in (("characters", "ref_image"), ("characters", "face_image"), ("characters", "voice_ref"),
                         ("locations", "ref_image"), ("props", "ref_image"), ("shots", "keyframe"),
                         ("shots", "video"), ("projects", "final_video")):
        db.execute(f"UPDATE {table} SET {field}='' WHERE {field}=?", [asset["path"]])
    try:
        pipeline.absolute(asset["path"]).unlink(missing_ok=True)
    except ValueError:
        pass
    return {"ok": True}


# ================= 任务 =================
@router.get("/tasks")
async def list_tasks(project_id: str = "", active: bool = False, limit: int = 100) -> list[dict]:
    sql, args = "SELECT * FROM tasks WHERE 1=1", []
    if project_id:
        sql += " AND project_id=?"
        args.append(project_id)
    if active:
        sql += " AND status IN ('queued','running','cancelling','paused')"
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    return db.query(sql, args)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    t = queue.cancel(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str) -> dict:
    t = queue.retry(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@router.post("/tasks/pause")
async def pause_tasks(project_id: str = "", batch_only: bool = False) -> dict:
    """暂停排队中的任务（正在运行的会跑完），之后可「继续」。"""
    return {"paused": queue.pause(project_id, batch_only)}


@router.post("/tasks/resume")
async def resume_tasks(project_id: str = "") -> dict:
    return {"resumed": queue.resume(project_id)}


@router.delete("/tasks")
async def clear_tasks(project_id: str = "") -> dict:
    sql = "DELETE FROM tasks WHERE status IN ('done','failed','cancelled')"
    args = []
    if project_id:
        sql += " AND project_id=?"
        args.append(project_id)
    db.execute(sql, args)
    return {"ok": True}


# ================= FaceFusion 工具箱 =================
@router.get("/facefusion/processors")
async def ff_processors() -> dict:
    return {"processors": facefusion.public_specs(), "common": facefusion.common_params()}


class FaceFusionIn(BaseModel):
    processor: str
    target: str
    source: str = ""
    params: dict[str, Any] = {}
    project_id: str = ""
    owner_type: str = ""
    owner_id: str = ""
    apply: bool = False


@router.post("/facefusion/run")
async def ff_run(body: FaceFusionIn) -> dict:
    spec = facefusion.public_specs().get(body.processor)
    if not spec:
        raise HTTPException(400, "不支持的处理功能")
    for path in filter(None, [body.target, body.source]):
        if not pipeline.exists(path):
            raise HTTPException(400, f"文件不存在：{path}")
    if spec.get("source_required") and not body.source:
        raise HTTPException(400, f"{spec['label']} 需要上传来源人物图片")
    return _submit("facefusion", f"FaceFusion：{spec['label']}", body.project_id, body.owner_type,
                   body.owner_id, body.model_dump(), dedupe=False)
