"""P3-5 声音后期：旁白/解说配音、音效库自动匹配、BGM 生成（ACE-Step，按情绪分段）、闪避混音 + 响度标准化。"""
from __future__ import annotations

import json
import math
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from . import db, media
from .config import get_settings
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")

NARRATORS = {"旁白", "解说", "画外音", "OS", "VO"}
AUDIO_SUFFIX = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
LUFS = -14.0                  # 短视频平台常用响度
TRUE_PEAK = -1.5


def sound_opts(project: dict) -> dict:
    o = (project.get("post") or {}).get("sound") or {}
    return {"narration": o.get("narration", True), "narr_volume": float(o.get("narr_volume", 1.0)),
            "sfx_auto": o.get("sfx_auto", True), "sfx_volume": float(o.get("sfx_volume", 0.6)),
            "bgm_auto": o.get("bgm_auto", True), "bgm_volume": float(o.get("bgm_volume", 0.25)),
            "duck": o.get("duck", True), "loudnorm": o.get("loudnorm", True), "lufs": float(o.get("lufs", LUFS)),
            "voice": o.get("voice", ""), "emo": o.get("emo", "平静、娓娓道来"), "style": o.get("style", "")}


# ======================= 旁白 / 解说 =======================
def narration_items(shot: dict) -> list[tuple[int, str]]:
    """本镜要配的旁白：台词里说话人是「旁白」的句子 + 镜头的解说词。返回 [(台词序号, 文本)]，解说词序号 = len(台词)。"""
    dlg = shot.get("dialogue") or []
    out = [(j, str(d.get("line") or "").strip()) for j, d in enumerate(dlg)
           if (d.get("speaker") or "").strip() in NARRATORS and str(d.get("line") or "").strip()]
    if (shot.get("narration") or "").strip():
        out.append((len(dlg), shot["narration"].strip()))
    return out


def narrator_voice(project: dict) -> Path | None:
    """旁白音色：后期设置里选的角色音色 / 上传的样本；都没有就用 data/voices/narrator.wav。"""
    from .pipeline import absolute, exists
    v = sound_opts(project)["voice"]
    if v.startswith("char:"):
        c = db.get("characters", v[5:])
        if c and exists(c.get("voice_ref")):
            return absolute(c["voice_ref"])
    elif v and exists(v):
        return absolute(v)
    default = get_settings().data_dir / "voices" / "narrator.wav"
    return default if default.is_file() else None


@handler("narration")
async def task_narration(ctx: TaskContext) -> dict:
    from . import voice
    from .gpu import gpu
    from .pipeline import add_asset, need, project_dir, shots_of
    project = need("projects", ctx.project_id, "项目")
    if not voice.tts_available():
        raise RuntimeError("未安装配音模型（IndexTTS2），请运行 bash deploy/06_voice.sh")
    ref = narrator_voice(project)
    if ref is None:
        raise ValueError("还没有旁白音色：在⑤成片 → 声音里选一个角色音色或上传旁白样本")
    ids = set(ctx.params.get("shot_ids") or [])
    shots = [s for s in shots_of(project["id"]) if (not ids or s["id"] in ids) and narration_items(s)]
    if not shots:
        raise ValueError("没有需要配旁白的镜头（台词里没有「旁白」，也没有写解说词）")
    emo = ctx.params.get("emo") or sound_opts(project)["emo"]
    work = project_dir(project["id"], "dub") / f"narr_{ctx.id}"
    work.mkdir(parents=True, exist_ok=True)
    items, index = [], []
    for s in shots:
        for k, (j, text) in enumerate(narration_items(s)):
            out = work / f"{s['id']}_{k}.wav"
            items.append({"text": text, "voice": str(ref), "emo": emo, "out": str(out)})
            index.append((s, j, out))
    done = []
    try:
        await ctx.progress(5, f"合成 {len(items)} 句旁白…")
        async with gpu.use("voice", ctx.task.get("title") or "旁白配音"):
            await voice.synthesize(items, ctx.log)
        await ctx.progress(80, "整理旁白音轨…")
        for s in shots:
            parts = [(j, p) for sh, j, p in index if sh["id"] == s["id"]]
            durs = [(await media.probe(p))["duration"] for _, p in parts]
            gap = 0.25
            total = sum(durs) + gap * (len(durs) - 1)
            limit = float(s.get("duration") or 5) - 0.4
            factor = 1.0
            if total > limit > 1:                     # 比镜头长：最多加速 20%，再长就让它延续到下一镜
                factor = min(1.2, total / limit)
                parts = [(j, await media.atempo(p, factor, p.with_name(p.stem + "_t.wav"))) for j, p in parts]
                durs = [d / factor for d in durs]
            starts, t = [], 0.0
            for d in durs:
                starts.append(t)
                t += d + gap
            out = project_dir(project["id"], "dub") / f"narr_{s['id']}_{time.strftime('%Y%m%d_%H%M%S')}.wav"
            await media.speech_track([(p, st) for (_, p), st in zip(parts, starts)], t - gap + 0.05, out)
            timing = [{"j": j, "start": round(st, 3), "dur": round(d, 3)} for (j, _), st, d in zip(parts, starts, durs)]
            a = add_asset(project["id"], "narration", out, "shot", s["id"],
                          {"timing": timing, "speed": round(factor, 2), "seconds": round(t - gap, 2),
                           "text": "；".join(x for _, x in narration_items(s))})
            db.update("shots", s["id"], {"narration_audio": a["path"]})
            done.append(s["id"])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"shots": len(done), "lines": len(items)}


