"""所有生成任务的实现。每个函数是一个任务处理器，由 tasks.TaskQueue 逐个执行。

流程：① 剧本 → ② 角色/场景设定 + 参考图 → ③ 分镜表 → ④ 分镜图（多图参考）→ ⑤ 镜头视频（H3）→ ⑥ 合成成片
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any

from . import comfy, db, facefusion, llm, media, prompts, qa, videox, vlm, voice, grade, package, sound, enhance, reframe
from .config import get_settings
from .gpu import gpu
from .tasks import TaskContext, handler, queue
from .workflows import (MAX_IMAGE_REFS, MAX_VIDEO_REFS, VIDEO_SIZES, image_workflow, video_i2v_workflow,
                        video_r2v_workflow)

IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_SUFFIX = {".mp4", ".webm", ".mov", ".mkv"}


# ---------------- 路径工具 ----------------
def media_root() -> Path:
    return get_settings().media_dir.resolve()


def rel(path: Path) -> str:
    return path.resolve().relative_to(media_root()).as_posix()


def absolute(relpath: str) -> Path:
    p = (media_root() / relpath).resolve()
    if p != media_root() and media_root() not in p.parents:
        raise ValueError("非法路径")
    return p


def exists(relpath: str | None) -> bool:
    return bool(relpath) and absolute(relpath).is_file()


def project_dir(project_id: str, sub: str) -> Path:
    d = media_root() / (project_id or "toolbox") / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def need(table: str, row_id: str, label: str) -> dict:
    row = db.get(table, row_id) if row_id else None
    if not row:
        raise ValueError(f"{label}不存在或已删除")
    return row


def characters_of(project_id: str) -> list[dict]:
    return db.query("SELECT * FROM characters WHERE project_id=? ORDER BY sort, created_at", [project_id])


def locations_of(project_id: str) -> list[dict]:
    return db.query("SELECT * FROM locations WHERE project_id=? ORDER BY sort, created_at", [project_id])


def props_of(project_id: str) -> list[dict]:
    return db.query("SELECT * FROM props WHERE project_id=? ORDER BY sort, created_at", [project_id])


def shots_of(project_id: str) -> list[dict]:
    return db.query("SELECT * FROM shots WHERE project_id=? ORDER BY sort, created_at", [project_id])


def add_asset(project_id: str, kind: str, path: Path, owner_type: str, owner_id: str, meta: dict) -> dict:
    return db.insert("assets", {"project_id": project_id, "kind": kind, "path": rel(path),
                                "owner_type": owner_type, "owner_id": owner_id, "meta": meta})


def progress_slice(ctx: TaskContext, start: int, end: int, prefix: str):
    async def cb(value: int, message: str) -> None:
        await ctx.progress(start + (end - start) * value // 100, f"{prefix} {message}".strip())
    return cb


# ---------------- ① 剧本 ----------------
@handler("script")
async def task_script(ctx: TaskContext) -> dict:
    project = need("projects", ctx.project_id, "项目")
    if not (project.get("source_text") or "").strip():
        raise ValueError("请先填写创意/小说/剧本素材")
    await ctx.progress(5, "文字模型编写剧本中…")
    from . import writers
    user = prompts.script_user(project) + "\n\n" + writers.genre_text(project)
    from . import series
    if project.get("series_id"):
        user += "\n\n" + series.series_text(project)
    if project.get("beats"):
        user += "\n\n【节拍表】（剧本必须按这些节拍展开，时间点对齐）\n" + writers.beats_text(project["beats"])
    chars = characters_of(project["id"])
    if chars:
        user += "\n\n【人设卡】（沿用这些角色和名字，台词符合各自说话方式）\n" + writers.bio_text(chars)
    if (project.get("script") or "").strip():
        writers.save_version(project["id"], project["script"], "重写前")
    data = await llm.chat_json(prompts.SCRIPT_SYSTEM, user,
                               validate=prompts.validate_script, temperature=0.85, max_tokens=12000)
    values: dict[str, Any] = {"script": data["script"], "synopsis": data["synopsis"]}
    if data["title"] and (project.get("title") or "").strip() in {"", "未命名项目"}:
        values["title"] = data["title"]
    db.update("projects", project["id"], values)
    writers.save_version(project["id"], data["script"], "AI 生成")
    return {"chars": len(data["script"])}


# ---------------- ② 角色与场景 ----------------
@handler("extract")
async def task_extract(ctx: TaskContext) -> dict:
    project = need("projects", ctx.project_id, "项目")
    if not (project.get("script") or "").strip():
        raise ValueError("请先生成或填写剧本")
    await ctx.progress(5, "提取角色与场景…")
    data = await llm.chat_json(prompts.EXTRACT_SYSTEM, prompts.extract_user(project),
                               validate=prompts.validate_extract, temperature=0.5, max_tokens=8192)
    existing = {c["name"]: c for c in characters_of(project["id"])}
    added = updated = 0
    for i, c in enumerate(data["characters"]):
        old = existing.get(c["name"])
        if old is None:
            db.insert("characters", {**c, "project_id": project["id"], "sort": len(existing) + i})
            added += 1
        elif not old["locked"]:
            db.update("characters", old["id"], {k: v for k, v in c.items() if v})
            updated += 1
    locs = {l["name"]: l for l in locations_of(project["id"])}
    for i, l in enumerate(data["locations"]):
        old = locs.get(l["name"])
        if old is None:
            db.insert("locations", {**l, "project_id": project["id"], "sort": len(locs) + i})
        elif l["description"]:
            db.update("locations", old["id"], {"description": l["description"]})
    props = {pr["name"]: pr for pr in props_of(project["id"])}
    for i, pr in enumerate(data.get("props") or []):
        old = props.get(pr["name"])
        if old is None:
            db.insert("props", {**pr, "project_id": project["id"], "sort": len(props) + i})
        elif pr["description"] and not old.get("description"):
            db.update("props", old["id"], {"description": pr["description"]})
    return {"characters_added": added, "characters_updated": updated, "locations": len(data["locations"]),
            "props": len(data.get("props") or [])}


# ---------------- ③ 分镜 ----------------
@handler("storyboard")
async def task_storyboard(ctx: TaskContext) -> dict:
    project = need("projects", ctx.project_id, "项目")
    if not (project.get("script") or "").strip():
        raise ValueError("请先生成或填写剧本")
    chars, locs, props = characters_of(project["id"]), locations_of(project["id"]), props_of(project["id"])
    validate = prompts.make_storyboard_validator([c["name"] for c in chars])
    chunks = prompts.split_script(project["script"])
    rows: list[dict] = []
    prev = ""
    for i, chunk in enumerate(chunks):
        await ctx.progress(5 + 85 * i // len(chunks), f"拆分镜 {i + 1}/{len(chunks)}")
        part = await llm.chat_json(prompts.STORYBOARD_SYSTEM,
                                   prompts.storyboard_user(project, chars, locs, chunk, prev, props),
                                   validate=validate, temperature=0.5, max_tokens=12000)
        rows += part
        prev = part[-1]["description"]

    by_name = {c["name"]: c["id"] for c in chars}
    prop_ids = {pr["name"]: pr["id"] for pr in props}
    loc_names = [l["name"] for l in locs]
    loc_ids = {l["name"]: l["id"] for l in locs}
    db.execute("DELETE FROM shots WHERE project_id=?", [project["id"]])
    db.execute("DELETE FROM assets WHERE project_id=? AND owner_type='shot'", [project["id"]])
    for i, r in enumerate(rows):
        raw_loc = r["location"] or r["scene"]
        loc_name = prompts.match_name(raw_loc, loc_names)
        if loc_name is None and raw_loc:
            new_loc = db.insert("locations", {"project_id": project["id"], "name": raw_loc,
                                              "description": "", "sort": len(loc_names)})
            loc_name = new_loc["name"]
            loc_names.append(loc_name)
            loc_ids[loc_name] = new_loc["id"]
        db.insert("shots", {
            "project_id": project["id"], "sort": i, "scene": r["scene"],
            "location_id": loc_ids.get(loc_name or "", ""), "shot_size": r["shot_size"],
            "camera_angle": r["camera_angle"], "camera_move": r["camera_move"],
            "description": r["description"], "action": r["action"],
            "character_ids": [by_name[n] for n in prompts.visible_characters(r, list(by_name))],
            "prop_ids": [prop_ids[n] for n in prompts.visible_props(r, list(prop_ids))],
            "dialogue": r["dialogue"], "sound": r["sound"], "duration": r["duration"],
            "continuous": 1 if r.get("continuous") and i > 0 else 0,
        })
    return {"shots": len(rows), "seconds": round(sum(r["duration"] for r in rows), 1)}


# ---------------- 生图公共 ----------------
async def _generate_images(ctx: TaskContext, prompt: str, aspect: str, ref_paths: list[Path], count: int,
                           out_dir: Path, random_seed: bool = False, size: tuple[int, int] | None = None) -> list[Path]:
    count = max(1, min(int(count or 1), 8))
    seed = None if random_seed else ctx.params.get("seed")
    results: list[Path] = []
    span = 90 // count
    from . import art
    proj = db.get("projects", ctx.project_id) if ctx.project_id else None
    lora = art.style_lora(proj) if proj else None
    async with gpu.use("comfyui", ctx.task.get("title") or "生图"):
        await gpu.comfy_switch("image")
        refs = [await comfy.upload_image(p) for p in ref_paths]
        for i in range(count):
            s = seed + i if isinstance(seed, int) and seed >= 0 else None
            files = await comfy.run(image_workflow(prompt, aspect, refs, seed=s, lora=lora, size=size), out_dir,
                                    progress_slice(ctx, 5 + i * span, 5 + (i + 1) * span, f"第 {i + 1}/{count} 张"))
            results += [f for f in files if f.suffix.lower() in IMAGE_SUFFIX]
    if not results:
        raise RuntimeError("没有得到图片")
    return results


@handler("portrait")
async def task_portrait(ctx: TaskContext) -> dict:
    char = need("characters", ctx.params.get("character_id"), "角色")
    project = need("projects", char["project_id"], "项目")
    prompt = (ctx.params.get("prompt") or "").strip() or prompts.portrait_prompt(project, char)
    files = await _generate_images(ctx, prompt, "3:4", [], ctx.params.get("count", 2),
                                   project_dir(project["id"], "characters"))
    assets = [add_asset(project["id"], "portrait", f, "character", char["id"], {"prompt": prompt}) for f in files]
    if not exists(char["ref_image"]):
        char = db.update("characters", char["id"], {"ref_image": assets[0]["path"]})
    result = {"assets": [a["id"] for a in assets]}
    # 第一次有定妆照时，顺手派生一张脸部特写（近景/特写镜头靠它锁五官）
    if ctx.params.get("with_face", True) and not exists(char.get("face_image")):
        face = await _make_face(ctx, project, char, 1)
        result["face_assets"] = [a["id"] for a in face]
    return result


async def _make_face(ctx: TaskContext, project: dict, char: dict, count: int) -> list[dict]:
    has_ref = exists(char.get("ref_image"))
    prompt = prompts.face_prompt(project, char, has_ref)
    refs = [absolute(char["ref_image"])] if has_ref else []
    files = await _generate_images(ctx, prompt, "1:1", refs, count, project_dir(project["id"], "characters"))
    assets = [add_asset(project["id"], "face", f, "character", char["id"],
                        {"prompt": prompt, "from_ref": char.get("ref_image") or ""}) for f in files]
    current = db.get("characters", char["id"]) or char
    if not exists(current.get("face_image")) or ctx.params.get("auto_select"):
        db.update("characters", char["id"], {"face_image": assets[0]["path"]})
    return assets


@handler("face_ref")
async def task_face_ref(ctx: TaskContext) -> dict:
    """根据当前定妆照生成脸部特写（可多张候选）。"""
    char = need("characters", ctx.params.get("character_id"), "角色")
    project = need("projects", char["project_id"], "项目")
    assets = await _make_face(ctx, project, char, ctx.params.get("count", 1))
    return {"assets": [a["id"] for a in assets]}


@handler("location_image")
async def task_location_image(ctx: TaskContext) -> dict:
    loc = need("locations", ctx.params.get("location_id"), "场景")
    project = need("projects", loc["project_id"], "项目")
    prompt = (ctx.params.get("prompt") or "").strip() or prompts.location_prompt(project, loc)
    files = await _generate_images(ctx, prompt, project["aspect_ratio"], [], ctx.params.get("count", 2),
                                   project_dir(project["id"], "locations"))
    assets = [add_asset(project["id"], "location", f, "location", loc["id"], {"prompt": prompt}) for f in files]
    if not exists(loc["ref_image"]):
        db.update("locations", loc["id"], {"ref_image": assets[0]["path"]})
    return {"assets": [a["id"] for a in assets]}


@handler("prop_image")
async def task_prop_image(ctx: TaskContext) -> dict:
    prop = need("props", ctx.params.get("prop_id"), "道具")
    project = need("projects", prop["project_id"], "项目")
    prompt = (ctx.params.get("prompt") or "").strip() or prompts.prop_prompt(project, prop)
    files = await _generate_images(ctx, prompt, "1:1", [], ctx.params.get("count", 2), project_dir(project["id"], "props"))
    assets = [add_asset(project["id"], "prop", f, "prop", prop["id"], {"prompt": prompt}) for f in files]
    if not exists(prop["ref_image"]):
        db.update("props", prop["id"], {"ref_image": assets[0]["path"]})
    return {"assets": [a["id"] for a in assets]}


def shot_props(shot: dict) -> list[dict]:
    all_props = {pr["id"]: pr for pr in props_of(shot["project_id"])}
    return [all_props[i] for i in shot.get("prop_ids") or [] if i in all_props]


def _qa_faces(chars: list[dict]) -> list[tuple[str, Path]]:
    out = []
    for c in chars:
        src = c.get("face_image") if exists(c.get("face_image")) else c.get("ref_image")
        if exists(src):
            out.append((c["name"], absolute(src)))
    return out


def _expects_text(shot: dict) -> bool:
    """画面描述里本来就要求出现文字（招牌、信件内容等）时，分镜图不查文字。"""
    text = (shot.get("description") or "") + (shot.get("image_prompt") or "")
    return bool(re.search(r"[“”「」\"]|写着|字样|招牌|标语|文字|字迹|笔迹", text))


def _shot_context(shot: dict) -> tuple[dict, dict[str, dict], list[dict], dict | None]:
    project = need("projects", shot["project_id"], "项目")
    all_chars = {c["id"]: c for c in characters_of(project["id"])}
    chars = [all_chars[i] for i in shot.get("character_ids") or [] if i in all_chars]
    loc = db.get("locations", shot["location_id"]) if shot.get("location_id") else None
    return project, all_chars, chars, loc


def char_refs(chars: list[dict], limit: int, start: int = 1, faces: bool = True) -> tuple[list[Path], list[str], dict]:
    """角色参考图编排：先保证每个角色都有一张（全身优先），名额有剩再补脸部特写。
    返回 (图片路径, 标签, ref_map)；ref_map = {角色id: {"full": 序号, "face": 序号}}，序号从 start 开始。"""
    refs: list[Path] = []
    labels: list[str] = []
    ref_map: dict[str, dict] = {}

    def add(c: dict, field: str, kind: str, label: str) -> None:
        refs.append(absolute(c[field]))
        tag = c.get("_view") if field == "ref_image" else c.get("_expr")
        labels.append(f"{label}:{c['name']}" + (f"（{tag}）" if tag else "") + (f"[{c['_outfit'].split('——')[0]}]" if field == "ref_image" and c.get("_outfit") else ""))
        ref_map.setdefault(c["id"], {})[kind] = start + len(refs) - 1

    for c in chars:
        if len(refs) >= limit:
            break
        if exists(c.get("ref_image")):
            add(c, "ref_image", "full", "角色")
        elif exists(c.get("face_image")):
            add(c, "face_image", "face", "脸部")
    if faces:
        for c in chars:
            if len(refs) >= limit:
                break
            m = ref_map.get(c["id"]) or {}
            if "full" in m and "face" not in m and exists(c.get("face_image")):
                add(c, "face_image", "face", "脸部")
    return refs, labels, ref_map


def keyframe_plan(shot: dict) -> dict:
    """计算某镜头的出图方案：参考图顺序 + 最终提示词（预览接口也用它）。
    场景图优先级高于「第二张脸部参考」：先给每个角色一张，再放场景图，最后用剩余名额补脸部特写。"""
    from . import art, board
    project, _, chars, loc = _shot_context(shot)
    chars = [art.dress(shot, c) for c in chars]          # 造型 / 侧面背面 / 表情
    loc = art.place(shot, loc)                           # 场景变体（夜 / 雨 / 反打…）
    props = shot_props(shot)
    has_loc = bool(loc and exists(loc.get("ref_image")))
    has_style = exists(project.get("style_ref"))
    has_pose = exists(shot.get("pose_ref"))
    base, _, _ = char_refs(chars, MAX_IMAGE_REFS, faces=False)
    reserve = (1 if has_loc else 0) + (1 if has_style else 0) + (1 if has_pose else 0) + sum(1 for pr in props if exists(pr.get("ref_image")))
    room = max(len(base), MAX_IMAGE_REFS - reserve)   # 每个角色一张优先，其次场景、道具，最后补脸部特写
    refs, labels, ref_map = char_refs(chars, room)
    loc_idx = None
    if has_loc and len(refs) < MAX_IMAGE_REFS:
        refs.append(absolute(loc["ref_image"]))
        labels.append(f"场景:{loc['name']}")
        loc_idx = len(refs)
    prop_refs: list[tuple[dict, int | None]] = []
    for pr in props:
        idx = None
        if exists(pr.get("ref_image")) and len(refs) < MAX_IMAGE_REFS:
            refs.append(absolute(pr["ref_image"]))
            labels.append(f"道具:{pr['name']}")
            idx = len(refs)
        prop_refs.append((pr, idx))
    style_idx = None
    if has_style and len(refs) < MAX_IMAGE_REFS:
        refs.append(absolute(project["style_ref"]))
        labels.append("画风参考")
        style_idx = len(refs)
    pose_idx = None
    if has_pose and len(refs) < MAX_IMAGE_REFS:
        refs.append(absolute(shot["pose_ref"]))
        labels.append("姿势参考")
        pose_idx = len(refs)
    prompt = prompts.keyframe_prompt(project, shot, chars, loc, ref_map, loc_idx, prop_refs)
    extra = board.position_lines(shot, chars) + ([board.pose_line(pose_idx)] if pose_idx else [])
    if extra:
        prompt = prompt.replace(f"画风：{prompts.style_text(project)}。", "\n".join(extra) + f"\n画风：{prompts.style_text(project)}。")
    if style_idx:
        prompt = prompt.replace(f"画风：{prompts.style_text(project)}。", "\n".join(art.style_lines(project, style_idx))
                                + f"\n画风：{prompts.style_text(project)}。")
    return {"project": project, "chars": chars, "loc": loc, "props": props, "refs": refs, "ref_labels": labels,
            "prompt": prompt}


@handler("keyframe")
async def task_keyframe(ctx: TaskContext) -> dict:
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    plan = keyframe_plan(shot)
    prompt = (ctx.params.get("prompt") or "").strip() or plan["prompt"]
    out_dir = project_dir(shot["project_id"], "keyframes")
    aspect = plan["project"]["aspect_ratio"]
    files = await _generate_images(ctx, prompt, aspect, plan["refs"], ctx.params.get("count", 1), out_dir)

    async def check(f: Path) -> dict | None:
        r = await qa.check(f, faces=_qa_faces(plan["chars"]), check_text=not _expects_text(shot),
                           style=plan["project"].get("style") or "")
        return vlm.merge_qa(r, await vlm.quick_check(shot, f))

    results = [(f, await check(f)) for f in files]
    retries = get_settings().qa_keyframe_retry if qa.available() or vlm.auto_enabled() else 0
    while retries > 0 and all(r and not r.get("ok") for _, r in results):
        retries -= 1
        await ctx.log("分镜图质检未通过（" + qa.summary(results[-1][1]) + "），自动重抽")
        for f in await _generate_images(ctx, prompt, aspect, plan["refs"], 1, out_dir, random_seed=True):
            results.append((f, await check(f)))
    assets = [add_asset(shot["project_id"], "keyframe", f, "shot", shot["id"],
                        {"prompt": prompt, "refs": plan["ref_labels"], "qa": r}) for f, r in results]
    best = min(range(len(results)), key=lambda i: qa.rank(results[i][1]))
    current = db.get("shots", shot["id"]) or shot
    if not exists(current["keyframe"]) or ctx.params.get("auto_select"):
        db.update("shots", shot["id"], {"keyframe": assets[best]["path"]})
    return {"assets": [a["id"] for a in assets], "qa": qa.summary(results[best][1]), "attempts": len(results)}


@handler("keyframe_edit")
async def task_keyframe_edit(ctx: TaskContext) -> dict:
    """指令改图：以当前分镜图为 <image1>，按文字说明修改，其余保持不变。"""
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    instruction = (ctx.params.get("instruction") or "").strip()
    if not exists(shot["keyframe"]) or not instruction:
        raise ValueError("需要先有分镜图，并填写修改说明")
    plan = keyframe_plan(shot)
    char_paths, _, ref_map = char_refs(plan["chars"], MAX_IMAGE_REFS - 1, start=2)
    refs = [absolute(shot["keyframe"]), *char_paths]
    lines = ["以 <image1> 为基础画面，保持构图、机位、人物身份、服装和画风不变，只做以下修改：" + instruction]
    lines += [ln for ln in prompts.char_ref_lines(plan["chars"], ref_map) if ln.startswith("<image")]
    lines.append("不要出现任何文字、字幕或水印。")
    prompt = "\n".join(lines)
    files = await _generate_images(ctx, prompt, plan["project"]["aspect_ratio"], refs, ctx.params.get("count", 1),
                                   project_dir(shot["project_id"], "keyframes"))
    assets = [add_asset(shot["project_id"], "keyframe", f, "shot", shot["id"],
                        {"prompt": prompt, "edit": instruction}) for f in files]
    db.update("shots", shot["id"], {"keyframe": assets[0]["path"]})
    return {"assets": [a["id"] for a in assets]}


# ---------------- ⑤ 视频 ----------------
# ---------------- 镜头衔接：连续动作用上一镜尾帧做首帧；可指定尾帧 ----------------
def prev_shot(shot: dict) -> dict | None:
    shots = shots_of(shot["project_id"])
    ids = [s["id"] for s in shots]
    i = ids.index(shot["id"]) if shot["id"] in ids else -1
    return shots[i - 1] if i > 0 else None


def chain_source(shot: dict) -> dict | None:
    """连续镜头的首帧来源：上一镜有视频 → 用它的最后一帧；只有尾帧图 → 用尾帧图；都没有 → None（退回本镜分镜图）。"""
    if not shot.get("continuous"):
        return None
    prev = prev_shot(shot)
    if not prev:
        return None
    if exists(prev.get("video")):
        return {"prev": prev, "kind": "video", "path": prev["video"]}
    if exists(prev.get("end_frame")):
        return {"prev": prev, "kind": "end_frame", "path": prev["end_frame"]}
    return None


async def chain_first_frame(ctx: TaskContext, shot: dict, src: dict) -> tuple[Path, list[str]]:
    """准备连续镜头的首帧；抽出的尾帧人脸不像时自动用 FaceFusion 修脸（需要已安装，且只修一次）。"""
    notes: list[str] = []
    if src["kind"] == "end_frame":
        return absolute(src["path"]), [f"首帧取自上一镜（#{src['prev']['sort'] + 1}）的尾帧图"]
    out_dir = project_dir(shot["project_id"], "keyframes")
    frame = await media.last_frame(absolute(src["path"]), out_dir / f"chain_{shot['id']}_{time.strftime('%Y%m%d_%H%M%S')}.png")
    notes.append(f"首帧取自上一镜（#{src['prev']['sort'] + 1}）视频的最后一帧")
    _, _, chars, _ = _shot_context(shot)
    faces = _qa_faces(chars)
    if faces and qa.available() and facefusion_available():
        r = await qa.check(frame, faces=faces, check_text=False, style=need("projects", shot["project_id"], "项目").get("style") or "")
        if r and any(i.get("type") == "face" for i in r.get("issues") or []):
            await ctx.log("上一镜尾帧的人脸不像，自动修脸…")
            try:
                frame = await _fix_faces(ctx, shot, frame, out_dir)
                notes.append("尾帧人脸已自动修正")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"尾帧修脸失败，直接使用：{str(exc)[:120]}")
    return frame, notes


def facefusion_available() -> bool:
    s = get_settings()
    return s.facefusion_python.is_file() and (s.facefusion_dir / "facefusion.py").is_file()


async def _fix_faces(ctx: TaskContext, shot: dict, image: Path, out_dir: Path) -> Path:
    plan = face_fix_plan(shot)
    current = image
    async with gpu.use("facefusion", "修正衔接帧人脸"):
        for i, step in enumerate(plan):
            params: dict[str, Any] = {"face_swapper_model": "hyperswap_1a_256", "face_swapper_pixel_boost": "512x512",
                                      "face_mask_types": ["box", "occlusion"]}
            if step["position"] == "auto":
                params.update(face_selector_mode="one", face_selector_order="large-small")
            else:
                params.update(face_selector_mode="reference", face_selector_order="left-right",
                              reference_face_position=int(step["position"]))
            output = out_dir / f"{image.stem}_ff{i}{image.suffix}"
            await facefusion.run("face_swapper", current, output, step["source"], params, log=ctx.log)
            current = output
    return current


def video_after(shot: dict) -> dict:
    """连续镜头的视频任务要等上一镜视频先做完（调度器据此排序）。"""
    if shot.get("continuous"):
        prev = prev_shot(shot)
        if prev:
            return {"_after": prev["id"]}
    return {}


END_FRAME_HINT = "保持人物身份、服装、场景、光线、机位和画风不变，画面变为这个镜头结束时的状态："


@handler("end_frame")
async def task_end_frame(ctx: TaskContext) -> dict:
    """尾帧图：以分镜图为基础，按镜头内动作画出「结束姿态」，视频首尾帧模式会让动作落到这里。"""
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    if not exists(shot["keyframe"]):
        raise ValueError("请先生成这个镜头的分镜图")
    target = (ctx.params.get("instruction") or "").strip() or (shot.get("action") or "").strip()
    if not target:
        raise ValueError("请填写镜头内动作，或写明结束时的画面")
    plan = keyframe_plan(shot)
    char_paths, _, ref_map = char_refs(plan["chars"], MAX_IMAGE_REFS - 1, start=2)
    refs = [absolute(shot["keyframe"]), *char_paths]
    lines = ["以 <image1> 为这个镜头的开头画面。" + END_FRAME_HINT + target + "。只画动作完成后的那一瞬间。"]
    lines += [ln for ln in prompts.char_ref_lines(plan["chars"], ref_map) if ln.startswith("<image")]
    lines.append("不要出现任何文字、字幕或水印。")
    prompt = "\n".join(lines)
    files = await _generate_images(ctx, prompt, plan["project"]["aspect_ratio"], refs, ctx.params.get("count", 1),
                                   project_dir(shot["project_id"], "keyframes"))
    assets = [add_asset(shot["project_id"], "end_frame", f, "shot", shot["id"], {"prompt": prompt, "end": target})
              for f in files]
    db.update("shots", shot["id"], {"end_frame": assets[0]["path"]})
    return {"assets": [a["id"] for a in assets]}


def r2v_available() -> bool:
    s = get_settings()
    return (s.comfyui_dir / "models" / "diffusion_models" / s.video_r2v_unet).is_file()


def video_plan(shot: dict, mode: str = "auto") -> dict:
    """i2v：分镜图作为首帧（推荐，画面最可控）；r2v：分镜图 + 角色参考图一起作为参考（需要 ref2va 模型）。"""
    project, all_chars, chars, loc = _shot_context(shot)
    by_name = {c["name"]: c for c in all_chars.values()}
    has_key = exists(shot.get("keyframe"))
    if mode not in {"i2v", "r2v"}:
        mode = "i2v" if has_key or not r2v_available() else "r2v"
    refs: list[Path] = []
    if mode == "i2v":
        if has_key:
            refs = [absolute(shot["keyframe"])]
        prompt = prompts.video_prompt(project, shot, by_name, loc)
        return {"project": project, "mode": mode, "refs": refs, "prompt": prompt}
    if has_key:
        refs.append(absolute(shot["keyframe"]))
    char_paths, labels, _ = char_refs(chars, MAX_VIDEO_REFS - len(refs))
    refs += char_paths
    for pr in shot_props(shot):
        if exists(pr.get("ref_image")) and len(refs) < MAX_VIDEO_REFS:
            refs.append(absolute(pr["ref_image"]))
            labels.append(f"道具:{pr['name']}")
    if not refs:
        raise ValueError("参考生视频需要分镜图或角色参考图")
    descs = []
    for label in labels:
        kind, name = label.split(":", 1)
        if kind == "道具":
            descs.append(f"道具「{name}」的外观参考，保持外观一致")
        else:
            descs.append(f"角色「{name}」的{'脸部特写，保持同一张脸' if kind == '脸部' else '形象参考，保持同一张脸和服装'}")
    prompt = prompts.video_prompt(project, shot, by_name, loc, descs if has_key else None)
    if not has_key:
        head = [f"<Picture {i}> 是{d}。" for i, d in enumerate(descs, 1)]
        prompt = "\n".join(head) + "\n" + prompt
    return {"project": project, "mode": mode, "refs": refs, "prompt": prompt}


@handler("video")
async def task_video(ctx: TaskContext) -> dict:
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    plan = video_plan(shot, ctx.params.get("mode") or "auto")
    if plan["mode"] == "r2v" and not r2v_available():
        raise RuntimeError("未下载 H3 参考生视频模型（ref2va），请改用图生视频，或运行 deploy/download_models.sh --r2v")
    project = plan["project"]
    quality = ctx.params.get("quality") if ctx.params.get("quality") in VIDEO_SIZES else "draft"
    turbo = bool(ctx.params.get("turbo", True))
    seconds = float(ctx.params.get("seconds") or shot["duration"] or 5)
    prompt = (ctx.params.get("prompt") or "").strip() or plan["prompt"]
    seed = ctx.params.get("seed") if isinstance(ctx.params.get("seed"), int) else None
    out_dir = project_dir(project["id"], "videos")
    _, _, chars, _ = _shot_context(shot)
    reference = absolute(shot["keyframe"]) if exists(shot.get("keyframe")) else None
    chain_notes: list[str] = []
    src = chain_source(shot) if ctx.params.get("chain", True) else None
    if src:                                   # 连续镜头：首帧 = 上一镜最后一帧（强制图生视频）
        frame, chain_notes = await chain_first_frame(ctx, shot, src)
        plan = {**plan, "mode": "i2v", "refs": [frame]}
        reference = frame
        if not (ctx.params.get("prompt") or "").strip():
            prompt = video_plan({**shot, "_chain": True, "_end_frame": exists(shot.get("end_frame"))}, "i2v")["prompt"]
    elif shot.get("continuous"):
        chain_notes.append("上一镜还没有视频，暂用本镜分镜图做首帧（上一镜出视频后建议重生成本镜）")
    end_frame = absolute(shot["end_frame"]) if exists(shot.get("end_frame")) and plan["mode"] == "i2v" else None
    if end_frame and not src and not (ctx.params.get("prompt") or "").strip():
        prompt = video_plan({**shot, "_end_frame": True}, "i2v")["prompt"]
    attempts = 1 + (max(0, get_settings().qa_video_retry) if qa.available() or vlm.auto_enabled() else 0)
    results: list[tuple[dict, dict | None]] = []
    for attempt in range(attempts):
        tag = f"（第 {attempt + 1} 次）" if attempt else ""
        async with gpu.use("comfyui", (ctx.task.get("title") or "生成视频") + tag):
            await gpu.comfy_switch("video")
            uploaded = [await comfy.upload_image(p) for p in plan["refs"]]
            up_end = await comfy.upload_image(end_frame) if end_frame else None
            s_ = seed if attempt == 0 else None
            if plan["mode"] == "r2v":
                wf = video_r2v_workflow(prompt, uploaded, project["aspect_ratio"], seconds, quality, turbo, s_)
            else:
                wf = video_i2v_workflow(prompt, uploaded[0] if uploaded else None, project["aspect_ratio"], seconds,
                                        quality, turbo, last_frame=up_end, seed=s_)
            cam_lora = videox.camera_loras().get(shot.get("camera_move") or "")
            if cam_lora:
                videox.chain_lora(wf, cam_lora)
            span = 94 // attempts
            files = await comfy.run(wf, out_dir, progress_slice(ctx, 3 + attempt * span, 3 + (attempt + 1) * span,
                                                                 "生成视频" + tag))
        videos = [f for f in files if f.suffix.lower() in VIDEO_SUFFIX]
        if not videos:
            raise RuntimeError("ComfyUI 没有产出视频文件")
        await ctx.log("自动质检中…")
        result = await qa.check(videos[0], reference=reference, faces=_qa_faces(chars), check_text=True,
                                style=project.get("style") or "")
        result = vlm.merge_qa(result, await vlm.quick_check(shot, videos[0]))
        asset = add_asset(project["id"], "video", videos[0], "shot", shot["id"],
                          {"prompt": prompt, "mode": plan["mode"], "quality": quality, "seconds": seconds,
                           "turbo": turbo, "qa": result, "attempt": attempt + 1,
                           "chain_from": src["path"] if src else "", "end_frame": rel(end_frame) if end_frame else "",
                           "notes": chain_notes})
        results.append((asset, result))
        if not result or result.get("ok"):
            break
        if attempt + 1 < attempts:
            await ctx.log(f"质检未通过（{qa.summary(result)}），自动换种子重抽")
    best, best_qa = min(results, key=lambda x: qa.rank(x[1]))
    db.update("shots", shot["id"], {"video": best["path"]})
    out = {"asset": best["id"], "attempts": len(results), "qa": qa.summary(best_qa), "notes": chain_notes}
    fresh = db.get("shots", shot["id"]) or shot
    if project.get("dub_mode") and voice.tts_available() and dub_lines(fresh)[0]:
        t = queue.submit("dub", project_id=project["id"], title=f"配音 #{shot['sort'] + 1}", owner_type="shot",
                         owner_id=shot["id"], params={"shot_id": shot["id"], "lipsync": "auto"})
        out["dub_task"] = t["id"]
    return out


# ---------------- ⑤-2 固定音色配音 + 对口型 ----------------
@handler("voice_sample")
async def task_voice_sample(ctx: TaskContext) -> dict:
    """用 H3 按角色的「声音描述」说一段话，提取成音色样本（之后所有台词都用 IndexTTS2 克隆这个声音）。"""
    char = need("characters", ctx.params.get("character_id"), "角色")
    project = need("projects", char["project_id"], "项目")
    face = char.get("face_image") if exists(char.get("face_image")) else char.get("ref_image")
    if not exists(face):
        raise ValueError("请先生成角色的定妆照或脸部特写")
    prompt, line = prompts.voice_sample_prompt(project, char)
    out_dir = project_dir(project["id"], "voices")
    aspect = "1:1" if "1:1" in VIDEO_SIZES["draft"] else project["aspect_ratio"]
    async with gpu.use("comfyui", ctx.task.get("title") or "音色样本"):
        await gpu.comfy_switch("video")
        uploaded = await comfy.upload_image(absolute(face))
        files = await comfy.run(video_i2v_workflow(prompt, uploaded, aspect, 6.0, "draft", True),
                                out_dir, progress_slice(ctx, 5, 85, "生成音色样本"))
    video = next((f for f in files if f.suffix.lower() in VIDEO_SUFFIX), None)
    if not video:
        raise RuntimeError("ComfyUI 没有产出视频文件")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = out_dir / f"work_{ctx.id}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        raw = await media.extract_audio(video, work / "raw.wav")
        if not raw:
            raise RuntimeError("视频模型没有生成声音，请重试")
        src = raw
        if voice.separator_available():
            await ctx.progress(90, "提取人声…")
            try:
                src = await voice.isolate_vocals(raw, work)
            except Exception as exc:  # noqa: BLE001
                await ctx.log(f"人声分离失败，使用原声：{exc}"[:200])
        final = await media.trim_silence(src, out_dir / f"voice_{char['id']}_{stamp}.wav")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if (await media.probe(final))["duration"] < 1.0:
        raise RuntimeError("音色样本太短（视频模型可能没有说话），请重试")
    asset = add_asset(project["id"], "voice", final, "character", char["id"],
                      {"line": line, "video": rel(video), "prompt": prompt})
    current = db.get("characters", char["id"]) or char
    if not exists(current.get("voice_ref")) or ctx.params.get("auto_select"):
        db.update("characters", char["id"], {"voice_ref": asset["path"]})
    return {"asset": asset["id"]}


def dub_lines(shot: dict) -> tuple[list[tuple[dict, dict]], list[str]]:
    """返回 (可配音的 [(台词, 角色)], 缺音色样本的角色名)。旁白暂不配音（保留字幕）。"""
    by_name = {c["name"]: c for c in characters_of(shot["project_id"])}
    voiced, missing = [], []
    for d in shot.get("dialogue") or []:
        c = by_name.get(d.get("speaker") or "")
        if not c or not str(d.get("line") or "").strip():
            continue
        if exists(c.get("voice_ref")):
            voiced.append((d, c))
        elif c["name"] not in missing:
            missing.append(c["name"])
    return voiced, missing


LIPSYNC_SIZES = {"中景", "近景", "特写"}


@handler("dub")
async def task_dub(ctx: TaskContext) -> dict:
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    project, _, chars, _ = _shot_context(shot)
    if not exists(shot.get("video")):
        raise ValueError("这个镜头还没有视频")
    voiced, missing = dub_lines(shot)
    if not voiced:
        raise ValueError("本镜头没有可配音的台词" + (f"：角色「{'、'.join(missing)}」还没有音色样本" if missing else ""))
    src_video = absolute(shot["video"])
    cur = db.query("SELECT * FROM assets WHERE path=? LIMIT 1", [shot["video"]])
    if cur and (cur[0].get("meta") or {}).get("dub_source") and exists(cur[0]["meta"]["dub_source"]):
        src_video = absolute(cur[0]["meta"]["dub_source"])      # 重新配音时从未配音的原视频开始，不叠加
    work = project_dir(project["id"], "dub") / f"work_{ctx.id}"
    work.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    lips = False
    factor = 1.0
    try:
        vdur = (await media.probe(src_video))["duration"] or float(shot.get("duration") or 5)
        items = [{"text": d["line"], "voice": str(absolute(c["voice_ref"])), "emo": d.get("emotion") or "",
                  "out": str(work / f"l{i}.wav")} for i, (d, c) in enumerate(voiced)]
        await ctx.progress(5, f"合成 {len(items)} 句台词…")
        async with gpu.use("voice", ctx.task.get("title") or "配音"):
            await voice.synthesize(items, ctx.log)
        wavs = [Path(it["out"]) for it in items]
        durs = [(await media.probe(w))["duration"] for w in wavs]
        lead, gap, tail = 0.3, 0.25, 0.3
        need_len = lead + sum(durs) + gap * (len(durs) - 1) + tail
        if need_len > vdur * 1.02:      # 台词比画面长：先最多加速 25%，还不够就定格最后一帧补时长
            factor = min(1.25, need_len / vdur)
            wavs = [await media.atempo(w, factor, work / f"t{i}.wav") for i, w in enumerate(wavs)]
            durs = [d / factor for d in durs]
            need_len = lead + sum(durs) + gap * (len(durs) - 1) + tail
        video, total = src_video, vdur
        if need_len > vdur + 0.05:
            video = await media.extend_video(src_video, need_len - vdur, work / "ext.mp4")
            total = need_len
            notes.append(f"台词较长，画面定格延长 {need_len - vdur:.1f} 秒")
        starts, t = [], lead
        for d in durs:
            starts.append(t)
            t += d + gap
        speech = await media.speech_track(list(zip(wavs, starts)), total, work / "speech.wav")
        dlg = shot.get("dialogue") or []
        timing = [{"j": next((k for k, x in enumerate(dlg) if x is d), -1), "start": round(st, 3), "dur": round(du, 3)}
                  for (d, _), st, du in zip(voiced, starts, durs)]

        await ctx.progress(35, "处理环境音…")
        tracks: list[tuple[Path, float]] = [(speech, 1.0)]
        orig = await media.extract_audio(video, work / "orig.wav")
        if orig:
            if voice.separator_available():
                try:
                    tracks.append((await voice.remove_vocals(orig, work / "sep"), 0.9))
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"去人声失败，原声已压低：{exc}"[:200])
                    tracks.append((orig, 0.15))
            elif project.get("dub_mode"):
                tracks.append((orig, 0.8))
            else:
                tracks.append((orig, 0.15))
                notes.append("未安装去人声组件，原声已压低音量")

        mode = ctx.params.get("lipsync") or "auto"
        speakers = {c["id"] for _, c in voiced}
        auto_ok = len(chars) == 1 and speakers == {chars[0]["id"]} and shot.get("shot_size") in LIPSYNC_SIZES
        if mode == "on" or (mode == "auto" and auto_ok):
            if voice.lipsync_available():
                await ctx.progress(50, "对口型（LatentSync）…")
                try:
                    async with gpu.use("voice", "对口型"):
                        video = await voice.lipsync(video, speech, work / "lips.mp4", ctx.log)
                    lips = True
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"对口型失败，保留原口型：{str(exc)[-160:]}")
            else:
                notes.append("未安装对口型组件，只替换了声音")
        elif mode == "auto":
            notes.append("多人或远景镜头，未做对口型（可手动强制）")
        final_dur = (await media.probe(video))["duration"] or total
        await ctx.progress(92, "混音…")
        stamp = time.strftime('%Y%m%d_%H%M%S')
        out = project_dir(project["id"], "videos") / f"dub_{shot['id']}_{stamp}.mp4"
        await media.mux(video, tracks, out, min(final_dur, total))
        # 分轨素材（导出剪映草稿 / FCPXML 时对白、环境音各占一轨）
        stems: dict = {}
        try:
            sp = project_dir(project["id"], "dub") / f"speech_{shot['id']}_{stamp}.wav"
            shutil.copy(speech, sp)
            stems["speech"] = rel(sp)
            if len(tracks) > 1:
                amb = project_dir(project["id"], "dub") / f"ambient_{shot['id']}_{stamp}.wav"
                await sound.place_track([{"path": p, "start": 0.0, "volume": v} for p, v in tracks[1:]],
                                        min(final_dur, total), amb)
                stems["ambient"] = rel(amb)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"分轨素材保存失败（不影响成片）：{exc}"[:160])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    asset = add_asset(project["id"], "video", out, "shot", shot["id"],
                      {"dub": True, "lipsync": lips, "dub_source": rel(src_video), "lines": len(voiced),
                       "speed": round(factor, 2), "notes": notes, "missing_voice": missing,
                       "timing": timing, "stems": stems})
    db.update("shots", shot["id"], {"video": asset["path"]})
    if missing:
        notes.append(f"角色「{'、'.join(missing)}」没有音色样本，其台词未配音")
    return {"asset": asset["id"], "lipsync": lips, "notes": notes}


# ---------------- ⑥ 成片 ----------------
@handler("assemble")
async def task_assemble(ctx: TaskContext) -> dict:
    project = need("projects", ctx.project_id, "项目")
    all_shots = shots_of(project["id"])
    shots = [s for s in all_shots if exists(s.get("video"))]
    if not shots:
        raise ValueError("还没有任何镜头视频")
    post = {**(project.get("post") or {}), **(ctx.params.get("post") or {})}
    clips = [absolute(s["video"]) for s in shots]
    durs = [(await media.probe(c))["duration"] or float(s["duration"] or 5) for s, c in zip(shots, clips)]
    out_dir = project_dir(project["id"], "final")
    bgm = absolute(ctx.params["bgm"]) if ctx.params.get("bgm") else None
    sizes = {**VIDEO_SIZES, **enhance.EXPORT_SIZES}
    quality = ctx.params.get("quality") if ctx.params.get("quality") in sizes else "final"
    aspect = ctx.params.get("aspect") or project["aspect_ratio"]
    if aspect not in reframe.ASPECTS:
        raise ValueError(f"不支持的画幅：{aspect}")
    alt = aspect != project["aspect_ratio"]          # P4-3 换画幅导出
    width, height = sizes[quality].get(aspect, sizes[quality]["16:9"])
    out_fps = int(post.get("fps") or 24) if int(post.get("fps") or 24) in (24, 25, 30, 48, 50, 60) else 24
    await ctx.progress(5, f"合成 {len(clips)} 个镜头（{width}x{height} · {out_fps}fps）…")
    safe_title = "".join(ch for ch in (project["title"] or "final") if ch not in '\\/:*?"<>|')[:40]
    output = out_dir / f"{safe_title}_{time.strftime('%Y%m%d_%H%M%S')}{'_' + aspect.replace(':', 'x') if alt else ''}.mp4"
    work = out_dir / f"work_{ctx.id}"
    work.mkdir(parents=True, exist_ok=True)
    g = {**(project.get("grade") or {}), **(ctx.params.get("grade") or {})}
    grade_info: dict = {}
    try:
        pre: list[str] = [""] * len(clips)
        if g.get("enabled") and (g.get("match", True) or g.get("lut") or g.get("deflicker")):
            await ctx.progress(6, "调色统一：分析各镜头色调…")
            gp = await grade.plan(clips, shots, work, strength=float(g.get("strength", 0.7)), lut=g.get("lut") or "",
                                  base_shot=g.get("base_shot") or "", per_scene=g.get("per_scene", True),
                                  match=g.get("match", True))
            pre = [grade.filters(c, bool(g.get("deflicker"))) for c in gp["cubes"]]
            grade_info = {"before": gp["before"], "after": gp["after"], "bases": gp["bases"], "lut": g.get("lut") or ""}
            await ctx.log(f"调色统一：相邻镜头平均色差 ΔE {gp['before']} → {gp['after']}")
        reframe_info: dict = {}
        if alt:
            opts = reframe.options(project)
            opts.update({k: v for k, v in (ctx.params.get("reframe") or {}).items() if k in ("mode", "shots")})
            await ctx.progress(8, f"重构图 {project['aspect_ratio']} → {aspect}：分析各镜头主体位置…")
            plans = await reframe.plan_shots(shots, clips, aspect, opts)
            pre = [",".join(f for f in (pre[i], reframe.filter_for(p, width, height)) if f) for i, p in enumerate(plans)]
            reframe_info = {"from": project["aspect_ratio"], "mode": opts["mode"], **reframe.summary(plans)}
            await ctx.log(f"重构图：裁切 {reframe_info['crop']} 个（其中跟随摇镜 {reframe_info['pan']}），"
                          f"模糊垫底 {reframe_info['pad']} 个")
        # 片头 / 片尾卡（P3-4）
        seq_shots, seq_clips, seq_durs, seq_pre = list(shots), list(clips), list(durs), list(pre)
        cover_src = package.pick_cover_shot(shots)
        bg = absolute(cover_src["keyframe"]) if cover_src and exists(cover_src.get("keyframe")) else None
        tc, ec = post.get("title_card") or {}, post.get("end_card") or {}
        n_intro = 0
        if tc.get("enabled"):
            d = float(tc.get("seconds") or 2.5)
            ep_sub = f"第 {project['episode']} 集" if project.get("series_id") and project.get("episode") else ""
            img = package.card_image(work / "title.png", width, height, tc.get("text") or project["title"] or "",
                                     tc.get("subtitle") or ep_sub, bg)
            seq_clips.insert(0, await package.card_clip(img, work / "title.mp4", d, width, height))
            seq_shots.insert(0, {"id": "_title", "transition": ""})
            seq_durs.insert(0, d)
            seq_pre.insert(0, "")
            seq_shots[1] = {**seq_shots[1], "transition": "叠化"}
            n_intro = 1
        if ec.get("enabled"):
            d = float(ec.get("seconds") or 2.5)
            img = package.card_image(work / "end.png", width, height, ec.get("text") or "未完待续", ec.get("subtitle") or "")
            seq_clips.append(await package.card_clip(img, work / "end.mp4", d, width, height))
            seq_shots.append({"id": "_end", "transition": "黑场"})
            seq_durs.append(d)
            seq_pre.append("")
        plan = package.transition_plan(seq_shots, seq_durs) if post.get("transitions", True) else [("", 0.0)] * len(seq_shots)
        starts = package.starts_with(seq_durs, plan)
        # 字幕：按转场后的真实起点；有配音时间就用配音时间
        so = sound.sound_opts(project)
        timeline, layer_shots = [], []
        for k, s in enumerate(shots):
            meta = (db.query("SELECT meta FROM assets WHERE path=? LIMIT 1", [s["video"]]) or [{}])[0].get("meta") or {}
            lines, exact = list(s.get("dialogue") or []), list(meta.get("timing") or [])
            if so["narration"] and exists(s.get("narration_audio")):      # 旁白配音：字幕按旁白实际时间
                nm = (db.query("SELECT meta FROM assets WHERE path=? LIMIT 1", [s["narration_audio"]]) or [{}])[0].get("meta") or {}
                off = sound.narration_offset(meta.get("timing"))
                if (s.get("narration") or "").strip():
                    lines.append({"speaker": "旁白", "line": s["narration"].strip()})
                exact += [{**t, "start": t["start"] + off} for t in nm.get("timing") or [] if t.get("j", -1) < len(lines)]
            timeline.append((starts[k + n_intro], durs[k], lines, exact or None))
            layer_shots.append({**s, "_dur": durs[k], "_dub_timing": meta.get("timing")})
        srt_rows = [(a, b, d["line"]) for st, du, lines, ex in timeline for a, b, d in package.line_timing(st, du, lines, ex)
                    if (d.get("line") or "").strip()]
        srt = "\n".join(f"{n + 1}\n{media._ts(a)} --> {media._ts(b)}\n{t}\n" for n, (a, b, t) in enumerate(srt_rows))
        (out_dir / "subtitles.srt").write_text(srt, encoding="utf-8")
        subs_on = ctx.params.get("subtitles", True)
        ass = package.build_ass(timeline, width, height, post.get("subtitle") or {}) if subs_on and srt_rows else ""
        # 声音后期（P3-5）：旁白 + 音效整轨、BGM（上传的优先，否则用生成的配乐）、人声闪避、响度标准化
        timeline_total = starts[-1] + seq_durs[-1]
        layers = await sound.build_layers(project, layer_shots, starts[n_intro:n_intro + len(shots)], timeline_total, work)
        bgm_volume = float(ctx.params.get("bgm_volume", so["bgm_volume"]))
        bgm_src = "上传" if bgm is not None else ""
        if bgm is None and so["bgm_auto"] and exists(project.get("bgm")):
            bgm, bgm_src = absolute(project["bgm"]), "生成"
        mix = {"narration": layers["narration"], "sfx": layers["sfx"], "bgm": bgm, "bgm_volume": bgm_volume,
               "duck": bool(so["duck"]), "loudnorm": bool(so["loudnorm"]), "lufs": so["lufs"], "duration": timeline_total}
        await ctx.progress(60, "合成画面、混音…")
        await media.assemble(seq_clips, output, width, height, work, srt="", ass=ass, pre=seq_pre, plan=plan, mix=mix,
                             fps=out_fps)
        sound_info = {**(mix.get("info") or {}), "narration": layers["narr_count"], "sfx": layers["sfx_count"],
                      "bgm": bgm_src}
        total = (await media.probe(output))["duration"]
        cover = None
        if post.get("cover", True) and cover_src and not alt:
            cover = _make_cover(project, cover_src, post)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    skipped = len(all_shots) - len(shots)
    asset = add_asset(project["id"], "final", output, "project", project["id"],
                      {"shots": len(clips), "seconds": round(total, 1), "skipped": skipped, "grade": grade_info,
                       "transitions": sum(1 for fx, _ in plan if fx), "cards": n_intro + (1 if ec.get("enabled") else 0),
                       "sound": sound_info, "size": f"{width}x{height}", "fps": out_fps, "aspect": aspect,
                       "reframe": reframe_info})
    if not alt:
        db.update("projects", project["id"], {"final_video": asset["path"]})
    return {"path": asset["path"], "seconds": round(total, 1), "skipped": skipped, "cover": cover, "aspect": aspect}


def _make_cover(project: dict, shot: dict, post: dict | None = None, title: str = "", subtitle: str = "",
                aspect: str = "") -> str:
    post = post or project.get("post") or {}
    cv = post.get("cover_opts") or {}
    aspect = aspect or cv.get("aspect") or ("3:4" if project["aspect_ratio"] in ("9:16", "3:4") else "16:9")
    w, h = {"3:4": (1080, 1440), "9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}.get(aspect, (1920, 1080))
    out = project_dir(project["id"], "final") / f"cover_{time.strftime('%Y%m%d_%H%M%S')}_{aspect.replace(':', 'x')}.jpg"
    package.cover_image(absolute(shot["keyframe"]), out, w, h, (title or cv.get("title") or project["title"] or "").replace("\\n", "\n"),
                        subtitle or cv.get("subtitle") or (project.get("synopsis") or "")[:24])
    a = add_asset(project["id"], "cover", out, "project", project["id"], {"shot": shot["id"], "aspect": aspect})
    db.update("projects", project["id"], {"cover": a["path"]})
    return a["path"]


# ---------------- 批量提交（批量按钮和一键成片共用） ----------------
def submit_batch(project_id: str, kind: str, only_missing: bool = True, count: int = 1, quality: str = "draft",
                 turbo: bool = True, mode: str = "auto", extra: dict | None = None) -> tuple[list[dict], str]:
    """kind: portraits / voices / keyframes / videos / dub。返回 (提交的任务, 批次号)。"""
    submitted = []
    bid = f"{kind}-{db.new_id()[:6]}"           # 批量任务标记：调度时让位给手动任务，可整体暂停

    def _submit(type_: str, title: str, project_id: str, owner_type: str = "", owner_id: str = "",
                params: dict | None = None) -> dict:
        return queue.submit(type_, project_id=project_id, title=title, owner_type=owner_type, owner_id=owner_id,
                            params={**(params or {}), "_batch": bid, **(extra or {})})

    if kind == "portraits":
        for c in characters_of(project_id):
            if not (only_missing and c["ref_image"]):
                submitted.append(_submit("portrait", f"角色形象：{c['name']}", project_id, "character", c["id"],
                                         {"character_id": c["id"], "count": count}))
            elif not c.get("face_image"):
                submitted.append(_submit("face_ref", f"脸部特写：{c['name']}", project_id, "character", c["id"],
                                         {"character_id": c["id"], "count": 1}))
        for loc in locations_of(project_id):
            if not (only_missing and loc["ref_image"]):
                submitted.append(_submit("location_image", f"场景图：{loc['name']}", project_id, "location",
                                         loc["id"], {"location_id": loc["id"], "count": count}))
        for pr in props_of(project_id):
            if not (only_missing and pr["ref_image"]):
                submitted.append(_submit("prop_image", f"道具图：{pr['name']}", project_id, "prop",
                                         pr["id"], {"prop_id": pr["id"], "count": count}))
    elif kind == "voices":
        for c in characters_of(project_id):
            if only_missing and c.get("voice_ref"):
                continue
            if c.get("ref_image") or c.get("face_image"):
                submitted.append(_submit("voice_sample", f"音色样本：{c['name']}", project_id, "character", c["id"],
                                         {"character_id": c["id"], "auto_select": True}))
    elif kind == "dub":
        if not voice.tts_available():
            raise ValueError("未安装配音组件，请在服务器运行 bash deploy/06_voice.sh")
        for s in shots_of(project_id):
            done = db.query("SELECT meta FROM assets WHERE path=? LIMIT 1", [s["video"]]) if s["video"] else []
            if not s["video"] or not dub_lines(s)[0]:
                continue
            if only_missing and done and (done[0].get("meta") or {}).get("dub"):
                continue
            submitted.append(_submit("dub", f"配音 #{s['sort'] + 1}", project_id, "shot", s["id"],
                                     {"shot_id": s["id"], "lipsync": "auto"}))
    elif kind in {"keyframes", "videos"}:
        from . import cloud
        all_shots = shots_of(project_id)
        project = need("projects", project_id, "项目")
        for idx, s in enumerate(all_shots):
            field = "keyframe" if kind == "keyframes" else "video"
            if only_missing and s[field]:
                continue
            if kind == "keyframes":
                submitted.append(_submit("keyframe", f"分镜图 #{s['sort'] + 1}", project_id, "shot", s["id"],
                                         {"shot_id": s["id"], "count": count}))
            else:
                submitted.append(cloud.submit_video(s, project, {"quality": quality, "turbo": turbo, "mode": mode,
                                                                 "_batch": bid, **(extra or {})},
                                                    index=idx, total=len(all_shots)))      # P4-4 按镜头分配本地/云端
    else:
        raise ValueError("未知批量类型")
    return submitted, bid


# ---------------- 动态分镜预览：分镜图 + 推拉摇移 + 台词字幕（可选配音试听），出视频前先看节奏 ----------------
CHARS_PER_SEC = 4.5          # 估算中文台词语速（没有配音试听时用来提示“台词塞不进镜头”）
SPEECH_LEAD, SPEECH_GAP, SPEECH_TAIL = 0.3, 0.25, 0.3


def estimate_speech(text: str) -> float:
    n = len(re.sub(r"[\s，。！？、；：,.!?;:…—“”\"'（）()]", "", text or ""))
    pauses = len(re.findall(r"[，。！？、；,.!?;…]", text or ""))
    return round(n / CHARS_PER_SEC + pauses * 0.15, 2)


def _shot_label(i: int, shot: dict, dur: float) -> str:
    return f"#{i + 1}  {shot.get('shot_size') or ''} · {shot.get('camera_move') or '固定'} · {dur:.1f}s"


async def _animatic_tts(ctx: TaskContext, project: dict, shots: list[dict]) -> dict[tuple[str, int], Path]:
    """为有音色样本的台词合成试听语音（按 文字+音色+情绪 缓存，改了哪句才重配哪句）。"""
    import hashlib
    cache = project_dir(project["id"], "animatic") / "tts"
    cache.mkdir(parents=True, exist_ok=True)
    out: dict[tuple[str, int], Path] = {}
    todo = []
    for shot in shots:
        voiced = {id(d): c for d, c in dub_lines(shot)[0]}
        for j, d in enumerate(shot.get("dialogue") or []):
            c = voiced.get(id(d))
            if not c:
                continue
            key = hashlib.sha1(f"{c['voice_ref']}|{d.get('emotion') or ''}|{d['line']}".encode()).hexdigest()[:16]
            wav = cache / f"{key}.wav"
            out[(shot["id"], j)] = wav
            if not wav.is_file():
                todo.append({"text": d["line"], "voice": str(absolute(c["voice_ref"])), "emo": d.get("emotion") or "",
                             "out": str(wav)})
    if todo:
        await ctx.progress(5, f"配音试听：合成 {len(todo)} 句台词…")
        async with gpu.use("voice", ctx.task.get("title") or "预览配音"):
            await voice.synthesize(todo, ctx.log)
    return out


@handler("animatic")
async def task_animatic(ctx: TaskContext) -> dict:
    project = need("projects", ctx.project_id, "项目")
    shots = shots_of(project["id"])
    if not shots:
        raise ValueError("还没有分镜")
    t_start = time.monotonic()
    width, height = VIDEO_SIZES["draft"].get(project["aspect_ratio"], VIDEO_SIZES["draft"]["16:9"])
    use_tts = bool(ctx.params.get("tts")) and voice.tts_available()
    prefer_video = bool(ctx.params.get("prefer_video"))
    wavs = await _animatic_tts(ctx, project, shots) if use_tts else {}

    out_dir = project_dir(project["id"], "animatic")
    work = out_dir / f"work_{ctx.id}"
    work.mkdir(parents=True, exist_ok=True)
    segments, speech, subs, labels, timing = [], [], [], [], []
    t = 0.0
    try:
        for i, shot in enumerate(shots):
            planned = float(shot.get("duration") or 5)
            lines = [d for d in (shot.get("dialogue") or []) if str(d.get("line") or "").strip()]
            durs = []
            for j, d in enumerate(shot.get("dialogue") or []):
                if not str(d.get("line") or "").strip():
                    continue
                wav = wavs.get((shot["id"], j))
                durs.append(((await media.probe(wav))["duration"], wav) if wav else (estimate_speech(d["line"]), None))
            need_len = (SPEECH_LEAD + sum(x for x, _ in durs) + SPEECH_GAP * (len(durs) - 1) + SPEECH_TAIL) if durs else 0
            dur = max(planned, need_len) if use_tts else planned     # 有试听声音时画面跟着声音延长，免得台词被截断
            # 字幕 / 声音按句排开；没有试听声音时按估算时长排，超出镜头的部分压缩到镜头内
            scale = min(1.0, dur / need_len) if need_len else 1.0
            cur = t + SPEECH_LEAD * scale
            for d, (sec, wav) in zip(lines, durs):
                seg = sec * scale
                speaker = d.get("speaker") or ""
                subs.append((cur, cur + seg, f"{speaker}：{d['line']}" if speaker and speaker != "旁白" else d["line"]))
                if wav:
                    speech.append((wav, cur))
                cur += seg + SPEECH_GAP * scale
            labels.append((t, t + dur, _shot_label(i, shot, dur) + ("（缺分镜图）" if not exists(shot.get("keyframe")) and
                                                                     not (prefer_video and exists(shot.get("video"))) else "")))
            target = work / f"s{i:04d}.mp4"
            if prefer_video and exists(shot.get("video")):
                await media.video_clip(absolute(shot["video"]), target, dur, width, height)
                source = "video"
            elif exists(shot.get("keyframe")):
                await media.still_clip(absolute(shot["keyframe"]), target, dur, width, height,
                                       shot.get("camera_move") or "固定")
                source = "keyframe"
            else:
                await media.card_clip(target, dur, width, height)
                source = "card"
            segments.append(target)
            timing.append({"shot_id": shot["id"], "index": i + 1, "planned": planned, "duration": round(dur, 2),
                           "speech": round(need_len, 2), "over": need_len > planned + 0.05, "source": source,
                           "estimated": not any(w for _, w in durs)})
            t += dur
            await ctx.progress(10 + int(80 * (i + 1) / len(shots)), f"预览片段 {i + 1}/{len(shots)}")
        await ctx.progress(92, "拼接预览…")
        output = out_dir / f"animatic_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        await media.animatic(segments, output, work, t, speech or None, media.ass_from(subs, labels, width, height))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    over = [x["index"] for x in timing if x["over"]]
    meta = {"seconds": round(t, 1), "shots": len(shots), "tts": use_tts, "timing": timing, "over": over,
            "missing_keyframes": [x["index"] for x in timing if x["source"] == "card"],
            "build_seconds": round(time.monotonic() - t_start, 1)}
    asset = add_asset(project["id"], "animatic", output, "project", project["id"], meta)
    return {"asset": asset["id"], "path": asset["path"], **meta}


# ---------------- 人脸修正：用角色脸部特写对镜头做 FaceFusion 换脸 ----------------
def face_fix_plan(shot: dict, items: list[dict] | None = None) -> list[dict]:
    """items: [{"character_id", "position": "auto" | 0.. | "skip"}]；缺省时按出镜角色自动编排：
    单人镜头 → 画面中最大的脸；多人镜头 → 按出镜顺序对应从左到右第 N 张脸。"""
    _, all_chars, chars, _ = _shot_context(shot)
    if items is None:
        items = [{"character_id": c["id"], "position": "auto" if len(chars) == 1 else i} for i, c in enumerate(chars)]
    plan = []
    for it in items:
        c = all_chars.get(it.get("character_id") or "")
        pos = it.get("position", "auto")
        if not c or pos in ("skip", None, ""):
            continue
        source = c.get("face_image") if exists(c.get("face_image")) else c.get("ref_image")
        if not exists(source):
            raise ValueError(f"角色「{c['name']}」还没有脸部特写或定妆照")
        plan.append({"character": c, "source": absolute(source), "position": pos})
    if not plan:
        raise ValueError("没有需要修正的角色")
    return plan


@handler("face_fix")
async def task_face_fix(ctx: TaskContext) -> dict:
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    field = "keyframe" if ctx.params.get("target") == "keyframe" else "video"
    if not exists(shot.get(field)):
        raise ValueError("这个镜头还没有" + ("分镜图" if field == "keyframe" else "视频"))
    plan = face_fix_plan(shot, ctx.params.get("items"))
    current = absolute(shot[field])
    out_dir = project_dir(shot["project_id"], "facefusion")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    enhance = bool(ctx.params.get("enhance", True))
    model = ctx.params.get("swapper_model") or "hyperswap_1a_256"
    async with gpu.use("facefusion", ctx.task.get("title") or "人脸修正"):
        for i, step in enumerate(plan):
            last = i == len(plan) - 1
            processors = "face_swapper face_enhancer" if (enhance and last) else "face_swapper"
            params: dict[str, Any] = {"face_swapper_model": model, "face_swapper_pixel_boost": "512x512",
                                      "face_mask_types": ["box", "occlusion"]}
            if step["position"] == "auto":
                params.update(face_selector_mode="one", face_selector_order="large-small")
            else:
                params.update(face_selector_mode="reference", face_selector_order="left-right",
                              reference_face_position=int(step["position"]))
            if processors.endswith("face_enhancer"):
                params.update(face_enhancer_model="gfpgan_1.4", face_enhancer_blend=60)
            output = out_dir / f"facefix_{shot['id']}_{stamp}_{i}{current.suffix}"
            await ctx.progress(5 + 90 * i // len(plan), f"修正「{step['character']['name']}」的脸 ({i + 1}/{len(plan)})")
            await facefusion.run(processors, current, output, step["source"], params, log=ctx.log)
            current = output
    asset = add_asset(shot["project_id"], field, current, "shot", shot["id"],
                      {"face_fix": [s["character"]["name"] for s in plan], "enhance": enhance})
    db.update("shots", shot["id"], {field: asset["path"]})
    return {"asset": asset["id"], "path": asset["path"]}


# ---------------- 工具箱：FaceFusion ----------------
@handler("facefusion")
async def task_facefusion(ctx: TaskContext) -> dict:
    p = ctx.params
    target = absolute(p["target"])
    source = absolute(p["source"]) if p.get("source") else None
    out_dir = project_dir(ctx.project_id, "facefusion")
    output = out_dir / f"{p['processor']}_{time.strftime('%Y%m%d_%H%M%S')}{target.suffix}"
    await ctx.progress(5, "FaceFusion 处理中…")
    async with gpu.use("facefusion", ctx.task.get("title") or "FaceFusion"):
        await facefusion.run(p["processor"], target, output, source, dict(p.get("params") or {}), log=ctx.log)
    asset = add_asset(ctx.project_id, "facefusion", output, p.get("owner_type", ""), p.get("owner_id", ""),
                      {"processor": p["processor"]})
    if p.get("owner_type") == "shot" and p.get("apply") and p.get("owner_id"):
        field = "keyframe" if facefusion.is_image(output) else "video"
        db.update("shots", p["owner_id"], {field: asset["path"]})
    return {"path": asset["path"], "asset": asset["id"]}
