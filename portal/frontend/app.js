/* NSL Portal app — talks to /api (same prefix as page URL). No placeholders; labels above inputs. */
"use strict";
const PREFIX = location.pathname.startsWith("/portal") ? "/portal" : "";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const S = { token: localStorage.getItem("nsl_token") || "", orgs: [], org: localStorage.getItem("nsl_org") || "",
  programs: [], controllers: [], curProg: null, curDest: null };

function err(msg) { $("err").textContent = msg || ""; }
function apiHeaders(json) {
  const h = { Authorization: "Bearer " + S.token };
  if (json) h["Content-Type"] = "application/json";
  return h;
}
async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign(apiHeaders(opts.json !== false && opts.body && typeof opts.body !== "string"), opts.headers || {});
  if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) opts.body = JSON.stringify(opts.body);
  const r = await fetch(PREFIX + path, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  if (!r.ok) throw new Error((body && body.detail) || ("HTTP " + r.status));
  return body;
}
function formData(obj) { const f = new FormData(); for (const k in obj) f.append(k, obj[k]); return f; }

// ---------- auth ----------
async function refreshMe() {
  if (!S.token) { showAuth(false); return; }
  try {
    const me = await api("/api/me");
    S.orgs = me.organisations || [];
    if (!S.orgs.some((o) => o.id === S.org)) S.org = (S.orgs[0] || {}).id || "";
    localStorage.setItem("nsl_org", S.org);
    $("who").textContent = "Signed in as " + me.user.email;
    $("orgsel").innerHTML = S.orgs.map((o) => `<option value="${o.id}"${o.id === S.org ? " selected" : ""}>${esc(o.name)} (${o.role})</option>`).join("");
    showAuth(true);
    await loadAll();
  } catch (e) { showAuth(false); $("auth-err").textContent = "Session expired — sign in again."; }
}
function showAuth(on) {
  $("authview").style.display = on ? "none" : "";
  $("mainview").style.display = on ? "" : "none";
  $("authbar").style.display = on ? "" : "none";
}
async function doLogin() {
  $("auth-err").textContent = "";
  try {
    const r = await api("/api/auth/login", { method: "POST", body: formData({ email: $("li-email").value, password: $("li-pass").value }) });
    S.token = r.token; localStorage.setItem("nsl_token", S.token);
    await refreshMe();
  } catch (e) { $("auth-err").textContent = String(e.message || e); }
}
async function doSignup() {
  const extra = $("auth-signup-extra");
  if (extra.style.display === "none") { extra.style.display = ""; return; }
  $("auth-err").textContent = "";
  try {
    const r = await api("/api/auth/signup", { method: "POST", body: formData({
      email: $("li-email").value, password: $("li-pass").value,
      name: $("su-name").value, org: $("su-org").value }) });
    S.token = r.token; localStorage.setItem("nsl_token", S.token);
    await refreshMe();
  } catch (e) { $("auth-err").textContent = String(e.message || e); }
}

// ---------- tabs ----------
document.querySelectorAll("#tabs .tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll("#tabs .tab").forEach((x) => x.classList.remove("on"));
  t.classList.add("on");
  ["programs", "codes", "controllers", "audio", "remote"].forEach((n) => { $("tab-" + n).style.display = (n === t.dataset.tab) ? "" : "none"; });
}));

