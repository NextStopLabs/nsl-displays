/* Visual board layout editor (plan.md 5.3c). No JSON typing required.
   Canvas at the display's real pixel size + live LED preview + properties panel.
   Uses globals api(), err(), esc() from app.js. */
"use strict";

const LayoutEditor = (() => {
  const FONT_H = { "pixel-small": 8, "pixel-medium": 12, "pixel-large": 20, "pixel-huge": 32, "mono-5x7": 7, "mono-6x8": 8 };
  const FONTS = Object.keys(FONT_H);
  const AMBER = "#FFB000";
  let raf = 0;
  let ed = null; // editor state

  function stop() { if (raf) cancelAnimationFrame(raf); raf = 0; ed = null; }

  function blank(w, h) {
    return { schema: 1, width: w, height: h, background: "#000000", page_seconds: 3,
             override: { mode: "replace", replaces: "dest", hide: [] }, elements: [] };
  }
  function autofit(w, h) {
    const nw = Math.min(56, Math.floor(w / 4));
    return { schema: 1, width: w, height: h, background: "#000000", page_seconds: 3,
      override: { mode: "replace", replaces: "dest", hide: [] },
      elements: [
        { type: "field", field: "num", x: 0, y: 0, w: nw, h: h, font: "pixel-large", colour: AMBER,
          align: "centre", valign: "middle", scroll: "none", flash_ms: 0, page: 1, show_if: "always" },
        { type: "field", field: "dest", x: nw + 4, y: 0, w: w - nw - 4, h: h, font: "pixel-medium", colour: AMBER,
          align: "left", valign: "middle", scroll: "if_too_long", flash_ms: 0, page: 1, show_if: "always" } ] };
  }

  function pagesUsed(lay) {
    const s = new Set((lay.elements || []).map((e) => parseInt(e.page || 1, 10)));
    return [...s].sort((a, b) => a - b);
  }
  function curPage(lay, t) {
    const pages = pagesUsed(lay);
    if (pages.length <= 1) return pages[0] || 1;
    return pages[Math.floor(t / (parseFloat(lay.page_seconds) || 3)) % pages.length];
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
      g.fillStyle = sel ? "rgba(255,176,0,.28)" : "rgba(255,176,0,.12)";
      g.fillRect(el.x * scale, el.y * scale, el.w * scale, el.h * scale);
      g.strokeStyle = sel ? "#ff4444" : AMBER; g.lineWidth = sel ? 2 : 1;
      g.strokeRect(el.x * scale, el.y * scale, el.w * scale, el.h * scale);
      g.fillStyle = sel ? "#fff" : AMBER;
      g.font = `${Math.max(9, 3 * scale)}px monospace`;
      g.textBaseline = "top"; g.textAlign = "left";
      const label = (el.type === "field" ? el.field : (el.text || "text").slice(0, 10)) + (el.page > 1 ? " p" + el.page : "");
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
    const { lay, fields, ovText, pscale } = ed;
    const off = document.createElement("canvas");
    off.width = lay.width; off.height = lay.height;
    const g = off.getContext("2d");
    g.fillStyle = lay.background || "#000000";
    g.fillRect(0, 0, off.width, off.height);
    const ovActive = !!ovText;
    const ov = lay.override || {};
    const page = curPage(lay, t);
    if (ov.mode === "full" && ovActive) {
      g.fillStyle = AMBER; g.font = `${Math.floor(lay.height / 2)}px monospace`;
      g.textBaseline = "middle"; g.textAlign = "left";
      g.fillText(ovText, 2, lay.height / 2);
    } else {
      const hide = hideSet(lay, ovActive);
      for (const el of (lay.elements || [])) {
        if (parseInt(el.page || 1, 10) !== page) continue;
        if (el.field && hide.has(el.field)) continue;
        if (!visible(el, fields, ovActive)) continue;
        if (el.flash_ms && (Math.floor(t * 1000) % (el.flash_ms * 2)) >= el.flash_ms) continue;
        const text = elText(el, fields, ovText, lay);
        if (!text) continue;
        const px = fontPx(el);
        g.font = `${px}px monospace`;
        g.fillStyle = el.colour || AMBER;
        g.textBaseline = el.valign === "middle" ? "middle" : (el.valign === "bottom" ? "bottom" : "top");
        g.textAlign = cssAlign(el.align || "left");
        const bx = el.x, by = el.y, bw = el.w, bh = el.h;
        const ty = el.valign === "middle" ? by + bh / 2 : (el.valign === "bottom" ? by + bh : by);
        const tw = g.measureText(text).width;
        const needScroll = el.scroll === "always" || (el.scroll === "if_too_long" && tw > bw);
        g.save();
        g.beginPath(); g.rect(bx, by, bw, bh); g.clip();
        if (needScroll) {
          const offx = Math.floor(t * 20) % Math.floor(tw + bw);
          let tx = bx + bw - offx;
          if (g.textAlign === "center") tx = bx + bw / 2 - offx + (tw + bw) / 2 - tw / 2;
          if (g.textAlign === "right") tx = bx + bw - offx;
          if (g.textAlign === "left") tx = bx - offx;
          g.fillText(text, tx, ty);
        } else {
          let tx = bx + 1;
          if (g.textAlign === "center") tx = bx + bw / 2;
          if (g.textAlign === "right") tx = bx + bw - 1;
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
    if (pg) pg.textContent = "Page " + page + " of " + pagesUsed(lay).length;
  }

  function loop(t) {
    if (!ed) return;
    drawPreview(t / 1000);
    raf = requestAnimationFrame(loop);
  }

  // ---------- properties panel ----------
  function renderProps() {
    const box = document.getElementById("lay-props");
    const list = document.getElementById("lay-ellist");
    if (!box) return;
    const els = ed.lay.elements || [];
    list.innerHTML = els.map((e, i) =>
      `<option value="${i}"${i === ed.sel ? " selected" : ""}>#${i + 1} ${(e.type === "field" ? e.field : "text")}</option>`).join("");
    const el = els[ed.sel];
    if (!el) { box.innerHTML = "<p class=hint>Select an element on the canvas or list, or add one.</p>"; return; }
    const num = (k, label) => `<div style="flex:1;min-width:70px"><label>${label}</label><input data-p=${k} type=number value="${el[k] ?? 0}"></div>`;
    box.innerHTML = `<div class=row>${num("x", "X")}${num("y", "Y")}${num("w", "Width")}${num("h", "Height")}</div>
      <div class=row>
      <div style="flex:1"><label>Type</label><select data-p=type><option value="field"${el.type === "field" ? " selected" : ""}>field</option><option value="text"${el.type === "text" ? " selected" : ""}>fixed text</option></select></div>
      <div style="flex:1"><label>${el.type === "field" ? "Field" : "Fixed text"}</label>${el.type === "field"
        ? `<select data-p=field>${["num", "via", "dest", ...ed.extraKeys].map((f) => `<option${f === el.field ? " selected" : ""}>${f}</option>`).join("")}</select>`
        : `<input data-p=text value="${(el.text || "").replace(/"/g, "&quot;")}">`}</div>
      </div>
      <div class=row>
      <div style="flex:1"><label>Font</label><select data-p=font>${FONTS.map((f) => `<option${f === el.font ? " selected" : ""}>${f}</option>`).join("")}</select></div>
      <div style="flex:1"><label>Colour</label><input data-p=colour type=color value="${/^#[0-9a-fA-F]{6}$/.test(el.colour || "") ? el.colour : "#FFB000"}"></div>
      <div style="flex:1"><label>Align</label><select data-p=align>${["left", "centre", "right"].map((a) => `<option${a === el.align ? " selected" : ""}>${a}</option>`).join("")}</select></div>
      <div style="flex:1"><label>Valign</label><select data-p=valign>${["top", "middle", "bottom"].map((a) => `<option${a === el.valign ? " selected" : ""}>${a}</option>`).join("")}</select></div>
      </div>
      <div class=row>
      <div style="flex:1"><label>Scroll</label><select data-p=scroll>${["none", "always", "if_too_long"].map((a) => `<option${a === el.scroll ? " selected" : ""}>${a}</option>`).join("")}</select></div>
      <div style="flex:1"><label>Flash ms (0 = steady)</label><input data-p=flash_ms type=number min=0 step=100 value="${el.flash_ms || 0}"></div>
      <div style="flex:1"><label>Page</label><input data-p=page type=number min=1 value="${el.page || 1}"></div>
      <div style="flex:1"><label>Show if</label><select data-p=show_if>${["always", "has_text", "override_active", "override_inactive"].map((a) => `<option${(el.show_if || "always") === a ? " selected" : ""}>${a}</option>`).join("")}</select></div>
      </div>
      <div class=row><button id=lay-del>Delete element</button></div>`;
    box.querySelectorAll("[data-p]").forEach((inp) => inp.addEventListener("change", () => {
      const k = inp.dataset.p;
      let v = inp.value;
      if (["x", "y", "w", "h", "flash_ms", "page"].includes(k)) v = Math.max(k === "w" || k === "h" ? 1 : 0, parseInt(v || "0", 10));
      el[k] = v;
      updateStatus(); drawEditor(); renderProps();
    }));
    const del = document.getElementById("lay-del");
    if (del) del.onclick = () => { els.splice(ed.sel, 1); ed.sel = Math.max(0, els.length - 1); updateStatus(); drawEditor(); renderProps(); };
  }

  function updateStatus() {
    const s = document.getElementById("lay-status");
    if (s) s.textContent = `${(ed.lay.elements || []).length} element(s) · ${ed.lay.width}×${ed.lay.height} · source: ${ed.src}`;
  }

  function renderOverride() {
    const box = document.getElementById("lay-ov");
    if (!box) return;
    const ov = ed.lay.override || (ed.lay.override = { mode: "replace", replaces: "dest", hide: [] });
    const fields = ["num", "via", "dest", ...ed.extraKeys];
    box.innerHTML = `<div class=row>
      <div style="flex:1"><label>While override active</label><select id=lay-ovmode>${["replace", "hide_others", "full"].map((m) => `<option${ov.mode === m ? " selected" : ""}>${m}</option>`).join("")}</select></div>
      <div style="flex:1"><label>Override replaces</label><select id=lay-ovrep>${fields.map((f) => `<option${ov.replaces === f ? " selected" : ""}>${f}</option>`).join("")}</select></div>
      <div style="flex:2"><label>Hide while override active</label><div>${fields.map((f) => `<label style="font-weight:normal"><input type=checkbox data-hide="${f}"${(ov.hide || []).includes(f) ? " checked" : ""}> ${f}</label>`).join(" ")}</div></div>
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
      scroll: "if_too_long", flash_ms: 0, page: 1, show_if: "always" };
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
    ed = { did, lay: null, src: "", sel: -1, scale: 3, pscale: 2,
           fields: { num: dest.num || serviceNumber || "", via: dest.via || "", dest: dest.dest || "" },
           ovText: "", extraKeys, displays, apiFn, errFn };
    for (const k of extraKeys) { try { ed.fields[k] = JSON.parse(dest.extra_fields)[k]; } catch (e) { /* ignore */ } }
    mount.innerHTML = `<h3>Board layout — ${dest.name} (${dest.code})</h3>
      <div class=row>
      <div style="flex:1;min-width:140px"><label for=lay-disp>Display</label><select id=lay-disp>${displays.map((d) => `<option value="${d.id}">${d.name} ${d.width_px}×${d.height_px}</option>`).join("")}</select></div>
      <div style="align-self:end" class=row><button id=lay-load>Load</button><button id=lay-autofit>Auto-fit starter</button><button id=lay-save class=primary>Save layout</button></div>
      </div>
      <div class=hint id=lay-status></div>
      <div class=row>
      <div class=group style="flex:1"><div class=grouptitle>Edit (click selects, drag moves, red corner resizes)</div>
        <canvas id=lay-canvas style="max-width:100%;cursor:crosshair"></canvas>
        <div class=row><button id=lay-addfield>Add field</button><button id=lay-addtext>Add fixed text</button>
        <div style="flex:1;min-width:120px"><label for=lay-ellist>Element</label><select id=lay-ellist></select></div></div>
      </div>
      <div class=group style="flex:1"><div class=grouptitle>Live LED preview <span id=lay-page class=hint></span></div>
        <canvas id=lay-preview class=led style="max-width:100%"></canvas>
        <div class=row>
        <div style="flex:1"><label for=lay-fnum>num</label><input id=lay-fnum></div>
        <div style="flex:1"><label for=lay-fvia>via</label><input id=lay-fvia></div>
        <div style="flex:1"><label for=lay-fdest>dest</label><input id=lay-fdest></div>
        </div>
        <div><label for=lay-fov>Override text (empty = inactive)</label><input id=lay-fov></div>
      </div>
      </div>
      <div class=group><div class=grouptitle>Selected element</div><div id=lay-props></div></div>
      <div class=group><div class=grouptitle>Override behaviour</div><div id=lay-ov></div></div>
      <div class=row><button id=lay-jsonbtn>Advanced: edit JSON</button></div>
      <div id=lay-jsonbox style="display:none"><label for=lay-json>Layout JSON</label><textarea id=lay-json rows=10></textarea>
      <div class=row><button id=lay-jsonapply>Apply JSON to editor</button></div></div>
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
        ed.sel = (ed.lay.elements || []).length ? 0 : -1;
        loadFields(); readFields();
        updateStatus(); drawEditor(); renderProps(); renderOverride();
      } catch (e) { document.getElementById("lay-err").textContent = String(e.message || e); }
    };
    document.getElementById("lay-load").onclick = doLoad;
    document.getElementById("lay-disp").onchange = doLoad;
    document.getElementById("lay-autofit").onclick = () => {
      const d = disp(); fitScales();
      ed.lay = autofit(d.width_px, d.height_px); ed.src = "auto-fit starter (unsaved)"; ed.sel = 0;
      updateStatus(); drawEditor(); renderProps(); renderOverride();
    };
    document.getElementById("lay-addfield").onclick = () => addElement("field");
    document.getElementById("lay-addtext").onclick = () => addElement("text");
    document.getElementById("lay-ellist").onchange = (e) => { ed.sel = parseInt(e.target.value, 10); drawEditor(); renderProps(); };
    document.getElementById("lay-jsonbtn").onclick = () => {
      const box = document.getElementById("lay-jsonbox");
      box.style.display = box.style.display === "none" ? "" : "none";
      if (ed.lay) document.getElementById("lay-json").value = JSON.stringify(ed.lay, null, 2);
    };
    document.getElementById("lay-jsonapply").onclick = () => {
      try {
        const lj = JSON.parse(document.getElementById("lay-json").value);
        ed.lay = lj; ed.src = "from JSON (unsaved)"; ed.sel = (lj.elements || []).length ? 0 : -1;
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
        for (const [i, e] of (ed.lay.elements || []).entries()) {
          if (e.x < 0 || e.y < 0 || e.w <= 0 || e.h <= 0 || e.x + e.w > d.width_px || e.y + e.h > d.height_px)
            throw new Error(`Element #${i + 1} lies outside the display — move or resize it.`);
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

  return { open, stop };
})();
