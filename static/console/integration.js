// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Mykhailo Zvir

(function () {
  "use strict";
  const { setAttr, setHidden, $, h, s, setText, setKids, setNum, num, isNum, clock, niceMax, api, poll, toast, fitBoard, fatal, clearFatal, DASH } = Encore;
  const BUCKETS = ["25", "50", "100", "250", "500", "1000", "2500", "5000", "inf"];
  const FIELDS = [["p_429", "Rate limit, 429"], ["p_500", "Server error, 500"], ["p_timeout", "Timeout"], ["p_duplicate", "Duplicate item"], ["p_shuffle", "Reversed page"], ["p_malformed", "Malformed item"]];
  const OFF = { p_429: 0, p_500: 0, p_timeout: 0, timeout_seconds: 10, p_duplicate: 0, p_shuffle: 0, p_malformed: 0, retry_after: 0.05, outage: false };
  let poller, loaded = false, tickNo = 0, last = null, faults = null, dragging = false, sendTimer = null;
  const sum = (rows, key) => rows.reduce((t, r) => t + (r[key] || 0), 0);
  const axisShort = (v) => (v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1000 ? (v / 1000).toFixed(v >= 1e4 ? 0 : 1) + "k" : String(v)).replace(".0", "");

  // The endpoint returns the last 30 stored minutes. Only those really inside the last 30 minutes count here.
  function windowed(metrics) {
    const now = Date.now();
    return (metrics || []).map((m) => ({ ...m, age: Math.floor((now - new Date(m.minute).getTime()) / 60000) })).filter((m) => m.age >= 0 && m.age < 30);
  }
  function upperBound(counts, q) {
    const total = BUCKETS.reduce((t, b) => t + (counts[b] || 0), 0);
    if (!total) return null;
    const target = total * q;
    let seen = 0;
    for (const b of BUCKETS) {
      seen += counts[b] || 0;
      if (seen >= target) return b === "inf" ? "> 5,000 ms" : "≤ " + num(Number(b)) + " ms";
    }
    return null;
  }
  function drawSwitch(state, traffic) {
    const up = state !== "open";
    const ang = { closed: 0, open: -27, half_open: -11 }[state] ?? 0;
    const px = 70, py = 36, len = 92, rad = ang * Math.PI / 180;
    setKids($("sym"), [state], () => [
      s("path", { d: "M-10 36H70", stroke: "var(--accent)", "stroke-width": 2, fill: "none" }),
      s("path", { d: "M162 36H242", stroke: up ? "var(--accent)" : "var(--wire)", "stroke-width": 2, fill: "none", "stroke-dasharray": up ? null : "2 5" }),
      s("line", { x1: px, y1: py, x2: (px + len * Math.cos(rad)).toFixed(1), y2: (py + len * Math.sin(rad)).toFixed(1), stroke: state === "open" ? "var(--ink)" : "var(--accent)", "stroke-width": 3.5, "stroke-linecap": "round", "stroke-dasharray": state === "half_open" ? "9 6" : null }),
      s("circle", { cx: 70, cy: 36, r: 5.5, fill: "var(--card)", stroke: "var(--ink)", "stroke-width": 2 }),
      s("circle", { cx: 162, cy: 36, r: 5.5, fill: "var(--card)", stroke: "var(--ink)", "stroke-width": 2 }),
    ]);
    setAttr("w1", "class", traffic ? "w live" : "w");
    setAttr("w2", "class", traffic ? "w live" : "w");
    setAttr("w3", "class", state === "open" ? "w dead" : traffic ? "w live" : "w");
  }
  function breakerText(c) {
    if (c.breaker === "open") {
      const wait = isNum(c.seconds_until_probe) && c.seconds_until_probe > 0 ? "Next probe in " + Math.ceil(c.seconds_until_probe) + " s." : "The next call goes out as a probe.";
      return "Calls are refused without being sent. " + wait;
    }
    if (c.breaker === "half_open") return "One probe decides: success closes it, failure reopens it.";
    if (c.consecutive_failures) return num(c.consecutive_failures) + " of " + num(c.failure_threshold) + " failures in a row. Calls still pass.";
    return "All calls pass through.";
  }
  function drawResults(rows, hasData) {
    const lines = [["2xx", "successes"], ["429", "rate_limited"], ["4xx", "client_errors"], ["5xx", "server_errors"], ["timeout", "timeouts"]];
    const total = sum(rows, "requests");
    setKids($("res"), [lines.map(([, k]) => sum(rows, k)), total, hasData], () => [
      ...lines.map(([label, k]) => {
        const v = sum(rows, k);
        return h("div", { class: "res" }, h("span", null, label),
          h("span", { class: "tr", "aria-hidden": "true" }, h("i", { style: "width:" + (total ? v / total * 100 : 0) + "%" })),
          h("span", { class: "vv" }, hasData ? num(v) : DASH));
      }),
      h("div", { class: "res tot" }, h("span", null, "requests"), h("span"), h("span", { class: "vv" }, hasData ? num(total) : DASH)),
    ]);
  }
  function drawTime(rows) {
    const svg = $("time"), box = svg.parentElement;
    setHidden("time-e", rows.length > 0);
    setAttr(svg, "visibility", rows.length ? "visible" : "hidden");
    const W = box.offsetWidth || 490, H = box.offsetHeight || 250;
    const slots = Array.from({ length: 30 }, () => null);
    rows.forEach((m) => { slots[29 - m.age] = m; });
    setKids(svg, [rows.map((m) => [m.age, m.requests, m.successes, m.rate_limited, m.client_errors, m.server_errors, m.timeouts]), W, H], () => {
      if (!rows.length) return [];
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      // The legend takes as many lines as the width needs. The plot gives up that height.
      const legend = [["success", "var(--f2)"], ["429 and 4xx", "var(--ink)"], ["5xx and timeouts", "var(--accent)"]];
      const L = 44, LINE = 18;
      let lx = L, ly = 0;
      const placed = legend.map(([n, c]) => {
        if (lx > L && lx + n.length * 7.3 + 15 > W) { lx = L; ly += LINE; }
        const at = { n, c, x: lx, y: ly };
        lx += n.length * 7 + 30;
        return at;
      });
      const B = 18, T = 24 + ly, ih = H - T - B, mx = niceMax(Math.max(...rows.map((m) => m.requests), 1)), sw = (W - L) / 30, out = [];
      [0, mx / 2, mx].forEach((v) => {
        const y = H - B - v / mx * ih;
        out.push(s("line", { x1: L, x2: W, y1: y, y2: y, stroke: "var(--line)" }));
        out.push(s("text", { x: L - 6, y: y + 4, "text-anchor": "end", class: "ax" }, axisShort(Math.round(v))));
      });
      slots.forEach((m, i) => {
        if (!m) return;
        let y = H - B;
        const X = L + i * sw + 1.5, bw = Math.max(sw - 3, 1);
        [[m.successes, "var(--f2)"], [m.rate_limited + m.client_errors, "var(--ink)"], [m.server_errors + m.timeouts, "var(--accent)"]].forEach(([v, c]) => {
          const a = v / mx * ih;
          if (a <= 0) return;
          y -= a;
          out.push(s("rect", { x: X.toFixed(1), y: y.toFixed(1), width: bw.toFixed(1), height: a.toFixed(1), fill: c }));
        });
      });
      [0, 10, 20, 30].forEach((m) => out.push(s("text", { x: L + m * sw, y: H - 3, "text-anchor": m === 30 ? "end" : m === 0 ? "start" : "middle", class: "ax" }, m === 30 ? "now" : "-" + (30 - m) + " min")));
      placed.forEach(({ n, c, x, y }) => {
        out.push(s("rect", { x, y: y + 4, width: 10, height: 10, rx: 2, fill: c }));
        out.push(s("text", { x: x + 15, y: y + 13, class: "ax leg" }, n));
      });
      return out;
    });
  }
  function drawHist(rows) {
    const svg = $("hist"), box = svg.parentElement;
    const counts = {};
    rows.forEach((m) => Object.entries(m.latency_buckets || {}).forEach(([b, c]) => { counts[b] = (counts[b] || 0) + c; }));
    const values = BUCKETS.map((b) => counts[b] || 0);
    const any = values.some((v) => v > 0);
    setHidden("hist-e", any);
    setAttr(svg, "visibility", any ? "visible" : "hidden");
    const p = [upperBound(counts, .5), upperBound(counts, .95), upperBound(counts, .99)];
    setText("pp", any ? "p50 " + p[0] + ", p95 " + p[1] + ", p99 " + p[2] : " ");
    const W = box.offsetWidth || 480, H = box.offsetHeight || 250;
    setKids(svg, [values, W, H], () => {
      if (!any) return [];
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      const nar = W < 460, L = 50, B = 20, T = 20, ih = H - T - B, mh = niceMax(Math.max(...values)), bw = (W - L) / 9, dot = 10, cols = nar ? 3 : 4, out = [];
      const short = (v) => (v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1000 ? (v / 1000).toFixed(v >= 1e4 ? 0 : 1) + "k" : String(v)).replace(".0", "");
      [0, mh / 2, mh].forEach((v) => {
        const y = H - B - v / mh * ih;
        out.push(s("line", { x1: L, x2: W, y1: y, y2: y, stroke: "var(--line)" }));
        out.push(s("text", { x: L - 6, y: y + 4, "text-anchor": "end", class: "ax" }, short(Math.round(v))));
      });
      const names = BUCKETS.map((b) => (b === "inf" ? ">5k" : (nar ? "" : "≤") + short(Number(b))));
      values.forEach((c, i) => {
        const hh = c / mh * ih, cx = L + i * bw + bw / 2, n = Math.floor(hh / dot);
        for (let k = 0; k < n; k++) for (let q = 0; q < cols; q++) out.push(s("circle", { cx: (cx + (q - (cols - 1) / 2) * dot).toFixed(1), cy: (H - B - (k + .5) * dot).toFixed(1), r: 3.6, fill: "var(--ink2)" }));
        const rest = Math.round((hh - n * dot) / dot * cols);
        for (let q = 0; q < rest; q++) out.push(s("circle", { cx: (cx + (q - (cols - 1) / 2) * dot).toFixed(1), cy: (H - B - (n + .5) * dot).toFixed(1), r: 3.6, fill: "var(--ink2)" }));
        // Too narrow for a count over every bar. The axis still gives the scale there.
        if (!nar) out.push(s("text", { x: cx, y: (H - B - Math.ceil(hh / dot) * dot - 5).toFixed(1), "text-anchor": "middle", class: "ax strong" }, short(c)));
        out.push(s("text", { x: cx, y: H - 4, "text-anchor": "middle", class: "ax" }, names[i]));
      });
      out.push(s("text", { x: W, y: 10, "text-anchor": "end", class: "ax" }, "bucket upper bound, ms"));
      return out;
    });
  }
  function draw(c) {
    last = c;
    const rows = windowed(c.metrics), has = rows.length > 0;
    const req = sum(rows, "requests");
    setNum("req", has ? req : null);
    setText("retries", has ? num(sum(rows, "retries")) : DASH);
    setText("rej", has ? num(sum(rows, "breaker_rejections")) : DASH);
    const rp = c.retry_policy || {};
    setText("p-att", num(rp.max_attempts));
    setText("p-back", isNum(rp.base_delay) ? rp.base_delay + " to " + rp.max_delay + " s" : DASH);
    setText("p-dead", isNum(rp.deadline) ? rp.deadline + " s" : DASH);
    setText("p-rate", isNum(c.rate_limit_per_second) ? c.rate_limit_per_second + " per s" : DASH);
    setText("b-state", { closed: "Closed", open: "Open", half_open: "Half-open" }[c.breaker] || c.breaker);
    setText("b-say", breakerText(c));
    drawSwitch(c.breaker, rows.some((m) => m.age < 2 && m.requests > 0));
    const th = c.failure_threshold || 0, cf = c.consecutive_failures || 0;
    setAttr("pips", "aria-label", cf + " consecutive failures of " + th);
    setKids($("pips"), [cf, th], () => Array.from({ length: th }, (_, i) => h("i", { class: i < cf ? "on" : "" })));
    drawResults(rows, has);
    const diff = req - ["successes", "rate_limited", "client_errors", "server_errors", "timeouts"].reduce((t, k) => t + sum(rows, k), 0);
    setText("eq", has ? "2xx + 429 + 4xx + 5xx + timeouts = requests. Difference " + num(diff) : " ");
    setNum("dl-n", c.dead_letters_open);
    const dls = c.dead_letters || [];
    setHidden("dl-none", dls.length > 0);
    setKids($("dls"), dls, () => dls.map((d) => h("div", { class: "dlr" }, h("code", null, d.kind), h("b", null, num(d.count)))));
    setHidden("dl-actions", dls.length === 0);
    setNum("ob", c.outbox_pending);
    setText("ob-dead", num(c.outbox_dead));
    const known = isNum(c.cursor_lag);
    setNum("lag", known ? c.cursor_lag : null);
    setText("lag-t", known ? "items behind the feed" : "Lag unknown: the feed head is not reported yet");
    setText("caught", c.cursor === "" && !known ? DASH : c.caught_up ? "yes" : "no");
    const cursorN = Number(c.cursor);
    setText("cur", (c.cursor === "" ? DASH : Number.isFinite(cursorN) ? num(cursorN) : c.cursor) + " / " + num(c.feed_head));
    setText("app", num(c.items_applied) + " / " + num(c.items_stale));
    const on = known && c.feed_head > 0 ? Math.round((c.feed_head - c.cursor_lag) / c.feed_head * 104) : 0;
    setAttr("lag-d", "aria-label", known ? num(c.cursor_lag) + " items behind the feed head" : "Lag unknown");
    setKids($("lag-d"), [on, known], () => Array.from({ length: 104 }, (_, i) => h("i", { class: i < on ? "on" : known ? "gap" : "" })));
    drawTime(rows);
    drawHist(rows);
  }
  function faultsSet(enabled) {
    $("f-set").disabled = !enabled;
    $("f-reset").disabled = !enabled;
  }
  function drawFaults(reply) {
    faults = { ...OFF, ...reply.faults };
    faultsSet(true);
    setHidden("f-off", true);
    setAttr("f-outage", "aria-checked", String(!!faults.outage));
    FIELDS.forEach(([k]) => {
      const input = $("r-" + k);
      const pct = Math.round((faults[k] || 0) * 100);
      if (!dragging) { input.value = pct; input.style.setProperty("--v", pct + "%"); setText("o-" + k, pct + " %"); }
    });
    const stats = reply.stats || {}, inj = stats.injected || {};
    setKids($("f-inj"), inj, () => Object.entries(inj).map(([k, v]) => h("div", null, h("span", { class: "m" }, k), h("b", { class: "m" }, num(v)))));
    setText("f-req", num(stats.requests));
    setText("f-se", num(stats.side_effects));
  }
  function faultsFailed(e) {
    faultsSet(false);
    setHidden("f-off", false);
    setText("f-off-t", e.message);
  }
  async function refreshFaults() {
    if (dragging || sendTimer) return;
    try { drawFaults(await api("/connector/faults")); } catch (e) { faultsFailed(e); }
  }
  async function sendFaults() {
    sendTimer = null;
    try {
      drawFaults(await api("/connector/faults", { method: "POST", body: faults }));
    } catch (e) {
      toast("Faults were not changed: " + e.message);
      refreshFaults();
    }
  }
  function scheduleSend() {
    clearTimeout(sendTimer);
    sendTimer = setTimeout(sendFaults, 200);
  }
  function buildSliders() {
    $("f-sliders").replaceChildren(...FIELDS.map(([k, label]) => {
      const input = h("input", { id: "r-" + k, type: "range", min: 0, max: 100, step: 1, value: 0 });
      input.addEventListener("input", () => {
        dragging = true;
        faults[k] = Number(input.value) / 100;
        input.style.setProperty("--v", input.value + "%");
        setText("o-" + k, input.value + " %");
        input.setAttribute("aria-valuetext", input.value + " percent");
        scheduleSend();
      });
      input.addEventListener("change", () => { dragging = false; });
      return h("div", { class: "fr" }, h("label", { for: "r-" + k }, label), h("output", { id: "o-" + k, for: "r-" + k }, DASH), input);
    }));
    $("f-outage").addEventListener("click", () => {
      if (!faults) return;
      faults.outage = !faults.outage;
      setAttr("f-outage", "aria-checked", String(faults.outage));
      scheduleSend();
    });
    $("f-reset").addEventListener("click", async () => {
      faults = { ...OFF };
      clearTimeout(sendTimer);
      await sendFaults();
      toast("All faults switched off.");
    });
  }
  /* dead letters: the payload is untrusted, so it only ever reaches the page as text */
  let letters = [], letterId = null;
  function drawLetter() {
    const row = letters.find((d) => d.id === letterId);
    setHidden("dl-body", !row);
    setHidden("dl-empty", letters.length > 0);
    setText("dl-count", letters.length ? (Number(last && last.dead_letters_open) > letters.length ? "Showing the latest " + num(letters.length) + " of " + num(last.dead_letters_open) + " open" : num(letters.length) + " open") : " ");
    $("dl-list").replaceChildren(...letters.map((d) => {
      const b = h("button", { class: "dlitem", type: "button", "aria-current": d.id === letterId ? "true" : "false" },
        h("code", null, d.kind), h("span", null, d.reason), h("small", { class: "m" }, clock(d.created_at)));
      b.addEventListener("click", () => { letterId = d.id; drawLetter(); });
      return h("li", null, b);
    }));
    if (!row) return;
    setText("dv-kind", row.kind);
    setText("dv-at", clock(row.created_at));
    setText("dv-reason", row.reason);
    $("dv-payload").textContent = JSON.stringify(row.payload, null, 2);
  }
  async function loadLetters() {
    setHidden("dl-loading", false);
    setHidden("dl-body", true);
    setHidden("dl-empty", true);
    setHidden("dl-err", true);
    try {
      letters = await api("/connector/dead-letters");
      if (!letters.some((d) => d.id === letterId)) letterId = letters.length ? letters[0].id : null;
      setHidden("dl-loading", true);
      drawLetter();
    } catch (e) {
      setHidden("dl-loading", true);
      setText("dl-err", "Dead letters did not load: " + e.message);
      setHidden("dl-err", false);
    }
  }
  async function resolveLetter() {
    const id = letterId;
    if (id === null) return;
    setHidden("dl-err", true);
    $("dv-resolve").disabled = true;
    try {
      await api(`/connector/dead-letters/${id}/resolve`, { method: "POST", body: {} });
      toast("Dead letter marked as resolved. The record is kept.");
    } catch (e) {
      // 404 means another operator closed it first. Either way, the fresh list is the truth.
      if (e.status !== 404) {
        setText("dl-err", "Not resolved: " + e.message);
        setHidden("dl-err", false);
        $("dv-resolve").disabled = false;
        return;
      }
      toast("This dead letter was already resolved.");
    }
    $("dv-resolve").disabled = false;
    letterId = null;
    await loadLetters();
    poller.now();
  }
  function initLetters() {
    const dlg = $("dlg-dl");
    $("dl-open").addEventListener("click", () => { dlg.showModal(); loadLetters(); });
    $("dl-close").addEventListener("click", () => dlg.close());
    $("dl-empty-close").addEventListener("click", () => dlg.close());
    $("dv-resolve").addEventListener("click", resolveLetter);
  }
  async function refresh() {
    let c;
    try {
      c = await api("/connector/health");
    } catch (e) {
      if (!loaded) fatal("Integration health did not load.", e.message, () => poller.now());
      throw e;
    }
    if (!loaded) { loaded = true; clearFatal($("board")); $("board").setAttribute("aria-busy", "false"); }
    draw(c);
    if (tickNo++ % 2 === 0) refreshFaults();
  }
  document.addEventListener("DOMContentLoaded", () => {
    fitBoard($("board"));
    buildSliders();
    initLetters();
    poller = poll(refresh, 2000);
    addEventListener("resize", () => { if (last) draw(last); });
  });
})();
