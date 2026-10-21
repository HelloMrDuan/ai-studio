"""音画核对用的语音识别：python asr.py <视频> [模型名]，输出 {"text": "..."}。需要 faster-whisper。"""
import json
import sys

from faster_whisper import WhisperModel

model = WhisperModel(sys.argv[2] if len(sys.argv) > 2 else "small", device="auto", compute_type="auto")
segs, _ = model.transcribe(sys.argv[1], language="zh", vad_filter=True, initial_prompt="以下是普通话的句子。")
print(json.dumps({"text": "".join(s.text for s in segs).strip()}, ensure_ascii=False))
