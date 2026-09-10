(() => {
  const ACTIVE = new Set(['queued','switching_gpu','running','generating','submitting']);
  const POLL_MS = 1800;
  const REFS_REFRESH_MS = 5000;
  const HIDDEN_POLL_MS = 8000;
  const localPending = new Map();
  let pollTimer = null;
  let refreshing = false;
  let lastRefs = null;
  let lastRefsAt = 0;
  let lastProjectId = '';
  let hadActive = false;
  let terminalSyncing = false;

  function pid() {
    try { return String(current || '').trim(); } catch (_) { return ''; }
  }
  function escHtml(value) {
    if (typeof esc === 'function') return esc(value);
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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
    if (document.getElementById('v3ReferenceUxStyles')) return;
    const style = document.createElement('style');
    style.id = 'v3ReferenceUxStyles';
    style.textContent = `
      .v3-ref-live{margin:8px 0 10px;padding:9px 10px;border:1px solid #2b4263;border-radius:9px;background:#0c1728;min-height:54px;contain:layout paint}
      .v3-ref-live-head{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:12px;color:#c8d7eb}
      .v3-ref-live-head b{font-size:12px;color:#eaf2ff}
      .v3-ref-live-bar{height:7px;background:#08111d;border-radius:999px;overflow:hidden;margin-top:7px}
      .v3-ref-live-bar i{display:block;height:100%;background:linear-gradient(90deg,#2f6fed,#56b4ff);border-radius:999px;transition:width .25s ease}
      .v3-ref-live-msg{margin-top:6px;color:#8fa6c4;font-size:11px;line-height:1.45;min-height:16px}
      .v3-ref-batch{margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;font-size:11px;color:#aabbd1}
      .v3-ref-batch span{border:1px solid #2b3d57;border-radius:999px;padding:4px 8px;background:#0b1524}
    `;
    document.head.appendChild(style);
  }

  function setButtonBusy(entityId, busy) {
    document.querySelectorAll('.v3-ref-card').forEach(card => {
      const button = [...card.querySelectorAll('button')].find(btn => (btn.getAttribute('onclick') || '').includes(`'${entityId}'`));
      if (!button) return;
      if (!button.dataset.v3ReferenceLabel) button.dataset.v3ReferenceLabel = button.textContent || '生成候选';
      button.disabled = !!busy;
      button.textContent = busy ? '任务提交中…' : button.dataset.v3ReferenceLabel;
    });
  }

  function itemState(item, submissions) {
    const candidate = item?.candidate || {};
    const cstate = String(candidate.status || '').toLowerCase();
    const related = submissions.find(row => (row.entity_ids || []).includes(String(item.entity_id || '')))
      || submissions.find(row => String(row.target_asset_id || '') === String(item.target_asset_id || ''));
    const rstate = String(related?.status || '').toLowerCase();
    const lstate = localPending.get(String(item.entity_id || ''));

    // The lightweight submission endpoint is fresher than the heavy references
    // payload while work is active. A stale "queued" card must not hide a newer
    // provider state such as switching_gpu/running.
    if (related && ACTIVE.has(rstate) && !['completed','failed'].includes(cstate)) {
      return {
        status: rstate,
        progress: Number(related.progress || candidate.progress || 0),
        message: related.message || candidate.message || '参考图任务已进入后台队列',
        error: related.error || candidate.error || '',
      };
    }
    if (cstate) {
      return {
        status: cstate,
        progress: Number(candidate.progress || related?.progress || 0),
        message: candidate.message || related?.message || '',
        error: candidate.error || related?.error || '',
      };
    }
    if (related && rstate) {
      return {
        status: rstate,
        progress: Number(related.progress || 0),
        message: related.message || '',
        error: related.error || '',
      };
    }
    if (lstate) return {status:'submitting',progress:1,message:'正在创建参考图生成任务',error:''};
    return {status:'',progress:0,message:'',error:''};
  }

  function statusText(state) {
    return ({
      submitting:'提交中', queued:'排队中', switching_gpu:'准备模型中',
      running:'模型生成中', generating:'模型生成中', completed:'候选已生成', failed:'生成失败'
    })[state] || state || '';
  }

  function patchLiveBlock(card, item, state) {
    const entityId = String(item?.entity_id || '');
    const status = String(state.status || '').toLowerCase();
    let block = card.querySelector(`.v3-ref-live[data-entity-id="${CSS.escape(entityId)}"]`);
    if (!status || item.ready) {
      if (block) block.remove();
      return;
    }
    if (!block) {
      block = document.createElement('div');
      block.className = 'v3-ref-live';
      block.dataset.entityId = entityId;
      block.innerHTML = `
        <div class="v3-ref-live-head"><b></b><span></span></div>
        <div class="v3-ref-live-bar"><i></i></div>
        <div class="v3-ref-live-msg"></div>`;
      const prompt = card.querySelector('.v3-ref-prompt');
      if (prompt?.parentNode) prompt.parentNode.insertBefore(block, prompt);
      else card.appendChild(block);
    }
    const progress = Math.max(0, Math.min(100, Number(state.progress || 0)));
    const title = block.querySelector('.v3-ref-live-head b');
    const pct = block.querySelector('.v3-ref-live-head span');
    const bar = block.querySelector('.v3-ref-live-bar');
    const fill = block.querySelector('.v3-ref-live-bar i');
    const msg = block.querySelector('.v3-ref-live-msg');
    const titleText = statusText(status);
    const pctText = progress > 0 ? `${Math.round(progress)}%` : '处理中';
    const msgText = state.error || state.message || '正在处理，请稍候';
    if (title && title.textContent !== titleText) title.textContent = titleText;
    if (pct && pct.textContent !== pctText) pct.textContent = pctText;
    if (bar) bar.style.display = progress > 0 ? '' : 'none';
    if (fill) fill.style.width = `${progress}%`;
    if (msg && msg.textContent !== msgText) msg.textContent = msgText;
  }

  function patchSummary(refs, counts) {
    const summary = document.getElementById('v3ReferenceSummary');
    if (!summary) return;
    let live = summary.querySelector('#v3ReferenceLiveSummary');
    if (!live) {
      live = document.createElement('div');
      live.id = 'v3ReferenceLiveSummary';
      live.className = 'v3-ref-batch';
      summary.appendChild(live);
    }
    const fingerprint = [counts.active, counts.candidateReady, counts.missing, counts.failed].join(':');
    if (live.dataset.fingerprint === fingerprint) return;
    live.dataset.fingerprint = fingerprint;
    live.innerHTML = `<span>生成中 ${counts.active}</span><span>候选待采用 ${counts.candidateReady}</span><span>未生成 ${counts.missing}</span><span>失败 ${counts.failed}</span>`;
  }

  function enhance(refs, submissionState) {
    const items = Array.isArray(refs?.items) ? refs.items : [];
    const submissions = Array.isArray(submissionState?.submissions) ? submissionState.submissions : [];
    const cards = [...document.querySelectorAll('.v3-ref-card')];
    const counts = {active:0, candidateReady:0, failed:0, missing:0};

    items.forEach((item, index) => {
      const card = cards[index];
      if (!card) return;
      const state = itemState(item, submissions);
      const status = String(state.status || '').toLowerCase();
      if (ACTIVE.has(status)) counts.active++;
      else if (status === 'completed') counts.candidateReady++;
      else if (status === 'failed') counts.failed++;
      else if (!item.ready) counts.missing++;

      if (!ACTIVE.has(status) && status !== 'failed') localPending.delete(String(item.entity_id || ''));
      setButtonBusy(String(item.entity_id || ''), ACTIVE.has(status));
      patchLiveBlock(card, item, state);
    });
    patchSummary(refs, counts);
    return counts.active > 0 || localPending.size > 0;
  }

  function resetForProject(projectId) {
    if (projectId === lastProjectId) return;
    lastProjectId = projectId;
    lastRefs = null;
    lastRefsAt = 0;
    hadActive = false;
    localPending.clear();
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedule(delay = POLL_MS) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = window.setTimeout(() => refreshUx(), Math.max(300, delay));
  }

  async function loadState(projectId, forceRefs = false) {
    const now = Date.now();
    const needRefs = forceRefs || !lastRefs || now - lastRefsAt >= REFS_REFRESH_MS;
    const submissionsPromise = json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/reference-submissions`)
      .catch(() => ({submissions:[]}));
    let refsPromise = null;
    if (needRefs) refsPromise = json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references`);
    const [submissions, refs] = await Promise.all([
      submissionsPromise,
      refsPromise || Promise.resolve(lastRefs),
    ]);
    if (refs) {
      lastRefs = refs;
      if (needRefs) lastRefsAt = now;
    }
    return {refs:lastRefs || {items:[]}, submissions};
  }

  async function syncTerminalCards(projectId) {
    if (terminalSyncing) return;
    terminalSyncing = true;
    try {
      // Full card rendering is expensive and moves the page. Do it exactly once
      // after active generation settles, never on every progress tick.
      if (typeof window.v3RefreshReferences === 'function') {
        await window.v3RefreshReferences(false);
      }
      lastRefs = await json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references`);
      lastRefsAt = Date.now();
    } catch (_) {
      // A failed cosmetic sync must not hide the already completed generation.
    } finally {
      terminalSyncing = false;
    }
  }

  async function refreshUx(forceRefs = false) {
    if (refreshing) return hadActive;
    const projectId = pid();
    if (!projectId) return false;
    resetForProject(projectId);
    if (document.visibilityState === 'hidden') {
      if (hadActive || localPending.size) schedule(HIDDEN_POLL_MS);
      return hadActive;
    }

    refreshing = true;
    try {
      const state = await loadState(projectId, forceRefs);
      const keep = enhance(state.refs, state.submissions);
      if (hadActive && !keep) {
        await syncTerminalCards(projectId);
        enhance(lastRefs || state.refs, state.submissions);
      }
      hadActive = keep;
      if (keep) schedule(POLL_MS);
      else if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      return keep;
    } catch (_) {
      if (hadActive || localPending.size) schedule(POLL_MS);
      return false;
    } finally {
      refreshing = false;
    }
  }

  function startPoll() {
    if (pollTimer) return;
    schedule(50);
  }

  function installActions() {
    window.v3GenerateReference = async (entityId, promptId, force) => {
      const projectId = pid();
      if (!projectId) return;
      resetForProject(projectId);
      const prompt = String(document.getElementById(promptId)?.value || '').trim();
      localPending.set(String(entityId), Date.now());
      setButtonBusy(String(entityId), true);
      if (lastRefs) enhance(lastRefs, {submissions:[]});
      startPoll();
      try {
        const result = await json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references/${encodeURIComponent(entityId)}/generate`, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({force:Boolean(force), prompt, prompt_text:prompt}),
        });
        notify(result?.already_pending ? '该参考图已经在生成中。' : '参考图任务已创建，后台生成中；页面会自动更新进度。');
      } catch (error) {
        localPending.delete(String(entityId));
        setButtonBusy(String(entityId), false);
        notify(error.message || String(error), true);
      }
      await refreshUx(true);
    };

    window.v3GenerateMissingReferences = async () => {
      const projectId = pid();
      if (!projectId) return;
      resetForProject(projectId);
      const button = [...document.querySelectorAll('#v3ReferencePanel button')].find(btn => btn.textContent.includes('自动生成缺失参考图'));
      if (button) {
        if (!button.dataset.v3ReferenceLabel) button.dataset.v3ReferenceLabel = button.textContent || '自动生成缺失参考图';
        button.disabled = true;
        button.textContent = '正在批量提交…';
      }

      const optimistic = [];
      for (const item of (lastRefs?.items || [])) {
        const state = String(item?.candidate?.status || '').toLowerCase();
        if (!item.ready && !ACTIVE.has(state) && state !== 'completed') {
          const id = String(item.entity_id || '');
          if (id) {
            optimistic.push(id);
            localPending.set(id, Date.now());
          }
        }
      }
      if (lastRefs) enhance(lastRefs, {submissions:[]});
      startPoll();
      notify('正在一次性创建全部缺失参考图任务…');

      try {
        const result = await json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references/generate-missing`, {
          method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
        });
        const submitted = result.submitted_entity_ids || [];
        submitted.forEach(id => localPending.set(String(id), Date.now()));
        optimistic.filter(id => !submitted.includes(id)).forEach(id => localPending.delete(String(id)));
        const waiting = (result.waiting_adoption_entity_ids || []).length;
        notify(submitted.length ? `已一次性提交 ${submitted.length} 个缺失参考图任务，后台继续生成。` : waiting ? `已有 ${waiting} 个候选等待采用。` : '当前没有需要补生成的参考图。');
      } catch (error) {
        optimistic.forEach(id => localPending.delete(String(id)));
        notify(error.message || String(error), true);
      } finally {
        if (button) {
          button.disabled = false;
          button.textContent = button.dataset.v3ReferenceLabel || '自动生成缺失参考图';
        }
      }
      await refreshUx(true);
    };
  }

  async function boot() {
    installStyles();
    installActions();
    const keep = await refreshUx(true);
    if (keep) startPoll();
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && (hadActive || localPending.size)) schedule(50);
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
