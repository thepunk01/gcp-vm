"use strict";

/* ================= 工具 ================= */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pad = (n) => String(n).padStart(2, "0");
const store = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch (_) { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
  del(k) { try { localStorage.removeItem(k); } catch (_) {} },
};
const genToken = () => [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "-");
const fmtClock = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
function fmtAgo(ts) {
  if (!ts) return "-";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)} 秒前`;
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)} 小时前`;
  return fmtTime(ts);
}
function splitBps(bps) {
  for (const [u, n] of [["Gbps", 1e9], ["Mbps", 1e6], ["Kbps", 1e3]]) {
    if (bps >= n) { const v = bps / n; return [v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2), u]; }
  }
  return [String(Math.round(bps || 0)), "bps"];
}
const fmtBps = (bps) => splitBps(bps).join(" ");

let token = store.get("gcpvm_token");
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (res.status === 401 && path !== "/api/login") { logout(); throw new Error("登录已过期，请重新登录"); }
  if (!res.ok) throw new Error((data && (data.error || data.message)) || `请求失败（HTTP ${res.status}）`);
  return data;
}
function toast(msg, isErr = false) {
  const el = document.createElement("div");
  if (isErr) el.className = "err";
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), isErr ? 7000 : 3800);
}
async function busy(btn, fn) {
  const html = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "处理中…";
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.innerHTML = html; }
}
async function copyText(text, okMsg) {
  try { await navigator.clipboard.writeText(text); toast(okMsg); }
  catch (_) { prompt("复制下面的内容：", text); }
}
const icon = (id) => `<svg aria-hidden="true"><use href="#i-${id}"/></svg>`;

/* ================= 主题 ================= */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $("#theme-toggle use").setAttribute("href", t === "dark" ? "#i-sun" : "#i-moon");
  drawAllCharts();
}
$("#theme-toggle").addEventListener("click", () => {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  store.set("gcpvm_theme", t);
  applyTheme(t);
});

/* ================= 状态 ================= */
const S = {
  instances: [], rates: {}, settings: null, presets: [], pushStatus: {},
  totalHist: [], instHist: {}, events: [], filter: "all", evFilter: "all", view: "overview",
};
const STOPPED = ["TERMINATED", "STOPPED", "SUSPENDED"];
const STATUS = {
  RUNNING: ["运行中", "ok"], TERMINATED: ["已关机", "idle"], STOPPED: ["已关机", "idle"], STOPPING: ["关机中", "warn"],
  PROVISIONING: ["创建中", "warn"], LOST: ["云端已不存在", "bad"], UNKNOWN: ["未知", "idle"], STAGING: ["启动中", "warn"], SUSPENDED: ["已挂起", "idle"], SUSPENDING: ["挂起中", "warn"], REPAIRING: ["修复中", "warn"],
};
const CN = { ok: ["国内可达", "ok"], blocked: ["疑似被墙", "bad"], down: ["服务不可达", "warn"], unknown: ["未检测", "idle"] };
const EVT = {
  alert: "服务监控", blocked: "被墙", server: "服务器", create: "新建实例", delete: "删除实例", firewall: "防火墙", exec: "执行命令",
  startup: "启动脚本", start: "开机", stop: "关机", reset: "重启", change_ip: "换 IP", change_ip_auto: "自动换 IP", auto_start: "自动开机",
  check: "检测", dns: "DNS", sync: "同步", account: "账号", replace: "自动替补", replace_new: "替补完成",
};
const isManual = (i) => i.provider === "manual";
const isAzure = (i) => i.provider === "azure";
const isGone = (i) => i.status === "LOST" || i.account_status === "banned";
const byId = (id) => S.instances.find((x) => x.id === +id);

function statusOf(i) {
  if (isManual(i)) {
    const r = S.rates[i.id];
    if (!r || !r.last_seen) return ["未接入上报", "idle"];
    return r.online ? ["在线", "ok"] : ["失联", "bad"];
  }
  return STATUS[i.status] || [i.status || "未知", "idle"];
}
function svcOf(i) {
  const r = S.rates[i.id];
  if (!r || !r.online || !r.svc || r.svc === "none") return null;
  return r.svc === "active" ? ["服务运行中", "ok"] : [r.svc === "inactive" ? "服务已停止" : `服务 ${r.svc}`, "bad"];
}
function issuesOf(i) {
  const out = [];
  const r = S.rates[i.id];
  if (i.account_status === "banned") return [["bad", "账号被封", `${i.account_name} 已判定为被封/停用${i.auto_replace ? "，等待自动替补" : ""}`]];
  if (i.status === "LOST") return [["bad", "实例已不存在", `云端找不到该实例${i.auto_replace ? "，等待自动替补" : ""}`]];
  if (i.cn_state === "blocked") out.push(["bad", "疑似被墙", `${i.external_ip}:${i.check_port} 国内不可达 · 连续 ${i.fail_count} 次${isManual(i) ? " · 需手动换 IP" : ""}`]);
  else if (i.cn_state === "down") out.push(["warn", "服务不可达", `${i.external_ip}:${i.check_port} 国内外都不通`]);
  if (!isManual(i) && STOPPED.includes(i.status)) out.push(["warn", "已关机", i.auto_start ? "等待自动开机" : "未开启自动开机"]);
  if (r && r.last_seen && !r.online && (isManual(i) || i.status === "RUNNING")) out.push(["bad", "失联", `上报中断 · 最后上报 ${fmtAgo(r.last_seen)}`]);
  else if (r && r.online && ["inactive", "failed"].includes(r.svc)) out.push(["bad", "服务已停止", `${(S.settings && S.settings.watch_service) || "服务"} 状态 ${r.svc}`]);
  return out;
}

