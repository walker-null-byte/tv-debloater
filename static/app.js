/* TVDebloater frontend — plain JS, no build step. */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------- plumbing -- */
let inflight = 0;
function loadbar(delta) {
  inflight = Math.max(0, inflight + delta);
  const el = $("#loadbar");
  if (el) el.classList.toggle("hidden", inflight === 0);
}

async function api(path, opts = {}) {
  loadbar(1);
  try {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  } finally { loadbar(-1); }
}

function toast(msg, kind = "", ms = 5200) {
  const wrap = $("#toasts");
  while (wrap.children.length >= 3) wrap.firstElementChild.remove();
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<div class="tmsg">${msg}</div>
    <div class="tbar"><i style="animation-duration:${ms}ms"></i></div>`;
  const t = setTimeout(() => el.remove(), ms);
  el.onclick = () => { clearTimeout(t); el.remove(); };
  wrap.appendChild(el);
}

function closeModal() { $("#modal").classList.add("hidden"); }
function openModal(html) {
  $("#modalBox").innerHTML = html;
  $("#modalBox").scrollTop = 0;
  $("#modal").classList.remove("hidden");
}

/* ------------------------------------------------------------------ state */
const S = {
  status: null, device: null, vendor: null, stats: null, counts: null,
  rows: [], selected: new Set(), shown: 150, expert: false,
  filters: { q: "", tier: "", risk: "", state: "enabled" },
  sort: { key: "risk_rank", dir: 1 },
};

const PRESET_ICONS = {
  "ads-telemetry": "🎯", "declutter": "🧹", "google-galore": "✨",
  "vendor-diet": "🏷️", "expert": "🧪",
};

/* ------------------------------------------------------------------- init */
(async function init() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
  $("#btnConnect").addEventListener("click", openConnect);
  $("#deviceBar").addEventListener("click", onDeviceBarClick);
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeModal(); $("#drawer").classList.add("hidden"); }
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
    if (e.key === "/" && !typing) { e.preventDefault(); showTab("packages"); $("#search").focus(); }
  });
  wirePackageToolbar();
  wireOtherTabs();
  await refreshStatus();
  if (S.status?.selected) await afterConnect();
})();

function showTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "presets") loadPresets();
  if (name === "tune") loadTuning();
  if (name === "journal") loadJournal();
}

async function refreshStatus() {
  try {
    S.status = await api("/api/status");
    const pill = $("#connPill");
    if (S.status.mock) $("#mockBadge").classList.remove("hidden");
    $("#ver").textContent = "v" + S.status.version;
    if (S.status.selected) {
      const st = S.status.devices.find((d) => d.serial === S.status.selected);
      pill.innerHTML = `<i class="dot"></i>${esc(st ? st.model || st.serial : "connected")}`;
      pill.className = "pill on";
      S.device = S.status.device;
      renderDeviceBar();
    } else {
      const unauthorized = S.status.devices.some?.((d) => d.state === "unauthorized");
      pill.innerHTML = `<i class="dot"></i>${unauthorized ? "unauthorized — check the TV dialog" : "not connected"}`;
      pill.className = `pill ${unauthorized ? "warn" : "off"}`;
    }
  } catch (err) {
    toast(`Backend unreachable: ${esc(err.message)}`, "bad");
  }
}

async function afterConnect() {
  await refreshDevice();
  await loadPackages(false);
  await loadPresets();
}

/* -------------------------------------------------------------- device bar */
function renderDeviceBar() {
  const d = S.device;
  const bar = $("#deviceBar");
  if (!d) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  const chip = (icon, html, cls = "") =>
    `<span class="dchip ${cls}">${icon ? icon + " " : ""}${html}</span>`;
  const chips = [
    chip("📺", `<b>${esc(d.model || "?")}</b> ${esc(d.brand || "")}`, "accent"),
    chip("", `Android ${esc(d.android_version || "?")} · SDK ${esc(d.sdk || "?")}`),
    d.ram_mb ? chip("🧠", `${d.ram_mb} MB RAM`) : "",
    d.data_free_mb ? chip("💾", `${(d.data_total_mb / 1024).toFixed(1)} GB · ${d.data_free_mb} MB free`) : "",
    d.resolution ? chip("🖥", esc(d.resolution)) : "",
    d.drm_running ? chip("🔐", `DRM ${esc(d.drm_running)}`) : "",
    S.vendor ? chip("🏷", `rules: <b>${esc(S.vendor.label)}</b>`) : "",
    chip("", esc(d.serial), "mono"),
  ].filter(Boolean).join("");
  bar.innerHTML = `<div class="dchips">${chips}</div>
    <div class="dactions">
      <button class="btn tiny ghost" data-action="refresh" title="Re-read the package list">↻ Refresh</button>
      <button class="btn tiny ghost" data-action="disconnect">Disconnect</button>
    </div>`;
}

async function onDeviceBarClick(e) {
  const b = e.target.closest("[data-action]");
  if (!b) return;
  if (b.dataset.action === "refresh") {
    toast("Re-reading packages from the TV…", "", 2500);
    await loadPackages(true);
    await loadPresets();
    toast("Package list refreshed.", "ok", 2600);
  }
  if (b.dataset.action === "disconnect") {
    try { await api("/api/disconnect", { method: "POST", body: {} }); }
    catch { /* ignore */ }
    S.selected.clear();
    S.device = null;
    if (S.status) S.status.selected = null;
    $("#deviceBar").classList.add("hidden");
    await refreshStatus();
    toast("Disconnected.", "ok", 3000);
  }
}

async function refreshDevice() {
  try { S.device = await api("/api/device?refresh=1"); renderDeviceBar(); }
  catch (e) { /* stay quiet */ }
}

/* -------------------------------------------------------------- connection */
function openConnect() {
  openModal(`
    <div class="mhead"><h3>Connect to your TV</h3>
      <button class="rowbtn" id="btnCloseModal">✕</button></div>
    <p class="statusmsg">On the TV: <b>Settings → System → About</b> → press OK on <b>Build</b> 7×, then
      <b>System → Developer options</b> → enable <b>USB debugging / Network debugging</b>.
      Laptop and TV must be on the same network.</p>
    <div class="grid2">
      <div class="panel">
        <h4>🔎 Automatic scan</h4>
        <p class="statusmsg">Looks for TVs with ADB debugging enabled on your network.</p>
        <button id="btnScan" class="btn primary">Scan network</button>
        <div id="scanOut" class="statusmsg"></div>
      </div>
      <div>
        <div class="panel">
          <h4>🖥 Connect by IP</h4>
          <div class="fieldrow">
            <input id="ip" type="text" placeholder="TV IP (e.g. 192.168.1.50)" style="flex:1;min-width:140px">
            <input id="port" type="text" value="5555" style="width:76px">
            <button id="btnConn" class="btn">Connect</button>
          </div>
        </div>
        <div class="panel">
          <h4>🔗 Wireless debugging (Android 11+)</h4>
          <p class="statusmsg">On the TV: <b>Wireless debugging → Pair device with pairing code</b>.
            Enter the IP, the <i>pairing</i> port and the 6-digit code shown there.</p>
          <div class="fieldrow">
            <input id="pairIp" type="text" placeholder="IP" style="flex:1;min-width:110px">
            <input id="pairPort" type="text" placeholder="pair port" style="width:100px">
            <input id="pairCode" type="text" placeholder="6-digit code" style="width:116px">
            <button id="btnPair" class="btn">Pair</button>
          </div>
        </div>
      </div>
    </div>`);
  $("#btnCloseModal").onclick = closeModal;
  $("#btnScan").onclick = runScan;
  $("#btnConn").onclick = () => doConnect($("#ip").value.trim(), $("#port").value.trim());
  $("#btnPair").onclick = () => doPair($("#pairIp").value.trim(), $("#pairPort").value.trim(), $("#pairCode").value.trim());
}

async function runScan() {
  const out = $("#scanOut");
  out.innerHTML = `<span class="spin"></span> Scanning your network for ADB devices (port 5555)…`;
  try {
    const res = await api("/api/discover");
    let html = "";
    if (res.mdns?.length) {
      html += `<div class="statusmsg">mDNS: ${res.mdns.map((m) =>
        `<code>${esc(m.name || m.address)} (${esc(m.type)}) ${esc(m.address)}:${esc(m.port)}</code>`).join(" ")}</div>`;
    }
    if (!res.found.length) {
      html += `<div class="statusmsg">Nothing found. ${esc(res.notes.join(" "))}</div>`;
    } else {
      html += res.found.map((f) => `
        <div class="scanrow">
          <span class="ip">${esc(f.ip)}:${esc(f.port)}</span>
          <span class="badge-s">${esc(f.hostname || "")} ${esc(f.mac || "")} · ${esc(f.source)}</span>
          <span class="spacer"></span>
          <button class="btn tiny primary" data-conn="${esc(f.ip)}" data-port="${esc(f.port)}">Connect</button>
        </div>`).join("");
    }
    out.innerHTML = html;
    out.querySelectorAll("[data-conn]").forEach((b) => {
      b.onclick = () => doConnect(b.dataset.conn, b.dataset.port);
    });
  } catch (err) {
    out.innerHTML = `<span class="bad">Scan failed: ${esc(err.message)}</span>`;
  }
}

async function doConnect(host, port) {
  if (!host) return toast("Enter the TV IP first", "bad");
  const out = $("#scanOut");
  if (out) out.innerHTML = `<span class="spin"></span> Connecting to ${esc(host)}…`;
  try {
    const res = await api("/api/connect", { method: "POST", body: { host, port: Number(port) || 5555 } });
    if (res.unauthorized) {
      toast("Connected, but <b>the TV must approve this computer</b> — look at the TV screen and allow USB debugging.", "bad", 12000);
    } else {
      toast(`Connected to ${esc(res.device?.model || res.serial)}`, "ok");
      closeModal();
    }
    await refreshStatus();
    await afterConnect();
  } catch (err) {
    if (out) out.innerHTML = `<span class="bad">${esc(err.message)}</span>`;
    toast(`Connect failed: ${esc(err.message)}`, "bad", 9000);
  }
}

async function doPair(host, port, code) {
  if (!host || !port || !code) return toast("IP, pairing port and code are all required", "bad");
  try {
    const res = await api("/api/pair", { method: "POST", body: { host, port: Number(port), code } });
    toast(`Paired: ${esc(res.message || "ok")}. Now connect on port 5555.`, "ok", 9000);
  } catch (err) {
    toast(`Pairing failed: ${esc(err.message)}`, "bad", 9000);
  }
}

/* --------------------------------------------------------------- packages */
function wirePackageToolbar() {
  $("#search").addEventListener("input", (e) => { S.filters.q = e.target.value.toLowerCase(); S.shown = 150; renderTable(); });
  $("#fTier").addEventListener("change", (e) => { S.filters.tier = e.target.value; S.shown = 150; renderChips(); renderTable(); });
  $("#fRisk").addEventListener("change", (e) => { S.filters.risk = e.target.value; S.shown = 150; renderTable(); });
  $("#fState").addEventListener("change", (e) => { S.filters.state = e.target.value; S.shown = 150; renderChips(); renderTable(); });
  $("#expertMode").addEventListener("change", (e) => { S.expert = e.target.checked; renderTable(); });
  $("#btnRecommended").addEventListener("click", selectRecommended);
  $("#btnClearSel").addEventListener("click", () => { S.selected.clear(); renderTable(); });
  $("#ckAll").addEventListener("change", (e) => {
    filtered().slice(0, S.shown).forEach((r) => e.target.checked ? S.selected.add(r.package) : S.selected.delete(r.package));
    renderTable();
  });
  $("#btnApply").addEventListener("click", () => applyFlow([...S.selected], $("#applyAction").value));
  $$("#pkgTable th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    S.sort = { key, dir: S.sort.key === key ? -S.sort.dir : 1 };
    renderTable();
  }));
  $("#loadMore").firstElementChild.addEventListener("click", () => { S.shown += 250; renderTable(); });
}

async function loadPackages(refresh) {
  try {
    const res = await api(`/api/packages${refresh ? "?refresh=1" : ""}`);
    S.rows = res.packages;
    S.stats = res.stats;
    S.counts = res.counts;
    S.vendor = res.vendor;
    renderChips();
    renderTierOptions();
    renderTable();
    renderDeviceBar();
  } catch (err) {
    toast(`Could not list packages: ${esc(err.message)}`, "bad", 8000);
  }
}

function renderTierOptions() {
  const tiers = S.stats?.tier_meta || {};
  $("#fTier").innerHTML = `<option value="">All tiers</option>` + Object.entries(tiers)
    .map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
}

function renderChips() {
  const counts = S.counts || {};
  const tiers = S.stats?.tiers || {};
  const meta = S.stats?.tier_meta || {};
  const t = (k) => tiers[k] || 0;
  const candidates = t("ads") + t("promo") + t("cruft");
  const protectedN = S.rows.filter((r) => r.keep).length;

  const stats = `
    <div class="stat"><b>${counts.total ?? 0}</b><span>apps on TV</span></div>
    <div class="stat warn"><b>${candidates}</b><span>cleanup candidates</span></div>
    <div class="stat"><b>${counts.disabled ?? 0}</b><span>disabled now</span></div>
    <div class="stat good"><b>${counts.user ?? 0}</b><span>user-installed</span></div>
    <div class="stat keep"><b>${protectedN}</b><span>protected</span></div>`;
  const pills = [
    `<span class="chip" data-clear="1" title="Reset all filters">✕ reset</span>`,
    ...Object.entries(tiers).filter(([k]) => k !== "unclassified").map(([k, v]) =>
      `<span class="chip ${S.filters.tier === k ? "active" : ""}" data-tier="${k}">
        ${esc(meta[k]?.label || k)} <b>${v}</b></span>`),
    `<span class="chip ${S.filters.state === "disabled" ? "active" : ""}" data-state="disabled">Disabled <b>${counts.disabled ?? 0}</b></span>`,
    `<span class="chip ${S.filters.state === "user" ? "active" : ""}" data-state="user">User apps <b>${counts.user ?? 0}</b></span>`,
  ];
  $("#statChips").innerHTML =
    `<div class="statrow">${stats}</div><div class="pillrow">${pills.join("")}</div>`;

  const box = $("#statChips");
  box.querySelectorAll("[data-tier]").forEach((c) => c.onclick = () => {
    S.filters.tier = S.filters.tier === c.dataset.tier ? "" : c.dataset.tier;
    $("#fTier").value = S.filters.tier;
    renderChips(); renderTable();
  });
  box.querySelectorAll("[data-state]").forEach((c) => c.onclick = () => {
    S.filters.state = S.filters.state === c.dataset.state ? "enabled" : c.dataset.state;
    $("#fState").value = S.filters.state;
    renderChips(); renderTable();
  });
  box.querySelectorAll("[data-clear]").forEach((c) => c.onclick = () => {
    S.filters = { q: "", tier: "", risk: "", state: "enabled" };
    $("#search").value = ""; $("#fTier").value = ""; $("#fRisk").value = ""; $("#fState").value = "enabled";
    renderChips(); renderTable();
  });
}

function rowState(r) {
  if (r.keep) return `<span class="state prot">protected</span>`;
  if (r.uninstalled) return `<span class="state off">uninstalled</span>`;
  if (r.disabled) return `<span class="state off">disabled</span>`;
  return `<span class="state on">${r.system ? "system" : "user"}</span>`;
}

function filtered() {
  const { q, tier, risk, state } = S.filters;
  return S.rows.filter((r) => {
    if (tier && r.tier !== tier) return false;
    if (risk === "keep" && !r.keep) return false;
    if (risk && risk !== "keep" && r.risk !== risk) return false;
    if (state === "enabled" && (r.disabled || r.uninstalled)) return false;
    if (state === "disabled" && !(r.disabled || r.uninstalled)) return false;
    if (state === "system" && !r.system) return false;
    if (state === "user" && r.system) return false;
    if (q) {
      const hay = `${r.label} ${r.package} ${r.desc} ${r.tier_label}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }).sort((a, b) => {
    const { key, dir } = S.sort;
    const va = a[key], vb = b[key];
    if (typeof va === "number" || typeof vb === "number") return (va - vb) * dir;
    return String(va ?? "").localeCompare(String(vb ?? "")) * dir;
  });
}

