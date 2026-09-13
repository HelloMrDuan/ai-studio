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
  function installStyles() {
    if (document.getElementById('v3CharacterPackageStyles')) return;
    const style = document.createElement('style');
    style.id = 'v3CharacterPackageStyles';
    style.textContent = `
      .v3-char-package{margin:8px 0 10px;padding:8px 10px;border:1px solid #29415f;border-radius:8px;background:#0b1626;font-size:11px;color:#aebfd5}
      .v3-char-package b{color:#e3edfb}
      .v3-char-package-steps{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
      .v3-char-package-steps span{padding:3px 7px;border:1px solid #30455f;border-radius:999px;background:#0d1a2c}
      .v3-char-package-steps span.done{border-color:#236b4b;color:#9fe0be;background:#0e241c}
      .v3-char-package-steps span.current{border-color:#75591d;color:#f2d589;background:#2b210c}
    `;
    document.head.appendChild(style);
  }
  function phaseLabel(phase) {
    return ({
      face_anchor:'阶段 1/3：锁定脸、年龄、发型与气质',
      costume:'阶段 2/3：锁定服装、鞋履、配饰与体型',
      turnaround:'阶段 3/3：使用脸部 + 服装双参考生成三视图',
      ready:'角色参考资产包已完成'
    })[phase] || '';
  }
  function patchCard(card, item) {
    if (!card || item?.entity_type !== 'character') return;
    const phase = String(item.generation_phase || '').toLowerCase();
    let panel = card.querySelector('.v3-char-package');
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'v3-char-package';
      const prompt = card.querySelector('.v3-ref-prompt');
      if (prompt?.parentNode) prompt.parentNode.insertBefore(panel, prompt);
      else card.appendChild(panel);
    }
    const face = !!item.face_anchor_ready;
    const costume = !!item.costume_ready;
    const turnaround = !!item.ready;
    const packageReady = !!item.reference_package_asset_id && turnaround;
    panel.innerHTML = `
      <b>${phaseLabel(phase)}</b>
      <div class="v3-char-package-steps">
        <span class="done">身份 ✓</span>
        <span class="${face ? 'done' : phase === 'face_anchor' ? 'current' : ''}">脸部 ${face ? '✓' : '待确认'}</span>
        <span class="${costume ? 'done' : phase === 'costume' ? 'current' : ''}">服装 ${costume ? '✓' : '待确认'}</span>
        <span class="${turnaround ? 'done' : phase === 'turnaround' ? 'current' : ''}">三视图 ${turnaround ? '✓' : '待确认'}</span>
        <span class="${packageReady ? 'done' : ''}">资产包 ${packageReady ? '✓' : '待组装'}</span>
      </div>`;

    const buttons = [...card.querySelectorAll('.v3-ref-actions button')];
    const candidateState = String(item?.candidate?.status || '').toLowerCase();
    for (const button of buttons) {
      const onclick = button.getAttribute('onclick') || '';
      if (onclick.includes('v3GenerateReference')) {
        const label = phase === 'face_anchor' ? '生成锁脸图'
          : phase === 'costume' ? '生成服装定装图'
          : phase === 'turnaround' ? '生成三视图候选'
          : button.textContent;
        if (!button.disabled && label) button.textContent = label;
        if (label) button.dataset.v3ReferenceLabel = label;
      }
      if (onclick.includes('v3AdoptReference') && candidateState === 'completed') {
        button.textContent = phase === 'face_anchor' ? '采用锁脸图'
          : phase === 'costume' ? '采用服装定装图'
          : phase === 'turnaround' ? '采用三视图'
          : '采用候选';
      }
    }
  }
  async function patchAll() {
    const projectId = pid();
    if (!projectId) return;
    try {
      const refs = await json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references`);
      const cards = [...document.querySelectorAll('.v3-ref-card')];
      (refs.items || []).forEach((item, index) => patchCard(cards[index], item));
    } catch (_) {}
  }
  function installRefreshWrapper() {
    const original = window.v3RefreshReferences;
    if (typeof original !== 'function' || original.__characterPackageWrapped) return;
    const wrapped = async (...args) => {
      const result = await original(...args);
      await patchAll();
      return result;
    };
    wrapped.__characterPackageWrapped = true;
    window.v3RefreshReferences = wrapped;
  }
  function installAdoption() {
    window.v3AdoptReference = async candidateId => {
      const projectId = pid();
      if (!projectId || !candidateId) return;
      try {
        const before = await json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references`);
        const item = (before.items || []).find(row => String(row?.candidate?.candidate_id || '') === String(candidateId));
        const phase = String(item?.generation_phase || '').toLowerCase();
        await json(`/api/director/workbench/projects/${encodeURIComponent(projectId)}/candidates/${encodeURIComponent(candidateId)}/confirm`, {
          method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({output_index:0}),
        });
        if (phase === 'face_anchor') {
          notify('锁脸图已采用。请检查后点击“生成服装定装图”，不会自动跳到下一阶段。');
        } else if (phase === 'costume') {
          notify('服装定装图已采用。请检查后点击“生成三视图候选”。');
        } else if (phase === 'turnaround') {
          notify('三视图已采用，角色参考资产包已完成，后续分镜使用正式三视图参考。');
        } else {
          notify('一致性参考图已采用。');
        }
        if (typeof window.v3RefreshReferences === 'function') await window.v3RefreshReferences(false);
        else await patchAll();
      } catch (error) {
        notify(error.message || String(error), true);
      }
    };
  }
  async function boot() {
    installStyles();
    installRefreshWrapper();
    installAdoption();
    await patchAll();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
