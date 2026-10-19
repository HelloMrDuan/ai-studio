"""列出 FaceFusion force-download 需要的全部模型文件，生成 aria2c 输入清单（多线程、断点续传，比 FaceFusion 自带的 curl 下载稳）。
用法（在 FaceFusion 目录下用其 venv 运行）：python ff_prefetch.py <scope: lite|full> <输出清单路径>
"""
import os, sys
sys.path.insert(0, os.getcwd())
from facefusion import state_manager
from facefusion.core import get_processors_modules
from facefusion.filesystem import get_file_name, resolve_file_paths

scope, out = (sys.argv[1] if len(sys.argv) > 1 else 'full'), (sys.argv[2] if len(sys.argv) > 2 else 'ff_models.txt')
state_manager.init_item('download_providers', os.environ.get('FF_PROVIDERS', 'huggingface github').split())
state_manager.init_item('download_scope', scope)
procs = get_processors_modules([get_file_name(p) for p in resolve_file_paths('facefusion/processors/modules')])
mods = []
for pm in procs:
    for cm in pm.get_common_modules():
        if cm not in mods:
            mods.append(cm)
seen, lines = set(), []
for m in mods + procs:
    if not hasattr(m, 'create_static_model_set'):
        continue
    for model in m.create_static_model_set(scope).values():
        for group in ('hashes', 'sources'):
            for item in (model.get(group) or {}).values():
                url, path = item.get('url'), item.get('path')
                if not url or path in seen or (os.path.isfile(path) and not os.path.exists(path + '.aria2')):
                    continue
                # deep_swapper 的 DeepFaceLive 模型合计 ~37GB，默认不预下载（用到时自动下载）；FF_PREFETCH_ALL=1 可全下
                if 'deepfacelive' in url and os.environ.get('FF_PREFETCH_ALL') != '1':
                    continue
                seen.add(path)
                lines.append(f"{url}\n  dir={os.path.dirname(os.path.abspath(path))}\n  out={os.path.basename(path)}\n")
open(out, 'w').write(''.join(lines))
print(f'{len(lines)} files -> {out}')
