"""P4-1 导出工程：剪映草稿（视频 / 对白 / 环境音 / 旁白 / 音效 / BGM / 字幕 分轨）、FCPXML 1.9、EDL（CMX3600）、SRT。
打包成 zip：素材在 media/，路径先写占位符，用户解压后双击「修复素材路径.bat」（或 python fix_paths.py）改成本机绝对路径。"""
from __future__ import annotations

import json
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, media
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")
ROOT_PH = "__MEDIA_ROOT__"        # 素材目录（绝对路径，正斜杠）
URL_PH = "__MEDIA_URL__"          # 同上，file:/// 形式（FCPXML 用）
US = 1_000_000


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def _us(sec: float) -> int:
    return int(round(sec * US))


# ======================= 时间线（与成片一致的硬切版本，转场留给剪辑软件） =======================
async def build_timeline(project: dict, fps: int = 30) -> dict:
    from . import package, sound
    from .pipeline import absolute, exists, shots_of
    from .workflows import VIDEO_SIZES
    shots = [s for s in shots_of(project["id"]) if exists(s.get("video"))]
    if not shots:
        raise ValueError("还没有任何镜头视频")
    w, h = VIDEO_SIZES["final"].get(project["aspect_ratio"], (1344, 768))
    so = sound.sound_opts(project)
    lib = sound.list_sfx()
    tl: dict = {"width": w, "height": h, "fps": fps, "video": [], "dialogue": [], "ambient": [], "narration": [],
                "sfx": [], "bgm": [], "subs": []}
    t = 0.0
    for i, s in enumerate(shots):
        path = absolute(s["video"])
        info = await media.probe(path)
        dur = info["duration"] or float(s.get("duration") or 5)
        meta = (db.query("SELECT meta FROM assets WHERE path=? LIMIT 1", [s["video"]]) or [{}])[0].get("meta") or {}
        stems = meta.get("stems") or {}
        has_stems = exists(stems.get("speech"))
        tl["video"].append({"path": path, "start": t, "dur": dur, "name": f"#{i + 1} {s.get('scene') or ''}".strip(),
                            "mute": has_stems, "has_audio": info["has_audio"], "shot": s["id"]})
        if has_stems:
            tl["dialogue"].append({"path": absolute(stems["speech"]), "start": t, "dur": dur, "name": f"对白 #{i + 1}"})
            if exists(stems.get("ambient")):
                tl["ambient"].append({"path": absolute(stems["ambient"]), "start": t, "dur": dur, "name": f"环境音 #{i + 1}"})
        lines, exact = list(s.get("dialogue") or []), list(meta.get("timing") or [])
        if so["narration"] and exists(s.get("narration_audio")):
            off = sound.narration_offset(meta.get("timing"))
            np_ = absolute(s["narration_audio"])
            tl["narration"].append({"path": np_, "start": t + off, "dur": (await media.probe(np_))["duration"],
                                    "name": f"旁白 #{i + 1}"})
            nm = (db.query("SELECT meta FROM assets WHERE path=? LIMIT 1", [s["narration_audio"]]) or [{}])[0].get("meta") or {}
            if (s.get("narration") or "").strip():
                lines.append({"speaker": "旁白", "line": s["narration"].strip()})
            exact += [{**x, "start": x["start"] + off} for x in nm.get("timing") or [] if x.get("j", -1) < len(lines)]
        for a, b, d in package.line_timing(t, dur, lines, exact or None):
            if (d.get("line") or "").strip():
                tl["subs"].append({"start": a, "end": b, "text": d["line"].strip(), "speaker": d.get("speaker") or ""})
        for fx in sound.shot_sfx(s, so["sfx_auto"], lib):
            p = sound.sfx_dir() / fx["name"]
            off = float(fx.get("offset") or 0)
            fdur = (await media.probe(p))["duration"]
            if fx.get("loop"):
                k = off
                while k < dur - 0.05:          # 循环音效按素材长度平铺到镜头结束
                    tl["sfx"].append({"path": p, "start": t + k, "dur": min(fdur, dur - k), "name": fx["name"]})
                    k += fdur
            else:
                tl["sfx"].append({"path": p, "start": t + off, "dur": min(fdur, dur - off), "name": fx["name"]})
        t += dur
    tl["total"] = t
    if so["bgm_auto"] and exists(project.get("bgm")):
        bp = absolute(project["bgm"])
        bd = (await media.probe(bp))["duration"] or t
        k = 0.0
        while k < t - 0.05:
            tl["bgm"].append({"path": bp, "start": k, "dur": min(bd, t - k), "name": "配乐", "volume": so["bgm_volume"]})
            k += bd
    return tl