/* ================= 登录 ================= */
function showApp(on) {
  $("#login-view").classList.toggle("hidden", on);
  $("#app").classList.toggle("hidden", !on);
}
function logout() {
  token = "";
  store.del("gcpvm_token");
  stopTimers();
  closeDrawer();
  showApp(false);
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    const r = await api("/api/login", { method: "POST", body: { password: $("#login-password").value } });
    token = r.token;
    store.set("gcpvm_token", token);
    $("#login-password").value = "";
    start();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", logout);

/* ================= 路由 ================= */
const VIEWS = { overview: "总览", instances: "实例", firewall: "防火墙", accounts: "账号与服务器", settings: "设置", events: "事件日志" };
function route() {
  const [view, sub] = (location.hash.replace(/^#\/?/, "") || "overview").split("/");
  const v = VIEWS[view] ? view : "overview";
  if (v !== S.view) closeDrawer();
  S.view = v;
  $$(".view").forEach((el) => el.classList.toggle("hidden", el.id !== `view-${v}`));
  $$(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.view === v));
  $("#view-title").textContent = VIEWS[v];
  document.title = `${VIEWS[v]} · AtomicGCP 控制台`;
  renderTopbar(v);
  $("#sidebar").classList.remove("open");
  $("#scrim").classList.add("hidden");
  if (v === "settings") S.sec = sub || "detect";
  ({ overview: loadOverview, instances: loadInstances, firewall: loadFirewall, accounts: loadAccounts, settings: () => loadSettings(sub), events: loadEvents })[v]();
}
window.addEventListener("hashchange", route);
$("#menu-btn").addEventListener("click", () => { $("#sidebar").classList.add("open"); $("#scrim").classList.remove("hidden"); });
$("#scrim").addEventListener("click", () => { $("#sidebar").classList.remove("open"); $("#scrim").classList.add("hidden"); });

function renderTopbar(v) {
  const box = $("#topbar-actions");
  box.innerHTML = "";
  const add = (html, cls, fn) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = `btn ${cls}`; b.innerHTML = html;
    b.title = b.textContent; b.setAttribute("aria-label", b.textContent);
    b.addEventListener("click", (e) => fn(e.currentTarget));
    box.appendChild(b);
  };
  if (v === "overview" || v === "instances") {
    add(`${icon("radar")}<span class="lbl">立即检测一轮</span>`, "small", (b) => busy(b, async () => toast((await api("/api/run-check", { method: "POST" })).message)));
    add(`${icon("refresh")}<span class="lbl">同步云实例</span>`, "small", (b) => busy(b, async () => {
      const r = await api("/api/sync", { method: "POST" });
      S.instances = r.instances;
      renderAll();
      const parts = Object.entries(r.result).map(([k, n]) => `${k}: ${typeof n === "number" ? n + " 台" : n}`);
      toast(parts.length ? `同步完成：${parts.join("；")}` : "还没有添加 GCP 账号");
    }));
  }
  if (v === "instances") add(`${icon("plus")}<span class="lbl">新建实例</span>`, "small primary", openCreate);
}

/* ================= 数据加载与轮询 ================= */
let timers = [];
function stopTimers() { timers.forEach(clearInterval); timers = []; }
async function loadInstances() {
  try {
    const [inst, rates] = await Promise.all([api("/api/instances"), api("/api/traffic").catch(() => S.rates)]);
    S.instances = inst;
    S.rates = rates;
    renderAll();
  } catch (e) { toast(e.message, true); }
}
async function loadRates() {
  try { S.rates = await api("/api/traffic"); } catch (_) { return; }
  let rx = 0, tx = 0;
  S.instances.forEach((i) => {
    const r = S.rates[i.id];
    if (r && r.online) { rx += r.rx_bps; tx += r.tx_bps; }
    const h = (S.instHist[i.id] = S.instHist[i.id] || []);
    h.push({ rx: r && r.online ? r.rx_bps : 0, tx: r && r.online ? r.tx_bps : 0 });
    if (h.length > 60) h.shift();
  });
  S.totalHist.push({ rx, tx });
  if (S.totalHist.length > 90) S.totalHist.shift();
  if (S.view === "overview") renderOverview(false);
  if (S.view === "instances") updateRateCells();
  if (drawerId) renderDrawerLive();
  renderNavBadge();
}
async function loadProbeStatus() {
  try {
    if (!S.settings) S.settings = await api("/api/settings");
    S.pushStatus = Object.fromEntries((await api("/api/push-probes")).map((p) => [p.name, p]));
  } catch (_) { return; }
  const pill = $("#probe-pill");
  const s = S.settings;
  if (s.detect_mode === "local") { pill.className = "probe-pill ok"; pill.querySelector("span").textContent = "本机检测"; return; }
  const push = Object.values(S.pushStatus);
  const on = push.filter((p) => p.online).length + (s.probes || []).length;
  const total = push.length + (s.probes || []).length;
  pill.className = `probe-pill ${total === 0 ? "" : on > 0 ? "ok" : "bad"}`;
  pill.querySelector("span").textContent = total === 0 ? "未配置探针" : `探针 ${on}/${total}`;
}
function renderAll() {
  if (S.view === "overview") renderOverview(true);
  if (S.view === "instances") renderInstances();
  if (drawerId) renderDrawer();
  renderNavBadge();
}
function renderNavBadge() {
  const n = S.instances.filter((i) => issuesOf(i).some((x) => x[0] === "bad")).length;
  const b = $("#nav-alerts");
  b.textContent = n;
  b.classList.toggle("hidden", !n);
}

/* ================= 图表 ================= */
function drawChart(svg, hist, max = 0) {
  if (!svg) return;
  const vb = svg.viewBox.baseVal;
  const W = vb.width, H = vb.height, cap = svg.dataset.cap ? +svg.dataset.cap : hist.length;
  const n = Math.max(cap, 2), step = W / (n - 1), off = n - hist.length;
  const m = Math.max(1, max || 0, ...hist.map((p) => Math.max(p.rx, p.tx))) * 1.15;
  const y = (v) => (H - 2 - (v / m) * (H - 8)).toFixed(1);
  const pts = (k) => hist.map((p, i) => `${((off + i) * step).toFixed(1)},${y(p[k])}`).join(" ");
  let grid = "";
  for (let g = 1; g <= 3; g++) grid += `<line x1="0" x2="${W}" y1="${(H * g / 4).toFixed(1)}" y2="${(H * g / 4).toFixed(1)}" stroke="var(--line)" stroke-width="1" vector-effect="non-scaling-stroke"/>`;
  if (hist.length < 2) { svg.innerHTML = grid; return; }
  const rx = pts("rx");
  svg.innerHTML = `${grid}
    <path d="M${(off * step).toFixed(1)},${H} L${rx.replace(/ /g, " L")} L${W},${H} Z" fill="var(--rx)" fill-opacity="0.13"/>
    <polyline points="${rx}" fill="none" stroke="var(--rx)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    <polyline points="${pts("tx")}" fill="none" stroke="var(--tx)" stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`;
}
function drawAllCharts() {
  drawChart($("#ov-chart"), S.totalHist);
  if (drawerId) drawChart($("#d-chart"), S.instHist[drawerId] || []);
  $$("svg.spark[data-id]").forEach((svg) => drawChart(svg, (S.instHist[svg.dataset.id] || []).slice(-30)));
}
$("#ov-chart").dataset.cap = 90;
$("#d-chart").dataset.cap = 60;

/* ================= 总览 ================= */
async function loadOverview() {
  await loadInstances();
  try { S.settings = await api("/api/settings"); } catch (_) {}
  loadProbeStatus();
  loadOverviewEvents();
}
async function loadOverviewEvents() {
  try {
    const rows = await api("/api/events?limit=12");
    $("#ov-events").innerHTML = rows.length ? rows.map(feedItem).join("") : `<li class="empty-line">暂无事件</li>`;
  } catch (_) {}
}
const feedItem = (e) => `<li class="${esc(e.level)}"><span class="tm">${fmtClock(e.ts)}</span><span class="msg">${e.instance_name ? `<b>${esc(e.instance_name)}</b>` : ""}${esc(e.message)}</span></li>`;
function renderOverview(full) {
  let rx = 0, tx = 0, up = 0;
  S.instances.forEach((i) => {
    const r = S.rates[i.id];
    if (r && r.online) { rx += r.rx_bps; tx += r.tx_bps; }
    if (isManual(i) ? r && r.online : i.status === "RUNNING") up += 1;
  });
  const [rv, ru] = splitBps(rx), [tv, tu] = splitBps(tx);
  $("#k-rx").textContent = rv; $("#k-rx-u").textContent = ru;
  $("#k-tx").textContent = tv; $("#k-tx-u").textContent = tu;
  $("#k-run").textContent = up; $("#k-total").textContent = `/ ${S.instances.length}`;
  drawChart($("#ov-chart"), S.totalHist);

  // 需要处理
  const items = [];
  S.instances.forEach((i) => issuesOf(i).forEach(([lvl, t, d]) => items.push({ lvl, t, d, i })));
  const s = S.settings;
  if (s && s.detect_mode !== "local") {
    const push = Object.values(S.pushStatus);
    const total = push.length + (s.probes || []).length;
    if (!total) items.push({ lvl: "warn", t: "未配置国内探针", d: "无法判断 IP 是否被墙", link: "#/settings/probes" });
    else if (!(s.probes || []).length && push.length && !push.some((p) => p.online)) items.push({ lvl: "bad", t: "国内探针离线", d: "主动上报探针超过 2 分钟未连接，被墙检测暂停", link: "#/settings/probes" });
  }
  const badCount = new Set(items.filter((x) => x.lvl === "bad" && x.i).map((x) => x.i.id)).size;
  $("#k-bad").textContent = badCount;
  $("#ov-issue-count").textContent = items.length ? `${items.length} 项` : "";
  items.sort((a, b) => (a.lvl === "bad" ? 0 : 1) - (b.lvl === "bad" ? 0 : 1));
  $("#ov-issues").innerHTML = items.length ? items.slice(0, 8).map((x) => `
    <li><span class="ic ${x.lvl}">${icon("alert")}</span>
      <span class="t"><b>${x.i ? esc(x.i.name) + " · " : ""}${esc(x.t)}</b><span>${esc(x.d)}</span></span>
      ${x.i ? `<button class="btn small ghost" data-open="${x.i.id}">处理</button>` : `<a class="btn small ghost" href="${x.link}">设置</a>`}</li>`).join("")
    : `<li><span class="ic ok">${icon("check")}</span><span class="t"><b>一切正常</b><span>没有被墙、失联或服务停止的机器</span></span></li>`;

  // 分组
  const groups = {};
  S.instances.forEach((i) => {
    const name = (i.grp || "").trim();
    if (!name) return;
    const g = (groups[name] = groups[name] || { name, on: 0, total: 0, rx: 0, tx: 0 });
    g.total += 1;
    const r = S.rates[i.id];
    if (r && r.online && !["inactive", "failed"].includes(r.svc)) { g.on += 1; g.rx += r.rx_bps; g.tx += r.tx_bps; }
  });
  const gl = Object.values(groups).sort((a, b) => a.name.localeCompare(b.name));
  $("#ov-groups").innerHTML = gl.length ? gl.map((g) => `
    <div class="group"><b>${esc(g.name)}</b>
      <span class="badge ${g.on ? (g.on === g.total ? "ok" : "warn") : "bad"}">${g.on}/${g.total} 在线</span>
      <span class="num"><span style="color:var(--rx)">↓ ${fmtBps(g.rx)}</span>　<span style="color:var(--tx)">↑ ${fmtBps(g.tx)}</span></span></div>`).join("")
    : `<div class="empty-line">还没有分组。在实例「设置」里填写分组，速率页会按组汇总。</div>`;
  if (full) loadOverviewEvents();
}
$("#ov-issues").addEventListener("click", (e) => { const b = e.target.closest("[data-open]"); if (b) openDrawer(+b.dataset.open); });

/* ================= 实例列表 ================= */
function filtered() {
  const q = $("#inst-search").value.trim().toLowerCase();
  return S.instances.filter((i) => {
    if (S.filter === "gcp" && i.provider !== "gcp") return false;
    if (S.filter === "azure" && !isAzure(i)) return false;
    if (S.filter === "manual" && !isManual(i)) return false;
    if (S.filter === "issue" && !issuesOf(i).length) return false;
    if (!q) return true;
    return [i.name, i.external_ip, i.grp, i.zone, i.note].some((v) => String(v || "").toLowerCase().includes(q));
  });
}
function rateCell(i) {
  const r = S.rates[i.id];
  if (!r || !r.last_seen) return `<span class="muted small">未安装上报</span>`;
  if (!r.online) return `<span class="small" style="color:var(--bad)" title="最后上报 ${fmtTime(r.last_seen)}">上报中断 · ${fmtAgo(r.last_seen)}</span>`;
  const svc = svcOf(i);
  return `<div class="rate"><span class="down">↓ ${fmtBps(r.rx_bps)}</span>　<span class="up">↑ ${fmtBps(r.tx_bps)}</span></div>
    ${svc ? `<div class="svc" style="color:var(--${svc[1]})">${svc[0]}</div>` : ""}`;
}
function renderInstances() {
  const all = S.instances;
  const counts = { all: all.length, gcp: all.filter((i) => i.provider === "gcp").length, azure: all.filter(isAzure).length, manual: all.filter(isManual).length, issue: all.filter((i) => issuesOf(i).length).length };
  $$("#inst-filter button").forEach((b) => {
    b.classList.toggle("on", b.dataset.f === S.filter);
    b.innerHTML = `${{ all: "全部", gcp: "GCP", azure: "Azure", manual: "自有", issue: "异常" }[b.dataset.f]}<span class="n">${counts[b.dataset.f]}</span>`;
    b.classList.toggle("hidden", b.dataset.f === "azure" && !counts.azure);
  });
  const list = filtered();
  const empty = $("#inst-empty");
  empty.classList.toggle("hidden", !!list.length);
  empty.innerHTML = all.length ? "没有符合条件的实例" : `还没有实例。到「<a href="#/accounts">账号与服务器</a>」添加 GCP 账号或自有服务器。`;
  $("#inst-table tbody").innerHTML = list.map((i) => {
    const [st, stc] = statusOf(i);
    const [cn, cnc] = CN[i.cn_state] || CN.unknown;
    const manual = isManual(i);
    const gone = isGone(i);
    const running = !gone && (manual || i.status === "RUNNING");
    const sub = manual ? `自有服务器 · SSH ${esc(i.ssh_user || "root")}:${i.ssh_port || 22}`
      : `${isAzure(i) ? "Azure · " : ""}${esc(i.zone)} · ${esc(i.machine_type)}${i.preemptible ? " · Spot" : ""}`;
    return `<tr class="click${drawerId === i.id ? " sel" : ""}" data-id="${i.id}">
      <td data-l="实例"><div><div class="name">${esc(i.name)}</div><div class="sub">${sub}</div></div></td>
      <td data-l="IP"><span class="ip" data-copy="${esc(i.external_ip)}" title="点击复制">${esc(i.external_ip) || "<span class='muted'>无外网 IP</span>"}</span></td>
      <td data-l="状态"><span class="badge ${i.account_status === "banned" ? "bad" : stc}" data-st="${i.id}">${i.account_status === "banned" ? "账号被封" : st}</span></td>
      <td data-l="国内"><span class="badge ${cnc}" title="上次检测 ${fmtTime(i.last_check)}">${cn}${i.cn_state === "blocked" ? ` ${i.fail_count}` : ""}</span></td>
      <td data-l="速率"><div style="display:flex;align-items:center;gap:10px"><svg class="spark" data-id="${i.id}" viewBox="0 0 92 26" preserveAspectRatio="none" aria-hidden="true"></svg><div data-rate="${i.id}">${rateCell(i)}</div></div></td>
      <td data-l="分组"><div class="tags">${i.grp ? `<span class="tag on">${esc(i.grp)}</span>` : `<span class="muted small">-</span>`}${!manual && i.auto_ip ? `<span class="tag" title="被墙自动换 IP">自动换 IP</span>` : ""}${i.auto_replace ? `<span class="tag" title="账号被封 / 实例被删除时自动替补">自动替补</span>` : ""}</div></td>
      <td><div class="ops">
        ${running ? `<button class="btn small ghost" data-act="check">检测</button>` : ""}
        ${!manual && !gone && i.status === "RUNNING" ? `<button class="btn small ghost danger" data-act="change_ip">换 IP</button>` : ""}
        ${!manual && !gone && STOPPED.includes(i.status) ? `<button class="btn small primary" data-act="start">开机</button>` : ""}
        <button class="btn small" data-act="open">管理</button>
      </div></td></tr>`;
  }).join("");
  $$("#inst-table svg.spark").forEach((svg) => drawChart(svg, (S.instHist[svg.dataset.id] || []).slice(-30)));
}
function updateRateCells() {
  $$("#inst-table [data-rate]").forEach((el) => { const i = byId(el.dataset.rate); if (i) el.innerHTML = rateCell(i); });
  $$("#inst-table [data-st]").forEach((el) => { const i = byId(el.dataset.st); if (i && i.account_status !== "banned") { const [st, c] = statusOf(i); el.textContent = st; el.className = `badge ${c}`; } });
  $$("#inst-table svg.spark").forEach((svg) => drawChart(svg, (S.instHist[svg.dataset.id] || []).slice(-30)));
}
$("#inst-filter").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.filter = b.dataset.f; renderInstances(); } });
$("#inst-search").addEventListener("input", renderInstances);
$("#inst-table").addEventListener("click", async (e) => {
  if (e.target.closest("[data-copy]")) return;
  const tr = e.target.closest("tr[data-id]");
  if (!tr) return;
  const btn = e.target.closest("[data-act]");
  const inst = byId(tr.dataset.id);
  if (!btn || btn.dataset.act === "open") return openDrawer(inst.id);
  await doAction(inst, btn.dataset.act, btn);
});
document.addEventListener("click", async (e) => {
  const c = e.target.closest("[data-copy]");
  if (c && c.dataset.copy) { e.stopPropagation(); await copyText(c.dataset.copy, `已复制 ${c.dataset.copy}`); }
});

