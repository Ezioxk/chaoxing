"use strict";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const token = $('meta[name="console-token"]').content;
const form = $("#settings-form");
const titles = {overview: "总览", courses: "我的课程", settings: "运行设置", logs: "运行日志", guide: "使用指南"};
const statusNames = {idle: "待开始", running: "运行中", completed: "已完成", partial: "部分完成", stopped: "已停止", error: "操作失败"};
let snapshot = null, cursor = 0, logs = [], selected = new Set(), initialized = false;
let courseSignature = "", chapterKey = null, chapterSignature = "", pending = false, toastTimer;
let lastEnd, pollFailures = 0;
const secretNames = ["common.password", "tiku.tokens", "tiku.key", "tiku.siliconflow_key", "notification.url", "notification.tg_chat_id"];

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = "toast" + (error ? " error" : "");
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, error ? 6500 : 3500);
}
async function api(path, data) {
  const response = await fetch(path, {method: data === undefined ? "GET" : "POST", headers: {"X-Console-Token": token, ...(data === undefined ? {} : {"Content-Type": "application/json"})}, ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const result = await response.json().catch(() => ({error: "服务器返回异常，请刷新页面后重试"}));
  if (!response.ok) throw new Error(result.error || "操作未成功，请查看日志");
  return result;
}
async function perform(action) {
  if (pending) return;
  pending = true;
  updateControls();
  try { await action(); await refresh(); }
  catch (error) { toast(error.message, true); }
  finally { pending = false; updateControls(); }
}
function navigate() {
  let page = location.hash.slice(1) || "overview";
  if (!titles[page]) page = "overview";
  $$(".view").forEach(node => { node.hidden = node.id !== "view-" + page; });
  $$('[data-nav]').forEach(node => {
    const active = node.dataset.nav === page;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
  });
  $("#breadcrumb").textContent = titles[page];
  document.title = titles[page] + " · 超星学习控制台";
}
window.addEventListener("hashchange", navigate);
navigate();
$("#account-link").onclick = () => { location.hash = "settings"; };
$("#today").textContent = new Intl.DateTimeFormat("zh-CN", {year: "numeric", month: "long", day: "numeric", weekday: "long"}).format(new Date());

function loadForm(settings, flags) {
  [...form.elements].forEach(input => {
    if (!input.name) return;
    const [section, key] = input.name.split(".");
    const value = settings[section]?.[key];
    if (input.type === "checkbox") input.checked = Boolean(value);
    else if (value !== undefined) input.value = value;
    if (flags[input.name]) input.placeholder = "已在当前会话中设置；留空保留，输入可替换";
    if (input.name === "common.password") input.disabled = settings.common.use_cookies;
  });
  updateProvider();
}
function formData() {
  const result = {common: {}, tiku: {}, notification: {}};
  [...form.elements].forEach(input => {
    if (!input.name) return;
    const [section, key] = input.name.split(".");
    // A blank secret preserves the server-side session value; logout clears all secrets.
    if (snapshot?.secret_flags[input.name] && !input.value) return;
    result[section][key] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
  });
  return result;
}
async function saveForm() {
  if (!form.reportValidity()) throw new Error("请检查设置字段中的格式和取值");
  await api("/api/settings", formData());
  // Clear credentials from the DOM after they reach the local process.
  secretNames.forEach(name => { const input = form.elements.namedItem(name); if (input) input.value = ""; });
  const password = form.elements.namedItem("common.password");
  password.type = "password";
  $(".show-password").textContent = "显示";
  $(".show-password").setAttribute("aria-label", "显示密码");
  $("#save-hint").textContent = "已保存普通设置；敏感凭据仅在本次会话中保留。";
}
form.onsubmit = event => {
  event.preventDefault();
  perform(async () => { await saveForm(); toast("设置已保存，下次操作时生效"); });
};
$("#login").onclick = () => perform(async () => {
  await saveForm();
  await api("/api/action", {action: "login"});
  toast(snapshot?.demo ? "正在载入演示课程" : "正在连接账号，完成后将自动读取课程");
});
$("#logout").onclick = () => perform(async () => {
  await api("/api/logout", {});
  selected.clear(); logs = []; renderLogs();
  [...form.elements].filter(input => input.name && snapshot?.secret_flags[input.name]).forEach(input => { input.value = ""; input.placeholder = "已清除，请重新输入"; });
  toast("已退出登录，界面会话凭据已清除");
});
$(".show-password").onclick = event => {
  const input = form.elements.namedItem("common.password");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  event.currentTarget.textContent = show ? "隐藏" : "显示";
  event.currentTarget.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
};
form.elements.namedItem("common.use_cookies").onchange = () => updateControls();
$$('[data-tab]').forEach(button => {
  button.onclick = () => {
    $$('[data-tab]').forEach(tab => { tab.classList.toggle("active", tab === button); tab.setAttribute("aria-selected", String(tab === button)); });
    $$(".settings-tab").forEach(panel => { panel.hidden = panel.id !== "tab-" + button.dataset.tab; });
  };
});
function updateProvider() {
  const provider = $("#tiku-provider").value;
  $$('[data-providers]').forEach(node => { node.hidden = !node.dataset.providers.split(",").includes(provider); });
}
$("#tiku-provider").onchange = updateProvider;

function visibleCourses() {
  const query = $("#course-search").value.trim().toLowerCase();
  return (snapshot?.courses || []).filter(course => [course.title, course.teacher, course.courseId, course.desc].some(value => String(value || "").toLowerCase().includes(query)));
}
function renderCourses() {
  if (!snapshot) return;
  const visible = visibleCourses();
  const grid = $("#course-grid");
  grid.replaceChildren();
  visible.forEach((course, index) => {
    const card = element("article", "course-card" + (selected.has(course.key) ? " selected" : ""));
    const cover = element("div", "course-cover");
    cover.append(element("span", "cover-number", String(index + 1).padStart(2, "0")));
    const label = element("label", "course-check");
    const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.checked = selected.has(course.key);
    checkbox.disabled = snapshot.busy || pending;
    checkbox.setAttribute("aria-label", "选择课程 " + course.title);
    checkbox.onchange = () => { checkbox.checked ? selected.add(course.key) : selected.delete(course.key); card.classList.toggle("selected", checkbox.checked); updateControls(); };
    label.append(checkbox, document.createTextNode("选择")); cover.append(label);
    const body = element("div", "course-body");
    body.append(element("h2", "", course.title), element("p", "", course.teacher || "教师信息未提供"));
    const meta = element("div", "course-meta");
    meta.append(element("span", "", "ID " + course.courseId), element("span", "", course.desc || "课程信息来自学习通"));
    const bottom = element("div", "course-bottom");
    const points = snapshot.chapters[course.key];
    bottom.append(element("span", "", points ? `${points.length} 个章节 · ${points.filter(p => p.has_finished).length} 个已完成` : "章节状态待查询"));
    const button = element("button", "", "查看章节 ↗"); button.type = "button"; button.disabled = snapshot.busy || pending;
    button.onclick = () => openChapters(course);
    bottom.append(button); body.append(meta, bottom); card.append(cover, body); grid.append(card);
  });
  $("#course-count").textContent = visible.length;
  $("#course-empty").hidden = visible.length > 0;
  $("#course-empty h2").textContent = snapshot.courses.length ? "没有找到匹配课程" : "还没有载入课程";
  $("#course-empty-text").textContent = snapshot.courses.length ? "换个关键词试试，或清空搜索条件。" : snapshot.authenticated ? "账号已连接，可以点击「刷新课程」重新读取。" : "连接账号后，你的课程会出现在这里。";
  $("#course-empty a").hidden = snapshot.authenticated || snapshot.courses.length > 0;
}
$("#course-search").oninput = renderCourses;
$("#select-visible").onclick = () => { visibleCourses().forEach(c => selected.add(c.key)); renderCourses(); updateControls(); };
$("#clear-selection").onclick = () => { selected.clear(); renderCourses(); updateControls(); };
$("#refresh-courses").onclick = () => perform(async () => { await api("/api/action", {action: "courses"}); });
$("#run-tasks").onclick = () => perform(async () => {
  if (!selected.size) throw new Error("请先勾选要处理的课程");
  await api("/api/action", {action: "run", keys: [...selected]});
  toast("后台任务已启动，可在运行日志中查看详情");
});
$("#stop-tasks").onclick = () => perform(async () => { await api("/api/stop", {}); toast("已发送停止指令"); });

function openChapters(course) {
  chapterKey = course.key;
  chapterSignature = "";
  $("#chapter-title").textContent = course.title;
  $("#chapter-dialog").showModal();
  if (snapshot.chapters[chapterKey]) renderChapters(); else readChapters();
}
function readChapters() {
  $("#chapter-content").replaceChildren(element("p", "chapter-loading", "正在读取章节，请稍候…"));
  chapterSignature = "loading";
  perform(async () => { await api("/api/action", {action: "chapters", keys: [chapterKey]}); });
}
function renderChapters() {
  if (!chapterKey || !snapshot) return;
  const points = snapshot.chapters[chapterKey];
  if (!points && snapshot.busy) return;
  const signature = JSON.stringify(points) + snapshot.busy;
  if (signature === chapterSignature) return;
  chapterSignature = signature;
  const content = $("#chapter-content");
  content.replaceChildren();
  if (!points) content.append(element("p", "chapter-loading", "未能读取章节。请查看日志，或点击重新读取。"));
  else if (!points.length) content.append(element("p", "chapter-loading", "平台未返回章节，课程可能尚未开放或没有章节内容。"));
  else points.forEach((point, index) => {
    const row = element("div", "chapter-row");
    row.append(element("small", "", String(index + 1).padStart(2, "0")), element("strong", "", point.title), element("small", "", `${point.jobCount ?? "—"} 个任务点`), element("span", "pill" + (point.has_finished ? " completed" : ""), point.has_finished ? "已完成" : point.need_unlock ? "需解锁" : "待完成"));
    content.append(row);
  });
}
$("#close-chapters").onclick = () => $("#chapter-dialog").close();
$("#reload-chapters").onclick = readChapters;
$("#chapter-dialog").addEventListener("close", () => { chapterKey = null; });

function timeText(timestamp) { return new Date(timestamp * 1000).toLocaleTimeString("zh-CN", {hour12: false}); }
function renderLogs() {
  const output = $("#log-output"), level = $("#log-level").value;
  const oldPosition = output.scrollTop;
  output.replaceChildren();
  const filtered = logs.filter(log => level === "ALL" || log.level === level);
  if (!filtered.length) output.append(element("div", "logs-empty", logs.length ? "没有符合此级别的日志。" : "等待操作。登录、读取课程或开始运行后，日志将出现在这里。"));
  filtered.forEach(log => {
    const line = element("div", "log-line " + log.level);
    line.append(element("time", "", timeText(log.time)), element("span", "log-level", log.level), element("span", "log-message", log.message));
    output.append(line);
  });
  $("#log-count").textContent = logs.length + " 条";
  output.scrollTop = $("#auto-scroll").checked ? output.scrollHeight : oldPosition;
  const activity = $("#recent-activity");
  if (logs.length) {
    activity.className = "activity-list";
    activity.replaceChildren();
    logs.slice(-4).reverse().forEach(log => {
      const item = element("div", "activity-item");
      item.append(element("i"), element("span", "", log.message.length > 70 ? log.message.slice(0, 70) + "…" : log.message), element("time", "", timeText(log.time)));
      activity.append(item);
    });
  } else {
    activity.className = "activity-empty";
    activity.replaceChildren(element("span", "empty-icon", "◷"), element("strong", "", "一切准备就绪"), element("p", "", "开始操作后，运行动态会显示在这里。"));
  }
}
$("#log-level").onchange = renderLogs;
$("#clear-logs").onclick = () => { logs = []; renderLogs(); toast("已清空页面显示，后续日志将继续显示"); };
$("#export-logs").onclick = () => {
  const data = logs.map(log => `${new Date(log.time * 1000).toLocaleString("zh-CN")} [${log.level}] ${log.message}`).join("\n");
  const url = URL.createObjectURL(new Blob(["\ufeff" + data], {type: "text/plain;charset=utf-8"}));
  const link = element("a"); link.href = url; link.download = "chaoxing-logs-" + new Date().toISOString().slice(0, 10) + ".txt";
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};

function updateControls() {
  const locked = pending || !snapshot || snapshot.busy;
  $("#selected-count").textContent = selected.size;
  $("#run-tasks").disabled = locked || !snapshot?.authenticated || !selected.size;
  ["#refresh-courses", "#select-visible", "#clear-selection"].forEach(id => { $(id).disabled = locked || !snapshot?.authenticated; });
  ["#login", "#save-settings"].forEach(id => { $(id).disabled = locked; });
  $("#logout").disabled = locked || !snapshot?.authenticated;
  $("#reload-chapters").disabled = locked;
  $("#stop-tasks").disabled = pending || !snapshot?.busy;
  [...form.elements].forEach(input => { if (input.name) input.disabled = locked || (input.name === "common.password" && form.elements.namedItem("common.use_cookies").checked); });
  $$(".course-card input,.course-bottom button").forEach(input => { input.disabled = locked; });
}
function renderState() {
  const {state, courses, authenticated, demo, busy, settings} = snapshot;
  $("#demo-banner").hidden = !demo;
  $("#connection").textContent = "本地服务已连接";
  $("#connection").prepend(element("i")); $("#connection").classList.remove("offline");
  $("#stat-account").replaceChildren(document.createTextNode(authenticated ? (demo ? "演示已连接" : "已连接") : "尚未连接"), element("span", "stat-icon", "↗"));
  $("#account-hint").textContent = authenticated ? "账号可用，可以前往选择课程" : "先连接你的超星学习通账号";
  $("#stat-courses").textContent = courses.length;
  $("#nav-course-count").textContent = courses.length;
  const p = state.progress;
  $("#stat-done").textContent = p.done;
  $("#stat-total").textContent = `/ ${p.total} 章节`;
  $("#stat-progress-hint").textContent = p.total ? `成功 ${p.success} · 跳过 ${p.skipped} · 失败 ${p.failed}` : "等待开始任务";
  $("#settings-account-status").textContent = authenticated ? "已连接" : "未登录";
  $("#settings-account-status").className = "pill" + (authenticated ? " completed" : "");
  $("#step-login").textContent = authenticated ? "✓" : "↗";
  $("#login-status").textContent = busy && state.action === "login" ? "正在连接，请稍候…" : authenticated ? "账号已连接" : "";
  $("#cookie-hint").textContent = snapshot.has_cookies ? "已检测到本地 Cookie 文件，勾选后可尝试验证登录。" : "未检测到 Cookie 文件，可先使用账号密码登录。";
  $("#run-summary").textContent = `${settings.common.speed}× 倍速 · ${settings.common.jobs} 个并发章节`;
  $("#task-dock").hidden = state.status === "idle";
  $("#task-status").textContent = statusNames[state.status] || state.status;
  $("#task-status").className = "pill " + state.status;
  $("#task-message").textContent = state.message;
  $("#task-progress").value = p.total ? Math.round(p.done / p.total * 100) : (busy ? 0 : 100);
  $("#task-current").textContent = state.current ? "最近处理：" + state.current : busy ? "正在等待平台返回数据…" : state.message;
  $("#task-counts").textContent = p.total ? `${p.done}/${p.total} · 成功 ${p.success} · 跳过 ${p.skipped} · 失败 ${p.failed}` : "";
  if (state.started_at) {
    const seconds = Math.max(0, Math.floor((state.ended_at || Date.now() / 1000) - state.started_at));
    $("#task-elapsed").textContent = `${Math.floor(seconds / 60)}分${seconds % 60}秒`;
  }
  if (state.ended_at && state.ended_at !== lastEnd && lastEnd !== undefined) toast(state.message, state.status === "error" || state.status === "partial");
  lastEnd = state.ended_at;
  const signature = JSON.stringify(courses) + JSON.stringify(snapshot.chapters);
  selected = new Set([...selected].filter(key => courses.some(c => c.key === key)));
  if (signature !== courseSignature) { courseSignature = signature; renderCourses(); }
  renderChapters();
  updateControls();
}
async function refresh() {
  const next = await api("/api/state?after=" + cursor);
  snapshot = next;
  pollFailures = 0;
  if (!initialized) { loadForm(next.settings, next.secret_flags); initialized = true; }
  else {
    [...form.elements].forEach(input => {
      if (input.name && next.secret_flags[input.name]) input.placeholder = "已在当前会话中设置；留空保留，输入可替换";
    });
  }
  if (next.logs.length) { logs.push(...next.logs); logs = logs.slice(-1200); renderLogs(); }
  cursor = next.cursor;
  renderState();
}
async function poll() {
  try { if (!pending) await refresh(); }
  catch (error) {
    pollFailures++;
    const connection = $("#connection"); connection.textContent = "本地服务连接中断"; connection.prepend(element("i")); connection.classList.add("offline");
    if (pollFailures === 1) toast(error.message + "。请检查启动窗口是否仍在运行。", true);
  }
  setTimeout(poll, pollFailures ? 3000 : 1000);
}
updateControls();
poll();
