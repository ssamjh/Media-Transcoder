"use strict";

/* ---------- helpers ---------- */

const el = (id) => document.getElementById(id);

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function bytes(n) {
  n = Number(n || 0);
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i >= 3 ? 2 : 0)} ${u[i]}`;
}

function hms(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m${String(s % 60).padStart(2, "0")}s`;
  return `${s}s`;
}

function ago(t, now) {
  if (!t) return "never";
  const d = Math.max(0, (now || Date.now() / 1000) - t);
  if (d < 60) return `${Math.floor(d)}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

function toast(message, bad) {
  const node = document.createElement("div");
  node.className = "toast" + (bad ? " bad" : "");
  node.textContent = message;
  el("toasts").appendChild(node);
  setTimeout(() => node.remove(), 4200);
}

// Injected into index.html when the page is served. The panel is same-origin
// and unauthenticated either way; the key exists so Sonarr and Radarr have a
// credential to present.
const KEY = (window.__API_KEY__ || "").startsWith("__API") ? "" : window.__API_KEY__;

async function api(path, body) {
  const headers = KEY ? { "X-Api-Key": KEY } : {};
  const opts = body
    ? { method: "POST",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify(body) }
    : { headers };
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function act(path, body, okMessage) {
  try {
    const d = await api(path, body || {});
    toast(okMessage || d.message || "Done");
    return d;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

const STATUS_CLASS = {
  done: "ok", skip: "", pending: "info", queued: "info",
  running: "warn", failed: "bad", rejected: "warn", cancelled: "warn",
};

const statusTag = (s) =>
  `<span class="tag ${STATUS_CLASS[s] || ""}">${esc(s)}</span>`;

const sel = (scope, key) => `[data-key="${CSS.escape(scope + key)}"]`;

/* ---------- tabs ---------- */

const TABS = ["dashboard", "libraries", "files", "history", "settings"];
let current = "dashboard";

function show(tab) {
  if (!TABS.includes(tab)) tab = "dashboard";
  current = tab;
  TABS.forEach((t) => { el("tab-" + t).hidden = t !== tab; });
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("on", b.dataset.tab === tab));
  if (location.hash.slice(1) !== tab) location.hash = tab;
  if (tab === "libraries") loadLibraries();
  if (tab === "files") loadFiles();
  if (tab === "history") loadHistory();
  if (tab === "settings") loadSettings();
}

document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => show(b.dataset.tab)));
window.addEventListener("hashchange", () => show(location.hash.slice(1)));

/* ---------- drawer ---------- */

function openDrawer(title, html) {
  el("d-title").textContent = title;
  el("d-body").innerHTML = html;
  el("drawer").hidden = false;
}
function closeDrawer() { el("drawer").hidden = true; }
document.querySelectorAll("[data-close]").forEach((n) =>
  n.addEventListener("click", closeDrawer));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDrawer();
});

function planHtml(plan) {
  if (!plan) return '<p class="muted">No plan recorded.</p>';
  let h = "";
  if (plan.library_name) {
    h += `<p class="muted">Library: ${esc(plan.library_name)} · `
      + `container: ${esc(plan.container || "mkv")}</p>`;
  }
  if (plan.skip_reason) {
    h += `<h4>Skipped</h4><p>${esc(plan.skip_reason)}</p>`;
  }
  if (plan.reasons?.length) {
    h += "<h4>Work to do</h4><ul class='list'>" +
      plan.reasons.map((r) => `<li>${esc(r)}</li>`).join("") + "</ul>";
  }
  if (plan.dropped?.length) {
    h += "<h4>Dropped</h4><ul class='list'>" +
      plan.dropped.map((d) => `<li class="bad">${esc(d)}</li>`).join("") + "</ul>";
  }
  if (plan.streams?.length) {
    h += "<h4>Output streams</h4><div class='card tablewrap'><table><thead><tr>" +
      "<th>#</th><th>Kind</th><th>Codec</th><th>From</th><th>Notes</th>" +
      "</tr></thead><tbody>" +
      plan.streams.map((s, i) => `<tr>
        <td class="num">${i}</td>
        <td>${esc(s.kind)}</td>
        <td>${s.action === "encode"
          ? `<span class="ok">${esc(s.codec)}</span>` : esc(s.codec)}</td>
        <td class="num">0:${s.src_index}</td>
        <td>${esc([s.note, s.title ? `title "${s.title}"` : "",
          s.disposition === "default" ? "default" : ""]
          .filter(Boolean).join(", "))}</td>
      </tr>`).join("") + "</tbody></table></div>";
  }
  return h || '<p class="muted">Nothing to do.</p>';
}

async function openFile(path) {
  openDrawer("Loading…", '<p class="muted">Loading…</p>');
  let d;
  try {
    d = await api("/api/file?path=" + encodeURIComponent(path));
  } catch (e) {
    return openDrawer("Error", `<p class="bad">${esc(e.message)}</p>`);
  }
  const f = d.file;
  const now = Date.now() / 1000;
  const saved = f.in_size && f.out_size ? f.in_size - f.out_size : 0;

  let h = `<div class="path">${esc(f.path)}</div>
    <h4>State</h4>
    <dl class="kv">
      <dt>Status</dt><dd>${statusTag(f.status)}${d.exists ? "" :
        ' <span class="tag bad">missing on disk</span>'}</dd>
      <dt>Library</dt><dd>${esc(f.plan?.library_name || f.library || "—")}</dd>
      <dt>Size</dt><dd>${bytes(f.size)}</dd>
      <dt>Resolution</dt><dd>${f.height ? f.height + "p" : "—"}</dd>
      <dt>Video codec</dt><dd>${esc(f.video_codec || "—")}</dd>
      <dt>Checked</dt><dd>${ago(f.last_checked, now)}</dd>
      ${saved ? `<dt>Saved</dt><dd class="ok">${bytes(saved)} (${
        (100 - f.out_size / f.in_size * 100).toFixed(0)}%)</dd>` : ""}
      <dt>Attempts</dt><dd>${f.attempts} of ${d.max_attempts}</dd>
      ${f.error ? `<dt>Note</dt><dd class="bad">${esc(f.error)}</dd>` : ""}
    </dl>
    <h4>Actions</h4>
    <div class="toolbar">
      <select id="d-mode" title="Applies to this run only">
        <option value="">Library profile</option>
        ${modes.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("")}
      </select>
      <button class="small" data-act="check">Re-check</button>
      <button class="small primary" data-act="process">Process now</button>
      <button class="small" data-act="force">Force retry</button>
      <button class="small danger" data-act="cancel">Cancel</button>
    </div>
    ${planHtml(f.plan)}`;

  if (d.history.length) {
    h += "<h4>History</h4><div class='card tablewrap'><table><thead><tr>" +
      "<th>When</th><th>Result</th><th>Before</th><th>After</th><th>Took</th>" +
      "</tr></thead><tbody>" +
      d.history.map((r) => `<tr>
        <td>${ago(r.finished, now)}</td>
        <td>${statusTag(r.status)}</td>
        <td class="num">${bytes(r.in_size)}</td>
        <td class="num">${bytes(r.out_size)}</td>
        <td class="num">${hms(r.elapsed)}</td>
      </tr>`).join("") + "</tbody></table></div>";
  }

  openDrawer(f.name || path, h);

  el("d-body").querySelectorAll("[data-act]").forEach((b) =>
    b.addEventListener("click", async () => {
      const a = b.dataset.act;
      const mode = el("d-mode")?.value || "";
      if (a === "check") {
        if (await act("/api/check", { path, mode }, "Checked")) openFile(path);
      } else if (a === "process") {
        if (await act("/api/process", { path, mode })) openFile(path);
      } else if (a === "force") {
        if (await act("/api/process", { path, mode, force: true })) openFile(path);
      } else if (a === "cancel") {
        if (await act("/api/cancel", { path })) openFile(path);
      }
    }));
}

/* ---------- settings fields (shared by Settings and Libraries) ---------- */

function readControl(node, type) {
  if (!node) return null;
  if (type === "bool") return node.checked;
  if (type === "list") {
    return node.value.split("\n").map((s) => s.trim())
      .filter((s, i, a) => s !== "" || i < a.length - 1);
  }
  if (type === "map") {
    const out = {};
    for (const line of node.value.split("\n")) {
      const t = line.trim();
      if (!t) continue;
      const [k, v] = t.split("=").map((x) => (x || "").trim());
      if (k) out[k] = v;
    }
    return out;
  }
  return node.value;
}

function control(f, scope) {
  const key = esc(scope + f.key);
  if (f.readonly) return `<input type="text" value="${esc(f.value)}" disabled>`;
  if (f.type === "bool") {
    return `<input type="checkbox" data-key="${key}" ${f.value ? "checked" : ""}>`;
  }
  if (f.choices) {
    return `<select data-key="${key}">${f.choices.map((c) =>
      `<option ${c === f.value ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  }
  if (f.type === "list") {
    return `<textarea data-key="${key}" spellcheck="false"
      rows="${Math.min(8, Math.max(2, f.value.length))}">${esc(f.value.join("\n"))}</textarea>
      <span class="hint">One per line</span>`;
  }
  if (f.type === "map") {
    const text = Object.entries(f.value).map(([k, v]) => `${k} = ${v}`).join("\n");
    return `<textarea data-key="${key}" spellcheck="false"
      rows="${Math.min(9, Math.max(2, Object.keys(f.value).length))}">${esc(text)}</textarea>
      <span class="hint">${esc(f.hint || "One key = number per line")}</span>`;
  }
  if (f.type === "int" || f.type === "float") {
    const step = f.type === "float" ? "any" : "1";
    return `<input type="number" step="${step}" data-key="${key}"
      ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""}
      value="${esc(f.value)}">
      ${f.min != null || f.max != null
        ? `<span class="hint">${f.min ?? ""}–${f.max ?? ""}</span>` : ""}`;
  }
  return `<input type="text" data-key="${key}" value="${esc(f.value)}">`;
}

function sectionsHtml(blocks, scope) {
  return blocks.map((sect) => `
    <div class="sect">
      <h3>${esc(sect.title)}</h3>
      ${sect.fields.map((f) => `
        <div class="field">
          <div>
            <label>${esc(f.name)}</label>
            <div class="desc">${esc(f.desc)}${f.restart
              ? " <em>Takes effect on restart.</em>" : ""}</div>
          </div>
          <div class="ctl">${control(f, scope)}</div>
        </div>`).join("")}
    </div>`).join("");
}

function collectDirty(blocks, scope, root) {
  const out = new Map();
  for (const sect of blocks) {
    for (const f of sect.fields) {
      if (f.readonly) continue;
      const node = root.querySelector(sel(scope, f.key));
      if (!node) continue;
      const v = readControl(node, f.type);
      const changed = JSON.stringify(v) !== JSON.stringify(f.value);
      node.closest(".field")?.classList.toggle("dirty", changed);
      if (changed) out.set(f.key, v);
    }
  }
  return out;
}

/* ---------- libraries ---------- */

let libraries = [];
const libOpen = new Set();

function statsHtml(st) {
  return `<span class="tag">${st.total} tracked</span>
    <span class="tag info">${st.counts.pending || 0} need work</span>
    <span class="tag ok">${st.counts.done || 0} done</span>
    ${st.counts.failed ? `<span class="tag bad">${st.counts.failed} failed</span>` : ""}
    <span class="tag">${bytes(st.bytes)} on disk</span>
    ${st.saved > 0 ? `<span class="tag ok">${bytes(st.saved)} reclaimed</span>` : ""}`;
}

function renderLibraries() {
  el("libraries").innerHTML = libraries.map((lib) => {
    const st = lib.stats || { total: 0, counts: {}, saved: 0, bytes: 0 };
    const s = lib.stages;
    const open = libOpen.has(lib.id);
    return `<div class="lib ${lib.enabled ? "" : "off"}" data-lib="${esc(lib.id)}">
      <div class="lib-head">
        <label class="toggle" title="Include in scans">
          <input type="checkbox" data-lib-on="${esc(lib.id)}" ${lib.enabled ? "checked" : ""}>
        </label>
        <span class="nm">${esc(lib.name)}</span>
        <span class="tag">${esc(lib.id)}</span>
        <div class="grow"></div>
        <button class="small" data-lib-scan="${esc(lib.id)}">Scan</button>
        ${s.notify ? `<button class="small" data-lib-notify="${esc(lib.id)}">Test hooks</button>` : ""}
        <button class="small" data-lib-edit="${esc(lib.id)}">${
          open ? "Close" : "Configure"}</button>
        <button class="small danger" data-lib-del="${esc(lib.id)}">Delete</button>
      </div>
      <div class="lib-paths">${lib.paths.map(esc).join("<br>")}</div>
      <div class="stage-row">
        <span class="stage ${s.video ? "on" : "off"}">re-encode video</span>
        <span class="stage ${s.audio ? "on" : "off"}">clean audio</span>
        <span class="stage ${s.subtitles ? "on" : "off"}">clean subtitles</span>
        <span class="stage ${s.replace ? "on" : "off"}">replace originals</span>
        <span class="stage ${s.notify ? "on" : "off"}">notify on finish</span>
      </div>
      <div class="lib-stats">${statsHtml(st)}</div>
      ${open ? `<div class="lib-body">
        <div class="toolbar" style="padding:13px 16px 0;margin:0">
          <button class="primary small" data-lib-save="${esc(lib.id)}" disabled>Save changes</button>
          <button class="small" data-lib-discard="${esc(lib.id)}">Discard</button>
          <span class="muted" data-lib-note="${esc(lib.id)}"></span>
        </div>
        ${sectionsHtml(lib.schema, lib.id + ":")}
      </div>` : ""}
    </div>`;
  }).join("") || '<div class="card"><div class="empty">No libraries yet</div></div>';

  wireLibraries();
}

function refreshLibDirty(lib) {
  const root = el("libraries").querySelector(`[data-lib="${CSS.escape(lib.id)}"]`);
  if (!root) return new Map();
  const d = collectDirty(lib.schema, lib.id + ":", root);
  const save = root.querySelector(`[data-lib-save="${CSS.escape(lib.id)}"]`);
  const note = root.querySelector(`[data-lib-note="${CSS.escape(lib.id)}"]`);
  if (save) save.disabled = d.size === 0;
  if (note) note.textContent = d.size ? `${d.size} unsaved` : "";
  return d;
}

function wireLibraries() {
  const root = el("libraries");

  root.querySelectorAll("[data-lib-edit]").forEach((b) =>
    b.addEventListener("click", () => {
      const id = b.dataset.libEdit;
      if (libOpen.has(id)) libOpen.delete(id); else libOpen.add(id);
      renderLibraries();
    }));

  root.querySelectorAll("[data-lib-scan]").forEach((b) =>
    b.addEventListener("click", () =>
      act("/api/scan", { library: b.dataset.libScan }, "Scan requested")));

  root.querySelectorAll("[data-lib-notify]").forEach((b) =>
    b.addEventListener("click", () =>
      act("/api/notify/test", { library: b.dataset.libNotify },
          "Test calls queued")));

  root.querySelectorAll("[data-lib-on]").forEach((n) =>
    n.addEventListener("change", async () => {
      const d = await act("/api/libraries/update",
        { id: n.dataset.libOn, updates: { enabled: n.checked } },
        n.checked ? "Library enabled" : "Library disabled");
      if (d) { libraries = d.libraries; renderLibraries(); } else loadLibraries();
    }));

  root.querySelectorAll("[data-lib-del]").forEach((b) =>
    b.addEventListener("click", async () => {
      const lib = libraries.find((l) => l.id === b.dataset.libDel);
      if (!confirm(`Delete library "${lib.name}"? Its tracked state is forgotten. `
        + "No media files are touched.")) return;
      const d = await act("/api/libraries/delete", { id: lib.id });
      if (d) { libraries = d.libraries; libOpen.delete(lib.id); renderLibraries(); }
    }));

  libraries.filter((l) => libOpen.has(l.id)).forEach((lib) => {
    const scope = CSS.escape(lib.id + ":");
    root.querySelectorAll(`[data-key^="${scope}"]`).forEach((n) => {
      n.addEventListener("input", () => refreshLibDirty(lib));
      n.addEventListener("change", () => refreshLibDirty(lib));
    });
    root.querySelector(`[data-lib-discard="${CSS.escape(lib.id)}"]`)
      ?.addEventListener("click", () => renderLibraries());
    root.querySelector(`[data-lib-save="${CSS.escape(lib.id)}"]`)
      ?.addEventListener("click", async () => {
        const d = refreshLibDirty(lib);
        if (!d.size) return;
        const res = await act("/api/libraries/update",
          { id: lib.id, updates: Object.fromEntries(d) },
          `Saved ${d.size} setting${d.size > 1 ? "s" : ""}`);
        if (res) { libraries = res.libraries; renderLibraries(); }
      });
    refreshLibDirty(lib);
  });
}

async function refreshLibStats() {
  // A scan running in the background keeps changing these counts, but a
  // re-render would throw away whatever the user is typing into an open
  // Configure panel - so update only the numbers.
  let fresh;
  try {
    fresh = (await api("/api/libraries")).libraries;
  } catch { return; }
  for (const lib of fresh) {
    const known = libraries.find((l) => l.id === lib.id);
    if (known) known.stats = lib.stats;
    const node = el("libraries")
      .querySelector(`[data-lib="${CSS.escape(lib.id)}"] .lib-stats`);
    if (node) node.innerHTML = statsHtml(lib.stats);
  }
}

async function loadLibraries() {
  try {
    libraries = (await api("/api/libraries")).libraries;
    renderLibraries();
  } catch (e) {
    el("libraries").innerHTML =
      `<div class="card"><div class="empty bad">${esc(e.message)}</div></div>`;
  }
}

el("l-add").addEventListener("click", () => {
  openDrawer("Add library", `<div class="newlib">
    <div><label for="nl-name">Name</label>
      <input id="nl-name" type="text" placeholder="TV Shows"></div>
    <div><label for="nl-paths">Paths</label>
      <textarea id="nl-paths" spellcheck="false" placeholder="/media/TV"></textarea>
      <span class="hint">One per line, as seen inside the container. They must
        not overlap another library.</span></div>
    <div><button class="primary" id="nl-go">Create</button></div>
    <p class="muted">The new library starts from the default profile.
      Configure it afterwards.</p>
  </div>`);
  el("nl-go").addEventListener("click", async () => {
    const d = await act("/api/libraries/add", {
      name: el("nl-name").value,
      paths: el("nl-paths").value,
    }, "Library created");
    if (d) {
      libraries = d.libraries;
      libOpen.add(d.id);
      closeDrawer();
      show("libraries");
      renderLibraries();
    }
  });
  el("nl-name").focus();
});

/* ---------- dashboard ---------- */

let schedBusy = false;

async function tick() {
  let d;
  try {
    d = await api("/api/status");
  } catch {
    el("conn").textContent = "disconnected";
    el("conn").className = "pill bad";
    return;
  }
  el("conn").className = "pill";
  el("conn").textContent = d.scanning
    ? `scanning ${d.scan_progress[0]}/${d.scan_progress[1]}`
    : `up ${hms(d.uptime)}`;

  if (!schedBusy) el("sched-toggle").checked = !!d.schedule_enabled;
  let note = d.schedule_enabled
    ? `every ${d.scan_interval_hours}h · next ${
        d.next_scan ? hms(d.next_scan - d.now) : "—"}`
    : "off — scans only when you ask";
  const warn = [];
  if (d.dry_run) warn.push("dry run");
  const off = d.libraries.filter((l) => !l.enabled).length;
  if (off) warn.push(`${off} library disabled`);
  if (warn.length) note += `  •  ${warn.join(", ")}`;
  el("sched-note").textContent = note;

  const libSel = el("f-library");
  const sig = d.libraries.map((l) => l.id).join("|");
  if (libSel.dataset.sig !== sig) {
    libSel.dataset.sig = sig;
    const keep = libSel.value;
    libSel.innerHTML = '<option value="">All libraries</option>' +
      d.libraries.map((l) =>
        `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("");
    libSel.value = keep;
  }

  const c = d.counts || {};
  el("tiles").innerHTML = [
    [d.libraries.length, "libraries"],
    [d.total, "files tracked"],
    [c.pending || 0, "need work"],
    [d.queue_depth, "queued"],
    [d.active.length, "encoding"],
    [d.encoded, "encoded"],
    [bytes(d.bytes_saved), "reclaimed"],
    [c.failed || 0, "failed"],
  ].map(([n, l]) =>
    `<div class="tile"><div class="n">${n}</div><div class="l">${l}</div></div>`
  ).join("");

  el("active-count").textContent = d.active.length || "";
  el("active").innerHTML = d.active.length ? d.active.map((j) => `
    <div class="job">
      <div class="job-head">
        <div class="job-name">${esc(j.name)}</div>
        <button class="small danger" data-cancel="${esc(j.path)}">Cancel</button>
      </div>
      <div class="meta">${j.percent.toFixed(1)}% · ${j.speed.toFixed(2)}x ·
        ${hms(j.elapsed)} elapsed${j.eta ? " · " + hms(j.eta) + " left" : ""}
        · ${bytes(j.in_size)}${j.library ? " · " + esc(j.library) : ""}</div>
      <div class="meta">${esc(j.reasons.join("; "))}</div>
      <div class="bar-track"><i style="width:${j.percent}%"></i></div>
    </div>`).join("") : '<div class="empty">Idle</div>';

  el("active").querySelectorAll("[data-cancel]").forEach((b) =>
    b.addEventListener("click", () => act("/api/cancel", { path: b.dataset.cancel })));

  el("queued-count").textContent = d.queue_depth || "";
  el("queued").innerHTML = d.queued.length
    ? d.queued.map((q) => `<div class="job"><div class="job-head">
        <div class="job-name">${esc(q.name)}</div>
        <button class="small" data-cancel2="${esc(q.path)}">Remove</button>
      </div></div>`).join("")
    : '<div class="empty">Nothing queued</div>';
  el("queued").querySelectorAll("[data-cancel2]").forEach((b) =>
    b.addEventListener("click", () => act("/api/cancel", { path: b.dataset.cancel2 })));

  const s = d.last_scan_summary || {};
  const perLib = Object.values(s.libraries || {});
  el("scaninfo").innerHTML = d.last_scan ? `<div class="job">
      <div class="meta">${ago(d.last_scan, d.now)} · took ${
        (s.elapsed || 0).toFixed(1)}s</div>
      <div style="margin-top:6px">
        <span class="tag">${s.examined || 0} examined</span>
        <span class="tag info">${s.need_work || 0} need work</span>
        <span class="tag ok">${s.already_fine || 0} already fine</span>
        <span class="tag">${s.cached || 0} cached</span>
        ${s.removed ? `<span class="tag">${s.removed} gone</span>` : ""}
        ${s.unowned ? `<span class="tag">${s.unowned} no longer in a library</span>` : ""}
        ${s.errors ? `<span class="tag bad">${s.errors} errors</span>` : ""}
      </div>
      ${perLib.length ? `<div class="meta" style="margin-top:7px">${
        perLib.map((t) => `${esc(t.name)}: ${t.examined} examined, ${
          t.need_work} need work`).join(" · ")}</div>` : ""}
    </div>` : '<div class="empty">No scan yet</div>';

  el("recent").innerHTML = d.recent.length ? `<table><thead><tr>
      <th>File</th><th>Result</th><th class="num">Before</th>
      <th class="num">After</th><th class="num">Saved</th>
      <th class="num">Took</th><th class="num">When</th></tr></thead><tbody>
      ${d.recent.map((r) => rowHistory(r, d.now)).join("")}
    </tbody></table>` : '<div class="empty">Nothing yet</div>';
  wireRows(el("recent"));
}

