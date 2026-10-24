"""自动质检（在 FaceFusion 的 Python 环境里运行，只用 CPU，不和 ComfyUI 抢显存）。

用法：python qa_check.py job.json  →  最后一行输出 JSON 结果
job = {"target": 图片/视频绝对路径, "reference": 参考图（视频用分镜图，用来区分「本来就有的文字」）,
       "faces": [{"name": 角色名, "path": 脸部特写}], "check_text": bool, "check_faces": bool,
       "models_dir": FaceFusion 的 .assets/models, "face_threshold": 0.3, "frames": 5}
检查项：
- text：画面里出现了参考图里没有的文字（视频模型偶尔会自己烧字幕）。需要 rapidocr。
- face：画面中的人脸与角色脸部特写的 ArcFace 相似度过低（脸崩 / 串脸）。用 FaceFusion 自带的 yoloface + arcface 模型。
另：job = {"layout": [图a, 图b], "models_dir", "identity"} → 只输出两张图的人脸布局对比（接续镜头用）。
"""
from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

VIDEO_SUFFIX = {".mp4", ".webm", ".mov", ".mkv"}
ARCFACE_112_V2 = np.array([[0.34191607, 0.46157411], [0.65653393, 0.45983393], [0.50022500, 0.64050536],
                           [0.37097589, 0.82469196], [0.63151696, 0.82325089]], dtype=np.float32)


# ---------------- 取帧 ----------------
def read_frames(path: Path, count: int) -> list[np.ndarray]:
    if path.suffix.lower() not in VIDEO_SUFFIX:
        img = cv2.imread(str(path))
        return [img] if img is not None else []
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frames = []
    if total > 0:
        for f in np.linspace(0.12, 0.95, count):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * f))
            ok, img = cap.read()
            if ok:
                frames.append(img)
    cap.release()
    return frames


# ---------------- 文字 ----------------
_ocr = None


def ocr_engine():
    global _ocr
    if _ocr is None:
        try:
            from rapidocr import RapidOCR  # rapidocr >= 2
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR  # 老版本
        _ocr = RapidOCR()
    return _ocr


def ocr(img: np.ndarray) -> list[tuple[str, float, float, float]]:
    """返回 [(文字, 置信度, 文字框中心的纵向位置 0~1, 横向位置 0~1)]"""
    out = ocr_engine()(img)
    items = []
    if isinstance(out, tuple):            # rapidocr_onnxruntime: (result, elapse)
        for box, text, score in out[0] or []:
            items.append((box, text, score))
    elif getattr(out, "txts", None):      # rapidocr >= 2: RapidOCROutput
        items = list(zip(out.boxes, out.txts, out.scores))
    h, w = img.shape[:2]
    res = []
    for box, text, score in items:
        pts = np.asarray(box).reshape(-1, 2)
        res.append((str(text), float(score), float(np.mean(pts[:, 1])) / h, float(np.mean(pts[:, 0])) / w))
    return res


def norm(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text))


def is_known(text: str, known: list[str]) -> bool:
    return any(text in k or k in text or difflib.SequenceMatcher(None, text, k).ratio() >= 0.5 for k in known)


def is_line(text: str, lines: list[str]) -> bool:
    """OCR 文字是不是台词 / 旁白（视频模型把台词烧成了字幕）。"""
    return any(len(text) >= 2 and (text in ln or (len(ln) >= 2 and ln in text)
                                   or difflib.SequenceMatcher(None, text, ln).ratio() >= 0.6) for ln in lines)


def check_text(frames: list[np.ndarray], reference: np.ndarray | None, lines: list[str] = ()) -> dict:
    """台词字幕（和本镜台词/旁白对得上）→ 一定判不合格；其他新出现的文字（招牌、衣服 logo、POLICE…）
    只有像字幕那样在画面下方居中、连续出现在 2 帧以上才判不合格，否则只记为提示。"""
    known = [norm(t) for t, s, *_ in (ocr(reference) if reference is not None else []) if s >= 0.5]
    known = [k for k in known if k]
    lines = [x for x in (norm(ln) for ln in lines) if x]
    hits, subs, caption_frames = [], set(), 0
    for img in frames:
        new = [(norm(t), y, x) for t, s, y, x in ocr(img) if s >= 0.6 and len(norm(t)) >= 2 and not is_known(norm(t), known)]
        if not new:
            continue
        hits.append([t for t, *_ in new])
        subs.update(t for t, *_ in new if is_line(t, lines))
        caption_frames += any(y > 0.7 and 0.25 < x < 0.75 and re.search(r"[\u4e00-\u9fff]", t) for t, y, x in new)
    if len(frames) == 1:
        flagged = bool(hits)
    else:
        flagged = bool(subs) or caption_frames >= 2
    return {"flagged": bool(flagged), "subtitle": sorted(subs)[:8], "texts": sorted({t for h in hits for t in h})[:8]}


