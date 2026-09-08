#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure V3 character identity cosine similarity with InsightFace.")
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
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

    app = FaceAnalysis(
        name="buffalo_l",
        root=str(args.insightface_root),
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    app.prepare(ctx_id=0, det_size=(640, 640))

    def largest_face(path: Path):
        image = cv2.imread(str(path))
        if image is None:
            raise SystemExit(f"cannot read image: {path}")
        faces = app.get(image)
        if not faces:
            raise SystemExit(f"no face detected: {path}")
        return max(
            faces,
            key=lambda face: (face.bbox[2] - face.bbox[0]) * (face.bbox[3] - face.bbox[1]),
        )

    reference_face = largest_face(args.reference)
    candidate_face = largest_face(args.candidate)
    score = float(np.dot(reference_face.normed_embedding, candidate_face.normed_embedding))

    payload = {
        "recognition_model": "buffalo_l/w600k_r50",
        "cosine_similarity": round(score, 6),
        "reference": str(args.reference),
        "candidate": str(args.candidate),
        "reference_face_bbox": [float(x) for x in reference_face.bbox.tolist()],
        "candidate_face_bbox": [float(x) for x in candidate_face.bbox.tolist()],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
