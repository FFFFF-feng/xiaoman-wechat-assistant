const $ = (selector) => document.querySelector(selector);
const weekNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const statusNames = {
  open: "待完成",
  snoozed: "已改期",
  done: "已完成",
  cancelled: "已取消",
};
const categoryNames = {
  life: "生活小事",
  work: "工作安排",
  study: "学习成长",
  health: "照顾自己",
};
const today = dateFromKey(shanghaiParts().date);
let selectedDay = new Date(today);
let weekStart = mondayOf(selectedDay);
let monthFocus = new Date(today.getFullYear(), today.getMonth(), 1);
let calendarMode = window.matchMedia("(min-width: 800px)").matches
  ? "month"
  : "week";
let weekTasks = [],
  accessToken = "",
  filter = "active",
  query = "";
let loadSerial = 0,
  loading = false,
  loaded = false,
  writeBusy = false,
  writeUncertain = false;
let editingTask = null,
  draftRequestId = "";

function ymd(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}
function shanghaiParts(value = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(value);
  const p = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return {
    date: `${p.year}-${p.month}-${p.day}`,
    time: `${p.hour}:${p.minute}`,
  };
}
function dateFromKey(key) {
  const [y, m, d] = key.split("-").map(Number);
  return new Date(y, m - 1, d);
}
function addDays(value, days) {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}
function mondayOf(value) {
  const d = new Date(value.getFullYear(), value.getMonth(), value.getDate());
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return d;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
function note(message, success = false) {
  const element = $("#pageNotice");
  element.textContent = message;
  element.hidden = !message;
  element.classList.toggle("success", success);
}
function getAccessToken() {
  const hashToken = new URLSearchParams(window.location.hash.slice(1)).get(
    "access",
  );
  if (hashToken) {
    try {
      sessionStorage.setItem("schedule_access", hashToken);
    } catch (_) {
      /* 本页仍可用 */
    }
    history.replaceState(null, "", window.location.pathname);
    return hashToken;
  }
  try {
    return sessionStorage.getItem("schedule_access") || "";
  } catch (_) {
    return "";
  }
}
async function requestWithRetry(
  path,
  options = {},
  fetcher = fetch,
  wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
) {
  const readOnly = (options.method || "GET").toUpperCase() === "GET";
  const attempts = readOnly ? 3 : 1;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      const response = await fetcher(path, {
        ...options,
        signal: AbortSignal.timeout(8000),
      });
      const transient =
        [502, 503, 504].includes(response.status) ||
        (response.status >= 520 && response.status <= 530);
      if (!transient || attempt === attempts - 1) return response;
      await response.body?.cancel();
    } catch (error) {
      if (attempt === attempts - 1) {
        const failure = new Error(
          readOnly
            ? "连接暂时中断，请点击“重新加载日程”。"
            : "操作结果尚未确认，请先重新加载日程确认状态，不要重复操作。",
        );
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
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    cache: "no-store",
  });
  if (response.status === 401) {
    try {
      sessionStorage.removeItem("schedule_access");
    } catch (_) {
      /* ignore */
    }
    accessToken = "";
    setWritable();
    throw new Error("日程链接已过期，请回到微信发送“打开日程”获取新链接。");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const writeUnknown =
      (options.method || "GET").toUpperCase() !== "GET" &&
      response.status >= 500;
    const error = new Error(
      writeUnknown
        ? "操作结果尚未确认，请先重新加载日程确认状态，不要重复操作。"
        : typeof detail.detail === "string"
          ? detail.detail
          : "请检查事项、日期和时间是否填写完整。",
    );
    error.uncertain = writeUnknown;
    throw error;
  }
  try {
    return await response.json();
  } catch (_) {
    const error = new Error("响应未完整收到，请先重新加载日程确认状态。");
    error.uncertain = (options.method || "GET").toUpperCase() !== "GET";
    throw error;
  }
}
function setWritable() {
  document
    .querySelectorAll(
      "[data-create], [data-template], .task-buttons button, .complete-check:not([data-ended])",
    )
    .forEach((button) => {
      button.disabled = !accessToken || writeBusy || writeUncertain;
    });
  $("#saveTask").disabled = !accessToken || writeBusy || writeUncertain;
  $("#closeDialog").disabled = writeBusy;
}
function loadedRange() {
  return calendarMode === "month"
    ? [
        new Date(monthFocus.getFullYear(), monthFocus.getMonth(), 1),
        new Date(monthFocus.getFullYear(), monthFocus.getMonth() + 1, 0),
      ]
    : [weekStart, addDays(weekStart, 6)];
}
function renderWeek() {
  const month = calendarMode === "month";
  $(".calendar").classList.toggle("week-mode", !month);
  document
    .querySelectorAll("[data-view]")
    .forEach((button) =>
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.view === calendarMode),
      ),
    );
  const end = addDays(weekStart, 6);
  $("#weekLabel").textContent = month
    ? `${monthFocus.getFullYear()}年 ${monthFocus.getMonth() + 1}月`
    : `${weekStart.getMonth() + 1}月${weekStart.getDate()}日 — ${end.getMonth() + 1}月${end.getDate()}日`;
  $("#prevWeek").setAttribute("aria-label", month ? "上个月" : "上一周");
  $("#nextWeek").setAttribute("aria-label", month ? "下个月" : "下一周");
  $("#dateJump").value = ymd(selectedDay);
  const container = $("#weekDays");
  container.replaceChildren();
  if (month)
    for (const name of ["一", "二", "三", "四", "五", "六", "日"]) {
      const label = document.createElement("span");
      label.className = "calendar-weekday";
      label.textContent = name;
      container.append(label);
    }
  const first = month ? mondayOf(monthFocus) : weekStart;
  const count = month
    ? Math.ceil(
        (((monthFocus.getDay() + 6) % 7) +
          new Date(
            monthFocus.getFullYear(),
            monthFocus.getMonth() + 1,
            0,
          ).getDate()) /
          7,
      ) * 7
    : 7;
  for (let i = 0; i < count; i++) {
    const day = addDays(first, i),
      key = ymd(day);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "day-choice";
    button.classList.toggle(
      "other-month",
      month && day.getMonth() !== monthFocus.getMonth(),
    );
    button.classList.toggle("is-today", key === ymd(today));
    button.classList.toggle(
      "has-tasks",
      weekTasks.some(
        (task) =>
          task.due_at.slice(0, 10) === key &&
          ["open", "snoozed"].includes(task.status),
      ),
    );
    button.setAttribute("aria-pressed", String(key === ymd(selectedDay)));
    button.setAttribute(
      "aria-label",
      `${day.getFullYear()}年${day.getMonth() + 1}月${day.getDate()}日，${weekNames[day.getDay()]}`,
    );
    button.dataset.date = key;
    if (!month) {
      const name = document.createElement("span");
      name.className = "weekday";
      name.textContent = weekNames[day.getDay()];
      button.append(name);
    }
    for (const [className, text] of [
      ["daydate", String(day.getDate())],
      ["dot", ""],
    ]) {
      const span = document.createElement("span");
      span.className = className;
      span.textContent = text;
      button.append(span);
    }
    button.addEventListener("click", () => selectDate(day));
    container.append(button);
  }
}
function snoozeTime(dueAt, nowMs = Date.now()) {
  const timestamp = /(?:Z|[+-]\d{2}:\d{2})$/i.test(dueAt)
    ? dueAt
    : `${dueAt}+08:00`;
  const plannedMs = Date.parse(timestamp);
  const baseMs = Number.isFinite(plannedMs)
    ? Math.max(plannedMs, nowMs)
    : nowMs;
  return new Date(baseMs + 3600000).toISOString();
}
function taskButton(label, action, taskId, dueAt, danger = false) {
  const button = document.createElement("button");
  button.type = "button";
  if (danger) button.classList.add("danger");
  if (["complete", "snooze", "edit"].includes(action))
    button.append(
      icon({ complete: "check", snooze: "clock", edit: "edit" }[action]),
    );
  button.append(document.createTextNode(label));
  button.addEventListener("click", async () => {
    const task = weekTasks.find((item) => item.id === taskId);
    if (!task || writeBusy || writeUncertain || !accessToken) return;
    if (action === "edit") {
      openEditor(task);
      return;
    }
    if (
      action === "cancel" &&
      !confirm(`取消“${task.title}”吗？取消后仍可在“已取消”中查看。`)
    )
      return;
    writeBusy = true;
    setWritable();
    note("");
    try {
      const options = { method: "POST" };
      if (action === "snooze")
        options.body = JSON.stringify({ due_at: snoozeTime(dueAt) });
      const result = await api(
        `/api/schedule/tasks/${taskId}/${action}`,
        options,
      );
      if (action === "snooze")
        pointAtDate(dateFromKey(result.due_at.slice(0, 10)));
      try {
        await loadWeek();
      } catch (_) {
        note("操作已成功，但列表刷新失败，请重新加载日程。");
        return;
      }
      note(
        action === "complete"
          ? "完成一件事，给自己一个小小的肯定。"
          : action === "cancel"
            ? "已取消日程。"
            : "已延后一小时。",
        true,
      );
    } catch (error) {
      writeUncertain = Boolean(error.uncertain);
      showLoadError(error);
    } finally {
      writeBusy = false;
      setWritable();
    }
  });
  return button;
}
function emptyState(title, hint, canCreate) {
  const empty = document.createElement("div");
  empty.className = "empty-state";
  const cat = document.createElement("span");
  cat.className = "empty-cat";
  cat.textContent = "ฅ^•ﻌ•^ฅ";
  cat.setAttribute("aria-hidden", "true");
  const strong = document.createElement("strong");
  strong.textContent = title;
  const text = document.createElement("span");
  text.textContent = hint;
  empty.append(cat, strong, text);
  if (canCreate) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "light-button";
    button.dataset.create = "";
    button.textContent = "＋ 记下第一件事";
    button.addEventListener("click", () => openEditor());
    empty.append(button);
  }
  return empty;
}
function renderAgenda() {
  const key = ymd(selectedDay),
    entries = weekTasks.filter((task) => task.due_at.slice(0, 10) === key);
  const active = entries.filter((task) =>
      ["open", "snoozed"].includes(task.status),
    ),
    done = entries.filter((task) => task.status === "done");
  const total = active.length + done.length,
    isToday = key === ymd(today);
  $("#dayWeekday").textContent =
    `${selectedDay.getFullYear()}年${selectedDay.getMonth() + 1}月${selectedDay.getDate()}日 · ${weekNames[selectedDay.getDay()]}`;
  $("#dayTitle").textContent = isToday
    ? "今天，留点时间给自己。"
    : `${selectedDay.getMonth() + 1}月${selectedDay.getDate()}日，慢慢安排。`;
  $("#daySummary").textContent = loading
    ? "小满正在翻看你的安排…"
    : !loaded
      ? "安排小事，也照顾自己。"
      : active.length
        ? `还有 ${active.length} 件小事，一件一件来就好。`
        : done.length
          ? "这一天的安排都完成啦，好好休息一下。"
          : "还没有安排的时间，也可以留给喜欢的事。";
  $("#dayProgress").max = total || 1;
  $("#dayProgress").value = done.length;
  $("#progressLabel").textContent = loading
    ? "正在读取安排"
    : total
      ? `已完成 ${done.length} / ${total}`
      : "给日子留一点空白";
  $("#agendaDate").textContent =
    `${selectedDay.getMonth() + 1}月${selectedDay.getDate()}日，${weekNames[selectedDay.getDay()]}`;
  $("#agendaTitle").firstChild.textContent = isToday
    ? "今天的安排 "
    : "这一天的安排 ";
  $("#agendaCount").textContent = loading ? "…" : String(entries.length);
  document
    .querySelectorAll("[data-filter]")
    .forEach((button) =>
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.filter === filter),
      ),
    );
  const list = $("#agendaList");
  list.replaceChildren();
  list.setAttribute("aria-busy", String(loading));
  if (loading) {
    list.append(emptyState("正在翻开这一页", "马上就能看到你的安排。"));
    setWritable();
    return;
  }
  if (!accessToken || !loaded) {
    list.append(
      emptyState(
        "从微信打开你的日程簿",
        "在微信发送“打开日程”，点击小满发来的专属链接。",
      ),
    );
    setWritable();
    return;
  }
  const matches = entries.filter(
    (task) =>
      (filter === "all" ||
        (filter === "active" && ["open", "snoozed"].includes(task.status)) ||
        task.status === filter) &&
      `${task.title} ${task.notes || ""}`.toLocaleLowerCase().includes(query),
  );
  if (!matches.length) {
    list.append(
      emptyState(
        query
          ? "没有找到这件事"
          : entries.length
            ? filter === "active"
              ? "待办清空，轻松一点"
              : "这里暂时没有日程"
            : "这一天，还可以有点小期待",
        query
          ? "换个关键词试试，也可以切换日期查找。"
          : entries.length
            ? "可以切换“全部”查看，或记下一件新的小事。"
            : "记一件想做的事，或者就留一点空白。",
        !query,
      ),
    );
    setWritable();
    return;
  }
  for (const task of matches) {
    const row = document.createElement("article");
    row.className = "agenda-item";
    row.dataset.priority = task.priority;
    const ended = ["done", "cancelled"].includes(task.status);
    row.classList.toggle("finished", ended);
    row.classList.toggle("cancelled", task.status === "cancelled");
    const clock = document.createElement("time");
    clock.className = "agenda-time";
    clock.dateTime = `${task.due_at}+08:00`;
    clock.textContent = task.due_at.slice(11, 16);
    const detail = document.createElement("div");
    detail.className = "agenda-detail";
    const top = document.createElement("div");
    top.className = "task-top";
    const complete = taskButton("", "complete", task.id, task.due_at);
    complete.className = "complete-check";
    complete.replaceChildren();
    complete.setAttribute(
      "aria-label",
      `${ended ? statusNames[task.status] : "完成"}：${task.title}`,
    );
    if (ended) {
      complete.dataset.ended = "";
      complete.disabled = true;
      complete.append(icon(task.status === "done" ? "check" : "close"));
    }
    const copy = document.createElement("div");
    copy.className = "task-copy";
    const title = document.createElement("h3");
    title.textContent = task.title;
    const meta = document.createElement("div");
    meta.className = "task-meta";
    const category = document.createElement("span");
    category.className = "category-badge";
    category.dataset.category = task.category || "life";
    category.textContent = categoryNames[task.category] || categoryNames.life;
    const status = document.createElement("span");
    status.className = "status";
    status.textContent =
      !ended &&
      task.reminder_enabled !== false &&
      Date.parse(`${task.due_at}+08:00`) < Date.now()
        ? "已过计划时间"
        : statusNames[task.status] || "待完成";
    const reminder = document.createElement("span");
    reminder.textContent =
      task.reminder_enabled === false
        ? "仅记录"
        : task.reminder_channel === "clawbot_pending"
          ? "提醒暂不可用"
          : "到点提醒";
    meta.append(category, status, reminder);
    if (task.priority === "high") {
      const priority = document.createElement("span");
      priority.className = "priority-badge";
      priority.textContent = "重要";
      meta.append(priority);
    }
    copy.append(title, meta);
    top.append(complete, copy);
    detail.append(top);
    if (task.notes) {
      const notes = document.createElement("p");
      notes.className = "task-notes";
      notes.textContent = task.notes;
      detail.append(notes);
    }
    if (!ended) {
      const actions = document.createElement("div");
      actions.className = "task-buttons";
      actions.append(
        taskButton("编辑", "edit", task.id, task.due_at),
        taskButton("延后1小时", "snooze", task.id, task.due_at),
        taskButton("取消", "cancel", task.id, task.due_at, true),
      );
      detail.append(actions);
    }
    row.append(clock, detail);
    list.append(row);
  }
  setWritable();
}
function render() {
  renderWeek();
  renderAgenda();
}
function pointAtDate(day) {
  selectedDay = day;
  weekStart = mondayOf(day);
  monthFocus = new Date(day.getFullYear(), day.getMonth(), 1);
}
async function loadWeek() {
  const serial = ++loadSerial,
    [start, end] = loadedRange();
  loading = true;
  $("#retryButton").hidden = true;
  render();
  try {
    const result = await api(
      `/api/schedule/tasks?start=${ymd(start)}&end=${ymd(end)}`,
    );
    if (serial !== loadSerial) return;
    weekTasks = result;
    loaded = true;
    loading = false;
    writeUncertain = false;
    render();
  } catch (error) {
    if (serial === loadSerial) {
      loading = false;
      loaded = false;
      render();
      showLoadError(error);
    }
    throw error;
  }
}
function showLoadError(error) {
  note(error.message);
  $("#retryButton").hidden = !accessToken;
}
async function selectDate(day) {
  const [start, end] = loadedRange();
  const covered = loaded && ymd(day) >= ymd(start) && ymd(day) <= ymd(end);
  pointAtDate(day);
  note("");
  if (covered) render();
  else
    try {
      await loadWeek();
    } catch (_) {
      /* shown by loadWeek */
    }
}
async function shiftWeek(direction) {
  pointAtDate(
    calendarMode === "month"
      ? new Date(monthFocus.getFullYear(), monthFocus.getMonth() + direction, 1)
      : addDays(selectedDay, direction * 7),
  );
  note("");
  try {
    await loadWeek();
  } catch (_) {
    /* shown by loadWeek */
  }
}
function updateFormHint() {
  const reminder = $("#taskReminder").checked;
  $("#taskDate").min = reminder ? shanghaiParts().date : "";
  $("#reminderHint").textContent = reminder
    ? "微信可用时，到点提醒你"
    : "只保存在日程中，不发送微信提醒";
  document
    .querySelectorAll("[data-time]")
    .forEach((button) =>
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.time === $("#taskTime").value),
      ),
    );
  $("#saveHint").textContent = reminder
    ? "安排好了，也可以随时调整。"
    : "只记下来，按自己的节奏来。";
}
function openEditor(task = null, category = "life") {
  if (!accessToken || writeBusy || writeUncertain) return;
  editingTask = task ? { ...task } : null;
  draftRequestId = crypto.randomUUID();
  $("#taskForm").reset();
  $("#formError").hidden = true;
  $("#formReload").hidden = true;
  $("#dialogTitle").textContent = task ? "调整这件事" : "记下一件事";
  $("#dialogEyebrow").textContent = task
    ? "日子可以慢慢调整"
    : "留给未来的自己";
  const next = shanghaiParts(new Date(Date.now() + 30 * 60000));
  const defaultDay =
    ymd(selectedDay) <= next.date ? next.date : ymd(selectedDay);
  $("#taskTitle").value = task?.title || "";
  $("#taskDate").value = task?.due_at.slice(0, 10) || defaultDay;
  $("#taskDate").min = shanghaiParts().date;
  $("#taskTime").value =
    task?.due_at.slice(11, 16) ||
    (defaultDay === next.date ? next.time : "09:00");
  $("#taskCategory").value = task?.category || category;
  $("#taskPriority").value = task?.priority || "normal";
  $("#taskNotes").value = task?.notes || "";
  $("#taskReminder").checked = task?.reminder_enabled !== false;
  updateFormHint();
  setWritable();
  $("#taskDialog").showModal();
}
function formPayload() {
  const sameTime =
    editingTask &&
    $("#taskDate").value === editingTask.due_at.slice(0, 10) &&
    $("#taskTime").value === editingTask.due_at.slice(11, 16);
  return {
    title: $("#taskTitle").value.trim(),
    due_at: sameTime
      ? `${editingTask.due_at}+08:00`
      : `${$("#taskDate").value}T${$("#taskTime").value}:00+08:00`,
    category: $("#taskCategory").value,
    priority: $("#taskPriority").value,
    notes: $("#taskNotes").value.trim(),
    reminder_enabled: $("#taskReminder").checked,
  };
}
$("#taskForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (writeBusy || writeUncertain || !accessToken) return;
  const payload = formPayload(),
    errorBox = $("#formError");
  errorBox.hidden = true;
  if (
    !payload.title ||
    !Number.isFinite(Date.parse(payload.due_at)) ||
    (payload.reminder_enabled && Date.parse(payload.due_at) <= Date.now())
  ) {
    errorBox.textContent = !payload.title
      ? "先给这件事起个名字吧。"
      : "这个提醒时间已经过去了，换个将来的时间吧；补记过去的事可以关闭提醒。";
    errorBox.hidden = false;
    return;
  }
  writeBusy = true;
  setWritable();
  $("#saveTask span").textContent = "正在保存…";
  try {
    const task = await api(
      editingTask
        ? `/api/schedule/tasks/${editingTask.id}/edit`
        : "/api/schedule/tasks",
      {
        method: "POST",
        body: JSON.stringify(
          editingTask
            ? { ...payload, expected_updated_at: editingTask.updated_at }
            : { ...payload, request_id: draftRequestId },
        ),
      },
    );
    $("#taskDialog").close();
    pointAtDate(dateFromKey(task.due_at.slice(0, 10)));
    filter = "active";
    query = "";
    $("#taskSearch").value = "";
    try {
      await loadWeek();
    } catch (_) {
      note("已经保存，但列表刷新失败。请重新加载查看。");
      return;
    }
    note(
      task.reminder_enabled && task.reminder_channel === "clawbot_pending"
        ? "已保存日程，但微信自动提醒暂不可用，可在这里查看。"
        : editingTask
          ? "已保存修改，按你的新安排来。"
          : task.reminder_enabled
            ? "已记在日程里，安排好了。"
            : "已记在日程里，这件事不发送微信提醒。",
      true,
    );
  } catch (error) {
    writeUncertain = Boolean(error.uncertain);
    errorBox.textContent = error.message;
    errorBox.hidden = false;
    $("#formReload").hidden = !writeUncertain;
    showLoadError(error);
  } finally {
    writeBusy = false;
    $("#saveTask span").textContent = "保存日程";
    setWritable();
  }
});
$("#taskDialog").addEventListener("cancel", (event) => {
  if (writeBusy) event.preventDefault();
});
$("#closeDialog").addEventListener("click", () => {
  if (!writeBusy) $("#taskDialog").close();
});
$("#formReload").addEventListener("click", async () => {
  const day = $("#taskDate").value;
  $("#taskDialog").close();
  if (day) pointAtDate(dateFromKey(day));
  try {
    await loadWeek();
    note("日程已重新加载，请先核对刚才的保存结果。");
  } catch (_) {
    /* shown */
  }
});
document
  .querySelectorAll("[data-create]")
  .forEach((button) => button.addEventListener("click", () => openEditor()));