const CONFIRM = {
  stop: (i) => `确定关闭实例「${i.name}」？`,
  reset: (i) => `确定强制重启实例「${i.name}」？（相当于断电重启）`,
  change_ip: (i) => `确定为「${i.name}」更换外网 IP？当前 IP ${i.external_ip || "-"} 将被释放，连接会中断。`,
};
async function doAction(inst, act, btn) {
  if (CONFIRM[act] && !confirm(CONFIRM[act](inst))) return;
  await busy(btn, async () => {
    const r = await api(`/api/instances/${inst.id}/action`, { method: "POST", body: { action: act } });
    if (act === "check") {
      const c = r.check;
      const cn = (c.cn || []).map((p) => `${p.source}: ${p.ok === null ? "探针异常" : p.ok ? `通 ${p.latency_ms}ms` : "不通"}`).join("；");
      toast(`${(CN[c.state] || [c.state])[0]}：${c.detail}${cn ? "（" + cn + "）" : ""}`, c.state === "blocked");
    } else if (act === "change_ip") {
      toast(`新 IP：${r.instance.external_ip}`);
    } else {
      toast(`操作成功，当前状态 ${(STATUS[r.instance.status] || [r.instance.status])[0]}`);
    }
    await loadInstances();
  });
}

/* ================= 详情抽屉 ================= */
let drawerId = null;
let drawerTab = "info";
function openDrawer(id, tab = "info") {
  const inst = byId(id);
  if (!inst) return;
  const changed = drawerId !== inst.id;
  drawerId = inst.id;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  if (changed) { resetExec(); fillConfig(inst); loadDrawerEvents(); }
  setDrawerTab(tab);
  renderDrawer();
  $$("#inst-table tr[data-id]").forEach((tr) => tr.classList.toggle("sel", +tr.dataset.id === drawerId));
  if (changed) attachRunningJob(inst);
}
function closeDrawer() {
  drawerId = null;
  clearTimeout(execTimer);
  execJob = null;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $$("#inst-table tr.sel").forEach((tr) => tr.classList.remove("sel"));
}
$("#drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawerId && !$("dialog[open]")) closeDrawer(); });
$$(".tabs button").forEach((b) => b.addEventListener("click", () => setDrawerTab(b.dataset.tab)));
function setDrawerTab(t) {
  drawerTab = t;
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  $$(".dtab").forEach((d) => d.classList.toggle("hidden", d.dataset.tab !== t));
  if (t === "exec") loadPresets();
}
function renderDrawer() {
  const i = byId(drawerId);
  if (!i) return closeDrawer();
  const manual = isManual(i);
  $("#d-name").textContent = i.name;
  const ipEl = $("#d-ip");
  ipEl.textContent = i.external_ip || "无外网 IP";
  ipEl.dataset.copy = i.external_ip || "";
  const [st, stc] = statusOf(i);
  const [cn, cnc] = CN[i.cn_state] || CN.unknown;
  $("#d-badges").innerHTML = `<span class="badge ${stc}">${st}</span><span class="badge ${cnc}">${cn}</span>`;
  const acts = [];
  const gone = isGone(i);
  if (!gone) {
    if (!manual && STOPPED.includes(i.status)) acts.push(["start", "开机", "primary"]);
    if (!manual && i.status === "RUNNING") acts.push(["stop", "关机", ""], ["reset", "重启", ""]);
    if (manual || i.status === "RUNNING") acts.push(["check", "检测", ""]);
    if (!manual && i.status === "RUNNING") acts.push(["change_ip", "换 IP", "danger"]);
    acts.push(["exec", "执行命令", "ghost"]);
  }
  $("#d-actions").innerHTML = acts.map(([a, l, c]) => `<button class="btn small ${c}" data-dact="${a}">${l}</button>`).join("");
  const kv = manual ? [
    ["类型", "自有服务器"], ["SSH", `${i.ssh_user || "root"} · 端口 ${i.ssh_port || 22}`], ["检测端口", i.check_port],
    ["被墙检测", i.auto_ip ? "开（被墙通知，需手动换 IP）" : "关"], ["分组", i.grp || "-"], ["上次检测", fmtTime(i.last_check)], ["备注", i.note || "-"],
  ] : isAzure(i) ? [
    ["类型", "Azure"], ["区域", i.zone], ["资源组", i.project], ["机型", `${i.machine_type}${i.preemptible ? " · Spot" : ""}`],
    ["内网 IP", i.internal_ip || "-"], ["IP 类型", "Standard 静态公网 IP"], ["检测端口", i.check_port],
    ["自动换 IP", i.auto_ip ? "开" : "关"], ["自动开机", i.auto_start ? "开" : "关"], ["分组", i.grp || "-"],
    ["上次检测", fmtTime(i.last_check)], ["上次换 IP", fmtTime(i.last_change_at)], ["账号", i.account_name], ["备注", i.note || "-"],
  ] : [
    ["区域", i.zone], ["机型", `${i.machine_type}${i.preemptible ? " · Spot" : ""}`], ["内网 IP", i.internal_ip || "-"],
    ["IP 类型", i.ip_mode === "static" ? "静态 IP" : "临时 IP"], ["网络层级", i.network_tier || "-"], ["检测端口", i.check_port],
    ["自动换 IP", i.auto_ip ? "开" : "关"], ["自动开机", i.auto_start ? "开" : "关"], ["自动替补", i.auto_replace ? "开" : "关"], ["分组", i.grp || "-"],
    ["上次检测", fmtTime(i.last_check)], ["上次换 IP", fmtTime(i.last_change_at)], ["项目", i.account_name], ["备注", i.note || "-"],
  ];
  $("#d-kv").innerHTML = kv.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  $("#sub-startup-btn").classList.toggle("hidden", i.provider !== "gcp");
  $("#exec-note").innerHTML = isAzure(i)
    ? `以 root 身份通过 SSH 执行 bash 脚本。首次执行会通过 Azure「运行命令」自动写入面板公钥（约 30-60 秒），需要网络安全组放行 22 端口（面板新建的实例默认全部放行）。`
    : manual
    ? `以 ${esc(i.ssh_user || "root")} 身份通过 SSH（端口 ${i.ssh_port || 22}）执行 bash 脚本。需要先在服务器上执行过一次「接入命令」（设置页复制），或添加时填写了 root 密码。`
    : `以 root 身份在实例上执行 bash 脚本并实时显示输出。首次执行会自动写入面板公钥（用户 <code>gcpvm</code>），需要防火墙放行 22 端口。`;
  renderDrawerLive();
}
function renderDrawerLive() {
  const i = byId(drawerId);
  if (!i) return;
  const r = S.rates[i.id];
  const live = r && r.online;
  $("#d-rx").textContent = live ? fmtBps(r.rx_bps) : "-";
  $("#d-tx").textContent = live ? fmtBps(r.tx_bps) : "-";
  const svc = svcOf(i);
  $("#d-svc").innerHTML = !r || !r.last_seen ? `<span class="muted">未安装上报</span>` : !live ? `<span style="color:var(--bad)">上报中断</span>`
    : svc ? `<span style="color:var(--${svc[1]})">${svc[0].replace("服务", "")}</span>` : `<span class="muted">未监控</span>`;
  drawChart($("#d-chart"), S.instHist[i.id] || []);
}
async function loadDrawerEvents() {
  const id = drawerId;
  try {
    const rows = await api(`/api/events?limit=15&instance_id=${id}`);
    if (id !== drawerId) return;
    $("#d-events").innerHTML = rows.length ? rows.map((e) => `<li class="${esc(e.level)}"><span class="tm">${fmtClock(e.ts)}</span><span class="msg">${esc(e.message)}</span></li>`).join("") : `<li class="empty-line">暂无事件</li>`;
  } catch (_) {}
}
$("#d-actions").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-dact]");
  if (!b) return;
  if (b.dataset.dact === "exec") return setDrawerTab("exec");
  await doAction(byId(drawerId), b.dataset.dact, b);
  loadDrawerEvents();
});