function rowHistory(r, now) {
  const saved = r.in_size && r.out_size ? 100 - (r.out_size / r.in_size) * 100 : 0;
  return `<tr class="click" data-path="${esc(r.path)}">
    <td class="name">${esc(r.name || r.path)}</td>
    <td>${statusTag(r.status)}${r.error
      ? ` <span class="muted">${esc(r.error.slice(0, 60))}</span>` : ""}</td>
    <td class="num">${bytes(r.in_size)}</td>
    <td class="num">${bytes(r.out_size)}</td>
    <td class="num ${saved > 0 ? "ok" : ""}">${
      saved > 0 ? saved.toFixed(0) + "%" : "—"}</td>
    <td class="num">${hms(r.elapsed)}</td>
    <td class="num">${ago(r.finished, now)}</td></tr>`;
}

function wireRows(root) {
  root.querySelectorAll("tr[data-path]").forEach((tr) =>
    tr.addEventListener("click", () => openFile(tr.dataset.path)));
}

el("btn-scan").addEventListener("click", () => act("/api/scan"));
el("btn-queue").addEventListener("click", () => act("/api/queue-pending"));
el("btn-cancel-all").addEventListener("click", () => act("/api/cancel-all"));
el("sched-toggle").addEventListener("change", async (e) => {
  schedBusy = true;
  await act("/api/schedule", { enabled: e.target.checked }, "Schedule updated");
  schedBusy = false;
});

