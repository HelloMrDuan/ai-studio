"""视频超分（在 ComfyUI 的 Python 环境里运行：自带 torch + spandrel）。
逐帧流式：ffmpeg 解码 → Real-ESRGAN 等超分模型 → 缩放到目标尺寸 → ffmpeg 编码，显存/内存占用与视频长度无关，原音轨直接复制。
  python upscale.py --input in.mp4 --output out.mp4 --model realesr-animevideov3.pth --width 1080 --height 1920 [--tile 512]
"""
import argparse
import json
import subprocess
import sys


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate,nb_frames", "-show_entries", "format=duration",
                          "-of", "json", path], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    s = d["streams"][0]
    num, den = (s.get("r_frame_rate") or "24/1").split("/")
    fps = float(num) / float(den or 1)
    frames = int(s.get("nb_frames") or 0) or int(float(d["format"].get("duration") or 0) * fps)
    return int(s["width"]), int(s["height"]), fps, s.get("r_frame_rate") or "24", frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--tile", type=int, default=0)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--crf", type=int, default=16)
    a = ap.parse_args()

    import numpy as np
    import torch
    import torch.nn.functional as F
    from spandrel import ModelLoader

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    half = dev == "cuda"
    desc = ModelLoader().load_from_file(a.model)
    model = desc.model.eval().to(dev)
    if half:
        model = model.half()
    scale = int(getattr(desc, "scale", 4) or 4)
    w, h, fps, rate, total = probe(a.input)
    print(f"INFO 源 {w}x{h} {fps:.2f}fps {total} 帧，模型 x{scale} → {a.width}x{a.height}", flush=True)

    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", a.input, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{a.width}x{a.height}",
                            "-r", rate, "-i", "-", "-i", a.input, "-map", "0:v", "-map", "1:a?", "-c:v", "libx264",
                            "-preset", "medium", "-crf", str(a.crf), "-pix_fmt", "yuv420p", "-c:a", "copy", a.output],
                           stdin=subprocess.PIPE)

    def run(x):
        if not a.tile or (x.shape[2] <= a.tile and x.shape[3] <= a.tile):
            return model(x)
        t, pad = a.tile, 16
        out = torch.zeros((x.shape[0], 3, x.shape[2] * scale, x.shape[3] * scale), dtype=x.dtype, device=x.device)
        for y0 in range(0, x.shape[2], t):
            for x0 in range(0, x.shape[3], t):
                ys, xs = max(0, y0 - pad), max(0, x0 - pad)
                ye, xe = min(x.shape[2], y0 + t + pad), min(x.shape[3], x0 + t + pad)
                o = model(x[:, :, ys:ye, xs:xe])
                oy, ox = (y0 - ys) * scale, (x0 - xs) * scale
                hh, ww = (min(y0 + t, x.shape[2]) - y0) * scale, (min(x0 + t, x.shape[3]) - x0) * scale
                out[:, :, y0 * scale:y0 * scale + hh, x0 * scale:x0 * scale + ww] = o[:, :, oy:oy + hh, ox:ox + ww]
        return out

    size, done = w * h * 3, 0
    with torch.inference_mode():
        while True:
            frames = []
            for _ in range(a.batch):
                buf = dec.stdout.read(size)
                if len(buf) < size:
                    break
                frames.append(np.frombuffer(buf, np.uint8).reshape(h, w, 3))
            if not frames:
                break
            x = torch.from_numpy(np.stack(frames)).to(dev).permute(0, 3, 1, 2)
            x = (x.half() if half else x.float()) / 255.0
            y = run(x)
            if y.shape[2:] != (a.height, a.width):
                y = F.interpolate(y.float(), size=(a.height, a.width), mode="bicubic", antialias=True, align_corners=False)
            y = (y.clamp(0, 1) * 255).round().to(torch.uint8).permute(0, 2, 3, 1).cpu().numpy()
            enc.stdin.write(y.tobytes())
            done += len(frames)
            print(f"PROGRESS {done}/{max(total, done)}", flush=True)
    enc.stdin.close()
    dec.wait()
    if enc.wait() != 0:
        sys.exit("ffmpeg 编码失败")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
