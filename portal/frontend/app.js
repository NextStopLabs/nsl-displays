/* NSL Portal app — talks to /api (same prefix as page URL). No placeholders; labels above inputs. */
"use strict";
const PREFIX = location.pathname.startsWith("/portal") ? "/portal" : "";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const S = { token: localStorage.getItem("nsl_token") || "", orgs: [], org: localStorage.getItem("nsl_org") || "",
  programs: [], controllers: [], curProg: null, curDest: null, clips: [] };

function slugifyClip(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/-{2,}/g, "-").replace(/^-|-$/g, "").slice(0, 64);
}
function clipLabel(a) { return (a && (a.name || a.filename)) || "(unnamed)"; }
function audioOptions(selected, { allowEmpty = true, emptyLabel = "No audio — silent" } = {}) {
  const opts = (S.clips || []).map((a) =>
    `<option value="${esc(a.sha256)}"${a.sha256 === selected ? " selected" : ""}>${esc(clipLabel(a))}</option>`).join("");
  return (allowEmpty ? `<option value="">${esc(emptyLabel)}</option>` : "") + opts;
}
async function playClip(sha) {
  try {
    const r = await fetch(PREFIX + `/api/audio/${encodeURIComponent(sha)}/file?organisation_id=${encodeURIComponent(S.org)}`,
      { headers: apiHeaders() });
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { msg = (await r.json()).detail || msg; } catch (e) { /* keep status */ }
      throw new Error(msg);
    }
    const blob = await r.blob();
    if (!blob.size) throw new Error("empty audio file — re-upload or regenerate this clip");
    const url = URL.createObjectURL(blob);
    const el = new Audio(url);
    el.onended = () => URL.revokeObjectURL(url);
    await el.play();
  } catch (e) { err("Play failed: " + String(e.message || e)); }
}
async function playSequence(shas) {
  for (const sha of (shas || [])) {
    const r = await fetch(PREFIX + `/api/audio/${encodeURIComponent(sha)}/file?organisation_id=${encodeURIComponent(S.org)}`,
      { headers: apiHeaders() });
    if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail || ("HTTP " + r.status));
    const blob = await r.blob();
    if (!blob.size) throw new Error("empty audio file in sequence");
    const url = URL.createObjectURL(blob);
    await new Promise((res, rej) => {
      const el = new Audio(url);
      el.onended = () => { URL.revokeObjectURL(url); res(); };
      el.onerror = (e) => { URL.revokeObjectURL(url); rej(new Error("audio playback failed")); };
      el.play().catch(rej);
    });
  }
}

// ---------- stop-by-stop preview (all screens + announcements) ----------
const Preview = (() => {
  let raf = 0, snap = null;
  const AMBER = "#FFB000";
  function stop() { if (raf) cancelAnimationFrame(raf); raf = 0; snap = null; }
  function autofit(w, h) {
    const nw = Math.max(16, Math.round(w * 70 / 240)), dh = Math.max(8, Math.round(h * 25 / 40));
    return { schema: 1, width: w, height: h, background: "#000000", page_seconds: 3,
      override: { mode: "replace", replaces: "dest", hide: [] },
      elements: [
        { type: "field", field: "num", x: 0, y: 0, w: nw, h: h, font: "han-1912B3E1", colour: AMBER,
          align: "centre", valign: "middle", scroll: "none", flash_ms: 0, page: 1, show_if: "always", scale: 2, spacing: 1 },
        { type: "field", field: "dest", x: nw, y: 0, w: w - nw, h: dh, font: "han-2423B3E1", colour: AMBER,
          align: "centre", valign: "middle", scroll: "if_too_long", flash_ms: 0, page: 1, show_if: "always", scale: 1, spacing: 1 },
        { type: "field", field: "via", x: nw, y: dh, w: w - nw, h: h - dh, font: "han-1610N2E1", colour: AMBER,
          align: "centre", valign: "middle", scroll: "if_too_long", flash_ms: 0, page: 1, show_if: "has_text", scale: 1, spacing: 1 } ] };
  }
  function drawOne(cv, lay, fields, ovText, t) {
    const H = (typeof LayoutEditor !== "undefined" && LayoutEditor.hanover) || null;
    const scale = parseInt(cv.dataset.scale || "3", 10);
    const off = document.createElement("canvas");
    off.width = lay.width; off.height = lay.height;
    const g = off.getContext("2d");
    g.fillStyle = lay.background || "#000000";
    g.fillRect(0, 0, off.width, off.height);
    const ovActive = !!ovText;
    const ov = lay.override || {};
    const pages = [...new Set((lay.elements || []).map((e) => parseInt(e.page || 1, 10)))].sort((a, b) => a - b);
    const page = pages.length <= 1 ? (pages[0] || 1) : pages[Math.floor(t / (parseFloat(lay.page_seconds) || 3)) % pages.length];
    const hide = new Set();
    if (ovActive) {
      if (ov.mode === "hide_others") {
        const keep = ov.replaces || "dest";
        for (const e of (lay.elements || [])) if (e.field !== keep) hide.add(e.field);
      } else hide.add(...(ov.hide || []));
    }
    const show = (el) => {
      const sh = el.show_if || "always";
      if (sh === "always") return true;
      if (sh === "has_text") return el.type === "field" ? !!fields[el.field] : !!el.text;
      if (sh === "override_active") return ovActive;
      if (sh === "override_inactive") return !ovActive;
      return true;
    };
    if (!(ov.mode === "full" && ovActive)) {
      for (const el of (lay.elements || [])) {
        if (parseInt(el.page || 1, 10) !== page) continue;
        if (el.field && hide.has(el.field)) continue;
        if (!show(el)) continue;
        if (el.flash_ms && (Math.floor(t * 1000) % (el.flash_ms * 2)) >= el.flash_ms) continue;
        let text = "";
        if (ovText && el.type === "field" && (ov.replaces || "dest") === el.field &&
            ["replace", "hide_others", "full"].includes(ov.mode || "replace")) text = ovText;
        else text = el.type === "text" ? (el.text || "") : (fields[el.field] || "");
        if (!text) continue;
        const han = H && H.isHan(el.font) ? H.get(el.font) : null;
        g.fillStyle = el.colour || AMBER;
        g.textBaseline = el.valign === "middle" ? "middle" : (el.valign === "bottom" ? "bottom" : "top");
        g.textAlign = el.align === "centre" || el.align === "center" ? "center" : (el.align === "right" ? "right" : "left");
        const bx = el.x, by = el.y, bw = el.w, bh = el.h;
        const ty = el.valign === "middle" ? by + bh / 2 : (el.valign === "bottom" ? by + bh : by);
        const sc = han ? Math.max(1, Math.min(8, parseInt(el.scale || 1, 10) || 1)) : 1;
        const hsp = han ? (() => { const v = parseInt(el.spacing || 0, 10); return isNaN(v) ? 0 : Math.max(-8, Math.min(16, v)); })() : 0;
        const tw1 = han ? H.width(han, text, hsp) : 0;
        const tw = han ? tw1 * sc : g.measureText(text).width;
        const needScroll = el.scroll === "always" || (el.scroll === "if_too_long" && tw > bw);
        const hanStrip = () => {
          const c1 = document.createElement("canvas");
          c1.width = Math.max(1, tw1); c1.height = han.height;
          H.draw(c1.getContext("2d"), han, text, 0, 0, el.colour || AMBER, hsp);
          if (sc <= 1) return c1;
          const c2 = document.createElement("canvas");
          c2.width = c1.width * sc; c2.height = c1.height * sc;
          const s2 = c2.getContext("2d");
          s2.imageSmoothingEnabled = false;
          s2.drawImage(c1, 0, 0, c1.width, c1.height, 0, 0, c2.width, c2.height);
          return c2;
        };
        // No clipping for static text (matches the bus): ink may overflow its
        // box and still shows. Only scrolling crops to the element.
        if (han && !needScroll) {
          const sh = han.height * sc;
          const y0 = el.valign === "middle" ? by + Math.floor((bh - sh) / 2)
            : (el.valign === "bottom" ? by + bh - sh : by);
          let tx = bx + 1;
          if (el.align === "centre" || el.align === "center") tx = bx + Math.floor((bw - tw) / 2);
          if (el.align === "right") tx = bx + bw - tw;
          g.drawImage(hanStrip(), tx, y0);
          continue;
        }
        if (!han && !needScroll) {
          g.font = "12px monospace";
          let tx = bx + 1;
          if (g.textAlign === "center") tx = bx + bw / 2;
          if (g.textAlign === "right") tx = bx + bw - 1;
          g.fillText(text, tx, ty);
          continue;
        }
        g.save();
        g.beginPath(); g.rect(bx, by, bw, bh); g.clip();
        if (han) {
          const sh = han.height * sc;
          const y0 = el.valign === "middle" ? by + Math.floor((bh - sh) / 2)
            : (el.valign === "bottom" ? by + bh - sh : by);
          const offx = Math.floor(t * 20) % Math.floor(tw + bw);
          const strip = hanStrip();
          const s2 = document.createElement("canvas");
          s2.width = Math.ceil(tw + bw); s2.height = bh;
          s2.getContext("2d").drawImage(strip, 0, Math.max(0, y0 - by));
          g.drawImage(s2, offx, 0, bw, bh, bx, by, bw, bh);
          g.restore();
          continue;
        }
        g.font = "12px monospace";
        let tx = bx + 1;
        if (g.textAlign === "center") tx = bx + bw / 2;
        if (g.textAlign === "right") tx = bx + bw - 1;
        if (needScroll) {
          const offx = Math.floor(t * 20) % Math.max(1, Math.floor(tw + bw));
          tx = g.textAlign === "center" ? bx + bw / 2 - offx + (tw + bw) / 2 - tw / 2
            : g.textAlign === "right" ? bx + bw - offx : bx - offx;
        }
        g.fillText(text, tx, ty);
        g.restore();
      }
    } else {
      const ovEl = (lay.elements || []).find((e) => e.field === (ov.replaces || "dest"));
      const ovHan = (H && ovEl && H.isHan(ovEl.font)) ? H.get(ovEl.font) : null;
      if (ovHan) {
        H.draw(g, ovHan, ovText, 2, Math.floor(lay.height / 2 - ovHan.height / 2), AMBER);
      } else {
        g.fillStyle = AMBER; g.font = `${Math.floor(lay.height / 2)}px monospace`;
        g.textBaseline = "middle"; g.textAlign = "left";
        g.fillText(ovText, 2, lay.height / 2);
      }
    }
    cv.width = lay.width * scale; cv.height = lay.height * scale;
    const p = cv.getContext("2d");
    p.imageSmoothingEnabled = false;
    p.drawImage(off, 0, 0, cv.width, cv.height);
    p.fillStyle = "rgba(0,0,0,.45)";
    for (let x = 0; x < cv.width; x += scale) p.fillRect(x, 0, 1, cv.height);
    for (let y = 0; y < cv.height; y += scale) p.fillRect(0, y, cv.width, 1);
  }
  function loop(t) {
    if (!snap) return;
    const secs = t / 1000;
    for (const disp of (snap.displays || [])) {
      const cv = document.getElementById("pv-cv-" + disp.name);
      if (!cv) continue;
      const entry = (snap.layouts || {})[disp.name] || {};
      drawOne(cv, entry.layout || autofit(disp.width_px, disp.height_px), snap.fields || {}, snap.board_override || "", secs);
    }
    raf = requestAnimationFrame(loop);
  }
  function start(s) {
    stop();
    snap = s;
    raf = requestAnimationFrame(loop);
  }
  return { start, stop };
})();