/* ---- 命令 ---- */
let execJob = null;
let execTimer = null;
function resetExec() {
  clearTimeout(execTimer);
  execJob = null;
  $("#exec-output").textContent = "";
  $("#exec-output").classList.add("hidden");
  $("#serial-output").textContent = "点「刷新」查看启动脚本输出";
  setExecRunning(false);
  switchSub("ssh");
}
function switchSub(name) {
  $$("#exec-subtabs button").forEach((b) => b.classList.toggle("on", b.dataset.sub === name));
  $("#sub-ssh").classList.toggle("hidden", name !== "ssh");
  $("#sub-startup").classList.toggle("hidden", name !== "startup");
  if (name === "startup") loadStartup();
}
$$("#exec-subtabs button").forEach((b) => b.addEventListener("click", () => switchSub(b.dataset.sub)));
async function loadPresets() {
  try {
    S.presets = (await api("/api/settings")).script_presets || [];
    $("#exec-preset").innerHTML = `<option value="">— 常用脚本 —</option>` + S.presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join("");
  } catch (_) {}
}
$("#exec-preset").addEventListener("change", (e) => { const p = S.presets[+e.target.value]; if (p) $("#exec-script").value = p.script; });
$("#preset-save").addEventListener("click", async () => {
  const script = $("#exec-script").value;
  if (!script.trim()) return toast("先输入脚本", true);
  const name = (prompt("常用脚本名称：") || "").trim();
  if (!name) return;
  const next = S.presets.filter((p) => p.name !== name).concat([{ name, script }]);
  try {
    S.presets = (await api("/api/settings", { method: "PUT", body: { script_presets: next } })).script_presets;
    await loadPresets();
    $("#exec-preset").value = String(S.presets.findIndex((p) => p.name === name));
    toast("已保存为常用脚本");
  } catch (e) { toast(e.message, true); }
});
$("#preset-del").addEventListener("click", async () => {
  const idx = $("#exec-preset").value;
  if (idx === "") return toast("先选择一个常用脚本", true);
  if (!confirm(`删除常用脚本「${S.presets[+idx].name}」？`)) return;
  try {
    S.presets = (await api("/api/settings", { method: "PUT", body: { script_presets: S.presets.filter((_, k) => k !== +idx) } })).script_presets;
    await loadPresets();
  } catch (e) { toast(e.message, true); }
});
async function fillTraffic(uninstall) {
  try {
    const r = await api(`/api/instances/${drawerId}/traffic-agent`, { method: "POST", body: { panel_url: location.origin, uninstall } });
    $("#exec-script").value = r.script;
    $("#exec-preset").value = "";
    toast(uninstall ? "已填入卸载脚本，点「执行」即可" : `已填入安装脚本（上报到 ${location.origin}），点「执行」即可`);
  } catch (e) { toast(e.message, true); }
}
$("#traffic-install").addEventListener("click", () => fillTraffic(false));
$("#traffic-uninstall").addEventListener("click", () => fillTraffic(true));
function setExecRunning(on, text = "") {
  $("#exec-run").disabled = on;
  $("#exec-stop").classList.toggle("hidden", !on);
  $("#exec-status").textContent = text;
}
async function attachRunningJob(inst) {
  try {
    const jobs = await api(`/api/jobs?instance_id=${inst.id}`);
    const running = jobs.find((j) => j.status === "running");
    if (running && drawerId === inst.id) { setDrawerTab("exec"); attachJob(running.id); }
  } catch (_) {}
}
function attachJob(id) {
  execJob = { id, offset: 0 };
  $("#exec-output").textContent = "";
  $("#exec-output").classList.remove("hidden");
  setExecRunning(true, "执行中…");
  pollJob();
}
async function pollJob() {
  if (!execJob) return;
  const job = execJob;
  try {
    const r = await api(`/api/jobs/${job.id}?offset=${job.offset}`);
    if (execJob !== job) return;
    const out = $("#exec-output");
    const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 30;
    if (r.output) out.textContent += r.output;
    if (atBottom) out.scrollTop = out.scrollHeight;
    job.offset = r.offset;
    if (r.status === "running") { execTimer = setTimeout(pollJob, 1000); return; }
    const label = { ok: "执行成功", failed: "执行失败", cancelled: "已停止" }[r.status] || r.status;
    setExecRunning(false, `${label}${r.exit_code !== null ? "，退出码 " + r.exit_code : ""}${r.truncated ? "（输出过长已截断）" : ""}`);
    execJob = null;
    loadDrawerEvents();
  } catch (e) { setExecRunning(false); toast(e.message, true); execJob = null; }
}
$("#exec-run").addEventListener("click", async () => {
  const script = $("#exec-script").value;
  if (!script.trim()) return toast("请输入要执行的命令", true);
  try {
    const job = await api(`/api/instances/${drawerId}/exec`, { method: "POST", body: { script, timeout: +$("#exec-timeout").value } });
    attachJob(job.id);
  } catch (e) { toast(e.message, true); }
});
$("#exec-stop").addEventListener("click", async () => {
  if (!execJob) return;
  try { await api(`/api/jobs/${execJob.id}/cancel`, { method: "POST" }); $("#exec-status").textContent = "正在停止…"; }
  catch (e) { toast(e.message, true); }
});
async function loadStartup() {
  const id = drawerId;
  try {
    const r = await api(`/api/instances/${id}/startup-script`);
    if (id !== drawerId) return;
    $("#startup-script").value = r.script;
    $("#startup-once").checked = r.has_script ? r.run_once : true;
  } catch (e) { toast(e.message, true); }
}
$("#startup-save").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const inst = byId(drawerId);
  const script = $("#startup-script").value;
  if (!script.trim()) throw new Error("请输入启动脚本");
  const reboot = $("#startup-reboot").checked;
  if (reboot && !confirm(`写入后将${inst.status === "RUNNING" ? "强制重启" : "开机"}「${inst.name}」，连接会中断，确定？`)) return;
  await api(`/api/instances/${inst.id}/startup-script`, { method: "PUT", body: { script, run_once: $("#startup-once").checked, reboot } });
  toast(reboot ? "已写入并重启，稍后点「刷新」查看启动日志" : "已写入，下次开机时执行");
  $("#startup-reboot").checked = false;
  loadInstances();
}));
$("#startup-clear").addEventListener("click", (e) => {
  if (!confirm("清除该实例的启动脚本？")) return;
  busy(e.currentTarget, async () => {
    await api(`/api/instances/${drawerId}/startup-script`, { method: "DELETE" });
    $("#startup-script").value = "";
    toast("已清除启动脚本");
  });
});
$("#serial-refresh").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const r = await api(`/api/instances/${drawerId}/serial-log${$("#serial-all").checked ? "?all=1" : ""}`);
  const out = $("#serial-output");
  out.textContent = r.contents.trim() || "暂无启动脚本输出（实例可能还没重启，或脚本尚未开始执行）";
  out.scrollTop = out.scrollHeight;
}));

