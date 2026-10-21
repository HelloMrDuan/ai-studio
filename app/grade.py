"""P3-3 调色统一 / 去闪烁：按场次以基准镜头做 Lab 色彩匹配（Reinhard），叠加可选风格 LUT，生成 .cube 交给 ffmpeg lut3d；
时域去闪烁 deflicker；用相邻镜头平均 Lab 差（ΔE）量化统一程度。纯 Python 实现，不依赖 numpy。"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

from . import media

LUT_SIZE = 33
STYLE_LUTS = {"": "不用", "电影": "青橙电影感", "清新": "明亮清新", "暖调": "温暖怀旧", "冷调": "冷峻蓝调",
              "赛博": "赛博霓虹", "复古": "褪色胶片", "黑白": "黑白"}


# ---------------- sRGB ↔ Lab（D65） ----------------
def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gam(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _f(t: float) -> float:
    return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116


def _fi(t: float) -> float:
    return t ** 3 if t ** 3 > 216 / 24389 else (116 * t - 16) / (24389 / 27)


def rgb2lab(r: float, g: float, b: float) -> tuple[float, float, float]:
    r, g, b = _lin(r), _lin(g), _lin(b)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    fx, fy, fz = _f(x), _f(y), _f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def lab2rgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    fy = (L + 16) / 116
    x, y, z = _fi(fy + a / 500) * 0.95047, _fi(fy), _fi(fy - b / 200) * 1.08883
    r = 3.2406 * x - 1.5372 * y - 0.4986 * z
    g = -0.9689 * x + 1.8758 * y + 0.0415 * z
    bb = 0.0557 * x - 0.2040 * y + 1.0570 * z
    return _gam(r), _gam(g), _gam(bb)


# ---------------- 统计 & 变换 ----------------
Stats = tuple[tuple[float, float, float], tuple[float, float, float]]   # (均值, 标准差)


def lab_stats(pixels: list[tuple[int, int, int]]) -> Stats:
    labs = [rgb2lab(r / 255, g / 255, b / 255) for r, g, b in pixels]
    n = max(1, len(labs))
    mean = tuple(sum(p[i] for p in labs) / n for i in range(3))
    std = tuple(max(1e-3, math.sqrt(sum((p[i] - mean[i]) ** 2 for p in labs) / n)) for i in range(3))
    return mean, std  # type: ignore[return-value]


def delta_e(a: Stats, b: Stats) -> float:
    return math.sqrt(sum((a[0][i] - b[0][i]) ** 2 for i in range(3)))


def transfer(src: Stats, ref: Stats, strength: float = 0.7) -> Callable[[float, float, float], tuple[float, float, float]]:
    """Reinhard 色彩迁移（Lab 空间逐通道匹配均值与标准差），比例限制在 0.6~1.6 防止过度拉伸。"""
    (ms, ss), (mt, st) = src, ref
    k = [min(1.6, max(0.6, st[i] / ss[i])) for i in range(3)]

    def fn(r: float, g: float, b: float) -> tuple[float, float, float]:
        lab = rgb2lab(r, g, b)
        out = [(lab[i] - ms[i]) * k[i] + mt[i] for i in range(3)]
        mixed = [lab[i] + (out[i] - lab[i]) * strength for i in range(3)]
        return lab2rgb(*mixed)
    return fn


def _sat(r: float, g: float, b: float, s: float) -> tuple[float, float, float]:
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return y + (r - y) * s, y + (g - y) * s, y + (b - y) * s


def _curve(x: float, contrast: float = 1.0, lift: float = 0.0, gain: float = 1.0) -> float:
    x = (x - 0.5) * contrast + 0.5
    return min(1.0, max(0.0, lift + x * (gain - lift)))


def style_fn(name: str) -> Callable[[float, float, float], tuple[float, float, float]] | None:
    if not name:
        return None

    def f(r: float, g: float, b: float) -> tuple[float, float, float]:
        y = 0.2126 * r + 0.7152 * g + 0.0722 * b
        if name == "电影":     # 阴影偏青、高光偏橙，略加对比
            sh, hi = 1 - y, y
            r, g, b = r + 0.06 * hi - 0.04 * sh, g + 0.01 * hi, b - 0.05 * hi + 0.06 * sh
            r, g, b = (_curve(c, 1.08) for c in (r, g, b))
        elif name == "清新":   # 提亮、降对比、略青绿
            r, g, b = (_curve(c, 0.9, 0.06, 1.0) for c in (r, g + 0.02, b + 0.03))
            r, g, b = _sat(r, g, b, 0.92)
        elif name == "暖调":
            r, g, b = r * 1.06 + 0.02, g * 1.02 + 0.01, b * 0.9
        elif name == "冷调":
            r, g, b = r * 0.92, g * 0.98, b * 1.07 + 0.02
            r, g, b = _sat(r, g, b, 0.9)
        elif name == "赛博":   # 阴影洋红、高光青
            sh, hi = 1 - y, y
            r, g, b = r + 0.08 * sh, g - 0.03 * sh + 0.03 * hi, b + 0.1 * sh + 0.06 * hi
            r, g, b = _sat(*(_curve(c, 1.12) for c in (r, g, b)), 1.15)
        elif name == "复古":   # 黑位抬起、褪色、偏黄
            r, g, b = (_curve(c, 0.85, 0.08, 0.95) for c in (r * 1.03, g, b * 0.88))
            r, g, b = _sat(r, g, b, 0.75)
        elif name == "黑白":
            v = _curve(y, 1.1)
            r = g = b = v
        return tuple(min(1.0, max(0.0, c)) for c in (r, g, b))  # type: ignore[return-value]
    return f


def compose(*fns):
    fns = [f for f in fns if f]

    def fn(r: float, g: float, b: float) -> tuple[float, float, float]:
        for f in fns:
            r, g, b = f(r, g, b)
        return r, g, b
    return fn if fns else None


def write_cube(fn, path: Path, size: int = LUT_SIZE) -> Path:
    lines = [f'TITLE "ai-studio"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    d = size - 1
    for b in range(size):
        for g in range(size):
            for r in range(size):
                o = fn(r / d, g / d, b / d)
                lines.append(f"{o[0]:.5f} {o[1]:.5f} {o[2]:.5f}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


# ---------------- 抽帧统计 ----------------
async def clip_pixels(video: Path, work: Path, n: int = 3) -> list[tuple[int, int, int]]:
    from PIL import Image
    dur = (await media.probe(video))["duration"] or 1.0
    px: list[tuple[int, int, int]] = []
    work.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        f = work / f"{video.stem}_{i}.png"
        await media._run("ffmpeg", "-y", "-v", "error", "-ss", f"{dur * (i + 1) / (n + 1):.3f}", "-i", str(video),
                         "-frames:v", "1", "-vf", "scale=64:-2", str(f))
        if f.is_file():
            with Image.open(f) as im:
                px += list(im.convert("RGB").getdata())
    return px


def _apply(fn, px: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    out = []
    for r, g, b in px[::2]:
        o = fn(r / 255, g / 255, b / 255)
        out.append((int(o[0] * 255), int(o[1] * 255), int(o[2] * 255)))
    return out


def pick_base(stats: list[Stats]) -> int:
    """组内基准：离组平均色调最近的镜头。"""
    mean = tuple(sum(s[0][i] for s in stats) / len(stats) for i in range(3))
    return min(range(len(stats)), key=lambda k: sum((stats[k][0][i] - mean[i]) ** 2 for i in range(3)))


def adjacent_de(stats: list[Stats]) -> float:
    if len(stats) < 2:
        return 0.0
    return round(sum(delta_e(stats[i], stats[i + 1]) for i in range(len(stats) - 1)) / (len(stats) - 1), 2)


async def plan(clips: list[Path], shots: list[dict], work: Path, *, strength: float = 0.7, lut: str = "",
               base_shot: str = "", per_scene: bool = True, match: bool = True) -> dict:
    """返回 {"cubes": [每个片段的 .cube 或 None], "before": ΔE, "after": ΔE, "bases": {...}}。"""
    px = [await clip_pixels(c, work / "frames") for c in clips]
    stats = [lab_stats(p) if p else ((50.0, 0.0, 0.0), (20.0, 10.0, 10.0)) for p in px]
    groups: dict[str, list[int]] = {}
    for i, s in enumerate(shots):
        key = (s.get("scene") or s.get("location_id") or "_") if per_scene else "_"
        groups.setdefault(key, []).append(i)
    style = style_fn(lut)
    cubes: list[Path | None] = [None] * len(clips)
    after: list[Stats] = list(stats)
    bases = {}
    for key, idx in groups.items():
        ids = [shots[i]["id"] for i in idx]
        b = idx[ids.index(base_shot)] if base_shot in ids else idx[pick_base([stats[i] for i in idx])]
        bases[key] = shots[b]["sort"] + 1
        for i in idx:
            fn = compose(transfer(stats[i], stats[b], strength) if match and i != b and len(idx) > 1 else None, style)
            if fn is None:
                continue
            cubes[i] = write_cube(fn, work / f"grade_{i:04d}.cube")
            if px[i]:
                after[i] = lab_stats(_apply(fn, px[i]))
    return {"cubes": cubes, "before": adjacent_de(stats), "after": adjacent_de(after), "bases": bases}


def filters(cube: Path | None, deflicker: bool) -> str:
    parts = []
    if deflicker:
        parts.append("deflicker=mode=pm:size=5")
    if cube:
        parts.append(f"lut3d=file='{cube.as_posix()}':interp=tetrahedral")
    return ",".join(parts)
