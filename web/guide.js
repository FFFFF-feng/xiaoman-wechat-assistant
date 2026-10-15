(() => {
  'use strict';
  const panel = document.getElementById('copy-feedback');
  const message = document.getElementById('copy-message');
  const fallback = document.getElementById('copy-fallback');
  let timer;
  let trigger;
  document.querySelectorAll('[data-copy]').forEach(button => {
    button.addEventListener('click', async () => {
      clearTimeout(timer);
      trigger = button;
      const text = button.dataset.copy;
      fallback.hidden = true;
      panel.hidden = false;
      message.textContent = '正在复制…';
      try {
        await navigator.clipboard.writeText(text);
        message.textContent = '已复制。回到微信粘贴并发送；这里没有执行任何操作。';
        timer = setTimeout(() => { panel.hidden = true; }, 6500);
      } catch {
        message.textContent = '浏览器不允许自动复制，请长按下面的文字复制：';
        fallback.value = text;
        fallback.hidden = false;
        fallback.focus();
        fallback.select();
      }
    });
  });
  document.getElementById('close-feedback').addEventListener('click', () => {
    clearTimeout(timer);
    panel.hidden = true;
    trigger?.focus();
  });
})();
