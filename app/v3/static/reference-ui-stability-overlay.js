(() => {
  const CACHE_MS = 1800;
  let cachedProject = '';
  let cachedRefs = null;
  let cachedAt = 0;
  let loading = false;
  let patchQueued = false;

  function projectId() {
    try { return String(current || '').trim(); } catch (_) { return ''; }
  }

  function escHtml(value) {
    if (typeof esc === 'function') return esc(value);
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function phaseInfo(item) {
    const phase = String(item?.generation_phase || '').toLowerCase();
    if (phase === 'face_anchor') return {
      text: '第 1/3 阶段：锁定脸、年龄、发型与气质',
      generate: '生成锁脸图',
      adopt: '采用锁脸图并继续定装',
    };
    if (phase === 'costume') return {
      text: '第 2/3 阶段：锁定服装、配色、鞋履、配饰与体型',
      generate: '生成服装定装图',
      adopt: '采用定装图并继续三视图',
    };
    if (phase === 'turnaround') return {
      text: '第 3/3 阶段：使用已采用锁脸图和定装图生成三视图',
      generate: '生成三视图候选',
      adopt: '采用三视图并完成资产包',
    };
    if (phase === 'ready') return {
      text: '角色参考资产包已完成',
      generate: '重新生成参考资产',
      adopt: '采用候选',
    };
    return null;
  }

  function patchFromCache() {
    const items = Array.isArray(cachedRefs?.items) ? cachedRefs.items : [];
    const cards = [...document.querySelectorAll('#v3ReferenceGrid .v3-ref-card')];
    if (!items.length || !cards.length) return;

    items.forEach((item, index) => {
      if (item?.entity_type !== 'character') return;
      const card = cards[index];
      if (!card) return;
      const info = phaseInfo(item);
      if (!info) return;

      let phase = card.querySelector('.v3-ref-phase');
      if (!phase) {
        phase = document.createElement('div');
        phase.className = 'v3-ref-phase';
        const prompt = card.querySelector('.v3-ref-prompt');
        if (prompt?.parentNode) prompt.parentNode.insertBefore(phase, prompt);
        else card.appendChild(phase);
      }
      const phaseHtml = `<b>${escHtml(info.text)}</b>`;
      if (phase.innerHTML !== phaseHtml) phase.innerHTML = phaseHtml;

      const candidate = item?.candidate || {};
      const cstate = String(candidate.status || '').toLowerCase();
      card.querySelectorAll('.v3-ref-actions button').forEach(button => {
        const onclick = button.getAttribute('onclick') || '';
        if (onclick.includes('v3GenerateReference')) {
          button.dataset.v3ReferenceLabel = info.generate;
          if (!button.disabled && button.textContent !== info.generate) button.textContent = info.generate;
        }
        if (onclick.includes('v3AdoptReference') && cstate === 'completed' && button.textContent !== info.adopt) {
          button.textContent = info.adopt;
        }
      });
    });
  }

  async function refresh(force = false) {
    const pid = projectId();
    if (!pid || loading) {
      patchFromCache();
      return;
    }
    const now = Date.now();
    if (!force && cachedProject === pid && cachedRefs && now - cachedAt < CACHE_MS) {
      patchFromCache();
      return;
    }
    loading = true;
    try {
      const response = await fetch(`/api/v3/studio/projects/${encodeURIComponent(pid)}/references`);
      if (!response.ok) return;
      const body = await response.json();
      cachedProject = pid;
      cachedRefs = body;
      cachedAt = Date.now();
      patchFromCache();
    } catch (_) {
    } finally {
      loading = false;
    }
  }

  function schedulePatch(force = false) {
    if (patchQueued) return;
    patchQueued = true;
    queueMicrotask(() => {
      patchQueued = false;
      patchFromCache();
      refresh(force);
    });
  }

  const originalRenderAll = typeof window.renderAll === 'function' ? window.renderAll : null;
  if (originalRenderAll) {
    window.renderAll = function(...args) {
      const result = originalRenderAll.apply(this, args);
      schedulePatch(false);
      return result;
    };
  }

  const originalRefreshReferences = typeof window.v3RefreshReferences === 'function' ? window.v3RefreshReferences : null;
  if (originalRefreshReferences) {
    window.v3RefreshReferences = async function(...args) {
      const result = await originalRefreshReferences.apply(this, args);
      await refresh(true);
      return result;
    };
  }

  const panelObserver = new MutationObserver(() => schedulePatch(false));
  const observe = () => {
    const grid = document.getElementById('v3ReferenceGrid');
    if (grid) panelObserver.observe(grid, {childList:true, subtree:true});
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      observe();
      refresh(true);
    }, {once:true});
  } else {
    observe();
    refresh(true);
  }
})();