/* ---------- files ---------- */

let fOffset = 0;
let fTimer = null;

async function loadFiles() {
  const q = el("f-q").value.trim();
  const params = new URLSearchParams({
    status: el("f-status").value, order: el("f-order").value,
    limit: "50", offset: String(fOffset),
  });
  if (q) params.set("q", q);
  if (el("f-library").value) params.set("library", el("f-library").value);

  let d;
  try {
    d = await api("/api/files?" + params);
  } catch (e) {
    el("files").innerHTML = `<div class="empty bad">${esc(e.message)}</div>`;
    return;
  }
  const now = Date.now() / 1000;

  el("files").innerHTML = d.files.length ? `<table><thead><tr>
      <th>File</th><th>Library</th><th>Status</th><th class="num">Size</th>
      <th class="num">Res</th><th>Video</th><th>Work</th>
      <th class="num">Checked</th><th></th>
    </tr></thead><tbody>${d.files.map((f) => `
      <tr class="click" data-path="${esc(f.path)}">
        <td class="name">${esc(f.name || f.path)}</td>
        <td>${esc(f.library || "—")}</td>
        <td>${statusTag(f.is_running ? "running" : f.status)}</td>
        <td class="num">${bytes(f.size)}</td>
        <td class="num">${f.height ? f.height + "p" : "—"}</td>
        <td>${esc(f.video_codec || "—")}</td>
        <td class="muted">${esc((f.reasons || []).join("; ") || "—")}</td>
        <td class="num">${ago(f.last_checked, now)}</td>
        <td class="actions">
          <button class="small" data-go="${esc(f.path)}">Process</button>
        </td>
      </tr>`).join("")}</tbody></table>`
    : '<div class="empty">No matching files</div>';

  wireRows(el("files"));
  el("files").querySelectorAll("[data-go]").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      act("/api/process", { path: b.dataset.go });
    }));

  const from = d.total ? fOffset + 1 : 0;
  el("files-pager").innerHTML = `
    <button ${fOffset === 0 ? "disabled" : ""} id="f-prev">Previous</button>
    <button ${fOffset + d.limit >= d.total ? "disabled" : ""} id="f-next">Next</button>
    <span class="muted">${from}–${Math.min(fOffset + d.limit, d.total)} of ${d.total}</span>`;
  el("f-prev")?.addEventListener("click", () => {
    fOffset = Math.max(0, fOffset - 50); loadFiles();
  });
  el("f-next")?.addEventListener("click", () => { fOffset += 50; loadFiles(); });
}