function renderTable() {
  const rows = filtered();
  const visible = rows.slice(0, S.shown);
  const tbody = $("#pkgBody");

  $$("#pkgTable th[data-sort]").forEach((th) => {
    th.classList.remove("asc", "desc");
    if (th.dataset.sort === S.sort.key) th.classList.add(S.sort.dir === 1 ? "asc" : "desc");
  });

  tbody.innerHTML = visible.map((r) => {
    const checked = S.selected.has(r.package) ? "checked" : "";
    const canToggle = !r.keep && !r.uninstalled && (S.expert || r.risk !== "expert");
    return `
    <tr class="${r.disabled ? "gone" : ""}" data-pkg="${esc(r.package)}" data-tier="${esc(r.tier)}">
      <td class="ck"><input type="checkbox" data-ck="${esc(r.package)}" ${checked}
          ${canToggle ? "" : "disabled"} title="${canToggle ? "Select" : "Protected — cannot be disabled"}"></td>
      <td class="app" style="cursor:pointer" data-open="${esc(r.package)}">
        <div class="lbl">${esc(r.label)} ${r.soft_keep ? '<span class="badge-s">⚠ soft-protected</span>' : ""}</div>
        <div class="pkg">${esc(r.package)}</div>
        ${r.desc ? `<div class="desc">${esc(r.desc)}</div>` : ""}
      </td>
      <td><span class="badge-t ${esc(r.tier)}">${esc(r.tier_label)}</span></td>
      <td><span class="badge-r ${r.keep ? "keep" : esc(r.risk)}">${r.keep ? "protected" : esc(r.risk)}</span></td>
      <td><span class="badge-s">${esc(r.source)}</span></td>
      <td>${rowState(r)}</td>
      <td><button class="rowbtn" data-info="${esc(r.package)}" title="Details">ⓘ</button></td>
    </tr>`;
  }).join("");
  if (!visible.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">📭 No packages match the filters.</td></tr>`;
  }
  $("#loadMore").classList.toggle("hidden", rows.length <= S.shown);

  tbody.querySelectorAll("[data-ck]").forEach((ck) => ck.onchange = () => {
    ck.checked ? S.selected.add(ck.dataset.ck) : S.selected.delete(ck.dataset.ck);
    updateSelInfo();
  });
  tbody.querySelectorAll("[data-open],[data-info]").forEach((el) => el.onclick = (ev) => {
    ev.stopPropagation();
    openDrawer(el.dataset.open || el.dataset.info);
  });
  updateSelInfo();
}

