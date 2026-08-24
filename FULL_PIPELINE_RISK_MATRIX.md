# Full Pipeline Risk Matrix

This matrix was completed before the cumulative patch. “Resolution” is updated
after tests; no issue below was inferred from a version comment alone.

| ID | Stage / API | Runtime path / file:line | Problem / trigger / root cause | Consequence | Severity | Minimal resolution / coverage |
|---|---|---|---|---|---|---|
| FP-001 | Stage04 `run-stage` | route 5456 → job 4630 → legacy generator ~4449 | Generic business entry bypasses V2.39.6.2 runtime | old schema/audits can become formal canonical; E2E semantic corruption | P0 | fail closed except approval; finalizer requires current runtime+contract; reachability test |
| FP-002 | Stage05/06 | fingerprint 3739 shadowed by 6159 | effective fingerprint omitted three states, video-start prompt, evidence/version | stale assets may look current after semantic change | P0 | one complete effective fingerprint + mutation tests |
| FP-003 | repair | runtime 3609-3646 | blank/null protection applied only to three state fields | valid summary/action/prompt could be erased | P0 | reject null/blank text patches globally; repair mutation injection |
| FP-004 | batch audit | runtime 6136-6203 | after boundary repair, persisted `source_audit` remained pre-repair audit | audit metadata refers to A while object B persists | P1 blocker | assign successful post-repair batch audit before persistence; closure test |
| FP-005 | Stage04 rebuild | main 4103-4124 | check occurs before awaited preflight; two requests can pass | concurrent rebuild/task/canonical collision | P1 blocker | per-project async lock + `starting` reservation; concurrency test |
| FP-006 | Stage04 status | main 3987,4127 | task state memory-only | restart returns idle/orphan and loses failure/progress | P1 blocker | atomic task journal; restart replay marks orphan failed |
| FP-007 | Stage04 persistence | runtime 7380-7810 | rollback existed only in process memory | process crash can leave partial graph/continuity/project canonical | P0 | durable rollback journal + entry-point recovery + crash replay |
| FP-008 | media task state | task_store 103-108 | task JSON direct write | crash can truncate JSON; loader silently skips record | P1 blocker | temp+replace atomic persistence; corrupt-write test |
| FP-009 | Director context | production_assets 931-955 | `context_manifest` included active ready but stale assets | downstream model may consume superseded facts | P0 | exclude `dependency_state=stale`; selection test |
| FP-010 | Stage05 confirm | main 5896-6000 | completed old candidate can be confirmed after Stage04 Shot changed | old/failed semantic lineage becomes canonical | P0 | compare current formal Shot + exact fingerprint at confirmation |
| FP-011 | Stage05 generic prompt | main 2576,7053,7086 | prompt read checked content/status incompletely | stale/superseded prompt can start production | P0 | central current/ready/non-stale prompt guard |
| FP-012 | Stage06 | main 7350-7379 | selected video lineage did not independently compare current formal Shot fingerprint | old semantic version could enter final | P0 | current-shot fingerprint check in asset selector and assembler replay |
| FP-013 | Stage04 vs Stage05 | rebuild 4103 and candidate worker | baseline did not block rebuild while Studio media candidates run | waste and late old-candidate adoption race | P1 blocker | active candidate guard + confirmation fingerprint gate |
| FP-014 | GPU/Qwen | start_llm 98-109 | launcher has legacy other-model fallback when required Qwen is not selected | non-Stage04 workspace may start another model | P2 / not Stage04 blocker | Stage04 exact preflight and every response.model already fail closed; retain, document |
| FP-015 | historical code | many duplicate definitions | aliases preserve old functions and static grep is ambiguous | wrong audit conclusions / maintenance risk | P3 | runtime inspect matrix; no mass deletion |
| FP-016 | Stage04→verifier | runtime commit 7661/7733; compact snapshot 208 | audit/state/version provenance was not fully persisted/exposed after reload | an accepted object could not be proven identical after restart | P1 blocker | persist Narrative/batch/scene-global/forward audit proofs and expose the full contract in compact snapshot |
| FP-017 | performance gate | runtime profiler 188-214 | counters were global only, so per-Scene regression could not be enforced | verifier could hide a single abnormal Scene | P1 blocker | record per-Scene call/token/phase deltas; verifier threshold replay |
| FP-018 | Stage05 video-start confirmation | main generation metadata vs publication policy | generated video-start target used `qwen3-32b@stage04` while confirmation required `qwen3-32b` | every valid video-start candidate could be rejected | P0 | standardize exact policy value and retain current-fingerprint gate |

## Resolution status after cumulative patch

| Severity | Confirmed before patch | Remaining blocker | Evidence |
|---|---:|---:|---|
| P0 | 9 | 0 | fingerprint mutation, repair mutation, stale selection, old-candidate, legacy-route, transaction recovery and metadata-policy tests |
| P1 blocker | 7 | 0 | durable reservation/task journal, atomic TaskStore, audit-object identity, active-candidate guard, audit provenance and per-Scene performance tests |
| P2 | 1 | 1 documented, non-blocking | launcher fallback is outside Stage04; Stage04 preflight and every response model remain fail closed |
| P3 | 1 | 1 documented, non-blocking | shadowed/aliased historical functions retained; effective objects verified by runtime import inspection |

All FP-001 through FP-013 and FP-016 through FP-018 are resolved in the V2.39.6.3 target payload. FP-014 and FP-015 are explicitly retained, bounded and not on the Stage04 semantic/canonical path.

No hardcoded story/person/action terms are used by any proposed resolution.