NARR_STYLES = {
    "解说": "第三人称短剧解说：交代人物关系和前因后果，制造悬念，口语化，像抖音解说号",
    "第一人称": "主角第一人称内心独白：说出心里话和隐藏的情绪，克制，有画面感",
    "悬念": "只在关键镜头点一句，制造反转或悬念钩子（如「她不知道，这通电话会改变一切」），其余镜头留空",
}
NARR_SYSTEM = """你是短剧后期的解说词撰稿人。给每个镜头写一句旁白解说词，配在画面上由配音员读出。
要求：
- 每句不超过「字数上限」，读完时间不超过镜头时长（约每秒 4 个字）；
- 不重复画面里角色已经说出来的台词，补充画面外的信息、心理或悬念；
- 已有对白很满的镜头可以留空（text 写空字符串）；
- 只输出 JSON：{"lines": [{"n": 镜头序号, "text": "解说词"}]}"""


@handler("narration_write")
async def task_narration_write(ctx: TaskContext) -> dict:
    from . import llm
    from .pipeline import need, shots_of
    project = need("projects", ctx.project_id, "项目")
    style = ctx.params.get("style") if ctx.params.get("style") in NARR_STYLES else "解说"
    overwrite = bool(ctx.params.get("overwrite"))
    shots = shots_of(project["id"])
    if not shots:
        raise ValueError("还没有分镜")
    rows = []
    for i, s in enumerate(shots):
        lines = "；".join(f"{d.get('speaker')}：{d.get('line')}" for d in s.get("dialogue") or [] if d.get("line"))
        cap = max(6, int(float(s.get("duration") or 5) * 4) - len(re.sub(r"\s", "", lines)) // 2)
        rows.append(f"{i + 1}. [{s.get('scene') or ''}] 画面：{s.get('description') or ''}｜动作：{s.get('action') or ''}"
                    f"｜台词：{lines or '无'}｜时长 {s.get('duration')} 秒｜字数上限 {cap}")
    user = (f"作品：{project.get('title')}\n梗概：{project.get('synopsis') or ''}\n解说风格：{style}——{NARR_STYLES[style]}\n\n"
            "镜头列表：\n" + "\n".join(rows))

    def ok(data: Any) -> dict:
        if not (isinstance(data, dict) and isinstance(data.get("lines"), list)):
            raise ValueError('需要 {"lines": [...]}')
        return data
    await ctx.progress(10, "撰写解说词…")
    data = await llm.chat_json(NARR_SYSTEM, user, validate=ok)
    n = 0
    for row in data["lines"]:
        try:
            k = int(row.get("n")) - 1
        except (TypeError, ValueError):
            continue
        text = str(row.get("text") or "").strip()
        if 0 <= k < len(shots) and text and (overwrite or not (shots[k].get("narration") or "").strip()):
            db.update("shots", shots[k]["id"], {"narration": text})
            n += 1
    return {"written": n, "style": style}


def narration_offset(dub_timing: list[dict] | None) -> float:
    """旁白在镜头里的起点：镜头有对白配音时排在对白后面，否则 0.3 秒处开始。"""
    if dub_timing:
        return round(max(0.3, max(t["start"] + t["dur"] for t in dub_timing) + 0.3), 3)
    return 0.3


# ======================= 音效库 =======================
def sfx_dir() -> Path:
    d = get_settings().data_dir / "sfx"
    d.mkdir(parents=True, exist_ok=True)
    return d


LOOP_HINTS = ("雨", "风", "人群", "嘈杂", "环境", "海浪", "虫鸣", "鸟叫", "车流", "街道", "背景", "火", "水流", "ambient", "loop")


def sfx_tags(name: str) -> list[str]:
    stem = Path(name).stem
    return [t for t in re.split(r"[_\-\s,，、.（）()]+", stem) if t and not t.isdigit()]


def list_sfx() -> list[dict]:
    out = []
    for p in sorted(sfx_dir().rglob("*")):
        if p.suffix.lower() in AUDIO_SUFFIX and p.is_file():
            out.append({"name": str(p.relative_to(sfx_dir())), "tags": sfx_tags(p.name),
                        "loop": any(h in p.stem.lower() for h in LOOP_HINTS)})
    return out


def match_sfx(text: str, lib: list[dict] | None = None, limit: int = 3) -> list[dict]:
    """把镜头的「环境音/音效」描述按顿号拆开，每一段找文件名标签重合最多的音效。"""
    lib = list_sfx() if lib is None else lib
    picked: list[dict] = []
    for phrase in [p.strip() for p in re.split(r"[、，,；;。/\n]+", text or "") if p.strip()]:
        best, score = None, 0
        for item in lib:
            s = sum(len(t) for t in item["tags"] if t.lower() in phrase.lower())
            if s > score:
                best, score = item, s
        if best and best["name"] not in {p["name"] for p in picked}:
            picked.append({"name": best["name"], "offset": 0.0, "loop": best["loop"] or any(h in phrase for h in LOOP_HINTS),
                           "phrase": phrase})
        if len(picked) >= limit:
            break
    return picked


def shot_sfx(shot: dict, auto: bool, lib: list[dict] | None = None) -> list[dict]:
    manual = shot.get("sfx") or []
    if manual:
        return [m for m in manual if isinstance(m, dict) and (sfx_dir() / str(m.get("name") or "")).is_file()]
    return match_sfx(shot.get("sound") or "", lib) if auto else []


async def place_track(items: list[dict], total: float, out: Path) -> Path:
    """items: [{"path", "start", "dur"(可空), "volume", "loop"}] 摆到 total 秒长的音轨上（第 0 路静音底轨定长）。"""
    args = ["ffmpeg", "-y", "-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    parts, mix = [], "[0:a]"
    for i, it in enumerate(items, start=1):
        if it.get("loop"):
            args += ["-stream_loop", "-1"]
        args += ["-i", str(it["path"])]
        ms = max(0, int(it["start"] * 1000))
        chain = f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo"
        if it.get("dur"):
            d = max(0.2, float(it["dur"]))
            chain += f",atrim=0:{d:.3f},afade=t=out:st={max(0.0, d - 0.3):.3f}:d=0.3"
        chain += f",volume={float(it.get('volume', 1.0)):.3f},adelay={ms}|{ms}[s{i}]"
        parts.append(chain)
        mix += f"[s{i}]"
    parts.append(f"{mix}amix=inputs={len(items) + 1}:duration=first:normalize=0:dropout_transition=0[out]")
    await media._run(*args, "-filter_complex", ";".join(parts), "-map", "[out]", "-ar", "48000", str(out))
    return out


# ======================= 混音：旁白 + 音效 + BGM 闪避 + 响度标准化 =======================
def mix_graph(idx: dict[str, int], bgm_volume: float, duck: bool, fade_out: float = 0.0) -> tuple[str, str]:
    """idx: {"narr": 输入序号, "sfx": 序号, "bgm": 序号}；返回 (filter_complex, 输出标签)。"""
    parts = ["[0:a]aresample=48000,aformat=channel_layouts=stereo[main]"]
    fg = "[main]"
    if "narr" in idx:
        parts.append(f"[{idx['narr']}:a]aresample=48000,aformat=channel_layouts=stereo[nr]")
        parts.append(f"{fg}[nr]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[vo]")
        fg = "[vo]"
    if "sfx" in idx:
        parts.append(f"[{idx['sfx']}:a]aresample=48000,aformat=channel_layouts=stereo[fx]")
    bg = ""
    if "bgm" in idx:
        b = f"[{idx['bgm']}:a]aresample=48000,aformat=channel_layouts=stereo,volume={bgm_volume:.3f},afade=t=in:d=1"
        if fade_out > 2:
            b += f",afade=t=out:st={fade_out - 1.5:.3f}:d=1.5"
        parts.append(b + "[bg]")
        bg = "[bg]"
        if duck:     # 人声（对白 + 旁白）一响，BGM 自动压低；人声停下 0.6 秒内恢复
            parts.append(f"{fg}asplit=2[fg1][key]")
            parts.append("[bg][key]sidechaincompress=threshold=0.02:ratio=8:attack=30:release=600:makeup=1[bgd]")
            fg, bg = "[fg1]", "[bgd]"
    inputs = [fg] + (["[fx]"] if "sfx" in idx else []) + ([bg] if bg else [])
    if len(inputs) > 1:
        parts.append(f"{''.join(inputs)}amix=inputs={len(inputs)}:duration=first:normalize=0:dropout_transition=0[mx]")
    else:
        parts.append(f"{fg}anull[mx]")
    return ";".join(parts), "[mx]"


def parse_loudnorm(text: str) -> dict | None:
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", text)
    if not m:
        return None
    data = json.loads(m[-1])
    try:
        if not math.isfinite(float(data["input_i"])) or float(data["input_i"]) < -70:
            return None
    except (KeyError, ValueError):
        return None
    return data


async def mix(video: Path, out: Path, narration: Path | None = None, sfx: Path | None = None, bgm: Path | None = None,
              bgm_volume: float = 0.25, duck: bool = True, loudnorm: bool = True, lufs: float = LUFS,
              duration: float = 0.0) -> dict:
    """两遍 loudnorm（先测量再线性校正），响度更准、不压动态。返回 {"lufs": 处理前响度, "target", "layers"}。"""
    args = ["-i", str(video)]
    idx: dict[str, int] = {}
    for key, p in (("narr", narration), ("sfx", sfx), ("bgm", bgm)):
        if p is not None:
            if key == "bgm":
                args += ["-stream_loop", "-1"]
            args += ["-i", str(p)]
            idx[key] = len(idx) + 1
    graph, lab = mix_graph(idx, bgm_volume, duck, duration)
    info: dict = {"target": lufs if loudnorm else None, "layers": sorted(idx)}
    final = lab
    if loudnorm:
        ln = f"loudnorm=I={lufs}:TP={TRUE_PEAK}:LRA=11"
        text = await media._run("ffmpeg", "-hide_banner", "-nostats", *args, "-filter_complex",
                                f"{graph};{lab}{ln}:print_format=json[ln]", "-map", "[ln]", "-f", "null", "-")
        meas = parse_loudnorm(text)
        if meas:
            info["lufs"] = round(float(meas["input_i"]), 1)
            graph += (f";{lab}{ln}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:measured_LRA={meas['input_lra']}"
                      f":measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true,aresample=48000[ln]")
            final = "[ln]"
        else:
            info["target"] = None                 # 全片无声：跳过响度标准化
    await media._run("ffmpeg", "-y", *args, "-filter_complex", graph, "-map", "0:v", "-map", final,
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", str(out))
    return info


async def measure_lufs(path: Path) -> float | None:
    text = await media._run("ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                            f"loudnorm=I={LUFS}:TP={TRUE_PEAK}:print_format=json", "-f", "null", "-")
    m = parse_loudnorm(text)
    return round(float(m["input_i"]), 1) if m else None


async def build_layers(project: dict, shots: list[dict], starts: list[float], total: float, work: Path) -> dict:
    """按成片时间线把旁白、音效摆成两条整轨。starts[k] 是 shots[k] 在成片里的起点（已算上片头、转场）。
    shots 里可带 _dur（镜头实际时长）、_dub_timing（对白配音时间）。"""
    from .pipeline import absolute, exists
    o = sound_opts(project)
    narr_items, sfx_items = [], []
    lib = list_sfx()
    for s, st in zip(shots, starts):
        dur = float(s.get("_dur") or s.get("duration") or 5)
        if o["narration"] and exists(s.get("narration_audio")):
            off = narration_offset(s.get("_dub_timing"))
            narr_items.append({"path": absolute(s["narration_audio"]), "start": st + off, "volume": o["narr_volume"]})
        for fx in shot_sfx(s, o["sfx_auto"], lib):
            off = float(fx.get("offset") or 0)
            sfx_items.append({"path": sfx_dir() / fx["name"], "start": st + off, "dur": dur - off,
                              "loop": bool(fx.get("loop")), "volume": float(fx.get("volume") or o["sfx_volume"])})
    out: dict = {"narration": None, "sfx": None, "narr_count": len(narr_items), "sfx_count": len(sfx_items)}
    if narr_items:
        out["narration"] = await place_track(narr_items, total, work / "narration.wav")
    if sfx_items:
        out["sfx"] = await place_track(sfx_items, total, work / "sfx.wav")
    return out


# ======================= BGM：ACE-Step（ComfyUI 原生节点） =======================
MOODS = {
    "紧张": ("suspense, tense, dark, low strings, pulsing synth bass, cinematic, instrumental",
             ("紧张", "危险", "追", "逃", "惊恐", "威胁", "怀疑", "阴谋", "跟踪", "冷笑", "质问", "愤怒")),
    "悲伤": ("sad, emotional, slow piano, soft strings, melancholy, cinematic, instrumental",
             ("哭", "泪", "悲", "难过", "离开", "失去", "痛", "绝望", "告别", "心碎", "委屈")),
    "温馨": ("warm, gentle, acoustic guitar, soft piano, heartwarming, light, instrumental",
             ("温柔", "微笑", "拥抱", "家", "温暖", "感动", "陪伴", "安慰")),
    "浪漫": ("romantic, love theme, piano and strings, dreamy, soft, instrumental",
             ("心动", "吻", "爱", "脸红", "牵手", "约会", "表白", "暧昧")),
    "燃": ("epic, powerful, orchestral, big drums, heroic, rising, trailer, instrumental",
           ("反击", "打脸", "逆袭", "爆发", "决战", "震惊", "霸气", "怒吼", "崛起", "胜利")),
    "欢快": ("upbeat, playful, cheerful, pizzicato, light percussion, comedy, instrumental",
             ("笑", "开心", "搞笑", "调皮", "得意", "兴奋", "惊喜")),
    "神秘": ("mysterious, ambient, ethereal pads, music box, curious, instrumental",
             ("秘密", "神秘", "奇怪", "线索", "照片", "身份", "梦", "回忆")),
    "平静": ("calm, ambient, soft piano, minimal, peaceful, instrumental", ()),
}
GENRE_MOOD = {"suspense": "紧张", "romance": "浪漫", "revenge": "燃", "comedy": "欢快", "fantasy": "神秘"}


def shot_mood(shot: dict, default: str = "平静") -> str:
    text = " ".join([shot.get("description") or "", shot.get("action") or "", shot.get("performance") or "",
                     " ".join(f"{d.get('emotion') or ''} {d.get('line') or ''}" for d in shot.get("dialogue") or [])])
    best, score = default, 0
    for mood, (_, words) in MOODS.items():
        s = sum(text.count(w) for w in words)
        if s > score:
            best, score = mood, s
    return best


def mood_segments(shots: list[dict], durs: list[float], genre: str = "", min_seconds: float = 8.0) -> list[dict]:
    """情绪曲线：按场次分段，每段取时长加权最多的情绪；相邻同情绪合并，太短的段并进前一段。"""
    from .director import _scene_key
    default = GENRE_MOOD.get(genre or "", "平静")
    segs: list[dict] = []
    for s, d in zip(shots, durs):
        key = _scene_key(s)
        if not (segs and segs[-1]["key"] == key):
            segs.append({"key": key, "seconds": 0.0, "votes": [], "shots": []})
        segs[-1]["seconds"] += d
        segs[-1]["votes"].append((shot_mood(s, default), d))
        segs[-1]["shots"].append(s["id"])
    for g in segs:
        w: dict[str, float] = {}
        for m, d in g["votes"]:
            w[m] = w.get(m, 0) + d + (0 if m == default else 0.5)    # 非默认情绪略加权，避免全片「平静」
        g["mood"] = max(w, key=w.get)
    merged: list[dict] = []
    for g in segs:
        if merged and (merged[-1]["mood"] == g["mood"] or g["seconds"] < min_seconds):
            merged[-1]["seconds"] += g["seconds"]
            merged[-1]["shots"] += g["shots"]
        else:
            merged.append({"mood": g["mood"], "seconds": g["seconds"], "shots": list(g["shots"])})
    if len(merged) > 1 and merged[0]["seconds"] < min_seconds:
        merged[1]["seconds"] += merged[0]["seconds"]
        merged[1]["shots"] = merged[0]["shots"] + merged[1]["shots"]
        merged.pop(0)
    return [{"mood": g["mood"], "seconds": round(g["seconds"], 2), "shots": g["shots"], "tags": MOODS[g["mood"]][0]}
            for g in merged]


def music_template() -> dict | None:
    p = get_settings().data_dir / "workflows" / "music.json"
    return json.loads(p.read_text("utf-8")) if p.is_file() else None


def music_available() -> bool:
    ckpt = get_settings().comfyui_dir / "models" / "checkpoints" / get_settings().music_ckpt
    return ckpt.is_file() or music_template() is not None


def music_workflow(tags: str, seconds: float, seed: int, lyrics: str = "[instrumental]", steps: int = 50) -> dict:
    """ACE-Step v1 3.5B（ComfyUI 原生支持）。data/workflows/music.json 存在时用它，占位符 {{TAGS}} {{LYRICS}} {{SECONDS}} {{SEED}} {{STEPS}}。"""
    tpl = music_template()
    if tpl is not None:
        from .videox import fill
        return fill(tpl, {"TAGS": tags, "LYRICS": lyrics, "SECONDS": round(seconds, 1), "SEED": seed, "STEPS": steps})
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": get_settings().music_ckpt}},
        "2": {"class_type": "TextEncodeAceStepAudio", "inputs": {"clip": ["1", 1], "tags": tags, "lyrics": lyrics,
                                                                  "lyrics_strength": 0.99}},
        "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}},
        "4": {"class_type": "EmptyAceStepLatentAudio", "inputs": {"seconds": round(seconds, 1), "batch_size": 1}},
        "5": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 5.0}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["5", 0], "positive": ["2", 0], "negative": ["3", 0],
                                                   "latent_image": ["4", 0], "seed": seed, "steps": steps, "cfg": 5.0,
                                                   "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveAudio", "inputs": {"audio": ["7", 0], "filename_prefix": "studio/bgm"}},
    }


async def crossfade_concat(parts: list[Path], out: Path, fade: float = 2.0) -> Path:
    if len(parts) == 1:
        await media._run("ffmpeg", "-y", "-i", str(parts[0]), "-ar", "48000", "-ac", "2", str(out))
        return out
    args, chain, last = ["ffmpeg", "-y"], [], "[0:a]"
    for p in parts:
        args += ["-i", str(p)]
    for i in range(1, len(parts)):
        chain.append(f"{last}[{i}:a]acrossfade=d={fade}:c1=tri:c2=tri[x{i}]")
        last = f"[x{i}]"
    await media._run(*args, "-filter_complex", ";".join(chain), "-map", last, "-ar", "48000", "-ac", "2", str(out))
    return out


@handler("bgm")
async def task_bgm(ctx: TaskContext) -> dict:
    from . import comfy
    from .gpu import gpu
    from .pipeline import absolute, add_asset, exists, need, progress_slice, project_dir, shots_of
    from .workflows import seed_or_random
    project = need("projects", ctx.project_id, "项目")
    shots = shots_of(project["id"])
    if not shots:
        raise ValueError("还没有分镜")
    durs = []
    for s in shots:
        d = float(s.get("duration") or 5)
        if exists(s.get("video")):
            d = (await media.probe(absolute(s["video"])))["duration"] or d
        durs.append(d)
    style = (ctx.params.get("style") or sound_opts(project)["style"] or "").strip()
    if ctx.params.get("single"):
        segs = [{"mood": "全片", "seconds": round(sum(durs), 2), "shots": [s["id"] for s in shots],
                 "tags": style or MOODS[GENRE_MOOD.get(project.get("genre") or "", "平静")][0]}]
    else:
        segs = mood_segments(shots, durs, project.get("genre") or "")
        if style:
            for g in segs:
                g["tags"] = f"{style}, {g['tags']}"
    fade = 2.0
    seed = seed_or_random(ctx.params.get("seed"))
    work = project_dir(project["id"], "audio") / f"bgm_{ctx.id}"
    work.mkdir(parents=True, exist_ok=True)
    parts = []
    try:
        async with gpu.use("comfyui", ctx.task.get("title") or "生成配乐"):
            await gpu.comfy_switch("music")
            for i, g in enumerate(segs):
                secs = min(240.0, g["seconds"] + (fade if i < len(segs) - 1 else 1.0))
                lo, hi = 5 + 85 * i // len(segs), 5 + 85 * (i + 1) // len(segs)
                await ctx.log(f"配乐第 {i + 1}/{len(segs)} 段：{g['mood']} {secs:.0f}s · {g['tags']}")
                files = await comfy.run(music_workflow(g["tags"], secs, seed + i), work / f"s{i}",
                                        progress_slice(ctx, lo, hi, f"配乐 {i + 1}/{len(segs)}"))
                a = next((f for f in files if f.suffix.lower() in AUDIO_SUFFIX), None)
                if not a:
                    raise RuntimeError("音乐工作流没有产出音频（检查 ACE-Step 模型是否已下载：bash deploy/09_music.sh）")
                parts.append(a)
        await ctx.progress(92, "拼接配乐…")
        out = project_dir(project["id"], "audio") / f"bgm_{time.strftime('%Y%m%d_%H%M%S')}.wav"
        await crossfade_concat(parts, out, fade)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    asset = add_asset(project["id"], "bgm", out, "project", project["id"],
                      {"segments": [{k: g[k] for k in ("mood", "seconds", "tags")} for g in segs], "seed": seed,
                       "seconds": round((await media.probe(out))["duration"], 1)})
    db.update("projects", project["id"], {"bgm": asset["path"]})
    return {"asset": asset["id"], "segments": len(segs)}


# ======================= 接口 =======================
class NarrBody(BaseModel):
    shot_ids: list[str] = []
    emo: str = ""


class NarrWriteBody(BaseModel):
    style: str = "解说"
    overwrite: bool = False


class BgmBody(BaseModel):
    style: str = ""
    single: bool = False
    seed: int | None = None


def _project(pid: str) -> dict:
    p = db.get("projects", pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


@router.post("/projects/{project_id}/narration")
async def make_narration(project_id: str, body: NarrBody) -> dict:
    _project(project_id)
    return queue.submit("narration", project_id=project_id, title="旁白配音", params=body.model_dump(), dedupe=False)


@router.post("/projects/{project_id}/narration/write")
async def write_narration(project_id: str, body: NarrWriteBody) -> dict:
    _project(project_id)
    return queue.submit("narration_write", project_id=project_id, title="撰写解说词", params=body.model_dump())


@router.post("/projects/{project_id}/narration/voice")
async def upload_narrator(project_id: str, file: UploadFile = File(...)) -> dict:
    from .api import _save_upload
    from .pipeline import add_asset, rel
    p = _project(project_id)
    target = await _save_upload(file, project_id, "uploads")
    add_asset(project_id, "voice", target, "project", project_id, {"narrator": True})
    post = dict(p.get("post") or {})
    post["sound"] = {**(post.get("sound") or {}), "voice": rel(target)}
    return db.update("projects", project_id, {"post": post})


@router.post("/projects/{project_id}/bgm")
async def make_bgm(project_id: str, body: BgmBody) -> dict:
    _project(project_id)
    return queue.submit("bgm", project_id=project_id, title="生成配乐", params=body.model_dump(), dedupe=False)


@router.get("/projects/{project_id}/bgm/plan")
async def bgm_plan(project_id: str) -> dict:
    from .pipeline import shots_of
    p = _project(project_id)
    shots = shots_of(project_id)
    return {"segments": mood_segments(shots, [float(s.get("duration") or 5) for s in shots], p.get("genre") or "")}


@router.get("/sfx")
async def get_sfx() -> list[dict]:
    return list_sfx()


@router.post("/sfx")
async def upload_sfx(file: UploadFile = File(...)) -> dict:
    name = Path(file.filename or "sfx.wav").name
    if Path(name).suffix.lower() not in AUDIO_SUFFIX:
        raise HTTPException(400, "只支持 wav/mp3/flac/ogg/m4a 音频")
    target = sfx_dir() / name
    with open(target, "wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    return {"name": name, "tags": sfx_tags(name)}


@router.delete("/sfx/{name:path}")
async def delete_sfx(name: str) -> dict:
    p = (sfx_dir() / name).resolve()
    if sfx_dir().resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "音效不存在")
    p.unlink()
    return {"ok": True}


@router.get("/shots/{shot_id}/sfx")
async def shot_sfx_preview(shot_id: str) -> dict:
    s = db.get("shots", shot_id)
    if not s:
        raise HTTPException(404, "镜头不存在")
    return {"manual": s.get("sfx") or [], "auto": match_sfx(s.get("sound") or "")}
