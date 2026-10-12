const $ = selector => document.querySelector(selector);
const weekNames = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const statusNames = { open: '待完成', snoozed: '已延后', done: '已完成', cancelled: '已取消' };
const today = new Date();
let selectedDay = new Date(today.getFullYear(), today.getMonth(), today.getDate());
let weekStart = mondayOf(selectedDay);
let weekTasks = [];
let accessToken = '';

function ymd(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}
function addDays(value, days) { const next = new Date(value); next.setDate(next.getDate() + days); return next; }
function mondayOf(value) { const d = new Date(value.getFullYear(), value.getMonth(), value.getDate()); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d; }
function note(message) { const element = $('#pageNotice'); element.textContent = message; element.hidden = !message; }
function getAccessToken() {
  const hashToken = new URLSearchParams(window.location.hash.slice(1)).get('access');
  if (hashToken) {
    try { sessionStorage.setItem('schedule_access', hashToken); } catch (_) { /* 隐私模式仍可在本页使用 */ }
    history.replaceState(null, '', window.location.pathname);
    return hashToken;
  }
  try { return sessionStorage.getItem('schedule_access') || ''; } catch (_) { return ''; }
}
async function requestWithRetry(path, options = {}, fetcher = fetch, wait = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  const readOnly = (options.method || 'GET').toUpperCase() === 'GET';
  const attempts = readOnly ? 3 : 1;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      const response = await fetcher(path, { ...options, signal: AbortSignal.timeout(8000) });
      const transient = [502, 503, 504].includes(response.status) || (response.status >= 520 && response.status <= 530);
      if (!transient || attempt === attempts - 1) return response;
      await response.body?.cancel();
    } catch (error) {
      if (attempt === attempts - 1) {
        const failure = new Error(readOnly ? '连接暂时中断，请点击“重新加载日程”。' : '操作结果尚未确认，请先重新加载日程确认状态，不要重复操作。');
        failure.uncertain = !readOnly;
        throw failure;
      }
    }
    await wait(500 * (attempt + 1));
  }
}
async function api(path, options = {}) {
  const response = await requestWithRetry(path, {
    ...options,
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json', ...(options.headers || {}) },
    cache: 'no-store',
  });
  if (response.status === 401) {
    try { sessionStorage.removeItem('schedule_access'); } catch (_) { /* ignore */ }
    accessToken = '';
    throw new Error('日程链接已过期，请回到微信发送“打开日程”获取新链接。');
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const writeUnknown = (options.method || 'GET').toUpperCase() !== 'GET' && response.status >= 500;
    const error = new Error(writeUnknown ? '操作结果尚未确认，请先重新加载日程确认状态，不要重复操作。' :
      typeof detail.detail === 'string' ? detail.detail : '日程暂时无法读取，请点击“重新加载日程”。');
    error.uncertain = writeUnknown;
    throw error;
  }
  try { return await response.json(); }
  catch (_) {
    const error = new Error('响应未完整收到，请先重新加载日程确认状态。');
    error.uncertain = (options.method || 'GET').toUpperCase() !== 'GET';
    throw error;
  }
}
function renderWeek() {
  const end = addDays(weekStart, 6);
  $('#weekLabel').textContent = `${weekStart.getMonth() + 1}月${weekStart.getDate()}日 — ${end.getMonth() + 1}月${end.getDate()}日`;
  const container = $('#weekDays');
  container.replaceChildren();
  for (let i = 0; i < 7; i++) {
    const day = addDays(weekStart, i);
    const key = ymd(day);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'day-choice';
    button.classList.toggle('has-tasks', weekTasks.some(task => task.due_at.slice(0, 10) === key && task.status !== 'cancelled'));
    button.setAttribute('aria-pressed', String(key === ymd(selectedDay)));
    button.setAttribute('aria-label', `${day.getMonth() + 1}月${day.getDate()}日，${weekNames[day.getDay()]}`);
    for (const [className, content] of [['weekday', weekNames[day.getDay()]], ['daydate', String(day.getDate())], ['dot', '']]) {
      const span = document.createElement('span'); span.className = className; span.textContent = content; button.appendChild(span);
    }
    button.addEventListener('click', () => { selectedDay = day; render(); });
    container.appendChild(button);
  }
}
function snoozeTime(dueAt, nowMs = Date.now()) {
  // Future tasks move from their planned time, not backwards to now + 1 hour.
  // The API's naive timestamps are in the application's Asia/Shanghai timezone.
  const timestamp = /(?:Z|[+-]\d{2}:\d{2})$/i.test(dueAt) ? dueAt : `${dueAt}+08:00`;
  const plannedMs = Date.parse(timestamp);
  const baseMs = Number.isFinite(plannedMs) ? Math.max(plannedMs, nowMs) : nowMs;
  return new Date(baseMs + 3600000).toISOString();
}
function taskButton(label, action, taskId, dueAt, danger = false) {
  const button = document.createElement('button');
  button.type = 'button'; button.textContent = label;
  if (danger) button.classList.add('danger');
  button.addEventListener('click', async () => {
    if (action === 'cancel' && !confirm('确定取消这条日程吗？')) return;
    button.disabled = true; note('');
    try {
      const options = { method: 'POST' };
      if (action === 'snooze') options.body = JSON.stringify({ due_at: snoozeTime(dueAt) });
      await api(`/api/schedule/tasks/${taskId}/${action}`, options);
      try { await loadWeek(); }
      catch (_) { showLoadError(new Error('操作已成功，但列表刷新失败，请重新加载日程。')); return; }
      note(action === 'complete' ? '已标记完成。' : action === 'cancel' ? '已取消日程。' : '已延后一小时。');
    } catch (error) { showLoadError(error); if (!error.uncertain) button.disabled = false; }
  });
  return button;
}
function renderAgenda() {
  const key = ymd(selectedDay);
  const entries = weekTasks.filter(task => task.due_at.slice(0, 10) === key);
  const active = entries.filter(task => task.status === 'open' || task.status === 'snoozed');
  const isToday = key === ymd(today);
  $('#dayNumber').textContent = String(selectedDay.getDate()).padStart(2, '0');
  $('#dayWeekday').textContent = `${selectedDay.getFullYear()}年${selectedDay.getMonth() + 1}月 · ${weekNames[selectedDay.getDay()]}`;
  $('#dayTitle').textContent = isToday ? '今天，慢慢来。' : `${selectedDay.getMonth() + 1}月${selectedDay.getDate()}日的安排`;
  $('#daySummary').textContent = active.length ? `有 ${active.length} 件事等着你，小满陪你一件件完成。` : '没有待完成的事，可以轻松一点。';
  $('#agendaCount').textContent = `${entries.length} 件`;
  const list = $('#agendaList'); list.replaceChildren();
  if (!entries.length) {
    const empty = document.createElement('div'); empty.className = 'empty-state';
    const title = document.createElement('strong'); title.textContent = '这一天还没有安排';
    const hint = document.createElement('span'); hint.textContent = '想记下一件事？回到微信聊天窗口告诉小满。';
    empty.append(title, hint); list.appendChild(empty); return;
  }
  for (const task of entries) {
    const row = document.createElement('article'); row.className = 'agenda-item';
    if (task.status === 'done' || task.status === 'cancelled') row.classList.add('finished');
    const clock = document.createElement('time'); clock.className = 'agenda-time'; clock.textContent = task.due_at.slice(11, 16);
    const track = document.createElement('div'); track.className = 'agenda-track'; track.setAttribute('aria-hidden', 'true');
    const detail = document.createElement('div'); detail.className = 'agenda-detail';
    const title = document.createElement('h3'); title.textContent = task.title;
    const state = document.createElement('p'); state.className = 'status'; state.textContent = statusNames[task.status] || task.status;
    detail.append(title, state);
    if (task.status === 'open' || task.status === 'snoozed') {
      const actions = document.createElement('div'); actions.className = 'task-buttons';
      actions.append(taskButton('完成', 'complete', task.id, task.due_at), taskButton('延后1小时', 'snooze', task.id, task.due_at), taskButton('取消', 'cancel', task.id, task.due_at, true));
      detail.appendChild(actions);
    }
    row.append(clock, track, detail); list.appendChild(row);
  }
}
function render() { renderWeek(); renderAgenda(); }
async function loadWeek() {
  $('#retryButton').hidden = true;
  const end = addDays(weekStart, 6);
  weekTasks = await api(`/api/schedule/tasks?start=${ymd(weekStart)}&end=${ymd(end)}`);
  render();
}
function showLoadError(error) { note(error.message); $('#retryButton').hidden = !accessToken; }
async function shiftWeek(days) {
  weekStart = addDays(weekStart, days);
  selectedDay = addDays(selectedDay, days);
  note('');
  try { await loadWeek(); } catch (error) { showLoadError(error); }
}
$('#prevWeek').addEventListener('click', () => shiftWeek(-7));
$('#nextWeek').addEventListener('click', () => shiftWeek(7));
$('#todayButton').addEventListener('click', async () => { selectedDay = new Date(today.getFullYear(), today.getMonth(), today.getDate()); weekStart = mondayOf(selectedDay); note(''); try { await loadWeek(); } catch (error) { showLoadError(error); } });
$('#retryButton').addEventListener('click', async () => { note('正在重新连接…'); try { await loadWeek(); note(''); } catch (error) { showLoadError(error); } });
accessToken = getAccessToken();
render();
if (!accessToken) {
  $('#agendaList').replaceChildren();
  note('请回到微信聊天窗口发送“打开日程”，再点击小满发来的专属链接。');
}
else loadWeek().catch(showLoadError);
