/* NSL Portal app - talks to /api (same prefix as page URL). No placeholders; labels above inputs. */
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
function audioOptions(selected, { allowEmpty = true, emptyLabel = "No audio - silent" } = {}) {
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
    if (!blob.size) throw new Error("empty audio file - re-upload or regenerate this clip");
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
  // One pages system: the destination's board pages (each with its own
  // via/dest) rotate every page_seconds. Each board page can have its own
  // layout via `page_elements` keyed by board-page position; missing key
  // falls back to the shared `elements` list.
  function slotFor(lay, t) {
    return Math.floor(t / (parseFloat(lay.page_seconds) || 3));
  }
  function boardPosFor(destPages, slot) {
    if (!destPages || !destPages.length) return 0;
    const ordered = [...destPages].sort((a, b) => (a.position || 0) - (b.position || 0));
    const pg = ordered[slot % ordered.length];
    return (pg && pg.position) || ((slot % ordered.length) + 1);
  }
  function fieldsForSlot(baseFields, destPages, slot) {
    if (!destPages || !destPages.length) return baseFields;
    const ordered = [...destPages].sort((a, b) => (a.position || 0) - (b.position || 0));
    const pg = ordered[slot % ordered.length];
    return Object.assign({}, baseFields, { via: pg.via || "", dest: pg.dest || "" });
  }
  function activeElements(lay, boardPos) {
    const pe = lay.page_elements || {};
    return pe[boardPos] || pe[String(boardPos)] || lay.elements || [];
  }
  function drawOne(cv, lay, baseFields, ovText, t, destPages) {
    const H = (typeof LayoutEditor !== "undefined" && LayoutEditor.hanover) || null;
    const scale = parseInt(cv.dataset.scale || "3", 10);
    const off = document.createElement("canvas");
    off.width = lay.width; off.height = lay.height;
    const g = off.getContext("2d");
    g.fillStyle = lay.background || "#000000";
    g.fillRect(0, 0, off.width, off.height);
    const ovActive = !!ovText;
    const ov = lay.override || {};
    const slot = slotFor(lay, t);
    const boardPos = boardPosFor(destPages, slot);
    const fields = fieldsForSlot(baseFields, destPages, slot);
    const elements = activeElements(lay, boardPos);
    const hide = new Set();
    if (ovActive) {
      if (ov.mode === "hide_others") {
        const keep = ov.replaces || "dest";
        for (const e of elements) if (e.field !== keep) hide.add(e.field);
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
      for (const el of elements) {
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
        const ink = el.colour || AMBER;
        const paper = lay.background || "#000000";
        const hanStrip = (colour) => {
          const c1 = document.createElement("canvas");
          c1.width = Math.max(1, tw1); c1.height = han.height;
          H.draw(c1.getContext("2d"), han, text, 0, 0, colour || ink, hsp);
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
        // Inverted boxes fill the whole element area (matches layout editor + bus).
        if (el.invert && !needScroll) {
          g.fillStyle = ink; g.fillRect(bx, by, bw, bh);
        }
        if (han && !needScroll) {
          const sh = han.height * sc;
          const y0 = el.valign === "middle" ? by + Math.floor((bh - sh) / 2)
            : (el.valign === "bottom" ? by + bh - sh : by);
          let tx = bx + 1;
          if (el.align === "centre" || el.align === "center") tx = bx + Math.floor((bw - tw) / 2);
          if (el.align === "right") tx = bx + bw - tw;
          g.drawImage(hanStrip(el.invert ? paper : ink), tx, y0);
          continue;
        }
        if (!han && !needScroll) {
          g.font = "12px monospace";
          g.fillStyle = el.invert ? paper : ink;
          let tx = bx + 1;
          if (g.textAlign === "center") tx = bx + bw / 2;
          if (g.textAlign === "right") tx = bx + bw - 1;
          g.fillText(text, tx, ty);
          continue;
        }
        g.save();
        g.beginPath(); g.rect(bx, by, bw, bh); g.clip();
        if (el.invert) { g.fillStyle = ink; g.fillRect(bx, by, bw, bh); }
        if (han) {
          const sh = han.height * sc;
          const y0 = el.valign === "middle" ? by + Math.floor((bh - sh) / 2)
            : (el.valign === "bottom" ? by + bh - sh : by);
          const offx = Math.floor(t * 20) % Math.floor(tw + bw);
          const strip = hanStrip(el.invert ? paper : ink);
          const s2 = document.createElement("canvas");
          s2.width = Math.ceil(tw + bw); s2.height = bh;
          s2.getContext("2d").drawImage(strip, 0, Math.max(0, y0 - by));
          g.drawImage(s2, offx, 0, bw, bh, bx, by, bw, bh);
          g.restore();
          continue;
        }
        g.font = "12px monospace";
        g.fillStyle = el.invert ? paper : ink;
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
      const ovEl = elements.find((e) => e.field === (ov.replaces || "dest"));
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
      drawOne(cv, entry.layout || autofit(disp.width_px, disp.height_px), snap.fields || {}, snap.board_override || "", secs, snap.pages);
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
  } catch (e) { showAuth(false); $("auth-err").textContent = "Session expired - sign in again."; }
}
function showAuth(on) {
  $("authview").style.display = on ? "none" : "";
  $("mainview").style.display = on ? "" : "none";
  const vis = on ? "" : "none";
  $("who").style.display = vis;
  $("orgsel").style.display = vis;
  $("btn-logout").style.display = vis;
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

// ---------- sidebar navigation ----------
function showSection(name) {
  ["programs", "codes", "controllers", "audio", "tfl", "remote"].forEach((n) => { $("tab-" + n).style.display = (n === name) ? "" : "none"; });
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("on", b.dataset.section === name));
}
document.querySelectorAll(".nav-item").forEach((t) => t.addEventListener("click", () => {
  showSection(t.dataset.section);
  LayoutEditor.stop();
  Preview.stop();
  if (t.dataset.section === "remote") refreshUpdateStatus();
}));

// ---------- programs ----------
async function loadAll() {
  err("");
  await Promise.all([loadPrograms(), loadControllers(), loadAudio(), loadVoices()]);
}
async function loadPrograms() {
  const r = await api("/api/programs?organisation_id=" + encodeURIComponent(S.org));
  S.programs = r.programs || [];
  $("progrows").innerHTML = S.programs.map((p) => {
    const svcCount = (p.services || []).length;
    const destCount = (p.services || []).reduce((n, s) => n + (s.destinations || []).length, 0);
    return `<tr><td><b>${esc(p.name)}</b><div class=hint>${svcCount} service(s) · ${destCount} destination(s)</div></td>` +
      `<td>v${esc(p.published_version || 0)}</td>` +
      `<td class=row>` +
      `<button class=primary data-open="${p.id}">Open</button>` +
      `<button data-dup="${p.id}">Duplicate</button>` +
      `<button data-exp="${p.id}">Export</button>` +
      `<button class=danger data-del="${p.id}">Delete</button></td></tr>`;
  }).join("") || `<tr><td colspan=3 class=hint>No programs yet. Create one above.</td></tr>`;
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

  const svcCount = (p.services || []).length;
  const destCount = flat.length;

  // ---- HELEN-style program header with sub-tabs ----
  let h = `<div class=prog-header>`;
  h += `<div class=col><h2 style="margin:0">${esc(p.name)}</h2><div class=meta>width ${esc(p.px_width)} px · published v${esc(p.published_version || 0)} · ${svcCount} service(s) · ${destCount} destination(s)</div></div>`;
  h += `<div style="align-self:end" class=row><button id=pd-close class=tool>Close program</button></div>`;
  h += `</div>`;

  h += `<div class=tabs id=pd-tabs>`;
  h += `<button class="tab on" data-pdtab=destinations>Destinations</button>`;
  h += `<button class=tab data-pdtab=displays>Displays</button>`;
  h += `<button class=tab data-pdtab=templates>Layouts</button>`;
  h += `<button class=tab data-pdtab=services>Services</button>`;
  h += `<button class=tab data-pdtab=preview>Preview</button>`;
  h += `<button class=tab data-pdtab=publish>Publish &amp; Upload</button>`;
  h += `</div>`;

  // ---- Destinations tab ----
  // Service number first, then its destinations; each destination owns its
  // board pages (each page has its own via/dest) plus a driver message
  // (the label the driver sees on the controller screen).
  const pageList = (d) => (d.pages && d.pages.length ? d.pages :
    [{ via: d.via || "", dest: d.dest || "" }]);
  const pageSummary = (d) => pageList(d).map((pg, i) =>
    `P${i + 1} ${esc(pg.dest || "—")}${pg.via ? " via " + esc(pg.via) : ""}`).join(" · ");
  h += `<section id=pdtab-destinations>`;
  h += `<div class=group><div class=grouptitle>1 · Service number</div>`;
  h += `<div class=row><div class=col><label for=dest-service>Service</label><select id=dest-service>` +
    `<option value="">All services (${destCount} destination(s))</option>` +
    (p.services || []).map((s) => `<option value="${s.id}">${esc(s.number)}${s.name ? " - " + esc(s.name) : ""} (${(s.destinations || []).length})</option>`).join("") +
    `</select></div><div class=col><label for=dest-filter>Filter</label><input id=dest-filter placeholder="code or driver message"></div></div>`;
  h += `<p class=hint>Pick a service to see its destinations. Each destination is one code the driver enters: it has a driver message for the controller screen and one or more board pages, each with its own via / dest.</p></div>`;
  for (const s of (p.services || [])) {
    const dests = [...(s.destinations || [])].sort((a, b) => (a.code < b.code ? -1 : 1));
    h += `<div class=group data-svc-group="${s.id}"><div class=grouptitle>Service ${esc(s.number)}${s.name ? " - " + esc(s.name) : ""} (${dests.length})</div>`;
    if (!dests.length) {
      h += `<p class=hint>No destinations yet - add one below.</p>`;
    } else {
      h += `<div class=dest-list><table><thead><tr><th>Code</th><th>Driver message</th><th>Board pages</th><th>Stops</th><th>Actions</th></tr></thead><tbody>`;
      for (const d of dests) {
        h += `<tr data-code="${esc(d.code)}" data-sid="${s.id}" data-name="${esc(((d.name || "") + " " + (d.via || "") + " " + (d.dest || "")).toLowerCase())}">` +
          `<td class=kbd>${esc(d.code)}</td><td>${esc(d.name || "—")}</td><td class=hint>${pageSummary(d)}</td>` +
          `<td>${(d.stops || []).length}</td>` +
          `<td class=row>` +
          `<button class=primary data-layout="${d.id}">Display</button>` +
          `<button data-pages="${d.id}">Pages</button>` +
          `<button data-stops="${d.id}">Stops</button>` +
          `<button data-edit="${d.id}">Edit</button>` +
          `<button data-dup="${d.id}">Duplicate</button>` +
          `<button class=danger data-del="${d.id}">Delete</button></td></tr>`;
      }
      h += `</tbody></table></div>`;
    }
    h += `</div>`;
  }

  h += `<div class=group><div class=grouptitle>New destination</div>`;
  h += `<div class=row>
    <div style="flex:0 0 180px"><label for=nd-service>2 · Service</label><select id=nd-service>` +
    (p.services || []).map((s) => `<option value="${s.id}">${esc(s.number)}${s.name ? " - " + esc(s.name) : ""}</option>`).join("") +
    `<option value="__new__">New service…</option></select></div>
    <div style="flex:0 0 140px"><label for=nd-code>3 · Code (1–8 digits)</label><input id=nd-code maxlength=8 inputmode=numeric></div>
    <div style="align-self:end"><button id=nd-suggest type=button>Suggest</button></div></div>`;
  h += `<div class=row id=nd-newsvc style="display:none">
    <div style="flex:0 0 120px"><label for=nd-newsvc-num>New service number <span class=req>(required)</span></label><input id=nd-newsvc-num></div>
    <div class=col><label for=nd-newsvc-name>Service description</label><input id=nd-newsvc-name></div></div>`;
  h += `<div class=row>
    <div class=col><label for=nd-name>4 · Driver message <span class=req>(required)</span></label><input id=nd-name></div></div>
    <p class=hint style="margin:0">Shown on the controller screen so the driver knows what they picked. The board itself shows the pages below.</p>`;
  h += `<div id=nd-pages></div>`;
  h += `<div class=row><div style="align-self:end"><button id=nd-addpage type=button>Add another board page</button></div>
    <div style="align-self:end"><button id=nd-add class=primary>Add destination</button></div></div>
  <div class=err id=nd-err></div></div>`;

  h += `<div class=group><div class=grouptitle>Import from bustimes.org</div>
    <p class=hint>Live lookup at bustimes.org. Typical destinations come from the description split on " - ". Stops come from the service timetable; stop audio is generated with Piper when a voice is picked.</p>
    <div class=row>
    <div style="flex:0 0 120px"><label for=bt-operator>Operator code</label><input id=bt-operator value="BULI"></div>
    <div class=col><label for=bt-search>Search (number or place)</label><input id=bt-search></div>
    <div style="align-self:end"><button id=bt-find>Search bustimes.org</button></div></div>
    <div class=err id=bt-err></div><div id=bt-results></div><div id=bt-detail></div></div>`;
  h += `</section>`;

  // ---- Displays tab ----
  h += `<section id=pdtab-displays style="display:none">`;
  h += `<div class=group><div class=grouptitle>Displays (${(p.displays || []).length})</div>`;
  h += `<p class=hint>Define the boards this program drives, e.g. front 240×40, side 160×40, rear 80×40. Layouts are saved per display.</p>`;
  if ((p.displays || []).length) {
    h += `<div class="tablewrap" id=pd-displays-list><table class=dataTable>
      <thead><tr><th style="min-width:160px">Name</th><th style="width:110px">Width (px)</th><th style="width:110px">Height (px)</th><th></th></tr></thead><tbody>`;
    h += (p.displays || []).map((d) =>
      `<tr data-disp-id="${esc(d.id)}">
        <td><input data-disp-name value="${esc(d.name)}"></td>
        <td><input data-disp-w type=number value="${esc(d.width_px)}"></td>
        <td><input data-disp-h type=number value="${esc(d.height_px)}"></td>
        <td class=row><button data-disp-save=1 class=primary>Save</button><button data-disp-del=1 class=danger>Delete</button></td>
      </tr>`).join("");
    h += `</tbody></table></div>`;
  } else {
    h += `<p class=hint>No displays yet.</p><div id=pd-displays-list></div>`;
  }
  h += `<div class=group style="margin-top:14px"><div class=grouptitle>Add display</div><div class=row>
    <div style="flex:1"><label for=pd-disp-name>Display name <span class=req>(required)</span></label><input id=pd-disp-name placeholder="e.g. side"></div>
    <div style="flex:0 0 110px"><label for=pd-disp-w>Width</label><input id=pd-disp-w type=number value=160></div>
    <div style="flex:0 0 110px"><label for=pd-disp-h>Height</label><input id=pd-disp-h type=number value=40></div>
    <div style="align-self:end"><button id=pd-disp-add class=primary>Add display</button></div>
  </div><div class=err id=pd-disp-err></div></div>`;
  h += `</section>`;

  // ---- Layouts (templates) tab ----
  h += `<section id=pdtab-templates style="display:none">`;
  h += `<div class=group><div class=grouptitle>Layout presets</div>`;
  h += `<p class=hint>Save default layouts for each display. New destinations start from the default layout for that display. You can also apply a preset while editing a destination layout.</p>`;
  h += `<div id=pd-templates-list>`;
  for (const d of (p.displays || [])) {
    const tmpls = (p.templates || []).filter((t) => t.display_id === d.id);
    h += `<div class=group><div class=grouptitle>${esc(d.name)} · ${esc(d.width_px)}×${esc(d.height_px)}</div>`;
    if (tmpls.length) {
      h += `<div class="tablewrap"><table class=dataTable><thead><tr><th>Name</th><th style="width:90px">Default</th><th></th></tr></thead><tbody>`;
      for (const t of tmpls) {
        h += `<tr data-tmpl-id="${esc(t.id)}">
          <td><input data-tmpl-name value="${esc(t.name)}"></td>
          <td style="text-align:center"><input data-tmpl-default type=checkbox${t.is_default ? " checked" : ""}></td>
          <td class=row><button data-tmpl-save=1 class=primary>Save</button><button data-tmpl-apply=1>Apply to current destination</button><button data-tmpl-del=1 class=danger>Delete</button></td>
        </tr>`;
      }
      h += `</tbody></table></div>`;
    } else {
      h += `<p class=hint>No presets for this display yet.</p>`;
    }
    h += `<div class=row style="margin-top:10px">
      <div style="flex:1"><label>New preset name</label><input data-tmpl-new-name data-tmpl-disp="${esc(d.id)}" placeholder="e.g. route/dest/via"></div>
      <div style="align-self:end"><button data-tmpl-add=1 data-tmpl-disp="${esc(d.id)}" class=primary>Add preset</button></div>
    </div>`;
    h += `</div>`;
  }
  h += `</div><div class=err id=pd-tmpl-err></div></div></section>`;

  // ---- Services tab ----
  h += `<section id=pdtab-services style="display:none">`;
  h += `<div class=group><div class=grouptitle>Add service</div><div class=row>
    <div style="flex:0 0 140px"><label for=ns-num>Service number <span class=req>(required)</span></label><input id=ns-num placeholder="e.g. 100"></div>
    <div class=col><label for=ns-name>Service description</label><input id=ns-name placeholder="e.g. Lichfield - Burton"></div>
    <div style="align-self:end"><button id=ns-add class=primary>Add service</button></div></div></div>`;
  h += `<div class=group><div class=grouptitle>Services (${svcCount})</div>`;
  if (svcCount) {
    h += `<div class="tablewrap" id=pd-services><table class=dataTable>
      <thead><tr>
        <th style="width:110px">Number</th>
        <th>Name</th>
        <th style="width:70px">Dests</th>
        <th style="min-width:180px">Next-stop audio</th>
        <th style="min-width:180px">Approaching audio</th>
        <th style="min-width:180px">Terminus suffix</th>
        <th style="width:150px">Terminus plays on</th>
        <th></th>
      </tr></thead><tbody>`;
    for (const s of (p.services || [])) {
      const destCount = (s.destinations || []).length;
      h += `<tr data-svc-row="${s.id}">
        <td><input data-svc-num="${s.id}" value="${esc(s.number)}"></td>
        <td><input data-svc-name="${s.id}" value="${esc(s.name || "")}"></td>
        <td style="text-align:center">${destCount}</td>
        <td><select data-svc-audio="next_stop_audio" data-svc="${s.id}">${audioOptions(s.next_stop_audio, { emptyLabel: "Default" })}</select></td>
        <td><select data-svc-audio="approaching_audio" data-svc="${s.id}">${audioOptions(s.approaching_audio, { emptyLabel: "Default" })}</select></td>
        <td><select data-svc-audio="last_stop_audio" data-svc="${s.id}">${audioOptions(s.last_stop_audio, { emptyLabel: "Default" })}</select></td>
        <td><select data-svc-mode="${s.id}">
          <option value="both"${(s.terminus_clip_mode || "both") === "both" ? " selected" : ""}>Next + approaching</option>
          <option value="next"${s.terminus_clip_mode === "next" ? " selected" : ""}>Next stop only</option>
          <option value="approaching"${s.terminus_clip_mode === "approaching" ? " selected" : ""}>Approaching only</option>
        </select></td>
        <td class=row><button data-svc-save="${s.id}" class=primary title="Save changes">Save</button><button data-svc-del="${s.id}" class=danger title="Delete service">Delete</button><div class=err data-svc-err="${s.id}" style="min-width:120px"></div></td>
      </tr>`;
    }
    h += `</tbody></table></div>`;
  } else {
    h += `<p class=hint>No services yet. Add one above, or import from bustimes.org.</p>`;
  }
  h += `</div></section>`;

  // ---- Preview tab ----
  h += `<section id=pdtab-preview style="display:none">`;
  h += `<div class=group><div class=grouptitle>Journey preview - screens + announcements</div>`;
  h += `<div class=row><div class=col><label for=pv-dest>Destination</label><select id=pv-dest>` +
    flat.map(({ s, d }) => `<option value="${d.id}">${esc(d.code)} · ${esc(s.number)} · ${esc(dispName(d, s))}</option>`).join("") +
    `</select></div><div style="flex:0 0 200px"><label for=pv-pos>Stop</label><div class=row><button id=pv-first class=icn title="First stop"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z"/></svg></button><button id=pv-prev class=icn title="Previous stop"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg></button><button id=pv-next class=icn title="Next stop"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg></button></div></div></div>`;
  h += `<div id=pv-meta class=hint></div><div id=pv-screens class=row style="align-items:flex-start"></div>`;
  h += `<div class=row><div class=col><label>Next-stop announcement</label><div id=pv-next-list class=row></div><div class=row><button id=pv-play-next class=primary>Play next-stop</button></div></div>`;
  h += `<div class=col><label>Approaching announcement</label><div id=pv-appr-list class=row></div><div class=row><button id=pv-play-appr class=primary>Play approaching</button></div></div></div>`;
  h += `<div class=err id=pv-err></div></div>`;
  h += `</section>`;

  // ---- Publish & Upload tab ----
  h += `<section id=pdtab-publish style="display:none">`;
  h += `<div class=group><div class=grouptitle>Publish new version</div>`;
  h += `<p class=hint>Edits are drafts until published. Publishing validates the program, builds an immutable numbered version, and prepares it for upload to controllers.</p>`;
  h += `<div class=row><div class=col><label for=pd-notes>Publish notes</label><input id=pd-notes></div>
    <div style="align-self:end"><button id=pd-publish class=primary>Publish new version</button></div></div>`;
  h += `<div id=pd-versions class=hint><b>Versions:</b> ` + (p.versions || []).map((v) => `v${v.version}`).join(", ") + `</div></div>`;

  h += `<div class=group><div class=grouptitle>Upload to controllers</div>`;
  h += `<div id=pd-ctls>` +
    S.controllers.map((c) => {
      const act = (c.programs || []).find((x) => x.active_version != null);
      const run = act ? ` · running ${esc(act.name || act.program_id)} v${act.active_version}` : "";
      const ghost = !c.last_seen_at ? ` <span class=err>never checked in - probably a duplicate row, delete it in the Controllers tab</span>` : "";
      return `<div class=row data-ctlrow="${c.id}" style="align-items:center"><label style="font-weight:normal;flex:1"><input type=checkbox value="${c.id}"> ${esc(c.name)} <span class="badge ${c.online ? "ok" : "err"}">${c.online ? "online" : "offline"}</span>${run}${ghost}</label>` +
        `<span class=hint data-upstat="${c.id}" style="margin-right:8px"></span>` +
        `<button data-up-latest="${c.id}" class=primary title="Publish (if changed) and upload the latest version to ${esc(c.name)}">Send latest</button></div>`;
    }).join("") || `<p class=hint>No controllers paired yet. Pair one in the Controllers tab.</p>` +
    `</div>`;
  h += `<div class=row>
    <div style="flex:0 0 120px"><label for=pd-ver>Exact version</label><input id=pd-ver type=number value="${esc(p.published_version || 0)}"></div>
    <div style="align-self:end"><button id=pd-upload>Queue exact version to ticked</button></div></div></div>`;
  h += `</section>`;

  // ---- Edit destination dialog ----
  h += `<dialog id=dest-dialog><form method=dialog>
    <h3 style="margin-top:0">Edit destination</h3>
    <div class=row><div style="flex:0 0 140px"><label for=ed-code>Code (1–8 digits) <span class=req>(required)</span></label><input id=ed-code maxlength=8 inputmode=numeric></div>
    <div class=col><label for=ed-service>Service</label><select id=ed-service></select></div></div>
    <div><label for=ed-name>Driver message <span class=req>(required)</span></label><input id=ed-name></div>
    <p class=hint style="margin:4px 0">The driver message shows on the controller screen. The boards show page 1 below - use the Pages button for extra board pages.</p>
    <div class=row><div class=col><label for=ed-via>Page 1 via</label><input id=ed-via></div>
    <div class=col><label for=ed-dest>Page 1 dest board</label><input id=ed-dest></div></div>
    <div class=err id=ed-err></div>
    <div class=row style="justify-content:flex-end"><button id=ed-close>Cancel</button><button id=ed-save class=primary value=default>Save</button></div>
    </form></dialog>`;

  // ---- Duplicate destination dialog (service + code, copies stops/pages/layouts) ----
  h += `<dialog id=dup-dialog><form method=dialog>
    <h3 style="margin-top:0">Duplicate destination</h3>
    <p class=hint id=dup-src style="margin:4px 0"></p>
    <div class=row><div style="flex:0 0 140px"><label for=dup-code>New code (1–8 digits) <span class=req>(required)</span></label><input id=dup-code maxlength=8 inputmode=numeric></div>
    <div class=col><label for=dup-service>Service</label><select id=dup-service></select></div></div>
    <div class=err id=dup-err></div>
    <div class=row style="justify-content:flex-end"><button id=dup-close>Cancel</button><button id=dup-save class=primary value=default>Duplicate</button></div>
    </form></dialog>`;

  h += `<div id=pd-edit></div>`;
  $("pd-body").innerHTML = h;

  // ---- sub-tab wiring ----
  document.querySelectorAll("#pd-tabs .tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll("#pd-tabs .tab").forEach((x) => x.classList.remove("on"));
    t.classList.add("on");
    ["destinations", "displays", "templates", "services", "preview", "publish"].forEach((n) => { $("pdtab-" + n).style.display = (n === t.dataset.pdtab) ? "" : "none"; });
    if (t.dataset.pdtab === "preview") loadPreview();
  }));

  // ---- displays ----
  $("pd-disp-add").onclick = async () => {
    $("pd-disp-err").textContent = "";
    try {
      const name = $("pd-disp-name").value.trim();
      if (!name) throw new Error("Display name is required.");
      await api(`/api/programs/${p.id}/displays`, { method: "POST", body: {
        name, width_px: parseInt($("pd-disp-w").value || "160", 10), height_px: parseInt($("pd-disp-h").value || "40", 10) } });
      $("pd-disp-name").value = ""; $("pd-disp-w").value = 160; $("pd-disp-h").value = 40;
      openProgram(p.id);
    } catch (e) { $("pd-disp-err").textContent = String(e.message || e); }
  };
  $("pd-displays-list").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const row = b.closest("[data-disp-id]");
    const did = row && row.dataset.dispId;
    $("pd-disp-err").textContent = "";
    try {
      if (b.dataset.dispSave) {
        await api(`/api/programs/${p.id}/displays/${did}`, { method: "PUT", body: {
          name: row.querySelector("[data-disp-name]").value,
          width_px: parseInt(row.querySelector("[data-disp-w]").value || "240", 10),
          height_px: parseInt(row.querySelector("[data-disp-h]").value || "40", 10) } });
        openProgram(p.id);
      }
      if (b.dataset.dispDel) {
        if (!confirm("Delete this display? All layouts for it will be lost.")) return;
        await api(`/api/programs/${p.id}/displays/${did}`, { method: "DELETE" });
        openProgram(p.id);
      }
    } catch (e) { $("pd-disp-err").textContent = String(e.message || e); }
  });

  // ---- templates (layout presets) ----
  $("pd-templates-list").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const errBox = $("pd-tmpl-err"); errBox.textContent = "";
    try {
      if (b.dataset.tmplAdd) {
        const did = b.dataset.tmplDisp;
        const name = (b.closest(".row") || b.parentElement).querySelector("[data-tmpl-new-name]").value.trim();
        if (!name) throw new Error("Preset name is required.");
        const d = (p.displays || []).find((x) => x.id === did);
        await api(`/api/programs/${p.id}/templates`, { method: "POST", body: {
          display_id: did, name, width_px: d && d.width_px, height_px: d && d.height_px } });
        openProgram(p.id);
      }
      if (b.dataset.tmplSave) {
        const row = b.closest("[data-tmpl-id]");
        await api(`/api/programs/${p.id}/templates/${row.dataset.tmplId}`, { method: "PUT", body: {
          name: row.querySelector("[data-tmpl-name]").value,
          is_default: row.querySelector("[data-tmpl-default]").checked } });
        openProgram(p.id);
      }
      if (b.dataset.tmplDel) {
        const row = b.closest("[data-tmpl-id]");
        if (!confirm("Delete this preset?")) return;
        await api(`/api/programs/${p.id}/templates/${row.dataset.tmplId}`, { method: "DELETE" });
        openProgram(p.id);
      }
      if (b.dataset.tmplApply) {
        const row = b.closest("[data-tmpl-id]");
        const t = (p.templates || []).find((x) => x.id === row.dataset.tmplId);
        if (LayoutEditor.isOpen() && t && t.layout) {
          LayoutEditor.loadLayout(JSON.parse(JSON.stringify(t.layout)), `template: ${t.name}`);
        } else {
          errBox.textContent = "Open a destination layout first, then click Apply.";
        }
      }
    } catch (e) { errBox.textContent = String(e.message || e); }
  });

  // ---- close / publish / upload ----
  $("pd-close").onclick = () => { $("progdetail").style.display = "none"; LayoutEditor.stop(); Preview.stop(); S.curProg = null; };
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
      alert(r.unchanged ? `No changes - v${r.version} re-queued for 1 controller.` : `Published v${r.version}, queued for 1 controller.`);
      loadPrograms(); refreshUploadStatus();
    } catch (e) { err(String(e.message || e)); }
  });
  if (window._upTimer) clearInterval(window._upTimer);
  window._upTimer = setInterval(() => { if ($("pd-ctls")) refreshUploadStatus(); }, 5000);
  refreshUploadStatus();

  // ---- destination filter (service first, then text) ----
  const applyDestFilter = () => {
    const sid = $("dest-service").value;
    const q = $("dest-filter").value.trim().toLowerCase();
    document.querySelectorAll("[data-svc-group]").forEach((g) => {
      const matchSvc = !sid || g.dataset.svcGroup === sid;
      let visible = 0;
      g.querySelectorAll("tr[data-code]").forEach((tr) => {
        const ok = matchSvc && (!q || (tr.dataset.code + " " + tr.dataset.name).includes(q));
        tr.style.display = ok ? "" : "none";
        if (ok) visible++;
      });
      g.style.display = matchSvc && (visible > 0 || !q) ? "" : "none";
    });
  };
  $("dest-service").addEventListener("change", applyDestFilter);
  $("dest-filter").addEventListener("input", applyDestFilter);

  // ---- new destination (service → code → driver message → board pages) ----
  const svcSel = $("nd-service");
  const syncNewSvc = () => { $("nd-newsvc").style.display = svcSel.value === "__new__" ? "" : "none"; };
  svcSel.addEventListener("change", syncNewSvc); syncNewSvc();
  const ndPageRow = (n) =>
    `<div class=row data-nd-page="${n}">
      <div style="flex:0 0 90px"><label>Page ${n}</label><div class=hint>${n === 1 ? "first shown" : "cycles after P" + (n - 1)}</div></div>
      <div class=col><label for=nd-via-${n}>Via</label><input id=nd-via-${n}></div>
      <div class=col><label for=nd-dest-${n}>Dest board</label><input id=nd-dest-${n}></div>
      ${n > 1 ? `<div style="align-self:end"><button data-nd-delpage type=button class=danger>Remove</button></div>` : ""}
    </div>`;
  const ndReadVals = () => [...document.querySelectorAll("[data-nd-page]")].map((row) => ({
    via: row.querySelector("input[id^=nd-via-]").value,
    dest: row.querySelector("input[id^=nd-dest-]").value }));
  const ndRenderVals = (vals) => {
    if (!vals.length) vals = [{ via: "", dest: "" }];
    $("nd-pages").innerHTML = `<label>5 · Board pages <span class=hint>each page gets its own via / dest; the boards cycle through them</span></label>` +
      vals.map((_, i) => ndPageRow(i + 1)).join("");
    [...document.querySelectorAll("[data-nd-page]")].forEach((row, i) => {
      row.querySelector("input[id^=nd-via-]").value = vals[i].via;
      row.querySelector("input[id^=nd-dest-]").value = vals[i].dest;
    });
    $("nd-pages").querySelectorAll("[data-nd-delpage]").forEach((b) => b.onclick = () => {
      const vals2 = ndReadVals();
      vals2.splice([...document.querySelectorAll("[data-nd-page]")].indexOf(b.closest("[data-nd-page]")), 1);
      ndRenderVals(vals2);
    });
  };
  ndRenderVals([{ via: "", dest: "" }]);
  $("nd-addpage").onclick = () => ndRenderVals([...ndReadVals(), { via: "", dest: "" }]);
  const checkNewCode = async () => {
    const code = $("nd-code").value.trim();
    const box = $("nd-err");
    if (!code) { box.textContent = ""; return null; }
    if (!/^[0-9]{1,8}$/.test(code)) { box.textContent = "Code must be 1–8 digits 0-9."; return null; }
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
      if (!sid) throw new Error("Create a service first (Services tab).");
      const extra = [];
      document.querySelectorAll("[data-nd-page]").forEach((row) => {
        const m = parseInt(row.dataset.ndPage, 10);
        if (m > 1) extra.push({ via: row.querySelector("input[id^=nd-via-]").value.trim(),
                               dest: row.querySelector("input[id^=nd-dest-]").value.trim() });
      });
      await api(`/api/services/${sid}/destinations`, { method: "POST",
        body: { code: $("nd-code").value.trim(), name: $("nd-name").value.trim() || $("nd-code").value.trim(),
                via: ($("nd-via-1") || {}).value ? $("nd-via-1").value.trim() : "",
                dest: ($("nd-dest-1") || {}).value ? $("nd-dest-1").value.trim() : "",
                pages: extra } });
      openProgram(p.id);
    } catch (e) { $("nd-err").textContent = String(e.message || e); }
  };
  $("ns-add").onclick = async () => {
    try {
      await api(`/api/programs/${p.id}/services`, { method: "POST", body: { number: $("ns-num").value, name: $("ns-name").value } });
      openProgram(p.id); loadPrograms();
    } catch (e) { err(String(e.message || e)); }
  };

  // ---- bustimes.org import ----
  wireBustimesImport(p);

  // ---- row actions ----
  const dlg = $("dest-dialog");
  const openEdit = (did) => {
    const found = findDest(did); if (!found) return;
    $("ed-service").innerHTML = (p.services || []).map((s) =>
      `<option value="${s.id}"${s.id === found.s.id ? " selected" : ""}>${esc(s.number)}${s.name ? " - " + esc(s.name) : ""}</option>`).join("");
    $("ed-code").value = found.d.code; $("ed-name").value = found.d.name;
    $("ed-via").value = found.d.via || ""; $("ed-dest").value = found.d.dest || "";
    $("ed-err").textContent = ""; dlg.dataset.did = did;
    (dlg.showModal ? dlg.showModal() : null);
  };
  $("ed-close").onclick = () => { if (dlg) dlg.close(); };
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
  const dupDlg = $("dup-dialog");
  const openDup = async (did) => {
    const found = findDest(did); if (!found) return;
    $("dup-service").innerHTML = (p.services || []).map((s) =>
      `<option value="${s.id}"${s.id === found.s.id ? " selected" : ""}>${esc(s.number)}${s.name ? " - " + esc(s.name) : ""}</option>`).join("");
    $("dup-src").textContent = `Copy ${found.d.code} ${found.d.name || ""} with its stops, board pages and layouts.`;
    $("dup-err").textContent = "";
    try {
      const chk = await api(`/api/programs/${p.id}/destinations/check-code?code=${encodeURIComponent(found.d.code)}`);
      $("dup-code").value = (chk && chk.suggestion) || "";
    } catch (e) { $("dup-code").value = ""; }
    dupDlg.dataset.did = did;
    (dupDlg.showModal ? dupDlg.showModal() : null);
  };
  $("dup-close").onclick = () => { if (dupDlg) dupDlg.close(); };
  $("dup-save").onclick = async (e) => {
    e.preventDefault();
    const did = dupDlg.dataset.did;
    try {
      await api(`/api/destinations/${did}/duplicate`, { method: "POST", body: {
        code: $("dup-code").value.trim(), service_id: $("dup-service").value } });
      dupDlg.close(); openProgram(p.id);
    } catch (ex) { $("dup-err").textContent = String(ex.message || ex); }
  };
  $("pd-body").querySelectorAll("[data-dup]").forEach((b) => b.onclick = () => { LayoutEditor.stop(); openDup(b.dataset.dup); });
  $("pd-body").querySelectorAll("[data-stops]").forEach((b) => b.onclick = () => { LayoutEditor.stop(); editStops(b.dataset.stops); });
  $("pd-body").querySelectorAll("[data-pages]").forEach((b) => b.onclick = () => { LayoutEditor.stop(); editPages(b.dataset.pages); });
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

  // ---- preview wiring ----
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
        : "No stops yet - add stops to preview.";
      $("pv-screens").innerHTML = (s.displays || []).map((dp) => {
        const e = (s.layouts || {})[dp.name] || {};
        return `<div class=col style="min-width:180px"><label>${esc(dp.name)} ${esc(dp.width_px)}×${esc(dp.height_px)} <span class=hint>${esc(e.source || "")}</span></label><div class=led><canvas id="pv-cv-${esc(dp.name)}" data-scale="3" style="max-width:100%"></canvas></div></div>`;
      }).join("") || `<span class=hint>No displays in this program.</span>`;
      const seqHtml = (seq) => seq.length
        ? seq.map((c) => `<button data-pv-clip="${esc(c.sha)}" title="${esc(c.kind)}">${esc(c.label)}</button>`).join("")
        : `<span class=hint>No clips - silent.</span>`;
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
}
function upStatusText(c, pid) {
  const row = ((c.programs || []).find((x) => x.program_id === pid)) || null;
  if (!row || row.desired_version == null) return "not uploaded";
  const inst = row.installed_version, des = row.desired_version, act = row.active_version;
  const sync = c.sync_state;
  if (sync && sync.phase && sync.phase !== "idle")
    return (sync.phase === "audio" && sync.total ? `fetching audio ${esc(sync.done)}/${esc(sync.total)}…`
      : sync.phase === "bundle" ? `downloading v${esc(sync.version)}…`
      : sync.phase === "installing" ? "installing…" : `${esc(sync.phase)}…`);
  if (inst == null) return c.online ? `queued v${esc(des)} - bus downloading…` : `queued v${esc(des)} - waiting for bus`;
  if (inst !== des) return `installing v${esc(des)}… (bus has v${esc(inst)})`;
  const okIcon = `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" style="vertical-align:middle;margin-right:2px"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>`;
  if (act == null || act !== des) return `${okIcon} installed v${esc(des)}` + (act != null ? ` - bus parked on v${esc(act)}, select a destination to switch` : " - bus will adopt it");
  return `${okIcon} live on v${esc(des)}`;
}
async function refreshUploadStatus() {
  try {
    const r = await api("/api/controllers?organisation_id=" + encodeURIComponent(S.org));
    S.controllers = r.controllers || [];
    const pid = S.curProg && S.curProg.id;
    for (const el of document.querySelectorAll("[data-upstat]")) {
      const c = S.controllers.find((x) => x.id === el.dataset.upstat);
      if (c && pid) el.innerHTML = upStatusText(c, pid);
    }
  } catch (e) { /* keep old text on failure */ }
}
function findDest(did) {
  for (const s of (S.curProg.services || [])) for (const d of (s.destinations || [])) if (d.id === did) return { s, d };
  return null;
}
function editStops(did) {
  const found = findDest(did); if (!found) return;
  const d = found.d;
  let h = `<h3>Stops - ${esc(d.name)} (${esc(d.code)})</h3><table><thead><tr><th>#</th><th>Stop name</th><th>Audio clip</th><th>Board text</th><th>Override</th><th>Terminus</th><th></th></tr></thead><tbody id=stoprows>`;
  (d.stops || []).forEach((st, i) => {
    h += `<tr><td>${i + 1}</td>
      <td><input data-f=name value="${esc(st.name)}"></td>
      <td><select data-f=audio>${audioOptions(st.audio || "")}</select></td>
      <td><input data-f=board_text value="${esc(st.board_text || "")}"></td>
      <td><input data-f=board_override value="${esc(st.board_override || "")}"></td>
      <td><input data-f=is_terminus type=checkbox${st.is_terminus ? " checked" : ""}></td>
      <td><button data-delstop="${i}" class=danger title="Delete stop"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg></button></td></tr>`;
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
function editPages(did) {
  const found = findDest(did); if (!found) return;
  const d = found.d;
  const pages = (d.pages && d.pages.length ? d.pages : [{ via: d.via || "", dest: d.dest || "" }]);
  let h = `<h3>Board pages - ${esc(d.name)} (${esc(d.code)})</h3>
    <p class=hint>Each page gets its own via / dest. The boards cycle P1 → P2 → … every few seconds (page time is set in the layout editor). Service number stays on every page.</p>
    <table><thead><tr><th>Page</th><th>Via</th><th>Dest board</th><th></th></tr></thead><tbody id=pagerows>`;
  pages.forEach((pg, i) => {
    h += `<tr data-pgid="${esc(pg.id || "")}"><td>P${i + 1}</td>
      <td><input data-f=via value="${esc(pg.via || "")}"></td>
      <td><input data-f=dest value="${esc(pg.dest || "")}"></td>
      <td>${i > 0 ? `<button data-delpage class=danger title="Delete page">Remove</button>` : `<span class=hint>first page</span>`}</td></tr>`;
  });
  h += `</tbody></table><div class=row><button id=page-add>Add page</button><button id=page-save class=primary>Save pages</button></div>
    <div class=err id=page-err></div>`;
  $("pd-edit").innerHTML = h;
  $("pd-edit").scrollIntoView();
  let deletedIds = [];
  $("pd-edit").querySelectorAll("[data-delpage]").forEach((b) => b.onclick = () => {
    const tr = b.closest("tr");
    if (tr.dataset.pgid) deletedIds.push(tr.dataset.pgid);
    tr.remove();
    [...document.querySelectorAll("#pagerows tr")].forEach((r, i) => r.cells[0].textContent = `P${i + 1}`);
  });
  $("page-add").onclick = () => {
    const tr = document.createElement("tr");
    tr.dataset.pgid = "";
    tr.innerHTML = `<td></td><td><input data-f=via></td><td><input data-f=dest></td>
      <td><button data-delpage class=danger>Remove</button></td>`;
    tr.querySelector("[data-delpage]").onclick = (e) => {
      e.target.closest("tr").remove();
      [...document.querySelectorAll("#pagerows tr")].forEach((r, i) => r.cells[0].textContent = `P${i + 1}`);
    };
    $("pagerows").appendChild(tr);
    [...document.querySelectorAll("#pagerows tr")].forEach((r, i) => r.cells[0].textContent = `P${i + 1}`);
  };
  $("page-save").onclick = async () => {
    try {
      for (const pgid of deletedIds) {
        await api(`/api/destination-pages/${pgid}`, { method: "DELETE" });
      }
      for (const tr of document.querySelectorAll("#pagerows tr")) {
        const via = tr.querySelector("[data-f=via]").value.trim();
        const dest = tr.querySelector("[data-f=dest]").value.trim();
        if (tr.dataset.pgid) {
          await api(`/api/destination-pages/${tr.dataset.pgid}`, { method: "PUT", body: { via, dest } });
        } else {
          await api(`/api/destinations/${did}/pages`, { method: "POST", body: { via, dest } });
        }
      }
      openProgram(S.curProg.id);
    } catch (e) { $("page-err").textContent = String(e.message || e); }
  };
}
async function editLayout(did) {
  const found = findDest(did); if (!found) return;
  LayoutEditor.stop();
  await LayoutEditor.open($("pd-edit"), {
    did, dest: found.d, serviceNumber: (found.s && found.s.number) || "",
    pages: (found.d.pages && found.d.pages.length ? found.d.pages :
      [{ via: found.d.via || "", dest: found.d.dest || "" }]),
    displays: S.curProg.displays || [], apiFn: api, errFn: err,
  });
}

// ---------- bustimes.org import ----------
function bustimesVoiceOptions(selected) {
  const vs = S.voices || [];
  return `<option value="">No voice - stops stay silent</option>` + vs.map((x) =>
    `<option value="${esc(x.id)}"${x.id === selected ? " selected" : ""}>${esc(x.name)}</option>`).join("");
}
function syncSpeakerFor(voiceSelId, speakerSelId) {
  const sbox = $(speakerSelId);
  if (!sbox) return;
  const v = (S.voices || []).find((x) => x.id === $(voiceSelId).value);
  const list = voiceSpeakers(v);
  const prev = sbox.value;
  if (list.length > 1) {
    sbox.innerHTML = list.map((s) => `<option value="${s.id}">${esc(s.name != null ? s.name : s.id)}</option>`).join("");
    if (list.some((s) => String(s.id) === prev)) sbox.value = prev;
    sbox.disabled = false;
  } else {
    sbox.innerHTML = `<option value="">Default speaker</option>`;
    sbox.disabled = true;
  }
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
      if (!rows.length) {
        box.innerHTML = `<p class=hint>No services found for that operator/search.</p>`;
        return;
      }
      const allConfig = `<div class=group style="margin-bottom:14px"><div class=grouptitle>Import all ${esc(rows.length)} shown service(s)</div>
        <div class=row>
          <div style="flex:1"><label for=bt-all-voice>Voice for stop audio</label><select id=bt-all-voice>${bustimesVoiceOptions(localStorage.getItem("nsl_voice") || "")}</select></div>
          <div style="flex:1"><label for=bt-all-speaker>Speaker</label><select id=bt-all-speaker></select></div>
          <div style="flex:1"><label for=bt-all-gen>Stop audio</label><select id=bt-all-gen><option value="yes">Generate missing stop audio</option><option value="no">Skip audio (silent stops)</option></select></div>
          <div style="align-self:end"><button id=bt-all-import class=primary>Import all ${esc(rows.length)} services</button></div></div>
        <div id=bt-all-progress></div><div id=bt-all-result></div><div class=err id=bt-all-err></div></div>`;
      box.innerHTML = allConfig + `<table><thead><tr><th>Service</th><th>Description</th><th>Typical destinations</th><th></th></tr></thead><tbody>` +
        rows.map((s) => `<tr><td><b>${esc(s.line_name)}</b></td><td>${esc(s.description)}</td>` +
          `<td>${(s.destinations || []).map(esc).join(" · ") || "-"}</td>` +
          `<td><button data-bt="${s.id}">Import…</button></td></tr>`).join("") + `</tbody></table>
        <p class=hint>${esc(r.count)} service(s) total. Typical destinations are the description split on " - ".</p>`;
      syncSpeakerFor("bt-all-voice", "bt-all-speaker");
      $("bt-all-voice").addEventListener("change", () => syncSpeakerFor("bt-all-voice", "bt-all-speaker"));
      box.querySelectorAll("[data-bt]").forEach((b) => b.onclick = () => showBustimesDetail(p, b.dataset.bt));
      $("bt-all-import").onclick = async () => {
        const ibtn = $("bt-all-import"), prog = $("bt-all-progress"), res = $("bt-all-result"), ierr = $("bt-all-err");
        const t0 = Date.now();
        const elapsed = () => `${Math.round((Date.now() - t0) / 1000)}s`;
        const btBar = (done, total, stage) => {
          const pct = total ? Math.round(100 * done / total) : 0;
          return `<p class=hint>${esc(stage || "working…")} - ${done}/${total}</p>` +
            `<div style="border:1px solid #888;background:#fff;height:16px;width:100%"><div style="background:#1a5fb4;height:100%;width:${pct}%"></div></div>`;
        };
        try {
          ibtn.disabled = true; ierr.textContent = ""; res.innerHTML = "";
          prog.innerHTML = btBar(0, 1, "sending import request");
          const tick = setInterval(() => { ibtn.textContent = `Importing all… ${elapsed()}`; }, 1000);
          const r = await api("/api/bustimes/import-all", { method: "POST", body: {
            program_id: p.id, operator: $("bt-operator").value.trim(), search: $("bt-search").value.trim(),
            voice_id: $("bt-all-voice").value || null,
            speaker_id: selectedSpeaker("bt-all-speaker"),
            generate_audio: $("bt-all-gen").value !== "no" } });
          clearInterval(tick);
          if (r.state === "done" || !r.job_id) {
            prog.innerHTML = "";
            res.innerHTML = `<div class=group><div class=grouptitle>Import complete</div>` +
              `<p>Imported <b>${esc(r.imported.length)}</b> service(s). Skipped <b>${esc(r.skipped.length)}</b>.</p>` +
              `${r.skipped.length ? `<p class=hint>Skipped: ${esc(r.skipped.map((x) => (x.line_name || x.id || "unknown") + ": " + x.reason).slice(0, 5).join(" | "))}${r.skipped.length > 5 ? "…" : ""}</p>` : ""}` +
              `</div>`;
            openProgram(p.id);
            return;
          }
          let job = null;
          for (;;) {
            await new Promise((done) => setTimeout(done, 1000));
            try { job = await api(`/api/bustimes/jobs/${encodeURIComponent(r.job_id)}`); }
            catch (e) { ierr.textContent = "Lost track of the import job: " + String(e.message || e); break; }
            if (!document.contains(prog)) break;
            prog.innerHTML = btBar(job.done || 0, job.total || 1, `${job.stage || "working"} · ${elapsed()} elapsed`);
            if (job.state === "done" || job.state === "error") break;
          }
          prog.innerHTML = "";
          if (job && job.state === "done") {
            res.innerHTML = `<div class=group><div class=grouptitle>Import complete</div>` +
              `<p>Imported <b>${esc(r.imported.length)}</b> service(s). Audio generated for <b>${job.result.generated || 0}</b> stop(s), reused <b>${job.result.reused || 0}</b>, silent <b>${(job.result.silent || []).length}</b>.</p>` +
              `${(job.result.errors || []).length ? `<p class=err>${esc(job.result.errors.slice(0, 5).join(" | "))}${job.result.errors.length > 5 ? "…" : ""}</p>` : ""}` +
              `</div>`;
            openProgram(p.id);
          } else if (job) {
            ierr.textContent = "Audio generation failed: " + (job.error || "unknown error") + " - services and stops were still imported.";
            openProgram(p.id);
          }
        } catch (e) { ierr.textContent = String(e.message || e); }
        finally { ibtn.disabled = false; ibtn.textContent = `Import all ${esc(rows.length)} services`; }
      };
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
    det.innerHTML = `<div class=group><div class=grouptitle>Import ${esc(d.line_name)} - ${esc(d.description)}</div>
      <div class=row>
      <div style="flex:1"><label for=bt-num>Service number</label><input id=bt-num value="${esc(d.line_name)}"></div>
      <div style="flex:2"><label for=bt-name>Service description</label><input id=bt-name value="${esc(d.description)}"></div></div>
      <div class=row>
      <div style="flex:1"><label for=bt-voice>Voice for stop audio</label><select id=bt-voice>${bustimesVoiceOptions(localStorage.getItem("nsl_voice") || "")}</select></div>
      <div style="flex:1"><label for=bt-speaker>Speaker</label><select id=bt-speaker></select></div>
      <div style="flex:1"><label for=bt-gen>Stop audio</label><select id=bt-gen><option value="yes">Generate missing stop audio</option><option value="no">Skip audio (silent stops)</option></select></div></div>
      <p class=hint>${(d.directions || []).map((g) => `Direction ${g.index + 1}: ${esc(g.from)} → ${esc(g.to)} - ${g.count} stops`).join("<br>") || "No timetable directions."}</p>
      <table><thead><tr><th>Destination name</th><th>Code</th><th>Via</th><th>Dest board</th><th>Timetable stops</th></tr></thead>
      <tbody>${destRows}</tbody></table>
      <div class=row><button id=bt-confirm class=primary>Import service</button></div>
      <div id=bt-progress></div>
      <div id=bt-result></div>
      <div class=err id=bt-import-err></div></div>`;
    const syncBtSpeaker = () => {
      const box = $("bt-speaker");
      if (!box) return;
      const v = (S.voices || []).find((x) => x.id === $("bt-voice").value);
      const list = voiceSpeakers(v);
      const prev = box.value;
      if (list.length > 1) {
        box.innerHTML = list.map((s) => `<option value="${s.id}">${esc(s.name != null ? s.name : s.id)}</option>`).join("");
        if (list.some((s) => String(s.id) === prev)) box.value = prev;
        box.disabled = false;
      } else {
        box.innerHTML = `<option value="">Default speaker</option>`;
        box.disabled = true;
      }
    };
    $("bt-voice").addEventListener("change", syncBtSpeaker);
    syncBtSpeaker();
    const btBar = (done, total, stage) => {
      const pct = total ? Math.round(100 * done / total) : 0;
      return `<p class=hint>${esc(stage || "working…")} - ${done}/${total}</p>` +
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
          `<li><span class=kbd>${esc(x.code)}</span> ${esc(x.name)} - ${x.stops} stop(s)</li>`).join("");
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
          speaker_id: selectedSpeaker("bt-speaker"),
          generate_audio: $("bt-gen").value !== "no",
          destinations: dests } });
        if (r.state === "done" || !r.job_id) {
          prog.innerHTML = "";
          res.innerHTML = confirmHtml(r, r.audio || {});
          openProgram(p.id);
          return;
        }
        // Audio generates in the background - poll for live progress.
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
            " - the service and stops were still imported; retry audio from the Audio tab.";
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
    const status = c.online ? `<span class="badge ok">ONLINE</span>` : `<span class="badge err">OFFLINE</span><br><span class=hint>last seen ${esc(last)}</span>`;
    const handshake = c.awaiting_pi ? ` <span class=err>waiting for Pi - pair done, Pi hasn't collected its token yet</span>` : "";
    const progs = (c.programs || []).map((p) => {
      const bits = [`want v${p.desired_version}`, `bus v${p.installed_version == null ? "-" : p.installed_version}`];
      if (p.active_version != null) bits.push(`<b class=ok>active v${p.active_version}</b>`);
      if (p.status && p.status !== "installed") bits.push(`(${esc(p.status)})`);
      let warn = "";
      if (p.active_version != null && p.desired_version != null && p.active_version !== p.desired_version)
        warn = ` <span class=err>bus parked on v${p.active_version} - select a v${p.desired_version} destination to switch</span>`;
      return `${esc(p.name || p.program_id)}: ${bits.join(" / ")}${warn}`;
    }).join("<br>") || "-";
    const pend = c.pending_commands ? ` · ${c.pending_commands} cmd(s) waiting` : "";
    return `<tr><td><b>${esc(c.name)}</b></td><td>${status}${esc(pend)}${handshake}</td><td>${esc(last)}</td><td>${esc(c.app_version || "")}</td><td>${progs}</td>
    <td class=row><button data-rename="${c.id}">Rename</button><button data-retoken="${c.id}" title="Issue a new device token (old one stops working)">Rotate token</button><button class=danger data-unpair="${c.id}" title="Remove this controller entirely - token revoked, disappears from the portal">Remove</button></td></tr>`;
  }).join("");
  $("rc-ctl").innerHTML = S.controllers.map((c) => `<option value="${c.id}">${esc(c.name)} (${c.online ? "online" : "offline"})</option>`).join("");
  await loadReleases();
}
async function loadReleases() {
  try {
    const r = await api("/api/admin/releases?organisation_id=" + encodeURIComponent(S.org));
    const rows = (r.releases || []).map((rel) => {
      const bytes = rel.bundle_sha256 ? (rel.bytes ? fmtBytes(rel.bytes) : "-") : "-";
      return `<tr><td><b>${esc(rel.version)}</b></td><td>${esc(rel.channel)}</td><td>${esc(bytes)}</td><td>${esc(rel.created_at || "")}</td></tr>`;
    }).join("");
    $("relrows").innerHTML = rows || `<tr><td colspan=4 class=hint>No releases uploaded yet.</td></tr>`;
  } catch (e) { $("relrows").innerHTML = `<tr><td colspan=4 class=err>Could not load releases: ${esc(String(e.message || e))}</td></tr>`; }
}
function fmtBytes(n) {
  if (n == null) return "-";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
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
  $("token-sub").innerHTML = `Controller “${esc(name || "controller")}” is paired. The Pi collects this token itself within ~10 s - watch the row flip from “waiting for Pi” to online. Only use the command below as a manual fallback. Lose it and use “Rotate token” for a new one.`;
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
    catch (e2) { err("Copy failed - select the token manually (Ctrl+C)."); }
  }
  setTimeout(() => { $("btn-token-copy").textContent = "Copy"; }, 2000);
});
$("btn-token-close").addEventListener("click", () => { const d = $("token-dialog"); if (d) d.close(); });
$("btn-pair").addEventListener("click", async () => {
  try {
    const name = $("pair-name").value;
    const dup = (S.controllers || []).some((c) => (c.name || "").toLowerCase() === name.trim().toLowerCase());
    if (dup && !confirm(`A controller named “${name.trim()}” already exists (check the table - the other row may be a ghost that never checked in). Pair another one with the same name?`)) return;
    const r = await api("/api/controllers/pair", { method: "POST",
      body: formData({ organisation_id: S.org, code: $("pair-code").value.trim().toUpperCase(), name: $("pair-name").value }) });
    $("pair-code").value = ""; $("pair-name").value = ""; loadControllers();
    if (r && r.device_token) showTokenDialog(name || "controller", r.device_token);
    else err("Paired, but the server did not return a device token - use “Rotate token” to issue one.");
  } catch (e) { err(String(e.message || e)); }
});
$("btn-sim").addEventListener("click", async () => {
  try {
    const r = await api("/api/device/register", { method: "POST", body: {} });
    $("pair-code").value = r.pairing_code;
    alert("Simulated device registered. Pairing code filled in - enter a name and press Pair controller.");
  } catch (e) { err(String(e.message || e)); }
});
$("btn-rel-upload").addEventListener("click", async () => {
  try {
    const version = $("rel-version").value.trim();
    const channel = $("rel-channel").value;
    const file = $("rel-file").files[0];
    if (!version || !file) throw new Error("Version and bundle file are required.");
    const fd = new FormData();
    fd.append("organisation_id", S.org);
    fd.append("version", version);
    fd.append("channel", channel);
    fd.append("file", file);
    const r = await api("/api/admin/releases", { method: "POST", body: fd });
    $("rel-version").value = "";
    $("rel-file").value = "";
    $("rel-msg").innerHTML = `Uploaded release <b>${esc(r.version)}</b> (${esc(r.channel)}) - ${fmtBytes(r.bytes)}. Buses on channel ${esc(r.channel)} will pull it on their next sync.`;
    await loadReleases();
  } catch (e) { $("rel-msg").innerHTML = `<span class=err>Upload failed: ${esc(String(e.message || e))}</span>`; }
});

