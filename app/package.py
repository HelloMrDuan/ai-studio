"""P3-4 字幕与包装：ASS 字幕样式（描边/阴影/位置/逐字高亮/关键词花字）、镜头转场（xfade）、片头/集数/片尾卡、封面图。"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import media

# ---------------- 字幕样式 ----------------
# 颜色用 ASS 的 &HAABBGGRR
SUB_PRESETS = {
    "经典": {"label": "白字黑边", "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H64000000", "size": 1.0,
             "outline_w": 2.2, "shadow": 0.8, "bold": 0},
    "短剧": {"label": "黄字粗黑边（短剧常见）", "primary": "&H0000E6FF", "outline": "&H00000000", "back": "&H80000000",
             "size": 1.15, "outline_w": 3.2, "shadow": 1.2, "bold": 1},
    "综艺": {"label": "白字彩边花字", "primary": "&H00FFFFFF", "outline": "&H00B4469B", "back": "&H00000000",
             "size": 1.2, "outline_w": 3.5, "shadow": 2.0, "bold": 1},
    "电影": {"label": "细小白字（电影感）", "primary": "&H00F0F0F0", "outline": "&H00202020", "back": "&H00000000",
             "size": 0.85, "outline_w": 1.0, "shadow": 0.6, "bold": 0},
    "底条": {"label": "半透明黑底条", "primary": "&H00FFFFFF", "outline": "&H96000000", "back": "&H96000000",
             "size": 1.0, "outline_w": 6, "shadow": 0, "bold": 0, "box": True},
}
POSITIONS = {"bottom": 2, "middle": 5, "top": 8}
HIGHLIGHT = "&H0000D7FF"     # 关键词：金黄
KARAOKE_DIM = "&H00A0A0A0"   # 逐字高亮：未读到的字


def _hex2ass(c: str) -> str:
    """#RRGGBB → &H00BBGGRR；已经是 ASS 格式的原样返回。"""
    c = (c or "").strip()
    if c.startswith("&H"):
        return c
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", c)
    if not m:
        return ""
    h = m.group(1)
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def esc(t: str) -> str:
    return t.replace("\\", "＼").replace("{", "（").replace("}", "）").replace("\n", " ")


def line_timing(start: float, dur: float, lines: list[dict], exact: list[dict] | None = None) -> list[tuple[float, float, dict]]:
    """每句台词的起止时间：有配音时间就用配音的，否则按字数比例分配。"""
    if not lines:
        return []
    by_j = {t["j"]: t for t in exact or [] if t.get("j", -1) >= 0}
    timed = [(start + by_j[j]["start"], start + by_j[j]["start"] + by_j[j]["dur"], d) for j, d in enumerate(lines) if j in by_j]
    rest = [d for j, d in enumerate(lines) if j not in by_j]       # 没配音的句子（缺音色 / 旁白未配）按字数比例铺满镜头
    weights = [max(2, len(re.sub(r"\s", "", d.get("line") or ""))) for d in rest]
    total, t, out = sum(weights), start, []
    for w, d in zip(weights, rest):
        seg = dur * w / total
        out.append((t, t + seg - 0.05, d))
        t += seg
    return sorted(timed + out, key=lambda x: x[0])


def wrap(text: str, max_chars: int) -> list[str]:
    """CJK 没有空格，libass 不会自动换行：超长时在最靠近中间的标点处断成两行（再长就按字数硬断）。"""
    if len(text) <= max_chars:
        return [text]
    mid = len(text) / 2
    cuts = [i + 1 for i, ch in enumerate(text[:-1]) if ch in "，。！？、；：,.!?;: …—"]
    cut = min(cuts, key=lambda i: abs(i - mid)) if cuts else 0
    if not cut or max(cut, len(text) - cut) > max_chars * 1.15:
        cut = int(round(mid))
    return [*wrap(text[:cut], max_chars), *wrap(text[cut:], max_chars)]


def karaoke(text: str, dur: float) -> str:
    """逐字高亮：\\kf 按字平均分配（标点不占时间）。"""
    chars = list(text.replace("【", "").replace("】", ""))
    n = max(1, sum(1 for ch in chars if not re.match(r"[\s，。！？、；：,.!?;:…—“”\"'（）()]", ch)))
    cs = max(1, int(dur * 100 * 0.92 / n))
    return "".join(ch if re.match(r"[\s，。！？、；：,.!?;:…—“”\"'（）()]", ch) else f"{{\\kf{cs}}}{esc(ch)}" for ch in chars)


