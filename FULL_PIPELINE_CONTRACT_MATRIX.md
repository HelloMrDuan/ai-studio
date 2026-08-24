# Full Pipeline Contract Matrix

The table records semantic ownership. “Repair” means a scoped, revalidated
patch; it never permits a downstream stage to invent narrative facts.

| Producer | Consumer | Object / field | Required / nullable | Semantic owner | Can repair / override | Persistence / reload validation | Version / failure |
|---|---|---|---|---|---|---|---|
| Source API | continuity, Stage01 | Scene source text, source_start/end | required / no | source asset | no downstream override | production graph text file + exact offsets | fail closed if source absent |
| continuity | Stage04 | Scene id, episode id, ordered range | required / no | continuity | structural range repair only | continuity JSON; range guard on use | invalid range fails |
| Stage04 extraction | classification/grouping | Anchor id, text, source_start/end | required / no | source lineage | Qwen classification; offsets immutable | transient then Beat provenance | missing/duplicate fails |
| Stage04 grouping | Shot pipeline | Beat order, summary, state_change, evidence ids/text/spans | required / no | Qwen semantics + source offsets | scoped grouping repair; evidence closure immutable after validation | continuity transient; audit payload includes spans | narrative audit fail closed |
| Stage04 Shot | Stage05 | shot_id, scene_id, beat orders | required / no | Stage04 | no Stage05 override | continuity JSON + entity mirror | strict-shot-v2 |
| Stage04 Shot | Stage05 | source_evidence ids/text/spans | required / no | Stage04/source | evidence-locked repair only | `source_provenance`; exact reload compare | missing/unknown fails |
| Stage04 Shot | Stage05 | representative_state | required / no | Stage04 Qwen | scoped audited repair | continuity + entity mirror | empty fails |
| Stage04 Shot | Stage05 | video_start_state / video_end_state | required / no | Stage04 Qwen | scoped audited repair | continuity + entity mirror | empty/order failure fails |
| deterministic compiler | image API | image_prompt | required / no | representative_state | downstream read-only | continuity, prompt asset with fingerprint | must equal representative_state |
| deterministic compiler | image API | video_start_prompt | required / no | video_start_state | downstream read-only | continuity, prompt asset with fingerprint | must equal video_start_state |
| deterministic compiler | video API | video_prompt | required / no | start→end states | downstream read-only | continuity, prompt asset with fingerprint | exact start/end form |
| Stage04 | Stage05/H3 | duration_seconds | required / no | Stage04 Qwen planner | no Stage05 semantic override | continuity + asset metadata | numeric 0.8–20s; H3 frame derivation |
| Stage04 | Stage05 | visible character/prop entity ids | list / empty allowed | Stage04 Qwen within allowed ids | audited repair may remove/add only allowed ids | continuity + entity relations | unknown id fails |
| Stage04 commit | snapshot/Stage05 | Storyboard master | required / no | Stage04 | versioned replacement after validation | production graph + content SHA + transaction recovery | runtime + contract version |
| Stage05 route | image worker | Image Task params | required / no | Stage04 prompt; user owns rendering knobs | no semantic compile in locked mode | TaskStore task.json | failed/cancelled never canonical |
| image worker | candidate confirm | Image candidate URL/task id | required on completion | media worker | user selects output only | candidate JSON + TaskStore | manual confirm required |
| confirm | Stage05/06 | Image canonical | ready/current/non-stale | Stage05 publication | newer validated candidate supersedes | production graph atomic file | fingerprint must match formal Shot |
| Stage05 route | video worker | Video Task prompt/start image/duration | required / no | Stage04 + adopted start image | rendering profile only | TaskStore + candidate JSON | lineage mismatch fails |
| confirm | Stage06 | Video canonical | ready/current/non-stale | Stage05 publication | newer validated candidate supersedes | graph; parent lineage | exact Shot fingerprint required |
| Stage06 | export/readback | ordered video assets | one per formal Shot | Stage04 order | no reorder | input_manifest in final asset | missing/duplicate/stale fails |
| Stage06 | final reader | Final Asset | ready/current/non-stale | Stage06 | new assembly supersedes | graph + file + ffprobe metadata | parent provenance required |

Global rules: identity and lineage fields cannot be repaired by replacement;
blank/null candidates cannot erase valid semantic text; candidate assets are
never canonical before explicit confirmation; runtime and contract versions are
part of the Shot fingerprint in V2.39.6.3.