function updateSelInfo() {
  const n = S.selected.size;
  $("#btnApply").disabled = n === 0;
  const info = $("#selInfo");
  if (!n) {
    info.innerHTML = `<span class="dim">Nothing selected — pick apps, or hit ✨ Select recommended.</span>`;
    return;
  }
  const rows = [...S.selected].map((p) => S.rows.find((r) => r.package === p)).filter(Boolean);
  const risky = rows.filter((r) => r.risk !== "safe").length;
  info.innerHTML = `<span class="selcount">${n}</span> selected` +
    (risky ? ` · <span class="down">${risky} not risk-free</span>` : "") +
    ` <span class="dim">· applied in batches of 10</span>`;
}

function selectRecommended() {
  const rec = (S.stats?.recommended || []).filter((p) => {
    const r = S.rows.find((x) => x.package === p);
    return r && r.enabled && !r.keep;
  });
  rec.forEach((p) => S.selected.add(p));
  renderTable();
  toast(`Selected <b>${rec.length}</b> recommended packages (ads, telemetry, promo, cruft…). Review, then “Preview &amp; apply”.`, "ok");
}

/* ---------------------------------------------------------------- apply -- */
async function applyFlow(packages, action, note = "") {
  if (!packages.length) return toast("Nothing selected", "bad");
  let plan;
  try {
    plan = await api("/api/apply", { method: "POST", body: { action, packages, dry_run: true, expert: S.expert, note } });
  } catch (err) { return toast(`Cannot plan: ${esc(err.message)}`, "bad", 9000); }

  const expertNeeded = plan.denied.some((d) => /expert/i.test(d.blocked_reason || ""));
  const batches = Math.ceil(plan.allowed.length / 10);
  openModal(`
    <div class="mhead">
      <h3>${action === "disable" ? "Disable" : esc(action)} ${plan.allowed.length} package(s)</h3>
      <button class="rowbtn" id="btnXApply">✕</button>
    </div>
    ${plan.denied.length ? `<div class="notice"><b>${plan.denied.length} blocked by safety rules:</b>
      ${plan.denied.map((d) => `<div>⛔ <code>${esc(d.package)}</code> — ${esc(d.blocked_reason)}</div>`).join("")}</div>` : ""}
    ${plan.allowed.length === 0 ? "<p>Nothing left to apply.</p>" : `
      <p class="statusmsg">This runs on the TV now.${batches > 1 ? ` <b>${batches} batches</b> of 10 will be applied.` : ""}
        Everything is journaled and reversible.</p>
      <div class="cmdlist">${plan.allowed.map((a) => `<div>adb shell ${esc(a.command)}
        ${a.risk !== "safe" ? ` <span class="badge-r ${esc(a.risk)}">${esc(a.risk)}</span>` : ""}</div>`).join("")}</div>`}
    <div class="row" style="margin-top:14px">
      ${expertNeeded ? `<label class="inline"><input type="checkbox" id="ackExpert"> I understand — this needs expert mode</label>` : ""}
      <button id="btnConfirmApply" class="btn primary" ${plan.allowed.length ? "" : "disabled"}>Apply ${plan.allowed.length} change(s)</button>
      <button id="btnCancelApply" class="btn ghost">Cancel</button>
    </div>
    <div id="applyProgress" class="statusmsg"></div>`);
  $("#btnXApply").onclick = closeModal;
  $("#btnCancelApply").onclick = closeModal;
  $("#btnConfirmApply").onclick = async () => {
    if (expertNeeded && !$("#ackExpert")?.checked) return toast("Tick the acknowledgement first", "bad");
    if (expertNeeded) { S.expert = true; $("#expertMode").checked = true; }
    const chunks = [];
    for (let i = 0; i < plan.allowed.length; i += 10)
      chunks.push(plan.allowed.slice(i, i + 10).map((a) => a.package));
    const all = { results: [], denied: plan.denied, batch_ids: [] };
    const prog = $("#applyProgress");
    for (let i = 0; i < chunks.length; i++) {
      const pct = Math.round((i / chunks.length) * 100);
      prog.innerHTML = `<div class="prog"><i style="width:${pct}%"></i></div>
        <div class="progtext"><span class="spin"></span> Batch ${i + 1}/${chunks.length} — ${chunks[i].length} package(s)…</div>`;
      try {
        const res = await api("/api/apply", { method: "POST", body: { action, packages: chunks[i], expert: S.expert, note } });
        all.results.push(...res.results);
        all.denied.push(...(res.denied || []));
        if (res.batch_id) all.batch_ids.push(res.batch_id);
      } catch (err) {
        all.results.push({ package: chunks[i].join(", "), ok: false, output: err.message });
      }
    }
    prog.innerHTML = `<div class="prog"><i style="width:100%"></i></div>`;
    await loadPackages(true);
    await loadPresets();   // preset counts depend on device state - keep them fresh too
    renderApplyResults(all);
  };
}