el("f-q").addEventListener("input", () => {
  clearTimeout(fTimer);
  fTimer = setTimeout(() => { fOffset = 0; loadFiles(); }, 250);
});
["f-status", "f-order", "f-library"].forEach((id) =>
  el(id).addEventListener("change", () => { fOffset = 0; loadFiles(); }));
el("f-retry-all").addEventListener("click", async () => {
  if (await act("/api/retry")) loadFiles();
});

/* ---------- history ---------- */

let hOffset = 0;

async function loadHistory() {
  let d;
  try {
    d = await api(`/api/history?limit=50&offset=${hOffset}`);
  } catch (e) {
    el("history").innerHTML = `<div class="empty bad">${esc(e.message)}</div>`;
    return;
  }
  const now = Date.now() / 1000;
  el("history").innerHTML = d.history.length ? `<table><thead><tr>
      <th>File</th><th>Result</th><th class="num">Before</th><th class="num">After</th>
      <th class="num">Saved</th><th class="num">Took</th><th class="num">When</th>
    </tr></thead><tbody>${d.history.map((r) => rowHistory(r, now)).join("")}</tbody></table>`
    : '<div class="empty">Nothing yet</div>';
  wireRows(el("history"));

  const from = d.total ? hOffset + 1 : 0;
  el("history-pager").innerHTML = `
    <button ${hOffset === 0 ? "disabled" : ""} id="h-prev">Previous</button>
    <button ${hOffset + d.limit >= d.total ? "disabled" : ""} id="h-next">Next</button>
    <span class="muted">${from}–${Math.min(hOffset + d.limit, d.total)} of ${d.total}</span>`;
  el("h-prev")?.addEventListener("click", () => {
    hOffset = Math.max(0, hOffset - 50); loadHistory();
  });
  el("h-next")?.addEventListener("click", () => { hOffset += 50; loadHistory(); });
}

