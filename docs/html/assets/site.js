// Shared behaviour for the HTML docs: navigation, theme, tooltips, tabs, steppers,
// checklists and quizzes. Plain JavaScript, no libraries, works from file://.
(function () {
  "use strict";

  var PAGES = [
    { group: "Start" },
    { href: "index.html", title: "Start here" },
    { href: "run-flow.html", title: "One run, step by step" },
    { group: "How the parts connect" },
    { href: "python-rust.html", title: "Python ↔ MOCHI (Rust)" },
    { href: "ui.html", title: "UI ↔ logic: backup_ui.py" },
    { href: "data.html", title: "Data on disk & SQLite" },
    { group: "File by file" },
    { href: "logs-backup.html", title: "logs_backup.py" },
    { href: "tests-tools.html", title: "Tests, build & tools" },
    { group: "Review" },
    { href: "review.html", title: "Reviewer checklist" }
  ];

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
    } catch (e) { return null; }
  }

  function el(tag, attrs, html) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (html != null) e.innerHTML = html;
    return e;
  }

  // ---- theme ----
  var saved = store("docs-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);

  document.addEventListener("DOMContentLoaded", function () {
    buildNav();
    buildToc();
    themeButton();
    terms();
    tabs();
    steppers();
    checklists();
    quizzes();
    expanders();
  });

  function buildNav() {
    var side = document.querySelector(".side nav");
    if (!side) return;
    var here = location.pathname.split("/").pop() || "index.html";
    var html = "";
    var i;
    for (i = 0; i < PAGES.length; i++) {
      var p = PAGES[i];
      if (p.group) { html += '<div class="group">' + p.group + "</div>"; continue; }
      html += '<a href="' + p.href + '"' + (p.href === here ? ' aria-current="page"' : "") + ">" + p.title + "</a>";
    }
    side.innerHTML = html;

    // previous / next at the bottom of the page
    var pages = PAGES.filter(function (p) { return p.href; });
    var at = pages.findIndex(function (p) { return p.href === here; });
    var main = document.querySelector(".main");
    if (main && at >= 0) {
      var pager = el("div", { "class": "pager" });
      if (at > 0) pager.appendChild(el("a", { href: pages[at - 1].href }, "<small>← Previous</small>" + pages[at - 1].title));
      else pager.appendChild(el("span"));
      if (at < pages.length - 1) pager.appendChild(el("a", { href: pages[at + 1].href, style: "text-align:right" }, "<small>Next →</small>" + pages[at + 1].title));
      main.appendChild(pager);
    }

    var menu = document.querySelector(".menu-btn");
    if (menu) menu.addEventListener("click", function () { document.body.classList.toggle("nav-open"); });
    document.addEventListener("click", function (e) {
      if (document.body.classList.contains("nav-open") && !e.target.closest(".side") && !e.target.closest(".menu-btn"))
        document.body.classList.remove("nav-open");
    });
  }

  function slug(text) {
    return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  function buildToc() {
    var side = document.querySelector(".side");
    var heads = document.querySelectorAll(".main h2");
    if (!side || heads.length < 2) return;
    var toc = el("div", { "class": "toc" }, '<div class="group" style="margin:0 10px 4px;font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-3)">On this page</div>');
    heads.forEach(function (h) {
      if (!h.id) h.id = slug(h.textContent);
      toc.appendChild(el("a", { href: "#" + h.id }, h.textContent));
    });
    side.appendChild(toc);
  }

  function themeButton() {
    var b = document.querySelector(".theme-btn");
    if (!b) return;
    function label() {
      var t = document.documentElement.getAttribute("data-theme");
      var dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      b.textContent = dark ? "☀ Light" : "☾ Dark";
      return dark;
    }
    label();
    b.addEventListener("click", function () {
      var next = label() ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      store("docs-theme", next);
      label();
    });
  }

  // ---- terms: <span class="term" data-def="...">word</span> ----
  function terms() {
    var tip = null;
    function show(t) {
      hide();
      tip = el("div", { "class": "tip", role: "tooltip" }, t.getAttribute("data-def"));
      document.body.appendChild(tip);
      var r = t.getBoundingClientRect();
      var left = Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - tip.offsetWidth - 12);
      tip.style.left = Math.max(8, left) + "px";
      tip.style.top = (window.scrollY + r.bottom + 6) + "px";
    }
    function hide() { if (tip) { tip.remove(); tip = null; } }
    document.querySelectorAll(".term").forEach(function (t) {
      t.setAttribute("tabindex", "0");
      t.addEventListener("mouseenter", function () { show(t); });
      t.addEventListener("mouseleave", hide);
      t.addEventListener("focus", function () { show(t); });
      t.addEventListener("blur", hide);
      t.addEventListener("click", function (e) { e.stopPropagation(); show(t); });
    });
    document.addEventListener("click", hide);
    window.addEventListener("scroll", hide, { passive: true });
  }

  // ---- tabs: .tabs > [role=tablist] > button[role=tab][aria-controls] ----
  function tabs() {
    document.querySelectorAll(".tabs").forEach(function (box) {
      var list = box.querySelectorAll('[role="tab"]');
      function pick(tab) {
        list.forEach(function (t) {
          var on = t === tab;
          t.setAttribute("aria-selected", on);
          t.tabIndex = on ? 0 : -1;
          document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
        });
      }
      list.forEach(function (t, i) {
        t.addEventListener("click", function () { pick(t); });
        t.addEventListener("keydown", function (e) {
          var d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
          if (d) { var n = list[(i + d + list.length) % list.length]; pick(n); n.focus(); }
        });
      });
      pick(list[0]);
    });
  }

  // ---- steppers ----
  // <div class="stepper" data-src="ID" data-lanes="Source,Staging,Archive,SQLite"></div>
  // <script type="application/json" id="ID">[{"title","who","html","lanes":{"Source":["a"]},"hot":["Source"]}]</script>
  function steppers() {
    document.querySelectorAll(".stepper[data-src]").forEach(function (box) {
      var steps = JSON.parse(document.getElementById(box.getAttribute("data-src")).textContent);
      var laneNames = (box.getAttribute("data-lanes") || "").split(",").filter(Boolean);
      var at = 0;
      box.innerHTML =
        '<div class="controls"><button class="prev">← Back</button><button class="next primary">Next →</button>' +
        '<button class="restart">Restart</button><span class="count"></span></div>' +
        '<div class="dots"></div><div class="title"></div><div class="who"></div><div class="text"></div>' +
        (laneNames.length ? '<div class="lanes" style="--n:' + laneNames.length + '"></div>' : "");
      var dots = box.querySelector(".dots");
      steps.forEach(function (s, i) {
        var d = el("button", { title: s.title, "aria-label": "Step " + (i + 1) + ": " + s.title }, String(i + 1));
        d.addEventListener("click", function () { go(i); });
        dots.appendChild(d);
      });
      function go(i) {
        at = Math.max(0, Math.min(steps.length - 1, i));
        var s = steps[at];
        box.querySelector(".title").textContent = s.title;
        box.querySelector(".who").innerHTML = s.who || "";
        box.querySelector(".text").innerHTML = s.html || "";
        box.querySelector(".count").textContent = "Step " + (at + 1) + " of " + steps.length;
        box.querySelector(".prev").disabled = at === 0;
        box.querySelector(".next").disabled = at === steps.length - 1;
        Array.prototype.forEach.call(dots.children, function (d, j) {
          if (j === at) d.setAttribute("aria-current", "step"); else d.removeAttribute("aria-current");
        });
        var lanes = box.querySelector(".lanes");
        if (lanes) {
          lanes.innerHTML = "";
          laneNames.forEach(function (name) {
            var items = (s.lanes && s.lanes[name]) || [];
            var hot = (s.hot || []).indexOf(name) >= 0;
            var lane = el("div", { "class": "lane" + (hot ? " hot" : "") }, "<h4>" + name + "</h4>");
            items.forEach(function (it) {
              var cls = "chip";
              if (it.charAt(0) === "+") { cls += " new"; it = it.slice(1); }
              else if (it.charAt(0) === "-") { cls += " gone"; it = it.slice(1); }
              lane.appendChild(el("span", { "class": cls }, it));
            });
            if (!items.length) lane.appendChild(el("span", { "class": "muted small" }, "(empty)"));
            lanes.appendChild(lane);
          });
        }
      }
      box.querySelector(".prev").addEventListener("click", function () { go(at - 1); });
      box.querySelector(".next").addEventListener("click", function () { go(at + 1); });
      box.querySelector(".restart").addEventListener("click", function () { go(0); });
      box.setAttribute("tabindex", "0");
      box.addEventListener("keydown", function (e) {
        if (e.target.tagName === "INPUT") return;
        if (e.key === "ArrowRight") go(at + 1);
        if (e.key === "ArrowLeft") go(at - 1);
      });
      go(0);
    });
  }

  // ---- checklists: <ul class="checklist" data-key="..."><li><input type=checkbox> text</li> ----
  function checklists() {
    document.querySelectorAll(".checklist[data-key]").forEach(function (list) {
      var key = "docs-check-" + list.getAttribute("data-key");
      var state = {};
      try { state = JSON.parse(store(key) || "{}"); } catch (e) { state = {}; }
      var boxes = list.querySelectorAll("input[type=checkbox]");
      var bar = document.querySelector('.progress-bar[data-for="' + list.getAttribute("data-key") + '"] > span');
      var label = document.querySelector('.progress-label[data-for="' + list.getAttribute("data-key") + '"]');
      function update() {
        var n = 0;
        boxes.forEach(function (b, i) {
          b.closest("li").classList.toggle("done", b.checked);
          if (b.checked) n++;
          state[i] = b.checked;
        });
        if (bar) bar.style.width = (100 * n / boxes.length) + "%";
        if (label) label.textContent = n + " of " + boxes.length + " checked";
        store(key, JSON.stringify(state));
      }
      boxes.forEach(function (b, i) {
        b.checked = !!state[i];
        b.addEventListener("change", update);
      });
      update();
      var reset = document.querySelector('.reset-check[data-for="' + list.getAttribute("data-key") + '"]');
      if (reset) reset.addEventListener("click", function () { boxes.forEach(function (b) { b.checked = false; }); update(); });
    });
  }

  // ---- quizzes: <div class="quiz"><p class="q">..</p><div class="opts"><button data-right>..</button>..</div><div class="why">..</div></div>
  function quizzes() {
    document.querySelectorAll(".quiz").forEach(function (q) {
      q.querySelectorAll(".opts button").forEach(function (b) {
        b.addEventListener("click", function () {
          q.classList.add("answered");
          q.querySelectorAll(".opts button").forEach(function (o) {
            if (o.hasAttribute("data-right")) o.classList.add("right");
          });
          if (!b.hasAttribute("data-right")) b.classList.add("wrong");
        });
      });
    });
  }

  // ---- "Open all / Close all" for a group of <details> ----
  function expanders() {
    document.querySelectorAll("[data-expand]").forEach(function (b) {
      b.addEventListener("click", function () {
        var open = b.getAttribute("data-expand") === "open";
        document.querySelectorAll(b.getAttribute("data-target") + " details").forEach(function (d) { d.open = open; });
      });
    });
  }
})();
