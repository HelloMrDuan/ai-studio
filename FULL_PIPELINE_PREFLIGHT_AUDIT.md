# AI Studio Stage04 → Stage06 Full Pipeline Preflight Audit

## Scope and evidence level

The frozen baseline is the actual completed
`2.39.6.2-stage04-narrative-lineage-closure` workspace, not V2.39.6.1. The
audit covered Stage01–06 entry points and deeply traced Stage03→04→05→06,
including dynamic wrappers, aliases, later-definition shadowing, task workers,
JSON/file persistence, candidates, canonical assets, restart recovery, GPU/Qwen
identity and final composition. No image, video, composition or real GPU job
was executed locally.

Modified-file baseline → target hashes:

| File | V2.39.6.2 baseline SHA256 | V2.39.6.3 target SHA256 |
|---|---|---|
| `app/main.py` | `0c54cb0fc4c5cb09f1d3584b5eec1ee6ff86b208e0a323a6e08447241b957eb3` | `82c5ce06876ea3f17dba1853af2b4ffcbe2c2ca13f93ea78a7005d107a12c787` |
| `app/stage04_v238_runtime.py` | `17f805fe365fc1ab418ebf97f0461a180c5e583c62b8dca163398a670766947d` | `e668321b8eccf9f8adaf02452ffd5c9a0c1f0b890db4ca53ff28bd718fbdf332` |
| `app/core/task_store.py` | `7d5ad3a4c4ba458dd9de80e5e249848c2951a02bd4453d6759d26c025c9276b8` | `508954c7e40513fe76b9e057b70d6b295c604f1ef294faabb7fc13760690c876` |
| `app/services/production_assets.py` | `4e4ca6598e1f55a2802ddcbdae48ed5642a2274daf020cf5889e100019eec1c4` | `7ec069ee77aa60995a9dbf929c7657bd61844e242c67050139f202079b4c819f` |
| `app/services/story_continuity.py` | `52b9a0feba2508c1a4aa8c4a04bf591fe37097be5313e1b5160da4fd2eec20cf` | `cfd5d89140f10b5ab57c0745dd82fb7465159fc5e76648396c644b35a0b0eaeb` |

## Effective Stage04 lifecycle

Scene source and offsets are split into superchunks and deterministic anchors.
Qwen owns classifications, Beat semantics and Shot semantics. Code owns source
offsets, allowed IDs, ordering, dedupe, coverage, state-derived prompt
compilation, duration structure, repair merge scope, persistence and version
selection. AnchorRepair/MembershipRepair cannot change source offsets. Beat
normalization sorts by source lineage and migrates event/state/evidence as one
closure. Out-of-order model Beats therefore cannot dictate chronology.

Each final Shot carries Beat orders, exact source evidence/spans, three states,
derived prompts, entity IDs, runtime/contract/model versions, and persisted
Narrative/batch/scene-global/forward-overlap audit proofs. Those fields survive
continuity persistence, compact API reload and the Shot entity mirror used by
Stage05.

## Repair and audit mutation conclusions

The first confirmed global mutation bug was `_merge_shot_repair_patch`: only
the three state fields were protected from present-but-empty values. V2.39.6.3
rejects `None`, empty and whitespace patches for every semantic text field,
while still allowing meaningful empty entity lists. Identity, evidence and Beat
bindings remain locked or recomputed from source lineage.

A second mismatch occurred after boundary repair: the repaired rows were
validated by `batch_audit`, but `source_audit` still referenced the earlier
object. The successful post-repair audit is now assigned before rows return.
Scene-global repairs still rerun deterministic checks and the global audit.
Audit proofs are metadata only; no audit, evidence check, forward-overlap guard,
strict-shot-v2 requirement or semantic contract was removed.

## Stage04 → Stage05

Stage04 remains the sole semantic authority. `_studio_formal_shot` reads the
persisted formal Shot, recovers any interrupted transaction, blocks active
canonical switches, hydrates only missing mirrored values and never rereads a
Scene to reinterpret events. Stage05 requires exact V2.39.6.3 runtime,
`strict-shot-v2`, `qwen3-32b`, evidence and all audit proofs. Image prompt must
equal representative state; video-start prompt must equal start state; motion
prompt must exactly encode start→end.

