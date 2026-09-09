(() => {
  const STATUS = {
    completed: '已完成 · 待采用',
    queued: '排队中',
    switching_gpu: '准备模型中',
    running: '生成中',
    generating: '生成中',
    pending: '待处理',
    confirmed: '已采用',
    rejected: '已丢弃',
    failed: '失败',
    stale: '已过期',
    ready: '已就绪',
  };

  function localize() {
    document.querySelectorAll('.candidateHead span,.candidateStatus,.taskStatus,.globalEditStatus [data-status]').forEach(node => {
      const raw = String(node.textContent || '').trim();
      const key = raw.toLowerCase();
      if (STATUS[key]) node.textContent = STATUS[key];
    });
  }

  function boot() {
    localize();
    const observer = new MutationObserver(() => localize());
    observer.observe(document.body, {childList: true, subtree: true});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
