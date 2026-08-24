# Stage04 / Production Task State Machine

Audit baseline: `2.39.6.2-stage04-narrative-lineage-closure`.

## Stage04 rebuild

| State | Allowed next | Recovery next | Meaning / invariant |
|---|---|---|---|
| `starting` | `warming`, `failed`, `cancelled` | `failed` | Durable reservation exists before the first awaited preflight operation. |
| `warming` | `queued`, `failed`, `cancelled` | `failed` | Qwen workspace is being acquired and verified. |
| `queued` | `running`, `failed`, `cancelled` | `failed` | Background worker has been scheduled. |
| `running` | `repairing`, `auditing`, `persisting`, `failed`, `cancelled` | `failed` | Scene pipeline is active; old canonical remains readable. |
| `repairing` | `running`, `auditing`, `failed`, `cancelled` | `failed` | A scoped repair is active; no canonical switch is allowed. |
| `auditing` | `running`, `persisting`, `failed`, `cancelled` | `failed` | Audit is checking the candidate that will be persisted. |
| `persisting` | `completed`, `failed` | transaction recovery then `failed` | Durable transaction journal exists until all canonical writes finish. |
| `completed` | none | none | Persistence, canonical switch, reloadable task metrics and transaction cleanup all finished. |
| `failed` | none | none | Current canonical is unchanged or a durable journal has restored it. |
| `cancelled` | none | none | No candidate may become canonical. |
| `stale` | none | none | Historical task record only; never treated as active. |

Illegal transitions include terminal-to-active, `running` directly to `completed`, and any transition to `completed` before persistence and canonical readback. An active record recovered after process restart is an orphan: recover the Stage04 transaction first, then record `failed` with a restart reason.

## Production candidate task

| State | Allowed next | Canonical effect |
|---|---|---|
| `queued` | `running`, `failed`, `cancelled` | none |
| `running` | `completed`, `failed`, `cancelled` | candidate only |
| `completed` | confirmation or supersession | still candidate until explicit confirmation |
| `failed` / `cancelled` | none | never selectable |
| confirmed | superseded/stale | publish only when the candidate fingerprint equals the current formal Shot fingerprint |

## Concurrency rules

- One active Stage04 rebuild per project. Reservation is durable and occurs before Qwen preflight.
- Stage04 rebuild is rejected while the same project has active image/video candidates.
- Stage05 and Stage06 reads are rejected while a Stage04 rebuild is active.
- Process restart converts orphan active task records to `failed` after transaction recovery.
- GPU leases are scoped by `try/finally`; an exception, timeout or cancellation must release the lease.
