"""所有生成任务的实现。每个函数是一个任务处理器，由 tasks.TaskQueue 逐个执行。

流程：① 剧本 → ② 角色/场景设定 + 参考图 → ③ 分镜表 → ④ 分镜图（多图参考）→ ⑤ 镜头视频（H3）→ ⑥ 合成成片
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any

from . import comfy, db, facefusion, llm, media, prompts, qa, voice
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
    data = await llm.chat_json(prompts.SCRIPT_SYSTEM, prompts.script_user(project),
                               validate=prompts.validate_script, temperature=0.85, max_tokens=12000)
    values: dict[str, Any] = {"script": data["script"], "synopsis": data["synopsis"]}
    if data["title"] and (project.get("title") or "").strip() in {"", "未命名项目"}:
        values["title"] = data["title"]
    db.update("projects", project["id"], values)
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
        })
    return {"shots": len(rows), "seconds": round(sum(r["duration"] for r in rows), 1)}


# ---------------- 生图公共 ----------------
async def _generate_images(ctx: TaskContext, prompt: str, aspect: str, ref_paths: list[Path], count: int,
                           out_dir: Path, random_seed: bool = False) -> list[Path]:
    count = max(1, min(int(count or 1), 8))
    seed = None if random_seed else ctx.params.get("seed")
    results: list[Path] = []
    span = 90 // count
    async with gpu.use("comfyui", ctx.task.get("title") or "生图"):
        await gpu.comfy_switch("image")
        refs = [await comfy.upload_image(p) for p in ref_paths]
        for i in range(count):
            s = seed + i if isinstance(seed, int) and seed >= 0 else None
            files = await comfy.run(image_workflow(prompt, aspect, refs, seed=s), out_dir,
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
        labels.append(f"{label}:{c['name']}")
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
    project, _, chars, loc = _shot_context(shot)
    props = shot_props(shot)
    has_loc = bool(loc and exists(loc.get("ref_image")))
    base, _, _ = char_refs(chars, MAX_IMAGE_REFS, faces=False)
    reserve = (1 if has_loc else 0) + sum(1 for pr in props if exists(pr.get("ref_image")))
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
    return {"project": project, "chars": chars, "loc": loc, "props": props, "refs": refs, "ref_labels": labels,
            "prompt": prompts.keyframe_prompt(project, shot, chars, loc, ref_map, loc_idx, prop_refs)}


@handler("keyframe")
async def task_keyframe(ctx: TaskContext) -> dict:
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    plan = keyframe_plan(shot)
    prompt = (ctx.params.get("prompt") or "").strip() or plan["prompt"]
    out_dir = project_dir(shot["project_id"], "keyframes")
    aspect = plan["project"]["aspect_ratio"]
    files = await _generate_images(ctx, prompt, aspect, plan["refs"], ctx.params.get("count", 1), out_dir)

    async def check(f: Path) -> dict | None:
        return await qa.check(f, faces=_qa_faces(plan["chars"]), check_text=not _expects_text(shot),
                              style=plan["project"].get("style") or "")

    results = [(f, await check(f)) for f in files]
    retries = get_settings().qa_keyframe_retry if qa.available() else 0
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
    attempts = 1 + (max(0, get_settings().qa_video_retry) if qa.available() else 0)
    results: list[tuple[dict, dict | None]] = []
    for attempt in range(attempts):
        tag = f"（第 {attempt + 1} 次）" if attempt else ""
        async with gpu.use("comfyui", (ctx.task.get("title") or "生成视频") + tag):
            await gpu.comfy_switch("video")
            uploaded = [await comfy.upload_image(p) for p in plan["refs"]]
            s_ = seed if attempt == 0 else None
            if plan["mode"] == "r2v":
                wf = video_r2v_workflow(prompt, uploaded, project["aspect_ratio"], seconds, quality, turbo, s_)
            else:
                wf = video_i2v_workflow(prompt, uploaded[0] if uploaded else None, project["aspect_ratio"], seconds,
                                        quality, turbo, seed=s_)
            span = 94 // attempts
            files = await comfy.run(wf, out_dir, progress_slice(ctx, 3 + attempt * span, 3 + (attempt + 1) * span,
                                                                 "生成视频" + tag))
        videos = [f for f in files if f.suffix.lower() in VIDEO_SUFFIX]
        if not videos:
            raise RuntimeError("ComfyUI 没有产出视频文件")
        await ctx.log("自动质检中…")
        result = await qa.check(videos[0], reference=reference, faces=_qa_faces(chars), check_text=True,
                                style=project.get("style") or "")
        asset = add_asset(project["id"], "video", videos[0], "shot", shot["id"],
                          {"prompt": prompt, "mode": plan["mode"], "quality": quality, "seconds": seconds,
                           "turbo": turbo, "qa": result, "attempt": attempt + 1})
        results.append((asset, result))
        if not result or result.get("ok"):
            break
        if attempt + 1 < attempts:
            await ctx.log(f"质检未通过（{qa.summary(result)}），自动换种子重抽")
    best, best_qa = min(results, key=lambda x: qa.rank(x[1]))
    db.update("shots", shot["id"], {"video": best["path"]})
    out = {"asset": best["id"], "attempts": len(results), "qa": qa.summary(best_qa)}
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
        out = project_dir(project["id"], "videos") / f"dub_{shot['id']}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        await media.mux(video, tracks, out, min(final_dur, total))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    asset = add_asset(project["id"], "video", out, "shot", shot["id"],
                      {"dub": True, "lipsync": lips, "dub_source": rel(src_video), "lines": len(voiced),
                       "speed": round(factor, 2), "notes": notes, "missing_voice": missing})
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
    clips = [absolute(s["video"]) for s in shots]
    timeline, t = [], 0.0
    for s, clip in zip(shots, clips):
        dur = (await media.probe(clip))["duration"] or float(s["duration"] or 5)
        timeline.append((t, dur, s.get("dialogue") or []))
        t += dur
    srt = media.build_srt(timeline)
    out_dir = project_dir(project["id"], "final")
    (out_dir / "subtitles.srt").write_text(srt, encoding="utf-8")
    bgm = absolute(ctx.params["bgm"]) if ctx.params.get("bgm") else None
    quality = ctx.params.get("quality") if ctx.params.get("quality") in VIDEO_SIZES else "final"
    width, height = VIDEO_SIZES[quality].get(project["aspect_ratio"], VIDEO_SIZES[quality]["16:9"])
    await ctx.progress(10, f"合成 {len(clips)} 个镜头…")
    safe_title = "".join(ch for ch in (project["title"] or "final") if ch not in '\\/:*?"<>|')[:40]
    output = out_dir / f"{safe_title}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    work = out_dir / f"work_{ctx.id}"
    try:
        await media.assemble(clips, output, width, height, work,
                             srt=srt if ctx.params.get("subtitles", True) else "",
                             bgm=bgm, bgm_volume=float(ctx.params.get("bgm_volume", 0.25)))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    skipped = len(all_shots) - len(shots)
    asset = add_asset(project["id"], "final", output, "project", project["id"],
                      {"shots": len(clips), "seconds": round(t, 1), "skipped": skipped})
    db.update("projects", project["id"], {"final_video": asset["path"]})
    return {"path": asset["path"], "seconds": round(t, 1), "skipped": skipped}


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