function renderApplyResults(all) {
  const okn = all.results.filter((r) => r.ok).length;
  const badn = all.results.length - okn;
  openModal(`
    <div class="mhead"><h3>${okn ? "✅" : "⚠"} ${okn} change(s) applied${badn ? ` · ${badn} failed` : ""}</h3>
      <button class="rowbtn" id="btnXDone">✕</button></div>
    <div class="results">${all.results.map((r) => `
      <div>${r.ok ? "✅" : "❌"} <code>${esc(r.package)}</code>
      ${r.output ? `<span class="badge-s">${esc((r.output || "").slice(0, 120))}</span>` : ""}</div>`).join("")}</div>
    <div class="notice" style="margin-top:15px">
      <b>Now test the TV</b> (the 30-second checklist):
      <ul class="checklist">
        <li>▢ Inputs / Source button still opens and can switch to HDMI</li>
        <li>▢ Netflix / YouTube still open (DRM still L1)</li>
        <li>▢ Sound works, remote works</li>
        <li>▢ On-screen keyboard still appears</li>
      </ul>
      If something broke: hit <b>Undo this batch</b> — everything comes back.
    </div>
    <div class="row">
      <button id="btnUndoBatch" class="btn danger" ${all.batch_ids.length ? "" : "disabled"}>↩ Undo what I just did</button>
      <button id="btnDone" class="btn primary">Looks fine ✓</button>
    </div>`);
  $("#btnXDone").onclick = closeModal;
  $("#btnDone").onclick = () => { closeModal(); toast("Nice. Next batch — test again after each one.", "ok"); };
  $("#btnUndoBatch").onclick = async () => {
    try {
      let total = 0;
      for (const id of all.batch_ids) {
        const res = await api("/api/undo", { method: "POST", body: { batch_id: id } });
        total += res.applied;
      }
      toast(`Restored ${total} package(s).`, "ok");
      await loadPackages(true);
      await loadPresets();
      closeModal();
    } catch (err) { toast(`Undo failed: ${esc(err.message)}`, "bad"); }
  };
}

