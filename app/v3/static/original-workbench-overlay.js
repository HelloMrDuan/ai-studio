(() => {
  const replacements = [
    ['AI 漫剧工作台', '小段映画工作台'],
    ['AI Studio', '小段映画'],
    ['FaceFusion', '人物处理'],
    ['Qwen3-32B', '本地大模型'],
    ['Qwen3', '本地大模型'],
    ['H3', '视频生成模型'],
    ['Z-Image-Turbo', '图像生成模型'],
    ['Smart', '智能选择'],
    ['Turbo', '加速模式'],
    ['Stage', '阶段'],
    ['Skill', '创作能力'],
    ['Runtime', '运行状态'],
    ['Prompt', '提示词'],
    ['Shot', '镜头'],
    ['READY', '已就绪'],
    ['WAIT', '待确认'],
    ['stale', '已过期'],
    ['active', '当前版本'],
    ['JSON', '结构数据'],
    ['MP4', '视频文件'],
    ['AI 超分', '智能超分'],
    ['AI', '智能'],
  ];

  function replaceText(value) {
    let result = String(value ?? '');
    for (const [from, to] of replacements) result = result.split(from).join(to);
    return result;
  }

  function shouldPreserve(node) {
    const el = node.parentElement;
    if (!el) return false;
    return !!el.closest('script,style,textarea,pre,.shotPrompt,.heroText,.json');
  }

  function translateNode(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (!shouldPreserve(node)) node.nodeValue = replaceText(node.nodeValue);
      return;
    }
    if (!(node instanceof Element)) return;
    if (node.matches('script,style,textarea,pre,.shotPrompt,.heroText,.json')) return;
    for (const attr of ['title', 'placeholder', 'aria-label']) {
      if (node.hasAttribute(attr)) node.setAttribute(attr, replaceText(node.getAttribute(attr)));
    }
    for (const child of node.childNodes) translateNode(child);
  }

  function localizeStaticWorkbench() {
    const brand = document.querySelector('.brand');
    if (brand) brand.textContent = '小段映画工作台';
    const face = document.querySelector('[data-app-menu="face"] span:last-child');
    if (face) face.textContent = '人物处理';
    const makeSub = document.querySelector('#view-make .resultHeader .sub');
    if (makeSub) {
      makeSub.textContent = '正式镜头是唯一制作单元。图片和视频均通过新版工作流生成候选；只有你预览并点击“采用”后，才会进入正式资产。';
    }
    const rebuild = document.querySelector('#stage04RebuildBtn');
    if (rebuild) rebuild.textContent = '重建④正式分镜';
    const imageModel = document.querySelector('#shotImageModel');
    if (imageModel) {
      const options = imageModel.options;
      if (options[0]) options[0].textContent = '图像模型（默认）';
      if (options[1]) options[1].textContent = '兼容图像模型';
    }
    const videoProfile = document.querySelector('#shotVideoProfile');
    if (videoProfile?.options?.[1]) videoProfile.options[1].textContent = '加速模式 · 4步';
    translateNode(document.body);
  }

  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const text = await response.text();
    let body;
    try { body = JSON.parse(text); } catch { body = { detail: text }; }
    if (!response.ok) throw new Error(body?.detail || text || `请求失败（${response.status}）`);
    return body;
  }

  function currentProjectId() {
    try { return String(current || '').trim(); } catch { return ''; }
  }

  function defaultNarration() {
    try {
      const rows = typeof formalShots === 'function' ? formalShots() : [];
      const parts = rows.map(item => String(item.narration || item.dialogue || '').trim()).filter(Boolean);
      if (parts.length) return parts.join('');
    } catch (_) {}
    return '';
  }

  function installFinalEditor() {
    const view = document.querySelector('#view-final');
    if (!view || document.querySelector('#v3PostProductionPanel')) return;

    const oldPanel = view.querySelector('.panel');
    if (oldPanel) oldPanel.style.display = 'none';

    const panel = document.createElement('div');
    panel.className = 'panel';
    panel.id = 'v3PostProductionPanel';
    panel.innerHTML = `
      <div class="resultHeader">
        <div>
          <h2>第 ⑥ 步 · 声音与成片</h2>
          <div class="sub">每一项都可以修改、试听、重做；全部确认后再生成最终成片。</div>
        </div>
        <button class="btn" id="v3PostRefresh">刷新</button>
      </div>

      <div class="panel" style="margin-top:12px">
        <h3>⑥-1 选择已采用视频</h3>
        <div class="muted">这里只使用⑤中你已经采用的正式视频，不会把未确认候选混入成片。</div>
        <div id="v3PostVideos" class="cards" style="margin-top:10px"></div>
      </div>

      <div class="panel">
        <h3>⑥-2 旁白与配音</h3>
        <div class="field"><label>旁白文本</label><textarea id="v3Narration" style="min-height:130px" placeholder="填写或修改最终旁白"></textarea></div>
        <div class="grid2" style="margin-top:10px">
          <div class="field"><label>配音角色</label><select id="v3Voice"><option value="zh-CN-XiaoxiaoNeural">晓晓 · 女声</option><option value="zh-CN-YunxiNeural">云希 · 男声</option><option value="zh-CN-YunjianNeural">云健 · 男声</option></select></div>
          <div class="field"><label>操作</label><button class="btn primary" id="v3GenerateVoice">生成 / 重新生成配音</button></div>
        </div>
        <audio id="v3VoicePlayer" controls style="width:100%;margin-top:10px;display:none"></audio>
      </div>

      <div class="panel">
        <h3>⑥-3 字幕</h3>
        <div class="row">
          <button class="btn" id="v3GenerateSubtitle">按当前旁白生成字幕</button>
          <button class="btn good" id="v3SaveSubtitle">保存手工修改字幕</button>
        </div>
        <div class="field" style="margin-top:10px"><label>字幕内容（可以直接修改时间和文字）</label><textarea id="v3SubtitleText" style="min-height:220px" placeholder="先生成字幕，再在这里修改"></textarea></div>
      </div>

      <div class="panel">
        <h3>⑥-4 背景音乐</h3>
        <div class="field"><label>上传或替换背景音乐</label><input id="v3BgmFile" type="file" accept="audio/*"></div>
        <button class="btn" id="v3UploadBgm" style="margin-top:9px">保存背景音乐</button>
        <audio id="v3BgmPlayer" controls style="width:100%;margin-top:10px;display:none"></audio>
      </div>

      <div class="panel">
        <h3>⑥-5 最终合成</h3>
        <div class="grid2">
          <div class="field"><label>成片名称</label><input id="v3FinalName" value="最终成片"></div>
          <div class="field"><label>说明</label><div class="notice">最终合成必须同时具备：已采用视频、配音、已保存字幕、背景音乐。</div></div>
        </div>
        <button class="btn primary" id="v3Compose" style="margin-top:12px">生成最终成片</button>
        <video id="v3FinalPlayer" controls style="width:100%;max-height:640px;margin-top:12px;display:none;background:#050912;border-radius:8px"></video>
      </div>
      <div id="v3PostMessage"></div>
    `;
    view.insertBefore(panel, view.firstChild);

    document.querySelector('#v3PostRefresh').onclick = refreshPostProduction;
    document.querySelector('#v3GenerateVoice').onclick = generateVoice;
    document.querySelector('#v3GenerateSubtitle').onclick = generateSubtitle;
    document.querySelector('#v3SaveSubtitle').onclick = saveSubtitle;
    document.querySelector('#v3UploadBgm').onclick = uploadBgm;
    document.querySelector('#v3Compose').onclick = composeFinal;
  }

  function selectedVideoIds() {
    return [...document.querySelectorAll('#v3PostVideos input[type="checkbox"]:checked')].map(item => item.value);
  }

  function postMessage(text, bad = false) {
    const box = document.querySelector('#v3PostMessage');
    if (!box) return;
    box.innerHTML = text ? `<div class="notice ${bad ? '' : 'ok'}" style="margin-top:10px">${String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}</div>` : '';
  }

  async function refreshPostProduction() {
    const projectId = currentProjectId();
    if (!projectId) return;
    try {
      const data = await request(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/postproduction`);
      const videos = document.querySelector('#v3PostVideos');
      if (videos) {
        const selected = new Set(data.selected_video_asset_ids || []);
        videos.innerHTML = (data.videos || []).map((item, index) => `
          <label class="card" style="cursor:pointer">
            <input type="checkbox" value="${item.asset_id}" ${selected.has(item.asset_id) || (!selected.size && index === 0) ? 'checked' : ''}>
            <b>${item.name || `已采用视频 ${index + 1}`}</b>
            <div class="meta">第 ${item.order || index + 1} 个镜头 · 第 ${item.version || 1} 版</div>
            <video src="${item.url}" controls preload="metadata"></video>
          </label>`).join('') || '<div class="empty">⑤中还没有已采用的视频。</div>';
      }
      const narration = document.querySelector('#v3Narration');
      if (narration && !narration.dataset.userTouched) narration.value = data.narration || defaultNarration();
      if (narration) narration.oninput = () => narration.dataset.userTouched = '1';
      const voice = document.querySelector('#v3Voice');
      if (voice && data.voice) voice.value = data.voice;
      const voicePlayer = document.querySelector('#v3VoicePlayer');
      if (voicePlayer) {
        voicePlayer.style.display = data.voice_url ? '' : 'none';
        if (data.voice_url) voicePlayer.src = data.voice_url + `?t=${Date.now()}`;
      }
      const subtitle = document.querySelector('#v3SubtitleText');
      if (subtitle && data.subtitle_text && !subtitle.dataset.userTouched) subtitle.value = data.subtitle_text;
      if (subtitle) subtitle.oninput = () => subtitle.dataset.userTouched = '1';
      const bgmPlayer = document.querySelector('#v3BgmPlayer');
      if (bgmPlayer) {
        bgmPlayer.style.display = data.bgm_url ? '' : 'none';
        if (data.bgm_url) bgmPlayer.src = data.bgm_url + `?t=${Date.now()}`;
      }
      const finalPlayer = document.querySelector('#v3FinalPlayer');
      if (finalPlayer) {
        finalPlayer.style.display = data.final_url ? '' : 'none';
        if (data.final_url) finalPlayer.src = data.final_url + `?t=${Date.now()}`;
      }
      postMessage('声音与成片状态已刷新。');
    } catch (error) {
      postMessage(error.message, true);
    }
  }

  async function generateVoice() {
    const projectId = currentProjectId();
    const narration = document.querySelector('#v3Narration')?.value.trim();
    if (!projectId || !narration) return postMessage('请先填写旁白文本。', true);
    postMessage('正在生成配音…');
    try {
      await request(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/postproduction/tts`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({narration, voice: document.querySelector('#v3Voice')?.value || 'zh-CN-XiaoxiaoNeural'})
      });
      postMessage('配音已生成。你可以先试听，不满意就修改旁白后重新生成。');
      await refreshPostProduction();
    } catch (error) { postMessage(error.message, true); }
  }

  async function generateSubtitle() {
    const projectId = currentProjectId();
    const narration = document.querySelector('#v3Narration')?.value.trim();
    const ids = selectedVideoIds();
    if (!narration) return postMessage('请先填写旁白文本。', true);
    if (!ids.length) return postMessage('请先选择至少一个已采用视频。', true);
    postMessage('正在按成片时长生成字幕…');
    try {
      const data = await request(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/postproduction/subtitle/generate`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({narration, video_asset_ids:ids})
      });
      const box = document.querySelector('#v3SubtitleText');
      box.value = data.subtitle_text || '';
      box.dataset.userTouched = '';
      postMessage('字幕已生成。请直接修改字幕文字或时间，确认后点击“保存手工修改字幕”。');
    } catch (error) { postMessage(error.message, true); }
  }

  async function saveSubtitle() {
    const projectId = currentProjectId();
    const text = document.querySelector('#v3SubtitleText')?.value.trim();
    if (!text) return postMessage('字幕内容不能为空。', true);
    try {
      await request(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/postproduction/subtitle`, {
        method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({srt_text:text})
      });
      postMessage('字幕修改已保存。');
    } catch (error) { postMessage(error.message, true); }
  }

  async function uploadBgm() {
    const projectId = currentProjectId();
    const file = document.querySelector('#v3BgmFile')?.files?.[0];
    if (!file) return postMessage('请选择背景音乐文件。', true);
    const form = new FormData(); form.append('file', file);
    postMessage('正在校验并保存背景音乐…');
    try {
      await request(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/postproduction/bgm`, {method:'POST', body:form});
      postMessage('背景音乐已保存。你可以继续替换，直到满意。');
      await refreshPostProduction();
    } catch (error) { postMessage(error.message, true); }
  }

  async function composeFinal() {
    const projectId = currentProjectId();
    const ids = selectedVideoIds();
    if (!ids.length) return postMessage('请先选择至少一个已采用视频。', true);
    postMessage('正在合成最终成片…');
    try {
      await request(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/postproduction/compose`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({video_asset_ids:ids, name:document.querySelector('#v3FinalName')?.value.trim() || '最终成片'})
      });
      postMessage('最终成片已生成，并已回写到原工作台“最终输出”。');
      await refreshPostProduction();
      try { await refreshAll(); } catch (_) {}
    } catch (error) { postMessage(error.message, true); }
  }

  function hookFinalView() {
    if (typeof showView !== 'function' || showView.__v3Wrapped) return;
    const original = showView;
    const wrapped = function(v, btn) {
      const result = original(v, btn);
      if (v === 'final') setTimeout(refreshPostProduction, 50);
      return result;
    };
    wrapped.__v3Wrapped = true;
    showView = wrapped;
  }

  function boot() {
    localizeStaticWorkbench();
    installFinalEditor();
    hookFinalView();
    const observer = new MutationObserver(mutations => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) translateNode(node);
      }
    });
    observer.observe(document.body, {childList:true, subtree:true});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
