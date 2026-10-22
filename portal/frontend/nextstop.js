/* Next-stop tab: pick a bus and open its GPS display. The display follows
   the bus's live service/destination/stop via the portal API. */
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  window.NextStopTab = { init() {
    if ($("ns-init")) { refresh(); return; }
    $("tab-nextstop").innerHTML = `
    <div class="group"><div class="grouptitle">Next stop display</div><span id="ns-init"></span>
      <p class="hint">Follows the live service on the bus: destination, stops and current stop come from what the driver has selected. Open the display on this laptop and full-screen it with F. No times - stops only.</p>
      <div class="row"><div class="col"><label for="ns-bus">Bus</label><select id="ns-bus"></select></div>
        <div style="align-self:end" class="row"><button id="ns-refresh">Refresh</button>
        <button id="ns-open" class="primary">Open display</button></div></div>
      <div class="hint" id="ns-msg"></div><div class="err" id="ns-err"></div>
    </div>`;
    $("ns-refresh").onclick = refresh;
    $("ns-open").onclick = () => {
      const cid = $("ns-bus").value;
      if (!cid) { $("ns-err").textContent = "Pick a bus first."; return; }
      try { localStorage.setItem("nsl_cid", cid); } catch (e) {}
      window.open("next-stop.html?cid=" + encodeURIComponent(cid), "_blank");
    };
    refresh();
  } };
  async function refresh() {
    $("ns-err").textContent = "";
    try {
      const org = (typeof S !== "undefined" && S.org) || localStorage.getItem("nsl_org") || "";
      const r = await api("/api/controllers?organisation_id=" + encodeURIComponent(org));
      const ctrls = r.controllers || [];
      const saved = localStorage.getItem("nsl_cid") || "";
      $("ns-bus").innerHTML = ctrls.map((c) =>
        `<option value="${c.id}"${c.id === saved ? " selected" : ""}>${esc(c.name || c.id.slice(0, 8))} ${c.online ? "● online" : "○ offline"}</option>`).join("") ||
        `<option value="">No controllers paired</option>`;
      $("ns-msg").textContent = ctrls.length ? "Display shows last-known state when the bus is offline." : "";
    } catch (e) { $("ns-err").textContent = String(e.message || e); }
  }
})();
