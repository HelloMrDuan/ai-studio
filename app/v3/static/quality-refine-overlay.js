(() => {
  const MARK = 'data-v3-quality-ready';

  function currentProject() {
    try { return String(current || '').trim(); } catch (_) { return ''; }
  }
  function rows() {
    try { return Array.isArray(snap?.candidates) ? snap.candidates : []; } catch (_) { return []; }
  }
  function notify(message, bad = false) {
    if (typeof toast === 'function') toast(message, bad);
    else alert(message);
  }
  function candidateId(card) {
    const button = Array.from(card.querySelectorAll('button')).find(btn =>
      String(btn.getAttribute('onclick') || '').includes('adoptShotCandidate(')
      || String(btn.getAttribute('onclick') || '').includes('rejectShotCandidate(')
    );
    const text = String(button?.getAttribute('onclick') || '');
    const match = text.match(/(?:adoptShotCandidate|rejectShotCandidate)\(['"]([^'"]+)['"]\)/);
    return match ? match[1] : '';
  }
  function candidateById(id) {
    return rows().find(item => String(item?.candidate_id || '') === String(id || '')) || null;
  }

  async function refine(candidateId) {
    const project = currentProject();
    if (!project || !candidateId) return;
    try {
      const response = await fetch(`/api/v3/studio/projects/${encodeURIComponent(project)}/image-candidates/${encodeURIComponent(candidateId)}/refine`, {method:'POST'});
      const text = await response.text();
      let body;
      try { body = JSON.parse(text); } catch (_) { body = text; }
      if (!response.ok) throw new Error(body?.detail || body || `请求失败：${response.status}`);
      notify(`高质量精修已提交：${body.quality_tier || 'B'}级 / ${body.final_steps || ''}步。完成后仍需你预览并采用。`);
      if (typeof refreshAll === 'function') await refreshAll();
      if (typeof showView === 'function') showView('make');
    } catch (error) {
      notify(error.message || String(error), true);
    }
  }
  window.v3RefineImageCandidate = refine;

  function makeSmartSelectorReal() {
    const select = document.getElementById('shotImageModel');
    if (!select || select.dataset.v3SmartQuality === '1') return;
    select.innerHTML = '<option value="smart">智能质量（自动选择生成成本）</option>';
    select.value = 'smart';
    select.dataset.v3SmartQuality = '1';
    select.disabled = true;
    const parent = select.closest('.field');
    const label = parent?.querySelector('label');
    if (label) label.textContent = '画面质量策略';
  }

  function decorate() {
    makeSmartSelectorReal();
    document.querySelectorAll('.candidateCard').forEach(card => {
      const id = candidateId(card);
      if (!id) return;
      const row = candidateById(id);
      if (!row || String(row.capability || '').toLowerCase() !== 'image' || String(row.status || '').toLowerCase() !== 'completed') return;
      const params = row.params && typeof row.params === 'object' ? row.params : {};
      const stage = String(params.quality_stage || 'preview').toLowerCase();
      if (stage === 'final') {
        if (!card.querySelector('.v3-quality-badge')) {
          const badge = document.createElement('div');
          badge.className = 'timelineCandidateNote ok v3-quality-badge';
          badge.textContent = `高质量精修候选 · ${String(params.quality_tier || 'B')}级`;
          const buttons = card.querySelector('.candidateButtons');
          card.insertBefore(badge, buttons || null);
        }
        card.setAttribute(MARK, 'final');
        return;
      }
      if (card.getAttribute(MARK)) return;
      const actions = card.querySelector('.candidateButtons');
      if (!actions) return;
      const button = document.createElement('button');
      button.className = 'btn good';
      button.type = 'button';
      button.textContent = '同种子高质量精修';
      button.addEventListener('click', () => refine(id));
      actions.insertBefore(button, actions.firstChild);
      const adopt = Array.from(actions.querySelectorAll('button')).find(btn => String(btn.getAttribute('onclick') || '').includes('adoptShotCandidate('));
      if (adopt) {
        adopt.textContent = '直接采用预览';
        adopt.title = '可以直接采用，但建议关键镜头先做高质量精修';
      }
      const note = document.createElement('div');
      note.className = 'timelineCandidateNote';
      note.textContent = `快速预览候选 · ${String(params.quality_tier || 'B')}级；精修会保留同一随机种子并提高生成预算，不会自动覆盖此候选。`;
      card.insertBefore(note, actions);
      card.setAttribute(MARK, 'preview');
    });
  }

  function boot() {
    decorate();
    const observer = new MutationObserver(() => setTimeout(decorate, 20));
    observer.observe(document.body, {childList:true, subtree:true});
    setInterval(decorate, 1200);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