def highlight(text: str, keywords: list[str], size: int) -> str:
    """关键词花字：变色 + 放大；【】里的内容也算关键词。"""
    t = esc(text)
    big = int(size * 1.25)
    t = re.sub(r"【(.+?)】", lambda m: f"{{\\c{HIGHLIGHT}\\fs{big}}}{m.group(1)}{{\\r}}", t)
    for k in sorted({k.strip() for k in keywords if k.strip()}, key=len, reverse=True):
        t = t.replace(esc(k), f"{{\\c{HIGHLIGHT}\\fs{big}}}{esc(k)}{{\\r}}")
    return t


def build_ass(timeline: list[tuple[float, float, list[dict], list[dict] | None]], width: int, height: int,
              opts: dict | None = None, font: str = "Noto Sans CJK SC") -> str:
    """timeline：(开始秒, 时长, 台词, 配音时间或 None)。opts：preset / color / size / position / karaoke /
    keywords / speaker / narration_style。"""
    o = opts or {}
    p = {**SUB_PRESETS["经典"], **SUB_PRESETS.get(o.get("preset") or "经典", {})}
    size = int(max(16, min(height // 18, width // 14)) * p["size"] * float(o.get("scale") or 1.0))
    max_chars = max(6, int((width - 80) / size))
    primary = _hex2ass(o.get("color") or "") or p["primary"]
    align = POSITIONS.get(o.get("position") or "bottom", 2)
    margin = int(height * (0.08 if align == 2 else 0.06))
    border = 3 if p.get("box") else 1
    kara = bool(o.get("karaoke"))
    secondary = KARAOKE_DIM if kara else primary
    styles = (f"Style: Sub,{font},{size},{primary},{secondary},{p['outline']},{p['back']},{p['bold']},0,0,0,100,100,0,0,"
              f"{border},{p['outline_w']},{p['shadow']},{align},40,40,{margin},1\n"
              f"Style: Narr,{font},{int(size * 0.92)},&H00E8E8E8,&H00E8E8E8,&H00000000,&H64000000,0,1,0,0,100,100,0,0,"
              f"1,{p['outline_w']},{p['shadow']},{align},40,40,{margin},1\n")
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n" + styles +
            "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    kws = [k for k in re.split(r"[,，、\s]+", o.get("keywords") or "") if k]
    rows = []
    for start, dur, lines, exact in timeline:
        for a, b, d in line_timing(start, dur, lines, exact):
            text = (d.get("line") or "").strip()
            if not text:
                continue
            style = "Narr" if d.get("speaker") == "旁白" else "Sub"
            parts = wrap(text.replace("【", "").replace("】", "") if kara else text, max_chars)
            if kara:
                body = "\\N".join(karaoke(x, (b - a) * len(x) / max(1, len(text))) for x in parts)
            elif o.get("highlight", True):
                body = "\\N".join(highlight(x, kws, size) for x in parts)
            else:
                body = "\\N".join(esc(x) for x in parts)
            if o.get("speaker") and d.get("speaker") and d["speaker"] != "旁白":
                body = f"{{\\fs{int(size * 0.7)}\\c&H00C8C8C8&}}{esc(d['speaker'])}：{{\\r}}" + body
            rows.append(f"Dialogue: 0,{media._ass_ts(a)},{media._ass_ts(max(a + 0.3, b))},{style},,0,0,0,,{body}")
    return head + "\n".join(rows) + "\n"


# ---------------- 转场 ----------------
TRANSITION_FX = {"叠化": ("fade", 0.5), "闪白": ("fadewhite", 0.3), "黑场": ("fadeblack", 0.6),
                 "划像": ("wipeleft", 0.5), "推移": ("slideleft", 0.5)}


def transition_plan(shots: list[dict], durs: list[float]) -> list[tuple[str, float]]:
    """第 i 项是第 i 个片段与上一个之间的转场（第 0 项恒为硬切）；转场时长不超过两侧片段的 1/3。"""
    out = [("", 0.0)]
    for i in range(1, len(shots)):
        fx = TRANSITION_FX.get(shots[i].get("transition") or "")
        if not fx:
            out.append(("", 0.0))
            continue
        d = min(fx[1], durs[i - 1] / 3, durs[i] / 3)
        out.append((fx[0], round(d, 3)) if d >= 0.1 else ("", 0.0))
    return out


def starts_with(durs: list[float], plan: list[tuple[str, float]], offset: float = 0.0) -> list[float]:
    """有转场重叠时每个片段在成片里的开始时间（字幕按这个对齐）。"""
    out, t = [], offset
    for i, d in enumerate(durs):
        t -= plan[i][1]
        out.append(round(t, 3))
        t += d
    return out


async def join(clips: list[Path], durs: list[float], plan: list[tuple[str, float]], out: Path) -> Path:
    """按转场计划拼接：硬切用 concat，转场用 xfade + acrossfade；片段需同分辨率/帧率/采样率（assemble 已归一化）。"""
    if not any(fx for fx, _ in plan):
        lst = out.parent / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in clips), encoding="utf-8")
        await media._run("ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out))
        return out
    args = ["ffmpeg", "-y", "-v", "error"]
    for c in clips:
        args += ["-i", str(c)]
    fc, cur_v, cur_a, cur_d = [], "[0:v]", "[0:a]", durs[0]
    for i in range(1, len(clips)):
        fx, d = plan[i]
        v, a = f"[v{i}]", f"[a{i}]"
        if fx:
            fc.append(f"{cur_v}[{i}:v]xfade=transition={fx}:duration={d}:offset={max(0.0, cur_d - d):.3f}{v}")
            fc.append(f"{cur_a}[{i}:a]acrossfade=d={d}:c1=tri:c2=tri{a}")
            cur_d += durs[i] - d
        else:
            fc.append(f"{cur_v}{cur_a}[{i}:v][{i}:a]concat=n=2:v=1:a=1{v}{a}")
            cur_d += durs[i]
        cur_v, cur_a = v, a
    args += ["-filter_complex", ";".join(fc), "-map", cur_v, "-map", cur_a, "-c:v", "libx264", "-preset", "medium",
             "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(out)]
    await media._run(*args)
    return out


# ---------------- 片头 / 集数 / 片尾卡、封面 ----------------
_FONT_CACHE: dict[bool, str] = {}


def font_path(bold: bool = True) -> str:
    """找一个能显示简体中文的字体文件：无衬线 > 衬线，SC/CJK > 其他，粗体优先（bold=True 时）。"""
    if bold in _FONT_CACHE:
        return _FONT_CACHE[bold]
    try:
        out = subprocess.run(["fc-list", ":lang=zh", "file", "family", "style"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:  # noqa: BLE001
        out = ""
    best, best_score = "", -1e9
    for row in out.splitlines():
        path = row.split(":", 1)[0].strip()
        meta = row.split(":", 1)[-1].lower()
        fname = Path(path).name.lower()
        if not Path(path).is_file():
            continue
        sc = 0
        sc += 40 if "sans" in meta else 0
        sc -= 30 if "serif" in meta and "sans" not in meta else 0
        sc += 30 if (" sc" in meta or "sc-" in fname or "cjk" in fname) else 0
        sc -= 20 if any(k in meta for k in (" tc", " hk", " jp", " kr")) and "cjk" not in fname else 0
        strong = any(k in meta for k in ("bold", "black", "heavy"))
        sc += (25 if strong else 0) if bold else (25 if "regular" in meta and not strong else 0)
        sc -= 15 if any(k in meta for k in ("thin", "light")) else 0
        if sc > best_score:
            best, best_score = path, sc
    _FONT_CACHE[bold] = best
    return best


def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    fp = font_path(bold)
    try:
        return ImageFont.truetype(fp, size) if fp else ImageFont.load_default()
    except OSError:
        return ImageFont.load_default()


def _center_text(draw, box_w: int, y: int, text: str, font, fill, stroke: int = 0, stroke_fill=(0, 0, 0)):
    w = draw.textlength(text, font=font)
    draw.text(((box_w - w) / 2, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)


def card_image(out: Path, width: int, height: int, title: str, subtitle: str = "", bg: Path | None = None,
               dark: float = 0.55) -> Path:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    if bg and bg.is_file():
        im = Image.open(bg).convert("RGB")
        im = _cover_crop(im, width, height).filter(ImageFilter.GaussianBlur(max(2, width // 200)))
        im = ImageEnhance.Brightness(im).enhance(1 - dark)
    else:
        im = Image.new("RGB", (width, height), (12, 12, 16))
    d = ImageDraw.Draw(im)
    big = _font(max(28, int(min(width, height) * 0.11)))
    small = _font(max(16, int(min(width, height) * 0.045)), bold=False)
    lines = [ln for ln in title.split("\n") if ln.strip()] or [" "]
    lh = int(big.size * 1.25)
    y = (height - lh * len(lines) - (int(small.size * 2) if subtitle else 0)) // 2
    for ln in lines:
        _center_text(d, width, y, ln, big, (255, 255, 255), stroke=max(1, big.size // 18))
        y += lh
    if subtitle:
        _center_text(d, width, y + int(small.size * 0.6), subtitle, small, (230, 200, 120))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out


def _cover_crop(im, w: int, h: int):
    from PIL import Image
    k = max(w / im.width, h / im.height)
    im = im.resize((int(im.width * k + 0.5), int(im.height * k + 0.5)), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


async def card_clip(image: Path, out: Path, dur: float, width: int, height: int) -> Path:
    """静态卡片 → 带淡入淡出的片段（含静音音轨，便于和正片拼接）。"""
    fade = min(0.5, dur / 4)
    vf = (f"scale={width}:{height},fps=24,format=yuv420p,fade=t=in:st=0:d={fade:.2f},"
          f"fade=t=out:st={dur - fade:.2f}:d={fade:.2f}")
    await media._run("ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(image), "-f", "lavfi", "-i",
                     "anullsrc=r=48000:cl=stereo", "-t", f"{dur:.2f}", "-vf", vf, "-c:v", "libx264", "-preset", "medium",
                     "-crf", "18", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", str(out))
    return out


def pick_cover_shot(shots: list[dict]) -> dict | None:
    """封面选帧：有分镜图、有人物、近景/中景优先，审片分高的优先，剧情靠前 1/3 的「爆点」优先。"""
    pref = {"特写": 3, "近景": 4, "中景": 3, "全景": 1, "远景": 0}
    cands = [s for s in shots if s.get("keyframe")]
    if not cands:
        return None
    n = len(shots)

    def score(s: dict) -> float:
        rv = (s.get("review") or {}).get("score")
        return (pref.get(s.get("shot_size") or "", 1) + (2 if s.get("character_ids") else 0)
                + (rv / 3 if isinstance(rv, (int, float)) else 1.5) + (1 if s.get("sort", 0) < n / 3 else 0)
                + (0.5 if (s.get("dialogue") or []) else 0))
    return max(cands, key=score)


def cover_image(src: Path, out: Path, width: int, height: int, title: str, subtitle: str = "") -> Path:
    """封面：高光帧铺满 + 底部渐变压暗 + 大标题（粗描边）+ 副标题。"""
    from PIL import Image, ImageDraw
    im = _cover_crop(Image.open(src).convert("RGB"), width, height)
    grad = Image.new("L", (1, height), 0)
    for y in range(height):
        grad.putpixel((0, y), int(220 * max(0.0, (y / height - 0.45) / 0.55) ** 1.4))
    shade = Image.new("RGB", (width, height), (0, 0, 0))
    im = Image.composite(shade, im, grad.resize((width, height)))
    d = ImageDraw.Draw(im)
    big = _font(max(32, int(width * (0.13 if height > width else 0.08))))
    small = _font(max(18, int(big.size * 0.42)))
    lines = [ln for ln in (title or " ").split("\n") if ln.strip()]
    y = int(height * 0.95) - int(big.size * 1.2) * len(lines) - (int(small.size * 1.6) if subtitle else 0)
    for ln in lines:
        _center_text(d, width, y, ln, big, (255, 230, 80), stroke=max(2, big.size // 12))
        y += int(big.size * 1.2)
    if subtitle:
        _center_text(d, width, y + 4, subtitle, small, (255, 255, 255), stroke=max(1, small.size // 14))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, quality=92)
    return out


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))