// ---------- programs ----------
async function loadAll() {
  err("");
  await Promise.all([loadPrograms(), loadControllers(), loadAudio(), loadVoices()]);
}
async function loadPrograms() {
  const r = await api("/api/programs?organisation_id=" + encodeURIComponent(S.org));
  S.programs = r.programs || [];
  $("progrows").innerHTML = S.programs.map((p) =>
    `<tr><td>${esc(p.name)}</td><td>${esc(p.published_version || 0)}</td><td>${esc(p.published_version || 0)}</td><td class=row>` +
    `<button data-open="${p.id}">Open</button><button data-dup="${p.id}">Duplicate</button>` +
    `<button data-exp="${p.id}">Export</button><button data-del="${p.id}">Delete</button></td></tr>`).join("");
  $("codes-prog").innerHTML = S.programs.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $("rc-prog").innerHTML = S.programs.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
}
$("progrows").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  try {
    if (b.dataset.open) return openProgram(b.dataset.open);
    if (b.dataset.dup) { await api(`/api/programs/${b.dataset.dup}/duplicate`, { method: "POST" }); return loadPrograms(); }
    if (b.dataset.del && confirm("Delete this program and all its drafts?")) {
      await api(`/api/programs/${b.dataset.del}`, { method: "DELETE" }); return loadPrograms();
    }
    if (b.dataset.exp) {
      const p = S.programs.find((x) => x.id === b.dataset.exp);
      const r = await fetch(PREFIX + `/api/programs/${p.id}/versions/${p.published_version}/export`, { headers: apiHeaders() });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = `${p.name}-v${p.published_version}.nslpack`; a.click();
    }
  } catch (ex) { err(String(ex.message || ex)); }
});
$("btn-newprog").addEventListener("click", async () => {
  try {
    await api("/api/programs", { method: "POST", body: { organisation_id: S.org, name: $("np-name").value, px_width: parseInt($("np-width").value || "240", 10) } });
    $("np-name").value = ""; await loadPrograms();
  } catch (e) { err(String(e.message || e)); }
});