el("h-clear").addEventListener("click", async () => {
  if (!confirm("Delete all history records? File state is kept.")) return;
  if (await act("/api/history/clear")) { hOffset = 0; loadHistory(); }
});

/* ---------- settings ---------- */

let schema = [];
let dirty = new Map();

function markDirty() {
  dirty = collectDirty(schema, "", el("settings"));
  el("s-note").textContent = dirty.size
    ? `${dirty.size} unsaved change${dirty.size > 1 ? "s" : ""}`
    : "";
  el("s-save").disabled = dirty.size === 0;
}

function renderSettings() {
  el("settings").innerHTML = sectionsHtml(schema, "");
  el("settings").querySelectorAll("[data-key]").forEach((n) => {
    n.addEventListener("input", markDirty);
    n.addEventListener("change", markDirty);
  });
  markDirty();
}

async function loadSettings() {
  try {
    const d = await api("/api/config");
    schema = d.schema;
    window.__toml = d.toml;
    window.__cfgpath = d.path;
    renderSettings();
    loadModes();
  } catch (e) {
    el("settings").innerHTML = `<div class="card"><div class="empty bad">${
      esc(e.message)}</div></div>`;
  }
}

el("s-save").addEventListener("click", async () => {
  if (!dirty.size) return;
  el("s-save").disabled = true;
  try {
    const d = await api("/api/config", { updates: Object.fromEntries(dirty) });
    schema = d.schema;
    window.__toml = d.toml;
    renderSettings();
    toast(`Saved ${d.changed.length} setting${d.changed.length > 1 ? "s" : ""}`);
    if (d.needs_restart.length) {
      toast(`Restart required for: ${d.needs_restart.join(", ")}`, true);
    }
  } catch (e) {
    toast(e.message, true);
    el("s-save").disabled = false;
  }
});