/* ---- 设置 ---- */
function fillConfig(inst) {
  const f = $("#inst-form");
  const manual = isManual(inst);
  f.auto_ip.checked = !!inst.auto_ip;
  f.auto_start.checked = !!inst.auto_start;
  f.check_port.value = inst.check_port;
  f.ip_mode.value = inst.ip_mode === "static" ? "static" : "ephemeral";
  f.dns_zone_id.value = inst.dns_zone_id || "";
  f.dns_record.value = inst.dns_record || "";
  f.note.value = inst.note || "";
  f.grp.value = inst.grp || "";
  $("#manual-fields").classList.toggle("hidden", !manual);
  $("#gcp-fields").classList.toggle("hidden", manual);
  $("#gcp-fields-2").classList.toggle("hidden", manual);
  $("#replace-field").classList.toggle("hidden", inst.provider !== "gcp");
  $("#ipmode-field").classList.toggle("hidden", inst.provider !== "gcp");
  f.auto_replace.checked = !!inst.auto_replace;
  $("#auto-ip-label").textContent = manual ? "被墙检测并通知（自有服务器无法自动换 IP）" : "被墙自动更换 IP";
  const gone = isGone(inst);
  $("#danger-title").textContent = manual || gone ? "从面板移除" : "删除实例";
  $("#danger-desc").textContent = manual ? "只是从面板移除，不影响服务器本身" : gone ? "云端实例已不存在或账号被封，只删除面板记录（也会停止对它的替补）" : "实例和启动盘中的数据将无法恢复";
  $("#inst-delete").textContent = manual || gone ? "移除" : "删除";
  if (manual) {
    f.m_name.value = inst.name; f.m_ip.value = inst.external_ip || "";
    f.m_ssh_port.value = inst.ssh_port || 22; f.m_ssh_user.value = inst.ssh_user || "root";
  }
}
$("#inst-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const inst = byId(drawerId);
  await busy($("button[type=submit]", f), async () => {
    if (isManual(inst)) {
      await api(`/api/servers/${inst.id}`, { method: "PATCH", body: { name: f.m_name.value.trim(), ip: f.m_ip.value.trim(), ssh_port: +f.m_ssh_port.value, ssh_user: f.m_ssh_user.value.trim() || "root" } });
      await api(`/api/instances/${inst.id}`, { method: "PATCH", body: { auto_ip: f.auto_ip.checked, check_port: +f.check_port.value, note: f.note.value.trim(), grp: f.grp.value.trim() } });
    } else {
      await api(`/api/instances/${inst.id}`, {
        method: "PATCH",
        body: {
          auto_ip: f.auto_ip.checked, auto_start: f.auto_start.checked, check_port: +f.check_port.value,
          ...(inst.provider === "gcp" ? { ip_mode: f.ip_mode.value, auto_replace: f.auto_replace.checked } : {}),
          dns_zone_id: f.dns_zone_id.value.trim(), dns_record: f.dns_record.value.trim(), note: f.note.value.trim(), grp: f.grp.value.trim(),
        },
      });
    }
    toast("已保存");
    await loadInstances();
  });
});
$("#m-bootstrap").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const r = await api(`/api/servers/${drawerId}/bootstrap-command`, { method: "POST", body: { panel_url: location.origin } });
  await copyText(r.command, "接入命令已复制，到服务器上用 root 执行一次");
}));
$("#inst-delete").addEventListener("click", async (e) => {
  const inst = byId(drawerId);
  const name = prompt(isManual(inst)
    ? `从面板移除这台服务器（不影响服务器本身）。\n确认请输入名称：${inst.name}`
    : `删除后实例和启动盘中的数据将无法恢复！\n确认删除请输入实例名称：${inst.name}`);
  if (name === null) return;
  if (name.trim() !== inst.name) return toast("名称不匹配，未删除", true);
  await busy(e.currentTarget, async () => {
    await api(`/api/instances/${inst.id}`, { method: "DELETE", body: { confirm_name: name.trim() } });
    toast(isManual(inst) ? `已从面板移除 ${inst.name}` : `实例 ${inst.name} 已删除`);
    closeDrawer();
    loadInstances();
  });
});

