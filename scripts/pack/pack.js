// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Mykhailo Zvir

(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const pages = [...document.querySelectorAll("section.page")];
  const order = pages.map((p) => p.id);

  /* routing: one page at a time, and a link can name a place inside it */
  function show(hash) {
    const target = (hash || "").replace(/^#/, "");
    let page = pages.find((p) => p.id === target);
    let inner = null;
    if (!page && target) {
      inner = document.getElementById(target);
      page = inner ? inner.closest("section.page") : null;
    }
    page = page || pages[0];
    pages.forEach((p) => p.classList.toggle("on", p === page));
    document.querySelectorAll(".nav a").forEach((a) => {
      if (a.getAttribute("href") === "#" + page.id) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    document.title = page.dataset.title + " · Encore";
    if (inner) { inner.scrollIntoView(); if (inner.tagName === "DETAILS") inner.open = true; } else window.scrollTo(0, 0);
    if (page.id === "program") startProgram();
    if (page.id === "code") startCode();
    if (page.id === "questions") startQuestions();
  }
  window.addEventListener("hashchange", () => show(location.hash));

  /* theme */
  function setTheme(next) {
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("encore.pack.theme", next); } catch (e) { /* Storage can be blocked. */ }
    renderStep();
  }
  $("theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme === "dark" ||
      (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    setTheme(dark ? "light" : "dark");
  });
  try {
    const saved = localStorage.getItem("encore.pack.theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (e) { /* Storage can be blocked. */ }

  /* copy buttons on commands */
  document.querySelectorAll("pre.cmd").forEach((pre) => {
    const b = document.createElement("button");
    b.className = "copy";
    b.type = "button";
    b.textContent = "Copy";
    b.addEventListener("click", async () => {
      const text = pre.querySelector("code").textContent;
      try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; }
      catch (e) { b.textContent = "Select it"; }
      setTimeout(() => { b.textContent = "Copy"; }, 1400);
    });
    pre.append(b);
  });

  /* files carried inside this page */
  function bytesOf(id) {
    const text = $(id).textContent.trim();
    const raw = atob(text);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function saveFile(id, name, type) {
    const url = URL.createObjectURL(new Blob([bytesOf(id)], { type }));
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 20000);
  }
  document.querySelectorAll("[data-save]").forEach((b) => {
    b.addEventListener("click", () => saveFile(b.dataset.save, b.dataset.name, b.dataset.type));
  });

  /* program: the real console, replaying replies recorded from a real run */
  let replay = null, stepIndex = 0, videoReady = false;
  function startProgram() {
    if (!replay) {
      replay = JSON.parse($("replay-data").textContent);
      const bar = $("steps");
      replay.steps.forEach((step, i) => {
        const b = document.createElement("button");
        b.type = "button";
        const n = document.createElement("span");
        n.className = "m";
        n.textContent = String(i + 1);
        b.append(n, step.title);
        b.addEventListener("click", () => go(i));
        bar.append(b);
      });
      go(0);
      $("step-next").addEventListener("click", () => go(Math.min(stepIndex + 1, replay.steps.length - 1)));
    }
    if (!videoReady && $("video-data")) {
      videoReady = true;
      const url = URL.createObjectURL(new Blob([bytesOf("video-data")], { type: "video/webm" }));
      $("demo-video").src = url;
    }
  }
  function currentTheme() {
    return document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  function go(i) { stepIndex = i; renderStep(); }
  function renderStep() {
    if (!replay) return;
    const step = replay.steps[stepIndex];
    [...$("steps").children].forEach((b, n) => {
      if (n === stepIndex) { b.setAttribute("aria-current", "step"); b.scrollIntoView({ block: "nearest", inline: "nearest" }); }
      else b.removeAttribute("aria-current");
    });
    $("step-note").textContent = step.note;
    $("step-next").hidden = stepIndex === replay.steps.length - 1;
    const page = replay.pages[step.page];
    const boot = "window.__REPLAY=" + JSON.stringify({ frames: step.frames, fallback: replay.fallback, runId: replay.runId }).replace(/</g, "\\u003c") + ";";
    $("app").srcdoc = page.replace("<!--REPLAY-BOOT-->", "<script>" + boot + "<\/script>").replace('data-theme="auto"', 'data-theme="' + currentTheme() + '"');
  }
  window.addEventListener("message", (e) => {
    if (!replay || e.source !== $("app").contentWindow || !e.data) return;
    if (e.data.go) {
      const found = replay.steps.findIndex((s, n) => s.path === e.data.go && n >= stepIndex);
      const any = found >= 0 ? found : replay.steps.findIndex((s) => s.path === e.data.go);
      if (any >= 0) go(any);
    }
    if (e.data.wrote) go(Math.min(stepIndex + 1, replay.steps.length - 1));
  });

  /* code browser */
  let code = null, currentFile = null;
  function startCode() {
    if (code) return;
    code = JSON.parse($("code-data").textContent);
    const tree = $("tree");
    let lastDir = null;
    const inFolder = (path) => (path.includes("/") ? 1 : 0);
    Object.keys(code).sort((a, b) => inFolder(a) - inFolder(b) || a.localeCompare(b)).forEach((path) => {
      const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "project root";
      if (dir !== lastDir) {
        const d = document.createElement("div");
        d.className = "d";
        d.textContent = dir;
        tree.append(d);
        lastDir = dir;
      }
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.path = path;
      b.textContent = path.slice(path.lastIndexOf("/") + 1);
      b.addEventListener("click", () => openFile(path));
      tree.append(b);
    });
    openFile(pendingFile || "imports/pipeline.py", pendingFind);
    $("code-search").addEventListener("input", searchCode);
  }
  let pendingFile = null, pendingFind = null;
  function openFile(path, find) {
    if (!code[path]) return;
    currentFile = path;
    const item = code[path];
    $("v-path").textContent = path;
    $("v-info").textContent = item.lines + " lines" + (item.about ? ". " + item.about : "");
    const body = $("v-body");
    const pre = document.createElement("pre");
    pre.className = "src";
    pre.innerHTML = item.html; // Escaped when this page was built, from the project's own files.
    body.replaceChildren(pre);
    [...$("tree").querySelectorAll("button")].forEach((b) => b.setAttribute("aria-current", b.dataset.path === path ? "true" : "false"));
    body.scrollTop = 0;
    if (find) {
      const line = [...pre.children].find((l) => l.textContent.includes(find));
      if (line) { line.classList.add("hit"); line.scrollIntoView({ block: "center" }); }
    }
  }
  function searchCode() {
    const term = $("code-search").value.trim().toLowerCase();
    const box = $("code-results");
    box.replaceChildren();
    box.hidden = term.length < 3;
    if (box.hidden) return;
    let shown = 0, total = 0;
    Object.keys(code).forEach((path) => {
      code[path].text.split("\n").forEach((line, n) => {
        if (!line.toLowerCase().includes(term)) return;
        total++;
        if (shown >= 80) return;
        shown++;
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = path + ":" + (n + 1) + "  " + line.trim().slice(0, 110);
        b.addEventListener("click", () => openFile(path, line));
        box.append(b);
      });
    });
    if (!total) { const d = document.createElement("div"); d.className = "empty"; d.textContent = "Nothing found."; box.append(d); }
  }
  document.addEventListener("click", (e) => {
    const link = e.target.closest("[data-file]");
    if (!link) return;
    e.preventDefault();
    pendingFile = link.dataset.file;
    pendingFind = link.dataset.find || null;
    location.hash = "#code";
    setTimeout(() => { startCode(); openFile(pendingFile, pendingFind); }, 30);
  });

  /* questions */
  let qReady = false, qGroup = "All";
  function filterQuestions() {
    const term = $("q-search").value.trim().toLowerCase();
    let shown = 0;
    document.querySelectorAll("details.q").forEach((d) => {
      const hit = (qGroup === "All" || d.dataset.group === qGroup) && (!term || d.textContent.toLowerCase().includes(term));
      d.hidden = !hit;
      if (hit) shown++;
    });
    $("q-count").textContent = shown + " of " + document.querySelectorAll("details.q").length;
    $("q-empty").hidden = shown > 0;
  }
  function startQuestions() {
    if (qReady) return;
    qReady = true;
    $("q-search").addEventListener("input", filterQuestions);
    document.querySelectorAll("#q-filters button").forEach((b) => b.addEventListener("click", () => {
      qGroup = b.dataset.group;
      document.querySelectorAll("#q-filters button").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
      filterQuestions();
    }));
    filterQuestions();
  }

  /* arithmetic on measured numbers */
  function hours(seconds) { return seconds < 3600 ? (seconds / 60).toFixed(0) + " minutes" : (seconds / 3600).toFixed(1) + " hours"; }
  function calc() {
    const rows = Number($("c-rows").value), speed = Number($("c-speed").value);
    $("c-window").textContent = rows > 0 && speed > 0 ? hours(rows / speed) : "—";
    const fans = Number($("c-fans").value), every = Number($("c-every").value);
    const rate = fans > 0 && every > 0 ? fans / every : null;
    $("c-rate").textContent = rate ? Math.round(rate).toLocaleString("en-US") + " requests per second" : "—";
    const top = Number($("c-rate").dataset.top);
    $("c-rate-note").textContent = !rate ? "" : rate <= top ? "Within what was measured here." : "Above the highest rate measured here, so this page says nothing about it.";
  }
  ["c-rows", "c-speed", "c-fans", "c-every"].forEach((id) => { if ($(id)) $(id).addEventListener("input", calc); });
  if ($("c-rows")) calc();

  void order;
  show(location.hash);
})();