/* ---------------------------------------------------------------- drawer -- */
async function openDrawer(pkg) {
  const d = $("#drawer");
  d.classList.remove("hidden");
  d.innerHTML = `
    <div class="dhead">
      <div><h3>${esc(pkg)}</h3><div class="sub">loading…</div></div>
      <button class="rowbtn dxclose" id="dxClose">✕</button>
    </div>
    <div class="statusmsg"><span class="spin"></span> loading details…</div>`;
  $("#dxClose").onclick = () => d.classList.add("hidden");
  let data;
  try { data = await api(`/api/package/${encodeURIComponent(pkg)}?expert=${S.expert ? 1 : 0}`); }
  catch (err) { d.innerHTML = `<p>Failed: ${esc(err.message)}</p>`; return; }
  const c = data.classification, det = data.detail || {};
  const row = S.rows.find((r) => r.package === pkg) || {};
  const kv = (k, v) => v ? `<div class="k">${k}</div><div>${v}</div>` : "";
  const reason = data.disable_check?.reason || "";
  d.innerHTML = `
    <div class="dhead">
      <div>
        <h3>${esc(c.label)}</h3>
        <div class="sub">${esc(pkg)}</div>
        <div class="drow">
          <span class="badge-t ${esc(c.tier)}">${esc(S.stats?.tier_meta?.[c.tier]?.label || c.tier)}</span>
          <span class="badge-r ${c.keep ? "keep" : esc(c.risk)}">${c.keep ? "protected" : esc(c.risk)}</span>
          <span class="badge-s">source: ${esc(c.source)}</span>
          ${row.disabled ? '<span class="state off">disabled</span>' : ""}
        </div>
      </div>
      <button class="rowbtn dxclose" id="dxClose">✕</button>
    </div>
    <p>${esc(c.desc || "No description in the databases.")}</p>
    ${c.keep ? `<div class="notice">🛡 <b>Protected:</b> ${esc(c.keep_reason)}</div>` : ""}
    ${!c.keep && data.disable_check && !data.disable_check.allowed && reason
      ? `<div class="notice">⛔ ${esc(reason)}</div>` : ""}
    <h4>Dependencies &amp; relationships</h4>
    <div class="kv">
      ${kv("depends on", (c.dependencies || []).length ? c.dependencies.map((x) => `<code>${esc(x)}</code>`).join(" ") : "<i>none documented</i>")}
      ${kv("needed by", (c.needed_by || []).length ? c.needed_by.map((x) => `<code>${esc(x)}</code>`).join(" ") : "<i>none documented</i>")}
      ${kv("shared UID", det.shared_user ? esc(det.shared_user) : "")}
      ${kv("code path", esc(det.code_path || row.path || ""))}
      ${kv("version", det.version_name ? `${esc(det.version_name)} (${esc(det.version_code || "")})` : "")}
      ${kv("installer", det.installer && det.installer !== "null" ? esc(det.installer) : "")}
      ${kv("installed", esc(det.first_install || "") + (det.last_update ? ` · updated ${esc(det.last_update)}` : ""))}
      ${kv("flags", (det.flags || []).map((f) => `<code>${esc(f)}</code>`).join(" "))}
      ${kv("sensitive perms", (det.dangerous_perms || []).map((p) => `<code>${esc(p)}</code>`).join(" ") || "<i>none</i>")}
    </div>
    <div class="actions">
      ${row.disabled
        ? `<button class="btn primary" data-act="enable">Enable</button>
           ${row.uninstalled ? `<button class="btn" data-act="install-existing">Restore (install-existing)</button>` : ""}`
        : `<button class="btn primary" data-act="disable" ${data.disable_check?.allowed ? "" : "disabled"}>Disable</button>`}
      <button class="btn" data-act="force-stop">Force stop</button>
      <button class="btn" data-act="clear-data">Clear data</button>
      <button class="btn danger" data-act="uninstall">Uninstall for user 0</button>
    </div>
    <h4>Command for your notes</h4>
    <pre>adb -s ${esc(S.device?.serial || "<tv-ip>:5555")} shell pm disable-user --user 0 ${esc(pkg)}
# undo:
adb -s ${esc(S.device?.serial || "<tv-ip>:5555")} shell pm enable ${esc(pkg)}</pre>
    <h4>Raw dumpsys (head)</h4>
    <pre>${esc(det.raw_head || det.error || "no data")}</pre>`;
  $("#dxClose").onclick = () => d.classList.add("hidden");
  d.querySelectorAll("[data-act]").forEach((b) => b.onclick = () => {
    d.classList.add("hidden");
    applyFlow([pkg], b.dataset.act, `from details: ${pkg}`);
  });
}