el("s-reset").addEventListener("click", () => renderSettings());

el("s-toml").addEventListener("click", () => {
  openDrawer(window.__cfgpath || "config.toml",
    `<pre class="toml">${esc(window.__toml || "")}</pre>`);
});

/* ---------- modes ---------- */

let modes = [];
let libraryKeys = [];
const modeOpen = new Set();

function renderModes() {
  el("modes").innerHTML = modes.map((m) => {
    const open = modeOpen.has(m.id);
    const keys = Object.keys(m.overrides);
    const summary = keys.length
      ? keys.map((k) => `<span class="stage off">${esc(k)} = ${
          esc(String(m.overrides[k]))}</span>`).join("")
      : '<span class="stage on">the library profile, unchanged</span>';
    return `<div class="lib" data-mode="${esc(m.id)}">
      <div class="lib-head">
        <span class="nm">${esc(m.name)}</span>
        <span class="tag">${esc(m.id)}</span>
        <div class="grow"></div>
        <button class="small" data-mode-edit="${esc(m.id)}">${
          open ? "Close" : "Configure"}</button>
        <button class="small danger" data-mode-del="${esc(m.id)}">Delete</button>
      </div>
      ${m.description ? `<div class="lib-paths">${esc(m.description)}</div>` : ""}
      <div class="stage-row">${summary}</div>
      ${open ? `<div class="lib-body">
        <div class="toolbar" style="padding:13px 16px 0;margin:0">
          <button class="primary small" data-mode-save="${esc(m.id)}" disabled>Save changes</button>
          <button class="small" data-mode-discard="${esc(m.id)}">Discard</button>
          <span class="muted" data-mode-note="${esc(m.id)}"></span>
        </div>
        ${sectionsHtml(m.schema, "mode:" + m.id + ":")}
        <div class="sect">
          <h3>Available keys</h3>
          <div class="field"><div class="desc">${
            libraryKeys.map((k) => `<code>${esc(k)}</code>`).join(" ")}</div></div>
        </div>
      </div>` : ""}
    </div>`;
  }).join("") || '<div class="card"><div class="empty">No modes defined</div></div>';

  wireModes();
}

