(() => {
  const POLL_MS = 1000;
  let polling = false;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function projectId() {
    try { return String(current || '').trim(); } catch (_) { return ''; }
  }

  function formatSeconds(value) {
    const seconds = Math.max(0, Math.round(Number(value) || 0));
    if (seconds < 60) return `${seconds} 秒`;
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    return mins ? `${hours} 小时 ${mins} 分` : `${hours} 小时`;
  }

  function ensureStyle() {
    if (document.querySelector('#v3StageProgressStyle')) return;
    const style = document.createElement('style');
    style.id = 'v3StageProgressStyle';
    style.textContent = `
      #v3DeepStageProgress{margin-top:14px;padding:15px 16px;border:1px solid #315076;background:#0a1423;border-radius:11px}
      #v3DeepStageProgress .v3spHead{display:flex;align-items:flex-start;justify-content:space-between;gap:14px}
      #v3DeepStageProgress .v3spTitle{font-size:14px;font-weight:800;color:#eef4ff}
      #v3DeepStageProgress .v3spCurrent{font-size:12px;color:#9fb5d3;margin-top:4px}
      #v3DeepStageProgress .v3spPercent{font-size:26px;font-weight:900;color:#78a8ff;white-space:nowrap}
      #v3DeepStageProgress .v3spBar{height:9px;background:#07101c;border:1px solid #1e314b;border-radius:999px;overflow:hidden;margin:11px 0 9px}
      #v3DeepStageProgress .v3spBar>i{display:block;height:100%;background:linear-gradient(90deg,#3978ef,#67bdff);border-radius:999px;transition:width .7s ease}
      #v3DeepStageProgress .v3spMeta{display:flex;gap:12px;flex-wrap:wrap;font-size:11px;color:#8fa5c3;margin-bottom:12px}
      #v3DeepStageProgress .v3spMeta b{color:#cfe0f7;font-weight:700}
      #v3DeepStageProgress .v3spSteps{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:7px}
      #v3DeepStageProgress .v3spStep{border:1px solid #203550;background:#08111e;border-radius:8px;padding:8px 9px;min-width:0}
      #v3DeepStageProgress .v3spStep.done{border-color:#24583a;background:#0c2118}
      #v3DeepStageProgress .v3spStep.running{border-color:#46699a;background:#101e33}
      #v3DeepStageProgress .v3spStepTop{display:flex;align-items:center;gap:7px;min-width:0}
      #v3DeepStageProgress .v3spDot{width:18px;height:18px;border-radius:50%;display:grid;place-items:center;flex:0 0 auto;background:#142138;color:#7890b1;font-size:10px}
      #v3DeepStageProgress .done .v3spDot{background:#1c5634;color:#b9f4ca}
      #v3DeepStageProgress .running .v3spDot{background:#295694;color:#dceaff}
      #v3DeepStageProgress .v3spStepName{font-size:11px;color:#c8d5e8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
      #v3DeepStageProgress .v3spStepPct{font-size:10px;color:#8ca2bf;white-space:nowrap}
      #v3DeepStageProgress .v3spMini{height:4px;background:#07101b;border-radius:999px;overflow:hidden;margin-top:7px}
      #v3DeepStageProgress .v3spMini>i{display:block;height:100%;background:#4c88ec;border-radius:999px;transition:width .7s ease}
      #v3DeepStageProgress .v3spFoot{font-size:10px;color:#7186a5;margin-top:10px;line-height:1.5}
      #v3DeepStageProgress .v3spError{margin-top:9px;padding:8px 9px;border:1px solid #713843;background:#35161c;color:#ffc1c9;border-radius:7px;font-size:11px;white-space:pre-wrap;word-break:break-word}
      @media(max-width:900px){#v3DeepStageProgress .v3spSteps{grid-template-columns:1fr}}
    `;
    document.head.appendChild(style);
  }

  function ensurePanel() {
    ensureStyle();
    const host = document.querySelector('#view-create .taskFocus') || document.querySelector('.taskFocus');
    if (!host) return null;
    let panel = document.querySelector('#v3DeepStageProgress');
    if (!panel) {
      panel = document.createElement('div');
      panel.id = 'v3DeepStageProgress';
      panel.style.display = 'none';
      const oldProgress = host.querySelector('.stageProgressDetail');
      if (oldProgress?.parentElement === host) oldProgress.insertAdjacentElement('afterend', panel);
      else host.appendChild(panel);
    } else if (!host.contains(panel)) {
      host.appendChild(panel);
    }
    return panel;
  }

  function oldProgress(host) {
    return [...host.querySelectorAll('.stageProgressDetail')].find(item => item.id !== 'v3DeepStageProgress') || null;
  }

  function setTaskCopy(host, data, active) {
    const title = host.querySelector('.taskMain h2');
    const desc = host.querySelector('.taskDesc');
    for (const node of [title, desc]) {
      if (node && !node.dataset.v3OriginalText) node.dataset.v3OriginalText = node.textContent || '';
    }
    if (!active) {
      if (title?.dataset.v3OriginalText) title.textContent = title.dataset.v3OriginalText;
      if (desc?.dataset.v3OriginalText) desc.textContent = desc.dataset.v3OriginalText;
      return;
    }
    if (title) {
      const readable = String(data.title || '').split('·').slice(1).join('·').trim();
      title.textContent = readable ? `正在生成：${readable}` : '正在生成当前阶段';
    }
    if (desc) {
      const names = (data.steps || []).map(item => item.name).filter(Boolean);
      desc.textContent = names.length ? `执行路径：${names.join(' → ')}` : '正在执行当前生产阶段';
    }
  }

  function etaText(data) {
    if (data.status === 'completed') return `总耗时 ${formatSeconds(data.elapsed_seconds)}`;
    if (data.status === 'failed') return '执行失败';
    if (data.status === 'waiting') return `已耗时 ${formatSeconds(data.elapsed_seconds)} · 等待下一内部步骤`;
    const low = data.estimated_remaining_low_seconds;
    const high = data.estimated_remaining_high_seconds;
    if (low != null && high != null) {
      return `已耗时 ${formatSeconds(data.elapsed_seconds)} · 预计剩余 ${formatSeconds(low)} – ${formatSeconds(high)}`;
    }
    return `已耗时 ${formatSeconds(data.elapsed_seconds)}`;
  }

  function render(data) {
    const panel = ensurePanel();
    const host = panel?.closest('.taskFocus');
    if (!panel || !host) return;
    const active = data?.supported && data.status && data.status !== 'idle';
    const legacy = oldProgress(host);
    if (!active) {
      panel.style.display = 'none';
      if (legacy) legacy.style.display = '';
      setTaskCopy(host, data || {}, false);
      return;
    }

    panel.style.display = '';
    if (legacy) legacy.style.display = 'none';
    setTaskCopy(host, data, true);

    const overall = Math.max(0, Math.min(100, Number(data.overall_percent) || 0));
    const current = data.current_step || {};
    const statusText = data.status === 'completed'
      ? '已完成'
      : data.status === 'failed'
        ? '执行失败'
        : data.status === 'waiting'
          ? '等待内部推进'
          : `${current.name || '正在处理'} · ${Math.round(Number(current.percent) || 0)}%`;
    const steps = (data.steps || []).map(item => {
      const pct = Math.max(0, Math.min(100, Number(item.percent) || 0));
      const cls = item.state === 'completed' ? 'done' : item.state === 'running' ? 'running' : '';
      const icon = item.state === 'completed' ? '✓' : item.state === 'running' ? '●' : '○';
      return `
        <div class="v3spStep ${cls}">
          <div class="v3spStepTop">
            <span class="v3spDot">${icon}</span>
            <span class="v3spStepName" title="${esc(item.name)}">${esc(item.name)}</span>
            <span class="v3spStepPct">${Math.round(pct)}%</span>
          </div>
          <div class="v3spMini"><i style="width:${pct}%"></i></div>
        </div>`;
    }).join('');
    const source = data.estimate_source === 'history'
      ? `ETA 已按本机最近 ${Number(data.history_samples) || 0} 次同类真实耗时校准`
      : '当前为首次/样本不足估算；完成几次后会自动按本机真实耗时校准';

    panel.innerHTML = `
      <div class="v3spHead">
        <div>
          <div class="v3spTitle">${esc(data.title || '当前生产阶段')}</div>
          <div class="v3spCurrent">${esc(statusText)}</div>
        </div>
        <div class="v3spPercent">${Math.round(overall)}%</div>
      </div>
      <div class="v3spBar"><i style="width:${overall}%"></i></div>
      <div class="v3spMeta">
        <span><b>${esc(etaText(data))}</b></span>
        <span>内部执行轮次 ${Number(data.turn_count) || 1}</span>
      </div>
      <div class="v3spSteps">${steps}</div>
      <div class="v3spFoot">${esc(source)}。百分比和剩余时间是基于真实执行边界、当前阶段耗时和历史样本计算的预计值，不伪装成模型内部不可观测的精确 Token 进度。</div>
      ${data.error ? `<div class="v3spError">${esc(data.error)}</div>` : ''}
    `;
  }

  async function refresh() {
    if (polling) return;
    const id = projectId();
    const panel = ensurePanel();
    if (!id) {
      if (panel) panel.style.display = 'none';
      return;
    }
    polling = true;
    try {
      const response = await fetch(`/api/v3/studio/projects/${encodeURIComponent(id)}/stage-progress`, {cache: 'no-store'});
      if (!response.ok) return;
      render(await response.json());
    } catch (_) {
      // Progress UI must never interrupt the creation workflow.
    } finally {
      polling = false;
    }
  }

  const observer = new MutationObserver(() => ensurePanel());
  window.addEventListener('DOMContentLoaded', () => {
    ensurePanel();
    refresh();
    observer.observe(document.body, {childList: true, subtree: true});
    window.setInterval(refresh, POLL_MS);
  });
})();
