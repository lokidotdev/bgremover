"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(`/admin/api${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== "/login") {
    showLogin();
    throw new Error("Signed out");
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || `Request failed (${res.status})`);
  }
  return res.status === 204 ? null : res.json();
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 2800);
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  for (const child of children) node.append(child);
  return node;
}

const fmt = new Intl.NumberFormat();
const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–");

/* ------------------------------------------------------------- auth */

function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#password").focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#login-error");
  err.hidden = true;
  try {
    await api("/login", { method: "POST", body: { password: $("#password").value } });
    $("#password").value = "";
    await boot();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/logout", { method: "POST" });
  showLogin();
});

/* ------------------------------------------------------------- routing */

function route() {
  const view = (location.hash || "#overview").slice(1);
  $$(".view").forEach((v) => (v.hidden = v.dataset.view !== view));
  $$(".sidebar a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  if (view === "overview") loadUsage();
}
window.addEventListener("hashchange", route);

/* ------------------------------------------------------------- overview */

function renderUsage(usage) {
  const t = usage.totals;
  $("#s-requests").textContent = fmt.format(t.requests);
  $("#s-succeeded").textContent = fmt.format(t.succeeded);
  $("#s-blocked").textContent = fmt.format(t.blocked);
  $("#s-avg").textContent = t.avg_ms ? `${fmt.format(t.avg_ms)} ms` : "–";

  // Fill in days with no traffic so the chart always shows the full window.
  const byDay = Object.fromEntries(usage.by_day.map((d) => [d.day, d]));
  const days = [];
  for (let i = usage.days - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * 864e5).toISOString().slice(0, 10);
    days.push(byDay[d] || { day: d, requests: 0, succeeded: 0 });
  }
  const max = Math.max(1, ...days.map((d) => d.requests));
  const chart = $("#chart");
  chart.replaceChildren(
    ...days.map((d) => {
      const ok = d.succeeded || 0;
      const bad = d.requests - ok;
      const bar = el("div", { className: "bar", title: `${d.day}: ${ok} succeeded, ${bad} blocked/failed` },
        el("i", { className: "ok", style: `height:${(ok / max) * 100}%` }),
        el("i", { className: "bad", style: `height:${(bad / max) * 100}%` }));
      const label = new Date(d.day + "T00:00:00").toLocaleDateString(undefined, { weekday: "short" });
      return el("div", { className: "day" }, bar, el("small", { textContent: label }));
    }),
  );

  const allowed = new Set(state.rules.filter((r) => r.enabled).map((r) => r.pattern));
  $("#hosts").replaceChildren(
    ...usage.by_host.map((h) => {
      const canAllow = h.host !== "(no origin)" && h.blocked > 0 && !allowed.has(h.host);
      const action = canAllow
        ? el("button", { className: "btn small", textContent: "Allow", onclick: () => addRule(h.host, "") })
        : "";
      return el("tr", {},
        el("td", { textContent: h.host }),
        el("td", { className: "num", textContent: fmt.format(h.requests) }),
        el("td", { className: "num", textContent: fmt.format(h.succeeded) }),
        el("td", { className: "num", textContent: fmt.format(h.blocked) }),
        el("td", { textContent: when(h.last_seen) }),
        el("td", { className: "actions" }, action));
    }),
  );
  $("#hosts-empty").hidden = usage.by_host.length > 0;
}

async function loadUsage() {
  try { renderUsage(await api("/usage?days=7")); } catch (e) { /* handled in api() */ }
}

/* ------------------------------------------------------------- domains */

const state = { rules: [] };

function renderRules() {
  $("#rules").replaceChildren(
    ...state.rules.map((r) => {
      const toggle = el("input", { type: "checkbox", checked: r.enabled, ariaLabel: `Enable ${r.pattern}` });
      toggle.addEventListener("change", () => updateRule(r.id, { enabled: toggle.checked }));
      const del = el("button", { className: "btn small ghost danger", textContent: "Remove" });
      del.addEventListener("click", () => deleteRule(r));
      return el("tr", { className: r.enabled ? "" : "disabled" },
        el("td", {}, el("code", { textContent: r.pattern })),
        el("td", { className: "muted", textContent: r.note || "–" }),
        el("td", { textContent: when(r.created_at) }),
        el("td", {}, el("label", { className: "switch" }, toggle, el("span"))),
        el("td", { className: "actions" }, del));
    }),
  );
  $("#rules-empty").hidden = state.rules.length > 0;
}