# ---------------- 人脸 ----------------
class Faces:
    def __init__(self, models_dir: Path) -> None:
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        cpu = ["CPUExecutionProvider"]
        self.det = ort.InferenceSession(str(models_dir / "yoloface_8n.onnx"), opts, providers=cpu)
        self.rec = ort.InferenceSession(str(models_dir / "arcface_w600k_r50.onnx"), opts, providers=cpu)

    def detect(self, img: np.ndarray, score: float = 0.5) -> list[tuple[np.ndarray, np.ndarray]]:
        """yoloface_8n（与 FaceFusion 相同的预处理）→ [(框 x1y1x2y2, 5 点关键点)]"""
        h, w = img.shape[:2]
        scale = min(640 / h, 640 / w, 1.0)
        small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img
        canvas = np.zeros((640, 640, 3), dtype=np.float32)
        canvas[:small.shape[0], :small.shape[1]] = small
        x = (canvas.transpose(2, 0, 1)[None] / 255.0).astype(np.float32)
        det = np.squeeze(self.det.run(None, {"input": x})[0]).T
        boxes_raw, scores, marks = det[:, :4], det[:, 4], det[:, 5:]
        keep = np.where(scores > score)[0]
        if not len(keep):
            return []
        r = 1 / scale
        boxes = np.stack([(boxes_raw[keep, 0] - boxes_raw[keep, 2] / 2) * r, (boxes_raw[keep, 1] - boxes_raw[keep, 3] / 2) * r,
                          (boxes_raw[keep, 0] + boxes_raw[keep, 2] / 2) * r, (boxes_raw[keep, 1] + boxes_raw[keep, 3] / 2) * r], 1)
        idx = cv2.dnn.NMSBoxes([[float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])] for b in boxes],
                               scores[keep].tolist(), score, 0.4)
        out = []
        for i in np.array(idx).reshape(-1):
            lm = marks[keep][i].reshape(-1, 3)[:, :2] * r
            out.append((boxes[i], lm.astype(np.float32)))
        return out

    def embed(self, img: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
        matrix = cv2.estimateAffinePartial2D(landmarks, ARCFACE_112_V2 * 112, method=cv2.RANSAC, ransacReprojThreshold=100)[0]
        crop = cv2.warpAffine(img, matrix, (112, 112), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_AREA)
        crop = (crop / 127.5 - 1)[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32)
        e = self.rec.run(None, {"input": crop})[0].ravel()
        return e / (np.linalg.norm(e) + 1e-9)

    def largest(self, img: np.ndarray) -> np.ndarray | None:
        faces = self.detect(img, 0.3)
        if not faces:
            return None
        box, lm = max(faces, key=lambda f: (f[0][2] - f[0][0]) * (f[0][3] - f[0][1]))
        return self.embed(img, lm)


def check_faces(frames: list[np.ndarray], refs: list[dict], models_dir: Path, threshold: float) -> dict:
    fx = Faces(models_dir)
    ref_emb = {}
    for r in refs:
        img = cv2.imread(r["path"])
        e = fx.largest(img) if img is not None else None
        if e is not None:
            ref_emb[r["name"]] = e
    per_char: dict[str, list[float]] = {n: [] for n in ref_emb}
    best_any: list[float] = []
    frames_with_faces = 0
    small = 0
    for img in frames:
        # 太小的脸（远景、480p 中景）ArcFace 不可靠，不参与判断（实测 90 像素的清晰正脸和分镜图相似度也只有 0.3~0.4）
        min_side = max(64, 0.12 * img.shape[0])
        found = fx.detect(img)
        sides = [float(min(box[2] - box[0], box[3] - box[1])) for box, lm in found]
        small = max([small] + [s for s in sides if s < min_side])
        embs = [fx.embed(img, lm) for (box, lm), s in zip(found, sides) if s >= min_side]
        if not embs or not ref_emb or len(embs) > len(ref_emb):   # 脸比角色多（照片/海报/路人）时无法对应，跳过
            continue
        frames_with_faces += 1
        sims = {n: [float(np.dot(ref, e)) for e in embs] for n, ref in ref_emb.items()}
        best_any.append(max(max(v) for v in sims.values()))
        if len(embs) == len(ref_emb):          # 人都露脸了才逐个判；有人背对镜头时只做整体判断
            for n, v in sims.items():
                per_char[n].append(max(v))
    scores = {n: round(float(np.percentile(v, 75)), 3) for n, v in per_char.items() if v}
    low = {n: s for n, s in scores.items() if s < threshold}
    # 多人镜头里只露出部分人脸（过肩、背影、虚化背景）时无法对应到具体角色，不判（虚化的脸相似度天然偏低，误报多）
    return {"scores": scores, "low": low, "frames_with_faces": frames_with_faces, "refs": list(ref_emb), "small_face": int(small)}


def face_layout(a: Path, b: Path, models_dir: Path, identity: bool) -> dict:
    """两张图的人脸布局（位置 cx、高度占比 s，按大小排序，忽略背景小脸）+ 两两 ArcFace 相似度（写实画风才算）。
    用于判断「上一镜尾帧」和「本镜分镜图」是不是同一构图（接续镜头）。"""
    fx = Faces(models_dir)
    out: dict = {}
    embs: dict[str, list] = {}
    for key, path in (("a", a), ("b", b)):
        img = cv2.imread(str(path))
        if img is None:
            raise RuntimeError(f"读不出图片：{path}")
        h, w = img.shape[:2]
        found = [(box, lm, float((box[3] - box[1]) / h)) for box, lm in fx.detect(img)]
        top = max((f[2] for f in found), default=0.0)
        found = sorted([f for f in found if f[2] >= max(0.04, 0.4 * top)], key=lambda f: -f[2])
        out[key] = [{"cx": round(float((box[0] + box[2]) / 2 / w), 3), "s": round(s, 3)} for box, lm, s in found]
        embs[key] = [fx.embed(img, lm) for box, lm, s in found] if identity else []
    out["sims"] = [[round(float(np.dot(x, y)), 3) for y in embs["b"]] for x in embs["a"]]
    return out


# ---------------- 运镜（光流） ----------------
def detect_cuts(path: Path) -> list[float]:
    """硬切检测：相邻帧颜色直方图突然对不上（且远大于平时的帧间变化）= 中途切到了别的画面。
    实测：H3 生成 21:9 公路远景时，2 秒处突然切到车内司机特写。返回切点秒数。"""
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    hists, diffs, prev = [], [], None
    while True:
        ok, img = cap.read()
        if not ok:
            break
        small = cv2.resize(img, (96, 54))
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        h = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
        cv2.normalize(h, h)
        g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            hists.append(cv2.compareHist(hists_prev, h, cv2.HISTCMP_CORREL))
            diffs.append(float(np.abs(g - prev).mean()))
        prev, hists_prev = g, h
    cap.release()
    if len(diffs) < 6:
        return []
    med = float(np.median(diffs)) + 1.0
    cuts = []
    for i, (c, d) in enumerate(zip(hists, diffs)):
        if (i + 1) / fps <= 0.2:
            continue
        near = max(diffs[i - 1] if i else 0.0, diffs[i + 1] if i + 1 < len(diffs) else 0.0)
        # 颜色突变；或色调相近但单帧画面突变（实测 21:9 公路 → 车内：帧差 61，平时 1.5，直方图相关 0.76）
        if (d > max(18.0, 6 * med) and c < 0.7) or (d > max(30.0, 12 * med) and d > 4 * near):
            cuts.append(round((i + 1) / fps, 2))
    return cuts


def camera_motion(path: Path) -> dict:
    """逐帧 LK 光流 + 相似变换，累计整段的缩放和平移（相对画面尺寸）。实测比视觉大模型看抽帧网格可靠得多。"""
    cap = cv2.VideoCapture(str(path))
    ok, prev = cap.read()
    if not ok:
        return {}
    h, w = prev.shape[:2]
    pg = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    scale, tx, ty, i = 1.0, 0.0, 0.0, 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if i % 2:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        p0 = cv2.goodFeaturesToTrack(pg, 400, 0.01, 8)
        if p0 is not None and len(p0) > 20:
            p1, st, _ = cv2.calcOpticalFlowPyrLK(pg, g, p0, None)
            m, _ = cv2.estimateAffinePartial2D(p0[st == 1], p1[st == 1], method=cv2.RANSAC)
            if m is not None:
                scale *= float(np.hypot(m[0, 0], m[1, 0]))
                tx += float(m[0, 2] + (m[0, 0] - 1) * w / 2 + m[0, 1] * h / 2) / w
                ty += float(m[1, 2] + m[1, 0] * w / 2 + (m[1, 1] - 1) * h / 2) / h
        pg = g
    return {"zoom": round(scale - 1, 3), "tx": round(tx, 3), "ty": round(ty, 3)}


def camera_mismatch(move: str, direction: str, m: dict) -> str:
    """只核对能可靠判断的运镜；返回问题描述，没问题返回空串。"""
    if not m:
        return ""
    z, tx, ty = m["zoom"], m["tx"], m["ty"]
    got = f"检测到缩放 {z:+.0%}、水平 {tx:+.0%}、垂直 {ty:+.0%}"
    if move == "固定" and max(abs(z), abs(tx), abs(ty)) > 0.12:
        return f"要求固定机位，但镜头有明显运动（{got}）"
    if move == "推镜" and z < 0.05:
        return f"要求推镜，但画面没有变近（{got}）"
    if move == "拉镜" and z > -0.05:
        return f"要求拉镜，但画面没有变远（{got}）"
    if move in ("摇镜", "移镜") and direction in ("", "左", "右"):
        sign = 1 if direction == "左" else -1          # 镜头向右 → 画面内容向左（tx<0）
        if tx * sign < 0.03 and abs(ty) < 0.08:
            return f"要求{'向' + direction if direction else '向右'}{move[0]}，但画面没有相应的横向运动（{got}）"
    return ""


def main() -> None:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if job.get("layout"):
        a, b = job["layout"]
        print(json.dumps(face_layout(Path(a), Path(b), Path(job["models_dir"]), bool(job.get("identity"))), ensure_ascii=False))
        return
    target = Path(job["target"])
    frames = read_frames(target, int(job.get("frames") or 5))
    result: dict = {"ok": True, "issues": [], "warnings": []}
    if not frames:
        result.update(ok=False, issues=[{"type": "broken", "detail": "读不出画面，文件可能损坏"}])
        print(json.dumps(result, ensure_ascii=False))
        return
    if job.get("check_text"):
        try:
            ref = cv2.imread(job["reference"]) if job.get("reference") else None
            t = check_text(frames, ref, job.get("lines") or [])
            result["text"] = t
            if t["flagged"]:
                what = "台词被烧成了字幕" if t["subtitle"] else "画面出现多余文字/字幕"
                result["issues"].append({"type": "text", "subtitle": bool(t["subtitle"]),
                                         "detail": f"{what}：" + "、".join((t["subtitle"] or t["texts"])[:3])})
            elif t["texts"]:
                result["warnings"].append("画面里有文字（场景/服装上的字，未判为字幕）：" + "、".join(t["texts"][:3]))
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"文字检测不可用：{exc}")
    if job.get("check_faces") and job.get("faces"):
        try:
            f = check_faces(frames, job["faces"], Path(job["models_dir"]), float(job.get("face_threshold") or 0.25))
            result["faces"] = f
            for name, s in f["low"].items():
                result["issues"].append({"type": "face", "detail": f"「{name}」的脸不像（相似度 {s:.2f}）"})
            if not f["frames_with_faces"] and f.get("small_face"):
                result["warnings"].append(f"人脸太小（约 {f['small_face']} 像素），没法自动判断像不像、有没有变形，请放大看一下；"
                                          "脸部要求高的镜头建议用「成品」画质")
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"人脸检测不可用：{exc}")
    if target.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv"):
        try:
            cuts = detect_cuts(target)
            result["cuts"] = cuts
            if cuts:
                result["issues"].append({"type": "cut", "detail": "画面在 " + "、".join(f"{c:g}" for c in cuts[:3])
                                         + " 秒处突然切换到别的画面（不是一镜到底）"})
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"切镜检测不可用：{exc}")
    if job.get("camera") and target.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv"):
        try:
            m = camera_motion(target)
            result["camera"] = m
            why = camera_mismatch(job["camera"], job.get("camera_dir") or "", m)
            if why:
                result["warnings"].append("运镜可能不符：" + why)
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"运镜检测不可用：{exc}")
    result["ok"] = not result["issues"]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