/* --------------------------------------------------------------- presets -- */
async function loadPresets() {
  if (!S.status?.selected) return;
  try {
    const res = await api("/api/presets");
    $("#presetCards").innerHTML = res.presets.map((p) => `
      <div class="card">
        <div class="phead"><span class="picon">${PRESET_ICONS[p.id] || "📦"}</span><h3>${esc(p.name)}</h3></div>
        <p>${esc(p.desc)}</p>
        <div class="count">${p._count} package(s) on this device · risk up to ${esc(p.max_risk)}</div>
        <div class="rowbtns" style="margin-top:12px">
          <button class="btn primary" data-preview="${esc(p.id)}">Preview…</button>
          <button class="btn ghost" data-apply="${esc(p.id)}">Apply now</button>
        </div>
      </div>`).join("") +
      `<div class="card">
        <div class="phead"><span class="picon">🏷️</span><h3>Vendor detection</h3></div>
        <p>Detected rules: <b>${esc(res.vendor.label)}</b> (${esc(res.vendor.source)}).<br>
          Recommended auto-selection: <b>${res.recommended.length}</b> packages.</p>
        <div class="rowbtns"><button class="btn" id="btnRecPreset">✨ Select recommended</button></div>
      </div>`;
    $("#presetCards").querySelectorAll("[data-preview]").forEach((b) => b.onclick = () => previewPreset(b.dataset.preview));
    $("#presetCards").querySelectorAll("[data-apply]").forEach((b) => b.onclick = async () => {
      const res2 = await api(`/api/preset/${b.dataset.apply}`);
      applyFlow(res2.packages.map((p) => p.package), "disable", `preset: ${res2.preset.name}`);
    });
    const rec = $("#btnRecPreset");
    if (rec) rec.onclick = () => { showTab("packages"); selectRecommended(); };
  } catch (err) { toast(`Presets failed: ${esc(err.message)}`, "bad"); }
}

