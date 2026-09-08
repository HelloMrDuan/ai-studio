# Xiaoduan Studio V3 Architecture

## Product boundary

The V3 product name is **xiaoduan映画 / Xiaoduan Studio**. The legacy `chuanzhang-ai-shijie-workflow` and fixed Stage01→Stage04 orchestration are not V3 authorities. Legacy code remains readable during migration but must not become a dependency of new V3 domain modules.

## Core shape

```text
User
  ↓
Creative Director (thin planner)
  ↓
Skill Registry
  ├─ screenplay
  ├─ character
  ├─ world
  ├─ storyboard
  ├─ cinematography
  ├─ continuity
  ├─ image_direction
  ├─ video_direction
  ├─ audio_direction
  └─ quality_audit
  ↓
Production Workflow (durable execution)
  ↓
Provider Gateway (capability driven)
  ↓
Remote API / Local OpenAI-compatible / ComfyUI / Local workers
  ↓
Resource / Entity / Versioned candidates
```

## V3 invariants introduced in the foundation

1. **No fixed 01→04 route.** A director supplies the professional skills required for the current goal; `SkillRegistry` expands only required hard dependencies.
2. **Local and remote models are peers.** Provider routing is based on required capabilities and an explicit selection never silently falls back to another provider.
3. **Project-scope context resolves once.** A locked resolution is reused by later shots; unresolved cultural context falls back to the project default (`Chinese`) only after explicit and contextual evidence are exhausted.
4. **Continuity is entity-ID based.** Persistent characters, creatures, props and locations are tracked as entities. Shot state cannot mutate immutable identity fields.
5. **Adoption creates canonical references.** Generation results are not automatically canonical. Only adopted resources may become persistent references for downstream shots.
6. **Visual success is more than HTTP success.** Required entity visibility is a separate audit concern and missing entities fail closed.
7. **Upstream work is continuously reviewed.** `waooAI/waoowaoo` and `harry0703/MoneyPrinterTurbo` baselines are tracked and a scheduled GitHub Action reports relevant upstream changes.

## Migration strategy on the V3 branch

The branch is a Big Bang product refactor, but implementation is built behind a clean V3 module boundary until a real end-to-end path is ready. This is not legacy compatibility architecture: the isolation exists so the old runtime cannot contaminate new domain ownership while development and tests continue.

The legacy Stage04 runtime will be audited and decomposed into four categories:

- Storyboard semantics: Beat, evidence, source fact and strict temporal rules.
- Deterministic validators: rules that can be computed without a model.
- Continuity / visual contracts: entity and reference obligations.
- Historical repair branches and workaround code: removed unless they express a still-valid domain invariant.

## Provider boundary

Business code asks for capabilities such as `structured_output`, `image_generation`, `identity_reference`, `multi_reference`, `first_frame`, `video_generation` or `tts`. A provider adapter declares whether it can satisfy those capabilities.

Supported deployment classes are first-class:

- remote API
- local HTTP service
- local OpenAI-compatible server (Qwen/vLLM/SGLang/LMDeploy/llama.cpp/compatible gateways)
- local ComfyUI
- local worker
- internal service

No V3 skill may hard-code a provider URL such as `localhost:6006` or a vendor API URL.

## Next migration slices

- Persist V3 Resource / Entity / Candidate versions in the project store.
- Wrap the existing local Qwen path as an OpenAI-compatible/local provider adapter.
- Wrap ComfyUI and H3 behind capability declarations instead of business-layer provider checks.
- Introduce durable workflow execution and separate infrastructure retry from semantic recovery.
- Port suitable MIT media services from MoneyPrinterTurbo (TTS, subtitle, BGM, video composition) with attribution.
- Replace legacy UI branding and fixed stage navigation once the V3 E2E path is runnable.
