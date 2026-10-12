(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const valid = value => /^[A-Za-z0-9_-]{40,100}$/.test(value || '');
  const fragment = new URLSearchParams(location.hash.slice(1));
  let token = fragment.get('invite') || sessionStorage.getItem('xiaoman-invite') || '';
  history.replaceState(null, '', location.pathname); // No bearer in browser history/referrers.
  window.addEventListener('hashchange', () => {
    // Opening a new invitation in the same tab may be a fragment-only navigation.
    if (valid(new URLSearchParams(location.hash.slice(1)).get('invite'))) location.reload();
  });
  let timer, busy = false, state = '', failures = 0;
  const messages = {
    issued: '邀请已就绪。确认数据用途后，生成你的专属二维码。',
    queued: '已排队，等待上一位用户完成扫码。请保留页面。',
    starting: '正在启动微信连接，首次加载可能需要几分钟。请保留页面。',
    awaiting_scan: '请用本人微信扫码，并按手机上的提示确认。',
    needs_code: '微信需要配对数字。请输入手机显示的数字，填错可重新输入。',
    connected: '已完成授权。请回到微信测试聊天和提醒。',
    failed: '连接未完成。请联系管理员领取新邀请；不要重复使用旧二维码。',
    cancelled: '本次连接已取消。需要重新接入时，请领取新邀请。',
    expired: '扫码会话已过期，请联系管理员领取新邀请。',
    revoked: '接入已撤销，旧日程链接已失效。历史任务仍保留。',
  };
  async function api(path, data) {
    const response = await fetch(`/api/join/${path}`, {
      method: data === undefined ? 'GET' : 'POST', cache: 'no-store', redirect: 'error',
      headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
      body: data === undefined ? undefined : JSON.stringify(data), signal: AbortSignal.timeout(12000),
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        clearTimeout(timer); token = ''; sessionStorage.removeItem('xiaoman-invite');
        $('qr-box').hidden = true; $('qr').removeAttribute('src'); $('invite-form').hidden = false;
        $('consent').hidden = $('code-form').hidden = $('cancel').hidden = $('revoke').hidden = true;
        $('phone-transfer').hidden = true; $('phone-link').value = '';
      }
      throw new Error(typeof result.detail === 'string' ? result.detail : '操作失败，请刷新状态后重试。');
    }
    return result;
  }
  function render(result) {
    state = result.state;
    $('status').textContent = messages[state] || '正在读取连接状态。';
    $('invite-form').hidden = true;
    $('consent').hidden = state !== 'issued';
    $('qr-box').hidden = !result.qr_image;
    if (result.qr_image && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(result.qr_image)) $('qr').src = result.qr_image;
    else $('qr').removeAttribute('src');
    $('code-form').hidden = state !== 'needs_code';
    $('success').hidden = state !== 'connected';
    $('cancel').hidden = !['queued','starting','awaiting_scan','needs_code'].includes(state);
    $('revoke').hidden = state !== 'connected';
    $('phone-transfer').hidden = ['failed','cancelled','expired','revoked'].includes(state);
    $('expiry').textContent = `邀请到期：${new Date(result.expires_at).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'})}`;
  }
  async function refresh() {
    clearTimeout(timer);
    if (!token) return;
    try { render(await api('status')); failures = 0; }
    catch (error) { $('status').textContent = error.message || '网络未连接，请稍后刷新页面。'; failures++; }
    if (token && !['failed','cancelled','expired','revoked','connected'].includes(state)) {
      timer = setTimeout(refresh, Math.min(15000, 2500 + failures * 2500));
    }
  }
  async function action(path, data = {}) {
    if (busy) return;
    busy = true;
    const buttons = [...document.querySelectorAll('button')];
    buttons.forEach(button => {button.disabled = true;});
    try { const result = await api(path, data); if (result.state) render(result); else $('status').textContent = '配对数字已提交，正在验证。'; }
    catch (error) { $('status').textContent = error.message || '网络中断，先刷新状态，不要重复扫码。'; }
    finally { busy = false; buttons.forEach(button => {button.disabled = false;}); $('start').disabled = !$('agree').checked; }
    clearTimeout(timer); timer = setTimeout(refresh, 2500);
  }
  $('invite-form').addEventListener('submit', event => {
    event.preventDefault(); const value = $('invite').value.trim();
    if (!valid(value)) { $('status').textContent = '邀请码格式不正确，请重新复制。'; return; }
    token = value; sessionStorage.setItem('xiaoman-invite', token); $('invite').value = ''; state = ''; refresh();
  });
  $('agree').addEventListener('change', () => {$('start').disabled = !$('agree').checked || busy;});
  $('start').addEventListener('click', () => {if ($('agree').checked) action('start', {consent:true});});
  $('code-form').addEventListener('submit', event => {event.preventDefault(); const code = $('code').value; $('code').value = ''; action('code', {code});});
  $('cancel').addEventListener('click', () => {if (confirm('取消本次扫码连接？')) action('cancel');});
  $('revoke').addEventListener('click', () => {if (confirm('停止小满对你的微信提供聊天和提醒，并使旧日程链接失效？历史任务不会删除。')) action('revoke');});
  $('copy-invite').addEventListener('click', async () => {
    if (location.protocol !== 'https:') {
      $('status').textContent = '这是本机测试页面。手机请使用管理员发给你的完整 HTTPS 邀请链接，不要复制 127.0.0.1。'; return;
    }
    const link = `${location.origin}/join#invite=${encodeURIComponent(token)}`;
    try { await navigator.clipboard.writeText(link); $('status').textContent = '邀请链接已复制。只发给这位体验者，在手机浏览器打开。'; }
    catch { $('phone-link').hidden = false; $('phone-link').value = link; $('phone-link').select(); $('status').textContent = '请复制下面选中的完整链接，只发给这位体验者。'; }
  });
  if (valid(token)) { sessionStorage.setItem('xiaoman-invite', token); refresh(); }
  else token = '';
})();
