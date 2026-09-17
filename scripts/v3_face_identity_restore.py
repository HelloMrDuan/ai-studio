#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore the adopted frontal face pixels after FaceFusion identity transfer."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--result-json", type=Path)
    parser.add_argument(
        "--insightface-root",
        type=Path,
        default=Path("/root/autodl-tmp/ai-studio/ComfyUI/models/insightface"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    import cv2
    import numpy as np
    from insightface.app import FaceAnalysis

    analyser = FaceAnalysis(
        name="buffalo_l",
        root=str(args.insightface_root),
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    analyser.prepare(ctx_id=0, det_size=(640, 640))
    source = cv2.imread(str(args.source), cv2.IMREAD_COLOR)
    target = cv2.imread(str(args.target), cv2.IMREAD_COLOR)
    if source is None or target is None:
        raise SystemExit("cannot read source or target image")

    def largest_face(image):
        faces = analyser.get(image)
        if not faces:
            raise SystemExit("no face detected")
        return max(
            faces,
            key=lambda face: float(
                (face.bbox[2] - face.bbox[0]) * (face.bbox[3] - face.bbox[1])
            ),
        )

    source_face = largest_face(source)
    target_scope = target[:, : target.shape[1] // 3] if target.shape[1] / target.shape[0] > 2.2 else target
    target_face = largest_face(target_scope)
    matrix, _ = cv2.estimateAffinePartial2D(
        np.asarray(source_face.kps, dtype=np.float32),
        np.asarray(target_face.kps, dtype=np.float32),
        method=cv2.LMEDS,
    )
    if matrix is None:
        raise SystemExit("cannot align adopted face to candidate")
    height, width = target.shape[:2]
    warped = cv2.warpAffine(
        source,
        matrix,
        (width, height),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REFLECT,
    )
    x1, y1, x2, y2 = [float(value) for value in source_face.bbox]
    face_width, face_height = x2 - x1, y2 - y1
    mask = np.zeros(source.shape[:2], dtype=np.uint8)
    center = (int((x1 + x2) / 2), int(y1 + face_height * 0.52))
    # Include the complete adopted face contour and hairline. The smaller mask
    # retained too much of the generated full-body face around the cheeks and
    # jaw, which was visible to users and capped Buffalo-L similarity near 0.88.
    axes = (int(face_width * 0.68), int(face_height * 0.69))
    cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
    warped_mask = cv2.warpAffine(mask, matrix, (width, height), flags=cv2.INTER_LINEAR)
    target_face_width = float(target_face.bbox[2] - target_face.bbox[0])
    warped_mask = cv2.GaussianBlur(
        warped_mask,
        (0, 0),
        sigmaX=max(3.0, target_face_width * 0.025),
    )
    alpha = warped_mask.astype(np.float32)[:, :, None] / 255.0
    restored = np.clip(
        warped.astype(np.float32) * alpha + target.astype(np.float32) * (1.0 - alpha),
        0,
        255,
    ).astype(np.uint8)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(args.output), restored, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
        raise SystemExit(f"cannot write restored image: {args.output}")
    payload = {
        "source_face_bbox": [float(value) for value in source_face.bbox.tolist()],
        "target_face_bbox": [float(value) for value in target_face.bbox.tolist()],
        "output": str(args.output),
    }
    if args.result_json:
        args.result_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
