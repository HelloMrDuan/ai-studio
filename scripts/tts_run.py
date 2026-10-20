"""IndexTTS2 批量合成（在 tts 环境里运行）：python tts_run.py job.json
job = {"tts_dir": IndexTTS 仓库目录, "items": [{"text", "voice", "emo", "out"}]}"""
import json
import os
import sys
from pathlib import Path

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
os.chdir(job["tts_dir"])
sys.path.insert(0, job["tts_dir"])
from indextts.infer_v2 import IndexTTS2  # noqa: E402

tts = IndexTTS2(cfg_path="checkpoints/config.yaml", model_dir="checkpoints", use_fp16=True,
                use_cuda_kernel=False, use_deepspeed=False)
for i, it in enumerate(job["items"], 1):
    kw = {}
    if (it.get("emo") or "").strip():
        kw.update(use_emo_text=True, emo_text=it["emo"], emo_alpha=0.6)
    Path(it["out"]).parent.mkdir(parents=True, exist_ok=True)
    tts.infer(spk_audio_prompt=it["voice"], text=it["text"], output_path=it["out"], verbose=False, **kw)
    print(f"配音 {i}/{len(job['items'])} 完成", flush=True)
