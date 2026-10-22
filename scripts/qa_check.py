"""自动质检（在 FaceFusion 的 Python 环境里运行，只用 CPU，不和 ComfyUI 抢显存）。

用法：python qa_check.py job.json  →  最后一行输出 JSON 结果
job = {"target": 图片/视频绝对路径, "reference": 参考图（视频用分镜图，用来区分「本来就有的文字」）,
       "faces": [{"name": 角色名, "path": 脸部特写}], "check_text": bool, "check_faces": bool,
       "models_dir": FaceFusion 的 .assets/models, "face_threshold": 0.3, "frames": 5}
检查项：
- text：画面里出现了参考图里没有的文字（视频模型偶尔会自己烧字幕）。需要 rapidocr。
- face：画面中的人脸与角色脸部特写的 ArcFace 相似度过低（脸崩 / 串脸）。用 FaceFusion 自带的 yoloface + arcface 模型。
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
    for img in frames:
        # 太小的脸（远景、480p 中景）ArcFace 不可靠，不参与判断
        min_side = max(64, 0.12 * img.shape[0])
        embs = [fx.embed(img, lm) for box, lm in fx.detect(img) if min(box[2] - box[0], box[3] - box[1]) >= min_side]
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
    return {"scores": scores, "low": low, "frames_with_faces": frames_with_faces, "refs": list(ref_emb)}


def main() -> None:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
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
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"人脸检测不可用：{exc}")
    result["ok"] = not result["issues"]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