async function openProgram(pid) {
  err("");
  const p = await api("/api/programs/" + pid);
  S.curProg = p; S.curDest = null;
  $("progdetail").style.display = "";
  renderProgram();
}
function renderProgram() {
  const p = S.curProg;
  const dispNames = (p.displays || []).map((d) => d.name);
  let h = `<p class=hint>id ${esc(p.id)} · width ${esc(p.px_width)} · published v${esc(p.published_version || 0)}</p>`;
  h += `<div class=row><div style="flex:2"><label for=pd-notes>Publish notes</label><input id=pd-notes></div>
    <div style="align-self:end" class=row><button id=pd-publish class=primary>Publish new version</button></div></div>`;
  h += `<div id=pd-versions><b>Versions:</b> ` + (p.versions || []).map((v) => `v${v.version} (${esc(v.content_hash || "").slice(0, 8)}…)`.replace("…", "")).join(", ") + `</div>`;
  h += `<div class=row><div style="flex:1"><label>Upload to controllers (version defaults to published)</label><div id=pd-ctls>` +
    S.controllers.map((c) => `<label style="font-weight:normal"><input type=checkbox value="${c.id}"> ${esc(c.name)}</label>`).join("") +
    `</div></div><div style="flex:1;min-width:120px"><label for=pd-ver>Version</label><input id=pd-ver type=number value="${esc(p.published_version || 0)}"></div>
    <div style="align-self:end"><button id=pd-upload>Queue upload</button></div></div>`;
  h += `<h3>Services</h3><div class=row>
    <div style="flex:1"><label for=ns-num>Service number <span class=req>(required)</span></label><input id=ns-num></div>
    <div style="flex:2"><label for=ns-name>Service name</label><input id=ns-name></div>
    <div style="align-self:end"><button id=ns-add>Add service</button></div></div><div id=pd-services>`;
  for (const s of (p.services || [])) {
    h += `<div class=group><div class=grouptitle>Service ${esc(s.number)} ${esc(s.name || "")}</div>`;
    h += `<table><thead><tr><th>Code</th><th>Name</th><th>Via</th><th>Dest</th><th>Stops</th><th>Actions</th></tr></thead><tbody>`;
    for (const d of (s.destinations || [])) {
      h += `<tr><td class=kbd>${esc(d.code)}</td><td>${esc(d.name)}</td><td>${esc(d.via || "")}</td><td>${esc(d.dest || "")}</td>` +
        `<td>${(d.stops || []).length}</td><td class=row><button data-stops="${d.id}">Stops</button><button data-layout="${d.id}">Layouts</button></td></tr>`;
    }
    h += `</tbody></table><div class=row>
      <div style="flex:1"><label>Code (8 digits) for service ${esc(s.number)}</label><input data-ncode="${s.id}" maxlength=8 inputmode=numeric></div>
      <div style="flex:2"><label>Name</label><input data-nname="${s.id}"></div>
      <div style="flex:1"><label>Via</label><input data-nvia="${s.id}"></div>
      <div style="flex:1"><label>Dest</label><input data-ndest="${s.id}"></div>
      <div style="align-self:end"><button data-adddest="${s.id}">Add destination</button></div></div>
      <div class=err data-codeerr="${s.id}"></div></div>`;
  }
  h += `</div><div id=pd-edit></div>`;
  $("pd-body").innerHTML = h;
  // wire
  $("pd-publish").onclick = async () => {
    try {
      const r = await api(`/api/programs/${p.id}/publish`, { method: "POST", body: { notes: $("pd-notes").value } });
      err(""); alert(r.unchanged ? "No changes since last version." : `Published v${r.version} (${r.audio_count} clips, ${r.audio_bytes} bytes audio).`);
      openProgram(p.id); loadPrograms();
    } catch (e) { err(String(e.message || e)); }
  };
  $("pd-upload").onclick = async () => {
    try {
      const cids = [...document.querySelectorAll("#pd-ctls input:checked")].map((x) => x.value);
      const r = await api(`/api/programs/${p.id}/upload`, { method: "POST", body: { controllers: cids, version: parseInt($("pd-ver").value, 10) } });
      alert("Queued for " + r.queued.length + " controller(s).");
    } catch (e) { err(String(e.message || e)); }
  };
  $("ns-add").onclick = async () => {
    try {
      await api(`/api/programs/${p.id}/services`, { method: "POST", body: { number: $("ns-num").value, name: $("ns-name").value } });
      openProgram(p.id); loadPrograms();
    } catch (e) { err(String(e.message || e)); }
  };
  $("pd-body").querySelectorAll("[data-adddest]").forEach((b) => b.onclick = async () => {
    const sid = b.dataset.adddest;
    const q = (n) => $(`pd-body`).querySelector(`[data-n${n}="${sid}"]`).value;
    try {
      await api(`/api/services/${sid}/destinations`, { method: "POST",
        body: { code: document.querySelector(`[data-ncode="${sid}"]`).value, name: document.querySelector(`[data-nname="${sid}"]`).value,
                via: document.querySelector(`[data-nvia="${sid}"]`).value, dest: document.querySelector(`[data-ndest="${sid}"]`).value } });
      openProgram(p.id);
    } catch (e) { err(String(e.message || e)); }
  });
  // live code check
  $("pd-body").querySelectorAll("[data-ncode]").forEach((inp) => inp.addEventListener("input", async () => {
    const sid = inp.dataset.ncode;
    const box = document.querySelector(`[data-codeerr="${sid}"]`);
    const code = inp.value;
    if (!/^[0-9]{8}$/.test(code)) { box.textContent = code ? "Code must be exactly 8 digits 0-9." : ""; return; }
    try {
      const r = await api(`/api/programs/${p.id}/destinations/check-code?code=${code}`);
      box.textContent = r.unique ? "" : `Code ${code} is already used in this program.` + (r.suggestion ? ` Suggestion: ${r.suggestion}.` : "");
    } catch (e) { /* ignore */ }
  }));
  $("pd-body").querySelectorAll("[data-stops]").forEach((b) => b.onclick = () => editStops(b.dataset.stops, dispNames));
  $("pd-body").querySelectorAll("[data-layout]").forEach((b) => b.onclick = () => editLayout(b.dataset.layout, dispNames));
}
function findDest(did) {
  for (const s of (S.curProg.services || [])) for (const d of (s.destinations || [])) if (d.id === did) return { s, d };
  return null;
}
function editStops(did, dispNames) {
  const found = findDest(did); if (!found) return;
  const d = found.d;
  let h = `<h3>Stops — ${esc(d.name)} (${esc(d.code)})</h3><table><thead><tr><th>#</th><th>Stop name</th><th>Audio SHA</th><th>Board text</th><th>Override</th><th>Terminus</th><th></th></tr></thead><tbody id=stoprows>`;
  (d.stops || []).forEach((st, i) => {
    h += `<tr><td>${i + 1}</td>
      <td><input data-f=name value="${esc(st.name)}"></td>
      <td><input data-f=audio value="${esc(st.audio || "")}" size=12></td>
      <td><input data-f=board_text value="${esc(st.board_text || "")}"></td>
      <td><input data-f=board_override value="${esc(st.board_override || "")}"></td>
      <td><input data-f=is_terminus type=checkbox${st.is_terminus ? " checked" : ""}></td>
      <td><button data-delstop="${i}">✕</button></td></tr>`;
  });
  h += `</tbody></table><div class=row><button id=stop-add>Add stop</button><button id=stop-save class=primary>Save stops</button></div>
    <p class=hint>Terminus must be the last stop. Positions are assigned in order.</p>`;
  $("pd-edit").innerHTML = h;
  $("pd-edit").scrollIntoView();
  const collect = () => [...document.querySelectorAll("#stoprows tr")].map((tr) => {
    const g = (f) => tr.querySelector(`[data-f=${f}]`);
    return { name: g("name").value, audio: g("audio").value || null, board_text: g("board_text").value || null,
      board_override: g("board_override").value || null, is_terminus: g("is_terminus").checked };
  });
  $("pd-edit").querySelectorAll("[data-delstop]").forEach((b) => b.onclick = () => { b.closest("tr").remove(); });
  $("stop-add").onclick = () => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>new</td><td><input data-f=name></td><td><input data-f=audio size=12></td><td><input data-f=board_text></td><td><input data-f=board_override></td><td><input data-f=is_terminus type=checkbox></td><td></td>`;
    $("stoprows").appendChild(tr);
  };
  $("stop-save").onclick = async () => {
    try { await api(`/api/destinations/${did}/stops`, { method: "PUT", body: { stops: collect() } }); openProgram(S.curProg.id); }
    catch (e) { err(String(e.message || e)); }
  };
}
async function editLayout(did, dispNames) {
  const found = findDest(did); if (!found) return;
  const d = found.d;
  let h = `<h3>Layouts — ${esc(d.name)} (${esc(d.code)})</h3><div class=row>
    <div style="flex:1"><label for=lay-disp>Display</label><select id=lay-disp>${dispNames.map((n) => `<option>${esc(n)}</option>`).join("")}</select></div>
    <div style="align-self:end" class=row><button id=lay-load>Load</button><button id=lay-save class=primary>Save</button></div></div>
    <div class=hint id=lay-src></div>
    <div><label for=lay-json>Layout JSON (must match display size)</label><textarea id=lay-json rows=14></textarea></div>
    <div class=err id=lay-err></div>`;
  $("pd-edit").innerHTML = h;
  $("pd-edit").scrollIntoView();
  $("lay-load").onclick = async () => {
    try {
      const r = await api(`/api/destinations/${did}/layouts/${encodeURIComponent($("lay-disp").value)}`);
      $("lay-json").value = r.layout ? JSON.stringify(r.layout, null, 2) : "";
      $("lay-src").textContent = "Source: " + r.source + (r.layout ? "" : " (nothing saved yet — paste JSON and Save)");
    } catch (e) { $("lay-err").textContent = String(e.message || e); }
  };
  $("lay-save").onclick = async () => {
    try {
      const lj = JSON.parse($("lay-json").value);
      await api(`/api/destinations/${did}/layouts/${encodeURIComponent($("lay-disp").value)}`, { method: "PUT", body: { layout: lj } });
      $("lay-err").textContent = ""; alert("Layout saved.");
    } catch (e) { $("lay-err").textContent = String(e.message || e); }
  };
}

// ---------- codes ----------
$("btn-codes").addEventListener("click", async () => {
  try {
    const p = await api("/api/programs/" + $("codes-prog").value);
    const rows = [];
    for (const s of (p.services || [])) for (const d of (s.destinations || [])) rows.push({ code: d.code, svc: s.number, name: d.name });
    rows.sort((a, b) => (a.code < b.code ? -1 : 1));
    $("coderows").innerHTML = rows.map((r) => `<tr><td class=kbd>${esc(r.code)}</td><td>${esc(r.svc)}</td><td>${esc(r.name)}</td></tr>`).join("");
  } catch (e) { err(String(e.message || e)); }
});

// ---------- controllers ----------
async function loadControllers() {
  const r = await api("/api/controllers?organisation_id=" + encodeURIComponent(S.org));
  S.controllers = r.controllers || [];
  $("ctlrows").innerHTML = S.controllers.map((c) =>
    `<tr><td>${esc(c.name)}</td><td>${esc(c.status)}</td><td>${esc(c.last_seen_at || "—")}</td><td>${esc(c.app_version || "")}</td>
    <td class=row><button data-rename="${c.id}">Rename</button><button data-unpair="${c.id}">Unpair</button></td></tr>`).join("");
  $("rc-ctl").innerHTML = S.controllers.map((c) => `<option value="${c.id}">${esc(c.name)} (${esc(c.status)})</option>`).join("");
}
$("ctlrows").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  try {
    if (b.dataset.rename) {
      const c = S.controllers.find((x) => x.id === b.dataset.rename);
      const name = prompt("Controller name:", c.name);
      if (name) { await api(`/api/controllers/${c.id}`, { method: "PATCH", body: { name } }); loadControllers(); }
    }
    if (b.dataset.unpair && confirm("Unpair this controller? Its token is revoked.")) {
      await api(`/api/controllers/${b.dataset.unpair}`, { method: "DELETE" }); loadControllers();
    }
  } catch (ex) { err(String(ex.message || ex)); }
});
$("btn-pair").addEventListener("click", async () => {
  try {
    await api("/api/controllers/pair", { method: "POST",
      body: formData({ organisation_id: S.org, code: $("pair-code").value.trim().toUpperCase(), name: $("pair-name").value }) });
    $("pair-code").value = ""; $("pair-name").value = ""; loadControllers();
  } catch (e) { err(String(e.message || e)); }
});
$("btn-sim").addEventListener("click", async () => {
  try {
    const r = await api("/api/device/register", { method: "POST", body: {} });
    $("pair-code").value = r.pairing_code;
    alert("Simulated device registered. Pairing code filled in — enter a name and press Pair controller.");
  } catch (e) { err(String(e.message || e)); }
});

// ---------- audio & voices ----------
async function loadAudio() {
  const r = await api("/api/audio?organisation_id=" + encodeURIComponent(S.org));
  $("audiorows").innerHTML = (r.clips || []).map((a) =>
    `<tr><td class=kbd>${esc((a.sha256 || "").slice(0, 12))}…</td><td>${esc(a.filename)}</td><td>${esc(a.source)}</td></tr>`).join("");
  const v = await api("/api/voices?organisation_id=" + encodeURIComponent(S.org));
  $("voicerows").innerHTML = (v.voices || []).map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.language || "")}</td><td>${esc(x.sample_rate || "")}</td></tr>`).join("");
  $("gen-voice").innerHTML = `<option value="">(default)</option>` + (v.voices || []).map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
}
async function loadVoices() { /* folded into loadAudio */ }
$("btn-upload").addEventListener("click", async () => {
  try {
    const f = $("au-file").files[0]; if (!f) throw new Error("Choose a file first.");
    const fd = new FormData(); fd.append("organisation_id", S.org); fd.append("file", f);
    const r = await fetch(PREFIX + "/api/audio", { method: "POST", headers: apiHeaders(), body: fd }).then(async (x) => {
      if (!x.ok) throw new Error((await x.json()).detail || ("HTTP " + x.status)); return x.json();
    });
    alert("Uploaded, sha " + r.sha256.slice(0, 12) + "…"); $("au-file").value = ""; loadAudio();
  } catch (e) { err(String(e.message || e)); }
});
$("btn-generate").addEventListener("click", async () => {
  try {
    const r = await api("/api/audio/generate", { method: "POST", body: { organisation_id: S.org,
      text: $("gen-text").value, name: $("gen-name").value, voice_id: $("gen-voice").value || null,
      speed: parseFloat($("gen-speed").value || "1") } });
    alert("Generated, sha " + r.sha256.slice(0, 12) + "…"); loadAudio();
  } catch (e) { err(String(e.message || e)); }
});
$("btn-voice").addEventListener("click", async () => {
  try {
    const m = $("vv-model").files[0], c = $("vv-config").files[0];
    if (!m || !c) throw new Error("Choose both model and config files.");
    const fd = new FormData();
    fd.append("organisation_id", S.org); fd.append("name", $("vv-name").value);
    fd.append("model", m); fd.append("config", c);
    const r = await fetch(PREFIX + "/api/voices", { method: "POST", headers: apiHeaders(), body: fd }).then(async (x) => {
      if (!x.ok) throw new Error((await x.json()).detail || ("HTTP " + x.status)); return x.json();
    });
    alert("Voice uploaded."); loadAudio();
  } catch (e) { err(String(e.message || e)); }
});

