# V2.39.6.3 Stage04 Full Pipeline Performance Patch Plan

## Scope and invariants

- Baseline runtime: `2.39.6.3-stage04-full-pipeline-preflight`.
- Delivery form: one transactional installer. The workspace `app/` and `scripts/` files are not modified directly.
- Business invariants remain unchanged: Narrative/temporal/scene-global audits, repair, evidence binding, `strict-shot-v2`, semantic guards and final validation all remain enabled.
- No business-keyword rules, story-specific branches, prompt downgrades, smaller model, or semantic acceptance relaxation are introduced.
- `scripts/start_llm.sh` is outside the installer payload. The 16K context change is a separate operational recommendation only.

## P0 — Anchor classification partial preservation

### Affected functions

- `app/main.py::_studio_v2374_classify_batch`
- Existing parser/validator helpers remain authoritative:
  - `_studio_v2373_extract_classification_plan`
  - `_studio_v2374_plan_parts`
  - `_studio_v2374_resolve_classification_ids`

### Change

1. Execute the primary 40-anchor classification request once.
2. Raise only this phase's output cap from the truncating 420 tokens to its existing requested 750-token contract budget; no other phase cap changes.
3. Parse and retain every valid, non-conflicting `beat_ids` / `support_evidence_ids` assignment from that response.
4. Recover valid quoted anchor IDs from either list even when the final JSON object is truncated; this parser only accepts IDs present in the requested anchor set and never infers a classification.
5. Define unresolved IDs only as missing IDs plus IDs present in both lists.
6. Invoke classification repair only for that unresolved set.
7. Merge repaired fields into the retained primary result and keep the existing exact-coverage, exclusivity and source-order checks.
8. Do not change the classification schema or semantic prompt.

### Expected benefit

- Removes the observed whole-batch strict retry for capped/truncated responses.
- Prevents already valid classifications from being resent or overwritten.
- Converts the two observed 40-anchor failure paths from full-batch retry plus broad repair into one primary call plus unresolved-only repair.
- Expected saving on the captured workload: approximately 13–18 seconds from the two redundant full-batch retries, with additional AnchorRepair savings proportional to the number of primary assignments preserved.

## P1 — MembershipRepair line-only bounded repair

### Affected function

- `app/main.py::_studio_v2374_resolve_group_membership`

### Change

1. Remove the fixed `json -> strict -> line` group escalation.
2. Use the existing line protocol directly for unresolved membership IDs.
3. Preserve every already valid Beat row and membership assignment.
4. Split unresolved IDs into bounded line-repair groups and enforce a shared maximum of five MembershipRepair LLM calls per Scene. If the bounded grouping cannot represent the unresolved set safely, fail closed before issuing an unbounded request.
5. Keep final exact membership, conflict, evidence ownership and Beat semantic validation unchanged.

### Expected benefit

- Removes the captured 17.465 seconds spent in two modes that accepted zero assignments.
- Reduces the captured six group-repair calls to two successful line calls.
- Keeps semantic ownership with Qwen while making serialization deterministic.

## P2 — Narrative audit token guard and semantic-preserving compaction

### Affected functions

- `app/main.py::_studio_v23963_audit_payload`
- `app/main.py::_studio_v23963_audit_prompt`
- `app/main.py::_studio_v2372_audit_extraction`
- `app/main.py::_studio_v2372b_complete_audit_schema`

### Change

1. Build the current full audit payload first.
2. Ask the active llama.cpp tokenizer through `DirectorService._count_prompt_tokens` for the real prompt count.
3. If the prompt is at or below 6,500 tokens, send the full payload unchanged.
4. If it exceeds 6,500 tokens, rebuild only the transport representation:
   - retain source order and compact source text once;
   - retain anchor `id`, source offsets and classification/evidence binding;
   - retain Beat index, `summary`/state, `state_change`, evidence IDs and evidence spans;
   - retain all five required audit dimensions and the existing response contract;
   - remove duplicated anchor narrative fields and Beat `source_evidence` text already represented by the single source text plus IDs/spans.
