/* NSL Displays site behaviour: live LED board demo, reveals, footer year, quote form. */
(function () {
  "use strict";
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* footer year */
  document.querySelectorAll("[data-year]").forEach(function (el) { el.textContent = new Date().getFullYear(); });

  /* gentle reveal on scroll */
  if (!reduceMotion && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.style.opacity = "1"; e.target.style.transform = "none"; io.unobserve(e.target); }
      });
    }, { threshold: 0.12 });
    document.querySelectorAll(".card, .step, .spec-row, .cta-band").forEach(function (el) {
      el.style.opacity = "0";
      el.style.transform = "translateY(14px)";
      el.style.transition = "opacity .5s ease, transform .5s ease";
      io.observe(el);
    });
  }

  /* live LED board demo — cycles board pages like the real signs (page_seconds: 3) */
  function ledBoard(canvas, pages) {
    if (!canvas) return;
    var W = 240, H = 40;
    canvas.width = W; canvas.height = H;
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    /* unlit dot grid (faint) + punch mask (opaque dots, transparent gaps) */
    function dotCanvas(color) {
      var c = document.createElement("canvas");
      c.width = W; c.height = H;
      var g = c.getContext("2d");
      g.fillStyle = color;
      for (var y = 2; y < H; y += 3) for (var x = 2; x < W; x += 3) {
        g.beginPath(); g.arc(x, y, 1, 0, 6.2832); g.fill();
      }
      return c;
    }
    var grid = dotCanvas("rgba(255,176,0,0.10)");
    var mask = dotCanvas("rgba(255,255,255,1)");
    var AMBER = "#ffb000";
    function draw(page) {
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, W, H);
      ctx.drawImage(grid, 0, 0);
      ctx.fillStyle = AMBER;
      ctx.textBaseline = "middle";
      /* route number, left block */
      ctx.font = "800 30px 'Arial Narrow', Helvetica, Arial, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(page.num, 35, H / 2 + 1);
      /* dest top-right, via bottom-right */
      ctx.textAlign = "center";
      ctx.font = "800 17px 'Arial Narrow', Helvetica, Arial, sans-serif";
      ctx.fillText(page.dest, 155, 12);
      ctx.font = "700 12px 'Arial Narrow', Helvetica, Arial, sans-serif";
      ctx.fillText(page.via, 155, 30);
      /* punch everything to the dot grid: lit text becomes amber dots */
      ctx.globalCompositeOperation = "destination-in";
      ctx.drawImage(mask, 0, 0);
      ctx.globalCompositeOperation = "source-over";
    }
    var i = 0;
    draw(pages[0]);
    if (!reduceMotion && pages.length > 1) {
      setInterval(function () { i = (i + 1) % pages.length; draw(pages[i]); }, 3000);
    }
    /* caption page indicator */
    var cap = canvas.closest(".board-shell");
    cap = cap && cap.querySelector("[data-board-page]");
    if (cap && pages.length > 1 && !reduceMotion) {
      setInterval(function () { cap.textContent = "P" + ((i % pages.length) + 1) + " / P" + pages.length; }, 3000);
      cap.textContent = "P1 / P" + pages.length;
    }
  }

  document.querySelectorAll("canvas[data-board]").forEach(function (c) {
    var pages;
    try { pages = JSON.parse(c.getAttribute("data-board")); }
    catch (e) { pages = [{ num: "8", dest: "BURTON", via: "Branston" }]; }
    ledBoard(c, pages);
  });

  /* quote form -> mailto compose (no backend on the static site) */
  document.querySelectorAll("form[data-quote]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var to = form.getAttribute("data-quote") || "hello@nsldisplays.com";
      var get = function (n) { var f = form.elements[n]; return f ? String(f.value || "").trim() : ""; };
      var subject = "Quote request — " + (get("fleet") || get("name") || "bus displays");
      var lines = [
        "Name: " + get("name"),
        "Organisation: " + get("org"),
        "Email: " + get("email"),
        "Phone: " + get("phone"),
        "Fleet size: " + get("fleet"),
        "Boards needed: " + get("boards"),
        "Details:",
        get("details")
      ];
      window.location.href = "mailto:" + encodeURIComponent(to) +
        "?subject=" + encodeURIComponent(subject) +
        "&body=" + encodeURIComponent(lines.join("\n"));
    });
  });
})();
