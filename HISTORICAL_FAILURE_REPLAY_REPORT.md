# Historical Failure Replay Report

## Source evidence

The replay corpus is grounded in all four requested real logs:

- `diagnostics/v2396-real-e2e.log`
- `diagnostics/platform-v2-after-v2396-e2e.log`
- `diagnostics/v23961-real-e2e.log`
- `diagnostics/platform-v2-v23961-narrative-fail.log`

No local replay is described as a real GPU E2E.

## Failure 1 — Qwen workspace/runtime identity

Historical risk: selected registry entry, loaded alias, `/v1/models`, and the
actual chat `response.model` could diverge or a missing selected model could
fall through to another installed model.

Replay: `test_v2396_qwen_runtime_contract.py` injects missing registry entry,
wrong alias, missing GGUF, wrong resolved model, wrong model list, and wrong
chat response model. All Stage04 variants fail closed. The exact valid contract
passes, timeout hierarchy is checked, and the rebuild-wide cached contract is
usable only while the verified GPU lease is active.

Result: **REPLAY PASS**. `GPUOwner.gemma` remains only a historical workspace
name. There is no Qwen→Gemma semantic fallback on the Stage04 path.

## Failure 2 — valid Shot states erased by repair

Historical log evidence: V2.39.6 terminated with Shot #1 missing
`representative_state`, `video_start_state`, and `video_end_state` after 77 LLM
calls. The real PERF block recorded 184,148 input tokens, 17,651 output tokens,
31 repair calls, Scene 1 ≈358.803s and overall ≈391.818s.

Replay: `test_v23961_shot_state_closure.py` covers direct generation,
missing-Beat completion, empty repair values, partial-field repair, final strict
validation and call scoping. V2.39.6.3 extends the mutation injection to valid
summary/action fields: `None`, empty and whitespace repair candidates cannot
erase any existing semantic text; a non-empty scoped model patch remains
allowed and prompts are deterministically recompiled from states.

Result: **REPLAY PASS**.

## Failure 3 — Narrative evidence/temporal closure

Historical log evidence: V2.39.6.1 failed before Shot generation in
`SUPERCHUNK_NARRATIVE_AUDIT`; total ≈235.676s, 42 calls, 25 repair calls,
108,365 input and 11,950 output tokens. The invalid Beat semantics were not
closed over their source evidence and source order.

Replay: `test_v23962_narrative_lineage_closure.py` uses the lineage fixture to
test offset ordering, out-of-order model Beats, evidence merge order, required
evidence retention, semantic/evidence closure, cross-Beat isolation and the
actual narrative-audit input. Deterministic closure adds no LLM call.

Result: **REPLAY PASS**.

## Amplification classification

- V2.39.6: 77 calls exceeded the normal-scene maximum of 60. Although repair
  share was 40.3%, the absolute call count was abnormal.
- V2.39.6.1: repair share was 59.5%, independently abnormal even though the
  total was 42.
- Both logs show stable llama.cpp output around 60–67 token/s; failure-driven
  schema/repair loops, not GPU decode rate, were the dominant observed issue.

The V2.39.6.3 verifier reports per-Scene total/repair/schema/AnchorRepair/
MembershipRepair/Shot-repair calls and blocks a full pass on either the
scene-class limit or repair share above 50%.