async function previewPreset(id) {
  const res = await api(`/api/preset/${id}`);
  openModal(`
    <div class="mhead">
      <h3>${PRESET_ICONS[res.preset.id] || "📦"} ${esc(res.preset.name)} — ${res.packages.length} package(s)</h3>
      <button class="rowbtn" id="pvX">✕</button>
    </div>
    <p class="statusmsg">${esc(res.preset.desc)}</p>
    <div class="cmdlist">${res.packages.map((p) => `
      <div><span class="badge-t ${esc(p.tier)}">${esc(p.tier_label)}</span>
      <span class="badge-r ${esc(p.risk)}">${esc(p.risk)}</span>
      <code>${esc(p.package)}</code> <span class="badge-s">${esc(p.label)}</span></div>`).join("")}</div>
    <div class="row">
      <button id="pvSelect" class="btn">Add to Packages selection</button>
      <button id="pvApply" class="btn primary">Preview &amp; apply now</button>
      <button id="pvClose" class="btn ghost">Close</button>
    </div>`);
  $("#pvX").onclick = closeModal;
  $("#pvClose").onclick = closeModal;
  $("#pvSelect").onclick = () => {
    res.packages.forEach((p) => S.selected.add(p.package));
    closeModal();
    showTab("packages"); renderTable();
    toast(`${res.packages.length} packages added to the selection.`, "ok");
  };
  $("#pvApply").onclick = () => {
    closeModal();
    applyFlow(res.packages.map((p) => p.package), "disable", `preset: ${res.preset.name}`);
  };
}

/* -------------------------------------------------------------- tune tab -- */
function wireOtherTabs() {
  $("#btnExportUndo").onclick = () => { window.location = "/api/export/undo-script"; };
  $("#btnRefreshJournal").onclick = loadJournal;
  $("#btnReport").onclick = generateReport;
  $("#btnCopyReport").onclick = () => {
    navigator.clipboard.writeText($("#reportOut").textContent);
    toast("Report copied", "ok");
  };
  $("#btnSnapBefore").onclick = () => takeSnapshot("before");
  $("#btnSnapAfter").onclick = () => takeSnapshot("after");
  $("#btnCompare").onclick = compareSnapshots;
}

async function loadTuning() {
  if (!S.status?.selected) return;
  try {
    const res = await api("/api/tuning");
    $("#tuningCards").innerHTML = Object.entries(res.tuning).map(([id, t]) => `
      <div class="card">
        <div class="phead"><span class="picon">⚙️</span><h3>${esc(t.name)}</h3></div>
        <p>${esc(t.desc)}</p>
        <div class="cmdlist">${t.commands.map((c) => `adb shell ${esc(c.cmd)}`).join("<br>")}</div>
        <div class="rowbtns" style="margin-top:12px"><button class="btn" data-tune="${esc(id)}">Apply</button></div>
      </div>`).join("");
    $("#tuningCards").querySelectorAll("[data-tune]").forEach((b) => b.onclick = () => applyTuning(b.dataset.tune));
    $("#guidanceList").innerHTML = res.guidance.map((g) => `
      <div class="card"><div class="phead"><span class="picon">💡</span><h3>${esc(g.title)}</h3></div>
      <p>${esc(g.body)}</p></div>`).join("");
  } catch (err) { toast(`Tuning load failed: ${esc(err.message)}`, "bad"); }
}

