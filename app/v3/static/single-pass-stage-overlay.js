(() => {
  function byId(id) {
    try { return typeof $ === 'function' ? $(id) : document.getElementById(id); }
    catch (_) { return document.getElementById(id); }
  }

  function runningProjectId() {
    try { return String(current || '').trim(); }
    catch (_) { return ''; }
  }

  async function singlePassRunPhase() {
    const projectId = runningProjectId();
    if (!projectId) return;
    const input = String(byId('phaseInput')?.value || '').trim();
    try {
      // max_turns=1 is intentional. Current Xiaoduan ①-④ Skills each own one
      // terminal stage deliverable. The backend also rejects legacy
      // native_control_action=advance, so there is no hidden second model turn.
      await jpost(`/api/studio/projects/${projectId}/run-stage`, {
        input,
        max_turns: 1,
      });
      const box = byId('phaseInput');
      if (box) box.value = '';
      try { manualInputOpen = false; } catch (_) {}
      toast('已开始单次生成当前阶段');
      await refreshAll();
      if (
        snap?.project?.status === 'completed'
        && (snap.project.completed_stages || []).includes('04')
      ) {
        showView('make');
        setTimeout(
          () => byId('view-make')?.scrollIntoView({behavior:'smooth', block:'start'}),
          80,
        );
      } else {
        showView('create');
        byId('taskFocus')?.scrollIntoView({behavior:'smooth', block:'start'});
      }
    } catch (error) {
      toast(error?.message || String(error), true);
    }
  }

  function singlePassRenderJob() {
    const j = snap?.active_job;
    const target = byId('jobBox');
    if (!target) return;
    if (!j || j.status === 'input_required') {
      target.innerHTML = '';
      return;
    }
    if (['queued', 'running'].includes(j.status)) {
      target.innerHTML = `
        <div class="job">
          <b>${esc(j.message || '正在单次生成当前阶段…')}</b>
          <div class="meta">单次生产模式 · 不会后台自动推进或重复调用模型</div>
        </div>`;
      return;
    }
    if (j.status === 'failed') {
      target.innerHTML = `
        <div class="job failed">
          <b>本阶段生成失败</b>
          ${j.error ? `<div class="meta">${esc(j.error)}</div>` : ''}
          <div class="meta">已停止，不会自动重跑。请查看错误后再手工重试。</div>
        </div>`;
      return;
    }
    target.innerHTML = '';
  }

  // Classic-script workbench functions are Window bindings. Replacing them
  // here keeps the original page layout while retiring its old multi-turn
  // authoring behavior.
  window.runPhase = singlePassRunPhase;
  window.renderJob = singlePassRenderJob;
  window.__xiaoduanSinglePassAuthoring = {
    enabled: true,
    maxTurns: 1,
    legacyAutoAdvance: false,
  };
})();
