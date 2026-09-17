(() => {
  function projectSnapshot() {
    try { return snap?.project || null; } catch (_) { return null; }
  }

  function stageIndex(stage) {
    const value = Number(String(stage || '').replace(/^0+/, ''));
    return Number.isFinite(value) && value >= 1 && value <= 4 ? value - 1 : -1;
  }

  function patchStep(data) {
    const project = projectSnapshot();
    if (!project || !data?.supported) return;
    const stage = String(data.stage || project.current_stage || '');
    const index = stageIndex(stage);
    if (index < 0) return;
    const steps = Array.from(document.querySelectorAll('#progress .step'));
    const step = steps[index];
    if (!step) return;
    const state = step.querySelector('em.stagePercent, em');
    const completed = new Set((project.completed_stages || []).map(String));

    if (completed.has(stage)) {
      step.classList.remove('current', 'ready');
      step.classList.add('done');
      if (state && state.textContent !== '100%') state.textContent = '100%';
      return;
    }

    // A native ①-④ production call can be fully complete before the user
    // confirms the stage. That is not "处理中" and must not look active.
    if (data.status === 'completed' && String(project.current_stage || '') === stage) {
      step.classList.remove('current', 'done');
      step.classList.add('ready');
      if (state && state.textContent !== '待确认') state.textContent = '待确认';
    }
  }

  function onSharedProgress(event) {
    patchStep(event?.detail || window.__v3StageProgressLast || null);
  }

  function boot() {
    // stage-progress-overlay.js owns the only stage-progress network poller.
    // Reuse its cached/event state so this overlay never doubles the same API
    // traffic and never races the main progress panel while the user clicks.
    if (window.__v3StageProgressLast) patchStep(window.__v3StageProgressLast);
    window.addEventListener('v3:stage-progress', onSharedProgress);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
