(() => {
  const POLL_MS = 900;
  let busy = false;

  function projectId() {
    try { return String(current || '').trim(); } catch (_) { return ''; }
  }

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
      if (state) state.textContent = '100%';
      return;
    }

    // A native ①-④ production call can be fully complete before the user
    // confirms the stage.  That is not "处理中" and must not look active.
    if (data.status === 'completed' && String(project.current_stage || '') === stage) {
      step.classList.remove('current', 'done');
      step.classList.add('ready');
      if (state) state.textContent = '待确认';
    }
  }

  async function refresh() {
    if (busy) return;
    const id = projectId();
    if (!id) return;
    busy = true;
    try {
      const response = await fetch(`/api/v3/studio/projects/${encodeURIComponent(id)}/stage-progress`, {cache: 'no-store'});
      if (!response.ok) return;
      patchStep(await response.json());
    } catch (_) {
      // Visual correction must never interrupt production.
    } finally {
      busy = false;
    }
  }

  function boot() {
    refresh();
    window.setInterval(refresh, POLL_MS);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
