import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="studio-test-"))
os.environ.update({
    "BASE_DIR": str(TMP), "DATA_DIR": str(TMP / "data"), "COMFYUI_DIR": str(TMP / "ComfyUI"),
    "LLM_PROVIDER": "api", "COMFYUI_URL": "http://127.0.0.1:1", "FACEFUSION_DIR": str(TMP / "ff"),
    "FACEFUSION_PYTHON": str(TMP / "ff" / "python"),
})