/* ================= 新建实例 ================= */
function defaultName() {
  const d = new Date();
  return `vm-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}
const DISK_LABELS = {
  "pd-balanced": "平衡型 pd-balanced（推荐）", "pd-standard": "标准型 pd-standard", "pd-ssd": "SSD pd-ssd",
  StandardSSD_LRS: "标准 SSD（推荐）", Standard_LRS: "标准 HDD", Premium_LRS: "高级 SSD",
};
let createOpts = null;
async function loadCreateOptions() {
  const f = $("#create-form");
  f.zone.innerHTML = `<option value="">加载中…</option>`;
  const opts = (createOpts = await api(`/api/accounts/${f.account.value}/create-options`));
  const azure = opts.provider === "azure";
  $("#tier-field").classList.toggle("hidden", azure);
  $("#tags-field").classList.toggle("hidden", azure);
  f.disk_type.innerHTML = opts.disk_types.map((d) => `<option value="${d}">${esc(DISK_LABELS[d] || d)}</option>`).join("");
  f.disk_size_gb.min = azure ? 30 : 10;
  f.disk_size_gb.value = Math.max(+f.disk_size_gb.value || 0, opts.default_disk_gb);
  if (!azure && +f.disk_size_gb.value === 30) f.disk_size_gb.value = 20;
  $("#create-note").textContent = azure
    ? "自动创建资源组 gcpvm-区域、虚拟网络和放行全部入站的安全组；公网 IP 为 Standard 静态 IP。面板公钥在首次开机时写入 root。"
    : "面板 SSH 公钥会在创建时写入，建好即可执行命令。不附加服务账号。";
  const groups = {};
  opts.zones.forEach((z) => (groups[z.region] = groups[z.region] || []).push(z));
  f.zone.innerHTML = Object.entries(groups).map(([r, zs]) => {
    const label = zs[0].label.includes(" · ") ? `${zs[0].label.split(" · ")[0]}（${r}）` : r;
    return `<optgroup label="${esc(label)}">${zs.map((z) => `<option value="${esc(z.name)}">${esc(z.name)}</option>`).join("")}</optgroup>`;
  }).join("");
  const pref = opts.zones.find((z) => z.name === opts.default_zone) || opts.zones[0];
  if (pref) f.zone.value = pref.name;
  f.image.innerHTML = opts.images.map((i) => `<option value="${esc(i.id)}">${esc(i.label)}</option>`).join("");
  await loadMachineTypes();
}
async function loadMachineTypes() {
  const f = $("#create-form");
  const prev = f.machine_type.value;
  f.machine_type.innerHTML = `<option value="">加载中…</option>`;
  try {
    const types = await api(`/api/accounts/${f.account.value}/machine-types?zone=${encodeURIComponent(f.zone.value)}`);
    const fams = {};
    const famOf = (m) => m.family || m.name.split("-")[0];
    types.forEach((m) => (fams[famOf(m)] = fams[famOf(m)] || []).push(m));
    f.machine_type.innerHTML = Object.entries(fams).map(([fam, ms]) =>
      `<optgroup label="${esc(fam.toUpperCase())}">${ms.map((m) => `<option value="${esc(m.name)}">${esc(m.label)}</option>`).join("")}</optgroup>`).join("");
    const def = createOpts ? createOpts.default_machine_type : "e2-small";
    const want = types.find((m) => m.name === prev) || types.find((m) => m.name === def) || types[0];
    if (want) f.machine_type.value = want.name;
  } catch (e) { f.machine_type.innerHTML = `<option value="">加载失败</option>`; toast(e.message, true); }
}
async function openCreate(btn) {
  const f = $("#create-form");
  await busy(btn, async () => {
    const accounts = await api("/api/accounts");
    const usable = accounts.filter((a) => a.status !== "banned");
    if (!usable.length) throw new Error("请先在「账号与服务器」添加 GCP 或 Azure 账号");
    f.account.innerHTML = usable.map((a) => `<option value="${a.id}">${a.kind === "azure" ? "Azure · " : "GCP · "}${esc(a.name)}（${a.instances} 台）</option>`).join("");
    f.name.value = defaultName();
    $("#create-hint").textContent = "";
    await loadPresets();
    $("#create-preset").innerHTML = `<option value="">— 从常用脚本填入 —</option>` + S.presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join("");
    await loadCreateOptions();
    $("#create-dialog").showModal();
  });
}
$("#create-close").addEventListener("click", () => $("#create-dialog").close());
$("#create-form").account.addEventListener("change", () => loadCreateOptions().catch((e) => toast(e.message, true)));
$("#create-form").zone.addEventListener("change", loadMachineTypes);
$("#create-form").spot.addEventListener("change", (e) => { if (e.target.checked) $("#create-form").auto_start.checked = true; });
$("#create-preset").addEventListener("change", (e) => { const p = S.presets[+e.target.value]; if (p) $("#create-form").startup_script.value = p.script; });
$("#create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  if (!f.machine_type.value) return toast("请选择机型", true);
  const body = {
    name: f.name.value.trim(), zone: f.zone.value, machine_type: f.machine_type.value, image: f.image.value,
    disk_size_gb: +f.disk_size_gb.value, disk_type: f.disk_type.value, network_tier: f.network_tier.value,
    spot: f.spot.checked, auto_ip: f.auto_ip.checked, auto_start: f.auto_start.checked, check_port: +f.check_port.value,
    tags: f.tags.value.trim(), note: f.note.value.trim(), startup_script: f.startup_script.value, run_once: true,
  };
  $("#create-hint").textContent = createOpts && createOpts.provider === "azure" ? "正在创建，Azure 通常需要 1-3 分钟…" : "正在创建，通常需要 30-60 秒…";
  await busy($("button[type=submit]", f), async () => {
    try {
      const r = await api(`/api/accounts/${f.account.value}/instances`, { method: "POST", body });
      toast(`实例 ${r.instance.name} 已创建，IP ${r.instance.external_ip || "-"}`);
      $("#create-dialog").close();
      f.startup_script.value = ""; f.note.value = ""; f.tags.value = "";
      await loadInstances();
      openDrawer(r.instance.id);
    } finally { $("#create-hint").textContent = ""; }
  });
});

/* ================= 防火墙 ================= */
async function loadFirewall() {
  const sel = $("#fw-account");
  try {
    const accounts = (await api("/api/accounts")).filter((a) => a.kind === "gcp" && a.status !== "banned");
    const prev = sel.value;
    sel.innerHTML = accounts.map((a) => `<option value="${a.id}">${esc(a.name)}（${esc(a.project_id)}）</option>`).join("");
    if (!accounts.length) { $("#fw-table tbody").innerHTML = `<tr><td colspan="8" class="muted">还没有 GCP 账号，先到「账号与服务器」添加</td></tr>`; return; }
    if (prev && accounts.some((a) => String(a.id) === prev)) sel.value = prev;
    await loadFirewallRules();
  } catch (e) { toast(e.message, true); }
}
async function loadFirewallRules() {
  const aid = $("#fw-account").value;
  if (!aid) return;
  const tbody = $("#fw-table tbody");
  tbody.innerHTML = `<tr><td colspan="8" class="muted">加载中…</td></tr>`;
  try {
    const r = await api(`/api/accounts/${aid}/firewalls`);
    const net = $("#fw-form").network;
    const cur = net.value;
    net.innerHTML = r.networks.map((n) => `<option>${esc(n)}</option>`).join("") || `<option>default</option>`;
    net.value = r.networks.includes(cur) ? cur : r.networks.includes("default") ? "default" : net.value;
    tbody.innerHTML = r.rules.length ? r.rules.map((f) => `<tr class="${f.disabled ? "off" : ""}" title="${esc(f.description)}">
      <td data-l="名称"><div><div class="name">${esc(f.name)}</div><div class="sub">${esc(f.network)}${f.disabled ? " · 已禁用" : ""}</div></div></td>
      <td data-l="方向">${f.direction === "INGRESS" ? "入站" : "出站"}</td>
      <td data-l="动作"><span class="badge ${f.action === "allow" ? "ok" : "bad"}">${f.action === "allow" ? "允许" : "拒绝"}</span></td>
      <td data-l="端口" class="rules">${esc(f.rules)}</td>
      <td data-l="来源" class="rules">${esc(f.ranges)}</td>
      <td data-l="作用于">${esc(f.targets)}</td>
      <td data-l="优先级">${f.priority}</td>
      <td><div class="ops"><button class="btn small ghost" data-fw-toggle="${esc(f.name)}" data-disabled="${f.disabled ? 1 : 0}">${f.disabled ? "启用" : "禁用"}</button>
        <button class="btn small ghost danger" data-fw-del="${esc(f.name)}">删除</button></div></td></tr>`).join("")
      : `<tr><td colspan="8" class="muted">没有防火墙规则</td></tr>`;
  } catch (e) { tbody.innerHTML = `<tr><td colspan="8" class="error">${esc(e.message)}</td></tr>`; }
}
$("#fw-account").addEventListener("change", loadFirewallRules);
$("#fw-refresh").addEventListener("click", loadFirewallRules);
$("#fw-table").addEventListener("click", async (e) => {
  const aid = $("#fw-account").value;
  const tog = e.target.closest("[data-fw-toggle]");
  const del = e.target.closest("[data-fw-del]");
  if (tog) {
    const name = tog.dataset.fwToggle;
    const disable = tog.dataset.disabled !== "1";
    if (disable && name === "default-allow-ssh" && !confirm("禁用 default-allow-ssh 后，「执行命令」将无法连接 GCP 实例，确定？")) return;
    await busy(tog, async () => {
      await api(`/api/accounts/${aid}/firewalls/${encodeURIComponent(name)}`, { method: "PATCH", body: { disabled: disable } });
      toast(`已${disable ? "禁用" : "启用"} ${name}`);
      await loadFirewallRules();
    });
  } else if (del) {
    const name = del.dataset.fwDel;
    if (!confirm(`删除防火墙规则「${name}」？${name === "default-allow-ssh" ? "\n注意：删除后「执行命令」将无法连接 GCP 实例！" : ""}`)) return;
    await busy(del, async () => {
      await api(`/api/accounts/${aid}/firewalls/${encodeURIComponent(name)}`, { method: "DELETE" });
      toast(`已删除 ${name}`);
      await loadFirewallRules();
    });
  }
});
const FW_QUICK = {
  all: { name: "allow-all-ingress", rules: "all", description: "放行全部端口" },
  web: { name: "allow-http-https", rules: "tcp:80,443 udp:443", description: "放行 80/443" },
  proxy: { name: "allow-all-tcp-udp", rules: "tcp udp", description: "放行全部 TCP/UDP 端口" },
};
$$("[data-quick]").forEach((b) => b.addEventListener("click", () => {
  const q = FW_QUICK[b.dataset.quick];
  const f = $("#fw-form");
  Object.assign(f, {});
  f.name.value = q.name; f.rules.value = q.rules; f.description.value = q.description;
  f.direction.value = "INGRESS"; f.action.value = "allow"; f.ranges.value = "0.0.0.0/0"; f.priority.value = 1000; f.target_tags.value = "";
  f.name.focus();
}));
$("#fw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const aid = $("#fw-account").value;
  if (!aid) return toast("请先添加 GCP 账号", true);
  const body = Object.fromEntries(["name", "network", "direction", "action", "rules", "ranges", "priority", "target_tags", "description"].map((k) => [k, f.elements[k].value.trim()]));
  if (body.action === "allow" && body.direction === "INGRESS" && body.rules.toLowerCase() === "all" && body.ranges.includes("0.0.0.0/0")
    && !confirm("将对所有 IP 放行所有端口，实例上监听的所有服务都会暴露到公网，确定？")) return;
  await busy($("button[type=submit]", f), async () => {
    await api(`/api/accounts/${aid}/firewalls`, { method: "POST", body });
    toast(`规则 ${body.name} 已创建，几秒内生效`);
    f.name.value = ""; f.rules.value = ""; f.description.value = "";
    await loadFirewallRules();
  });
});

/* ================= 账号与服务器 ================= */
async function loadAccounts() {
  try {
    const rows = await api("/api/accounts");
    const cap = (S.settings && S.settings.replace_max_per_account) || 3;
    $("#accounts-table tbody").innerHTML = rows.length
      ? rows.map((a) => `<tr>
          <td data-l="名称"><div><div class="name">${esc(a.name)}</div><div class="sub mono">${esc(a.client_email)}</div></div></td>
          <td data-l="类型"><span class="tag ${a.kind === "azure" ? "" : "on"}">${a.kind === "azure" ? "Azure" : "GCP"}</span></td>
          <td data-l="项目" class="mono small">${esc(a.project_id)}</td>
          <td data-l="实例">${a.instances}${a.kind === "gcp" ? `<span class="muted"> / ${cap}</span>` : ""}</td>
          <td data-l="状态">${a.status === "banned"
            ? `<span class="badge bad" title="${esc(a.status_msg)}">被封 / 停用</span>`
            : `<span class="badge ok">正常</span>`}</td>
          <td class="r"><div class="ops">
            ${a.status === "banned" ? `<button class="btn small ghost" data-reset="${a.id}">解除标记</button>` : ""}
            <button class="btn small ghost danger" data-del="${a.id}" data-name="${esc(a.name)}">删除</button></div></td></tr>`).join("")
      : `<tr><td colspan="6" class="muted">暂无云账号</td></tr>`;
  } catch (e) { toast(e.message, true); }
}
$("#accounts-table").addEventListener("click", async (e) => {
  const r = e.target.closest("[data-reset]");
  if (r) {
    if (!confirm("确认账号已恢复可用？解除后会重新同步、参与替补。")) return;
    return busy(r, async () => { await api(`/api/accounts/${r.dataset.reset}/reset-status`, { method: "POST" }); toast("已解除标记"); loadAccounts(); });
  }
  const b = e.target.closest("[data-del]");
  if (!b || !confirm(`删除账号「${b.dataset.name}」？其下实例的管理配置也会一并删除（不影响云上的实例）。`)) return;
  await busy(b, async () => { await api(`/api/accounts/${b.dataset.del}`, { method: "DELETE" }); toast("已删除"); loadAccounts(); });
});
let acctKind = "gcp";
$("#acct-kind").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  acctKind = b.dataset.k;
  $$("#acct-kind button").forEach((x) => x.classList.toggle("on", x === b));
  $$("#account-form [data-kind]").forEach((d) => d.classList.toggle("hidden", d.dataset.kind !== acctKind));
});
$("#az-paste").addEventListener("input", (e) => {
  const f = $("#account-form");
  try {
    const j = JSON.parse(e.target.value);
    if (j.appId) f.client_id.value = j.appId;
    if (j.password) f.client_secret.value = j.password;
    if (j.tenant) f.tenant_id.value = j.tenant;
    if (j.subscriptionId || j.subscription_id) f.subscription_id.value = j.subscriptionId || j.subscription_id;
    if (j.displayName && !f.name.value) f.name.value = j.displayName;
  } catch (_) {}
});
$("#key-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (file) $("#account-form").key.value = await file.text();
});
$("#account-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  await busy($("button[type=submit]", f), async () => {
    const body = acctKind === "azure"
      ? { kind: "azure", name: f.name.value.trim(), subscription_id: f.subscription_id.value.trim(), tenant_id: f.tenant_id.value.trim(), client_id: f.client_id.value.trim(), client_secret: f.client_secret.value.trim() }
      : { name: f.name.value.trim(), key: f.key.value.trim() };
    const acc = await api("/api/accounts", { method: "POST", body });
    toast(`账号 ${acc.name} 添加成功，已同步实例`);
    f.reset();
    $("#az-paste").value = "";
    loadAccounts();
  });
});
$("#server-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  await busy($("button[type=submit]", f), async () => {
    const r = await api("/api/servers", {
      method: "POST",
      body: {
        name: f.name.value.trim(), ip: f.ip.value.trim(), ssh_port: +f.ssh_port.value, ssh_user: f.ssh_user.value.trim() || "root",
        password: f.password.value, grp: f.grp.value.trim(), check_port: +f.check_port.value, auto_ip: f.auto_ip.checked,
        note: f.note.value.trim(), panel_url: location.origin,
      },
    });
    f.password.value = "";
    f.reset();
    if (r.job) {
      toast(`已添加 ${r.instance.name}，正在用密码接入（写入面板公钥 + 安装上报）…`);
      location.hash = "#/instances";
      await loadInstances();
      openDrawer(r.instance.id, "exec");
    } else {
      const c = await api(`/api/servers/${r.instance.id}/bootstrap-command`, { method: "POST", body: { panel_url: location.origin } });
      await copyText(c.command, `已添加 ${r.instance.name}。接入命令已复制，请到服务器上用 root 执行一次`);
    }
  });
});

/* ================= 设置 ================= */
function probeRow(p = {}) {
  const row = document.createElement("div");
  row.className = "probe-row";
  row.innerHTML = `<input placeholder="名称，如 上海电信" value="${esc(p.name)}" data-k="name" aria-label="探针名称">
    <input placeholder="http://国内机器IP:9800" value="${esc(p.url)}" data-k="url" aria-label="探针地址">
    <input placeholder="令牌 PROBE_TOKEN" value="${esc(p.token)}" data-k="token" type="password" autocomplete="off" aria-label="探针令牌">
    <button type="button" class="btn small ghost danger">移除</button>`;
  $("button", row).addEventListener("click", () => { row.remove(); markDirty(); });
  return row;
}
function pushRow(p = {}) {
  const row = document.createElement("div");
  row.className = "push-row";
  const st = S.pushStatus[p.name];
  const badge = !p.name ? "" : st && st.online
    ? `<span class="badge ok" title="上次连接 ${fmtTime(st.last_seen)}">在线</span>`
    : `<span class="badge idle" title="${st && st.last_seen ? "上次连接 " + fmtTime(st.last_seen) : "尚未连接过"}">离线</span>`;
  row.innerHTML = `<input placeholder="名称，如 家里电信" value="${esc(p.name)}" data-k="name" aria-label="探针名称">
    <input value="${esc(p.token || genToken())}" data-k="token" class="mono" autocomplete="off" spellcheck="false" aria-label="探针令牌">
    <span>${badge}</span>
    <button type="button" class="btn small" data-install>安装命令</button>
    <button type="button" class="btn small ghost danger" data-remove>移除</button>`;
  $("[data-remove]", row).addEventListener("click", () => { row.remove(); markDirty(); });
  $("[data-install]", row).addEventListener("click", () => showInstall(row));
  return row;
}
function showInstall(row) {
  const tok = $("[data-k=token]", row).value.trim();
  const panel = location.origin;
  const out = $("#push-install");
  out.textContent = [
    "# 1) 在 iStoreOS / OpenWrt 上（SSH 登录路由器后执行）",
    "opkg update && opkg install python3",
    "mkdir -p /root/gcpvm-probe && cd /root/gcpvm-probe",
    `wget -O probe.py ${panel}/probe/probe.py`,
    `wget -O /etc/init.d/gcpvm-probe ${panel}/probe/gcpvm-probe.openwrt && chmod +x /etc/init.d/gcpvm-probe`,
    `sed -i 's#^PANEL=.*#PANEL="${panel}"#; s#^TOKEN=.*#TOKEN="${tok}"#' /etc/init.d/gcpvm-probe`,
    "/etc/init.d/gcpvm-probe enable && /etc/init.d/gcpvm-probe start",
    "logread -e gcpvm   # 看到「已连接面板」即成功",
    "",
    "# 2) 或在任意 Linux 上前台试运行",
    `PROBE_PANEL=${panel} PROBE_TOKEN=${tok} python3 probe.py`,
    "",
    "# 别忘了点「保存设置」，否则令牌不生效",
  ].join("\n");
  out.classList.remove("hidden");
}
function toggleProbeBlock() {
  const mode = $("#settings-form").querySelector("[name=detect_mode]:checked")?.value;
  $("#probes-block").classList.toggle("hidden", mode === "local");
}
function statusEmbed() {
  const f = $("#settings-form");
  const url = `${location.origin}/status?key=${encodeURIComponent(f.status_page_key.value.trim())}&theme=${$("#status-theme").value}`;
  $("#status-open").href = url;
  $("#status-embed").value = `<iframe src="${url}" width="100%" height="${+$("#status-height").value || 260}" style="border:0;display:block" loading="lazy"></iframe>`;
}
function markDirty() { $(".savebar").classList.add("dirty"); $("#save-hint").textContent = "有未保存的修改"; }
function clearDirty() { $(".savebar").classList.remove("dirty"); $("#save-hint").textContent = "修改后记得保存"; }
async function loadSettings(sub) {
  try {
    const [s, push] = await Promise.all([api("/api/settings"), api("/api/push-probes").catch(() => [])]);
    S.settings = s;
    S.pushStatus = Object.fromEntries(push.map((p) => [p.name, p]));
    const f = $("#settings-form");
    for (const [k, v] of Object.entries(s)) {
      if (["probes", "push_probes", "script_presets", "replace_template", "panel_url"].includes(k)) continue;
      if (k === "detect_mode") { $$("[name=detect_mode]", f).forEach((r) => (r.checked = r.value === v)); continue; }
      const el = f.elements[k];
      if (!el || !el.tagName) continue;
      if (el.type === "checkbox") el.checked = !!v; else el.value = v;
    }
    const tpl = s.replace_template || {};
    $$("#replace-sec [data-t]").forEach((el) => {
      const v = tpl[el.dataset.t];
      if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? "";
    });
    const pbox = $("#push-probes");
    pbox.innerHTML = "";
    (s.push_probes || []).forEach((p) => pbox.appendChild(pushRow(p)));
    const box = $("#probes");
    box.innerHTML = "";
    (s.probes || []).forEach((p) => box.appendChild(probeRow(p)));
    $("#push-install").classList.add("hidden");
    toggleProbeBlock();
    statusEmbed();
    suggestStatusHeight();
    clearDirty();
  } catch (e) { toast(e.message, true); }
  scrollToSection(sub);
}
function scrollToSection(sub) {
  const sec = sub || S.sec || "detect";
  S.sec = sec;
  $$("#settings-nav a").forEach((a) => a.classList.toggle("on", a.dataset.sec === sec));
  const el = $(`#settings-form .sec[data-sec="${sec}"]`);
  if (el && sub) el.scrollIntoView({ behavior: "smooth", block: "start" });
}
async function suggestStatusHeight() {
  const h = $("#status-height");
  if (h.dataset.touched) return;
  try {
    const groups = new Set((S.instances.length ? S.instances : await api("/api/instances")).map((i) => (i.grp || "").trim()).filter(Boolean));
    h.value = 196 + 64 * Math.max(1, groups.size);
    statusEmbed();
  } catch (_) {}
}
$("#settings-form").addEventListener("input", (e) => { if (!["status-theme", "status-height", "status-embed"].includes(e.target.id)) markDirty(); });
$$("[name=detect_mode]").forEach((r) => r.addEventListener("change", toggleProbeBlock));
$("#add-probe").addEventListener("click", () => { $("#probes").appendChild(probeRow()); markDirty(); });
$("#add-push-probe").addEventListener("click", () => { const r = pushRow(); $("#push-probes").appendChild(r); $("[data-k=name]", r).focus(); markDirty(); });
$("#status-key-gen").addEventListener("click", () => { $("#settings-form").status_page_key.value = genToken(); statusEmbed(); markDirty(); });
["#status-theme", "#status-height"].forEach((s) => $(s).addEventListener("input", statusEmbed));
$("#status-height").addEventListener("input", (e) => (e.target.dataset.touched = "1"));
$("#settings-form").status_page_key.addEventListener("input", statusEmbed);
$("#status-copy").addEventListener("click", () => copyText($("#status-embed").value, "已复制，粘贴到 NY「站点公告」并保存"));
$("#settings-form").status_page_enabled.addEventListener("change", (e) => {
  const f = $("#settings-form");
  if (e.target.checked && !f.status_page_key.value.trim()) { f.status_page_key.value = genToken(); statusEmbed(); }
});
$("#tg-test").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const f = $("#settings-form");
  const res = await fetch("/api/telegram-test", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ telegram_bot_token: f.telegram_bot_token.value.trim(), telegram_chat_id: f.telegram_chat_id.value.trim() }),
  });
  const r = await res.json().catch(() => ({}));
  if (res.status === 401) { logout(); throw new Error("登录已过期"); }
  toast(r.message || r.error || `HTTP ${res.status}`, !r.ok);
}));
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const body = {
    detect_mode: f.querySelector("[name=detect_mode]:checked")?.value || "agent",
    control_check: f.control_check.checked,
    verify_new_ip: f.verify_new_ip.checked,
    status_page_enabled: f.status_page_enabled.checked,
    status_page_title: f.status_page_title.value.trim(),
    status_page_key: f.status_page_key.value.trim(),
    service_alert_enabled: f.service_alert_enabled.checked,
    watch_service: f.watch_service.value.trim(),
    service_alert_seconds: +f.service_alert_seconds.value || 60,
    panel_url: location.origin,
    replace_enabled: f.replace_enabled.checked,
    replace_max_per_account: +f.replace_max_per_account.value || 3,
    replace_max_per_day: +f.replace_max_per_day.value || 6,
    replace_template: Object.fromEntries($$("#replace-sec [data-t]").map((el) => [el.dataset.t,
      el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value])),
    probes: $$(".probe-row").map((r) => Object.fromEntries($$("input", r).map((i) => [i.dataset.k, i.value.trim()]))).filter((p) => p.url),
    push_probes: $$(".push-row").map((r) => Object.fromEntries($$("input", r).map((i) => [i.dataset.k, i.value.trim()]))).filter((p) => p.name || p.token),
  };
  for (const k of ["check_interval", "fail_threshold", "tcp_timeout", "tcp_tries", "cn_success_ratio", "cooldown_minutes", "max_changes_per_day"]) body[k] = +f.elements[k].value;
  for (const k of ["telegram_bot_token", "telegram_chat_id", "cf_api_token"]) body[k] = f.elements[k].value.trim();
  await busy($("button[type=submit]", f), async () => {
    await api("/api/settings", { method: "PUT", body });
    toast("设置已保存");
    S.settings = null;
    await loadSettings();
    loadProbeStatus();
  });
});
$("#replace-create").addEventListener("click", (e) => {
  if ($(".savebar").classList.contains("dirty")) return toast("请先保存设置，再按模板新建", true);
  if (!confirm("按替补模板，在第一个有空位的 GCP 账号新建一台实例？")) return;
  busy(e.currentTarget, async () => {
    toast("正在创建，通常需要 30-60 秒…");
    const r = await api("/api/replace/create", { method: "POST" });
    toast(`已创建 ${r.instance.name}（${r.instance.account_name}），IP ${r.instance.external_ip || "-"}`);
  });
});
$("#probe-test-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  await busy($("button", f), async () => {
    const r = await api("/api/probe-test", { method: "POST", body: { host: f.host.value.trim(), port: +f.port.value } });
    const lines = [`结论：${(CN[r.state] || [r.state])[0]} — ${r.detail}`];
    (r.cn || []).forEach((p) => lines.push(`  [国内] ${p.source}: ${p.ok === null ? "探针异常 " + p.error : p.ok ? `可达 ${p.latency_ms} ms` : "不可达 " + p.error}`));
    if (r.control) lines.push(`  [海外] ${r.control.source}: ${r.control.ok ? `可达 ${r.control.latency_ms} ms` : "不可达 " + r.control.error}`);
    const out = $("#probe-test-result");
    out.textContent = lines.join("\n");
    out.classList.remove("hidden");
  });
});

