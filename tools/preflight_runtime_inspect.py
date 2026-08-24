"""Import-only runtime binding inspection for the V2.39.6.3 preflight.

The copied Windows snapshot does not carry every Linux runtime dependency.
This inspector supplies only the two packaging shims required during import;
it never calls a route, starts a task, or touches a GPU service.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "diagnostics" / "preflight_runtime_inspect"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_import_shims() -> None:
    import pydantic

    class BaseSettings(pydantic.BaseModel):
        def __init__(self, **values):
            for name in self.__class__.model_fields:
                env_name = name.upper()
                if name not in values and env_name in os.environ:
                    values[name] = os.environ[env_name]
            super().__init__(**values)

    settings_module = types.ModuleType("pydantic_settings")
    settings_module.BaseSettings = BaseSettings
    settings_module.SettingsConfigDict = lambda **values: values
    sys.modules.setdefault("pydantic_settings", settings_module)

    def parse_options_header(value):
        return value, {}

    multipart_root = types.ModuleType("multipart")
    multipart_root.__version__ = "0.0.20"
    multipart_sub = types.ModuleType("multipart.multipart")
    multipart_sub.parse_options_header = parse_options_header
    sys.modules.setdefault("multipart", multipart_root)
    sys.modules.setdefault("multipart.multipart", multipart_sub)

    python_multipart = types.ModuleType("python_multipart")
    python_multipart.__version__ = "0.0.20"
    python_multipart_sub = types.ModuleType("python_multipart.multipart")
    python_multipart_sub.parse_options_header = parse_options_header
    sys.modules.setdefault("python_multipart", python_multipart)
    sys.modules.setdefault("python_multipart.multipart", python_multipart_sub)

    # Pillow is used only inside media execution paths.  Import inspection does
    # not invoke those paths, so empty module objects are sufficient here.
    pil = types.ModuleType("PIL")
    for name in ("Image", "ImageDraw", "ImageOps", "ImageStat"):
        module = types.ModuleType(f"PIL.{name}")
        setattr(pil, name, module)
        sys.modules.setdefault(f"PIL.{name}", module)
    sys.modules.setdefault("PIL", pil)


def _configure_isolated_paths() -> None:
    values = {
        "DATA_DIR": SCRATCH / "data",
        "DIRECTOR_PROJECTS_DIR": SCRATCH / "projects",
        "LLM_SELECTION_PATH": SCRATCH / "llm_selection.json",
        "LLM_REGISTRY_PATH": ROOT / "config" / "llm_models.json",
        "DIRECTOR_MANIFEST_PATH": ROOT / "config" / "director_skills.json",
        "DIRECTOR_SKILL_ROOT": ROOT,
        "COMFYUI_WORKFLOW_PATH": ROOT / "workflows" / "sdxl_api.json",
    }
    for key, value in values.items():
        os.environ[key] = str(value)


def _source_line(value) -> int | None:
    try:
        return inspect.getsourcelines(value)[1]
    except (OSError, TypeError):
        return None


def main() -> None:
    _configure_isolated_paths()
    _install_import_shims()

    import app.main as runtime

    symbols = [
        "_studio_v2371_rebuild_stage04",
        "_studio_v2371_generate_batch",
        "_studio_v2371_validate_rows",
        "_studio_v2371_audit_batch",
        "_studio_stage04_scene_source",
        "_studio_v2372_generate_chunk_beats",
        "_studio_v2374_group_all",
        "_studio_shot_contract_fingerprint",
    ]
    aliases = [
        "_ORIGINAL_V2371_VALIDATE_ROWS",
        "_V2371E_PREVIOUS_VALIDATE_ROWS",
        "_V2371G_PREVIOUS_VALIDATE_ROWS",
        "_V2372_R1_PREVIOUS_NORMALIZE_SHOT",
        "_V2372_R1_PREVIOUS_VALIDATE_ROWS",
        "_V2372_R1_PREVIOUS_GENERATE_BATCH",
        "_V2372_R2_PREVIOUS_NORMALIZE_SHOT",
        "_V2372_R2_PREVIOUS_VALIDATE_ROWS",
        "_V2372_R2_PREVIOUS_GENERATE_BATCH",
        "_V2372A_PREVIOUS_SCOPE",
        "_V2372B_PREVIOUS_GENERATE_CHUNK_BEATS",
        "_V2372C_VALIDATE_EXTRACTION",
        "_V2372E_PREVIOUS_FIND_ASSIGNMENT_LIST",
        "_V2375_PREVIOUS_GROUP_BATCH",
    ]
    route_prefixes = (
        "/api/studio",
        "/api/tasks",
        "/api/image",
        "/api/video",
        "/api/director/workbench",
    )
    payload = {
        "module_file": inspect.getsourcefile(runtime),
        "symbols": {
            name: _source_line(getattr(runtime, name)) for name in symbols
        },
        "aliases": {
            name: _source_line(getattr(runtime, name)) for name in aliases
        },
        "routes": [
            {
                "methods": sorted(getattr(route, "methods", set()) or set()),
                "path": route.path,
                "endpoint": route.endpoint.__name__,
                "defined_at": _source_line(route.endpoint),
                "same_as_module_symbol": (
                    getattr(runtime, route.endpoint.__name__, None) is route.endpoint
                ),
            }
            for route in runtime.app.routes
            if hasattr(route, "endpoint")
            and str(getattr(route, "path", "")).startswith(route_prefixes)
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
