# Full Pipeline Preflight Test Report

Target: `2.39.6.3-stage04-full-pipeline-preflight`  
Real GPU Stage04/05/06 execution: **NOT RUN**.

## Result summary

| Category | Result | Evidence |
|---|---|---|
| Python AST / py_compile | STATIC PASS | all five modified runtime files, installer and verifier compile |
| Runtime call graph / effective objects | STATIC PASS | import inspection plus AST last-definition test; every inspected route endpoint equals its module symbol |
| Unit / contract / lineage / repair | SIMULATED PASS | 70 unittest cases |
| Persistence / reload | LOCAL INTEGRATION PASS | JSON round-trip; compact contract exposure; durable partial-commit recovery |
| Stage04→Stage05 contract | SIMULATED PASS | prompt closure, entity mirror fields, runtime/contract/model gates |
| Stage05→Stage06 assets | SIMULATED PASS | stale/old-fingerprint selectors reject; failed candidates cannot publish |
| Historical failures | REPLAY PASS | Qwen identity, Shot state mutation, Narrative lineage fixtures |
| Installer self-test | LOCAL INTEGRATION PASS | payload AST/hash plus exact-live-backup rollback simulation |
| Verifier self-test | SIMULATED PASS | two-Scene fixture, 34 acceptance gates, no media route |
| Real GPU E2E | REAL GPU E2E NOT RUN | intentionally deferred until this preflight is complete |

Command: `D:\soft\python3\python.exe -m unittest discover -s tests -p 'test_*.py'`  
Observed: `Ran 70 tests ... OK`.

## Failure injection matrix

| Injection | Expected result | Local proof/result |
|---|---|---|
| Qwen empty/null/missing Shot state | scoped repair then strict failure if unresolved | SIMULATED PASS |
| Qwen extra Shot field | normalized contract ignores non-writable data; semantic fields unchanged | STATIC/SIMULATED PASS |
| invalid enum/identity/entity destination | schema/allowed-id validator rejects | STATIC PASS |
| Beat out of order / duplicate / missing anchor | source-offset reorder or fail closed; no insertion-order authority | REPLAY PASS |
| invalid destination / missing evidence / wrong evidence order | closure and span validator rejects | REPLAY PASS |
| semantics/evidence not closed | Narrative audit fail closed | REPLAY PASS |
| repair empty state / partial repair | preserve valid fields; scoped non-empty patch; full revalidation | SIMULATED PASS |
| all three states absent | strict validation fails; no empty default | REPLAY PASS |
| audit missing field / malformed JSON / truncated output | schema completion is model-scoped; unresolved parse fails closed | STATIC PASS; real response not invoked |
| invalid JSON after retry | no default-valid conversion | STATIC PASS |
| Qwen timeout/startup failure | task fails; GPU lease exits | REPLAY/STATIC PASS |
| response model mismatch | immediate fail closed | REPLAY PASS |
| duplicate Stage04 request | durable `starting` reservation under per-project lock rejects second request | STATIC/SIMULATED PASS |
| service restart during active rebuild | orphan marked failed; transaction recovery occurs first | SIMULATED PASS |
| process crash during persistence | journal restores project, continuity, graph and removes new candidate files | LOCAL INTEGRATION PASS |
| stale/failed/old media candidate | excluded from context, confirmation and Stage06 selection | SIMULATED PASS |
| old runtime Shot | Stage05 exact runtime/contract/model check rejects | SIMULATED PASS |
| reload missing contract field | compact/readiness validator fails closed | SIMULATED PASS |
| task cancellation/timeout | terminal candidate never canonical; persisted active generic tasks become failed on restart | STATIC PASS |
| GPU PID/port/CUDA/llama binary failure | existing orchestrator/start script fails before Stage04 identity contract can pass | STATIC/REPLAY PASS; REAL GPU REQUIRED for device behavior |

No injection produced silent semantic defaults, silent model fallback, or a
canonical switch from a failed candidate.

## Canonical isolation and transaction sequence

The candidate is fully generated, repaired and audited in memory. Persistence
then records `persisting`, writes a durable rollback journal, creates the new
storyboard/entities/Shots, saves continuity and project, removes the journal,
and only then records `completed`. An exception restores the exact pre-switch
bytes. A process restart discovers and restores the journal before project,
formal Shot, Stage06, status or rebuild read paths proceed.

## Installer/verifier safety

The installer contains target payloads and baseline hashes only. It backs up
the live installation bytes, writes atomically, compiles, restarts, validates
health/OpenAPI/runtime/hash readback, and rolls back from that exact backup.
It calls no business mutation endpoint. The verifier requires explicit
`REBUILD_STAGE04_ONLY` confirmation and calls only Stage04 rebuild/status and
read endpoints; image, video and assemble routes are absent.
