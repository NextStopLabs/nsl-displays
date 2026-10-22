/* Fonts tab: preview every Hanover bitmap font + built-ins with your own text.
   Renders with the same blitter the layout editor uses, so WYSIWYG holds. */
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const BUILTINS = [
    { id: "pixel-small", height: 8 }, { id: "pixel-medium", height: 12 },
    { id: "pixel-large", height: 20 }, { id: "pixel-huge", height: 32 },
    { id: "mono-5x7", height: 7 }, { id: "mono-6x8", height: 8 },
  ];
  const S = { fonts: [], detail: {}, filter: "", sort: "height" };

  window.FontsTab = { init() {
    if ($("ft-init")) { return; }
    $("tab-fonts").innerHTML = `
    <div id="fe-mount"></div>
    <div class="group"><div class="grouptitle">Font previewer</div><span id="ft-init"></span>
      <p class="hint">Every Hanover bitmap font (<span class="kbd">han-…</span> ids for layouts) plus the built-in pixel fonts, drawn with the real glyphs. Click a row to copy its id.</p>
      <div class="row">
        <div class="col"><label for="ft-text">Sample text</label><input id="ft-text" value="Burton 100 via Branston"></div>
        <div style="flex:0 0 110px"><label for="ft-scale">Preview zoom</label><select id="ft-scale"><option value="2">×2</option><option value="3" selected>×3</option><option value="4">×4</option></select></div>
        <div style="flex:0 0 130px"><label for="ft-sort">Sort by</label><select id="ft-sort"><option value="height">Pixel height</option><option value="name">Name</option><option value="coverage">Coverage</option></select></div>
      </div>
      <div class="row"><div class="col"><label for="ft-filter">Filter</label><input id="ft-filter" placeholder="e.g. 19, bold, arial"></div>
        <div style="align-self:end"><label style="font-weight:normal"><input type="checkbox" id="ft-full" style="width:auto"> Full ASCII only</label></div></div>
      <div class="hint" id="ft-count"></div>
      <div id="ft-list"></div>
      <div class="err" id="ft-err"></div>
    </div>`;
    for (const id of ["ft-text", "ft-scale", "ft-sort", "ft-filter", "ft-full"])
      $(id).addEventListener("input", () => { S.filter = $("ft-filter").value.toLowerCase(); S.sort = $("ft-sort").value; paint(); });
    if (window.FontEdit) FontEdit.mount($("fe-mount"));
    load();
  } };

  async function load() {
    try {
      const r = await api("/api/fonts");
      S.fonts = (r.fonts || []).slice();
      paint();
      // fetch glyph details in the background, few at a time
      const q = S.fonts.slice();
      const workers = Array.from({ length: 4 }, async () => {
        while (q.length) {
          const f = q.shift();
          try { S.detail[f.id] = await api("/api/fonts/" + encodeURIComponent(f.id)); }
          catch (e) { S.detail[f.id] = null; }
          paintOne(f.id);
        }
      });
      await Promise.all(workers);
      paint();
    } catch (e) { $("ft-err").textContent = String(e.message || e); }
  }

  function rows() {
    let list = S.fonts.slice();
    if ($("ft-full") && $("ft-full").checked) list = list.filter((f) => (f.coverage || 0) >= 95);
    if (S.filter) list = list.filter((f) => (f.id + " " + (f.name || "")).toLowerCase().includes(S.filter));
    const byH = (a, b) => (a.height - b.height) || (a.id < b.id ? -1 : 1);
    if (S.sort === "name") list.sort((a, b) => (a.id < b.id ? -1 : 1));
    else if (S.sort === "coverage") list.sort((a, b) => (b.coverage || 0) - (a.coverage || 0) || byH(a, b));
    else list.sort(byH);
    return list;
  }

  function paint() {
    const list = rows();
    const built = !S.filter || "pixel mono built-in".includes(S.filter) || BUILTINS.some((b) => b.id.includes(S.filter));
    $("ft-count").textContent = `${list.length} Hanover font(s)` + (Object.keys(S.detail).length < S.fonts.length ? ` · loading glyphs (${Object.keys(S.detail).length}/${S.fonts.length})…` : " · all loaded");
    let h = "";
    if (built) {
      h += `<div class="group"><div class="grouptitle">Built-in pixel fonts</div><div id="ft-built"></div></div>`;
    }
    h += list.map((f) =>
      `<div class="group" data-font="${esc(f.id)}" style="cursor:pointer" title="Click to copy ${esc(f.id)}">
        <b>${esc(f.id)}</b> <span class="hint">${f.height}px · ${f.coverage || 0}/95 ASCII · ${f.glyphs || 0} glyphs</span>
        <span style="float:right"><button class="tool" data-edit="${esc(f.id)}">Edit</button></span>
        <div><canvas data-cv="${esc(f.id)}" style="background:#000;max-width:100%"></canvas></div>
      </div>`).join("") || `<p class="hint">No fonts match.</p>`;
    $("ft-list").innerHTML = h;
    $("ft-list").querySelectorAll("[data-edit]").forEach((b) => b.onclick = (e) => {
      e.stopPropagation();
      if (window.FontEdit) FontEdit.load(b.dataset.edit);
    });
    $("ft-list").querySelectorAll("[data-font]").forEach((el) => el.onclick = () => {
      const id = el.dataset.font;
      (navigator.clipboard ? navigator.clipboard.writeText(id) : Promise.reject())
        .then(() => { $("ft-count").textContent = `Copied ${id}`; })
        .catch(() => { $("ft-count").textContent = id; });
    });
    if (built) paintBuiltins();
    for (const f of list) paintOne(f.id);
  }

  function paintOne(fid) {
    const cv = document.querySelector(`canvas[data-cv="${CSS.escape(fid)}"]`);
    if (!cv) return;
    const f = S.detail[fid];
    const H = (typeof LayoutEditor !== "undefined" && LayoutEditor.hanover) || null;
    const text = ($("ft-text") || {}).value || "";
    const z = parseInt(($("ft-scale") || {}).value || "3", 10);
    if (!f || !H) {
      cv.width = 300; cv.height = 24;
      const g = cv.getContext("2d"); g.fillStyle = "#000"; g.fillRect(0, 0, cv.width, cv.height);
      g.fillStyle = "#666"; g.font = "12px monospace"; g.fillText(f ? "" : "loading…", 6, 16);
      return;
    }
    const tw = Math.max(1, H.width(f, text, 0)), th = Math.max(1, f.height);
    cv.width = tw * z + 8; cv.height = th * z + 8;
    const g = cv.getContext("2d");
    g.fillStyle = "#000"; g.fillRect(0, 0, cv.width, cv.height);
    g.save(); g.scale(z, z); g.translate(4 / z, 4 / z);
    g.imageSmoothingEnabled = false;
    H.draw(g, f, text, 0, 0, "#FFB000", 0);
    g.restore();
    grid(g, cv.width, cv.height, z);
  }

  function paintBuiltins() {
    const box = $("ft-built");
    if (!box) return;
    const text = ($("ft-text") || {}).value || "";
    const z = 2;
    box.innerHTML = BUILTINS.map((b) =>
      `<div data-font="${b.id}" style="cursor:pointer;margin:6px 0" title="Click to copy ${b.id}">
        <b>${b.id}</b> <span class="hint">~${b.height}px · built-in (approximation)</span><br>
        <canvas data-bcv="${b.id}" style="background:#000;max-width:100%"></canvas></div>`).join("");
    box.querySelectorAll("[data-font]").forEach((el) => el.onclick = () => {
      const id = el.dataset.font;
      (navigator.clipboard ? navigator.clipboard.writeText(id) : Promise.reject()).then(() => { $("ft-count").textContent = `Copied ${id}`; });
    });
    for (const b of BUILTINS) {
      const cv = box.querySelector(`canvas[data-bcv="${b.id}"]`);
      const g = cv.getContext("2d");
      g.font = `${b.height}px monospace`;
      const tw = Math.max(1, Math.ceil(g.measureText(text).width));
      cv.width = tw * z + 8; cv.height = (b.height + 4) * z;
      g.fillStyle = "#000"; g.fillRect(0, 0, cv.width, cv.height);
      g.font = `${b.height * z}px monospace`; g.fillStyle = "#FFB000"; g.textBaseline = "top";
      g.fillText(text, 4, 2 * z);
      grid(g, cv.width, cv.height, z);
    }
  }

  function grid(g, w, h, z) {
    g.fillStyle = "rgba(0,0,0,.45)";
    for (let x = 0; x < w; x += z) g.fillRect(x, 0, 1, h);
    for (let y = 0; y < h; y += z) g.fillRect(0, y, w, 1);
  }
})();
