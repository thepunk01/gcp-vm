"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let token = "";
try { token = localStorage.getItem("gcpvm_token") || ""; } catch (_) {}
let instances = [];
let refreshTimer = null;

// ---------------- api ----------------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (res.status === 401 && path !== "/api/login") { logout(); throw new Error("登录已过期"); }
  if (!res.ok) throw new Error((data && data.error) || `请求失败（HTTP ${res.status}）`);
  return data;
}

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  if (isErr) el.className = "err";
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), isErr ? 7000 : 3500);
}

async function busy(btn, fn) {
  const text = btn.textContent;
  btn.disabled = true;
  btn.textContent = "处理中…";
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = text; }
}

const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "-");

// ---------------- auth ----------------
function showMain(on) {
  $("#login-view").classList.toggle("hidden", on);
  $("#main-view").classList.toggle("hidden", !on);
}
function logout() {
  token = "";
  try { localStorage.removeItem("gcpvm_token"); } catch (_) {}
  clearInterval(refreshTimer);
  showMain(false);
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    const { token: t } = await api("/api/login", { method: "POST", body: { password: $("#login-password").value } });
    token = t;
    try { localStorage.setItem("gcpvm_token", t); } catch (_) {}
    $("#login-password").value = "";
    start();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", logout);

// ---------------- tabs ----------------
$$(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
function switchTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${name}`));
  ({ instances: loadInstances, firewall: loadFirewall, accounts: loadAccounts, settings: loadSettings, events: loadEvents })[name]();
}

// ---------------- instances ----------------
const STATUS = {
  RUNNING: ["运行中", "ok"], TERMINATED: ["已关机", "idle"], STOPPED: ["已关机", "idle"],
  STOPPING: ["关机中", "warn"], PROVISIONING: ["创建中", "warn"], STAGING: ["启动中", "warn"],
  SUSPENDED: ["已挂起", "idle"], SUSPENDING: ["挂起中", "warn"], REPAIRING: ["修复中", "warn"],
};
const CN = { ok: ["国内可达", "ok"], blocked: ["疑似被墙", "bad"], down: ["服务不可达", "warn"], unknown: ["未检测", "idle"] };

async function loadInstances() {
  try { instances = await api("/api/instances"); renderInstances(); }
  catch (e) { toast(e.message, true); }
}

function renderInstances() {
  const running = instances.filter((i) => i.status === "RUNNING").length;
  const blocked = instances.filter((i) => i.cn_state === "blocked").length;
  const auto = instances.filter((i) => i.auto_ip).length;
  $("#stats").innerHTML = `<span><b>${instances.length}</b>台实例</span><span><b>${running}</b>运行中</span><span><b>${auto}</b>开启自动换 IP</span><span><b style="color:var(--bad)">${blocked}</b>疑似被墙</span>`;

  const box = $("#instances");
  if (!instances.length) {
    box.innerHTML = `<div class="empty">还没有实例。先到「账号」页添加 GCP 服务账号，然后点击「从 GCP 同步」。</div>`;
    return;
  }
  box.innerHTML = instances.map((i) => {
    const [st, stc] = STATUS[i.status] || [i.status || "未知", "idle"];
    const [cn, cnc] = CN[i.cn_state] || CN.unknown;
    const running = i.status === "RUNNING";
    const stopped = ["TERMINATED", "STOPPED", "SUSPENDED"].includes(i.status);
    return `<div class="card" data-id="${i.id}">
      <div>
        <div class="name">${esc(i.name)}</div>
        <div class="sub">${esc(i.account_name)} · ${esc(i.zone)} · ${esc(i.machine_type)}${i.preemptible ? " · Spot" : ""}</div>
        ${i.note ? `<div class="sub">${esc(i.note)}</div>` : ""}
      </div>
      <div>
        <div class="ip" title="点击复制" data-copy="${esc(i.external_ip)}">${esc(i.external_ip) || "<span class='muted'>无外网 IP</span>"}</div>
        <div class="sub">内网 ${esc(i.internal_ip) || "-"}${i.dns_record ? " · " + esc(i.dns_record) : ""}</div>
      </div>
      <div>
        <div class="flags"><span class="badge ${stc}">${st}</span><span class="badge ${cnc}" title="上次检测 ${fmtTime(i.last_check)}">${cn}${i.cn_state === "blocked" ? ` ${i.fail_count}` : ""}</span></div>
        <div class="flags">
          <span class="tag ${i.auto_ip ? "on" : ""}">自动换IP ${i.auto_ip ? "开" : "关"}</span>
          <span class="tag ${i.auto_start ? "on" : ""}">自动开机 ${i.auto_start ? "开" : "关"}</span>
          <span class="tag">端口 ${i.check_port}</span>
          <span class="tag">${i.ip_mode === "static" ? "静态IP" : "临时IP"}</span>
        </div>
      </div>
      <div class="actions">
        ${stopped ? `<button class="btn small primary" data-act="start">开机</button>` : ""}
        ${running ? `<button class="btn small" data-act="stop">关机</button>` : ""}
        ${running ? `<button class="btn small" data-act="reset">重启</button>` : ""}
        ${running ? `<button class="btn small" data-act="check">检测</button>` : ""}
        ${running ? `<button class="btn small danger" data-act="change_ip">换 IP</button>` : ""}
        <button class="btn small ghost" data-act="exec">命令</button>
        <button class="btn small ghost" data-act="settings">设置</button>
      </div>
    </div>`;
  }).join("");
}

const CONFIRM = {
  stop: (i) => `确定关闭实例「${i.name}」？`,
  reset: (i) => `确定强制重启实例「${i.name}」？（相当于断电重启）`,
  change_ip: (i) => `确定为「${i.name}」更换外网 IP？当前 IP ${i.external_ip || "-"} 将被释放，连接会中断。`,
};

$("#instances").addEventListener("click", async (e) => {
  const copy = e.target.closest("[data-copy]");
  if (copy && copy.dataset.copy) {
    try { await navigator.clipboard.writeText(copy.dataset.copy); toast(`已复制 ${copy.dataset.copy}`); } catch (_) {}
    return;
  }
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const id = +btn.closest(".card").dataset.id;
  const inst = instances.find((x) => x.id === id);
  const act = btn.dataset.act;
  if (act === "settings") return openSettings(inst);
  if (act === "exec") return openExec(inst);
  if (CONFIRM[act] && !confirm(CONFIRM[act](inst))) return;
  await busy(btn, async () => {
    const r = await api(`/api/instances/${id}/action`, { method: "POST", body: { action: act } });
    if (act === "check") {
      const c = r.check;
      const cn = (c.cn || []).map((p) => `${p.source}: ${p.ok === null ? "探针异常" : p.ok ? `通 ${p.latency_ms}ms` : "不通"}`).join("；");
      toast(`${CN[c.state]?.[0] || c.state}：${c.detail}${cn ? "（" + cn + "）" : ""}`, c.state === "blocked");
    } else if (act === "change_ip") {
      toast(`新 IP：${r.instance.external_ip}`);
    } else {
      toast(`操作成功，当前状态 ${STATUS[r.instance.status]?.[0] || r.instance.status}`);
    }
    await loadInstances();
  });
});

$("#btn-sync").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const r = await api("/api/sync", { method: "POST" });
  instances = r.instances;
  renderInstances();
  const parts = Object.entries(r.result).map(([k, v]) => `${k}: ${typeof v === "number" ? v + " 台" : v}`);
  toast(parts.length ? `同步完成：${parts.join("；")}` : "还没有添加账号");
}));

$("#btn-run-check").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const r = await api("/api/run-check", { method: "POST" });
  toast(r.message);
}));

// ---- 实例设置弹窗 ----
let editing = null;
function openSettings(inst) {
  editing = inst;
  const f = $("#inst-form");
  $("#inst-dialog-title").textContent = `实例设置 · ${inst.name}`;
  f.auto_ip.checked = !!inst.auto_ip;
  f.auto_start.checked = !!inst.auto_start;
  f.check_port.value = inst.check_port;
  f.ip_mode.value = inst.ip_mode;
  f.dns_zone_id.value = inst.dns_zone_id || "";
  f.dns_record.value = inst.dns_record || "";
  f.note.value = inst.note || "";
  $("#inst-dialog").showModal();
}
$("#inst-dialog").addEventListener("close", async () => {
  if ($("#inst-dialog").returnValue !== "save" || !editing) return;
  const f = $("#inst-form");
  try {
    await api(`/api/instances/${editing.id}`, {
      method: "PATCH",
      body: {
        auto_ip: f.auto_ip.checked, auto_start: f.auto_start.checked, check_port: +f.check_port.value,
        ip_mode: f.ip_mode.value, dns_zone_id: f.dns_zone_id.value.trim(), dns_record: f.dns_record.value.trim(),
        note: f.note.value.trim(),
      },
    });
    toast("已保存");
    loadInstances();
  } catch (e) { toast(e.message, true); }
});

// ---- 执行命令弹窗 ----
let execInst = null;
let execJob = null;
let execTimer = null;
let presets = [];

function switchSub(name) {
  $$(".subtabs button").forEach((b) => b.classList.toggle("active", b.dataset.sub === name));
  $("#sub-ssh").classList.toggle("hidden", name !== "ssh");
  $("#sub-startup").classList.toggle("hidden", name !== "startup");
  if (name === "startup") loadStartup();
}
$$(".subtabs button").forEach((b) => b.addEventListener("click", () => switchSub(b.dataset.sub)));

function renderPresets() {
  $("#exec-preset").innerHTML = `<option value="">— 常用脚本 —</option>` +
    presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join("");
}
async function loadPresets() {
  try { presets = (await api("/api/settings")).script_presets || []; renderPresets(); } catch (e) { toast(e.message, true); }
}
$("#exec-preset").addEventListener("change", (e) => {
  const p = presets[+e.target.value];
  if (p) $("#exec-script").value = p.script;
});
$("#preset-save").addEventListener("click", async () => {
  const script = $("#exec-script").value;
  if (!script.trim()) return toast("先输入脚本", true);
  const name = prompt("常用脚本名称：");
  if (!name || !name.trim()) return;
  const next = presets.filter((p) => p.name !== name.trim()).concat([{ name: name.trim(), script }]);
  try {
    presets = (await api("/api/settings", { method: "PUT", body: { script_presets: next } })).script_presets;
    renderPresets();
    $("#exec-preset").value = String(presets.findIndex((p) => p.name === name.trim()));
    toast("已保存为常用脚本");
  } catch (e) { toast(e.message, true); }
});
$("#preset-del").addEventListener("click", async () => {
  const i = $("#exec-preset").value;
  if (i === "") return toast("先选择一个常用脚本", true);
  if (!confirm(`删除常用脚本「${presets[+i].name}」？`)) return;
  try {
    presets = (await api("/api/settings", { method: "PUT", body: { script_presets: presets.filter((_, k) => k !== +i) } })).script_presets;
    renderPresets();
  } catch (e) { toast(e.message, true); }
});

function setExecRunning(on, statusText = "") {
  $("#exec-run").disabled = on;
  $("#exec-stop").classList.toggle("hidden", !on);
  $("#exec-status").textContent = statusText;
}

async function openExec(inst) {
  execInst = inst;
  execJob = null;
  clearTimeout(execTimer);
  $("#exec-title").textContent = `执行命令 · ${inst.name}（${inst.external_ip || "无外网 IP"}）`;
  $("#exec-output").textContent = "";
  $("#exec-output").classList.add("hidden");
  $("#serial-output").textContent = "点「刷新」查看最近的启动脚本输出";
  setExecRunning(false);
  switchSub(inst.status === "RUNNING" ? "ssh" : "startup");
  $("#exec-dialog").showModal();
  loadPresets();
  // 若该实例已有任务在执行，接上输出
  try {
    const jobs = await api(`/api/jobs?instance_id=${inst.id}`);
    const running = jobs.find((j) => j.status === "running");
    if (running) attachJob(running.id);
  } catch (_) {}
}
$("#exec-close").addEventListener("click", () => $("#exec-dialog").close());
$("#exec-dialog").addEventListener("close", () => { clearTimeout(execTimer); execJob = null; });

function attachJob(id) {
  execJob = { id, offset: 0 };
  const out = $("#exec-output");
  out.textContent = "";
  out.classList.remove("hidden");
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
  } catch (e) {
    setExecRunning(false, "");
    toast(e.message, true);
    execJob = null;
  }
}
$("#exec-run").addEventListener("click", async () => {
  const script = $("#exec-script").value;
  if (!script.trim()) return toast("请输入要执行的命令", true);
  try {
    const job = await api(`/api/instances/${execInst.id}/exec`, { method: "POST", body: { script, timeout: +$("#exec-timeout").value } });
    attachJob(job.id);
  } catch (e) { toast(e.message, true); }
});
$("#exec-stop").addEventListener("click", async () => {
  if (!execJob) return;
  try { await api(`/api/jobs/${execJob.id}/cancel`, { method: "POST" }); $("#exec-status").textContent = "正在停止…"; }
  catch (e) { toast(e.message, true); }
});

async function loadStartup() {
  const inst = execInst;
  try {
    const r = await api(`/api/instances/${inst.id}/startup-script`);
    if (execInst !== inst) return;
    $("#startup-script").value = r.script;
    $("#startup-once").checked = r.has_script ? r.run_once : true;
  } catch (e) { toast(e.message, true); }
}
$("#startup-save").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const script = $("#startup-script").value;
  if (!script.trim()) throw new Error("请输入启动脚本");
  const reboot = $("#startup-reboot").checked;
  if (reboot && !confirm(`写入后将${execInst.status === "RUNNING" ? "强制重启" : "开机"}「${execInst.name}」，连接会中断，确定？`)) return;
  await api(`/api/instances/${execInst.id}/startup-script`, {
    method: "PUT", body: { script, run_once: $("#startup-once").checked, reboot },
  });
  toast(reboot ? "已写入并重启，稍后点「刷新」查看启动日志" : "已写入，下次开机时执行");
  $("#startup-reboot").checked = false;
  loadInstances();
}));
$("#startup-clear").addEventListener("click", (e) => {
  if (!confirm("清除该实例的启动脚本？")) return;
  busy(e.currentTarget, async () => {
    await api(`/api/instances/${execInst.id}/startup-script`, { method: "DELETE" });
    $("#startup-script").value = "";
    toast("已清除启动脚本");
  });
});
$("#serial-refresh").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const r = await api(`/api/instances/${execInst.id}/serial-log${$("#serial-all").checked ? "?all=1" : ""}`);
  const out = $("#serial-output");
  out.textContent = r.contents.trim() || "暂无启动脚本输出（实例可能还没重启，或脚本尚未开始执行）";
  out.scrollTop = out.scrollHeight;
}));

// ---------------- firewall ----------------
async function loadFirewall() {
  const sel = $("#fw-account");
  try {
    const accounts = await api("/api/accounts");
    const prev = sel.value;
    sel.innerHTML = accounts.map((a) => `<option value="${a.id}">${esc(a.name)}（${esc(a.project_id)}）</option>`).join("");
    if (!accounts.length) {
      $("#fw-table tbody").innerHTML = `<tr><td colspan="9" class="muted">还没有账号，先到「账号」页添加</td></tr>`;
      return;
    }
    if (prev && accounts.some((a) => String(a.id) === prev)) sel.value = prev;
    await loadFirewallRules();
  } catch (e) { toast(e.message, true); }
}
async function loadFirewallRules() {
  const aid = $("#fw-account").value;
  if (!aid) return;
  const tbody = $("#fw-table tbody");
  tbody.innerHTML = `<tr><td colspan="9" class="muted">加载中…</td></tr>`;
  try {
    const r = await api(`/api/accounts/${aid}/firewalls`);
    const netSel = $("#fw-form").network;
    const cur = netSel.value;
    netSel.innerHTML = r.networks.map((n) => `<option>${esc(n)}</option>`).join("") || `<option>default</option>`;
    if (r.networks.includes(cur)) netSel.value = cur; else if (r.networks.includes("default")) netSel.value = "default";
    tbody.innerHTML = r.rules.length ? r.rules.map((f) => `<tr class="${f.disabled ? "disabled" : ""}" title="${esc(f.description)}">
      <td>${esc(f.name)}${f.disabled ? ` <span class="badge idle">已禁用</span>` : ""}</td>
      <td>${esc(f.network)}</td>
      <td>${f.direction === "INGRESS" ? "入站" : "出站"}</td>
      <td><span class="badge ${f.action === "allow" ? "ok" : "bad"}">${f.action === "allow" ? "允许" : "拒绝"}</span></td>
      <td class="rules">${esc(f.rules)}</td>
      <td class="rules">${esc(f.ranges)}</td>
      <td>${esc(f.targets)}</td>
      <td>${f.priority}</td>
      <td class="ops"><button class="btn small" data-fw-toggle="${esc(f.name)}" data-disabled="${f.disabled ? 1 : 0}">${f.disabled ? "启用" : "禁用"}</button>
        <button class="btn small danger" data-fw-del="${esc(f.name)}">删除</button></td>
    </tr>`).join("") : `<tr><td colspan="9" class="muted">没有防火墙规则</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="error">${esc(e.message)}</td></tr>`;
  }
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
    if (disable && name === "default-allow-ssh" && !confirm("禁用 default-allow-ssh 后，面板「SSH 执行」将无法连接实例，确定？")) return;
    await busy(tog, async () => {
      await api(`/api/accounts/${aid}/firewalls/${encodeURIComponent(name)}`, { method: "PATCH", body: { disabled: disable } });
      toast(`已${disable ? "禁用" : "启用"} ${name}`);
      await loadFirewallRules();
    });
  } else if (del) {
    const name = del.dataset.fwDel;
    const extra = name === "default-allow-ssh" ? "\n注意：删除后面板「SSH 执行」将无法连接实例！" : "";
    if (!confirm(`删除防火墙规则「${name}」？${extra}`)) return;
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
  f.name.value = q.name; f.rules.value = q.rules; f.description.value = q.description;
  f.direction.value = "INGRESS"; f.action.value = "allow"; f.ranges.value = "0.0.0.0/0"; f.priority.value = 1000; f.target_tags.value = "";
  f.name.focus();
}));
$("#fw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const aid = $("#fw-account").value;
  if (!aid) return toast("请先添加账号", true);
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

// ---------------- accounts ----------------
async function loadAccounts() {
  try {
    const rows = await api("/api/accounts");
    $("#accounts-table tbody").innerHTML = rows.length
      ? rows.map((a) => `<tr><td>${esc(a.name)}</td><td class="mono">${esc(a.project_id)}</td><td class="mono">${esc(a.client_email)}</td>
          <td style="text-align:right"><button class="btn small danger" data-del="${a.id}" data-name="${esc(a.name)}">删除</button></td></tr>`).join("")
      : `<tr><td colspan="4" class="muted">暂无账号</td></tr>`;
  } catch (e) { toast(e.message, true); }
}
$("#accounts-table").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del]");
  if (!b || !confirm(`删除账号「${b.dataset.name}」？其下实例的管理配置也会一并删除（不影响 GCP 上的实例）。`)) return;
  await busy(b, async () => { await api(`/api/accounts/${b.dataset.del}`, { method: "DELETE" }); toast("已删除"); loadAccounts(); });
});
$("#key-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (file) $("#account-form").key.value = await file.text();
});
$("#account-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  await busy($("button[type=submit]", f), async () => {
    const acc = await api("/api/accounts", { method: "POST", body: { name: f.name.value.trim(), key: f.key.value.trim() } });
    toast(`账号 ${acc.name} 添加成功，已同步实例`);
    f.reset();
    loadAccounts();
  });
});

// ---------------- settings ----------------
function probeRow(p = {}) {
  const row = document.createElement("div");
  row.className = "probe-row";
  row.innerHTML = `<input placeholder="名称，如 上海电信" value="${esc(p.name)}" data-k="name">
    <input placeholder="http://国内机器IP:9800" value="${esc(p.url)}" data-k="url">
    <input placeholder="令牌 PROBE_TOKEN" value="${esc(p.token)}" data-k="token" type="password" autocomplete="off">
    <button type="button" class="btn small danger">移除</button>`;
  $("button", row).addEventListener("click", () => row.remove());
  return row;
}
const genToken = () => [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
let pushStatus = {};
function pushRow(p = {}) {
  const row = document.createElement("div");
  row.className = "push-row";
  const st = pushStatus[p.name];
  const badge = !p.name ? "" : st && st.online
    ? `<span class="badge ok" title="上次连接 ${fmtTime(st.last_seen)}">在线</span>`
    : `<span class="badge idle" title="${st && st.last_seen ? "上次连接 " + fmtTime(st.last_seen) : "尚未连接过"}">离线</span>`;
  row.innerHTML = `<input placeholder="名称，如 家里电信" value="${esc(p.name)}" data-k="name">
    <input value="${esc(p.token || genToken())}" data-k="token" class="mono-input" autocomplete="off" spellcheck="false">
    <span>${badge}</span>
    <button type="button" class="btn small" data-install>安装命令</button>
    <button type="button" class="btn small danger" data-remove>移除</button>`;
  $("[data-remove]", row).addEventListener("click", () => row.remove());
  $("[data-install]", row).addEventListener("click", () => showInstall(row));
  return row;
}
function showInstall(row) {
  const token = $("[data-k=token]", row).value.trim();
  const panel = location.origin;
  const raw = `${panel}/probe`;
  const out = $("#push-install");
  out.textContent = [
    "# 1) 在 iStoreOS / OpenWrt 上（SSH 登录路由器后执行）",
    "opkg update && opkg install python3",
    "mkdir -p /root/gcpvm-probe && cd /root/gcpvm-probe",
    `wget -O probe.py ${raw}/probe.py`,
    `wget -O /etc/init.d/gcpvm-probe ${raw}/gcpvm-probe.openwrt && chmod +x /etc/init.d/gcpvm-probe`,
    `sed -i 's#^PANEL=.*#PANEL="${panel}"#; s#^TOKEN=.*#TOKEN="${token}"#' /etc/init.d/gcpvm-probe`,
    "/etc/init.d/gcpvm-probe enable && /etc/init.d/gcpvm-probe start",
    "logread -e gcpvm   # 看到「已连接面板」即成功",
    "",
    "# 2) 或在任意 Linux 上前台试运行",
    `PROBE_PANEL=${panel} PROBE_TOKEN=${token} python3 probe.py`,
    "",
    "# 别忘了在本页点「保存设置」，否则令牌不生效",
  ].join("\n");
  out.classList.remove("hidden");
}
function toggleProbeBlock() {
  const mode = $("#settings-form").querySelector("[name=detect_mode]:checked")?.value;
  $("#probes-block").classList.toggle("hidden", mode === "local");
}
async function loadSettings() {
  try {
    const s = await api("/api/settings");
    const f = $("#settings-form");
    for (const [k, v] of Object.entries(s)) {
      if (k === "probes") continue;
      if (k === "detect_mode") { $$("[name=detect_mode]", f).forEach((r) => (r.checked = r.value === v)); continue; }
      const el = f.elements[k];
      if (!el) continue;
      if (el.type === "checkbox") el.checked = !!v; else el.value = v;
    }
    try { pushStatus = Object.fromEntries((await api("/api/push-probes")).map((p) => [p.name, p])); } catch (_) { pushStatus = {}; }
    const pbox = $("#push-probes");
    pbox.innerHTML = "";
    (s.push_probes || []).forEach((p) => pbox.appendChild(pushRow(p)));
    $("#push-install").classList.add("hidden");
    const box = $("#probes");
    box.innerHTML = "";
    (s.probes || []).forEach((p) => box.appendChild(probeRow(p)));
    if (!(s.probes || []).length) box.appendChild(probeRow());
    toggleProbeBlock();
  } catch (e) { toast(e.message, true); }
}
$$("[name=detect_mode]").forEach((r) => r.addEventListener("change", toggleProbeBlock));
$("#add-probe").addEventListener("click", () => $("#probes").appendChild(probeRow()));
$("#add-push-probe").addEventListener("click", () => { const r = pushRow(); $("#push-probes").appendChild(r); $("[data-k=name]", r).focus(); });
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const body = {
    detect_mode: f.querySelector("[name=detect_mode]:checked")?.value || "agent",
    control_check: f.control_check.checked,
    probes: $$(".probe-row").map((r) => Object.fromEntries($$("input", r).map((i) => [i.dataset.k, i.value.trim()]))).filter((p) => p.url),
    push_probes: $$(".push-row").map((r) => Object.fromEntries($$("input", r).map((i) => [i.dataset.k, i.value.trim()]))).filter((p) => p.name || p.token),
  };
  for (const k of ["check_interval", "fail_threshold", "tcp_timeout", "tcp_tries", "cn_success_ratio", "cooldown_minutes", "max_changes_per_day"]) body[k] = +f.elements[k].value;
  for (const k of ["telegram_bot_token", "telegram_chat_id", "cf_api_token"]) body[k] = f.elements[k].value.trim();
  await busy($("button[type=submit]", f), async () => { await api("/api/settings", { method: "PUT", body }); toast("设置已保存"); loadSettings(); });
});
$("#probe-test-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  await busy($("button", f), async () => {
    const r = await api("/api/probe-test", { method: "POST", body: { host: f.host.value.trim(), port: +f.port.value } });
    const lines = [`结论：${CN[r.state]?.[0] || r.state} — ${r.detail}`];
    (r.cn || []).forEach((p) => lines.push(`  [国内] ${p.source}: ${p.ok === null ? "探针异常 " + p.error : p.ok ? `可达 ${p.latency_ms} ms` : "不可达 " + p.error}`));
    if (r.control) lines.push(`  [海外] ${r.control.source}: ${r.control.ok ? `可达 ${r.control.latency_ms} ms` : "不可达 " + r.control.error}`);
    const out = $("#probe-test-result");
    out.textContent = lines.join("\n");
    out.classList.remove("hidden");
  });
});

// ---------------- events ----------------
const EVT = { firewall: "防火墙", exec: "执行命令", startup: "启动脚本", start: "开机", stop: "关机", reset: "重启", change_ip: "换 IP", change_ip_auto: "自动换 IP", auto_start: "自动开机", check: "检测", dns: "DNS", sync: "同步", account: "账号" };
async function loadEvents() {
  try {
    const rows = await api("/api/events?limit=300");
    $("#events-table tbody").innerHTML = rows.length
      ? rows.map((e) => `<tr class="lvl-${esc(e.level)}"><td class="mono">${fmtTime(e.ts)}</td><td>${esc(e.instance_name || "-")}</td><td>${esc(EVT[e.type] || e.type)}</td><td>${esc(e.message)}</td></tr>`).join("")
      : `<tr><td colspan="4" class="muted">暂无事件</td></tr>`;
  } catch (e) { toast(e.message, true); }
}
$("#btn-events-refresh").addEventListener("click", loadEvents);

// ---------------- boot ----------------
function start() {
  showMain(true);
  switchTab("instances");
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    if (!$("#tab-instances").classList.contains("hidden") && !$("#inst-dialog").open && !$("#exec-dialog").open) loadInstances();
  }, 30000);
}
if (token) start(); else showMain(false);
