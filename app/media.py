"""ffmpeg 工具：成片拼接、字幕、背景音乐。"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
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
