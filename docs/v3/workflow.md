# Xiaoduan Studio V3 Durable Workflow

Temporal is the V3 execution control plane for long-running production work. It is not the product database and it does not own screenplay, entity, asset or billing truth.

## Ownership

Temporal owns:

- ordering
- durable execution permission
- infrastructure retry
- timeout
- cancellation
- human review waits
- worker restart recovery

Domain services own:

- Resource / Entity state
- candidate/adopted versions
- provider external IDs
- semantic validation
- continuity decisions
- artifacts and media

## Infrastructure failure vs semantic failure

`xiaoduan_execute_step` is an Activity. Network errors, worker loss and provider 5xx errors may fail the Activity and are subject to a bounded Temporal retry policy.

A semantic failure must instead return `StepActivityResult(kind="semantic_failure")`. The Workflow stops the current production plan and does **not** replay the same semantic request as infrastructure recovery.

A human gate returns `kind="review_required"`. The Workflow waits for a durable `review` signal. Rejection becomes a typed workflow result rather than silently regenerating work.

## Idempotency

Activities are at-least-once. Every `ProductionStep` therefore carries an `idempotency_key`. The domain executor that creates a provider job, DB record or media artifact must use that key to deduplicate side effects.

## Payload size

Workflow inputs carry resource references (`payload_ref`) rather than full prompts, model transcripts or binary media. Large domain objects remain in the project/resource store.

## Worker boundary

`ProductionActivities` accepts a domain `StepExecutor`. This keeps Temporal transport separate from business dispatch and allows the same domain executor to be tested without a running Temporal server.