function err(msg) { $("err").textContent = msg || ""; }
function apiHeaders(json) {
  const h = { Authorization: "Bearer " + S.token };
  if (json) h["Content-Type"] = "application/json";
  return h;
}
async function api(path, opts) {
  opts = opts || {};
  const headers = Object.assign({ Authorization: "Bearer " + S.token }, opts.headers || {});
  let body = opts.body;
  if (body && typeof body === "object" && !(body instanceof FormData)) {
    body = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const r = await fetch(PREFIX + path, { method: opts.method || "GET", headers, body });
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) throw new Error((data && data.detail) || ("HTTP " + r.status));
  return data;
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
function showAuthPage(which) {
  $("auth-signin").style.display = which === "signin" ? "" : "none";
  $("auth-signup").style.display = which === "signup" ? "" : "none";
}
async function doLogin() {
  $("si-err").textContent = "";
  if (!$("si-email").value.trim() || !$("si-pass").value) { $("si-err").textContent = "Enter your email and password."; return; }
  try {
    const r = await api("/api/auth/login", { method: "POST", body: formData({ email: $("si-email").value, password: $("si-pass").value }) });
    S.token = r.token; localStorage.setItem("nsl_token", S.token);
    await refreshMe();
  } catch (e) { $("si-err").textContent = String(e.message || e); }
}
async function doCreate() {
  $("su-err").textContent = "";
  if (!$("su-org").value.trim()) { $("su-err").textContent = "Organisation name is required."; return; }
  if (!$("su-email").value.trim()) { $("su-err").textContent = "Email is required."; return; }
  if (($("su-pass").value || "").length < 8) { $("su-err").textContent = "Password must be at least 8 characters."; return; }
  if ($("su-pass").value !== $("su-pass2").value) { $("su-err").textContent = "Passwords do not match."; return; }
  try {
    const r = await api("/api/auth/signup", { method: "POST", body: formData({
      email: $("su-email").value, password: $("su-pass").value,
      name: $("su-name").value, org: $("su-org").value }) });
    S.token = r.token; localStorage.setItem("nsl_token", S.token);
    await refreshMe();
  } catch (e) { $("su-err").textContent = String(e.message || e); }
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
      a.href = URL.createObjectURL(blob); a.download = `${p.name}-v${p.published_version}.nslpack`;
      document.body.appendChild(a); a.click(); a.remove();
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
  LayoutEditor.stop();
  Preview.stop();
  const p = S.curProg;
  const flat = [];
  for (const s of (p.services || [])) for (const d of (s.destinations || [])) flat.push({ s, d });
  flat.sort((a, b) => (a.d.code < b.d.code ? -1 : 1));
  const dispName = (d, s) => [d.num || s.number, d.via, d.dest].filter(Boolean).join(" · ") || d.name;

  let h = `<p class=hint>${esc(p.name)} · width ${esc(p.px_width)} · published v${esc(p.published_version || 0)}</p>`;
  h += `<div class=row><div style="flex:2"><label for=pd-notes>Publish notes</label><input id=pd-notes></div>
    <div style="align-self:end" class=row><button id=pd-publish class=primary>Publish new version</button></div></div>`;
  h += `<div id=pd-versions><b>Versions:</b> ` + (p.versions || []).map((v) => `v${v.version} (${esc(v.content_hash || "").slice(0, 8)}…)`.replace("…", "")).join(", ") + `</div>`;
  h += `<details id=up-details><summary>Upload to controllers</summary><div class=row><div style="flex:1"><label>Controllers</label><div id=pd-ctls>` +
    S.controllers.map((c) => {
      const act = (c.programs || []).find((x) => x.active_version != null);
      const run = act ? ` · running ${esc(act.name || act.program_id)} v${act.active_version}` : "";
      const ghost = !c.last_seen_at ? ` <span class=err>never checked in — probably a duplicate row, delete it in the Controllers tab</span>` : "";
      return `<div class=row data-ctlrow="${c.id}" style="align-items:center"><label style="font-weight:normal;flex:1"><input type=checkbox value="${c.id}"> ${esc(c.name)} (${c.online ? "online" : "offline"}${run})${ghost}</label>` +
        `<span class=hint data-upstat="${c.id}"></span>` +
        `<button data-up-latest="${c.id}" title="Publish (if changed) and upload the latest version to ${esc(c.name)}">Send latest</button></div>`;
    }).join("") +
    `</div></div></div><div class=row><div style="flex:1;min-width:120px"><label for=pd-ver>Advanced: exact version</label><input id=pd-ver type=number value="${esc(p.published_version || 0)}"></div>
    <div style="align-self:end"><button id=pd-upload>Queue exact version to ticked</button></div></div></details>`;

  // ---- HELEN-style destination list: one table, one add form ----
  h += `<h3>Destinations (${flat.length})</h3>`;
  h += `<div class=row><div style="flex:1"><label for=dest-filter>Filter</label><input id=dest-filter></div></div>`;
  h += `<table><thead><tr><th>Code</th><th>Service</th><th>Destination</th><th>Stops</th><th>Actions</th></tr></thead><tbody id=destrows>`;
  for (const { s, d } of flat) {
    h += `<tr data-code="${esc(d.code)}" data-name="${esc((d.name + " " + (d.via || "") + " " + (d.dest || "")).toLowerCase())}">` +
      `<td class=kbd>${esc(d.code)}</td><td>${esc(s.number)}</td><td>${esc(dispName(d, s))}</td>` +
      `<td>${(d.stops || []).length}</td>` +
      `<td class=row><button data-edit="${d.id}">Edit</button><button data-stops="${d.id}">Stops</button><button data-layout="${d.id}">Display</button><button data-del="${d.id}">Delete</button></td></tr>`;
  }
  h += `</tbody></table>`;
  h += `<div class=group><div class=grouptitle>New destination</div><div class=row>
    <div style="flex:1;min-width:140px"><label for=nd-service>Service</label><select id=nd-service>` +
    (p.services || []).map((s) => `<option value="${s.id}">${esc(s.number)}${s.name ? " — " + esc(s.name) : ""}</option>`).join("") +
    `<option value="__new__">New service…</option></select></div>
    <div style="flex:1;min-width:120px"><label for=nd-code>Code (8 digits)</label><input id=nd-code maxlength=8 inputmode=numeric></div>
    <div style="align-self:end" class=row><button id=nd-suggest type=button>Suggest</button></div></div>
    <div class=row id=nd-newsvc style="display:none">
      <div style="flex:1"><label for=nd-newsvc-num>New service number <span class=req>(required)</span></label><input id=nd-newsvc-num></div>
      <div style="flex:2"><label for=nd-newsvc-name>New service name</label><input id=nd-newsvc-name></div></div>
    <div class=row>
      <div style="flex:2"><label for=nd-name>Name <span class=req>(required)</span></label><input id=nd-name></div>
      <div style="flex:1"><label for=nd-via>Via</label><input id=nd-via></div>
      <div style="flex:1"><label for=nd-dest>Dest</label><input id=nd-dest></div>
      <div style="align-self:end"><button id=nd-add class=primary>Add destination</button></div></div>
    <div class=err id=nd-err></div></div>`;

  // ---- bustimes.org import ----
  h += `<div class=group><div class=grouptitle>Import from bustimes.org</div>
    <p class=hint>Live lookup at bustimes.org. Typical destinations come from the description split on " - "
    (one part for circulars like "Lichfield City Circular", more for "Tamworth - Lichfield - St. Francis of Assisi").
    Stops come from the service timetable; stop audio is generated with Piper when a voice is picked.</p>
    <div class=row>
    <div style="flex:1;min-width:120px"><label for=bt-operator>Operator code</label><input id=bt-operator value="BULI"></div>
    <div style="flex:2;min-width:160px"><label for=bt-search>Search (number or place)</label><input id=bt-search></div>
    <div style="align-self:end"><button id=bt-find>Search bustimes.org</button></div></div>
    <div class=err id=bt-err></div><div id=bt-results></div><div id=bt-detail></div></div>`;

  // ---- services tucked away: rename / announcements / delete ----
  h += `<details id=svc-details><summary>Services (${(p.services || []).length}) — announcements &amp; rename</summary>`;
  h += `<div class=row>
    <div style="flex:1"><label for=ns-num>Service number <span class=req>(required)</span></label><input id=ns-num></div>
    <div style="flex:2"><label for=ns-name>Service name</label><input id=ns-name></div>
    <div style="align-self:end"><button id=ns-add>Add service</button></div></div><div id=pd-services>`;
  for (const s of (p.services || [])) {
    h += `<div class=group><div class=grouptitle>Service ${esc(s.number)} ${esc(s.name || "")}</div>`;
    h += `<div class=row>
      <div style="flex:1"><label>Number</label><input data-svc-num="${s.id}" value="${esc(s.number)}"></div>
      <div style="flex:2"><label>Name</label><input data-svc-name="${s.id}" value="${esc(s.name || "")}"></div></div>`;
    h += `<div class=row>
      <div style="flex:1"><label>Next-stop announcement</label><select data-svc-audio="next_stop_audio" data-svc="${s.id}">${audioOptions(s.next_stop_audio, { emptyLabel: "Default — the next stop is…" })}</select></div>
      <div style="flex:1"><label>Approaching announcement</label><select data-svc-audio="approaching_audio" data-svc="${s.id}">${audioOptions(s.approaching_audio, { emptyLabel: "Default — approaching…" })}</select></div>
      <div style="flex:1"><label>Terminus suffix</label><select data-svc-audio="last_stop_audio" data-svc="${s.id}">${audioOptions(s.last_stop_audio, { emptyLabel: "Default — …where this service terminates" })}</select></div>
      <div style="flex:1;min-width:140px"><label>Terminus suffix plays on</label><select data-svc-mode="${s.id}">
        <option value="both"${(s.terminus_clip_mode || "both") === "both" ? " selected" : ""}>Next stop + approaching</option>
        <option value="next"${s.terminus_clip_mode === "next" ? " selected" : ""}>Next stop only</option>
        <option value="approaching"${s.terminus_clip_mode === "approaching" ? " selected" : ""}>Approaching only</option>
      </select></div></div>`;
    h += `<div class=row><button data-svc-save="${s.id}">Save service</button><button data-svc-del="${s.id}">Delete service</button></div>`;
    h += `<div class=err data-svc-err="${s.id}"></div></div>`;
  }
  h += `</div></details>`;

  // ---- preview: all screens stop-by-stop + announcements ----
  h += `<div class=group><div class=grouptitle>Preview — screens + announcements (stop by stop)</div>`;
  h += `<div class=row><div style="flex:2;min-width:200px"><label for=pv-dest>Destination</label><select id=pv-dest>` +
    flat.map(({ s, d }) => `<option value="${d.id}">${esc(d.code)} · ${esc(s.number)} · ${esc(dispName(d, s))}</option>`).join("") +
    `</select></div><div style="flex:1;min-width:140px"><label for=pv-pos>Stop</label><div class=row><button id=pv-first>|◀</button><button id=pv-prev>◀</button><button id=pv-next>▶</button></div></div></div>`;
  h += `<div id=pv-meta class=hint></div><div id=pv-screens class=row style="align-items:flex-start"></div>`;
  h += `<div class=row><div style="flex:1"><label>Next-stop announcement</label><div id=pv-next-list class=row></div><div class=row><button id=pv-play-next class=primary>Play next-stop</button></div></div>`;
  h += `<div style="flex:1"><label>Approaching announcement</label><div id=pv-appr-list class=row></div><div class=row><button id=pv-play-appr class=primary>Play approaching</button></div></div></div>`;
  h += `<div class=err id=pv-err></div></div>`;

  // ---- HELEN "Enter Route Code and Name" dialog ----
  h += `<dialog id=dest-dialog><form method=dialog>
    <h3 style="margin-top:0">Edit destination</h3>
    <div class=row><div style="flex:1"><label for=ed-code>Code (8 digits) <span class=req>(required)</span></label><input id=ed-code maxlength=8 inputmode=numeric></div>
    <div style="flex:1"><label for=ed-service>Service</label><select id=ed-service></select></div></div>
    <div><label for=ed-name>Name <span class=req>(required)</span></label><input id=ed-name></div>
    <div class=row><div style="flex:1"><label for=ed-via>Via</label><input id=ed-via></div>
    <div style="flex:1"><label for=ed-dest>Dest</label><input id=ed-dest></div></div>
    <div class=err id=ed-err></div>
    <div class=row><button id=ed-save class=primary value=default>Save</button><button id=ed-close value=cancel>Cancel</button></div>
    </form></dialog>`;

  h += `<div id=pd-edit></div>`;
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
  $("pd-ctls").querySelectorAll("[data-up-latest]").forEach((b) => b.onclick = async () => {
    try {
      const r = await api(`/api/programs/${p.id}/publish-upload`, { method: "POST",
        body: { controllers: [b.dataset.upLatest], notes: $("pd-notes").value } });
      err("");
      alert(r.unchanged ? `No changes — v${r.version} re-queued for 1 controller.` : `Published v${r.version}, queued for 1 controller.`);
      loadPrograms(); refreshUploadStatus();
    } catch (e) { err(String(e.message || e)); }
  });
  if (window._upTimer) clearInterval(window._upTimer);
  window._upTimer = setInterval(() => { if ($("pd-ctls")) refreshUploadStatus(); }, 5000);
  refreshUploadStatus();
function upStatusText(c, pid) {
  const row = ((c.programs || []).find((x) => x.program_id === pid)) || null;
  if (!row || row.desired_version == null) return "not uploaded";
  const inst = row.installed_version, des = row.desired_version, act = row.active_version;
  const sync = c.sync_state;
  if (sync && sync.phase && sync.phase !== "idle")
    return (sync.phase === "audio" && sync.total ? `fetching audio ${sync.done || ""}/${sync.total}…`
      : sync.phase === "bundle" ? `downloading v${sync.version || ""}…`
      : sync.phase === "installing" ? "installing…" : `${sync.phase}…`);
  if (inst == null) return c.online ? `queued v${des} — bus downloading…` : `queued v${des} — waiting for bus`;
  if (inst !== des) return `installing v${des}… (bus has v${inst})`;
  if (act == null || act !== des) return `✓ installed v${des}` + (act != null ? ` — bus parked on v${act}, select a destination to switch` : " — bus will adopt it");
  return `✓ live on v${des}`;
}
async function refreshUploadStatus() {
  try {
    const r = await api("/api/controllers?organisation_id=" + encodeURIComponent(S.org));
    S.controllers = r.controllers || [];
    const pid = S.curProg && S.curProg.id;
    for (const el of document.querySelectorAll("[data-upstat]")) {
      const c = S.controllers.find((x) => x.id === el.dataset.upstat);
      if (c && pid) el.textContent = upStatusText(c, pid);
    }
  } catch (e) { /* keep old text on failure */ }
}
  $("dest-filter").addEventListener("input", () => {
    const q = $("dest-filter").value.trim().toLowerCase();
    document.querySelectorAll("#destrows tr").forEach((tr) => {
      tr.style.display = !q || (tr.dataset.code + " " + tr.dataset.name).includes(q) ? "" : "none";
    });
  });
  // new destination: single form for the whole program
  const svcSel = $("nd-service");
  const syncNewSvc = () => { $("nd-newsvc").style.display = svcSel.value === "__new__" ? "" : "none"; };
  svcSel.addEventListener("change", syncNewSvc); syncNewSvc();
  const checkNewCode = async () => {
    const code = $("nd-code").value.trim();
    const box = $("nd-err");
    if (!code) { box.textContent = ""; return null; }
    if (!/^[0-9]{8}$/.test(code)) { box.textContent = "Code must be exactly 8 digits 0-9."; return null; }
    try {
      const r = await api(`/api/programs/${p.id}/destinations/check-code?code=${code}`);
      box.textContent = r.unique ? "" : `Code ${code} is already used in this program.` + (r.suggestion ? ` Suggestion: ${r.suggestion}.` : "");
      return r;
    } catch (e) { return null; }
  };
  $("nd-code").addEventListener("input", checkNewCode);
  $("nd-suggest").onclick = async () => {
    try {
      const r = await api(`/api/programs/${p.id}/destinations/check-code?code=00000000`);
      if (r.suggestion) { $("nd-code").value = r.suggestion; checkNewCode(); }
    } catch (e) { err(String(e.message || e)); }
  };
  $("nd-add").onclick = async () => {
    try {
      let sid = svcSel.value;
      if (sid === "__new__") {
        const num = $("nd-newsvc-num").value.trim(), nm = $("nd-newsvc-name").value.trim();
        if (!num) throw new Error("Enter the new service number first.");
        sid = (await api(`/api/programs/${p.id}/services`, { method: "POST", body: { number: num, name: nm } })).service_id;
      }
      if (!sid) throw new Error("Create a service first (Services section below).");
      await api(`/api/services/${sid}/destinations`, { method: "POST",
        body: { code: $("nd-code").value.trim(), name: $("nd-name").value.trim() || $("nd-code").value.trim(),
                via: $("nd-via").value.trim(), dest: $("nd-dest").value.trim() } });
      openProgram(p.id);
    } catch (e) { $("nd-err").textContent = String(e.message || e); }
  };
  $("ns-add").onclick = async () => {
    try {
      await api(`/api/programs/${p.id}/services`, { method: "POST", body: { number: $("ns-num").value, name: $("ns-name").value } });
      openProgram(p.id); loadPrograms();
    } catch (e) { err(String(e.message || e)); }
  };
  // bustimes.org import
  wireBustimesImport(p);
  // row actions: edit dialog / stops / display / delete
  const dlg = $("dest-dialog");
  const openEdit = (did) => {
    const found = findDest(did); if (!found) return;
    $("ed-service").innerHTML = (p.services || []).map((s) =>
      `<option value="${s.id}"${s.id === found.s.id ? " selected" : ""}>${esc(s.number)}${s.name ? " — " + esc(s.name) : ""}</option>`).join("");
    $("ed-code").value = found.d.code; $("ed-name").value = found.d.name;
    $("ed-via").value = found.d.via || ""; $("ed-dest").value = found.d.dest || "";
    $("ed-err").textContent = ""; dlg.dataset.did = did;
    (dlg.showModal ? dlg.showModal() : null);
  };
  $("ed-close").onclick = () => { };
  $("ed-save").onclick = async (e) => {
    e.preventDefault();
    const did = dlg.dataset.did;
    try {
      await api(`/api/destinations/${did}`, { method: "PUT", body: {
        code: $("ed-code").value.trim(), service_id: $("ed-service").value,
        name: $("ed-name").value.trim(), via: $("ed-via").value.trim(), dest: $("ed-dest").value.trim() } });
      dlg.close(); openProgram(p.id);
    } catch (ex) { $("ed-err").textContent = String(ex.message || ex); }
  };
  $("pd-body").querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => { LayoutEditor.stop(); openEdit(b.dataset.edit); });
  $("pd-body").querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    const found = findDest(b.dataset.del); if (!found) return;
    if (!confirm(`Delete destination ${found.d.code} ${found.d.name}? Its stops and layouts go too.`)) return;
    try { await api(`/api/destinations/${b.dataset.del}`, { method: "DELETE" }); openProgram(p.id); }
    catch (e) { err(String(e.message || e)); }
  });
  $("pd-body").querySelectorAll("[data-stops]").forEach((b) => b.onclick = () => { LayoutEditor.stop(); editStops(b.dataset.stops); });
  $("pd-body").querySelectorAll("[data-layout]").forEach((b) => b.onclick = () => editLayout(b.dataset.layout));
  $("pd-body").querySelectorAll("[data-svc-save]").forEach((b) => b.onclick = async () => {
    const sid = b.dataset.svcSave;
    const get = (k) => document.querySelector(`[data-svc-audio="${k}"][data-svc="${sid}"]`).value || null;
    try {
      await api(`/api/services/${sid}`, { method: "PUT", body: {
        number: document.querySelector(`[data-svc-num="${sid}"]`).value,
        name: document.querySelector(`[data-svc-name="${sid}"]`).value,
        next_stop_audio: get("next_stop_audio"), approaching_audio: get("approaching_audio"),
        last_stop_audio: get("last_stop_audio"),
        terminus_clip_mode: document.querySelector(`[data-svc-mode="${sid}"]`).value } });
      err(""); openProgram(p.id);
    } catch (e) { document.querySelector(`[data-svc-err="${sid}"]`).textContent = String(e.message || e); }
  });
  $("pd-body").querySelectorAll("[data-svc-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this service and ALL its destinations, stops and layouts?")) return;
    try { await api(`/api/services/${b.dataset.svcDel}`, { method: "DELETE" }); openProgram(p.id); }
    catch (e) { err(String(e.message || e)); }
  });
  // preview wiring
  Preview.stop();
  const pvErr = (m) => { $("pv-err").textContent = m || ""; };
  async function loadPreview() {
    const did = $("pv-dest").value;
    if (!did) return;
    const cur = Preview._pos && Preview._did === did ? Preview._pos : 1;
    try {
      pvErr("");
      const s = await api(`/api/destinations/${did}/preview?pos=${cur}`);
      Preview._did = did; Preview._pos = s.pos;
      $("pv-meta").textContent = s.count
        ? `Stop ${s.pos}/${s.count}: ${s.stop.name}${s.stop.is_terminus ? " (terminus)" : ""} · board: "${s.fields.__board_text}"${s.board_override ? ` · override: "${s.board_override}"` : ""}`
        : "No stops yet — add stops to preview.";
      $("pv-screens").innerHTML = (s.displays || []).map((dp) => {
        const e = (s.layouts || {})[dp.name] || {};
        return `<div style="flex:1;min-width:180px"><label>${esc(dp.name)} ${esc(dp.width_px)}×${esc(dp.height_px)} <span class=hint>${esc(e.source || "")}</span></label><br><canvas id="pv-cv-${esc(dp.name)}" data-scale="3" style="border:1px solid #444;max-width:100%"></canvas></div>`;
      }).join("") || `<span class=hint>No displays in this program.</span>`;
      const seqHtml = (seq) => seq.length
        ? seq.map((c) => `<button data-pv-clip="${esc(c.sha)}" title="${esc(c.kind)}">${esc(c.label)}</button>`).join("")
        : `<span class=hint>No clips — silent.</span>`;
      $("pv-next-list").innerHTML = seqHtml(s.next || []);
      $("pv-appr-list").innerHTML = seqHtml(s.approaching || []);
      $("pv-next-list").querySelectorAll("[data-pv-clip]").forEach((b) => b.onclick = () => playClip(b.dataset.pvClip));
      $("pv-appr-list").querySelectorAll("[data-pv-clip]").forEach((b) => b.onclick = () => playClip(b.dataset.pvClip));
      $("pv-play-next").onclick = async () => {
        try { pvErr(""); await playSequence((s.next || []).map((c) => c.sha)); }
        catch (e) { pvErr(String(e.message || e)); }
      };
      $("pv-play-appr").onclick = async () => {
        try { pvErr(""); await playSequence((s.approaching || []).map((c) => c.sha)); }
        catch (e) { pvErr(String(e.message || e)); }
      };
      Preview.start(s);
    } catch (e) { pvErr(String(e.message || e)); }
  }
  $("pv-dest").addEventListener("change", () => { Preview._pos = 1; loadPreview(); });
  $("pv-first").onclick = () => { Preview._pos = 1; loadPreview(); };
  $("pv-prev").onclick = () => { Preview._pos = Math.max(1, (Preview._pos || 1) - 1); loadPreview(); };
  $("pv-next").onclick = () => { Preview._pos = (Preview._pos || 1) + 1; loadPreview(); };
  if ($("pv-dest").value) { Preview._did = $("pv-dest").value; Preview._pos = 1; loadPreview(); }
}
function findDest(did) {
  for (const s of (S.curProg.services || [])) for (const d of (s.destinations || [])) if (d.id === did) return { s, d };
  return null;
}
function editStops(did) {
  const found = findDest(did); if (!found) return;
  const d = found.d;
  let h = `<h3>Stops — ${esc(d.name)} (${esc(d.code)})</h3><table><thead><tr><th>#</th><th>Stop name</th><th>Audio clip</th><th>Board text</th><th>Override</th><th>Terminus</th><th></th></tr></thead><tbody id=stoprows>`;
  (d.stops || []).forEach((st, i) => {
    h += `<tr><td>${i + 1}</td>
      <td><input data-f=name value="${esc(st.name)}"></td>
      <td><select data-f=audio>${audioOptions(st.audio || "")}</select></td>
      <td><input data-f=board_text value="${esc(st.board_text || "")}"></td>
      <td><input data-f=board_override value="${esc(st.board_override || "")}"></td>
      <td><input data-f=is_terminus type=checkbox${st.is_terminus ? " checked" : ""}></td>
      <td><button data-delstop="${i}">✕</button></td></tr>`;
  });
  h += `</tbody></table><div class=row><button id=stop-add>Add stop</button><button id=stop-save class=primary>Save stops</button></div>
    <p class=hint>Pick a clip by name for each stop, or leave it silent. Terminus must be the last stop. Positions are assigned in order.</p>`;
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
    const sel = document.createElement("select"); sel.setAttribute("data-f", "audio"); sel.innerHTML = audioOptions("");
    tr.innerHTML = `<td>new</td><td><input data-f=name></td><td></td><td><input data-f=board_text></td><td><input data-f=board_override></td><td><input data-f=is_terminus type=checkbox></td><td></td>`;
    tr.children[2].appendChild(sel);
    $("stoprows").appendChild(tr);
  };
  $("stop-save").onclick = async () => {
    try { await api(`/api/destinations/${did}/stops`, { method: "PUT", body: { stops: collect() } }); openProgram(S.curProg.id); }
    catch (e) { err(String(e.message || e)); }
  };
}
async function editLayout(did) {
  const found = findDest(did); if (!found) return;
  LayoutEditor.stop();
  await LayoutEditor.open($("pd-edit"), {
    did, dest: found.d, serviceNumber: (found.s && found.s.number) || "",
    displays: S.curProg.displays || [], apiFn: api, errFn: err,
  });
}