def media_names(tl: dict) -> dict[str, str]:
    """源文件 → 包内文件名（按轨道编号，避免重名）。"""
    names: dict[str, str] = {}
    prefix = {"video": "v", "dialogue": "d", "ambient": "a", "narration": "n", "sfx": "fx", "bgm": "m"}
    for kind, pre in prefix.items():
        for i, c in enumerate(tl[kind], start=1):
            src = str(c["path"])
            if src not in names:
                names[src] = f"{pre}{i:02d}_{Path(src).name}" if kind != "sfx" else f"sfx_{Path(src).name}"
    return names


# ======================= 剪映草稿（5.x 明文格式，剪映专业版可直接打开） =======================
def _speed() -> dict:
    return {"curve_speed": None, "id": _uid(), "mode": 0, "speed": 1.0, "type": "speed"}


def _segment(mat_id: str, start: float, dur: float, speed_id: str, volume: float = 1.0, clip: dict | None = None,
             render: int = 0) -> dict:
    seg = {"cartoon": False, "clip": clip, "common_keyframes": [], "enable_adjust": True, "enable_color_curves": True,
           "enable_color_match_adjust": False, "enable_color_wheels": True, "enable_lut": True,
           "enable_smart_color_adjust": False, "extra_material_refs": [speed_id], "group_id": "",
           "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000}, "id": _uid(), "intensifies_audio": False,
           "is_placeholder": False, "is_tone_modify": False, "keyframe_refs": [], "last_nonzero_volume": 1.0,
           "material_id": mat_id, "render_index": render, "reverse": False,
           "source_timerange": {"duration": _us(dur), "start": 0}, "speed": 1.0,
           "target_timerange": {"duration": _us(dur), "start": _us(start)}, "template_id": "",
           "template_scene": "default-template", "track_attribute": 0, "track_render_index": 0,
           "uniform_scale": {"on": True, "value": 1.0}, "visible": True, "volume": volume}
    return seg


def _clip(y: float = 0.0) -> dict:
    return {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False}, "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": y}}