/* ================= 事件日志 ================= */
async function loadEvents() {
  try { S.events = await api("/api/events?limit=300"); renderEvents(); } catch (e) { toast(e.message, true); }
}
function renderEvents() {
  const lv = S.evFilter;
  const rows = S.events.filter((e) => lv === "all" || (lv === "warn" ? ["warn", "error"].includes(e.level) : e.level === "error"));
  $$("#ev-filter button").forEach((b) => b.classList.toggle("on", b.dataset.f === lv));
  $("#events-table tbody").innerHTML = rows.length
    ? rows.map((e) => `<tr class="lvl-${esc(e.level)}"><td class="mono small">${fmtTime(e.ts)}</td><td>${esc(e.instance_name || "-")}</td><td><span class="tag">${esc(EVT[e.type] || e.type)}</span></td><td>${esc(e.message)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">暂无事件</td></tr>`;
}
$("#ev-filter").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.evFilter = b.dataset.f; renderEvents(); } });
$("#btn-events-refresh").addEventListener("click", loadEvents);

/* ================= 启动 ================= */
function start() {
  showApp(true);
  stopTimers();
  if (!location.hash) location.hash = "#/overview";
  route();
  loadProbeStatus();
  timers.push(setInterval(loadRates, 2000));
  timers.push(setInterval(() => {
    if (["overview", "instances"].includes(S.view) && !$("dialog[open]")) loadInstances();
    if (S.view === "overview") loadOverviewEvents();
  }, 20000));
  timers.push(setInterval(loadProbeStatus, 30000));
}
applyTheme(store.get("gcpvm_theme") || "dark");
if (token) start(); else showApp(false);