async function addRule(pattern, note) {
  const err = $("#add-error");
  err.hidden = true;
  try {
    const rule = await api("/domains", { method: "POST", body: { pattern, note } });
    state.rules = [...state.rules, rule].sort((a, b) => a.pattern.localeCompare(b.pattern));
    renderRules();
    toast(`${rule.pattern} can now use the API`);
    loadUsage();
    return true;
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
    toast(ex.message);
    return false;
  }
}

async function updateRule(id, patch) {
  try {
    const rule = await api(`/domains/${id}`, { method: "PATCH", body: patch });
    state.rules = state.rules.map((r) => (r.id === id ? rule : r));
    renderRules();
    toast(`${rule.pattern} ${rule.enabled ? "enabled" : "disabled"}`);
  } catch (ex) {
    toast(ex.message);
    renderRules();
  }
}

async function deleteRule(rule) {
  if (!confirm(`Remove ${rule.pattern}? Requests from it will be blocked.`)) return;
  try {
    await api(`/domains/${rule.id}`, { method: "DELETE" });
    state.rules = state.rules.filter((r) => r.id !== rule.id);
    renderRules();
    toast(`${rule.pattern} removed`);
  } catch (ex) {
    toast(ex.message);
  }
}

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await addRule($("#pattern").value, $("#note").value)) {
    $("#pattern").value = "";
    $("#note").value = "";
    $("#pattern").focus();
  }
});

$("#allow-no-origin").addEventListener("change", async (e) => {
  try {
    const s = await api("/settings", { method: "PUT", body: { allow_no_origin: e.target.checked } });
    e.target.checked = s.allow_no_origin;
    toast(s.allow_no_origin ? "Requests without an Origin are allowed" : "Requests without an Origin are blocked");
  } catch (ex) {
    e.target.checked = !e.target.checked;
    toast(ex.message);
  }
});

/* ------------------------------------------------------------- playground */

let lastUrls = [];

async function runPlayground(file) {
  if (!file) return;
  const drop = $("#drop");
  drop.classList.add("busy");
  drop.querySelector("span").textContent = "Removing background…";
  lastUrls.forEach(URL.revokeObjectURL);

  const form = new FormData();
  form.append("image", file);
  form.append("format", $("#opt-format").value);
  if ($("#opt-bg-on").checked) form.append("background", $("#opt-bg").value);
  form.append("crop", $("#opt-crop").checked);
  form.append("refine_edges", $("#opt-refine").checked);

  try {
    const res = await fetch("/admin/api/try", { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Failed (${res.status})`);
    const blob = await res.blob();
    const before = URL.createObjectURL(file);
    const after = URL.createObjectURL(blob);
    lastUrls = [before, after];
    $("#before").src = before;
    $("#after").src = after;
    $("#after-cap").textContent = `Result · ${res.headers.get("X-Processing-Ms")} ms · ${(blob.size / 1024).toFixed(0)} KB`;
    const ext = $("#opt-format").value === "jpg" ? "jpg" : $("#opt-format").value === "webp" ? "webp" : "png";
    const dl = $("#download");
    dl.href = after;
    dl.download = file.name.replace(/\.[^.]+$/, "") + `-nobg.${ext}`;
    dl.hidden = false;
    $("#result").hidden = false;
  } catch (ex) {
    toast(ex.message);
  } finally {
    drop.classList.remove("busy");
    drop.querySelector("span").innerHTML = "Drop another image or <u>browse</u>";
  }
}

$("#file").addEventListener("change", (e) => runPlayground(e.target.files[0]));
const drop = $("#drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => { e.preventDefault(); runPlayground(e.dataTransfer.files[0]); });

/* ------------------------------------------------------------- boot */

async function boot() {
  let data;
  try {
    data = await api("/overview");
  } catch {
    return;
  }
  $("#login").hidden = true;
  $("#app").hidden = false;
  state.rules = data.rules;
  $("#device").textContent = data.device.replace("ExecutionProvider", "").toUpperCase();
  $("#allow-no-origin").checked = data.settings.allow_no_origin;
  $$(".origin").forEach((n) => (n.textContent = location.origin));
  renderRules();
  renderUsage(data.usage);
  route();
}

boot();
