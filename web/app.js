const messages = document.querySelector('#messages');
const tasks = document.querySelector('#tasks');
const notifications = document.querySelector('#notifications');
const petStatus = document.querySelector('#petStatus');
const parserMode = document.querySelector('#parserMode');

function addMessage(text, who = 'pet') {
  const bubble = document.createElement('div');
  bubble.className = `bubble ${who}`;
  bubble.textContent = text;
  const time = document.createElement('small');
  time.textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  bubble.appendChild(time);
  messages.appendChild(bubble);
  messages.scrollTop = messages.scrollHeight;
}

function formatDate(value) { return new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
function statusText(status) { return ({ open: '待完成', done: '已完成', snoozed: '已延后', cancelled: '已取消' })[status] || status; }

async function loadTasks() {
  const response = await fetch('/api/tasks?today=true');
  const data = await response.json();
  tasks.innerHTML = '';
  const visible = data.filter(t => t.status !== 'cancelled');
  if (!visible.length) { tasks.innerHTML = '<div class="empty">今晚还没有任务，陪你看会儿星星。</div>'; petStatus.textContent = '清醒，陪你聊会儿'; return; }
  visible.slice(0, 12).forEach(task => {
    const item = document.createElement('div'); item.className = `task-item ${task.status === 'done' ? 'done' : ''}`;
    item.innerHTML = `<div class="task-row"><span class="task-title">${escapeHtml(task.title)}</span><span class="task-status">${statusText(task.status)}</span></div><div class="task-time">${formatDate(task.due_at)}</div>`;
    if (task.status !== 'done') {
      const actions = document.createElement('div'); actions.className = 'task-actions';
      actions.innerHTML = '<button data-action="complete">完成</button><button data-action="snooze">延后1小时</button><button class="cancel" data-action="cancel">取消</button>';
      actions.querySelectorAll('button').forEach(button => button.addEventListener('click', () => taskAction(task.id, button.dataset.action)));
      item.appendChild(actions);
    }
    tasks.appendChild(item);
  });
  const hasDue = visible.some(t => t.status === 'open');
  petStatus.textContent = hasDue ? '清醒，守着你的任务' : '清醒，等你吩咐';
}

async function taskAction(id, action) {
  const endpoint = action === 'complete' ? 'complete' : action === 'cancel' ? 'cancel' : 'snooze';
  const body = action === 'snooze' ? JSON.stringify({ due_at: new Date(Date.now() + 3600000).toISOString() }) : undefined;
  await fetch(`/api/tasks/${id}/${endpoint}`, { method: 'POST', headers: body ? { 'Content-Type': 'application/json' } : {}, body });
  addMessage(action === 'complete' ? '收到，已经帮你标记完成啦。' : action === 'cancel' ? '好，任务已取消。' : '没问题，任务延后一个小时。');
  await refresh();
}

async function loadNotifications() {
  const response = await fetch('/api/notifications'); const data = await response.json(); notifications.innerHTML = '';
  if (!data.length) { notifications.innerHTML = '<div class="empty">还没有提醒记录。</div>'; return; }
  data.slice(0, 8).forEach(item => {
    const el = document.createElement('div'); el.className = 'notice';
    const delivery = item.status === 'sent' ? '已发送' : item.status === 'failed' ? '发送失败' : item.status === 'missed' ? '已错过' : '发送中';
    const channel = item.channel === 'wechat' ? '微信' : '本地';
    const time = item.sent_at || item.reminder_time;
    el.innerHTML = `该完成：${escapeHtml(item.title)}<span class="notice-time">${channel} · ${delivery} · ${formatDate(time)}</span>`;
    notifications.appendChild(el);
  });
}

async function sendChat(content) {
  if (!content.trim()) return;
  addMessage(content, 'user'); document.querySelector('#chatInput').value = '';
  petStatus.textContent = '竖起耳朵，认真听你说';
  const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }) });
  const data = await response.json();
  addMessage(data.reply || data.detail || '猫咪暂时没有回应。');
  parserMode.textContent = data.parser === 'model'
    ? (data.action === 'chat' ? '陪伴对话 · 大模型' : '大模型判断 · 任务分流')
    : data.parser === 'hybrid'
      ? '大模型判断 · 本地时间校验'
    : '规则解析 · 任务模式';
  await refresh();
}

async function refresh() { await Promise.all([loadTasks(), loadNotifications()]); }
async function loadStatus() {
  try {
    const data = await (await fetch('/health')).json();
    parserMode.textContent = data.model?.chat_enabled && data.model?.available !== false
      ? `陪聊与意图判断 · ${data.model.model}`
      : data.model?.chat_enabled
        ? '模型暂不可用 · 任务规则仍可用'
        : '规则解析 · 陪聊需配置模型';
  } catch (_) { parserMode.textContent = '解析状态未知'; }
}
function escapeHtml(value) { return value.replace(/[&<>'\"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c])); }

document.querySelector('#chatForm').addEventListener('submit', e => { e.preventDefault(); sendChat(document.querySelector('#chatInput').value); });
document.querySelectorAll('[data-example]').forEach(button => button.addEventListener('click', () => sendChat(button.dataset.example)));
document.querySelector('#refreshBtn').addEventListener('click', refresh);
addMessage('晚上好，我是小满。你可以和我聊聊今天，也可以让我记住一件要提醒你的事。');
refresh();
loadStatus();
setInterval(refresh, 6000);
