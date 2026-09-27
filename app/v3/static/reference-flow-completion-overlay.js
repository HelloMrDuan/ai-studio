(() => {
  function pid() {
    try { return String(current || '').trim(); } catch (_) { return ''; }
  }
  async function json(url, options = {}) {
    const response = await fetch(url, options);
    const text = await response.text();
    let body;
    try { body = JSON.parse(text); } catch (_) { body = {detail:text}; }
    if (!response.ok) throw new Error(body?.detail || text || `请求失败（${response.status}）`);
    return body;
  }
  function notify(message, bad = false) {
    if (typeof toast === 'function') toast(message, bad);
  }
  function phaseText(phase) {
    return ({
      face_anchor:'第 1/3 阶段：锁定脸、年龄、发型与气质',
      costume:'第 2/3 阶段：锁定服装、配色、鞋履、配饰与体型',
      turnaround:'第 3/3 阶段：使用已采用锁脸图 + 服装定装图生成三视图',
      ready:'角色参考资产包已完成'
    })[String(phase || '').toLowerCase()] || '';
  }
  async function loadRefs(projectId) {
    return json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references`);
  }
  function patchCards(refs) {
    const items = Array.isArray(refs?.items) ? refs.items : [];
    const cards = [...document.querySelectorAll('.v3-ref-card')];
    items.forEach((item, index) => {
      if (String(item?.entity_type || '').toLowerCase() !== 'character') return;
      const card = cards[index];
      if (!card) return;
      const phase = String(item?.generation_phase || '').toLowerCase();
      const phaseNode = card.querySelector('.v3-ref-phase b');
      if (phaseNode) phaseNode.textContent = phaseText(phase);
      const buttons = [...card.querySelectorAll('.v3-ref-actions button')];
      for (const button of buttons) {
        const onclick = button.getAttribute('onclick') || '';
        if (onclick.includes('v3GenerateReference')) {
          const label = phase === 'face_anchor' ? '生成锁脸图'
            : phase === 'costume' ? '生成服装定装图'
            : phase === 'turnaround' ? '生成三视图候选'
            : button.textContent;
          if (label && !button.disabled) button.textContent = label;
          if (label) button.dataset.v3ReferenceLabel = label;
        }
        if (onclick.includes('v3AdoptReference') && String(item?.candidate?.status || '').toLowerCase() === 'completed') {
          button.textContent = phase === 'face_anchor' ? '采用锁脸图并继续定装'
            : phase === 'costume' ? '采用定装图并继续三视图'
            : phase === 'turnaround' ? '采用三视图并完成资产包'
            : '采用候选';
        }
      }
    });
  }
  async function refreshCards(projectId) {
    try {
      if (typeof window.v3RefreshReferences === 'function') await window.v3RefreshReferences(false);
    } catch (_) {}
    try {
      const refs = await loadRefs(projectId);
      patchCards(refs);
      return refs;
    } catch (_) {
      return {items:[]};
    }
  }
  async function submitNext(projectId, entityId) {
    return json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references/${encodeURIComponent(entityId)}/generate`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({force:false})
    });
  }
  function installAdoptionFlow() {
    window.v3AdoptReference = async candidateId => {
      const projectId = pid();
      if (!projectId || !candidateId) return;
      try {
        const before = await loadRefs(projectId);
        const item = (before.items || []).find(row => String(row?.candidate?.candidate_id || '') === String(candidateId));
        if (!item) throw new Error('找不到当前候选所属的参考资产，请刷新后重试。');
        const phase = String(item.generation_phase || '').toLowerCase();
        const entityId = String(item.entity_id || '');
        await json(`/api/director/workbench/projects/${encodeURIComponent(projectId)}/candidates/${encodeURIComponent(candidateId)}/confirm`, {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({output_index:0})
        });

        if (phase === 'face_anchor') {
          await submitNext(projectId, entityId);
          notify('锁脸图已采用，已自动进入第 2/3 阶段：服装定装图生成。完成后仍需你确认采用。');
        } else if (phase === 'costume') {
          await submitNext(projectId, entityId);
          notify('服装定装图已采用，已自动进入第 3/3 阶段：三视图生成。完成后仍需你确认采用。');
        } else if (phase === 'turnaround') {
          notify('三视图已采用，角色参考资产包已完成；后续分镜使用正式三视图参考。');
        } else {
          notify('一致性参考图已采用。');
        }
        await refreshCards(projectId);
      } catch (error) {
        notify(error.message || String(error), true);
      }
    };
  }
  function installGenerateMessage() {
    const previous = window.v3GenerateReference;
    if (typeof previous !== 'function' || previous.__xiaoduanCompleteThreeStageFlow) return;
    const wrapped = async (...args) => {
      const result = await previous(...args);
      const projectId = pid();
      if (projectId) setTimeout(() => refreshCards(projectId), 80);
      return result;
    };
    wrapped.__xiaoduanCompleteThreeStageFlow = true;
    window.v3GenerateReference = wrapped;
  }
  async function boot() {
    installAdoptionFlow();
    installGenerateMessage();
    const projectId = pid();
    if (projectId) await refreshCards(projectId);
    setInterval(() => {
      const currentProject = pid();
      if (currentProject) loadRefs(currentProject).then(patchCards).catch(() => {});
    }, 2500);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
