# Runtime Reachability / Dead Code Matrix

| symbol | defined_at | referenced_by | runtime_reachable | effective_definition | shadowed_by | legacy_only | test_reachable | api_reachable | safe_to_remove |
|---|---:|---|---|---:|---|---|---|---|---|
| `_studio_v2371_rebuild_stage04` | 3989, 25803 | rebuild route background task | yes | 25803 | 25803 | first definition yes | yes | yes | no |
| `stage04_v238_runtime.rebuild` | runtime:7600 | final wrapper | yes | 7600 | — | no | yes | yes | no |
| `_studio_v2371_generate_batch` | 3823,7794,11229,13284,16561 | alias chain/runtime env | yes | 16561 | later definitions | no | yes | indirect | no |
| `_studio_v2371_validate_rows` | 3851,7966,9792,10484,12908,15830 | alias chain/runtime env | yes | 15830 | later definitions | no | yes | indirect | no |
| `_studio_v2371_audit_batch` | 3941,7899,13122,16389 | runtime env | yes | 16389 | later definitions | no | yes | indirect | no |
| `_ORIGINAL_V2371_VALIDATE_ROWS` | alias at 7963 | later validator | yes | captured 3851 | — | compatibility | yes | indirect | no |
| `_V2371E_PREVIOUS_VALIDATE_ROWS` | 9789 | wrapper | yes | captured 7966 | — | compatibility | yes | indirect | no |
| `_V2371G_PREVIOUS_VALIDATE_ROWS` | 10479 | wrapper | yes | captured 9792 | — | compatibility | yes | indirect | no |
| `_V2372_R1_PREVIOUS_GENERATE_BATCH` | 13279 | wrapper | yes | captured 11229 | — | compatibility | yes | indirect | no |
| `_V2372_R2_PREVIOUS_GENERATE_BATCH` | 16556 | final wrapper | yes | captured 13284 | — | compatibility | yes | indirect | no |
| `_studio_stage04_scene_source` | 3393,11417,13332,16808 | runtime env | yes | 16808 | earlier defs | no | yes | indirect | no |
| `_studio_v2372_generate_chunk_beats` | 12332,14247,17461,18459,21381,22600,24809 | only shadowed ensure at 12505 calls the symbol | no on effective rebuild path | 24809 | later defs | compatibility/shadowed | possible direct | no | no broad removal this round |
| `_studio_v2374_group_all` | 24574,25682 | Beat ensure | yes | 25682 | 25682 | no | yes | indirect | no |
| `_studio_shot_contract_fingerprint` | 3739,6159 | Stage05/06 | yes | 6159 baseline | 6159 | second implementation was narrower | yes | yes | no |
| `_studio_compile_video_start_contract` | 6572 | no caller | no | 6572 | — | yes | possible direct | no | not this round |
| `_studio_video_contract_prompt_asset` | 6763 | no caller | no | 6763 | — | yes | possible direct | no | not this round |
| `_studio_stage04_generate_detailed` | ~4449 | generic Stage04 run job | yes at baseline | legacy | — | yes but reachable | yes | yes | no; first disable reachability |
| `_studio_stage04_finalize` | 4551 | Stage04 approval | yes | 4551 | — | compatibility gate | yes | yes | no |
| `_production_asset_file` | 1386,7587 | candidate execution | yes | 7587 | 7587 | first dead | yes | yes | no |
| `_wb_validate_media_asset` | 2460,7559 | video/media candidate execution | yes | 7559 | 7559 | first shadowed | yes | yes | no |
| `ProductionAssetService.context_manifest` | service:931 | Director context | yes | 931 | — | no | yes | indirect | no |

`safe_to_remove=no` means this preflight intentionally did not perform broad
historical cleanup. Shadowed code can still be captured by aliases or used by
tests; removal needs a separate migration proof.

## V2.39.6.3 reachability delta

| symbol | final target line | runtime status after patch | proof |
|---|---:|---|---|
| `_studio_v2371_rebuild_stage04` | 4069, 25956 | final definition effective; first definition shadowed | runtime import + AST last-definition equality test |
| `stage04_v238_runtime.rebuild` | 7753 | effective | final wrapper delegates through `globals()` |
| `_studio_v2371_generate_batch` | 16714 | effective alias wrapper | runtime import inspection |
| `_studio_v2371_validate_rows` | 15983 | effective alias wrapper | runtime import inspection |
| `_studio_v2371_audit_batch` | 16542 | effective alias wrapper | runtime import inspection |
| `_studio_stage04_scene_source` | 16961 | effective | runtime import inspection |
| `_studio_v2372_generate_chunk_beats` | 24809 | final symbol, not effective rebuild callee | caller search + final ensure at 14419 |
| `_studio_v2371b_ensure_scene_beats` | 14419 | effective Narrative pipeline | runtime `scene_shots` global lookup + last-definition inspection |
| `_studio_v2374_group_all` | 25835 | effective | runtime import inspection |
| `_studio_shot_contract_fingerprint` | 6310 | effective complete contract | mutation test for state/prompt/evidence/version |
| `_studio_stage04_generate_detailed` | legacy | no longer API-reachable for generation | route and worker reject non-approval Stage04 input |

The historical aliases remain runtime-reachable where the effective wrappers
explicitly call them. They are not safe to delete in this cumulative patch.
