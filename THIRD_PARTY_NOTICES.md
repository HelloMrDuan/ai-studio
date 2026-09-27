# Third-Party Notices

Xiaoduan Studio V3 intentionally tracks and reuses mature open-source work where license terms allow it.

## MoneyPrinterTurbo

- Repository: https://github.com/harry0703/MoneyPrinterTurbo
- License: MIT
- Reviewed upstream baseline: `5ceffd02a267de2ede0bbdb0fab8d7d875ea9842`
- Directly adapted in V3:
  - `app/services/task_artifacts.py` → `app/v3/media/task_artifacts.py`
  - SRT parsing / Levenshtein helpers from `app/services/subtitle.py` → `app/v3/media/subtitle_utils.py`
  - subtitle timeline/correction strategy from `app/services/subtitle.py` → `app/v3/media/subtitle.py`
  - provider/self-hosted TTS dispatch patterns from `app/services/voice.py` → capability-neutral `app/v3/media/tts.py`
  - bounded BGM upload, filename validation, FFmpeg audio validation and atomic persistence from `app/services/bgm.py` → `app/v3/media/bgm.py`
  - FFmpeg/video codec fallback, AAC 192k audio and composition safety ideas from `app/services/video.py` → `app/v3/media/composition.py`
- Xiaoduan modifications remove MoneyPrinterTurbo-specific global config, Streamlit, monolithic task orchestration and fixed provider assumptions. Media stages are independent services intended for Temporal Activities and capability-driven providers.

MoneyPrinterTurbo MIT license text:

> MIT License
>
> Copyright (c) 2024 Harry
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## waoowaoo

- Repository: https://github.com/waooAI/waoowaoo
- License: Elastic License 2.0
- Reviewed upstream baseline: `6cbbe22cc6492159e0f649d507e4e21a9aec3074`
- Architecture/contracts studied rather than copied verbatim:
  - Creative Skill separation between script development, creative direction, reusable asset development and video direction.
  - `asset-development` discipline: only reusable character/location/prop identities become persistent reference assets; stable identity/space/object design is separated from momentary shot action.
  - Generated reusable media enters a review checkpoint before becoming the adopted version consumed downstream.
  - Resource versions and lineage remain explicit so an upstream revision invalidates downstream work instead of silently overwriting it.
  - Temporal durable-execution and resource-lineage concepts used by the independently implemented V3 workflow layer.
- Xiaoduan independently implements those concepts in `app/v3/front_half_skill_overlay.py`, `app/v3/reference_assets.py`, `app/v3/stage_revision.py`, the V3 ResourceStore and Temporal workflow code. No waoowaoo implementation file is copied into this repository.
- Any future direct reuse must be separately reviewed against Elastic License 2.0 and preserve required notices and modification disclosures.

Tracked baselines are machine-readable in `config/upstreams.json` and are checked by `.github/workflows/upstream-watch.yml`.
