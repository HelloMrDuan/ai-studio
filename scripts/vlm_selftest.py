"""视觉审片自检：生成一张已知内容的图（两个色块 + 文字），问视觉模型看到了什么。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from app import vlm  # noqa: E402


async def main() -> int:
    if not vlm.available():
        print("视觉模型未配置（LLM_MMPROJ / VLM_BASE_URL）")
        return 1
    im = Image.new("RGB", (640, 360), (240, 240, 240))
    d = ImageDraw.Draw(im)
    d.ellipse([60, 80, 260, 280], fill=(220, 30, 30))
    d.rectangle([380, 80, 580, 280], fill=(30, 60, 220))
    d.text((280, 20), "HELLO 123", fill=(0, 0, 0))
    r = await vlm.ask([im], "画面里有什么形状、分别什么颜色？有没有文字？按要求输出 JSON（issues 里用 artifact 类型描述你看到的东西）。")
    print(r)
    text = str(r)
    ok = ("红" in text or "red" in text.lower()) and ("蓝" in text or "blue" in text.lower())
    print("✓ 视觉模型工作正常" if ok else "⚠ 视觉模型回答不符合预期")
    return 0 if ok else 2


sys.exit(asyncio.run(main()))
