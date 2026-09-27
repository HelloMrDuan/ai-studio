(() => {
  const STYLE_ID = 'v3ProjectDeleteStyle';

  function installStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #projectList .projectItem { position: relative; padding-right: 62px; }
      #projectList .v3ProjectDeleteButton {
        position: absolute;
        right: 8px;
        top: 50%;
        transform: translateY(-50%);
        border: 1px solid #6f3037;
        background: #32161a;
        color: #ffb4ba;
        border-radius: 7px;
        padding: 5px 8px;
        font-size: 11px;
        line-height: 1;
        cursor: pointer;
        z-index: 3;
      }
      #projectList .v3ProjectDeleteButton:hover { background: #4a1d23; color: #fff; }
      #projectList .v3ProjectDeleteButton:disabled { opacity: .55; cursor: wait; }
    `;
    document.head.appendChild(style);
  }

  function projectIdFromItem(item) {
    const raw = String(item.getAttribute('onclick') || '');
    const match = raw.match(/openProject\(['\"]([^'\"]+)['\"]\)/);
    return match ? match[1] : '';
  }

  async function deleteProject(projectId, title, button) {
    const confirmed = window.confirm(
      `确定删除作品“${title || '未命名作品'}”吗？\n\n` +
      '会删除这个作品的剧本、阶段状态、候选记录和项目内资源引用。\n' +
      '共享素材库和其他作品不会被删除。'
    );
    if (!confirmed) return;

    button.disabled = true;
    button.textContent = '删除中';
    try {
      const response = await fetch(`/api/v3/studio/projects/${encodeURIComponent(projectId)}`, {
        method: 'DELETE',
      });
      const text = await response.text();
      let body = {};
      try { body = JSON.parse(text); } catch (_) { body = { detail: text }; }
      if (!response.ok) throw new Error(body.detail || text || `删除失败（${response.status}）`);
      try {
        if (typeof toast === 'function') toast('作品已删除');
      } catch (_) {}
      window.setTimeout(() => window.location.reload(), 120);
    } catch (error) {
      button.disabled = false;
      button.textContent = '删除';
      try {
        if (typeof toast === 'function') toast(error.message || '删除失败', true);
        else window.alert(error.message || '删除失败');
      } catch (_) {
        window.alert('删除失败');
      }
    }
  }

  function enhanceProjectList() {
    const root = document.getElementById('projectList');
    if (!root) return;
    for (const item of root.querySelectorAll('.projectItem')) {
      if (item.querySelector('.v3ProjectDeleteButton')) continue;
      const projectId = projectIdFromItem(item);
      if (!projectId) continue;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'v3ProjectDeleteButton';
      button.textContent = '删除';
      button.title = '删除这个作品';
      button.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        const title = item.querySelector('b')?.textContent?.trim() || '未命名作品';
        deleteProject(projectId, title, button);
      });
      item.appendChild(button);
    }
  }

  function hookProjectRenderer() {
    const renderer = window.renderProjectList;
    if (typeof renderer !== 'function' || renderer.__v3DeleteWrapped) return;
    const wrapped = function(...args) {
      const result = renderer.apply(this, args);
      queueMicrotask(enhanceProjectList);
      return result;
    };
    wrapped.__v3DeleteWrapped = true;
    window.renderProjectList = wrapped;
  }

  function boot() {
    installStyle();
    hookProjectRenderer();
    enhanceProjectList();
    const observer = new MutationObserver(() => enhanceProjectList());
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