Prompt assets and candidate targets carry the complete Shot fingerprint.
Stale/superseded/non-ready prompts are rejected centrally. A candidate can be
published only after explicit confirmation and only if its fingerprint still
equals the current formal Shot. The former video-start policy spelling mismatch
was normalized to exact `qwen3-32b`.

## Stage05 → Stage06

Current image/video selectors require active, READY, non-stale assets, exact
formal-Shot fingerprint and correct parent ancestry. Failed/cancelled candidates
remain candidates. New confirmation supersedes the prior canonical and marks
descendants stale through the production graph. `context_manifest` and Studio
text lists now exclude stale assets.

Stage06 sorts formal Shots by global order, selects exactly one current video
per Shot, rejects old/duplicate/out-of-order requested IDs, verifies files with
ffprobe, normalizes and concatenates in that same order, and records the input
manifest and parent asset lineage. It cannot select a video from an older Shot
contract after this patch.

## Canonical/task/persistence/runtime

- One project obtains a durable `starting` reservation under an async lock
  before awaited Qwen startup. Active image/video candidates block rebuild.
- Stage04 status is atomic and durable. Restarted orphan active records become
  failed after transaction recovery.
- The canonical switch uses a disk journal containing exact pre-switch project,
  continuity and graph bytes plus the preexisting file set. New files are
  removed on recovery. Journal paths are recomputed, not trusted from JSON.
- `completed` is written only after project/continuity/graph persistence and
  journal deletion. Runtime performance is persisted afterward without changing
  canonical semantics.
- generic TaskStore JSON writes use temp+replace; active tasks recovered after
  restart become failed.
- GPU lease exit remains in `finally`. Stage04 verifies selected ID/alias,
  installed GGUF, resolved alias, exact `/v1/models`, probe content, and actual
  response model. Any mismatch fails closed.

## Historical compatibility

Duplicate and shadowed functions remain. Runtime import inspection—not version
comments—identified the effective final definitions. Several earlier functions
are deliberately captured by compatibility aliases and are therefore not dead.
The legacy Stage04 generator remains in source but both API route and worker
reject non-approval generation through it. No broad dead-code deletion was done.

## Answers required by the audit

1. The rebuild API calls the final `_studio_v2371_rebuild_stage04`, which wraps
   `stage04_v238_runtime.rebuild(globals(), ...)`.
2. Duplicate/shadowed functions exist; actual effective objects and aliases are
   enumerated in the reachability matrix.
3. Legacy helpers remain inside explicit compatibility wrappers; the unsafe
   generic Stage04 generator is no longer API-reachable.
4. Source→Anchor→Beat→Shot lineage closes over ordered offsets, evidence and
   audit provenance; local replay passes.
5. Empty/null repair candidates can no longer destroy valid semantic text.
6. The known post-boundary A-vs-B audit mismatch is fixed; final saved semantic
   rows are the rows validated by batch and scene-global audits.
7. Candidate/canonical/stale cross-version reads are blocked by complete
   fingerprints, readiness, dependency state and ancestry.
8. Stage05 does not reinterpret Stage04 semantics.
9. Stage06 rejects old, failed, stale, mismatched and duplicate assets.
10. `completed` cannot occur before persistence/canonical switch.
11. Qwen Stage04 is fail closed at registry, process, model-list, probe and every
    response identity boundary.
12. There is no Stage04 Qwen→Gemma/other-model fallback. A legacy launcher
    default remains only when Stage04 Qwen is not the explicitly selected model;
    Stage04 cannot pass its preflight in that state.
13. All three historical failures are locally replayed and pass.
14. Call budgets are simple 8–18 (max 24), normal 20–45 (max 60), complex
    45–90 (max 120).
15. The largest observed repair amplification is 25/42 = 59.5% in the
    V2.39.6.1 real failure; V2.39.6 had 31/77 repairs and an abnormal total.
16. With confirmed P0 and P1 blockers at zero and all required local categories
    passing, the next isolated real Stage04 AutoDL E2E is justified. Stage05/06
    real asset generation remains behind the Stage04 semantic acceptance gate.

## Residual proof boundary

CUDA behavior, llama-server startup on the real device, Qwen semantic quality,
real response timing, and the real two-Scene acceptance remain
`REAL GPU E2E REQUIRED`. Local simulation does not claim those results.
