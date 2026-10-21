"""ffmpeg 工具：成片拼接、字幕、背景音乐。"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} 失败：{text[-1500:]}")
    return text


async def probe(path: Path) -> dict:
    text = await _run("ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path))
    data = json.loads(text)
    streams = data.get("streams") or []
    return {"duration": float((data.get("format") or {}).get("duration") or 0),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(items: list[tuple[float, float, list[dict]]]) -> str:
    """items: (开始秒, 时长, 台词列表)；同一镜头多句台词平均分配时长。"""
    out, n = [], 0
    for start, dur, lines in items:
        if not lines:
            continue
        seg = dur / len(lines)
        for i, line in enumerate(lines):
            n += 1
            a, b = start + i * seg, start + (i + 1) * seg - 0.05
            out.append(f"{n}\n{_ts(a)} --> {_ts(b)}\n{line['line']}\n")
    return "\n".join(out)


async def assemble(clips: list[Path], output: Path, width: int, height: int, work: Path,
                   srt: str = "", bgm: Path | None = None, bgm_volume: float = 0.25) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("系统没有安装 ffmpeg")
    work.mkdir(parents=True, exist_ok=True)
    normalized = []
    for i, clip in enumerate(clips):
        info = await probe(clip)
        target = work / f"n{i:04d}.mp4"
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p")
        args = ["ffmpeg", "-y", "-i", str(clip)]
        if not info["has_audio"]:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
        args += ["-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k", str(target)]
        await _run(*args)
        normalized.append(target)
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in normalized), encoding="utf-8")
    joined = work / "joined.mp4"
    await _run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined))
    current = joined
    if bgm is not None:
        mixed = work / "bgm.mp4"
        await _run("ffmpeg", "-y", "-i", str(current), "-stream_loop", "-1", "-i", str(bgm),
                   "-filter_complex", f"[1:a]volume={bgm_volume}[b];[0:a][b]amix=inputs=2:duration=first:dropout_transition=0[a]",
                   "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(mixed))
        current = mixed
    if srt.strip():
        srt_path = work / "subs.srt"
        srt_path.write_text(srt, encoding="utf-8")
        subbed = work / "subs.mp4"
        style = "FontName=Noto Sans CJK SC,FontSize=16,Outline=1,Shadow=0,MarginV=24"
        await _run("ffmpeg", "-y", "-i", str(current), "-vf",
                   f"subtitles={srt_path.as_posix()}:force_style='{style}'",
                   "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "copy", str(subbed))
        current = subbed
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(current), output)
    return output


# ---------------- 配音用的音频工具 ----------------
async def extract_audio(video: Path, out: Path, rate: int = 44100, mono: bool = False) -> Path | None:
    if not (await probe(video))["has_audio"]:
        return None
    await _run("ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1" if mono else "2", "-ar", str(rate),
               str(out))
    return out


async def trim_silence(audio: Path, out: Path) -> Path:
    """去掉首尾静音（音色样本用），统一成 24kHz 单声道。"""
    f = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,areverse,"
         "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,areverse")
    await _run("ffmpeg", "-y", "-v", "error", "-i", str(audio), "-af", f, "-ac", "1", "-ar", "24000", str(out))
    return out


async def atempo(audio: Path, factor: float, out: Path) -> Path:
    await _run("ffmpeg", "-y", "-v", "error", "-i", str(audio), "-af", f"atempo={factor:.3f}", str(out))
    return out


async def speech_track(lines: list[tuple[Path, float]], total: float, out: Path) -> Path:
    """把多句台词按开始时间排进一条音轨，补静音到 total 秒（LatentSync 输出时长取视频和音频较短者）。
    第 0 路是 total 秒的静音底轨，amix 以它的长度为准，输出时长确定。"""
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
    for wav, _ in lines:
        args += ["-i", str(wav)]
    parts = []
    for i, (_, start) in enumerate(lines, 1):
        ms = int(start * 1000)
        parts.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{i}]")
    mix = "[0:a]" + "".join(f"[a{i}]" for i in range(1, len(lines) + 1))
    parts.append(f"{mix}amix=inputs={len(lines) + 1}:duration=first:normalize=0:dropout_transition=0[out]")
    args += ["-filter_complex", ";".join(parts), "-map", "[out]", "-t", f"{total:.3f}", "-ar", "44100", "-ac", "2", str(out)]
    await _run(*args)
    return out


async def extend_video(video: Path, extra: float, out: Path) -> Path:
    """台词比画面长时，定格最后一帧补足时长。"""
    await _run("ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", f"tpad=stop_mode=clone:stop_duration={extra:.3f}",
               "-af", f"apad=pad_dur={extra:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-c:a", "aac", str(out))
    return out


async def mux(video: Path, tracks: list[tuple[Path, float]], out: Path, duration: float) -> Path:
    """用 tracks=[(音频, 音量)] 混音后替换视频原声，时长对齐 duration（静音底轨定长）。"""
    args = ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", "anullsrc=r=44100:cl=stereo"]
    for a, _ in tracks:
        args += ["-i", str(a)]
    parts = [f"[{i + 2}:a]aresample=44100,aformat=channel_layouts=stereo,volume={vol}[t{i}]" for i, (_, vol) in enumerate(tracks)]
    ins = "[1:a]" + "".join(f"[t{i}]" for i in range(len(tracks)))
    parts.append(f"{ins}amix=inputs={len(tracks) + 1}:duration=first:normalize=0:dropout_transition=0[a]")
    args += ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[a]", "-t", f"{duration:.3f}",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)]
    await _run(*args)
    return out


# ---------------- 动态分镜预览（Animatic） ----------------
_C = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
_PAN_X, _PAN_Y = "(iw-iw/zoom)*{p}", "(ih-ih/zoom)*{p}"
# 运镜 → (zoom, x, y)；{p} 为 0→1 的进度
KEN_BURNS = {
    "固定": ("1.0+0.05*{p}", *_C),
    "推镜": ("1.0+0.3*{p}", *_C),
    "拉镜": ("1.3-0.3*{p}", *_C),
    "摇镜": ("1.2", _PAN_X, _C[1]),
    "移镜": ("1.2", _PAN_X.replace("{p}", "(1-{p})"), _C[1]),
    "跟拍": ("1.1+0.1*{p}", _PAN_X, _C[1]),
    "环绕": ("1.25-0.05*{p}", _PAN_X.replace("{p}", "(1-{p})"), _C[1]),
    "升降": ("1.2", _C[0], _PAN_Y.replace("{p}", "(1-{p})")),
}
PREVIEW_FPS = 24


async def still_clip(image: Path, out: Path, dur: float, width: int, height: int, move: str = "固定") -> Path:
    """静态分镜图 + 推拉摇移（Ken Burns），无声，固定 24fps。"""
    frames = max(1, round(dur * PREVIEW_FPS))
    z, x, y = (e.replace("{p}", f"(on/{frames})") for e in KEN_BURNS.get(move, KEN_BURNS["固定"]))
    vf = (f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,crop={width * 2}:{height * 2},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={PREVIEW_FPS},format=yuv420p")
    await _run("ffmpeg", "-y", "-v", "error", "-i", str(image), "-vf", vf, "-frames:v", str(frames), "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(PREVIEW_FPS), str(out))
    return out


async def video_clip(video: Path, out: Path, dur: float, width: int, height: int) -> Path:
    """已有镜头视频：缩放到预览尺寸，裁/定格到 dur 秒，无声。"""
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
          f"fps={PREVIEW_FPS},tpad=stop_mode=clone:stop_duration={dur:.3f},format=yuv420p")
    await _run("ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", vf, "-t", f"{dur:.3f}", "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(PREVIEW_FPS), str(out))
    return out


async def card_clip(out: Path, dur: float, width: int, height: int) -> Path:
    """缺分镜图的镜头：灰底占位卡片（镜头说明由字幕层画在左上角）。"""
    await _run("ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
               f"color=c=0x2b2f36:s={width}x{height}:r={PREVIEW_FPS}:d={dur:.3f}", "-vf", "format=yuv420p", "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out))
    return out


def _ass_ts(sec: float) -> str:
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_from(subs: list[tuple[float, float, str]], labels: list[tuple[float, float, str]], width: int, height: int,
             font: str = "Noto Sans CJK SC") -> str:
    """预览用 ASS 字幕：底部台词 + 左上角镜头标签（#序号 景别·运镜·时长）。不依赖 drawtext。"""
    size, lsize = max(16, height // 18), max(12, height // 30)

    def esc(t: str) -> str:
        return t.replace("\\", "＼").replace("{", "（").replace("}", "）").replace("\n", " ")

    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Sub,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,{height // 20},1\n"
            f"Style: Label,{font},{lsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,3,4,0,7,14,14,12,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    rows = [f"Dialogue: 0,{_ass_ts(a)},{_ass_ts(max(a + 0.2, b))},Sub,,0,0,0,,{esc(t)}" for a, b, t in subs]
    rows += [f"Dialogue: 1,{_ass_ts(a)},{_ass_ts(b)},Label,,0,0,0,,{esc(t)}" for a, b, t in labels]
    return head + "\n".join(rows) + "\n"


async def animatic(segments: list[Path], output: Path, work: Path, total: float,
                   speech: list[tuple[Path, float]] | None = None, ass: str = "") -> Path:
    """拼接预览片段（同编码，直接 copy），再一次性加声音（台词试听或静音）和字幕。"""
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in segments), encoding="utf-8")
    joined = work / "joined.mp4"
    await _run("ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined))
    audio = await speech_track(speech, total, work / "speech.wav") if speech else None
    args = ["ffmpeg", "-y", "-v", "error", "-i", str(joined)]
    args += ["-i", str(audio)] if audio else ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
    if ass.strip():
        ass_path = work / "subs.ass"
        ass_path.write_text(ass, encoding="utf-8")
        args += ["-vf", f"ass={ass_path.as_posix()}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    else:
        args += ["-c:v", "copy"]
    args += ["-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.3f}",
             "-movflags", "+faststart", str(output)]
    output.parent.mkdir(parents=True, exist_ok=True)
    await _run(*args)
    return output
