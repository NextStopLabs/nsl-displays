/* Visual board layout editor (plan.md 5.3c). No JSON typing required.
   Canvas at the display's real pixel size + live LED preview + properties panel.
   Uses globals api(), err(), esc() from app.js. */
"use strict";

const LayoutEditor = (() => {
  const FONT_H = { "pixel-small": 8, "pixel-medium": 12, "pixel-large": 20, "pixel-huge": 32, "mono-5x7": 7, "mono-6x8": 8 };
  const FONTS = Object.keys(FONT_H);
  const AMBER = "#FFB000";

  // ---------- Hanover bitmap fonts (server .Fnt files, 1:1 pixels, LSB = top) ----------
  // Standalone (no editor state): app.js Preview uses LayoutEditor.hanover too.
  const hanCache = {};
  async function hanFetch(id) {
    if (hanCache[id]) return hanCache[id];
    const r = await api(`/api/fonts/${encodeURIComponent(id)}`);
    hanCache[id] = r;
    return r;
  }
  function hanGet(id) {
    if (hanCache[id] === "failed") return null;
    if (!hanCache[id]) {
      hanFetch(id).catch(() => { hanCache[id] = "failed"; });
      return null;
    }
    return hanCache[id];
  }
  function hanWidth(f, text, spacing) {
    let w = 0;
    const sp = spacing || 0;
    for (const ch of (text || "")) {
      const g = f.glyphs[String(ch.codePointAt(0))] || f.glyphs["63"];
      if (g) w += g[0] + sp;
    }
    return w;
  }
  const hanScale = (el) => Math.max(1, Math.min(8, parseInt((el && el.scale) || 1, 10) || 1));
  const hanSpacing = (el) => { const v = parseInt((el && el.spacing) || 0, 10); return isNaN(v) ? 0 : Math.max(-8, Math.min(16, v)); };
  function hanStrip(han, sc, text, colour, spacing) {
    const w1 = Math.max(1, hanWidth(han, text, spacing || 0)), h1 = han.height;
    const c1 = document.createElement("canvas");
    c1.width = w1; c1.height = h1;
    hanDraw(c1.getContext("2d"), han, text, 0, 0, colour, spacing);
    if (sc <= 1) return c1;
    const c2 = document.createElement("canvas");
    c2.width = w1 * sc; c2.height = h1 * sc;
    const s2 = c2.getContext("2d");
    s2.imageSmoothingEnabled = false;
    s2.drawImage(c1, 0, 0, w1, h1, 0, 0, w1 * sc, h1 * sc);
    return c2;
  }
  function hanDraw(g, f, text, x, y, colour, spacing) {
    g.fillStyle = colour || AMBER;
    let cx = Math.round(x);
    const y0 = Math.round(y), sp = spacing || 0;
    for (const ch of (text || "")) {
      const gl = f.glyphs[String(ch.codePointAt(0))] || f.glyphs["63"];
      if (!gl) continue;
      const cols = gl.slice(1);
      for (let dx = 0; dx < cols.length; dx++) {
        let b = cols[dx], yy = y0;
        while (b) { if (b & 1) g.fillRect(cx + dx, yy, 1, 1); yy++; b >>= 1; }
      }
      cx += gl[0] + sp;
    }
  }
  const hanover = {
    list: async () => {
      if (!hanover._list) hanover._list = await api("/api/fonts").then((r) => r.fonts || []).catch(() => []);
      return hanover._list;
    },
    get: hanGet, width: hanWidth, draw: hanDraw,
    isHan: (id) => typeof id === "string" && id.indexOf("han-") === 0,
  };
  let raf = 0;
  let ed = null; // editor state

  function stop() { if (raf) cancelAnimationFrame(raf); raf = 0; ed = null; }

  function blank(w, h) {
    return { schema: 1, width: w, height: h, background: "#000000", page_seconds: 3,
             override: { mode: "replace", replaces: "dest", hide: [] }, elements: [] };
  }
  function autofit(w, h) {
    const nw = Math.max(16, Math.round(w * 70 / 240)), dh = Math.max(8, Math.round(h * 25 / 40));
    return { schema: 1, width: w, height: h, background: "#000000", page_seconds: 3,
      override: { mode: "replace", replaces: "dest", hide: [] },
      elements: [
        { type: "field", field: "num", x: 0, y: 0, w: nw, h: h, font: "han-1912B3E1", colour: AMBER,
          align: "centre", valign: "middle", scroll: "none", flash_ms: 0, page: 1, show_if: "always", scale: 2, spacing: 1, invert: false },
        { type: "field", field: "dest", x: nw, y: 0, w: w - nw, h: dh, font: "han-2423B3E1", colour: AMBER,
          align: "centre", valign: "middle", scroll: "if_too_long", flash_ms: 0, page: 1, show_if: "always", scale: 1, spacing: 1, invert: false },
        { type: "field", field: "via", x: nw, y: dh, w: w - nw, h: h - dh, font: "han-1610N2E1", colour: AMBER,
          align: "centre", valign: "middle", scroll: "if_too_long", flash_ms: 0, page: 1, show_if: "has_text", scale: 1, spacing: 1, invert: false } ] };
  }

  // One pages system: the destination's board pages rotate every
  // page_seconds. Each board page can have its own full layout stored in
  // `layout.page_elements[position]`; missing key falls back to `elements`.
  function slotFor(lay, t) {
    return Math.floor(t / (parseFloat(lay.page_seconds) || 3));
  }
  function orderedBoardPages() {
    return [...(ed.pages || [])].sort((a, b) => (a.position || 0) - (b.position || 0));
  }
  function boardPosForSlot(slot) {
    const ordered = orderedBoardPages();
    if (!ordered.length) return 0;
    const pg = ordered[slot % ordered.length];
    return (pg && pg.position) || ((slot % ordered.length) + 1);
  }
  function activePageElements(lay, pos) {
    const pe = lay.page_elements || {};
    return pe[pos] || pe[String(pos)] || lay.elements || [];
  }
  function switchBoardTab(newPos) {
    if (!ed || !ed.lay) return;
    const pe = ed.lay.page_elements || (ed.lay.page_elements = {});
    // stash current tab's elements back into its page slot
    pe[ed.tab] = ed.lay.elements;
    ed.tab = newPos;
    // load the new tab, seeding from the previous tab if it has no layout yet
    if (!pe[newPos]) pe[newPos] = JSON.parse(JSON.stringify(ed.lay.elements || []));
    ed.lay.elements = pe[newPos];
    ed.sel = (ed.lay.elements || []).length ? 0 : -1;
    updateStatus(); drawEditor(); renderProps(); renderOverride();
  }
  function ensurePageLayouts() {
    // Seed each board page with its own copy of the default layout.
    // If an old layout used per-element `pages` arrays, migrate those
    // into separate page layouts now.
    if (!ed.lay) return;
    const ordered = orderedBoardPages();
    if (ordered.length <= 1) return;
    const pe = ed.lay.page_elements || (ed.lay.page_elements = {});
    const legacy = (ed.lay.elements || []).some((e) => e.pages && e.pages.length);
    for (const pg of ordered) {
      const pos = pg.position;
      if (pe[pos]) continue;
      if (legacy) {
        pe[pos] = (ed.lay.elements || []).filter((e) =>
          !e.pages || !e.pages.length || e.pages.indexOf(pos) >= 0
        ).map((e) => { const c = JSON.parse(JSON.stringify(e)); delete c.pages; return c; });
      } else {
        pe[pos] = JSON.parse(JSON.stringify(ed.lay.elements || []));
      }
    }
    ed.lay.elements = pe[ed.tab] || pe[ordered[0].position] || ed.lay.elements;
  }
  function renderBoardTabs() {
    const box = document.getElementById("lay-boardtabs");
    if (!box || !ed) return;
    const ordered = orderedBoardPages();
    if (ordered.length <= 1) { box.innerHTML = `<span class=hint>Single board page.</span>`; return; }
    box.innerHTML = ordered.map((pg) =>
      `<button class="${ed.tab === pg.position ? "on" : ""}" data-btab="${pg.position}" title="Arrange board page ${pg.position}">P${pg.position}</button>`).join("");
    box.querySelectorAll("[data-btab]").forEach((b) => b.onclick = () => {
      switchBoardTab(parseInt(b.dataset.btab, 10));
    });
  }
  function syncPageSeconds() {
    const box = document.getElementById("lay-seconds");
    if (box && ed && ed.lay) ed.lay.page_seconds = Math.max(1, parseInt(box.value || "3", 10));
  }
  function visible(el, fields, ovActive) {
    const sh = el.show_if || "always";
    if (sh === "always") return true;
    if (sh === "has_text") return el.type === "field" ? !!fields[el.field] : !!el.text;
    if (sh === "override_active") return ovActive;
    if (sh === "override_inactive") return !ovActive;
    return true;
  }
  function elText(el, fields, ovText, lay) {
    const ov = lay.override || {};
    if (ovText && el.type === "field" && (ov.replaces || "dest") === el.field &&
        ["replace", "hide_others", "full"].includes(ov.mode || "replace")) return ovText;
    if (el.type === "text") return el.text || "";
    return fields[el.field] || "";
  }
  function hideSet(lay, ovActive) {
    const ov = lay.override || {};
    if (!ovActive) return new Set();
    if (ov.mode === "hide_others") {
      const keep = ov.replaces || "dest";
      const s = new Set();
      for (const e of (lay.elements || [])) if (e.field !== keep) s.add(e.field);
      return s;
    }
    return new Set(ov.hide || []);
  }

  function fontPx(el) {
    const fh = FONT_H[el.font] || 12;
    const hh = parseInt(el.h || 12, 10);
    return hh > 4 ? Math.min(hh - 2, fh + 4) : fh;
  }
  function cssAlign(a) { return a === "centre" || a === "center" ? "center" : (a === "right" ? "right" : "left"); }

  // ---------- editor canvas (clean boxes) ----------
  function drawEditor() {
    if (!ed) return;
    const { lay, scale } = ed;
    const cv = document.getElementById("lay-canvas");
    cv.width = lay.width * scale; cv.height = lay.height * scale;
    const g = cv.getContext("2d");
    g.fillStyle = "#141414"; g.fillRect(0, 0, cv.width, cv.height);
    g.strokeStyle = "#2c2c2c"; g.lineWidth = 1;
    for (let x = 0; x <= lay.width; x += 8) { g.beginPath(); g.moveTo(x * scale + .5, 0); g.lineTo(x * scale + .5, cv.height); g.stroke(); }
    for (let y = 0; y <= lay.height; y += 8) { g.beginPath(); g.moveTo(0, y * scale + .5); g.lineTo(cv.width, y * scale + .5); g.stroke(); }
    (lay.elements || []).forEach((el, i) => {
      const sel = i === ed.sel;
      const inv = el.invert;
      g.fillStyle = inv ? (el.colour || AMBER) : (sel ? "rgba(255,176,0,.28)" : "rgba(255,176,0,.12)");
      g.fillRect(el.x * scale, el.y * scale, el.w * scale, el.h * scale);
      g.strokeStyle = sel ? "#ff4444" : AMBER; g.lineWidth = sel ? 2 : 1;
      g.strokeRect(el.x * scale, el.y * scale, el.w * scale, el.h * scale);
      g.fillStyle = inv ? "#000" : (sel ? "#fff" : AMBER);
      g.font = `${Math.max(9, 3 * scale)}px monospace`;
      g.textBaseline = "top"; g.textAlign = "left";
      const label = (el.type === "field" ? el.field : (el.text || "text").slice(0, 10)) + (inv ? " inv" : "");
      g.fillText(label, el.x * scale + 2, el.y * scale + 2);
      if (sel) {
        g.fillStyle = "#ff4444";
        g.fillRect((el.x + el.w) * scale - 5, (el.y + el.h) * scale - 5, 10, 10);
      }
    });
  }

  // ---------- LED preview ----------
  function drawPreview(t) {
    if (!ed) return;
    const { lay, ovText, pscale } = ed;
    const off = document.createElement("canvas");
    off.width = lay.width; off.height = lay.height;
    const g = off.getContext("2d");
    g.fillStyle = lay.background || "#000000";
    g.fillRect(0, 0, off.width, off.height);
    const ovActive = !!ovText;
    const ov = lay.override || {};
    // One pages system: the destination's board pages rotate every
    // page_seconds, but inside the layout editor the preview stays locked
    // to the selected board tab so the user edits exactly what they see.
    // The main Preview tab (app.js) does the real rotation.
    const slot = slotFor(lay, t);
    let fields = ed.fields;
    let boardPos = 0;
    let boardLabel = "";
    if (ed.pages && ed.pages.length) {
      const ordered = orderedBoardPages();
      boardPos = ed.tab || ordered[0].position;
      const pg = ordered.find((p) => p.position === boardPos) || ordered[0];
      fields = Object.assign({}, ed.fields, { via: pg.via || "", dest: pg.dest || "" });
      boardLabel = ordered.length > 1
        ? `Board P${pg.position} of ${ordered.length}` : "";
    } else {
      boardPos = boardPosForSlot(slot);
    }
    const previewEls = activePageElements(lay, boardPos) || [];
    if (ov.mode === "full" && ovActive) {
      const ovEl = previewEls.find((e) => e.field === (ov.replaces || "dest"));
      const ovHan = ovEl && hanover.isHan(ovEl.font) ? hanGet(ovEl.font) : null;
      if (ovHan) {
        hanDraw(g, ovHan, ovText, 2, Math.floor(lay.height / 2 - ovHan.height / 2), AMBER);
      } else {
        g.fillStyle = AMBER; g.font = `${Math.floor(lay.height / 2)}px monospace`;
        g.textBaseline = "middle"; g.textAlign = "left";
        g.fillText(ovText, 2, lay.height / 2);
      }
    } else {
      const hide = hideSet(lay, ovActive);
      for (const el of previewEls) {
        if (el.field && hide.has(el.field)) continue;
        if (!visible(el, fields, ovActive)) continue;
        if (el.flash_ms && (Math.floor(t * 1000) % (el.flash_ms * 2)) >= el.flash_ms) continue;
        const text = elText(el, fields, ovText, lay);
        if (!text) continue;
        const han = hanover.isHan(el.font) ? hanGet(el.font) : null;
        const px = fontPx(el);
        g.font = `${px}px monospace`;
        const ink = el.colour || AMBER;
        const paper = lay.background || "#000000";
        g.textBaseline = el.valign === "middle" ? "middle" : (el.valign === "bottom" ? "bottom" : "top");
        g.textAlign = cssAlign(el.align || "left");
        const bx = el.x, by = el.y, bw = el.w, bh = el.h;
        const ty = el.valign === "middle" ? by + bh / 2 : (el.valign === "bottom" ? by + bh : by);
        const sc = han ? hanScale(el) : 1;
        const hsp = han ? hanSpacing(el) : 0;
        const tw = han ? hanWidth(han, text, hsp) * sc : g.measureText(text).width;
        const needScroll = el.scroll === "always" || (el.scroll === "if_too_long" && tw > bw);
        // Inverted boxes fill with the element colour and draw text in black.
        if (el.invert && !needScroll) {
          g.fillStyle = ink; g.fillRect(bx, by, bw, bh);
        }
        // No clipping for static text: ink may legally overflow its box (wide
        // glyphs, tall fonts) and the bus shows it. Only scrolling crops.
        if (!needScroll) {
          if (han) {
            const sh = han.height * sc;
            const y0 = el.valign === "middle" ? by + Math.floor((bh - sh) / 2)
              : (el.valign === "bottom" ? by + bh - sh : by);
            let tx = bx + 1;
            if (el.align === "centre" || el.align === "center") tx = bx + Math.floor((bw - tw) / 2);
            if (el.align === "right") tx = bx + bw - tw;
            g.drawImage(hanStrip(han, sc, text, el.invert ? paper : ink, hsp), tx, y0);
          } else {
            g.fillStyle = el.invert ? paper : ink;
            let tx = bx + 1;
            if (g.textAlign === "center") tx = bx + bw / 2;
            if (g.textAlign === "right") tx = bx + bw - 1;
            g.fillText(text, tx, ty);
          }
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
          const strip = hanStrip(han, sc, text, el.invert ? paper : ink, hsp);
          const s2 = document.createElement("canvas");
          s2.width = Math.ceil(tw + bw); s2.height = bh;
          s2.getContext("2d").drawImage(strip, 0, Math.max(0, y0 - by));
          g.drawImage(s2, offx, 0, bw, bh, bx, by, bw, bh);
        } else {
          g.fillStyle = el.invert ? paper : ink;
          const offx = Math.floor(t * 20) % Math.floor(tw + bw);
          let tx = bx + bw - offx;
          if (g.textAlign === "center") tx = bx + bw / 2 - offx + (tw + bw) / 2 - tw / 2;
          if (g.textAlign === "right") tx = bx + bw - offx;
          if (g.textAlign === "left") tx = bx - offx;
          g.fillText(text, tx, ty);
        }
        g.restore();
      }
    }
    const cv = document.getElementById("lay-preview");
    if (!cv) return;
    cv.width = lay.width * pscale; cv.height = lay.height * pscale;
    const p = cv.getContext("2d");
    p.imageSmoothingEnabled = false;
    p.drawImage(off, 0, 0, cv.width, cv.height);
    p.fillStyle = "rgba(0,0,0,.45)";
    for (let x = 0; x < cv.width; x += pscale) p.fillRect(x, 0, 1, cv.height);
    for (let y = 0; y < cv.height; y += pscale) p.fillRect(0, y, cv.width, 1);
    const pg = document.getElementById("lay-page");
    if (pg) pg.textContent = boardLabel;
  }

  function loop(t) {
    if (!ed) return;
    drawPreview(t / 1000);
    raf = requestAnimationFrame(loop);
  }

  // ---------- properties panel ----------
  function fontSizeKey(font) {
    if (FONTS.includes(font)) return font;
    // Hanover font ids: han-<stem>. Numeric prefix gives the size group;
    // stems with no digits (e.g. LOGO) keep their full id as the group.
    const m = (font || "").match(/^han-.*?\b(\d+)/);
    return m ? "han-" + m[1] : (font || "");
  }
  function fontSizeOptions(selectedFont) {
    const han = (ed.hanIds || []).filter((f) => typeof f === "string" && f.startsWith("han-"));
    const groups = new Map();
    for (const f of han) {
      const key = fontSizeKey(f);
      if (!groups.has(key)) {
        const digits = key.match(/^han-(\d+)$/);
        groups.set(key, digits ? `Hanover ${digits[1]} px` : key.replace("han-", "Hanover "));
      }
    }
    const sizes = [
      { key: "pixel-small", label: "Built-in 8 px" },
      { key: "pixel-medium", label: "Built-in 12 px" },
      { key: "pixel-large", label: "Built-in 20 px" },
      { key: "pixel-huge", label: "Built-in 32 px" },
      { key: "mono-5x7", label: "Built-in 5×7" },
      { key: "mono-6x8", label: "Built-in 6×8" },
      ...[...groups.keys()].sort().map((k) => ({ key: k, label: groups.get(k) }))
    ];
    const sel = fontSizeKey(selectedFont);
    return sizes.map((s) => `<option value="${esc(s.key)}"${s.key === sel ? " selected" : ""}>${esc(s.label)}</option>`).join("");
  }
  function fontStyleOptions(selectedFont) {
    const size = fontSizeKey(selectedFont);
    if (FONTS.includes(size)) {
      return `<option value="${esc(size)}" selected>${esc(size)}</option>`;
    }
    const han = (ed.hanIds || []).filter((f) => typeof f === "string" && fontSizeKey(f) === size);
    const currentStyle = styleSuffix(selectedFont);
    return han.map((f) => {
      const label = styleSuffix(f) || f;
      return `<option value="${esc(f)}"${f === selectedFont ? " selected" : ""}>${esc(label)}</option>`;
    }).join("");
  }
  function styleSuffix(font) {
    // Return the part after the size digits, or the stem if there are no digits.
    const m = font.match(/^han-.*?\b(\d+)(.*)$/);
    if (m) return m[2] || font;
    const stem = font.replace(/^han-/, "");
    return stem || font;
  }
  function stylesForSize(size) {
    if (FONTS.includes(size)) return [size];
    return (ed.hanIds || []).filter((f) => typeof f === "string" && fontSizeKey(f) === size);
  }
  function renderProps() {
    const box = document.getElementById("lay-props");
    const list = document.getElementById("lay-ellist");
    if (!box) return;
    const els = ed.lay.elements || [];
    list.innerHTML = els.map((e, i) =>
      `<option value="${i}"${i === ed.sel ? " selected" : ""}>#${i + 1} ${(e.type === "field" ? e.field : "text")}</option>`).join("");
    const el = els[ed.sel];
    if (!el) { box.innerHTML = "<p class=hint>Select an element on the canvas or list, or add one.</p>"; return; }
    const num = (k, label, opts = "") => `<div><label>${label}</label><input data-p=${k} type=number ${opts} value="${el[k] ?? 0}"></div>`;
    const fieldSelect = el.type === "field"
      ? `<select data-p=field>${["num", "via", "dest", ...ed.extraKeys].map((f) => `<option${f === el.field ? " selected" : ""}>${f}</option>`).join("")}</select>`
      : `<input data-p=text value="${esc(el.text || "")}">`;
    box.innerHTML = `
      <div class="props-section"><div class="props-title">Position &amp; size</div>
        <div class=row>${num("x", "X")}${num("y", "Y")}${num("w", "Width")}${num("h", "Height")}</div>
      </div>
      <div class="props-section"><div class="props-title">Content</div>
        <div class=row>
          <div style="flex:0 0 110px"><label>Type</label><select data-p=type><option value="field"${el.type === "field" ? " selected" : ""}>field</option><option value="text"${el.type === "text" ? " selected" : ""}>fixed text</option></select></div>
          <div class=col><label>${el.type === "field" ? "Field" : "Fixed text"}</label>${fieldSelect}</div>
        </div>
      </div>
      <div class="props-section"><div class="props-title">Appearance</div>
        <div class=row>
          <div style="flex:0 0 130px"><label>Font size</label><select data-p=font-size>${fontSizeOptions(el.font)}</select></div>
          <div class=col><label>Font style</label><select data-p=font-style>${fontStyleOptions(el.font)}</select></div>
          <div style="flex:0 0 80px"><label>Colour</label><input data-p=colour type=color value="${/^#[0-9a-fA-F]{6}$/.test(el.colour || "") ? el.colour : "#FFB000"}"></div>
        </div>
        <div class=row>
          <div style="flex:0 0 110px"><label>Align</label><select data-p=align>${["left", "centre", "right"].map((a) => `<option${a === el.align ? " selected" : ""}>${a}</option>`).join("")}</select></div>
          <div style="flex:0 0 110px"><label>Valign</label><select data-p=valign>${["top", "middle", "bottom"].map((a) => `<option${a === el.valign ? " selected" : ""}>${a}</option>`).join("")}</select></div>
          <div style="flex:0 0 90px"><label>Scale</label><input data-p=scale type=number min=1 max=8 value="${el.scale || 1}"></div>
          <div style="flex:0 0 90px"><label>Spacing</label><input data-p=spacing type=number min=-8 max=16 value="${el.spacing || 0}"></div>
        </div>
      </div>
      <div class="props-section"><div class="props-title">Behaviour</div>
        <div class=row>
          <div style="flex:0 0 130px"><label>Scroll</label><select data-p=scroll>${["none", "always", "if_too_long"].map((a) => `<option${a === el.scroll ? " selected" : ""}>${a}</option>`).join("")}</select></div>
          <div style="flex:0 0 100px"><label>Flash ms</label><input data-p=flash_ms type=number min=0 step=100 value="${el.flash_ms || 0}"></div>
          <div class=col><label>Show if</label><select data-p=show_if>${["always", "has_text", "override_active", "override_inactive"].map((a) => `<option${(el.show_if || "always") === a ? " selected" : ""}>${a}</option>`).join("")}</select></div>
          <div style="flex:0 0 70px;display:flex;align-items:flex-end"><label style="display:flex;align-items:center;gap:6px;cursor:pointer"><input type=checkbox data-p=invert${el.invert ? " checked" : ""}> Invert</label></div>
        </div>
      </div>
      <div class="row actions" style="margin-top:14px"><button id=lay-del class=danger>Delete element</button></div>`;
    box.querySelectorAll("[data-p]").forEach((inp) => inp.addEventListener("change", () => {
      const k = inp.dataset.p;
      let v = inp.type === "checkbox" ? inp.checked : inp.value;
      if (["x", "y", "w", "h", "flash_ms", "scale", "spacing"].includes(k))
        v = k === "scale" ? Math.min(8, Math.max(1, parseInt(v || "1", 10) || 1))
          : k === "spacing" ? Math.min(16, Math.max(-8, parseInt(v || "0", 10) || 0))
          : Math.max(k === "w" || k === "h" ? 1 : 0, parseInt(v || "0", 10));
      if (k === "font-size") {
        const styles = stylesForSize(v);
        const newFont = styles[0] || v;
        if (el.font === newFont) return;
        el.font = newFont;
      } else if (k === "font-style") {
        if (el.font === v) return;
        el.font = v;
      } else {
        el[k] = v;
      }
      updateStatus(); drawEditor(); renderProps();
    }));
    const del = document.getElementById("lay-del");
    if (del) del.onclick = () => { els.splice(ed.sel, 1); ed.sel = Math.max(0, els.length - 1); updateStatus(); drawEditor(); renderProps(); };
  }

  function updateStatus() {
    const s = document.getElementById("lay-status");
    if (s) {
      const n = orderedBoardPages().length;
      const tab = n > 1 ? ` · arranging board P${ed.tab} of ${n}` : "";
      s.textContent = `${(ed.lay.elements || []).length} element(s) · ${ed.lay.width}×${ed.lay.height}${tab} · source: ${ed.src}`;
    }
    renderBoardTabs();
  }

  function renderOverride() {
    const box = document.getElementById("lay-ov");
    if (!box) return;
    const ov = ed.lay.override || (ed.lay.override = { mode: "replace", replaces: "dest", hide: [] });
    const fields = ["num", "via", "dest", ...ed.extraKeys];
    box.innerHTML = `<div class=row>
      <div><label>While override active</label><select id=lay-ovmode>${["replace", "hide_others", "full"].map((m) => `<option${ov.mode === m ? " selected" : ""}>${m}</option>`).join("")}</select></div>
      <div><label>Override replaces</label><select id=lay-ovrep>${fields.map((f) => `<option${ov.replaces === f ? " selected" : ""}>${f}</option>`).join("")}</select></div>
    </div>
    <div class=row>
      <div><label>Hide while override active</label><div>${fields.map((f) => `<label><input type=checkbox data-hide="${f}"${(ov.hide || []).includes(f) ? " checked" : ""}> ${f}</label>`).join(" ")}</div></div>
    </div>`;
    document.getElementById("lay-ovmode").onchange = (e) => { ov.mode = e.target.value; };
    document.getElementById("lay-ovrep").onchange = (e) => { ov.replaces = e.target.value; };
    box.querySelectorAll("[data-hide]").forEach((c) => c.onchange = () => {
      ov.hide = [...box.querySelectorAll("[data-hide]:checked")].map((x) => x.dataset.hide);
    });
  }

  // ---------- mouse interaction ----------
  function canvasPos(e) {
    const cv = document.getElementById("lay-canvas");
    const r = cv.getBoundingClientRect();
    return { x: Math.floor((e.clientX - r.left) / ed.scale), y: Math.floor((e.clientY - r.top) / ed.scale) };
  }
  function hitTest(p) {
    const els = ed.lay.elements || [];
    for (let i = els.length - 1; i >= 0; i--) {
      const e = els[i];
      if (p.x >= e.x && p.x < e.x + e.w && p.y >= e.y && p.y < e.y + e.h) return i;
    }
    return -1;
  }
  function onHandle(mx, my) {
    const el = (ed.lay.elements || [])[ed.sel];
    if (!el) return false;
    const hx = (el.x + el.w) * ed.scale, hy = (el.y + el.h) * ed.scale;
    return Math.abs(mx - hx) <= 7 && Math.abs(my - hy) <= 7;
  }
  function bindMouse() {
    const cv = document.getElementById("lay-canvas");
    let drag = null;
    cv.onmousedown = (e) => {
      const cvr = cv.getBoundingClientRect();
      const mx = e.clientX - cvr.left, my = e.clientY - cvr.top;
      const p = canvasPos(e);
      if (ed.sel >= 0 && onHandle(mx, my)) {
        drag = { mode: "resize" };
      } else {
        const i = hitTest(p);
        ed.sel = i;
        renderProps(); drawEditor();
        if (i < 0) return;
        const el = ed.lay.elements[i];
        drag = { mode: "move", dx: p.x - el.x, dy: p.y - el.y };
      }
      e.preventDefault();
    };
    cv.onmousemove = (e) => {
      if (!drag || ed.sel < 0) return;
      const p = canvasPos(e);
      const el = ed.lay.elements[ed.sel];
      if (drag.mode === "move") { el.x = p.x - drag.dx; el.y = p.y - drag.dy; }
      else { el.w = Math.max(1, p.x - el.x + 1); el.h = Math.max(1, p.y - el.y + 1); }
      drawEditor();
    };
    cv.onmouseup = () => {
      if (drag && ed.sel >= 0) {
        const el = ed.lay.elements[ed.sel];
        el.x = Math.round(el.x); el.y = Math.round(el.y);
        el.w = Math.round(el.w); el.h = Math.round(el.h);
        renderProps(); drawEditor();
      }
      drag = null;
    };
  }

  function addElement(type) {
    const lay = ed.lay;
    const base = { type, x: 0, y: 0, w: Math.floor(lay.width / 2), h: lay.height,
      font: "pixel-medium", colour: AMBER, align: "left", valign: "middle",
      scroll: "if_too_long", flash_ms: 0, page: 1, show_if: "always", invert: false };
    if (type === "field") base.field = "dest";
    else base.text = "MESSAGE";
    lay.elements.push(base);
    ed.sel = lay.elements.length - 1;
    updateStatus(); drawEditor(); renderProps();
  }

  // ---------- open ----------
  async function open(mount, ctx) {
    stop();
    const { did, dest, serviceNumber, displays, apiFn, errFn } = ctx;
    let extraKeys = [];
    try { extraKeys = Object.keys(JSON.parse(dest.extra_fields || "{}")); } catch (e) { /* ignore */ }
    ed = { did, lay: null, src: "", sel: -1, scale: 3, pscale: 2, tab: 1, hanIds: [],
           fields: { num: dest.num || serviceNumber || "", via: dest.via || "", dest: dest.dest || "" },
           pages: (ctx.pages && ctx.pages.length ? ctx.pages :
             [{ via: dest.via || "", dest: dest.dest || "" }]),
           ovText: "", extraKeys, displays, apiFn, errFn };
    apiFn("/api/fonts").then((r) => { if (ed) { ed.hanIds = (r.fonts || []).map((f) => f.id); renderProps(); } })
      .catch(() => { if (ed) ed.hanIds = []; });
    for (const k of extraKeys) { try { ed.fields[k] = JSON.parse(dest.extra_fields)[k]; } catch (e) { /* ignore */ } }
    mount.innerHTML = `
      <div class=prog-header>
        <div class=col><h2 style="margin:0">Display layout</h2><div class=meta>${esc(dest.name)} · code <span class=kbd>${esc(dest.code)}</span> · service ${esc(serviceNumber || "")}</div></div>
        <div style="align-self:end" class=row>
          <div style="flex:0 0 180px"><label for=lay-disp>Display</label><select id=lay-disp>${displays.map((d) => `<option value="${esc(d.id)}">${esc(d.name)} ${esc(d.width_px)}×${esc(d.height_px)}</option>`).join("")}</select></div>
          <div style="align-self:end" class=row><button id=lay-load>Load</button><button id=lay-autofit>Auto-fit</button><button id=lay-save class=primary>Save layout</button><button id=lay-close>Close</button></div>
        </div>
      </div>
      <div class=hint id=lay-status></div>

      <div class=lay-shell>
        <!-- LEFT: tools -->
        <div class=lay-tools>
          <div class=group style="margin-top:0"><div class=grouptitle>Tools</div>
            <div class=row><button id=lay-addfield class=primary style="flex:1">Add field</button></div>
            <div class=row><button id=lay-addtext style="flex:1">Add fixed text</button></div>
          </div>
          <div class=group><div class=grouptitle>Elements</div>
            <select id=lay-ellist size=8 style="min-height:120px"></select>
            <div class=hint>Click an element to select it.</div>
          </div>
          <div class=group><div class=grouptitle>Board rotation</div>
            <div class=hint>The destination's board pages (via/dest per page, set in the Destinations tab) rotate on the boards every few seconds. Pick a board tab to arrange that page's layout - each page can look different. New elements land on the selected tab.</div>
            <div id=lay-boardtabs class=page-list></div>
            <div class=row>
              <div style="flex:0 0 80px"><label>Seconds</label><input id=lay-seconds class=page-seconds type=number min=1 step=1 value=3></div>
            </div>
          </div>
          <div class=group><div class=grouptitle>Preview inputs</div>
            <div class=hint id=lay-pages-hint></div>
            <div style="flex:1"><label for=lay-fnum>num</label><input id=lay-fnum></div>
            <div style="flex:1" id=lay-fvia-wrap><label for=lay-fvia>via</label><input id=lay-fvia></div>
            <div style="flex:1" id=lay-fdest-wrap><label for=lay-fdest>dest</label><input id=lay-fdest></div>
            <div><label for=lay-fov>Override text (empty = inactive)</label><input id=lay-fov></div>
          </div>
        </div>

        <!-- CENTER: canvas + preview -->
        <div class=lay-canvas>
          <div class=group style="margin-top:0"><div class=grouptitle>Edit canvas <span class=hint>click selects, drag moves, corner resizes</span></div>
            <canvas id=lay-canvas style="cursor:crosshair"></canvas>
          </div>
          <div class=group><div class=grouptitle>Live LED preview <span id=lay-page class=hint></span></div>
            <div class=lay-preview><canvas id=lay-preview></canvas></div>
          </div>
        </div>

        <!-- RIGHT: properties -->
        <div class=lay-props>
          <div class=group style="margin-top:0"><div class=grouptitle>Selected element</div><div id=lay-props></div></div>
          <div class=group><div class=grouptitle>Override behaviour</div><div id=lay-ov></div></div>
          <div class=group><div class=grouptitle>Advanced</div>
            <button id=lay-jsonbtn>Edit JSON</button>
            <div id=lay-jsonbox style="display:none;margin-top:8px"><label for=lay-json>Layout JSON</label><textarea id=lay-json rows=8></textarea>
            <div class=row><button id=lay-jsonapply>Apply JSON</button></div></div>
          </div>
        </div>
      </div>
      <div class=err id=lay-err></div>`;
    mount.scrollIntoView();
    const disp = () => displays.find((d) => d.id === document.getElementById("lay-disp").value) || displays[0];
    const fitScales = () => {
      const d = disp();
      ed.scale = Math.max(2, Math.round(640 / d.width_px));
      ed.pscale = Math.max(2, Math.round(480 / d.width_px));
    };
    const loadFields = () => {
      document.getElementById("lay-fnum").value = ed.fields.num || "";
      document.getElementById("lay-fvia").value = ed.fields.via || "";
      document.getElementById("lay-fdest").value = ed.fields.dest || "";
      const multi = ed.pages && ed.pages.length > 1;
      const hint = document.getElementById("lay-pages-hint");
      if (hint) hint.textContent = multi
        ? `This destination has ${ed.pages.length} board pages - the preview rotates through each page's via/dest, exactly like the bus. Edit pages in the Destinations tab.`
        : "";
      // via/dest preview inputs only apply with a single board page
      const vw = document.getElementById("lay-fvia-wrap"), dw = document.getElementById("lay-fdest-wrap");
      if (vw) vw.style.display = multi ? "none" : "";
      if (dw) dw.style.display = multi ? "none" : "";
    };
    const readFields = () => {
      ed.fields.num = document.getElementById("lay-fnum").value;
      ed.fields.via = document.getElementById("lay-fvia").value;
      ed.fields.dest = document.getElementById("lay-fdest").value;
      ed.ovText = document.getElementById("lay-fov").value;
    };
    ["lay-fnum", "lay-fvia", "lay-fdest", "lay-fov"].forEach((id) =>
      document.getElementById(id).addEventListener("input", readFields));
    const doLoad = async () => {
      errFn("");
      try {
        const d = disp();
        const r = await apiFn(`/api/destinations/${did}/layouts/${encodeURIComponent(d.name)}`);
        fitScales();
        if (r.layout) { ed.lay = r.layout; ed.src = r.source; }
        else { ed.lay = blank(d.width_px, d.height_px); ed.src = "none yet (blank canvas)"; }
        ed.tab = 1;
        ensurePageLayouts();
        ed.sel = (ed.lay.elements || []).length ? 0 : -1;
        document.getElementById("lay-seconds").value = ed.lay.page_seconds || 3;
        loadFields(); readFields();
        updateStatus(); drawEditor(); renderProps(); renderOverride();
      } catch (e) { document.getElementById("lay-err").textContent = String(e.message || e); }
    };
    document.getElementById("lay-load").onclick = doLoad;
    document.getElementById("lay-disp").onchange = doLoad;
    document.getElementById("lay-close").onclick = () => { stop(); mount.innerHTML = ""; };
    document.getElementById("lay-autofit").onclick = () => {
      const d = disp(); fitScales();
      ed.lay = autofit(d.width_px, d.height_px); ed.src = "auto-fit starter (unsaved)";
      ed.tab = 1;
      ensurePageLayouts();
      ed.sel = (ed.lay.elements || []).length ? 0 : -1;
      updateStatus(); drawEditor(); renderProps(); renderOverride();
    };
    document.getElementById("lay-addfield").onclick = () => addElement("field");
    document.getElementById("lay-addtext").onclick = () => addElement("text");
    document.getElementById("lay-ellist").onchange = (e) => {
      ed.sel = parseInt(e.target.value, 10);
      drawEditor(); renderProps();
    };
    document.getElementById("lay-seconds").addEventListener("input", syncPageSeconds);
    document.getElementById("lay-jsonbtn").onclick = () => {
      const box = document.getElementById("lay-jsonbox");
      box.style.display = box.style.display === "none" ? "" : "none";
      if (ed.lay) document.getElementById("lay-json").value = JSON.stringify(ed.lay, null, 2);
    };
    document.getElementById("lay-jsonapply").onclick = () => {
      try {
        const lj = JSON.parse(document.getElementById("lay-json").value);
        ed.lay = lj; ed.src = "from JSON (unsaved)";
        ed.tab = 1;
        ensurePageLayouts();
        ed.sel = (ed.lay.elements || []).length ? 0 : -1;
        document.getElementById("lay-err").textContent = "";
        updateStatus(); drawEditor(); renderProps(); renderOverride();
      } catch (e) { document.getElementById("lay-err").textContent = "Invalid JSON: " + e.message; }
    };
    document.getElementById("lay-save").onclick = async () => {
      const box = document.getElementById("lay-err");
      try {
        const d = disp();
        if (!ed.lay) throw new Error("Load a display first.");
        if (ed.lay.width !== d.width_px || ed.lay.height !== d.height_px)
          throw new Error(`Layout is ${ed.lay.width}×${ed.lay.height} but display ${d.name} is ${d.width_px}×${d.height_px}.`);
        // freeze current tab into its page slot before saving
        const pe = ed.lay.page_elements || (ed.lay.page_elements = {});
        const currentTabEls = ed.lay.elements;
        pe[ed.tab] = currentTabEls;
        // use the first board page as the fallback/default elements list
        const ordered = orderedBoardPages();
        if (ordered.length > 1) {
          ed.lay.elements = JSON.parse(JSON.stringify(pe[ordered[0].position] || currentTabEls));
        }
        // keep the editor on the active tab's layout
        ed.lay.elements = currentTabEls;
        const allLayouts = ordered.length > 1
          ? ordered.map((pg) => ({ name: `page ${pg.position}`, els: pe[pg.position] || [] }))
          : [{ name: "default", els: ed.lay.elements || [] }];
        for (const { name, els } of allLayouts) {
          for (const [i, e] of els.entries()) {
            if (e.x < 0 || e.y < 0 || e.w <= 0 || e.h <= 0 || e.x + e.w > d.width_px || e.y + e.h > d.height_px)
              throw new Error(`Layout ${name}, element #${i + 1} lies outside the display - move or resize it.`);
          }
        }
        await apiFn(`/api/destinations/${did}/layouts/${encodeURIComponent(d.name)}`, { method: "PUT", body: { layout: ed.lay } });
        box.textContent = ""; ed.src = "destination (saved)";
        updateStatus(); alert("Layout saved.");
      } catch (e) { box.textContent = String(e.message || e); }
    };
    bindMouse();
    await doLoad();
    readFields();
    raf = requestAnimationFrame(loop);
  }

  function isOpen() { return !!ed; }
  function loadLayout(layout, source, displayId) {
    if (!ed) return false;
    const dispSel = document.getElementById("lay-disp");
    if (displayId && dispSel) {
      const match = (ed.displays || []).find((d) => d.id === displayId);
      if (match && dispSel.value !== displayId) {
        dispSel.value = displayId;
        dispSel.dispatchEvent(new Event("change"));
      }
    }
    const d = (ed.displays || []).find((x) => x.id === document.getElementById("lay-disp").value) || ed.displays[0];
    if (!d) return false;
    if (layout.width !== d.width_px || layout.height !== d.height_px) {
      document.getElementById("lay-err").textContent =
        `Warning: preset is ${layout.width}×${layout.height} but ${d.name} is ${d.width_px}×${d.height_px}. Resize the display first if needed.`;
    }
    ed.lay = JSON.parse(JSON.stringify(layout));
    ed.src = source || "template";
    ed.tab = 1;
    ensurePageLayouts();
    ed.sel = (ed.lay.elements || []).length ? 0 : -1;
    document.getElementById("lay-seconds").value = ed.lay.page_seconds || 3;
    updateStatus(); drawEditor(); renderProps(); renderOverride();
    return true;
  }

  return { open, stop, hanover, isOpen, loadLayout };
})();
