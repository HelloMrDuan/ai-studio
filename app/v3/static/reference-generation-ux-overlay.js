(() => {
  const ACTIVE = new Set(['queued','switching_gpu','running','generating','submitting']);
  const localPending = new Map();
  let pollTimer = null;

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
      .v3-ref-live{margin:8px 0 10px;padding:9px 10px;border:1px solid #2b4263;border-radius:9px;background:#0c1728}
      .v3-ref-live-head{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:12px;color:#c8d7eb}
      .v3-ref-live-head b{font-size:12px;color:#eaf2ff}
      .v3-ref-live-bar{height:7px;background:#08111d;border-radius:999px;overflow:hidden;margin-top:7px}
      .v3-ref-live-bar i{display:block;height:100%;background:linear-gradient(90deg,#2f6fed,#56b4ff);border-radius:999px;transition:width .25s ease}
      .v3-ref-live-msg{margin-top:6px;color:#8fa6c4;font-size:11px;line-height:1.45}
      .v3-ref-batch{margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;font-size:11px;color:#aabbd1}
      .v3-ref-batch span{border:1px solid #2b3d57;border-radius:999px;padding:4px 8px;background:#0b1524}
    `;
    document.head.appendChild(style);
  }
  function setButtonBusy(entityId, busy) {
    document.querySelectorAll('.v3-ref-card').forEach(card => {
      const button = [...card.querySelectorAll('button')].find(btn => (btn.getAttribute('onclick') || '').includes(`'${entityId}'`));
      if (!button) return;
      button.disabled = !!busy;
      if (busy) button.textContent = '任务提交中…';
    });
  }
  function itemState(item, submissions) {
    const candidate = item?.candidate || {};
    const cstate = String(candidate.status || '').toLowerCase();
    const related = submissions.find(row => (row.entity_ids || []).includes(String(item.entity_id || '')))
      || submissions.find(row => String(row.target_asset_id || '') === String(item.target_asset_id || ''));
    const lstate = localPending.get(String(item.entity_id || ''));
    if (cstate) {
      return {
        status: cstate,
        progress: Number(candidate.progress || 0),
        message: candidate.message || related?.message || '',
        error: candidate.error || related?.error || '',
      };
    }
    if (related && ACTIVE.has(String(related.status || '').toLowerCase())) {
      return {
        status: String(related.status || '').toLowerCase(),
        progress: Number(related.progress || 0),
        message: related.message || '参考图任务已进入后台队列',
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
  function enhance(refs, submissionState) {
    const items = Array.isArray(refs?.items) ? refs.items : [];
    const submissions = Array.isArray(submissionState?.submissions) ? submissionState.submissions : [];
    const cards = [...document.querySelectorAll('.v3-ref-card')];
    let active = 0, candidateReady = 0, failed = 0, missing = 0;
    items.forEach((item, index) => {
      const card = cards[index];
      if (!card) return;
      card.querySelectorAll('.v3-ref-live').forEach(node => node.remove());
      const state = itemState(item, submissions);
      const status = String(state.status || '').toLowerCase();
      if (ACTIVE.has(status)) active++;
      else if (status === 'completed') candidateReady++;
      else if (status === 'failed') failed++;
      else if (!item.ready) missing++;
      if (!ACTIVE.has(status) && status !== 'failed') {
        localPending.delete(String(item.entity_id || ''));
      }
      if (!status || item.ready) return;
      const block = document.createElement('div');
      block.className = 'v3-ref-live';
      const progress = Math.max(0, Math.min(100, Number(state.progress || 0)));
      block.innerHTML = `
        <div class="v3-ref-live-head"><b>${escHtml(statusText(status))}</b><span>${progress > 0 ? `${progress}%` : '处理中'}</span></div>
        ${progress > 0 ? `<div class="v3-ref-live-bar"><i style="width:${progress}%"></i></div>` : ''}
        <div class="v3-ref-live-msg">${escHtml(state.error || state.message || '正在处理，请稍候')}</div>`;
      const prompt = card.querySelector('.v3-ref-prompt');
      if (prompt?.parentNode) prompt.parentNode.insertBefore(block, prompt);
    });
    const summary = document.getElementById('v3ReferenceSummary');
    if (summary && items.length) {
      const adopted = Number(refs.ready_count || 0);
      const total = Number(refs.required_count || items.length);
      summary.innerHTML = `共 ${total} 个可复用资产，已采用 ${adopted} 个。` +
        `<div class="v3-ref-batch"><span>生成中 ${active}</span><span>候选待采用 ${candidateReady}</span><span>未生成 ${missing}</span><span>失败 ${failed}</span></div>`;
    }
    return active > 0 || localPending.size > 0;
  }
  async function refreshUx() {
    const projectId = pid();
    if (!projectId) return false;
    try {
      if (typeof window.v3RefreshReferences === 'function') await window.v3RefreshReferences(false);
      const [refs, submissions] = await Promise.all([
        json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references`),
        json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/reference-submissions`).catch(() => ({submissions:[]})),
      ]);
      await new Promise(resolve => setTimeout(resolve, 30));
      const keep = enhance(refs, submissions);
      if (!keep && pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      return keep;
    } catch (_) {
      return false;
    }
  }
  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(refreshUx, 1200);
    refreshUx();
  }

  function installActions() {
    window.v3GenerateReference = async (entityId, promptId, force) => {
      const projectId = pid();
      if (!projectId) return;
      const prompt = String(document.getElementById(promptId)?.value || '').trim();
      localPending.set(String(entityId), Date.now());
      setButtonBusy(String(entityId), true);
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
      await refreshUx();
    };

    window.v3GenerateMissingReferences = async () => {
      const projectId = pid();
      if (!projectId) return;
      const button = [...document.querySelectorAll('#v3ReferencePanel button')].find(btn => btn.textContent.includes('自动生成缺失参考图'));
      if (button) { button.disabled = true; button.textContent = '正在批量提交…'; }
      notify('正在一次性创建全部缺失参考图任务…');
      try {
        const result = await json(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references/generate-missing`, {
          method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
        });
        const submitted = result.submitted_entity_ids || [];
        submitted.forEach(id => localPending.set(String(id), Date.now()));
        const waiting = (result.waiting_adoption_entity_ids || []).length;
        notify(submitted.length ? `已一次性提交 ${submitted.length} 个缺失参考图任务，后台继续生成。` : waiting ? `已有 ${waiting} 个候选等待采用。` : '当前没有需要补生成的参考图。');
        startPoll();
      } catch (error) {
        notify(error.message || String(error), true);
      } finally {
        if (button) { button.disabled = false; button.textContent = '自动生成缺失参考图'; }
      }
      await refreshUx();
    };
  }

  function boot() {
    installStyles();
    installActions();
    setTimeout(refreshUx, 250);
    const observer = new MutationObserver(() => {
      if (document.getElementById('v3ReferencePanel') && (localPending.size || document.querySelector('.v3-ref-status.wait'))) startPoll();
    });
    observer.observe(document.body, {childList:true, subtree:true});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