// ---------- bustimes.org import ----------
function bustimesVoiceOptions(selected) {
  const vs = S.voices || [];
  return `<option value="">No voice — stops stay silent</option>` + vs.map((x) =>
    `<option value="${esc(x.id)}"${x.id === selected ? " selected" : ""}>${esc(x.name)}</option>`).join("");
}
function wireBustimesImport(p) {
  const box = $("bt-results"), det = $("bt-detail"), ebox = $("bt-err");
  if (!$("bt-find")) return;
  $("bt-find").onclick = async () => {
    const btn = $("bt-find");
    try {
      btn.disabled = true; btn.textContent = "Searching…";
      ebox.textContent = ""; box.innerHTML = `<span class=hint>Searching bustimes.org…</span>`; det.innerHTML = "";
      const r = await api("/api/bustimes/services?operator=" + encodeURIComponent($("bt-operator").value.trim()) +
        "&search=" + encodeURIComponent($("bt-search").value.trim()));
      const rows = r.results || [];
      box.innerHTML = rows.length ? `<table><thead><tr><th>Service</th><th>Description</th><th>Typical destinations</th><th></th></tr></thead><tbody>` +
        rows.map((s) => `<tr><td><b>${esc(s.line_name)}</b></td><td>${esc(s.description)}</td>` +
          `<td>${(s.destinations || []).map(esc).join(" · ") || "—"}</td>` +
          `<td><button data-bt="${s.id}">Import…</button></td></tr>`).join("") + `</tbody></table>
        <p class=hint>${esc(r.count)} service(s). Typical destinations are the description split on " - ".</p>`
        : `<p class=hint>No services found for that operator/search.</p>`;
      box.querySelectorAll("[data-bt]").forEach((b) => b.onclick = () => showBustimesDetail(p, b.dataset.bt));
    } catch (e) { ebox.textContent = String(e.message || e); box.innerHTML = ""; }
    finally { btn.disabled = false; btn.textContent = "Search bustimes.org"; }
  };
}
async function showBustimesDetail(p, bustimesId) {
  const det = $("bt-detail"), ebox = $("bt-err");
  try {
    ebox.textContent = ""; det.innerHTML = `<span class=hint>Loading timetable…</span>`;
    const d = await api(`/api/bustimes/services/${encodeURIComponent(bustimesId)}`);
    const dirOpts = (sel) => (d.directions || []).map((g) =>
      `<option value="${g.index}"${String(g.index) === String(sel) ? " selected" : ""}>` +
      `${esc(g.from)} → ${esc(g.to)} (${g.count} stops)</option>`).join("");
    const destRows = (d.suggested && d.suggested.length ? d.suggested : [{ name: d.description, direction: 0 }])
      .map((s, i) => `<tr><td><input data-btd-name="${i}" value="${esc(s.name)}"></td>
        <td><input data-btd-code="${i}" maxlength=8 inputmode=numeric placeholder="auto"></td>
        <td><input data-btd-via="${i}" placeholder="via"></td>
        <td><input data-btd-dest="${i}" value="${esc(s.name)}"></td>
        <td><select data-btd-dir="${i}"><option value="">No stops</option>${dirOpts(s.direction)}</select></td></tr>`).join("");
    det.innerHTML = `<div class=group><div class=grouptitle>Import ${esc(d.line_name)} — ${esc(d.description)}</div>
      <div class=row>
      <div style="flex:1"><label for=bt-num>Service number</label><input id=bt-num value="${esc(d.line_name)}"></div>
      <div style="flex:2"><label for=bt-name>Service name</label><input id=bt-name value="${esc(d.description)}"></div></div>
      <div class=row>
      <div style="flex:1"><label for=bt-voice>Voice for stop audio</label><select id=bt-voice>${bustimesVoiceOptions(localStorage.getItem("nsl_voice") || "")}</select></div>
      <div style="flex:1"><label for=bt-gen>Stop audio</label><select id=bt-gen><option value="yes">Generate missing stop audio</option><option value="no">Skip audio (silent stops)</option></select></div></div>
      <p class=hint>${(d.directions || []).map((g) => `Direction ${g.index + 1}: ${esc(g.from)} → ${esc(g.to)} — ${g.count} stops`).join("<br>") || "No timetable directions."}</p>
      <table><thead><tr><th>Destination name</th><th>Code</th><th>Via</th><th>Dest board</th><th>Timetable stops</th></tr></thead>
      <tbody>${destRows}</tbody></table>
      <div class=row><button id=bt-confirm class=primary>Import service</button></div>
      <div id=bt-progress></div>
      <div id=bt-result></div>
      <div class=err id=bt-import-err></div></div>`;
    const btBar = (done, total, stage) => {
      const pct = total ? Math.round(100 * done / total) : 0;
      return `<p class=hint>${esc(stage || "working…")} — ${done}/${total}</p>` +
        `<div style="border:1px solid #888;background:#fff;height:16px;width:100%">` +
        `<div style="background:#1a5fb4;height:100%;width:${pct}%"></div></div>`;
    };
    $("bt-confirm").onclick = async () => {
      const btn = $("bt-confirm"), prog = $("bt-progress"), res = $("bt-result"), ierr = $("bt-import-err");
      const t0 = Date.now();
      const elapsed = () => `${Math.round((Date.now() - t0) / 1000)}s`;
      let tick = 0;
      const confirmHtml = (r, audio) => {
        const destLines = (r.destinations || []).map((x) =>
          `<li><span class=kbd>${esc(x.code)}</span> ${esc(x.name)} — ${x.stops} stop(s)</li>`).join("");
        const warns = (r.warnings || []).filter(Boolean);
        return `<div class=group><div class=grouptitle>Import complete</div>` +
          `<p>Service <b>${esc(r.service_number)}</b>: ${r.destinations.length} destination(s) imported.</p>` +
          `<ul>${destLines}</ul>` +
          `<p>Stop audio: <b>${audio.generated != null ? audio.generated + " generated" : (audio.with_audio || 0) + " with audio"}</b>` +
          `${audio.reused != null ? `, ${audio.reused} reused` : ""}` +
          `${(audio.silent || []).length ? `, <span class=err>${audio.silent.length} silent: ${esc(audio.silent.slice(0, 8).join("; "))}${audio.silent.length > 8 ? "…" : ""}</span>` : ", none silent"}</p>` +
          `${(audio.errors || []).length ? `<p class=err>${esc(audio.errors.slice(0, 5).join(" | "))}${audio.errors.length > 5 ? "…" : ""}</p>` : ""}` +
          `${warns.length ? `<p class=hint>Warnings: ${esc(warns.slice(0, 3).join(" | "))}</p>` : ""}</div>`;
      };
      try {
        btn.disabled = true; ierr.textContent = ""; res.innerHTML = "";
        const rows = (d.suggested && d.suggested.length ? d.suggested : [{ name: d.description }]);
        const dests = rows.map((_, i) => {
          const dir = document.querySelector(`[data-btd-dir="${i}"]`).value;
          return { name: document.querySelector(`[data-btd-name="${i}"]`).value.trim(),
            code: document.querySelector(`[data-btd-code="${i}"]`).value.trim(),
            via: document.querySelector(`[data-btd-via="${i}"]`).value.trim(),
            dest: document.querySelector(`[data-btd-dest="${i}"]`).value.trim(),
            direction: dir === "" ? null : parseInt(dir, 10) };
        });
        btn.textContent = "Importing service…";
        prog.innerHTML = btBar(0, 1, "sending import request");
        tick = setInterval(() => { btn.textContent = `Importing service… ${elapsed()}`; }, 1000);
        const r = await api("/api/bustimes/import", { method: "POST", body: {
          program_id: p.id, bustimes_id: d.id,
          service_number: $("bt-num").value.trim() || d.line_name,
          service_name: $("bt-name").value.trim() || d.description,
          voice_id: $("bt-voice").value || null,
          generate_audio: $("bt-gen").value !== "no",
          destinations: dests } });
        if (r.state === "done" || !r.job_id) {
          prog.innerHTML = "";
          res.innerHTML = confirmHtml(r, r.audio || {});
          openProgram(p.id);
          return;
        }
        // Audio generates in the background — poll for live progress.
        let job = null;
        for (;;) {
          await new Promise((done) => setTimeout(done, 1000));
          try {
            job = await api(`/api/bustimes/jobs/${encodeURIComponent(r.job_id)}`);
          } catch (e) { ierr.textContent = "Lost track of the import job: " + String(e.message || e); break; }
          if (!document.contains(prog)) break; // user navigated away; job continues server-side
          prog.innerHTML = btBar(job.done || 0, job.total || 1,
            `${job.stage || "working"} · ${elapsed()} elapsed`);
          if (job.state === "done" || job.state === "error") break;
        }
        prog.innerHTML = "";
        if (job && job.state === "done") {
          const a = Object.assign({}, r.audio, job.result, { with_audio: (job.result.generated || 0) + (r.audio.with_audio || 0) });
          res.innerHTML = confirmHtml(r, a);
          openProgram(p.id);
        } else if (job) {
          ierr.textContent = "Audio generation failed: " + (job.error || "unknown error") +
            " — the service and stops were still imported; retry audio from the Audio tab.";
          openProgram(p.id);
        }
      } catch (e) { ierr.textContent = String(e.message || e); }
      finally { clearInterval(tick); btn.disabled = false; btn.textContent = "Import service"; }
    };
  } catch (e) { ebox.textContent = String(e.message || e); }
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
  $("ctlrows").innerHTML = S.controllers.map((c) => {
    const last = c.last_seen_at || "never";
    const status = c.online ? `<b class=ok>ONLINE</b>` : `offline<br><span class=hint>last seen ${esc(last)}</span>`;
    const handshake = c.awaiting_pi ? ` <span class=err>waiting for Pi — pair done, Pi hasn't collected its token yet</span>` : "";
    const progs = (c.programs || []).map((p) => {
      const bits = [`want v${p.desired_version}`, `bus v${p.installed_version == null ? "—" : p.installed_version}`];
      if (p.active_version != null) bits.push(`<b class=ok>active v${p.active_version}</b>`);
      if (p.status && p.status !== "installed") bits.push(`(${esc(p.status)})`);
      let warn = "";
      if (p.active_version != null && p.desired_version != null && p.active_version !== p.desired_version)
        warn = ` <span class=err>bus parked on v${p.active_version} — select a v${p.desired_version} destination to switch</span>`;
      return `${esc(p.name || p.program_id)}: ${bits.join(" / ")}${warn}`;
    }).join("<br>") || "—";
    const pend = c.pending_commands ? ` · ${c.pending_commands} cmd(s) waiting` : "";
    return `<tr><td>${esc(c.name)}</td><td>${status}${esc(pend)}${handshake}</td><td>${esc(last)}</td><td>${esc(c.app_version || "")}</td><td>${progs}</td>
    <td class=row><button data-rename="${c.id}">Rename</button><button data-retoken="${c.id}" title="Issue a new device token (old one stops working)">Rotate token</button><button data-unpair="${c.id}" title="Remove this controller entirely — token revoked, disappears from the portal">Remove</button></td></tr>`;
  }).join("");
  $("rc-ctl").innerHTML = S.controllers.map((c) => `<option value="${c.id}">${esc(c.name)} (${c.online ? "online" : "offline"})</option>`).join("");
}
$("ctlrows").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  try {
    if (b.dataset.rename) {
      const c = S.controllers.find((x) => x.id === b.dataset.rename);
      const name = prompt("Controller name:", c.name);
      if (name) { await api(`/api/controllers/${c.id}`, { method: "PATCH", body: { name } }); loadControllers(); }
    }
    if (b.dataset.unpair && confirm("Remove this controller entirely? Its token is revoked and it disappears from the portal (history kept in the audit log).")) {
      await api(`/api/controllers/${b.dataset.unpair}`, { method: "DELETE" }); loadControllers();
    }
    if (b.dataset.retoken) {
      const c = S.controllers.find((x) => x.id === b.dataset.retoken);
      if (confirm(`Issue a new device token for “${c ? c.name : b.dataset.retoken}”? The current token stops working immediately.`)) {
        const r = await api(`/api/controllers/${b.dataset.retoken}/token`, { method: "POST", body: {} });
        showTokenDialog(c ? c.name : "controller", r.device_token);
      }
    }
  } catch (ex) { err(String(ex.message || ex)); }
});
function showTokenDialog(name, token) {
  $("token-value").value = token || "";
  $("token-sub").innerHTML = `Controller “${esc(name || "controller")}” is paired. The Pi collects this token itself within ~10 s — watch the row flip from “waiting for Pi” to online. Only use the command below as a manual fallback. Lose it and use “Rotate token” for a new one.`;
  const base = location.origin + PREFIX;
  $("token-pi").textContent =
    `sudo systemctl edit nsl-agent   # add under [Service]:\nEnvironment="PORTAL_URL=${base}"\nEnvironment="NSL_TOKEN=${token}"\n` +
    `sudo systemctl daemon-reload && sudo systemctl restart nsl-agent`;
  const d = $("token-dialog");
  if (d && d.showModal) d.showModal();
}
$("btn-token-copy").addEventListener("click", async () => {
  const v = $("token-value").value;
  try { await navigator.clipboard.writeText(v); $("btn-token-copy").textContent = "Copied"; }
  catch (e) {
    $("token-value").select();
    try { document.execCommand("copy"); $("btn-token-copy").textContent = "Copied"; }
    catch (e2) { err("Copy failed — select the token manually (Ctrl+C)."); }
  }
  setTimeout(() => { $("btn-token-copy").textContent = "Copy"; }, 2000);
});
$("btn-token-close").addEventListener("click", () => { const d = $("token-dialog"); if (d) d.close(); });
$("btn-pair").addEventListener("click", async () => {
  try {
    const name = $("pair-name").value;
    const dup = (S.controllers || []).some((c) => (c.name || "").toLowerCase() === name.trim().toLowerCase());
    if (dup && !confirm(`A controller named “${name.trim()}” already exists (check the table — the other row may be a ghost that never checked in). Pair another one with the same name?`)) return;
    const r = await api("/api/controllers/pair", { method: "POST",
      body: formData({ organisation_id: S.org, code: $("pair-code").value.trim().toUpperCase(), name: $("pair-name").value }) });
    $("pair-code").value = ""; $("pair-name").value = ""; loadControllers();
    if (r && r.device_token) showTokenDialog(name || "controller", r.device_token);
    else err("Paired, but the server did not return a device token — use “Rotate token” to issue one.");
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
  S.clips = r.clips || [];
  const srcLabel = (a) => a.source === "tts"
    ? (a.placeholder ? "text-to-speech (preview tone)" : "text-to-speech")
    : (a.source || "upload");
  const durLabel = (a) => (a.duration_ms != null ? (Number(a.duration_ms) / 1000).toFixed(1) + "s" : "—");
  $("audiorows").innerHTML = S.clips.map((a) =>
    `<tr><td><b>${esc(clipLabel(a))}</b>` +
    (a.placeholder ? ` <span class=hint>preview tone</span>` : "") +
    (a.silent_legacy ? ` <span class=err>silent — regenerate</span>` : "") + `</td>` +
    `<td>${esc(srcLabel(a))} · ${esc(durLabel(a))}</td>` +
    `<td><button data-play="${esc(a.sha256)}">Play</button></td>` +
    `<td class=row><button data-rename="${esc(a.sha256)}">Rename</button><button data-delclip="${esc(a.sha256)}">Delete</button></td></tr>`).join("") ||
    `<tr><td colspan=4 class=hint>No clips yet — upload one or generate it from text below.</td></tr>`;
  const v = await api("/api/voices?organisation_id=" + encodeURIComponent(S.org));
  S.voices = v.voices || [];
  S.piper_available = v.piper_available;
  $("voicerows").innerHTML = (v.voices || []).map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.language || "")}</td><td>${esc(x.sample_rate || "")}</td></tr>`).join("") ||
    `<tr><td colspan=3 class=hint>No voices uploaded yet.</td></tr>`;
  if (v.piper_available === false) {
    err("Piper binary not found on the server — voice generation will fall back to preview tone. Install piper-tts + espeak-ng on the server.");
  }
  const prevVoice = $("gen-voice").value || localStorage.getItem("nsl_voice") || "";
  const voices = v.voices || [];
  const stillThere = voices.some((x) => x.id === prevVoice);
  const sel = stillThere ? prevVoice : (voices.length === 1 ? voices[0].id : "");
  const emptyLabel = voices.length ? "Preview tone (no voice) — select a voice for real speech"
    : "Preview tone (no voice installed)";
  $("gen-voice").innerHTML = `<option value="">${esc(emptyLabel)}</option>` + voices.map((x) => `<option value="${x.id}"${x.id === sel ? " selected" : ""}>${esc(x.name)}</option>`).join("");
  if (sel) localStorage.setItem("nsl_voice", sel);
}
$("gen-voice").addEventListener("change", () => localStorage.setItem("nsl_voice", $("gen-voice").value || ""));
async function loadVoices() { /* folded into loadAudio */ }
$("audiorows").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  try {
    if (b.dataset.play) return playClip(b.dataset.play);
    const sha = b.dataset.rename || b.dataset.delclip;
    const clip = S.clips.find((a) => a.sha256 === sha);
    if (b.dataset.rename) {
      const next = prompt("Clip name (lowercase, numbers, dashes):", clipLabel(clip));
      if (!next) return;
      const r = await api(`/api/audio/${encodeURIComponent(sha)}`, { method: "PATCH",
        body: { organisation_id: S.org, name: next } });
      err(""); loadAudio();
      if (S.curProg) openProgram(S.curProg.id);
      alert(`Renamed to ${r.name}.`);
    }
    if (b.dataset.delclip) {
      if (!confirm(`Delete clip "${clipLabel(clip)}"?`)) return;
      await api(`/api/audio/${encodeURIComponent(sha)}?organisation_id=${encodeURIComponent(S.org)}`, { method: "DELETE" });
      loadAudio();
    }
  } catch (ex) { err(String(ex.message || ex)); }
});
$("au-file").addEventListener("change", () => {
  if ($("au-name").value.trim()) return;
  const f = $("au-file").files[0]; if (!f) return;
  $("au-name").value = slugifyClip(f.name.replace(/\.[^.]+$/, ""));
});
$("gen-text").addEventListener("input", () => {
  if ($("gen-name").value.trim()) return;
  $("gen-name").value = slugifyClip($("gen-text").value.split(/\s+/).slice(0, 5).join("-"));
});
function assertClipName(v, box) {
  const s = slugifyClip(v);
  const ok = s.length >= 2;
  if (box) box.textContent = ok || !v ? "" : "Use lowercase letters, numbers and dashes, e.g. derby-bus-station.";
  return ok ? s : null;
}
$("btn-upload").addEventListener("click", async () => {
  try {
    const f = $("au-file").files[0]; if (!f) throw new Error("Choose a file first.");
    const s = assertClipName($("au-name").value, $("au-name-err"));
    if (!s) throw new Error("Give this clip a name, e.g. derby-bus-station.");
    const fd = new FormData(); fd.append("organisation_id", S.org); fd.append("file", f); fd.append("name", s);
    const r = await fetch(PREFIX + "/api/audio", { method: "POST", headers: apiHeaders(), body: fd }).then(async (x) => {
      if (!x.ok) throw new Error((await x.json()).detail || ("HTTP " + x.status)); return x.json();
    });
    alert(`Saved as "${r.name}".`); $("au-file").value = ""; $("au-name").value = ""; loadAudio();
  } catch (e) { err(String(e.message || e)); }
});
$("btn-generate").addEventListener("click", async () => {
  try {
    if (!$("gen-text").value.trim()) throw new Error("Type the text to speak first.");
    const s = assertClipName($("gen-name").value, $("gen-name-err"));
    if (!s) throw new Error("Give this clip a name, e.g. derby-bus-station.");
    const r = await api("/api/audio/generate", { method: "POST", body: { organisation_id: S.org,
      text: $("gen-text").value, name: s, voice_id: $("gen-voice").value || null,
      speed: parseFloat($("gen-speed").value || "1") } });
    alert(r.placeholder
      ? `Saved as "${r.name}" (preview tone — ${r.note || "upload a Piper voice for real speech"}).`
      : `Saved as "${r.name}".`);
    loadAudio();
  } catch (e) { err(String(e.message || e)); }
});
$("btn-voice").addEventListener("click", async () => {
  try {
    const m = $("vv-model").files[0], c = $("vv-config").files[0];
    if (!m || !c) throw new Error("Choose both model and config files.");
    if (!$("vv-name").value.trim()) throw new Error("Give the voice a name first.");
    const fd = new FormData();
    fd.append("organisation_id", S.org); fd.append("name", $("vv-name").value.trim());
    fd.append("model", m); fd.append("config", c);
    const r = await fetch(PREFIX + "/api/voices", { method: "POST", headers: apiHeaders(), body: fd }).then(async (x) => {
      if (!x.ok) throw new Error((await x.json()).detail || ("HTTP " + x.status)); return x.json();
    });
    alert(`Voice "${$("vv-name").value.trim()}" uploaded.`); $("vv-name").value = ""; $("vv-model").value = ""; $("vv-config").value = ""; loadAudio();
  } catch (e) { err(String(e.message || e)); }
});

// ---------- remote ----------
async function rcSend(body) {
  const cid = $("rc-ctl").value;
  if (!cid) throw new Error("Choose a controller first.");
  const c = S.controllers.find((x) => x.id === cid);
  if (c && !c.online) throw new Error(`“${c.name}” is offline (last seen ${c.last_seen_at || "never"}) — command refused, nothing was queued.`);
  const r = await api(`/api/controllers/${cid}/command`, { method: "POST", body });
  const when = new Date().toLocaleTimeString();
  const msg = `Sent to “${c ? c.name : cid}” at ${when} — the bus picks it up within ~10 s.`;
  $("rc-state").textContent = msg;
  if ($("rc-result")) $("rc-result").textContent = msg;
  refreshLive();
  return r;
}
function rcLiveHtml(l) {
  if (!l) return "Select a controller to see its current stop.";
  const stale = l.stale ? ` <span class=err>(last known — bus offline, last seen ${esc(l.last_seen_at || "never")})</span>` : "";
  if (!l.program) return `Idle — no program selected on the bus.${stale}`;
  const dest = l.destination
    ? `<span class=kbd>${esc(l.destination.code || "")}</span> ${esc(l.destination.name || "")}` : "—";
  const svc = l.service ? `Service <b>${esc(l.service.number || "")}</b> · ` : "";
  const stop = l.stop
    ? `<br>Current stop <b>${l.stop.position}/${l.stop.count}: ${esc(l.stop.board_text || l.stop.name)}</b>` +
      (l.stop.is_terminus ? ` <span class=hint>(terminus)</span>` : "") +
      (l.next_stop ? `<br>Next stop: ${esc(l.next_stop.name)}` : `<br>Next stop: — (end of route)`)
    : "<br>Current stop: —";
  const ov = l.override ? `<br>Override: <b>${esc(l.override)}</b>` : "";
  const vol = `<br>Volume: ${l.muted ? "<b>muted</b>" : esc(l.volume)}`;
  return `${svc}${dest}${stop}${ov}${vol}${stale}`;
}
async function refreshLive() {
  const box = $("rc-live");
  if (!box) return;
  const cid = $("rc-ctl") && $("rc-ctl").value;
  if (!cid) { box.innerHTML = "Select a controller to see its current stop."; return; }
  try {
    const l = await api(`/api/controllers/${encodeURIComponent(cid)}/live`);
    box.innerHTML = rcLiveHtml(l);
  } catch (e) { box.innerHTML = `<span class=err>Live status unavailable: ${esc(String(e.message || e))}</span>`; }
}
// Live poll while the remote tab is on screen; the bus reports state on
// heartbeat, so a sent command appears here once the bus picks it up.
if (!window._rcLiveTimer) {
  window._rcLiveTimer = setInterval(() => {
    try {
      if (S.token && $("tab-remote") && $("tab-remote").style.display !== "none" && $("rc-ctl") && $("rc-ctl").value)
        refreshLive();
    } catch (e) { /* keep polling */ }
  }, 5000);
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
  $("rc-state").textContent = c ? `${c.name}: ${c.online ? "ONLINE" : "OFFLINE"} — last seen ${c.last_seen_at || "never"}, app ${c.app_version || "?"}, ${c.pending_commands || 0} command(s) waiting.` : "Select a controller.";
  refreshLive();
});
$("rc-ctl").addEventListener("change", () => refreshLive());

// ---------- boot ----------
$("form-login").addEventListener("submit", (e) => { e.preventDefault(); doLogin(); });
$("form-signup").addEventListener("submit", (e) => { e.preventDefault(); doCreate(); });
$("link-signup").addEventListener("click", (e) => { e.preventDefault(); showAuthPage("signup"); $("su-name").focus(); });
$("link-signin").addEventListener("click", (e) => { e.preventDefault(); showAuthPage("signin"); $("si-email").focus(); });
$("btn-logout").addEventListener("click", () => {
  S.token = ""; localStorage.removeItem("nsl_token"); showAuthPage("signin"); showAuth(false);
});
$("orgsel").addEventListener("change", () => {
  S.org = $("orgsel").value; localStorage.setItem("nsl_org", S.org); loadAll();
});
document.querySelector('#tabs .tab').classList.add("on");
refreshMe();