5. Recount the compact prompt. Refuse to send if it still exceeds 6,500 tokens; do not truncate semantic objects silently.
6. Schema completion does not reuse the semantic audit context. It sends exactly five top-level fields: `scene_id`, `audit_id`, `missing_fields`, `previous_audit_result` and `required_schema`.
7. `previous_audit_result` preserves every existing audit conclusion plus evidence IDs, Beat binding and deduplicated temporal ranges. It never carries source text, full anchors, full Beats, evidence narrative or duplicate spans.
8. Count the schema-completion request independently and refuse dispatch above 6,000 tokens.

### Expected benefit

- Prevents the observed 9,114-token request from reaching an 8,192-token runtime.
- Removes repeated evidence text without removing evidence IDs, offsets, binding or audit dimensions.
- Makes context overflow zero for the captured failing superchunk and allows execution to reach Shot generation.

## P3 — Context-overflow propagation in GemmaService

### Affected function

- `app/services/gemma.py::_request_messages`

### Change

1. Detect llama.cpp context overflow from HTTP 400/422 response body (`exceed_context_size_error`, `exceeds the available context size`, or equivalent structured code/message).
2. Raise a dedicated, metrics-bearing `LLMContextOverflowError` immediately.
3. Set request attempts to one and retries to zero for this condition.
4. Keep the existing 400/422 diagnostic log, message normalization, Qwen model verification, usage/timings collection and ordinary retry behavior for non-context failures.
5. Stage04 compaction happens before dispatch; the transport layer never mutates prompts or retries an identical over-budget payload.

### Expected benefit

- Removes the captured guaranteed-failure duplicate request and retry delay.
- Preserves a clear boundary: the transport reports overflow, while the Stage04 semantic caller owns compaction.

## P4 — llama.cpp 16K context operational recommendation

This installer does **not** modify `scripts/start_llm.sh`.

Recommended AutoDL launch change after validating VRAM headroom:

```bash
--ctx-size 16384
```

Keep the Stage04 6,500-token audit guard even after the runtime context is raised. The larger runtime window is operational headroom, not a substitute for duplicate-payload removal. Validate startup, KV-cache allocation, Qwen READY and a representative Stage04 run before making this launch parameter permanent.

## Installer contract

The installer will:

- guard the exact V2.39.6.3 baseline hashes and baseline OpenAPI version;
- reject active tasks before and after platform shutdown;
- save exact live bytes and modes;
- atomically write only `app/main.py` and `app/services/gemma.py`;
- run AST/`py_compile` and focused static/self-tests;
- restart and verify health/OpenAPI target version;
- verify target hashes by readback;
- restore exact backup bytes, verify rollback hashes and restart the baseline after any post-write failure.

## Acceptance tests

1. Partial classification preserves valid primary IDs and repairs only missing/conflicting IDs.
2. Classification schema and exact coverage remain unchanged.
3. Membership repair uses line mode directly and never invokes JSON/strict rounds.
4. Membership repair call budget is at most five.
5. Full audit payload remains unchanged below 6,500 tokens.
6. Oversized audit payload compacts duplicate narrative/evidence text while preserving IDs, offsets, state, evidence binding and required fields.
7. Compact audit prompt is recounted and must be at most 6,500 tokens.
8. An 8,000-token-scale audit context produces a schema-completion payload at most 6,000 tokens.
9. Schema completion contains exactly the five allowed top-level fields and recursively rejects `source_text`, `full_anchors` and `full_beats`.
10. Context overflow produces one attempt, zero retry and propagates to the Stage04 caller.
11. Non-context 400/422 behavior and response model/usage/timings checks remain covered.
12. Existing V2.39.6.1/V2.39.6.2/V2.39.6.3 protection suites continue to pass.
