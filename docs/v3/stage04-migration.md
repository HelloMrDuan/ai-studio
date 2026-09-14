# Legacy Stage04 → V3 Storyboard Migration

This document records the **implemented migration boundary**, not a promise that legacy Stage04 has already been deleted.

## What V3 preserves as domain rules

The following concepts from `app/stage04_v238_runtime.py` are valid domain ideas and are being moved out of the giant runtime:

| Legacy concept | V3 owner | Current status |
| --- | --- | --- |
| Beat order / Beat-local evidence | `app/v3/storyboard` | implemented contract + deterministic audit |
| `observable_transition` | Storyboard domain | implemented deterministic state-shape audit |
| `static_outcome` | Storyboard domain | implemented narrative lock + presentation-only variation audit |
| `insufficient_visual_evidence` | Storyboard domain | fail-closed deterministic outcome |
| no future Beat borrowing | Storyboard deterministic audit | implemented |
| evidence must stay inside current Beat | Storyboard deterministic audit | implemented |
| entity membership | Storyboard + Continuity | implemented deterministic boundary |
| character/creature/prop/location references | Continuity + generation contract | implemented reference-first contract |
| infrastructure retry | Temporal workflow | moved out of Stage04; implemented V3 boundary |
| semantic recovery | Storyboard domain | not yet migrated as a complete replacement |
| LLM semantic audit | Quality Audit skill | not yet connected to a V3 provider |

## What is deliberately not copied into V3

The V3 modules do not import `stage04_v238_runtime.py`. Legacy repair branches, regroup branches, Qwen request orchestration, performance instrumentation and UI progress concerns are not automatically copied into Storyboard just because they existed in the old file.

A legacy branch is ported only if it represents a still-valid invariant. Infrastructure errors belong to Temporal/Provider; semantic failures belong to professional domain recovery; UI progress belongs to workflow/task projection.

## Strict audit boundary

V3 deterministic audit owns rules that can be proven without a model. Only after deterministic validation succeeds is a semantic audit eligible to run.

Malformed model/auditor protocol output is an audit/provider protocol failure. It must not be interpreted as a semantic `false` and must not trigger evidence regroup or temporal recovery.

## Retirement criterion

The old Stage04 file is not considered retired until all of these are true:

1. V3 Storyboard produces a complete shot contract from source/Beat/evidence.
2. V3 deterministic + semantic audit closes the strict contract.
3. V3 semantic recovery is bounded and tested.
4. V3 generation consumes entity/reference contracts instead of raw independent shot prompts.
5. The fixed snow-mountain fixture and additional diverse stories pass real AutoDL E2E.
6. No active V3 API/UI imports the legacy Stage04 runtime.

Until then, the old code remains a legacy reference path on this refactor branch, not the V3 authority.