async function applyTuning(id) {
  try {
    const plan = await api("/api/tuning", { method: "POST", body: { id, dry_run: true } });
    openModal(`
      <div class="mhead"><h3>⚙️ ${esc(plan.tuning.name)}</h3>
        <button class="rowbtn" id="tuneX">✕</button></div>
      <div class="cmdlist">${plan.commands.map((c) => `adb shell ${esc(c.cmd)}
        ${c.old_value ? `<span class="badge-s">(was: ${esc(c.old_value)})</span>` : ""}`).join("<br>")}</div>
      <div class="row"><button id="tuneGo" class="btn primary">Apply</button>
        <button id="tuneNo" class="btn ghost">Cancel</button></div>`);
    $("#tuneX").onclick = closeModal;
    $("#tuneNo").onclick = closeModal;
    $("#tuneGo").onclick = async () => {
      const res = await api("/api/tuning", { method: "POST", body: { id } });
      toast(`${esc(res.tuning.name)}: ${res.applied}/${res.results.length} OK`, "ok");
      closeModal();
    };
  } catch (err) { toast(esc(err.message), "bad"); }
}

async function takeSnapshot(label) {
  try {
    toast(`Taking “${label}” snapshot… (a few seconds)`);
    await api("/api/metrics/snapshot", { method: "POST", body: { label } });
    toast(`Snapshot “${label}” saved.`, "ok");
  } catch (err) { toast(esc(err.message), "bad"); }
}

async function compareSnapshots() {
  const el = $("#metricsBox");
  try {
    const res = await api("/api/metrics/compare?before=before&after=after");
    const d = res.diff;
    const rowsHtml = [
      ...Object.entries(d.ram).map(([k, v]) =>
        `<tr><td>${esc(k)} (MB)</td><td>${v.before}</td><td>${v.after}</td>
         <td class="${v.delta <= 0 ? "up" : "down"}">${v.delta > 0 ? "+" : ""}${v.delta}</td></tr>`),
      ...Object.entries(d.counts).map(([k, v]) =>
        `<tr><td>packages ${esc(k)}</td><td>${v.before}</td><td>${v.after}</td>
         <td>${v.delta > 0 ? "+" : ""}${v.delta}</td></tr>`),
    ].join("");
    el.innerHTML = `<table class="metrics-table">
      <tr><th>metric</th><th>before</th><th>after</th><th>Δ</th></tr>${rowsHtml}</table>`;
  } catch (err) { el.innerHTML = `<div class="notice">${esc(err.message)}</div>`; }
}

/* ---------------------------------------------------------------- journal -- */
async function loadJournal() {
  if (!S.status?.selected) return;
  const box = $("#journalList");
  box.innerHTML = `<p class="statusmsg"><span class="spin"></span> loading journal…</p>`;
  try {
    const res = await api("/api/journal");
    if (!res.batches.length) {
      box.innerHTML = `<div class="card" style="grid-column:1/-1"><div class="phead">
        <span class="picon">🕘</span><h3>No changes yet</h3></div>
        <p>Every action you take on the TV appears here with its undo command.</p></div>`;
      return;
    }
    box.innerHTML = res.batches.map((b) => `
      <div class="journal-batch">
        <h4>${b.action === "undo" ? "↩ undo" : esc(b.action)} · ${b.ok} ok${b.failed ? ` · <span class="fail">${b.failed} failed</span>` : ""}</h4>
        <div class="meta">${esc(b.ts)} · ${esc(b.device || "")} · batch <code>${esc(b.batch_id)}</code> ${b.note ? `· ${esc(b.note)}` : ""}</div>
        <ul>${b.items.map((i) => `<li>${i.ok ? "✅" : "❌"} <code>${esc(i.package)}</code>
          <span class="badge-s">${esc(i.label || "")}</span> → <code>${esc(i.command || "")}</code></li>`).join("")}</ul>
        ${b.action !== "undo" ? `<button class="btn ghost tiny" data-undo="${esc(b.batch_id)}">↩ Undo this batch</button>` : ""}
      </div>`).join("");
    box.querySelectorAll("[data-undo]").forEach((btn) => btn.onclick = async () => {
      try {
        const res2 = await api("/api/undo", { method: "POST", body: { batch_id: btn.dataset.undo } });
        toast(`Undo: ${res2.applied} restored, ${res2.failed} failed`, res2.failed ? "bad" : "ok");
        await loadPackages(true); await loadPresets(); loadJournal();
      } catch (err) { toast(esc(err.message), "bad"); }
    });
  } catch (err) { toast(esc(err.message), "bad"); }
}

async function generateReport() {
  try {
    const res = await api("/api/report");
    $("#reportOut").textContent = res.markdown;
    toast(`Saved to <code>${esc(res.saved)}</code> and <code>${esc(res.undo_script_saved)}</code>`, "ok", 8000);
  } catch (err) { toast(esc(err.message), "bad"); }
}
