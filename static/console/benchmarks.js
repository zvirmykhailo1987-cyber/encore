// Charts of the numbers in docs/benchmarks.json. The tables on the page carry the same values without script.
(function () {
  "use strict";
  const { $, s, num, isNum, niceMax } = Encore;
  let data = { imports: [], fans: [], doors: [] };

  function size(svg, fallbackH) {
    const box = svg.parentElement;
    const W = box.offsetWidth || 480, H = box.offsetHeight || fallbackH;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    return [W, H];
  }
  const label = (x, y, text, anchor, cls) => s("text", { x, y, "text-anchor": anchor || "start", class: "ax " + (cls || "") }, text);

  function drawImport() {
    const svg = $("chart-import");
    if (!svg) return;
    const [W, H] = size(svg, 128);
    const rows = data.imports.filter((r) => isNum(r.value));
    if (!rows.length) { svg.replaceChildren(); return; }
    const L = 124, R = 24, B = 20, T = 18, hi = niceMax(Math.max(...rows.map((r) => r.value)));
    const X = (v) => L + v / hi * (W - L - R), lane = (H - T - B) / rows.length, out = [];
    out.push(label(W - R, 11, "rows per second", "end"));
    [0, hi / 2, hi].forEach((v) => {
      out.push(s("line", { x1: X(v), x2: X(v), y1: T, y2: H - B, stroke: "var(--line)" }));
      out.push(label(X(v), H - 4, num(v), "middle"));
    });
    rows.forEach((r, i) => {
      const y = T + i * lane + lane / 2;
      out.push(label(L - 8, y + 4, r.label, "end"));
      out.push(s("rect", { x: L, y: y - 9, width: Math.max(X(r.value) - L, 1).toFixed(1), height: 18, rx: 2, fill: "var(--f2)" }));
      out.push(label(L + 8, y + 4, num(r.value), "start", "strong"));
    });
    svg.replaceChildren(...out);
  }

  // Sub-millisecond and second-long responses share these charts, so the scale is logarithmic and says so.
  function drawLogLatency(id, rows) {
    const svg = $(id);
    if (!svg) return;
    const [W, H] = size(svg, 176);
    const all = rows.flatMap((r) => [r.p50, r.p95, r.p99]).filter((v) => isNum(v) && v > 0);
    if (!all.length) { svg.replaceChildren(); return; }
    const lo = Math.floor(Math.log10(Math.min(...all))), hi = Math.max(Math.ceil(Math.log10(Math.max(...all))), lo + 1);
    const L = 104, R = 24, B = 20, T = 40;
    const X = (v) => L + (Math.log10(v) - lo) / (hi - lo) * (W - L - R), lane = (H - T - B) / rows.length, out = [];
    out.push(label(W - R, 11, "response time, ms, log scale", "end"));
    let lx = L;
    [["p50", "none", "var(--ink2)"], ["p95", "var(--ink2)", "var(--ink2)"], ["p99", "var(--accent)", "var(--accent)"]].forEach(([n, fill, stroke]) => {
      out.push(s("circle", { cx: lx + 4, cy: 27, r: 4, fill, stroke, "stroke-width": 1.5 }));
      out.push(label(lx + 13, 31, n));
      lx += 48;
    });
    // On a narrow chart every second power keeps its number, so neighbours do not run together.
    const wide = (W - L - R) / (hi - lo) >= 46;
    for (let e = lo; e <= hi; e++) {
      const v = Math.pow(10, e);
      out.push(s("line", { x1: X(v), x2: X(v), y1: T, y2: H - B, stroke: "var(--line)" }));
      if (wide || (hi - e) % 2 === 0) out.push(label(X(v), H - 4, e < 0 ? v.toFixed(-e) : num(v), "middle"));
    }
    rows.forEach((r, i) => {
      const y = T + i * lane + lane / 2;
      out.push(label(L - 10, y + 4, r.label, "end"));
      const pts = [r.p50, r.p99].filter((v) => isNum(v) && v > 0);
      if (pts.length === 2) out.push(s("line", { x1: X(pts[0]).toFixed(1), x2: X(pts[1]).toFixed(1), y1: y, y2: y, stroke: "var(--wire)", "stroke-width": 1.5 }));
      [[r.p50, "var(--card)", "var(--ink2)"], [r.p95, "var(--ink2)", "var(--ink2)"], [r.p99, "var(--accent)", "var(--accent)"]].forEach(([v, fill, stroke]) => {
        if (isNum(v) && v > 0) out.push(s("circle", { cx: X(v).toFixed(1), cy: y, r: 4, fill, stroke, "stroke-width": 1.5 }));
      });
    });
    svg.replaceChildren(...out);
  }

  // Requested against admitted. Points on the dashed line kept up. Points under it did not.
  function drawDoors() {
    const svg = $("chart-doors");
    if (!svg) return;
    const [W, H] = size(svg, 176);
    const rows = data.doors.filter((r) => isNum(r.requested) && isNum(r.admitted));
    if (!rows.length) { svg.replaceChildren(); return; }
    const L = 48, R = 24, B = 20, T = 22, hi = niceMax(Math.max(...rows.map((r) => Math.max(r.requested, r.admitted))));
    const X = (v) => L + v / hi * (W - L - R), Y = (v) => H - B - v / hi * (H - T - B), out = [];
    out.push(label(L, 11, "admitted/s, computed"));
    out.push(label(W - R, 11, "requested/s", "end"));
    [0, hi / 2, hi].forEach((v) => {
      out.push(s("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: "var(--line)" }));
      out.push(label(L - 6, Y(v) + 4, num(v), "end"));
      out.push(label(X(v), H - 4, num(v), v === hi ? "end" : "middle"));
    });
    out.push(s("line", { x1: X(0), y1: Y(0), x2: X(hi), y2: Y(hi), stroke: "var(--wire)", "stroke-dasharray": "4 4" }));
    out.push(s("polyline", { fill: "none", stroke: "var(--ink2)", "stroke-width": 1.5, points: rows.map((r) => X(r.requested).toFixed(1) + "," + Y(r.admitted).toFixed(1)).join(" ") }));
    rows.forEach((r) => {
      out.push(s("circle", { cx: X(r.requested).toFixed(1), cy: Y(r.admitted).toFixed(1), r: 4, fill: "var(--ink2)" }));
      out.push(label(X(r.requested), Y(r.admitted) + 18, num(Math.round(r.admitted)), "middle", "strong"));
    });
    svg.replaceChildren(...out);
  }

  function draw() {
    try {
      data = JSON.parse($("bench-data").textContent);
      drawImport();
      drawLogLatency("chart-fans", data.fans);
      drawDoors();
      drawLogLatency("chart-gate", data.doors);
    } catch (e) {
      $("chart-err").hidden = false;
    }
  }
  document.addEventListener("DOMContentLoaded", draw);
  addEventListener("resize", draw);
})();