document
  .querySelectorAll("[data-template]")
  .forEach((button) =>
    button.addEventListener("click", () =>
      openEditor(null, button.dataset.template),
    ),
  );
document.querySelectorAll("[data-time]").forEach((button) =>
  button.addEventListener("click", () => {
    $("#taskTime").value = button.dataset.time;
    updateFormHint();
  }),
);
$("#taskTime").addEventListener("input", updateFormHint);
$("#taskReminder").addEventListener("change", updateFormHint);
document.querySelectorAll("[data-filter]").forEach((button) =>
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    renderAgenda();
  }),
);
$("#taskSearch").addEventListener("input", (event) => {
  query = event.target.value.trim().toLocaleLowerCase();
  renderAgenda();
});
document.querySelectorAll("[data-view]").forEach((button) =>
  button.addEventListener("click", async () => {
    if (calendarMode === button.dataset.view) return;
    calendarMode = button.dataset.view;
    pointAtDate(selectedDay);
    try {
      await loadWeek();
    } catch (_) {
      /* shown */
    }
  }),
);
$("#dateJump").addEventListener("change", (event) => {
  if (event.target.value) selectDate(dateFromKey(event.target.value));
});
$("#prevWeek").addEventListener("click", () => shiftWeek(-1));
$("#nextWeek").addEventListener("click", () => shiftWeek(1));
$("#todayButton").addEventListener("click", () =>
  selectDate(dateFromKey(shanghaiParts().date)),
);
$("#retryButton").addEventListener("click", async () => {
  note("正在重新连接…");
  try {
    await loadWeek();
    note("");
  } catch (_) {
    /* shown */
  }
});
accessToken = getAccessToken();
render();
if (!accessToken) note("请回到微信发送“打开日程”，点击小满发来的专属链接。");
else loadWeek().catch(() => {});