function refreshModeDirty(m) {
  const root = el("modes").querySelector(`[data-mode="${CSS.escape(m.id)}"]`);
  if (!root) return new Map();
  const d = collectDirty(m.schema, "mode:" + m.id + ":", root);
  const save = root.querySelector(`[data-mode-save="${CSS.escape(m.id)}"]`);
  const note = root.querySelector(`[data-mode-note="${CSS.escape(m.id)}"]`);
  if (save) save.disabled = d.size === 0;
  if (note) note.textContent = d.size ? `${d.size} unsaved` : "";
  return d;
}

function wireModes() {
  const root = el("modes");

  root.querySelectorAll("[data-mode-edit]").forEach((b) =>
    b.addEventListener("click", () => {
      const id = b.dataset.modeEdit;
      if (modeOpen.has(id)) modeOpen.delete(id); else modeOpen.add(id);
      renderModes();
    }));

  root.querySelectorAll("[data-mode-del]").forEach((b) =>
    b.addEventListener("click", async () => {
      const m = modes.find((x) => x.id === b.dataset.modeDel);
      if (!m) return;
      if (!confirm(`Delete the "${m.name}" mode? Files are not touched.`)) return;
      if (await act("/api/modes/delete", { id: m.id })) loadModes();
    }));

  modes.filter((m) => modeOpen.has(m.id)).forEach((m) => {
    const scope = CSS.escape("mode:" + m.id + ":");
    root.querySelectorAll(`[data-key^="${scope}"]`).forEach((n) => {
      n.addEventListener("input", () => refreshModeDirty(m));
      n.addEventListener("change", () => refreshModeDirty(m));
    });
    root.querySelector(`[data-mode-discard="${CSS.escape(m.id)}"]`)
      ?.addEventListener("click", () => renderModes());
    root.querySelector(`[data-mode-save="${CSS.escape(m.id)}"]`)
      ?.addEventListener("click", async () => {
        const d = refreshModeDirty(m);
        if (!d.size) return;
        const res = await act("/api/modes/update",
          { id: m.id, updates: Object.fromEntries(d) });
        if (res) loadModes();
      });
  });
}

