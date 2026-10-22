/* Hanover font editor: edit glyphs 1:1, import/export .Fnt and .BDF.
   Mounted inside the Fonts tab by fonts.js (window.FontEdit.mount). */
"use strict";
(function () {
  const CELL = 22;
  const ED = { model: null, code: 65, undo: [], clip: null, isNew: false };
  let mountEl = null;
  const $ = (id) => document.getElementById(id);

  window.FontEdit = {
  pure: { blank, fromFntText, toFntText, fromBDF, toBDF },
  mount(el) {
    mountEl = el;
    el.innerHTML = `
    <div class="group"><div class="grouptitle">Font editor (.Fnt / .BDF)</div>
      <p class="hint">Pick an existing font, start a new one, or import a file. Click squares to draw (drag paints). Advance width is the red line. Use it then Download or Upload to the portal.</p>
      <div class="row">
        <div class="col"><label for="fe-src">Existing font</label><select id="fe-src"><option value="">(loading…)</option></select></div>
        <div style="align-self:end" class="row"><button id="fe-load">Load</button><button id="fe-new">New</button>
        <button id="fe-imp-fnt">Import .Fnt</button><button id="fe-imp-bdf">Import .BDF</button></div>
      </div>
      <input id="fe-file" type="file" accept=".Fnt,.fnt,.bdf,.BDF" style="display:none">
      <div class="row">
        <div class="col"><label for="fe-stem">Font name (stem → id han-…)</label><input id="fe-stem"></div>
        <div style="flex:0 0 100px"><label for="fe-h">Height</label><input id="fe-h" type="number" min="4" max="64" value="19" disabled></div>
        <div style="flex:0 0 110px"><label for="fe-w">Max width</label><input id="fe-w" type="number" min="1" max="64" value="19" disabled></div>
      </div>
      <div class="row"><div class="col"><label>Glyphs (codes 32–255)</label>
        <div class="row" style="margin-top:0"><button id="fe-f-all" class="tool">All</button><button id="fe-f-ascii" class="tool">ASCII</button><button id="fe-f-ink" class="tool">With ink</button></div>
        <div id="fe-strip" style="max-height:150px;overflow:auto;border:1px solid #888;background:#111;padding:4px"></div></div></div>
      <div class="row">
        <div><label>Glyph <span id="fe-glabel" class="kbd">A 65</span></label>
          <canvas id="fe-grid" style="background:#000;cursor:crosshair;image-rendering:pixelated"></canvas>
          <div class="row"><div class="col"><label for="fe-adv">Advance width <span id="fe-advv"></span></label><input id="fe-adv" type="range" min="0" max="19" value="0"></div></div>
        </div>
        <div class="col"><label>Tools</label>
          <div class="row">
            <button id="fe-clear" class="tool">Clear</button><button id="fe-fill" class="tool">Fill</button>
            <button id="fe-inv" class="tool">Invert</button><button id="fe-flip" class="tool">Flip ↔</button>
            <button id="fe-l" class="tool">←</button><button id="fe-r" class="tool">→</button>
            <button id="fe-u" class="tool">↑</button><button id="fe-d" class="tool">↓</button>
            <button id="fe-copy" class="tool">Copy</button><button id="fe-paste" class="tool">Paste</button>
            <button id="fe-undo" class="tool">Undo</button>
          </div>
          <div><label for="fe-prev">Preview text (this font)</label><input id="fe-prev" value="Burton 100"></div>
          <div><canvas id="fe-pcv" style="background:#000;max-width:100%;margin-top:6px"></canvas></div>
        </div>
      </div>
      <div class="row">
        <button id="fe-dl-fnt" class="primary">Download .Fnt</button><button id="fe-dl-bdf">Download .BDF</button>
        <button id="fe-up" class="primary">Upload .Fnt to portal</button>
      </div>
      <div class="hint" id="fe-msg"></div><div class="err" id="fe-err"></div>
    </div>`;
    bind();
    refreshSrc();
  },
  async load(id) {
    if (!mountEl || !$(`fe-src`)) return false;
    err(""); msg("");
    try {
      const d = await api("/api/fonts/" + encodeURIComponent(id));
      ED.model = fromServer(d, id.replace(/^han-/, "")); ED.isNew = false;
      ED.code = 65; ED.undo = [];
      afterLoad(`Loaded ${id} (${d.height}px, max width ${d.maxwidth}).`);
      mountEl.scrollIntoView();
      return true;
    } catch (e) { err(String(e.message || e)); return false; }
  } };

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const msg = (t) => { $("fe-msg").textContent = t || ""; };
  const err = (t) => { $("fe-err").textContent = t || ""; };
  const M = () => ED.model;

  /* ---------- model ---------- */
  function blank(stem, h, w) {
    const adv = {}, cols = {};
    for (let c = 32; c < 256; c++) { adv[c] = 0; cols[c] = new Array(w).fill(0); }
    return { stem, magic: "2", tail: ["0", "0"], height: h, maxwidth: w, adv, cols };
  }
  function fromServer(detail, stem) {
    const m = blank(stem, detail.height, detail.maxwidth);
    for (const [k, v] of Object.entries(detail.glyphs || {})) {
      const c = parseInt(k, 10);
      if (!(c >= 32 && c < 256) || !Array.isArray(v)) continue;
      m.adv[c] = v[0] | 0;
      const cc = new Array(m.maxwidth).fill(0);
      for (let i = 0; i < Math.min(m.maxwidth, v.length - 1); i++) cc[i] = v[i + 1] | 0;
      m.cols[c] = cc;
    }
    return m;
  }
  function fromFntText(text, filename) {
    const toks = text.replace(/\r\n/g, "\n").trim().split(/\s+/);
    if (toks.length < 5 || (toks[0] !== "0" && toks[0] !== "2")) throw new Error("not a Hanover .Fnt file (bad magic)");
    const h = parseInt(toks[1], 10), w = parseInt(toks[2], 10);
    if (!(h >= 1 && h <= 128 && w >= 1 && w <= 128)) throw new Error("bad height/width");
    if (toks.length !== 5 + 224 + 224 * w) throw new Error(`bad token count: ${toks.length} != ${5 + 224 + 224 * w}`);
    const widths = toks.slice(5, 5 + 224).map(Number);
    const m = blank(filename.replace(/\.fnt$/i, ""), h, w);
    m.magic = toks[0]; m.tail = [toks[3], toks[4]];
    let i = 5 + 224;
    for (let c = 32; c < 256; c++) {
      m.adv[c] = widths[c - 32] | 0;
      m.cols[c] = toks.slice(i, i + w).map(Number);
      i += w;
    }
    return m;
  }
  function toFntText(m) {
    m = m || M();
    const out = [];
    out.push(`${m.magic} ${m.height} ${m.maxwidth} ${m.tail[0]} ${m.tail[1]}`);
    const widths = [];
    for (let c = 32; c < 256; c++) widths.push(m.adv[c] | 0);
    out.push(widths.join(" "));
    for (let c = 32; c < 256; c++) out.push(m.cols[c].map((v) => v | 0).join(" "));
    return out.join("\n") + "\n";
  }

  /* ---------- BDF ---------- */
  function hexRowToBits(row, w) {
    row = row.trim();
    const bits = [];
    for (let x = 0; x < w; x++) {
      const hx = row[Math.floor(x / 4)] || "0";
      bits.push((parseInt(hx, 16) >> (3 - (x % 4))) & 1);
    }
    return bits;
  }
  function bitsToHexRow(bits) {
    let s = "";
    for (let x = 0; x < bits.length; x += 4) {
      let v = 0;
      for (let b = 0; b < 4; b++) v = (v << 1) | (bits[x + b] ? 1 : 0);
      s += v.toString(16).toUpperCase();
    }
    return s;
  }
  function fromBDF(text, filename) {
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    let gW = 0, gH = 0, name = filename.replace(/\.bdf$/i, "");
    const m1 = text.match(/FONTBOUNDINGBOX\s+(-?\d+)\s+(-?\d+)/);
    if (m1) { gW = +m1[1]; gH = +m1[2]; }
    const fm = text.match(/^FONT\s+(\S+)/m);
    if (fm) name = fm[1];
    const glyphs = [];
    let cur = null, inBmp = false, bmp = [];
    for (const ln of lines) {
      const t = ln.trim().split(/\s+/);
      if (t[0] === "STARTCHAR") { cur = { enc: -1, w: 0, h: 0, xoff: 0, yoff: 0, dw: null, bmp: [] }; inBmp = false; }
      else if (cur && t[0] === "ENCODING") cur.enc = parseInt(t[1], 10);
      else if (cur && t[0] === "BBX") { cur.w = +t[1]; cur.h = +t[2]; cur.xoff = +(t[3] || 0); cur.yoff = +(t[4] || 0); }
      else if (cur && t[0] === "DWIDTH") cur.dw = +t[1];
      else if (cur && t[0] === "BITMAP") inBmp = true;
      else if (cur && t[0] === "ENDCHAR") { cur.bmp = bmp; bmp = []; inBmp = false; glyphs.push(cur); cur = null; }
      else if (cur && inBmp && /^[0-9A-Fa-f]+$/.test(t[0])) bmp.push(t[0]);
    }
    if (!gH) gH = Math.max(...glyphs.map((g) => g.h), 8);
    if (!gW) gW = Math.max(...glyphs.map((g) => g.w), 4);
    const warns = [];
    const m = blank(name.replace(/[^A-Za-z0-9_.-]/g, "_").slice(0, 40) || "import", Math.min(64, gH), Math.min(64, gW));
    for (const g of glyphs) {
      if (!(g.enc >= 32 && g.enc < 256)) continue;
      if (g.xoff || g.yoff) warns.push(`U+${g.enc.toString(16).toUpperCase()}: offset ${g.xoff},${g.yoff} applied by shift`);
      const adv = Math.max(0, Math.min(m.maxwidth, g.dw != null ? g.dw : g.w));
      m.adv[g.enc] = adv;
      const cc = new Array(m.maxwidth).fill(0);
      for (let r = 0; r < Math.min(g.h, g.bmp.length, m.height); r++) {
        const bits = hexRowToBits(g.bmp[r], g.w);
        for (let x = 0; x < Math.min(g.w, m.maxwidth); x++) {
          if (bits[x]) cc[x] |= (1 << r);
        }
      }
      // yoff shifts the drawing down, xoff shifts right (clip)
      if (g.yoff > 0 || g.xoff > 0) {
        const sh = new Array(m.maxwidth).fill(0);
        for (let x = 0; x < m.maxwidth; x++) {
          let v = 0;
          for (let r = 0; r < m.height; r++) {
            const sx = x - g.xoff, sr = r - g.yoff;
            if (sx >= 0 && sr >= 0 && (cc[sx] & (1 << sr))) v |= (1 << r);
          }
          sh[x] = v;
        }
        m.cols[g.enc] = sh;
      } else m.cols[g.enc] = cc;
      if (g.h > m.height || g.w > m.maxwidth) warns.push(`U+${g.enc.toString(16).toUpperCase()}: clipped to ${m.maxwidth}×${m.height}`);
    }
    return { model: m, warns };
  }
  function toBDF(m) {
    m = m || M();
    const L = [];
    L.push("STARTFONT 2.1", `FONT ${m.stem}`, `SIZE ${m.height} 75 75`,
      `FONTBOUNDINGBOX ${m.maxwidth} ${m.height} 0 0`, "STARTPROPERTIES 2",
      `FONT_ASCENT ${m.height}`, "FONT_DESCENT 0", "ENDPROPERTIES");
    const used = [];
    for (let c = 32; c < 256; c++) if (m.cols[c].some((v) => v)) used.push(c);
    L.push(`CHARS ${used.length}`);
    for (const c of used) {
      const adv = Math.max(0, m.adv[c] | 0);
      L.push(`STARTCHAR U+${c.toString(16).toUpperCase().padStart(4, "0")}`, `ENCODING ${c}`,
        `SWIDTH ${(adv * 1000 / m.maxwidth) | 0} 0`, `DWIDTH ${adv} 0`, `BBX ${adv} ${m.height} 0 0`, "BITMAP");
      for (let r = 0; r < m.height; r++) {
        const bits = [];
        for (let x = 0; x < adv; x++) bits.push((m.cols[c][x] & (1 << r)) ? 1 : 0);
        L.push(bits.length ? bitsToHexRow(bits) : "00");
      }
      L.push("ENDCHAR");
    }
    L.push("ENDFONT");
    return L.join("\n") + "\n";
  }

  /* ---------- UI ---------- */
  function bind() {
    $("fe-load").onclick = async () => {
      err(""); msg("");
      const id = $("fe-src").value;
      if (!id) return;
      try {
        const d = await api("/api/fonts/" + encodeURIComponent(id));
        ED.model = fromServer(d, id.replace(/^han-/, "")); ED.isNew = false;
        afterLoad(`Loaded ${id} (${d.height}px, max width ${d.maxwidth}).`);
      } catch (e) { err(String(e.message || e)); }
    };
    $("fe-new").onclick = () => {
      const h = Math.max(4, Math.min(64, parseInt($("fe-h").value || "19", 10)));
      const w = Math.max(1, Math.min(64, parseInt($("fe-w").value || "19", 10)));
      const stem = ($("fe-stem").value.trim() || "myfont").replace(/[^A-Za-z0-9_.-]/g, "_");
      ED.model = blank(stem, h, w); ED.isNew = true; ED.code = 65; ED.undo = [];
      afterLoad(`New font ${stem}: ${h}px tall, max width ${w}.`);
    };
    let importKind = "fnt";
    $("fe-imp-fnt").onclick = () => { importKind = "fnt"; $("fe-file").click(); };
    $("fe-imp-bdf").onclick = () => { importKind = "bdf"; $("fe-file").click(); };
    $("fe-file").addEventListener("change", async (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      if (!f) return;
      err(""); msg("");
      try {
        const text = await f.text();
        if (importKind === "fnt") {
          ED.model = fromFntText(text, f.name); ED.isNew = false;
          afterLoad(`Imported ${f.name}.`);
        } else {
          const { model, warns } = fromBDF(text, f.name);
          ED.model = model; ED.isNew = true;
          afterLoad(`Imported ${f.name}: ${model.maxwidth}×${model.height}` + (warns.length ? ". " + warns.slice(0, 3).join("; ") + (warns.length > 3 ? ` (+${warns.length - 3} more)` : "") : ""));
        }
        ED.code = 65; ED.undo = [];
      } catch (ex) { err("Import failed: " + String(ex.message || ex)); }
    });
    $("fe-strip").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-c]"); if (!b) return;
      ED.code = +b.dataset.c; drawGrid(); markStrip(); drawPreview();
    });
    for (const [id, fn] of [["fe-f-all", 0], ["fe-f-ascii", 1], ["fe-f-ink", 2]])
      $(id).onclick = () => buildStrip(fn);
    $("fe-adv").addEventListener("input", () => {
      if (!M()) return;
      pushUndo();
      M().adv[ED.code] = Math.max(0, Math.min(M().maxwidth, parseInt($("fe-adv").value || "0", 10)));
      drawGrid(); drawPreview(); markStrip();
    });
    const tool = (id, fn) => $(id).onclick = () => { if (!M()) return; pushUndo(); fn(); drawGrid(); drawPreview(); markStrip(); };
    tool("fe-clear", () => M().cols[ED.code].fill(0));
    tool("fe-fill", () => M().cols[ED.code].fill((1 << M().height) - 1));
    tool("fe-inv", () => { const m = M().cols[ED.code], mk = (1 << M().height) - 1; for (let i = 0; i < m.length; i++) m[i] = (~m[i]) & mk; });
    tool("fe-flip", () => M().cols[ED.code].reverse());
    tool("fe-l", () => shift(-1, 0)); tool("fe-r", () => shift(1, 0));
    tool("fe-u", () => shift(0, -1)); tool("fe-d", () => shift(0, 1));
    $("fe-copy").onclick = () => { if (M()) ED.clip = { adv: M().adv[ED.code], cols: M().cols[ED.code].slice() }; };
    $("fe-paste").onclick = () => { if (M() && ED.clip) { pushUndo(); M().adv[ED.code] = ED.clip.adv; M().cols[ED.code] = ED.clip.cols.slice(0, M().maxwidth); while (M().cols[ED.code].length < M().maxwidth) M().cols[ED.code].push(0); drawGrid(); drawPreview(); markStrip(); } };
    $("fe-undo").onclick = undo;
    $("fe-prev").addEventListener("input", drawPreview);
    $("fe-dl-fnt").onclick = () => download(toFntText(), M().stem + ".Fnt", "text/plain");
    $("fe-dl-bdf").onclick = () => download(toBDF(), M().stem + ".bdf", "text/plain");
    $("fe-up").onclick = async () => {
      err(""); msg("");
      if (!M()) { err("Load or create a font first."); return; }
      try {
        const blob = new Blob([toFntText()], { type: "text/plain" });
        const fd = new FormData(); fd.append("file", blob, M().stem + ".Fnt");
        const token = localStorage.getItem("nsl_token") || "";
        const r = await fetch((location.pathname.startsWith("/portal") ? "/portal" : "") + "/api/fonts",
          { method: "POST", headers: { "Authorization": "Bearer " + token }, body: fd });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status));
        msg(`Saved as ${d.id}${d.overwritten ? " (overwrote existing)" : ""}. Usable in layouts now.`);
        refreshSrc();
      } catch (e) { err("Upload failed: " + String(e.message || e)); }
    };
    bindGrid();
  }
  function shift(dx, dy) {
    const m = M(), cc = m.cols[ED.code], n = new Array(m.maxwidth).fill(0);
    for (let x = 0; x < m.maxwidth; x++) {
      const sx = x - dx;
      if (sx < 0 || sx >= m.maxwidth) continue;
      let v = 0;
      for (let r = 0; r < m.height; r++) {
        const sr = r - dy;
        if (sr >= 0 && (cc[sx] & (1 << sr))) v |= (1 << r);
      }
      n[x] = v;
    }
    m.cols[ED.code] = n;
  }
  function pushUndo() {
    ED.undo.push({ code: ED.code, adv: M().adv[ED.code], cols: M().cols[ED.code].slice() });
    if (ED.undo.length > 50) ED.undo.shift();
  }
  function undo() {
    const u = ED.undo.pop();
    if (!u || !M()) return;
    ED.code = u.code;
    M().adv[u.code] = u.adv; M().cols[u.code] = u.cols;
    drawGrid(); markStrip(); drawPreview();
  }
  function afterLoad(t) {
    $("fe-stem").value = M().stem;
    $("fe-h").value = M().height; $("fe-w").value = M().width || M().maxwidth;
    $("fe-h").disabled = $("fe-w").disabled = !ED.isNew;
    buildStrip(0); drawGrid(); drawPreview(); msg(t);
  }
  async function refreshSrc() {
    try {
      const r = await api("/api/fonts");
      $("fe-src").innerHTML = (r.fonts || []).map((f) => `<option value="${esc(f.id)}">${esc(f.id)} · ${f.height}px</option>`).join("");
    } catch (e) { $("fe-src").innerHTML = `<option value="">(failed to load)</option>`; }
  }

  /* ---------- strip ---------- */
  let stripMode = 0;
  function buildStrip(mode) {
    stripMode = mode;
    if (!M()) { $("fe-strip").innerHTML = `<span class="hint">Load a font to see its glyphs.</span>`; return; }
    let h = "";
    for (let c = 32; c < 256; c++) {
      if (mode === 1 && c > 126) continue;
      const ink = M().cols[c].some((v) => v);
      if (mode === 2 && !ink) continue;
      const label = c >= 32 && c <= 126 ? String.fromCharCode(c) : c.toString(16).toUpperCase();
      h += `<button class="tool" data-c="${c}" data-ink="${ink ? 1 : 0}" title="code ${c}" style="${ink ? "border-color:#FFB000;color:#FFB000" : ""}">${esc(label)}</button>`;
    }
    $("fe-strip").innerHTML = h || `<span class="hint">No glyphs in this view.</span>`;
    markStrip();
  }
  function markStrip() {
    $("fe-strip").querySelectorAll("button[data-c]").forEach((b) => {
      const c = +b.dataset.c;
      const on = M() && c === ED.code;
      b.classList.toggle("primary", on);
      if (M()) {
        const ink = M().cols[c].some((v) => v) ? 1 : 0;
        if (b.dataset.ink !== String(ink)) { b.dataset.ink = ink; b.style.borderColor = ink ? "#FFB000" : ""; b.style.color = ink ? "#FFB000" : ""; }
      }
    });
  }

  /* ---------- grid ---------- */
  function drawGrid() {
    const cv = $("fe-grid");
    if (!M()) { cv.width = 200; cv.height = 60; return; }
    const m = M();
    cv.width = m.maxwidth * CELL; cv.height = m.height * CELL;
    const g = cv.getContext("2d");
    g.fillStyle = "#000"; g.fillRect(0, 0, cv.width, cv.height);
    const cc = m.cols[ED.code];
    for (let x = 0; x < m.maxwidth; x++) for (let r = 0; r < m.height; r++) {
      g.fillStyle = (cc[x] & (1 << r)) ? "#FFB000" : "#1a1a1a";
      g.fillRect(x * CELL + 1, r * CELL + 1, CELL - 2, CELL - 2);
    }
    // advance line
    const ax = (m.adv[ED.code] | 0) * CELL;
    g.fillStyle = "#ff4444"; g.fillRect(Math.min(ax, cv.width - 2), 0, 2, cv.height);
    g.fillStyle = "#666";
    for (let x = 0; x <= m.maxwidth; x++) g.fillRect(x * CELL - 0.5, 0, 1, cv.height);
    for (let r = 0; r <= m.height; r++) g.fillRect(0, r * CELL - 0.5, cv.width, 1);
    const ch = ED.code >= 32 && ED.code <= 126 ? `“${String.fromCharCode(ED.code)}” ` : "";
    $("fe-glabel").textContent = `${ch}${ED.code}`;
    $("fe-adv").max = m.maxwidth;
    $("fe-adv").value = m.adv[ED.code] | 0;
    $("fe-advv").textContent = `= ${m.adv[ED.code] | 0}px`;
  }
  function bindGrid() {
    const cv = () => $("fe-grid");
    let painting = null;
    const cell = (e) => {
      const r = cv().getBoundingClientRect();
      return { x: Math.floor((e.clientX - r.left) / (r.width / M().maxwidth)),
               y: Math.floor((e.clientY - r.top) / (r.height / M().height)) };
    };
    cv().addEventListener("pointerdown", (e) => {
      if (!M()) return;
      e.preventDefault();
      cv().setPointerCapture(e.pointerId);
      const { x, y } = cell(e);
      if (x < 0 || x >= M().maxwidth || y < 0 || y >= M().height) return;
      pushUndo();
      painting = (M().cols[ED.code][x] & (1 << y)) ? 0 : 1;
      dot(x, y, painting);
    });
    cv().addEventListener("pointermove", (e) => {
      if (painting == null || !M()) return;
      const { x, y } = cell(e);
      if (x < 0 || x >= M().maxwidth || y < 0 || y >= M().height) return;
      dot(x, y, painting);
    });
    const end = () => { if (painting != null) { painting = null; markStrip(); } };
    cv().addEventListener("pointerup", end);
    cv().addEventListener("pointercancel", end);
  }
  function dot(x, y, v) {
    const cc = M().cols[ED.code];
    if (v) cc[x] |= (1 << y); else cc[x] &= ~(1 << y);
    drawGrid(); drawPreview();
  }

  /* ---------- preview ---------- */
  function drawPreview() {
    const cv = $("fe-pcv");
    if (!M()) return;
    const text = $("fe-prev").value || "", z = 3;
    let w = 0;
    for (const ch of text) w += (M().adv[ch.codePointAt(0)] ?? M().adv[63] ?? 0);
    cv.width = Math.max(1, w * z + 8); cv.height = M().height * z + 8;
    const g = cv.getContext("2d");
    g.fillStyle = "#000"; g.fillRect(0, 0, cv.width, cv.height);
    let cx = 0;
    for (const ch of text) {
      const code = ch.codePointAt(0);
      const cc = (code >= 32 && code < 256 && M().cols[code]) ? M().cols[code] : M().cols[63];
      const adv = (code >= 32 && code < 256 && M().adv[code] != null) ? M().adv[code] : (M().adv[63] || 0);
      for (let x = 0; x < Math.min(adv, M().maxwidth); x++)
        for (let r = 0; r < M().height; r++)
          if (cc[x] & (1 << r)) { g.fillStyle = "#FFB000"; g.fillRect((cx + x) * z + 4, r * z + 4, z, z); }
      cx += adv;
    }
    g.fillStyle = "rgba(0,0,0,.45)";
    for (let x = 0; x < cv.width; x += z) g.fillRect(x, 0, 1, cv.height);
    for (let y = 0; y < cv.height; y += z) g.fillRect(0, y, cv.width, 1);
  }

  function download(text, name) {    if (!M()) { err("Load or create a font first."); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
    msg(`Downloaded ${name}.`);
  }
})();
