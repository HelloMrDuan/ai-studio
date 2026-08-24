# Stage04 Normal LLM Call Budget

This budget is a regression guard, not permission to remove semantic validation. It is derived from the two real logs and the effective V2.39.6.2 call graph. Nested timing categories must not be summed as wall time.

| Scene class | Expected calls | Maximum normal | Abnormal threshold |
|---|---:|---:|---:|
| simple | 8–18 | 24 | >24 |
| normal | 20–45 | 60 | >60 |
| complex | 45–90 | 120 | >120 |

Any scene with repair calls greater than 50% of total calls is also abnormal, regardless of the absolute count.

## Phase budget

| Phase | Necessary / normal | Maximum normal | Classification |
|---|---:|---:|---|
| anchor extraction | 0 | 0 | deterministic |
| anchor classification | 1 per configured batch | batch count + 1 | necessary semantic |
| AnchorRepair | 0–2 scoped batches | 5 | schema/coverage repair |
| Beat grouping | 1 per superchunk | superchunk count | necessary semantic |
| MembershipRepair | 0–2 per failed group | 6 per scene | repair amplification guard |
| adaptive grouping | 0–1 | 2 | fallback, only after structural failure |
| narrative audit | 1 per superchunk | superchunk count + repaired reruns | necessary audit |
| adjacent reconcile | 0–1 per boundary | boundary count | necessary semantic |
| forward projection/audit | 1 per affected boundary | affected boundary count + rerun | necessary audit |
| direct Shot generation | 1 per Shot batch | batch count | necessary semantic |
| missing Beat completion | 0 | missing batch count | scoped repair only |
| duration planning | 0 | 0 | deterministic |
| temporal/schema/evidence repair | 0–2 per failed batch | 2 per batch | repair amplification guard |
| boundary repair | 0–1 per failing boundary | boundary count | scoped repair |
| batch audit | 1 plus repair reruns | 3 per batch | necessary audit |
| scene-global audit | 1 plus one repair rerun | 2 | necessary audit |

## Real-log baseline

- V2.39.6 Scene 1 failed before completion: 77 calls, 31 repair calls, 184,148 input tokens and 17,651 output tokens. This is abnormal repair amplification.
- V2.39.6.1 failed before Shot generation: 42 calls, 25 repair calls, 108,365 input tokens and 11,950 output tokens. Repair share exceeded 50%, therefore abnormal despite remaining below the normal-scene absolute threshold.

The real verifier must print per-scene calls, repair/schema/AnchorRepair/MembershipRepair/Shot-repair counts, token usage and phase seconds. Semantic success above a threshold is `SEMANTIC PASS / PERFORMANCE REGRESSION`, not full pass.
