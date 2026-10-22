"""光线锁定（qa_python 环境：opencv + numpy）。
实测：H3 图生视频即使提示词写明「保持首帧光线」，黄昏首帧 5 秒内也会逐渐变成白天（亮度 +60%）。
做法：逐帧把 Lab 的亮度均值/对比度、色偏拉回首帧（统计量做时间平滑，避免闪烁），原音轨直接复制。
用法：python light_lock.py job.json   job = {src, out, ffmpeg, strength, min_drift}
输出一行 JSON：{"applied": bool, "drift": 最大亮度漂移, "fixed": 修正后最大漂移}"""
import json
import subprocess
import sys

import cv2
import numpy as np


def stats(frame):
    lab = cv2.cvtColor(cv2.resize(frame, (160, int(160 * frame.shape[0] / frame.shape[1]))), cv2.COLOR_BGR2LAB).astype(np.float32)
    return np.concatenate([lab.reshape(-1, 3).mean(0), lab.reshape(-1, 3).std(0)])


def smooth(a, k=9):
    pad = np.pad(a, ((k // 2, k // 2), (0, 0)), mode="edge")
    ker = np.ones(k) / k
    return np.stack([np.convolve(pad[:, i], ker, mode="valid") for i in range(a.shape[1])], 1)


def cdf_lut(src, ref):
    """把 src 通道的分布映射到 ref 的分布（直方图匹配），返回 256 项查找表。"""
    hs = np.bincount(src.ravel(), minlength=256).astype(np.float64)
    hr = np.bincount(ref.ravel(), minlength=256).astype(np.float64)
    cs, cr = np.cumsum(hs) / hs.sum(), np.cumsum(hr) / hr.sum()
    return np.interp(cs, cr, np.arange(256))


def small_lab(f):
    return cv2.cvtColor(cv2.resize(f, (192, int(192 * f.shape[0] / f.shape[1]))), cv2.COLOR_BGR2LAB)


def main():
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    cap = cv2.VideoCapture(job["src"])
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    if len(frames) < 8:
        print(json.dumps({"applied": False, "reason": "帧太少"}))
        return
    st = smooth(np.stack([stats(f) for f in frames]))
    ref = np.stack([stats(f) for f in frames[:3]]).mean(0)
    drift = float(np.abs(st[:, 0] - ref[0]).max())
    k = float(job.get("strength", 1.0))
    if drift < float(job.get("min_drift", 8)):
        print(json.dumps({"applied": False, "drift": round(drift, 1)}))
        return
    h, w = frames[0].shape[:2]
    ff = subprocess.Popen([job.get("ffmpeg") or "ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-i", job["src"], "-map", "0:v", "-map", "1:a?",
                           "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy",
                           "-shortest", job["out"]], stdin=subprocess.PIPE)
    after = []
    luts = None
    if job.get("mode", "hist") == "hist":           # 逐通道直方图匹配：能把变蓝的天空拉回暖色，比只拉均值更像首帧
        r = small_lab(frames[0])
        raw = np.stack([np.stack([cdf_lut(small_lab(f)[..., c], r[..., c]) for c in range(3)]) for f in frames])
        win = 9
        pad = np.pad(raw, ((win // 2, win // 2), (0, 0), (0, 0)), mode="edge")
        luts = np.stack([pad[i:i + win].mean(0) for i in range(len(frames))])
        ident = np.arange(256)[None, :]
        luts = np.clip(ident + (luts - ident) * k, 0, 255).astype(np.uint8)
    for n, (f, s) in enumerate(zip(frames, st)):
        if luts is not None:
            lab = cv2.cvtColor(f, cv2.COLOR_BGR2LAB)
            for c in range(3):
                lab[..., c] = luts[n, c][lab[..., c]]
            out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            after.append(stats(out)[0])
            ff.stdin.write(out.tobytes())
            continue
        lab = cv2.cvtColor(f, cv2.COLOR_BGR2LAB).astype(np.float32)
        ratio = np.clip(ref[3] / max(s[3], 1e-3), 0.5, 2.0) ** k
        shift = np.clip(ref[0] - s[0], -100, 100) * k
        lab[..., 0] = (lab[..., 0] - s[0]) * ratio + s[0] + shift
        for c in (1, 2):
            lab[..., c] += np.clip(ref[c] - s[c], -15, 15) * k
        out = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
        after.append(stats(out)[0])
        ff.stdin.write(out.tobytes())
    ff.stdin.close()
    if ff.wait() != 0:
        raise SystemExit("ffmpeg 编码失败")
    print(json.dumps({"applied": True, "drift": round(drift, 1),
                      "fixed": round(float(np.abs(smooth(np.array(after)[:, None])[:, 0] - ref[0]).max()), 1)}))


if __name__ == "__main__":
    main()
