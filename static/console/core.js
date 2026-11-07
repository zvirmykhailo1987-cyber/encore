// Shared helpers. Rule for every page: API data enters the DOM only as text nodes or attribute values set
// through these functions. Nothing here or in the page scripts assigns HTML from a string.
window.Encore = (function () {
  "use strict";
  const API = "/api/v1";
  const DASH = "—";
  const TIMEOUT_MS = 8000;

  function $(id) { return document.getElementById(id); }

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    applyAttrs(el, attrs);
    // Every button built here gets its listener from the caller. The marker tells the audit gate so.
    if (tag === "button") el.setAttribute("data-has-js", "");
    kids.flat().forEach((k) => { if (k !== null && k !== undefined) el.append(k); });
    return el;
  }
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    applyAttrs(el, attrs);
    kids.flat().forEach((k) => { if (k !== null && k !== undefined) el.append(k); });
    return el;
  }
  function applyAttrs(el, attrs) {
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") el.setAttribute("class", v);
      else el.setAttribute(k, v === true ? "" : String(v));
    });
  }
  function icon(id, cls) {
    return s("svg", { class: cls || "ico", "aria-hidden": "true" }, s("use", { href: "#" + id }));
  }
  function setText(target, value) {
    const el = typeof target === "string" ? $(target) : target;
    const text = value === null || value === undefined || value === "" ? DASH : String(value);
    if (el && el.textContent !== text) el.textContent = text;
  }
  // Writing an unchanged attribute still counts as a DOM change, so these skip it. Refreshes stay silent.
  function setAttr(target, name, value) {
    const el = typeof target === "string" ? $(target) : target;
    if (el.getAttribute(name) !== value) el.setAttribute(name, value);
  }
  function setHidden(target, hidden) {
    const el = typeof target === "string" ? $(target) : target;
    if (el.hidden !== Boolean(hidden)) el.hidden = Boolean(hidden);
  }
  // Replace children only when what they show has changed. This is what keeps a 2 s refresh from flickering.
  function setKids(el, signature, build) {
    const sig = JSON.stringify(signature);
    if (el.dataset.sig === sig) return false;
    el.dataset.sig = sig;
    el.replaceChildren(...[build()].flat().filter(Boolean));
    return true;
  }

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const num = (v) => (isNum(v) ? v.toLocaleString("en-US") : DASH);
  // Long numbers step down in size so 10,000,000+ still fits the node it belongs to.
  function setNum(target, value) {
    const el = typeof target === "string" ? $(target) : target;
    const text = num(value);
    setText(el, text);
    const len = text.length;
    el.classList.toggle("fit1", len > 7 && len <= 10);
    el.classList.toggle("fit2", len > 10 && len <= 13);
    el.classList.toggle("fit3", len > 13);
  }
  function bytes(v) {
    if (!isNum(v)) return DASH;
    if (v < 1000) return v + " B";
    if (v < 1e6) return (v / 1e3).toFixed(1) + " KB";
    if (v < 1e9) return (v / 1e6).toFixed(1) + " MB";
    return (v / 1e9).toFixed(2) + " GB";
  }
  function duration(sec) {
    if (!isNum(sec)) return DASH;
    if (sec < 90) return Math.round(sec) + " s";
    if (sec < 5400) return Math.round(sec / 60) + " min";
    return (sec / 3600).toFixed(1) + " h";
  }
  function clock(iso) {
    if (!iso) return DASH;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return DASH;
    const same = d.toDateString() === new Date().toDateString();
    const t = d.toLocaleTimeString("en-GB", { hour12: false });
    return same ? t : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) + " " + t;
  }
  // A nice round upper bound for an axis. Never zero, so a chart of zeroes cannot divide by it.
  function niceMax(v) {
    if (!isNum(v) || v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }
  function token() {
    try { return sessionStorage.getItem("encore.token") || ""; } catch (e) { return ""; }
  }
  async function api(path, options) {
    const opts = options || {};
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs || TIMEOUT_MS);
    const headers = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (opts.method && opts.method !== "GET" && token()) headers.Authorization = "Bearer " + token();
    let response;
    try {
      response = await fetch(API + path, {
        method: opts.method || "GET",
        headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: ctrl.signal,
        cache: "no-store",
      });
    } catch (e) {
      throw new ApiError(0, e.name === "AbortError" ? "The server did not answer in time." : "The server cannot be reached.");
    } finally {
      clearTimeout(timer);
    }
    let body = null;
    try { body = await response.json(); } catch (e) { /* An error page is not JSON. The status still says enough. */ }
    if (!response.ok) {
      if ((response.status === 401 || response.status === 403) && opts.method && opts.method !== "GET") askToken();
      const text = body && (body.error || body.detail);
      throw new ApiError(response.status, text ? String(text) : "The server answered with status " + response.status + ".");
    }
    return opts.withTotal ? { body, total: Number(response.headers.get("X-Total-Count")) } : body;
  }

  /* connection banner and refresh loop */
  let lastOk = null;
  function connection(ok, error) {
    const bar = $("conn");
    if (ok) {
      bar.hidden = true;
      lastOk = new Date();
      setText("updated", "Updated " + lastOk.toLocaleTimeString("en-GB", { hour12: false }));
    } else {
      const since = lastOk ? " Showing data from " + lastOk.toLocaleTimeString("en-GB", { hour12: false }) + "." : "";
      const lost = !error || !error.status;
      setText(bar, (lost ? "Connection lost." : "The server answered with an error (" + error.status + ").") + since + " Retrying every 2 seconds.");
      bar.hidden = false;
    }
  }
  function poll(fn, intervalMs) {
    const every = intervalMs || 2000;
    let timer = null, running = false, stopped = false, fails = 0;
    async function tick() {
      clearTimeout(timer);
      if (stopped || running || document.hidden) return;
      running = true;
      try {
        await fn();
        fails = 0;
        connection(true);
      } catch (e) {
        fails += 1;
        // One slow answer is not an outage. Two in a row are worth telling the operator about.
        if (fails >= 2 || !lastOk) connection(false, e);
      }
      running = false;
      if (!stopped && !document.hidden) timer = setTimeout(tick, every);
    }
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) clearTimeout(timer);
      else tick();
    });
    tick();
    return { stop() { stopped = true; clearTimeout(timer); }, now: tick };
  }

  let toastTimer;
  function toast(msg, ms) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("on"), ms || 3200);
  }
  function confirmAction(title, body, label) {
    return new Promise((resolve) => {
      const d = $("confirm");
      setText("confirm-title", title);
      setText("confirm-body", body);
      setText("confirm-yes", label);
      d.returnValue = "no";
      d.addEventListener("close", () => resolve(d.returnValue === "yes"), { once: true });
      d.showModal();
    });
  }
  function showDetail(title, text) {
    setText("detail-title", title);
    $("detail-body").textContent = text;
    $("detail").showModal();
  }

  const STATUS = {
    pending: ["i-den", "Pending"],
    running: [null, "Running"],
    interrupted: ["i-uvaha", "Interrupted"],
    paused: ["i-den", "Paused"],
    failed: ["i-vidmova", "Failed"],
    completed: ["i-hotovo", "Completed"],
    rolled_back: ["i-skasuvaty", "Rolled back"],
  };
  function statusChip(status) {
    const [ic, label] = STATUS[status] || ["i-uvaha", String(status)];
    const bad = status === "failed" || status === "interrupted";
    return h("span", { class: "chip" + (bad ? " bad" : "") + (status === "rolled_back" ? " dash" : "") },
      ic ? icon(ic) : h("i", { class: "live-dot" }), label);
  }
  const dryChip = () => h("span", { class: "chip hatch" }, "Dry run");

  function fileName(path) { return String(path || "").split(/[\\/]/).pop(); }

  function askToken() {
    const d = $("token");
    if (d && !d.open) d.showModal();
  }
  function initChrome() {
    $("theme-toggle").addEventListener("click", () => {
      const dark = getComputedStyle(document.documentElement).getPropertyValue("--is-dark").trim() === "1";
      const next = dark ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("encore.theme", next); } catch (e) { /* Only this page load keeps the choice. */ }
      window.dispatchEvent(new Event("encore-theme"));
    });
    const open = $("token-open");
    if (open) {
      const label = () => setText("token-state", token() ? "Token set" : "Operator token");
      label();
      open.addEventListener("click", askToken);
      // Cancel is not a submit button, so Enter in the field means "Use token".
      $("token-cancel").addEventListener("click", () => $("token").close("no"));
      $("token").addEventListener("close", () => {
        if ($("token").returnValue === "yes") {
          try { sessionStorage.setItem("encore.token", $("token-input").value.trim()); } catch (e) { toast("This browser blocks tab storage, so the token cannot be kept."); }
        }
        $("token-input").value = "";
        label();
      });
    }
  }
  document.addEventListener("DOMContentLoaded", initChrome);

  // The fixed-size schematic scales as a whole down to 1200 px wide. Below that it becomes a two-column grid, then one column.
  function fitBoard(board) {
    function fit() {
      const field = board.closest(".field");
      if (innerWidth >= 1200 && !board.hidden) {
        const scale = Math.min((innerWidth - 56) / 1384, 1.25);
        board.style.transform = "scale(" + scale + ")";
        // The field is a flex item, so its size is set through the basis: the scaled board, or more on a taller screen.
        field.style.flex = "1 0 " + (790 * scale + 20) + "px";
        // Past the scale cap the board no longer fills the width, so the spare width goes to both sides.
        const spare = field.clientWidth - 56 - 1384 * scale;
        board.style.marginLeft = spare > 1 ? spare / 2 + "px" : "";
      } else {
        board.style.transform = "";
        board.style.marginLeft = "";
        field.style.flex = "";
      }
    }
    addEventListener("resize", fit);
    fit();
  }
  function fatal(title, text, retry) {
    $("board").hidden = true;
    $("board").closest(".field").style.flex = "";
    $("board-state").hidden = false;
    const kids = [h("b", null, title), h("p", null, text)];
    if (retry) {
      const b = h("button", { class: "btn", type: "button" }, "Try again");
      b.addEventListener("click", retry);
      kids.push(b);
    }
    kids.push(h("p", null, h("a", { class: "lnk", href: "/" }, "Back to imports")));
    $("state-body").replaceChildren(icon("i-uvaha", "ico lg"), h("div", null, kids));
  }
  function clearFatal(board) {
    if (!$("board-state").hidden) {
      $("board-state").hidden = true;
      $("board").hidden = false;
      fitBoard(board);
    }
  }

  return { $, h, s, icon, setText, setAttr, setHidden, setKids, setNum, num, isNum, bytes, duration, clock, niceMax, api, ApiError, poll, connection,
    toast, confirmAction, showDetail, statusChip, dryChip, fileName, fitBoard, fatal, clearFatal, DASH };
})();