def jianying_draft(tl: dict, name: str, names: dict[str, str]) -> tuple[dict, dict]:
    mats: dict[str, list] = {k: [] for k in ("audios", "videos", "texts", "speeds", "canvases", "sound_channel_mappings",
                                              "material_animations", "beats", "effects", "transitions", "stickers",
                                              "audio_effects", "audio_fades", "placeholders", "tail_leaders", "vocal_separations")}
    tracks = []
    video_mat: dict[str, str] = {}
    segs = []
    for c in tl["video"]:
        src = str(c["path"])
        if src not in video_mat:
            mid = _uid()
            video_mat[src] = mid
            mats["videos"].append({"audio_fade": None, "category_id": "", "category_name": "local", "check_flag": 63487,
                                   "crop": {"lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0, "lower_right_y": 1.0,
                                            "upper_left_x": 0.0, "upper_left_y": 0.0, "upper_right_x": 1.0, "upper_right_y": 0.0},
                                   "crop_ratio": "free", "crop_scale": 1.0, "duration": _us(c["dur"]), "has_audio": c["has_audio"],
                                   "height": tl["height"], "id": mid, "local_material_id": "", "material_id": mid,
                                   "material_name": names[src], "media_path": "", "path": f"{ROOT_PH}/{names[src]}",
                                   "type": "video", "width": tl["width"]})
        sp = _speed()
        mats["speeds"].append(sp)
        segs.append(_segment(video_mat[src], c["start"], c["dur"], sp["id"], 0.0 if c["mute"] else 1.0, _clip()))
    tracks.append({"attribute": 0, "flag": 0, "id": _uid(), "is_default_name": False, "name": "画面", "segments": segs,
                   "type": "video"})
    audio_mat: dict[str, str] = {}
    for kind, label in (("dialogue", "对白"), ("narration", "旁白"), ("ambient", "环境音"), ("sfx", "音效"), ("bgm", "配乐")):
        if not tl[kind]:
            continue
        segs = []
        for c in tl[kind]:
            src = str(c["path"])
            if src not in audio_mat:
                mid = _uid()
                audio_mat[src] = mid
                mats["audios"].append({"app_id": 0, "category_id": "", "category_name": "local", "check_flag": 1,
                                       "duration": _us(c["dur"]), "effect_id": "", "formula_id": "", "id": mid,
                                       "local_material_id": mid, "music_id": mid, "name": names[src],
                                       "path": f"{ROOT_PH}/{names[src]}", "source_platform": 0, "team_id": "",
                                       "text_id": "", "tone_category_id": "", "tone_category_name": "", "tone_effect_id": "",
                                       "tone_effect_name": "", "tone_speaker": "", "tone_type": "",
                                       "type": "extract_music", "video_id": "", "wave_points": []})
            sp = _speed()
            mats["speeds"].append(sp)
            segs.append(_segment(audio_mat[src], c["start"], c["dur"], sp["id"], float(c.get("volume", 1.0))))
        tracks.append({"attribute": 0, "flag": 0, "id": _uid(), "is_default_name": False, "name": label,
                       "segments": segs, "type": "audio"})
    if tl["subs"]:
        segs = []
        for sub in tl["subs"]:
            mid = _uid()
            text = sub["text"]
            content = {"styles": [{"fill": {"alpha": 1.0, "content": {"render_type": "solid", "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}}},
                                   "range": [0, len(text)], "size": 7.0, "bold": False, "italic": False, "underline": False,
                                   "strokes": [{"content": {"solid": {"alpha": 1.0, "color": [0.0, 0.0, 0.0]}}, "width": 0.08}]}],
                       "text": text}
            mats["texts"].append({"add_type": 0, "alignment": 1, "background_alpha": 1.0, "background_color": "",
                                  "check_flag": 7, "content": json.dumps(content, ensure_ascii=False), "font_size": 7.0,
                                  "global_alpha": 1.0, "id": mid, "letter_spacing": 0.0, "line_feed": 1, "line_max_width": 0.82,
                                  "line_spacing": 0.02, "name": "", "text_alpha": 1.0, "text_color": "#FFFFFF",
                                  "text_size": 30, "type": "subtitle", "typesetting": 0, "words": {"end_time": [], "start_time": [], "text": []}})
            segs.append({**_segment(mid, sub["start"], max(0.2, sub["end"] - sub["start"]), "", 1.0, _clip(-0.75), 14000),
                         "extra_material_refs": []})
        tracks.append({"attribute": 0, "flag": 0, "id": _uid(), "is_default_name": False, "name": "字幕", "segments": segs,
                       "type": "text"})
    now = int(time.time() * US)
    did = _uid()
    content = {"canvas_config": {"height": tl["height"], "ratio": "original", "width": tl["width"]}, "color_space": 0,
               "config": {"adjust_max_index": 1, "attachment_info": [], "combination_max_index": 1, "export_range": None,
                          "extract_audio_last_index": 1, "lyrics_recognition_id": "", "lyrics_sync": True,
                          "lyrics_taskinfo": [], "maintrack_adsorb": True, "material_save_mode": 0,
                          "original_sound_last_index": 1, "record_audio_last_index": 1, "sticker_max_index": 1,
                          "subtitle_recognition_id": "", "subtitle_sync": True, "subtitle_taskinfo": [],
                          "system_font_list": [], "video_mute": False, "zoom_info_params": None},
               "cover": None, "create_time": 0, "duration": _us(tl["total"]), "extra_info": None, "fps": float(tl["fps"]),
               "free_render_index_mode_on": False, "group_container": None, "id": did,
               "keyframe_graph_list": [], "keyframes": {k: [] for k in ("adjusts", "audios", "effects", "filters", "handwrites",
                                                                       "stickers", "texts", "videos")},
               "last_modified_platform": {"app_id": 3704, "app_source": "lv", "app_version": "5.9.0", "os": "windows"},
               "materials": mats, "mutable_config": None, "name": name, "new_version": "110.0.0",
               "platform": {"app_id": 3704, "app_source": "lv", "app_version": "5.9.0", "os": "windows"},
               "relationships": [], "render_index_track_mode_on": False, "retouch_cover": None, "source": "default",
               "static_cover_image_path": "", "time_marks": None, "tracks": tracks, "update_time": 0, "version": 360000}
    meta = {"cloud_package_completed_time": "", "draft_cloud_capcut_purchase_info": "", "draft_cloud_last_action_download": False,
            "draft_cloud_materials": [], "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
            "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "", "draft_cover": "",
            "draft_deeplink_url": "", "draft_enterprise_info": {"draft_enterprise_extra": "", "draft_enterprise_id": "",
                                                                 "draft_enterprise_name": "", "enterprise_material": []},
            "draft_fold_path": "", "draft_id": did, "draft_is_ai_packaging_used": False, "draft_is_ai_shorts": False,
            "draft_is_ai_translate": False, "draft_is_article_video_draft": False, "draft_is_from_deeplink": "false",
            "draft_is_invisible": False, "draft_materials": [{"type": k, "value": []} for k in (0, 1, 2, 3, 6, 7, 8)],
            "draft_materials_copied_info": [], "draft_name": name, "draft_new_version": "", "draft_removable_storage_device": "",
            "draft_root_path": "", "draft_segment_extra_info": [], "draft_timeline_materials_size_": 0,
            "draft_type": "", "tm_draft_cloud_completed": "", "tm_draft_cloud_modified": 0, "tm_draft_create": now,
            "tm_draft_modified": now, "tm_draft_removed": 0, "tm_duration": _us(tl["total"])}
    return content, meta


# ======================= FCPXML 1.9 / EDL / SRT =======================
def _rt(sec: float, fps: int) -> str:
    return f"{int(round(sec * fps))}/{fps}s"


def fcpxml(tl: dict, name: str, names: dict[str, str]) -> str:
    fps = tl["fps"]
    res, ids = [f'<format id="r0" name="FFVideoFormat{tl["height"]}p{fps}" frameDuration="1/{fps}s" '
                f'width="{tl["width"]}" height="{tl["height"]}"/>'], {}
    for kind in ("video", "dialogue", "ambient", "narration", "sfx", "bgm"):
        for c in tl[kind]:
            src = str(c["path"])
            if src in ids:
                continue
            rid = f"r{len(ids) + 1}"
            ids[src] = rid
            video = kind == "video"
            fmt = 'format="r0" ' if video else ""
            res.append(f'<asset id="{rid}" name={quoteattr(names[src])} start="0s" duration="{_rt(c["dur"], fps)}" '
                       f'hasVideo="{1 if video else 0}" hasAudio="1" {fmt}audioSources="1" audioChannels="2">'
                       f'<media-rep kind="original-media" src={quoteattr(f"{URL_PH}/{names[src]}")}/></asset>')
    lanes = {"dialogue": -1, "narration": -2, "ambient": -3, "sfx": -4, "bgm": -5}
    spine = []
    for c in tl["video"]:
        inner = []
        for kind, lane in lanes.items():
            for a in tl[kind]:
                if c["start"] - 1e-6 <= a["start"] < c["start"] + c["dur"] - 1e-6:
                    off = a["start"] - c["start"]          # 连接片段的 offset 以父片段内部时间计
                    inner.append(f'<asset-clip ref="{ids[str(a["path"])]}" lane="{lane}" name={quoteattr(a["name"])} '
                                 f'offset="{_rt(off, fps)}" start="0s" duration="{_rt(a["dur"], fps)}" audioRole="dialogue"/>')
        mute = '<adjust-volume amount="-96dB"/>' if c["mute"] else ""
        spine.append(f'<asset-clip ref="{ids[str(c["path"])]}" name={quoteattr(c["name"])} offset="{_rt(c["start"], fps)}" '
                     f'start="0s" duration="{_rt(c["dur"], fps)}" format="r0" tcFormat="NDF">{mute}{"".join(inner)}</asset-clip>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.9">\n'
            f'<resources>{"".join(res)}</resources>\n<library><event name={quoteattr(name)}><project name={quoteattr(name)}>'
            f'<sequence format="r0" duration="{_rt(tl["total"], fps)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">'
            f'<spine>{"".join(spine)}</spine></sequence></project></event></library>\n</fcpxml>\n')


def _tc(sec: float, fps: int) -> str:
    f = int(round(sec * fps))
    return f"{f // (3600 * fps):02d}:{f // (60 * fps) % 60:02d}:{f // fps % 60:02d}:{f % fps:02d}"


def edl(tl: dict, name: str, names: dict[str, str]) -> str:
    fps = tl["fps"]
    out = [f"TITLE: {name}", "FCM: NON-DROP FRAME", ""]
    for n, c in enumerate(tl["video"], start=1):
        reel = f"V{n:03d}"
        out.append(f"{n:03d}  {reel:<8} AA/V  C        {_tc(0, fps)} {_tc(c['dur'], fps)} "
                   f"{_tc(3600 + c['start'], fps)} {_tc(3600 + c['start'] + c['dur'], fps)}")
        out.append(f"* FROM CLIP NAME: {names[str(c['path'])]}")
        out.append("")
    return "\n".join(out)


def srt(tl: dict) -> str:
    return "\n".join(f"{n}\n{media._ts(s['start'])} --> {media._ts(s['end'])}\n{s['text']}\n"
                     for n, s in enumerate(tl["subs"], start=1))


FIX_BAT = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$r=(Resolve-Path 'media').Path -replace '\\','/'; Get-ChildItem -Recurse -Include draft_content.json,draft_meta_info.json,*.fcpxml | ForEach-Object { $t=[IO.File]::ReadAllText($_.FullName); $t=$t.Replace('__MEDIA_URL__','file:///'+$r).Replace('__MEDIA_ROOT__',$r); [IO.File]::WriteAllText($_.FullName,$t) }"
echo 素材路径已修复。
echo 剪映：把「剪映草稿」里的文件夹整个复制到剪映草稿目录（剪映 → 全局设置 → 草稿位置），重启剪映即可在草稿列表看到。
echo 达芬奇 / Final Cut：导入 .fcpxml；Premiere：导入 .edl 后手动链接 media 文件夹。
pause
"""
FIX_PY = '''"""把工程文件里的素材占位路径改成本机绝对路径：python fix_paths.py"""
from pathlib import Path
here = Path(__file__).resolve().parent
root = (here / "media").as_posix()
url = "file:///" + root.lstrip("/")
for p in list(here.rglob("draft_content.json")) + list(here.rglob("draft_meta_info.json")) + list(here.rglob("*.fcpxml")):
    t = p.read_text("utf-8").replace("__MEDIA_URL__", url).replace("__MEDIA_ROOT__", root)
    p.write_text(t, "utf-8")
    print("已修复", p.relative_to(here))
'''
README = """AI Studio 工程导出
==================
1. 解压到一个固定位置（之后不要再移动 media 文件夹）。
2. Windows 双击「修复素材路径.bat」；macOS / Linux 运行 python3 fix_paths.py。
3. 剪映专业版：把「剪映草稿」下的文件夹复制到剪映草稿目录（剪映 → 全局设置 → 草稿位置），重启剪映。
   轨道：画面（有分轨配音的镜头已静音）、对白、旁白、环境音、音效、配乐、字幕。
4. 达芬奇 / Final Cut Pro：导入 {name}.fcpxml。Premiere：导入 {name}.edl（只有画面），字幕导入 subtitles.srt。
"""


@handler("export")
async def task_export(ctx: TaskContext) -> dict:
    from .pipeline import add_asset, need, project_dir
    project = need("projects", ctx.project_id, "项目")
    fps = int(ctx.params.get("fps") or (project.get("post") or {}).get("fps") or 30)
    await ctx.progress(5, "整理时间线…")
    tl = await build_timeline(project, fps)
    names = media_names(tl)
    name = "".join(ch for ch in (project["title"] or "AI Studio") if ch not in '\\/:*?"<>|').strip()[:40] or "AI Studio"
    out_dir = project_dir(project["id"], "final")
    out = out_dir / f"{name}_工程_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    draft, meta = jianying_draft(tl, name, names)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        total = len(names)
        for i, (src, dst) in enumerate(names.items()):
            z.write(src, f"media/{dst}", compress_type=zipfile.ZIP_STORED)
            await ctx.progress(10 + int(80 * (i + 1) / max(1, total)), f"打包素材 {i + 1}/{total}")
        z.writestr(f"剪映草稿/{name}/draft_content.json", json.dumps(draft, ensure_ascii=False))
        z.writestr(f"剪映草稿/{name}/draft_meta_info.json", json.dumps(meta, ensure_ascii=False))
        z.writestr(f"{name}.fcpxml", fcpxml(tl, name, names))
        z.writestr(f"{name}.edl", edl(tl, name, names))
        z.writestr("subtitles.srt", srt(tl))
        z.writestr("修复素材路径.bat", FIX_BAT.replace("\n", "\r\n").encode("utf-8"))
        z.writestr("fix_paths.py", FIX_PY)
        z.writestr("README.txt", README.format(name=name))
    counts = {k: len(tl[k]) for k in ("video", "dialogue", "ambient", "narration", "sfx", "bgm", "subs")}
    a = add_asset(project["id"], "export", out, "project", project["id"],
                  {"tracks": counts, "seconds": round(tl["total"], 2), "fps": fps,
                   "size_mb": round(out.stat().st_size / 1e6, 1)})
    return {"path": a["path"], "tracks": counts}


class ExportIn(BaseModel):
    fps: int = 0


@router.post("/projects/{project_id}/export")
async def export_project(project_id: str, body: ExportIn) -> dict:
    if not db.get("projects", project_id):
        raise HTTPException(404, "项目不存在")
    return queue.submit("export", project_id=project_id, title="导出剪映草稿 / 工程", owner_type="project",
                        owner_id=f"{project_id}:export", params={"fps": body.fps} if body.fps else {})