// ---------- audio & voices ----------
async function loadAudio() {
  const r = await api("/api/audio?organisation_id=" + encodeURIComponent(S.org));
  S.clips = r.clips || [];
  const srcLabel = (a) => a.source === "tts"
    ? (a.placeholder ? "text-to-speech (preview tone)" : "text-to-speech")
    : (a.source || "upload");
  const durLabel = (a) => (a.duration_ms != null ? (Number(a.duration_ms) / 1000).toFixed(1) + "s" : "-");
  $("audiorows").innerHTML = S.clips.map((a) =>
    `<tr><td><b>${esc(clipLabel(a))}</b>` +
    (a.placeholder ? ` <span class=hint>preview tone</span>` : "") +
    (a.silent_legacy ? ` <span class=err>silent - regenerate</span>` : "") + `</td>` +
    `<td>${esc(srcLabel(a))} · ${esc(durLabel(a))}</td>` +
    `<td><button data-play="${esc(a.sha256)}">Play</button></td>` +
    `<td class=row><button data-rename="${esc(a.sha256)}">Rename</button><button data-delclip="${esc(a.sha256)}">Delete</button></td></tr>`).join("") ||
    `<tr><td colspan=4 class=hint>No clips yet - upload one or generate it from text below.</td></tr>`;
  const v = await api("/api/voices?organisation_id=" + encodeURIComponent(S.org));
  S.voices = v.voices || [];
  S.piper_available = v.piper_available;
  $("voicerows").innerHTML = (v.voices || []).map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.language || "")}</td><td>${esc(x.sample_rate || "")}</td><td>${esc(speakerLabel(x))}</td></tr>`).join("") ||
    `<tr><td colspan=4 class=hint>No voices uploaded yet.</td></tr>`;
  if (v.piper_available === false) {
    err("Piper binary not found on the server - voice generation will fall back to preview tone. Install piper-tts + espeak-ng on the server.");
  }
  const prevVoice = $("gen-voice").value || localStorage.getItem("nsl_voice") || "";
  const voices = v.voices || [];
  const stillThere = voices.some((x) => x.id === prevVoice);
  const sel = stillThere ? prevVoice : (voices.length === 1 ? voices[0].id : "");
  const emptyLabel = voices.length ? "Preview tone (no voice) - select a voice for real speech"
    : "Preview tone (no voice installed)";
  $("gen-voice").innerHTML = `<option value="">${esc(emptyLabel)}</option>` + voices.map((x) => `<option value="${x.id}"${x.id === sel ? " selected" : ""}>${esc(x.name)}</option>`).join("");
  if (sel) localStorage.setItem("nsl_voice", sel);
  syncGenSpeaker();
}
function voiceSpeakers(v) {
  if (!v) return [];
  try {
    const list = JSON.parse(v.speakers || "[]");
    return Array.isArray(list) ? list : [];
  } catch (e) { return []; }
}
function speakerLabel(x) {
  const list = voiceSpeakers(x);
  if (!list.length) return "1";
  return `${list.length} — ` + list.map((s) => s.name != null ? s.name : s.id).join(", ");
}
function syncGenSpeaker() {
  const box = $("gen-speaker");
  if (!box) return;
  const v = (S.voices || []).find((x) => x.id === $("gen-voice").value);
  const list = voiceSpeakers(v);
  const prev = box.value;
  if (list.length > 1) {
    box.innerHTML = list.map((s) => `<option value="${s.id}">${esc(s.name != null ? s.name : s.id)}</option>`).join("");
    if (list.some((s) => String(s.id) === prev)) box.value = prev;
    box.disabled = false;
    box.title = "";
  } else {
    box.innerHTML = `<option value="">Default speaker</option>`;
    box.disabled = true;
    box.title = "This voice has a single speaker";
  }
}
function selectedSpeaker(boxId) {
  const box = $(boxId);
  if (!box || box.disabled || box.value === "") return null;
  const n = parseInt(box.value, 10);
  return isNaN(n) ? null : n;
}
$("gen-voice").addEventListener("change", () => { localStorage.setItem("nsl_voice", $("gen-voice").value || ""); syncGenSpeaker(); });
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
      const delSha = b.dataset.delclip;
      const delClip = S.clips.find((a) => a.sha256 === delSha);
      try {
        const uses = await api(`/api/audio/${encodeURIComponent(delSha)}/usages?organisation_id=${encodeURIComponent(S.org)}`);
        if (!uses.stop_count && !(uses.services || []).length) {
          if (!confirm(`Delete clip "${clipLabel(delClip)}"?`)) return;
          await api(`/api/audio/${encodeURIComponent(delSha)}?organisation_id=${encodeURIComponent(S.org)}`, { method: "DELETE" });
          loadAudio();
          return;
        }
        const stopLines = (uses.stops || []).map((s) =>
          `${s.stop_name} (${s.service_number} · ${s.dest_code} · ${s.program_name})`);
        if (uses.stop_count > stopLines.length) stopLines.push(`…and ${uses.stop_count - stopLines.length} more`);
        const svcLines = (uses.services || []).map((s) =>
          `${s.field} on service ${s.service_number || "?"}${s.service_name ? " - " + s.service_name : ""} (${s.program_name})`);
        $("cdd-title").textContent = `Delete clip "${clipLabel(delClip)}"?`;
        $("cdd-uses").innerHTML =
          (stopLines.length ? `<div><b>Stops (${uses.stop_count}):</b><br>${stopLines.map(esc).join("<br>")}</div>` : "") +
          (svcLines.length ? `<div><b>Announcements:</b><br>${svcLines.map(esc).join("<br>")}</div>` : "");
        $("cdd-replace").innerHTML = `<option value="">Silent (no audio)</option>` +
          (S.clips || []).filter((a) => a.sha256 !== delSha).map((a) =>
            `<option value="${esc(a.sha256)}">${esc(clipLabel(a))}</option>`).join("");
        $("cdd-err").textContent = "";
        pendingDelSha = delSha;
        const dlg = $("clip-del-dialog");
        if (dlg && dlg.showModal) dlg.showModal();
      } catch (ex) { err(String(ex.message || ex)); }
    }
  } catch (ex) { err(String(ex.message || ex)); }
});
let pendingDelSha = "";
$("cdd-cancel").addEventListener("click", () => { const d = $("clip-del-dialog"); if (d) d.close(); });
$("cdd-confirm").addEventListener("click", async () => {
  const sha = pendingDelSha;
  if (!sha) return;
  try {
    $("cdd-err").textContent = "";
    const rep = $("cdd-replace").value;
    const r = await api(`/api/audio/${encodeURIComponent(sha)}?organisation_id=${encodeURIComponent(S.org)}&replacement=${encodeURIComponent(rep)}`, { method: "DELETE" });
    const dlg = $("clip-del-dialog");
    if (dlg) dlg.close();
    pendingDelSha = "";
    err("");
    await loadAudio();
    if (S.curProg) openProgram(S.curProg.id);
    const bits = [];
    if (r.replaced_stops) bits.push(`${r.replaced_stops} stop(s)`);
    if (r.replaced_services) bits.push(`${r.replaced_services} announcement slot(s)`);
    alert(bits.length ? `Deleted. ${bits.join(" and ")} now use ${rep ? "the replacement clip" : "silence"}.` : "Deleted.");
  } catch (ex) { $("cdd-err").textContent = String(ex.message || ex); }
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
      speaker_id: selectedSpeaker("gen-speaker"),
      speed: parseFloat($("gen-speed").value || "1") } });
    alert(r.placeholder
      ? `Saved as "${r.name}" (preview tone - ${r.note || "upload a Piper voice for real speech"}).`
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
    alert(`Voice "${$("vv-name").value.trim()}" uploaded.` +
      (r.num_speakers > 1 ? ` ${r.num_speakers} speakers detected - pick one in the Speaker dropdown.` : ""));
    $("vv-name").value = ""; $("vv-model").value = ""; $("vv-config").value = ""; loadAudio();
  } catch (e) { err(String(e.message || e)); }
});

// ---------- remote ----------
async function rcSend(body) {
  const cid = $("rc-ctl").value;
  if (!cid) throw new Error("Choose a controller first.");
  const c = S.controllers.find((x) => x.id === cid);
  if (c && !c.online) throw new Error(`“${c.name}” is offline (last seen ${c.last_seen_at || "never"}) - command refused, nothing was queued.`);
  const r = await api(`/api/controllers/${cid}/command`, { method: "POST", body });
  const when = new Date().toLocaleTimeString();
  const msg = `Sent to “${c ? c.name : cid}” at ${when} - the bus picks it up within ~10 s.`;
  $("rc-state").textContent = msg;
  if ($("rc-result")) $("rc-result").textContent = msg;
  refreshLive();
  return r;
}
function rcLiveHtml(l) {
  if (!l) return "Select a controller to see its current stop.";
  const stale = l.stale ? ` <span class=err>(last known - bus offline, last seen ${esc(l.last_seen_at || "never")})</span>` : "";
  if (!l.program) return `Idle - no program selected on the bus.${stale}`;
  const dest = l.destination
    ? `<span class=kbd>${esc(l.destination.code || "")}</span> ${esc(l.destination.name || "")}` : "-";
  const svc = l.service ? `Service <b>${esc(l.service.number || "")}</b> · ` : "";
  const stop = l.stop
    ? `<br>Current stop <b>${l.stop.position}/${l.stop.count}: ${esc(l.stop.board_text || l.stop.name)}</b>` +
      (l.stop.is_terminus ? ` <span class=hint>(terminus)</span>` : "") +
      (l.next_stop ? `<br>Next stop: ${esc(l.next_stop.name)}` : `<br>Next stop: - (end of route)`)
    : "<br>Current stop: -";
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
async function refreshUpdateStatus() {
  const box = $("rc-update");
  if (!box) return;
  const cid = $("rc-ctl") && $("rc-ctl").value;
  const btn = $("btn-rc-update");
  if (!cid) { box.textContent = "Select a controller to see update status."; if (btn) btn.disabled = true; return; }
  try {
    const s = await api(`/api/controllers/${encodeURIComponent(cid)}/update`);
    if (!s.latest_version) {
      box.innerHTML = `Current version: <b>${esc(s.current_version || "unknown")}</b>. No release published for channel <b>${esc(s.channel)}</b>.`;
      if (btn) btn.disabled = true;
    } else if (s.up_to_date) {
      box.innerHTML = `Up to date on version <b>${esc(s.current_version)}</b> (channel ${esc(s.channel)}).`;
      if (btn) btn.disabled = true;
    } else {
      box.innerHTML = `Current version: <b>${esc(s.current_version || "unknown")}</b>. Latest release: <b>${esc(s.latest_version)}</b> (channel ${esc(s.channel)}).`;
      if (btn) btn.disabled = !s.can_update;
    }
  } catch (e) {
    box.innerHTML = `<span class=err>Update status unavailable: ${esc(String(e.message || e))}</span>`;
    if (btn) btn.disabled = true;
  }
}
// Live poll while the remote tab is on screen; the bus reports state on
// heartbeat, so a sent command appears here once the bus picks it up.
if (!window._rcLiveTimer) {
  window._rcLiveTimer = setInterval(() => {
    try {
      if (S.token && $("tab-remote") && $("tab-remote").style.display !== "none" && $("rc-ctl") && $("rc-ctl").value) {
        refreshLive();
        refreshUpdateStatus();
      }
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
  $("rc-state").textContent = c ? `${c.name}: ${c.online ? "ONLINE" : "OFFLINE"} - last seen ${c.last_seen_at || "never"}, app ${c.app_version || "?"}, ${c.pending_commands || 0} command(s) waiting.` : "Select a controller.";
  refreshLive();
  refreshUpdateStatus();
});
$("rc-ctl").addEventListener("change", () => { refreshLive(); refreshUpdateStatus(); });
rc("btn-rc-update", async () => {
  const cid = $("rc-ctl").value;
  const c = S.controllers.find((x) => x.id === cid);
  if (c && !c.online) throw new Error(`“${c.name}” is offline - update refused.`);
  const s = await api(`/api/controllers/${encodeURIComponent(cid)}/update`);
  if (!s.latest_version || s.up_to_date) throw new Error("No update available.");
  const r = await rcSend({ command: "update_now" });
  $("rc-update").innerHTML = `Update to <b>${esc(s.latest_version)}</b> queued. The bus will download, verify, and restart automatically. Watch the controller status to see it come back online.`;
  return r;
});

// ---------- TfL audio creator (search + trim + save to library) ----------
const Tfl = (() => {
  let file = "", label = "", lib = "", buf = null, objUrl = "", actx = null, src = null;
  const terr = (m) => { $("tfl-err").textContent = m || ""; };
  function fmt(s) { return (Number(s) || 0).toFixed(2) + "s"; }
  function syncDur() {
    const a = parseFloat($("tfl-start").value || "0"), b = parseFloat($("tfl-end").value || "0");
    $("tfl-dur").textContent = buf ? `${fmt(a)} → ${fmt(b)} (keeps ${fmt(Math.max(0, b - a))} of ${fmt(buf.duration)})` : "-";
  }
  function draw() {
    const cv = $("tfl-wave");
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth || 600, H = 110;
    cv.width = W * dpr; cv.height = H * dpr;
    const g = cv.getContext("2d");
    g.scale(dpr, dpr);
    g.fillStyle = "#111"; g.fillRect(0, 0, W, H);
    if (!buf) return;
    const ch = buf.getChannelData(0);
    const a = Math.max(0, parseFloat($("tfl-start").value || "0")), b = Math.min(buf.duration, parseFloat($("tfl-end").value || "0"));
    const peak = (x0, x1) => {
      const i0 = Math.floor(x0 / buf.duration * ch.length), i1 = Math.max(i0 + 1, Math.floor(x1 / buf.duration * ch.length));
      let m = 0;
      for (let i = i0; i < i1; i += Math.max(1, Math.floor((i1 - i0) / 40))) m = Math.max(m, Math.abs(ch[i]));
      return m;
    };
    const step = buf.duration / W;
    for (let x = 0; x < W; x++) {
      const t0 = x * step, m = peak(t0, t0 + step);
      const h = Math.max(1, m * H * 0.95);
      const sel = t0 >= a && t0 <= b;
      g.fillStyle = sel ? "#FFB000" : "#666";
      g.fillRect(x, (H - h) / 2, 1, h);
    }
    g.fillStyle = "rgba(255,255,255,.6)";
    g.fillRect(a / buf.duration * W - 1, 0, 2, H);
    g.fillRect(b / buf.duration * W - 1, 0, 2, H);
  }
  function setRange(a, b) {
    if (!buf) return;
    a = Math.max(0, Math.min(buf.duration, a)); b = Math.max(0, Math.min(buf.duration, b));
    if (b < a) [a, b] = [b, a];
    $("tfl-start").value = a.toFixed(2); $("tfl-end").value = b.toFixed(2);
    $("tfl-start").max = buf.duration.toFixed(2); $("tfl-end").max = buf.duration.toFixed(2);
    syncDur(); draw();
  }
  async function ensureCtx() {
    if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
    if (actx.state === "suspended") await actx.resume();
    return actx;
  }
  async function load(f, lab, folder) {
    terr(""); $("tfl-save-msg").textContent = "";
    try {
      const r = await fetch(PREFIX + `/api/tfl/file?organisation_id=${encodeURIComponent(S.org)}&file=${encodeURIComponent(f)}`, { headers: apiHeaders() });
      if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail || ("HTTP " + r.status));
      const blob = await r.blob();
      if (objUrl) URL.revokeObjectURL(objUrl);
      objUrl = URL.createObjectURL(blob);
      $("tfl-audio").src = objUrl;
      const ab = await blob.arrayBuffer();
      const cx = await ensureCtx();
      buf = await cx.decodeAudioData(ab.slice(0));
      file = f; label = lab; lib = folder || "";
      $("tfl-editor").style.display = "";
      $("tfl-title").textContent = "Trim - " + lab + (lib ? ` (${lib})` : "");
      $("tfl-start").value = "0.00"; $("tfl-end").value = buf.duration.toFixed(2);
      $("tfl-name").value = slugifyClip(lab);
      $("tfl-err").textContent = "";
      syncDur(); draw();
      $("tfl-editor").scrollIntoView();
    } catch (e) { terr(String(e.message || e)); }
  }
  async function search() {
    terr(""); $("tfl-save-msg").textContent = "";
    try {
      const q = $("tfl-q").value.trim();
      const folder = $("tfl-folder") ? $("tfl-folder").value : "";
      const r = await api(`/api/tfl/search?organisation_id=${encodeURIComponent(S.org)}&q=${encodeURIComponent(q)}&limit=100&folder=${encodeURIComponent(folder)}`);
      $("tfl-count").textContent = r.count ? `${r.count} match(es)${r.count > r.results.length ? ` - showing first ${r.results.length}` : ""}` : "No matches - try a shorter word.";
      $("tfl-rows").innerHTML = (r.results || []).map((x) =>
        `<tr><td>${esc(x.label)}</td><td class=hint>${esc(x.folder || "")}</td><td class=row><button data-tfl-load="${esc(x.file)}" data-lab="${esc(x.label)}" data-folder="${esc(x.folder || "")}">Load</button><button data-tfl-add="${esc(x.file)}" data-lab="${esc(x.label)}" data-folder="${esc(x.folder || "")}" title="Add the whole file to the merge list">+ Merge</button></td></tr>`).join("")
        || `<tr><td colspan=3 class=hint>Type a word and press Search.</td></tr>`;
      $("tfl-rows").querySelectorAll("[data-tfl-load]").forEach((b) => b.onclick = () => load(b.dataset.tflLoad, b.dataset.lab, b.dataset.folder));
      $("tfl-rows").querySelectorAll("[data-tfl-add]").forEach((b) => b.onclick = () => addFullToMerge(b.dataset.tflAdd, b.dataset.lab, b.dataset.folder));
    } catch (e) { terr(String(e.message || e)); }
  }
  function stopSrc() { try { src && src.stop(); } catch (e) { /* already stopped */ } src = null; }
  async function playSel() {
    terr("");
    if (!buf) { terr("Load a file first."); return; }
    const a = parseFloat($("tfl-start").value || "0"), b = parseFloat($("tfl-end").value || "0");
    if (!(b > a)) { terr("Selection is empty - drag a region on the waveform first."); return; }
    try {
      const cx = await ensureCtx();
      stopSrc(); stopMergePlay();
      src = cx.createBufferSource(); src.buffer = buf;
      src.connect(cx.destination);
      src.start(0, a, Math.min(b - a, buf.duration - a));
    } catch (e) { terr(String(e.message || e)); }
  }
  async function save() {
    terr(""); $("tfl-save-msg").textContent = "";
    if (!file || !buf) { terr("Load a file first."); return; }
    const a = parseFloat($("tfl-start").value || "0"), b = parseFloat($("tfl-end").value || "0");
    const s = assertClipName($("tfl-name").value, $("tfl-name-err"));
    if (!s) { terr("Give this clip a name, e.g. morrisons-wood-lane."); return; }
    try {
      $("tfl-save-msg").textContent = "Cutting & uploading…";
      const r = await api("/api/tfl/clip", { method: "POST", body: {
        organisation_id: S.org, source: file,
        start_ms: Math.round(a * 1000), end_ms: Math.round(b * 1000), name: s } });
      $("tfl-save-msg").textContent = `Saved as "${r.name}" (${(r.duration_ms / 1000).toFixed(1)}s) - now in your Audio library, pick it in the stops portal.`;
      await loadAudio();
      if (S.curProg) openProgram(S.curProg.id);
    } catch (e) { terr(String(e.message || e)); $("tfl-save-msg").textContent = ""; }
  }
  // ---------- merge timeline (tweakable parts + per-join crossfades) ----------
  let mergeParts = [];   // [{file, label, start, end, buf}]
  let mergeJoins = [];   // one per boundary: {gap (ms silence), xfade (ms overlap)}
  let editingIndex = -1; // part currently loaded back in the trimmer for tweaking
  let mergePlaySrc = null;
  const merr = (m) => { $("tfl-merge-err").textContent = m || ""; };
  function stopMergePlay() { try { mergePlaySrc && mergePlaySrc.stop(); } catch (e) { /* already stopped */ } mergePlaySrc = null; }
  function syncAddBtn() {
    const b = $("btn-tfl-add-merge");
    if (b) b.textContent = editingIndex >= 0 ? `Update part #${editingIndex + 1}` : "Add selection to merge";
  }
  function slicePart(p) {
    const ch = p.buf.getChannelData(0), sr = p.buf.sampleRate;
    const s0 = Math.max(0, Math.floor(p.start * sr)), s1 = Math.min(ch.length, Math.ceil(p.end * sr));
    if (s1 - s0 < 32) return null;
    return ch.slice(s0, s1);
  }
  // Mirror of the server mix (equal-power crossfades, gap silence, edge fades)
  // so the preview is exactly what save will produce.
  function buildMerged() {
    if (mergeParts.length < 2) return null;
    const sr = mergeParts[0].buf.sampleRate, micro = Math.floor(sr * 0.005);
    const slices = mergeParts.map(slicePart);
    if (slices.some((s) => !s)) return null;
    let out = Array.from(slices[0]);
    const ticks = [];
    for (let i = 1; i < slices.length; i++) {
      const j = mergeJoins[i - 1] || { gap: 120, xfade: 0 };
      const seg = Array.from(slices[i]);
      if (j.xfade > 0) {
        const ov = Math.min(Math.round(j.xfade / 1000 * sr), out.length, seg.length);
        if (ov < 32) return { error: `crossfade on join ${i} is longer than the words either side - shorten it` };
        const mixed = new Array(out.length - ov + seg.length);
        for (let k = 0; k < out.length - ov; k++) mixed[k] = out[k];
        for (let k = 0; k < ov; k++) {
          const t = k / Math.max(1, ov - 1);
          mixed[out.length - ov + k] = out[out.length - ov + k] * Math.cos(t * Math.PI / 2) + seg[k] * Math.sin(t * Math.PI / 2);
        }
        for (let k = ov; k < seg.length; k++) mixed[out.length - ov + k] = seg[k];
        ticks.push(out.length - ov / 2);
        out = mixed;
      } else {
        const f = Math.min(micro, out.length >> 1, seg.length >> 1);
        for (let k = 0; k < f; k++) { out[out.length - f + k] *= (f - k) / f; seg[k] *= (k + 1) / f; }
        const g = Math.round(Math.max(0, Math.min(2000, j.gap)) / 1000 * sr);
        ticks.push(out.length + g / 2);
        out = out.concat(new Array(g).fill(0), seg);
      }
    }
    const edge = Math.min(Math.floor(out.length / 4), Math.floor(sr * 0.008));
    for (let k = 0; k < edge; k++) { out[k] *= k / edge; out[out.length - 1 - k] *= k / edge; }
    return { data: out, sr, ticks, total: out.length / sr };
  }
  function drawMerged(m) {
    const cv = $("tfl-merge-wave");
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth || 600, H = 70;
    cv.width = W * dpr; cv.height = H * dpr;
    const g = cv.getContext("2d");
    g.scale(dpr, dpr);
    g.fillStyle = "#111"; g.fillRect(0, 0, W, H);
    if (!m || !m.data || !m.data.length) return;
    const n = m.data.length;
    let peak = 0;
    for (let i = 0; i < n; i += 7) peak = Math.max(peak, Math.abs(m.data[i]));
    if (!peak) return;
    for (let x = 0; x < W; x++) {
      const i = Math.floor(x / W * n);
      const hgt = Math.max(1, Math.abs(m.data[i]) / peak * H * 0.95);
      g.fillStyle = "#FFB000";
      g.fillRect(x, (H - hgt) / 2, 1, hgt);
    }
    g.fillStyle = "rgba(255,255,255,.7)";
    for (const t of (m.ticks || [])) g.fillRect(t / n * W - 1, 0, 2, H);
  }
  function rebuildMergePreview() {
    merr("");
    if (mergeParts.length < 2) {
      drawMerged(null);
      $("tfl-merge-total").textContent = mergeParts.length ? "Add at least 2 parts to merge." : "";
      return;
    }
    const m = buildMerged();
    if (!m) { drawMerged(null); $("tfl-merge-total").textContent = "A part has an empty selection - fix its trim."; return; }
    if (m.error) { drawMerged(null); $("tfl-merge-total").textContent = m.error; return; }
    drawMerged(m);
    const jd = mergeJoins.map((j) => j.xfade > 0 ? `${Math.round(j.xfade)}ms crossfade` : `${Math.round(j.gap)}ms gap`).join(" + ");
    $("tfl-merge-total").textContent = `${mergeParts.length} parts · ${m.total.toFixed(2)}s total · ${jd}`;
  }
  function renderMerge() {
    stopMergePlay();
    const box = $("tfl-merge-list");
    if (!mergeParts.length) {
      box.innerHTML = `No parts yet - add selections with the button above, or “+ Merge” a whole file from the search results.`;
      drawMerged(null);
      $("tfl-merge-total").textContent = "";
      return;
    }
    let h = `<table><thead><tr><th>#</th><th>Word</th><th>Trim (tweak me)</th><th></th></tr></thead><tbody>`;
    mergeParts.forEach((p, i) => {
      const j = mergeJoins[i];
      h += `<tr${editingIndex === i ? ` class=editing` : ""}><td>${i + 1}</td><td>${esc(p.label)} <span class=hint>${esc(p.folder || "")}</span></td>` +
        `<td><input type=number step=0.01 min=0 max=${p.buf.duration.toFixed(2)} value=${p.start.toFixed(2)} data-mstart="${i}" style="width:64px" title="Part start (s)">–` +
        `<input type=number step=0.01 min=0 max=${p.buf.duration.toFixed(2)} value=${p.end.toFixed(2)} data-mend="${i}" style="width:64px" title="Part end (s)">s</td>` +
        `<td class=row><button data-msolo="${i}" title="Play just this part"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></button>` +
        `<button data-mtrim="${i}" title="Load back into the trimmer for visual tweaking">${editingIndex === i ? "Cancel" : "Trim"}</button>` +
        `<button data-mup="${i}"${i === 0 ? " disabled" : ""} title="Move up">↑</button>` +
        `<button data-mdown="${i}"${i === mergeParts.length - 1 ? " disabled" : ""} title="Move down">↓</button>` +
        `<button data-mdel="${i}" title="Remove" class=danger><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg></button></td></tr>`;
      if (j) {
        h += `<tr><td></td><td colspan=2><span class=hint>transition →</span> ` +
          `gap <input type=number min=0 max=2000 step=10 value=${Math.round(j.gap)} data-jgap="${i}" style="width:60px" title="Silence between these words in ms (ignored when crossfade > 0)">ms ` +
          `<span class=hint>or crossfade</span> ` +
          `<input type=number min=0 max=2000 step=10 value=${Math.round(j.xfade)} data-jxfade="${i}" style="width:60px" title="Overlap the words with an equal-power crossfade in ms">ms</td><td></td></tr>`;
      }
    });
    box.innerHTML = h + `</tbody></table>`;
    box.querySelectorAll("[data-mstart]").forEach((el) => el.addEventListener("input", () => {
      const p = mergeParts[+el.dataset.mstart];
      p.start = Math.max(0, Math.min(p.buf.duration, parseFloat(el.value || "0")));
      rebuildMergePreview();
    }));
    box.querySelectorAll("[data-mend]").forEach((el) => el.addEventListener("input", () => {
      const p = mergeParts[+el.dataset.mend];
      p.end = Math.max(0, Math.min(p.buf.duration, parseFloat(el.value || "0")));
      rebuildMergePreview();
    }));
    box.querySelectorAll("[data-msolo]").forEach((b) => b.onclick = () => soloPart(+b.dataset.msolo));
    box.querySelectorAll("[data-mtrim]").forEach((b) => b.onclick = () => trimPart(+b.dataset.mtrim));
    box.querySelectorAll("[data-mup]").forEach((b) => b.onclick = () => {
      const i = +b.dataset.mup;
      [mergeParts[i - 1], mergeParts[i]] = [mergeParts[i], mergeParts[i - 1]];
      editingIndex = -1; syncAddBtn(); renderMerge(); // joins stay positional
    });
    box.querySelectorAll("[data-mdown]").forEach((b) => b.onclick = () => {
      const i = +b.dataset.mdown;
      [mergeParts[i + 1], mergeParts[i]] = [mergeParts[i], mergeParts[i + 1]];
      editingIndex = -1; syncAddBtn(); renderMerge();
    });
    box.querySelectorAll("[data-mdel]").forEach((b) => b.onclick = () => {
      const i = +b.dataset.mdel;
      mergeParts.splice(i, 1);
      if (i < mergeJoins.length) mergeJoins.splice(i, 1); else mergeJoins.splice(i - 1, 1);
      if (editingIndex === i) editingIndex = -1; else if (editingIndex > i) editingIndex--;
      syncAddBtn(); renderMerge();
    });
    box.querySelectorAll("[data-jgap]").forEach((el) => el.addEventListener("input", () => {
      mergeJoins[+el.dataset.jgap].gap = Math.max(0, Math.min(2000, parseFloat(el.value || "0") || 0));
      rebuildMergePreview();
    }));
    box.querySelectorAll("[data-jxfade]").forEach((el) => el.addEventListener("input", () => {
      mergeJoins[+el.dataset.jxfade].xfade = Math.max(0, Math.min(2000, parseFloat(el.value || "0") || 0));
      rebuildMergePreview();
    }));
    rebuildMergePreview();
  }
  async function soloPart(i) {
    merr("");
    const p = mergeParts[i];
    if (!p) return;
    try {
      const cx = await ensureCtx();
      stopSrc(); stopMergePlay();
      const s = cx.createBufferSource();
      s.buffer = p.buf; s.connect(cx.destination);
      s.start(0, p.start, Math.max(0.05, Math.min(p.end - p.start, p.buf.duration - p.start)));
      mergePlaySrc = s;
    } catch (e) { merr(String(e.message || e)); }
  }
  function trimPart(i) {
    merr("");
    if (editingIndex === i) { editingIndex = -1; syncAddBtn(); renderMerge(); return; }
    const p = mergeParts[i];
    if (!p) return;
    file = p.file; label = p.label; lib = p.folder || ""; buf = p.buf;
    editingIndex = i;
    $("tfl-editor").style.display = "";
    $("tfl-title").textContent = `Trim - ${p.label} (tweaking part #${i + 1})`;
    $("tfl-start").value = p.start.toFixed(2);
    $("tfl-end").value = p.end.toFixed(2);
    $("tfl-name").value = slugifyClip(p.label);
    syncDur(); draw(); syncAddBtn();
    $("tfl-editor").scrollIntoView();
    renderMerge();
  }
  async function fetchDecode(f) {
    const r = await fetch(PREFIX + `/api/tfl/file?organisation_id=${encodeURIComponent(S.org)}&file=${encodeURIComponent(f)}`, { headers: apiHeaders() });
    if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail || ("HTTP " + r.status));
    const ab = await (await r.blob()).arrayBuffer();
    return await (await ensureCtx()).decodeAudioData(ab.slice(0));
  }
  function pushMergePart(f, lab, folder, a, b, decoded) {
    if (!(b > a)) { merr("Selection is empty - drag a region on the waveform first."); return; }
    mergeParts.push({ file: f, label: lab, folder: folder || "", start: a, end: b, buf: decoded });
    mergeJoins.push({ gap: 120, xfade: 0 });
    if (!$("tfl-merge-name").value.trim())
      $("tfl-merge-name").value = slugifyClip(mergeParts.map((p) => p.label).join(" "));
    merr(""); $("tfl-merge-msg").textContent = `Added “${lab}” (${a.toFixed(2)}–${b.toFixed(2)}s) - ${mergeParts.length} part(s).`;
    renderMerge();
  }
  async function addCurrentToMerge() {
    merr("");
    if (!file || !buf) { merr("Load a file first, then add its selection."); return; }
    const a = parseFloat($("tfl-start").value || "0"), b = parseFloat($("tfl-end").value || "0");
    if (editingIndex >= 0 && mergeParts[editingIndex] && mergeParts[editingIndex].file === file) {
      if (!(b > a)) { merr("Selection is empty - drag a region on the waveform first."); return; }
      const p = mergeParts[editingIndex];
      p.start = a; p.end = b; p.buf = buf; p.label = label; p.folder = lib;
      $("tfl-merge-msg").textContent = `Updated part #${editingIndex + 1} (“${label}”) - preview and re-save below.`;
      editingIndex = -1; syncAddBtn();
      $("tfl-title").textContent = "Trim - " + label + (lib ? ` (${lib})` : "");
      renderMerge();
      return;
    }
    editingIndex = -1; syncAddBtn();
    pushMergePart(file, label, lib, a, b, buf);
  }
  async function addFullToMerge(f, lab, folder) {
    merr("");
    editingIndex = -1; syncAddBtn();
    try {
      $("tfl-merge-msg").textContent = `Loading “${lab}”…`;
      const decoded = await fetchDecode(f);
      pushMergePart(f, lab, folder, 0, decoded.duration, decoded);
    } catch (e) { merr(String(e.message || e)); $("tfl-merge-msg").textContent = ""; }
  }
  async function playMerge() {
    merr("");
    if (mergeParts.length < 2) { merr("Add at least 2 parts first (e.g. Bloomfield + Crescent)."); return; }
    try {
      const cx = await ensureCtx();
      stopSrc(); stopMergePlay();
      const m = buildMerged();
      if (!m || m.error) { merr(m ? m.error : "A part has an empty selection - fix its trim."); return; }
      const ab = cx.createBuffer(1, m.data.length, m.sr);
      ab.getChannelData(0).set(m.data);
      const s = cx.createBufferSource();
      s.buffer = ab; s.connect(cx.destination); s.start();
      mergePlaySrc = s;
      $("tfl-merge-msg").textContent = `Playing ${mergeParts.length} words (${m.total.toFixed(2)}s) - exactly what save will produce.`;
    } catch (e) { merr(String(e.message || e)); }
  }
  async function saveMerge() {
    merr(""); $("tfl-merge-msg").textContent = "";
    if (mergeParts.length < 2) { merr("Add at least 2 parts first (e.g. Bloomfield + Crescent)."); return; }
    const s = assertClipName($("tfl-merge-name").value, $("tfl-merge-name-err"));
    if (!s) { merr("Give the merged clip a name, e.g. bloomfield-crescent."); return; }
    try {
      $("tfl-merge-msg").textContent = "Merging & uploading…";
      const r = await api("/api/tfl/merge", { method: "POST", body: {
        organisation_id: S.org,
        parts: mergeParts.map((p) => ({ source: p.file,
          start_ms: Math.round(p.start * 1000), end_ms: Math.round(p.end * 1000) })),
        joins: mergeJoins.map((j) => ({ gap_ms: Math.round(j.gap), xfade_ms: Math.round(j.xfade) })),
        name: s } });
      const jd = (r.joins || []).map((j) => j.xfade_ms > 0 ? `${j.xfade_ms}ms crossfade` : `${j.gap_ms}ms gap`).join(" + ");
      $("tfl-merge-msg").textContent = `Saved as "${r.name}" (${(r.duration_ms / 1000).toFixed(1)}s, ${r.parts} parts${jd ? ` · ${jd}` : ""}) - still below, tweak and re-save any time.`;
      await loadAudio();
      if (S.curProg) openProgram(S.curProg.id);
    } catch (e) { merr(String(e.message || e)); $("tfl-merge-msg").textContent = ""; }
  }
  function clearMerge() {
    stopMergePlay();
    mergeParts = []; mergeJoins = []; editingIndex = -1;
    syncAddBtn(); renderMerge();
    $("tfl-merge-msg").textContent = ""; merr("");
  }
  function wire() {
    if (!($("btn-tfl-search"))) return;
    $("btn-tfl-search").onclick = search;
    $("tfl-q").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); search(); } });
    $("tfl-start").addEventListener("input", () => { syncDur(); draw(); });
    $("tfl-end").addEventListener("input", () => { syncDur(); draw(); });
    $("btn-tfl-play-sel").onclick = playSel;
    $("btn-tfl-save").onclick = save;
    $("btn-tfl-add-merge").onclick = addCurrentToMerge;
    $("btn-tfl-play-merge").onclick = playMerge;
    $("btn-tfl-merge-save").onclick = saveMerge;
    $("btn-tfl-merge-clear").onclick = clearMerge;
    window.addEventListener("resize", () => { if (buf) draw(); });
    const cv = $("tfl-wave");
    let dragging = false;
    const pos = (e) => {
      const r = cv.getBoundingClientRect();
      const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
      return Math.max(0, Math.min(1, x / r.width)) * (buf ? buf.duration : 0);
    };
    cv.addEventListener("mousedown", (e) => { if (!buf) return; dragging = true; const t = pos(e); setRange(t, t); });
    cv.addEventListener("mousemove", (e) => { if (!dragging || !buf) return; setRange(parseFloat($("tfl-start").value), pos(e)); });
    window.addEventListener("mouseup", () => dragging = false);
    cv.addEventListener("touchstart", (e) => { if (!buf) return; dragging = true; const t = pos(e); setRange(t, t); e.preventDefault(); }, { passive: false });
    cv.addEventListener("touchmove", (e) => { if (!dragging || !buf) return; setRange(parseFloat($("tfl-start").value), pos(e)); e.preventDefault(); }, { passive: false });
    cv.addEventListener("touchend", () => dragging = false);
  }
  return { wire };
})();

// ---------- theme ----------
function applyTheme(dark) {
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  const btn = $("theme-toggle");
  if (!btn) return;
  const moon = btn.querySelector(".moon");
  const sun = btn.querySelector(".sun");
  const label = btn.querySelector(".label");
  if (moon) moon.style.display = dark ? "none" : "";
  if (sun) sun.style.display = dark ? "" : "none";
  if (label) label.textContent = dark ? "Light" : "Dark";
}
let isDark = localStorage.getItem("nsl_dark") === "1" ||
  (localStorage.getItem("nsl_dark") == null && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
applyTheme(isDark);
$("theme-toggle").addEventListener("click", () => { isDark = !isDark; localStorage.setItem("nsl_dark", isDark ? "1" : "0"); applyTheme(isDark); });

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
showSection("programs");
Tfl.wire();
refreshMe();