async function loadModes() {
  try {
    const d = await api("/api/modes");
    modes = d.modes;
    libraryKeys = d.library_keys || [];
    renderModes();
    renderIntegration();
  } catch (e) {
    el("modes").innerHTML = `<div class="card"><div class="empty bad">${
      esc(e.message)}</div></div>`;
  }
}

el("m-add").addEventListener("click", () => {
  openDrawer("Add mode", `
    <div class="sect">
      <div class="field">
        <div><label>Name</label><div class="desc">A display name. The id is
          generated from it.</div></div>
        <div class="ctl"><input type="text" id="nm-name" placeholder="Cleanup"></div>
      </div>
      <div class="field">
        <div><label>Overrides</label><div class="desc">Library settings this
          mode replaces, one per line.</div></div>
        <div class="ctl"><textarea id="nm-ov" rows="4" spellcheck="false"
          placeholder="video.enabled = false"></textarea>
          <span class="hint">One library.setting = value per line</span></div>
      </div>
    </div>
    <div class="toolbar"><button class="primary" id="nm-go">Add mode</button></div>`);

  el("nm-go").addEventListener("click", async () => {
    const overrides = {};
    for (const line of el("nm-ov").value.split(String.fromCharCode(10))) {
      const t = line.trim();
      if (!t) continue;
      const i = t.indexOf("=");
      if (i < 0) continue;
      overrides[t.slice(0, i).trim()] = t.slice(i + 1).trim();
    }
    const d = await act("/api/modes/add",
      { name: el("nm-name").value, overrides });
    if (d) { closeDrawer(); loadModes(); }
  });
});

function renderIntegration() {
  const origin = location.origin;
  const auth = KEY ? ` -H "X-Api-Key: ${KEY}"` : "";
  const list = modes.map((m) => `<code>${esc(m.id)}</code>`).join(", ");
  const nl = String.fromCharCode(10);
  const script = [
    "#!/bin/sh",
    "# Sonarr: Settings > Connect > Custom Script, on Import and Upgrade.",
    "# Radarr: use $radarr_moviefile_path instead.",
    'curl -fsS -X POST ' + origin + '/api/process' + auth + " \\",
    '  -H "Content-Type: application/json" \\',
    '  -d "{\\"path\\": \\"$sonarr_episodefile_path\\", \\"mode\\": \\"cleanup\\"}"',
  ].join(nl);

  el("integration").innerHTML = `
    <div class="sect">
      <h3>API key</h3>
      <div class="field">
        <div><div class="desc">Sent as an <code>X-Api-Key</code> header or an
          <code>?apikey=</code> parameter on every <code>/api/</code> request.
          The panel embeds it, so anyone who can load this page can read it —
          it authenticates Sonarr and Radarr, it does not make the panel safe
          to expose.</div></div>
        <div class="ctl">
          <input type="text" id="int-key" value="${esc(KEY || "(no key set)")}" readonly>
          <span class="hint">Change it under Settings &rarr; Web panel</span>
        </div>
      </div>
      <h3>Process a file on import</h3>
      <div class="field">
        <div><div class="desc">Queues one file. <code>mode</code> is optional and
          applies to that run only — the next scheduled scan plans the file under
          its library's normal profile again. Known modes: ${list}.</div></div>
        <div class="ctl"><pre class="toml">${esc(script)}</pre></div>
      </div>
    </div>`;
}

/* ---------- boot ---------- */

show(location.hash.slice(1) || "dashboard");
loadModes();
tick();
setInterval(() => { if (current === "dashboard") tick(); }, 2000);
setInterval(() => { if (current === "files") loadFiles(); }, 8000);
setInterval(() => {
  if (current !== "libraries") return;
  if (libOpen.size === 0) loadLibraries(); else refreshLibStats();
}, 8000);