// ---------- remote ----------
async function rcSend(body) {
  const cid = $("rc-ctl").value;
  if (!cid) throw new Error("Choose a controller first.");
  const r = await api(`/api/controllers/${cid}/command`, { method: "POST", body });
  $("rc-state").textContent = "Command queued at " + new Date().toLocaleTimeString() + " (applied when the device is online).";
  return r;
}
const rc = (id, fn) => $(id).addEventListener("click", () => fn().catch((e) => err(String(e.message || e))));
rc("btn-next", () => rcSend({ command: "stop_next" }));
rc("btn-prev", () => rcSend({ command: "stop_prev" }));
rc("btn-an", () => rcSend({ command: "announce_next" }));
rc("btn-aa", () => rcSend({ command: "announce_approaching" }));
rc("btn-ov", () => rcSend({ command: "override_apply", text: $("rc-ov").value }));
rc("btn-ovc", () => rcSend({ command: "override_clear" }));
rc("btn-vol", () => rcSend({ command: "volume", volume: parseInt($("rc-vol").value || "80", 10) }));
rc("btn-mute", () => rcSend({ command: "volume", muted: true }));
rc("btn-rc-code", () => rcSend({ command: "destination_code", code: $("rc-code").value }));
rc("btn-rc-prog", () => rcSend({ command: "program_select", program_id: $("rc-prog").value }));
$("btn-rc-refresh").addEventListener("click", async () => {
  await loadControllers();
  const c = S.controllers.find((x) => x.id === $("rc-ctl").value);
  $("rc-state").textContent = c ? `Status: ${c.status}, last seen ${c.last_seen_at || "never"}, app ${c.app_version || "?"}.` : "Select a controller.";
});

// ---------- boot ----------
$("btn-login").addEventListener("click", doLogin);
$("btn-signup").addEventListener("click", doSignup);
$("btn-logout").addEventListener("click", () => {
  S.token = ""; localStorage.removeItem("nsl_token"); showAuth(false);
});
$("orgsel").addEventListener("change", () => {
  S.org = $("orgsel").value; localStorage.setItem("nsl_org", S.org); loadAll();
});
document.querySelector('#tabs .tab').classList.add("on");
refreshMe();
