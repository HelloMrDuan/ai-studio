# Full Pipeline Runtime Call Graph

Audit baseline: `2.39.6.2-stage04-narrative-lineage-closure`. Runtime import was
performed with `tools/preflight_runtime_inspect.py`; no route or media job was
executed. `file:line` values refer to the frozen V2.39.6.2 snapshot and are
updated in the final report where the V2.39.6.3 patch moves a line.

## V2.39.6.3 effective-runtime readback

After the cumulative patch, a second import inspection confirmed the following
effective definitions and route objects (line numbers below are the final target):

- rebuild route `app/main.py:4183` reserves a durable `starting` task before Qwen preflight;
- the background symbol resolves to the last definition at `app/main.py:25956`;
- that wrapper calls `app/stage04_v238_runtime.py:7753` through `globals()`;
- final Scene implementation is `runtime.py:7058`; durable transaction snapshot/recovery is `7474/7498`; formal commit is `7598`;
- effective fingerprint is the final definition at `app/main.py:6310` and now covers states, all three prompts, evidence and version fields;
- every inspected FastAPI `route.endpoint` remains identical to its current module symbol.

The generic Stage04 `run-stage` path is no longer a semantic generator: both
the route and worker fail closed for non-approval input (`main.py:4781,5581`).
Approval remains reachable only through `_studio_stage04_finalize`, which now
requires the exact V2.39.6.3 runtime, `strict-shot-v2`, and `qwen3-32b` pipeline.

## Stage01 → Stage03 creation chain

`POST /api/studio/projects/{project_id}/run-stage` (`app/main.py:5456`)
→ validates project and persisted active job (`5458-5465`)
→ persists job then `asyncio.create_task(_studio_run_stage_job)` (`5466-5485`)
→ selects `_STUDIO_STAGE_SKILLS[stage]` (`4630-4653`)
→ `gpu.use(GPUOwner.gemma)` → `director.message` (`4768-4769`)
→ `DirectorService` / Skill runtime → production graph materialization
→ job JSON persistence (`_studio_save_job`).

The Stage02 classifier has a documented non-blocking fallback (`4681-4694`),
but it is routing metadata, not Stage04 semantic authority.

## Stage04 dedicated rebuild — actual production chain

ENTRY `POST .../stage04/rebuild-production` (`app/main.py:4102`)
↓ request/project validation and Qwen preflight (`4103-4124`)
↓ `_studio_v2396_prepare_stage04_qwen` (`6466`)
↓ `gpu.ensure_ready(GPUOwner.gemma)` → selected registry model reload → exact
`/models` and probe response identity (`6390-6487`)
↓ background symbol `_studio_v2371_rebuild_stage04`
↓ ACTUAL CALLEE: final definition at `app/main.py:25803`
↓ INDIRECT WRAPPER: `stage04_v238_runtime.rebuild(globals(), project_id, task_id)`
↓ EFFECTIVE IMPLEMENTATION: `app/stage04_v238_runtime.py:7600`
↓ `scene_shots` (`7034`)
↓ final main-global callbacks selected at runtime: Scene source final symbol,
effective `_studio_v2371b_ensure_scene_beats` at `main.py:14419`, and final
grouping wrappers; the inspectable final `_studio_v2372_generate_chunk_beats`
is not called by that effective ensure function
↓ final Shot generate/validate/audit aliases selected through `globals()`
↓ adjacent Beat reconciliation and forward projection (`runtime.py:2372`)
↓ Shot generation / missing Beat completion (`5422`, `4121`)
↓ `validate_rows` (`2962`) and deterministic prompt compiler (`2904`)
↓ evidence-locked, boundary, batch and scene-global audits/repairs
(`4543`, `4874`, `6370`, `6704`)
↓ deterministic scene closure (`6206`)
↓ PERSISTENCE transaction and `_commit_formal_shots` (`7380-7810`)
↓ task `completed` only after storyboard, continuity, production graph and
project save (`7811-7821`)
↓ CANONICAL READBACK: project snapshot (`main.py:5398`) and
`_studio_formal_shot` (`5713`).

## Stage04 legacy generic route

ENTRY `POST .../run-stage` at Stage04 (`5456`)
→ `_studio_run_stage_job` (`4630`)
→ `_studio_stage04_generate_detailed` (`4449` vicinity)
→ global `_studio_stage04_scene_shots` (effective final definition is not
`stage04_v238_runtime.scene_shots`)
→ old persistence/finalize path (`4450-4625`).

This path was API-reachable at the frozen baseline and bypassed the V2.39.6.2
wrapper. V2.39.6.3 disables generation through it; the helper remains legacy
code but is no longer API-reachable.

## Stage04 status/read

`GET .../stage04/rebuild-production/status` (`4127`) → baseline in-memory
`_STUDIO_V2371_REBUILD_TASKS`; V2.39.6.3 adds an atomic disk journal and restart
recovery. Storyboard read is via `GET /api/studio/projects/{project_id}`
(`5398`) → project + complete production graph + continuity snapshot.

## Shot image production

ENTRY `POST .../shots/{shot_id}/generate-image` (`6850`)
→ `_studio_formal_shot` → strict-shot-v2 validation
→ `_studio_v2371_prompt_asset(kind=image)` (`3760`), directly consuming
`image_prompt`
→ ephemeral `_studio_shot_target` (`5788`)
→ `director_workbench_execute_candidate` (`2576`)
→ internal POST `/api/image/tasks` → `image_task` (`687`), locked passthrough
(`787-815`) → `TaskRunner.submit_image` → `_run_image`
→ persisted TaskStore candidate
→ manual confirm (`2788`) → `_studio_publish_confirmed_shot_candidate` (`5896`)
→ canonical ready asset.

## Shot video production

Video start ENTRY (`6900`) follows the image chain but consumes only the
persisted `video_start_prompt` and binds the `video_prompt` asset. Video ENTRY
(`6952`) requires the current adopted video-start asset and exact fingerprint,
then calls `/api/video/tasks` (`956`) → `TaskRunner._run_video` → H3 service.
Manual confirmation is the only canonical switch.

## Generic task status

`GET /api/tasks/{task_id}` (`1109`) → persistent `TaskStore.get`. Image/video
workers persist `switching_gpu → running → completed|failed`; on restart,
baseline TaskStore converts active records to failed (`core/task_store.py:22-38`).

## Stage06 final composition

ENTRY `POST .../assemble` (`7350`)
→ reload formal shots from continuity and source-order sort (`7357-7363`)
→ `_studio_latest_shot_asset(..., VIDEO)` for every shot (`7366-7373`)
→ exact ordered request equality and uniqueness (`7374-7379`)
→ ffprobe every source, normalize via ffmpeg, concat, final ffprobe
(`7389-7477`)
→ `register_existing_file(logical_key=studio:final_cut)` (`7479-7502`)
→ parent lineage relations (`7503-7507`)
→ canonical final asset read through project snapshot/production APIs.

## Dynamic binding proof

Runtime inspection resolved final lines: rebuild `25803`, generate batch
`16561`, validate rows `15830`, batch audit `16389`, scene source `16808`,
chunk Beat generation `24656`, group-all `25682`, and fingerprint `6159`.
Every listed FastAPI route endpoint was identical to its module symbol. Saved
aliases preserve several earlier implementations; the detailed matrix is in
`RUNTIME_REACHABILITY_DEAD_CODE_MATRIX.md`.
