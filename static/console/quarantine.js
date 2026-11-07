(function () {
  "use strict";
  const { setAttr, setHidden, $, h, setText, setKids, num, api, poll, toast, DASH } = Encore;
  const runId = document.body.dataset.runId;
  const PAGE = 200;
  let reason = "", offset = 0, total = 0, rows = [], selected = null, inspected = null, runInfo = null, loaded = false, busy = false;

  function retryBlock(run) {
    if (!run) return "The run state is not known yet.";
    if (run.dry_run) return "A dry run wrote nothing, so its rows cannot be retried. They can be dismissed.";
    if (run.status !== "completed" && run.status !== "paused") {
      return "Retry works when the run is completed or paused. This run is " + run.status.replace("_", " ") + ". Rows can be dismissed at any time.";
    }
    return "";
  }
  const STAT = { open: "Open", resolved: "Resolved", dismissed: "Dismissed" };
  function statusCell(st) {
    return h("span", { class: "rowst " + st }, h("i", { "aria-hidden": "true" }), STAT[st] || st);
  }
  function drawFilters() {
    const open = (runInfo && runInfo.quarantine_open) || [];
    const openTotal = open.reduce((t, r) => t + r.count, 0);
    const items = [{ reason: "", count: openTotal, label: "All reasons" }].concat(open.map((r) => ({ reason: r.reason, count: r.count, label: r.reason })));
    // A filter whose last open row was just handled stays visible, so the operator can step back out of it.
    if (reason && !open.some((r) => r.reason === reason)) items.push({ reason, count: 0, label: reason });
    setKids($("filters"), [items, reason], () => items.map((it) => {
      const b = h("button", { class: "fchip", type: "button", "aria-pressed": String(it.reason === reason) }, it.label, h("span", { class: "m" }, num(it.count)));
      b.addEventListener("click", () => { reason = it.reason; offset = 0; selected = null; poller.now(); });
      return b;
    }));
  }
  function drawRows() {
    const empty = rows.length === 0;
    setHidden("q-wrap", empty);
    setHidden("q-empty", !empty);
    if (empty) {
      setText("q-empty-t", reason ? "No rows with this reason." : "No quarantined rows.");
      setText("q-empty-p", reason ? "Choose another reason, or all reasons." : "Every row read so far passed validation.");
    }
    setHidden("pager", total === 0);
    setText("pg-range", total ? "Rows " + num(offset + 1) + " to " + num(offset + rows.length) + " of " + num(total) : " ");
    const atStart = offset === 0, atEnd = offset + PAGE >= total;
    [["pg-first", atStart], ["pg-prev", atStart], ["pg-next", atEnd], ["pg-last", atEnd]].forEach(([id, off]) => { if ($(id).disabled !== off) $(id).disabled = off; });
    setKids($("q-rows"), [rows.map((r) => [r.id, r.status, r.reason, r.detail]), selected], () => rows.map((r) => {
      const open = h("button", { class: "rowbtn m", type: "button", "aria-current": r.id === selected ? "true" : "false", "data-id": r.id, "aria-label": "Open row " + r.row_number }, num(r.row_number));
      open.addEventListener("click", () => select(r.id));
      const tr = h("tr", { class: r.id === selected ? "cur" : "", role: "row" },
        h("td", { class: "r", role: "cell" }, open),
        h("td", { class: "m nw", role: "cell" }, r.reason),
        h("td", { class: "det", role: "cell", title: r.detail }, r.detail || DASH),
        h("td", { role: "cell" }, statusCell(r.status)));
      tr.addEventListener("click", (e) => { if (!e.target.closest("button")) select(r.id); });
      return tr;
    }));
  }
  function select(id) {
    selected = id;
    drawRows();
    drawInspector(true);
    const btn = document.querySelector(`.rowbtn[data-id="${id}"]`);
    if (btn) btn.focus({ preventScroll: true });
    // Stacked, the inspector sits under the list. Without this a click on a row shows nothing happening.
    if (matchMedia("(max-width: 1199px)").matches) {
      $("inspector").scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }
  }
  function drawInspector(force) {
    const row = rows.find((r) => r.id === selected);
    const show = !!row;
    setHidden("in-none", show);
    setHidden("in-form", !show);
    setText("in-status", show ? "Row " + num(row.row_number) + " · " + (STAT[row.status] || row.status) : " ");
    if (!show) { inspected = null; return; }
    setText("in-reason", row.reason);
    setText("in-detail", row.detail);
    const open = row.status === "open";
    const block = retryBlock(runInfo);
    // Rebuilding the inputs on a refresh would throw away what the operator is typing.
    if (force || inspected !== row.id + row.status) {
      inspected = row.id + row.status;
      const err = $("in-err");
      err.hidden = true;
      const raw = row.raw && typeof row.raw === "object" ? row.raw : {};
      $("in-fields").replaceChildren(...Object.entries(raw).map(([key, value], i) => {
        const id = "fld" + i;
        const input = h("input", { class: "inp m", id, autocomplete: "off", spellcheck: "false", "data-key": key });
        input.value = value === null || value === undefined ? "" : String(value);
        input.defaultValue = input.value;
        input.disabled = !open;
        return h("div", { class: "fld" }, h("label", { class: "field-l m", for: id }, key), input);
      }));
    }
    $("in-retry").disabled = !open || !!block || busy;
    setAttr("in-retry", "title", block);
    $("in-dismiss").disabled = !open || busy;
    setHidden("q-rule", !block);
    if (block) setText("q-rule", block);
  }
  async function act(row, path, body, done) {
    const err = $("in-err");
    err.hidden = true;
    busy = true;
    drawInspector(false);
    try {
      await api(`/imports/quarantine/${row.id}/${path}`, { method: "POST", body });
      toast(done);
      inspected = null;
      await refresh();
    } catch (e) {
      setText(err, e.status === 422 ? "Still invalid: " + e.message + "." : e.message);
      err.hidden = false;
    }
    busy = false;
    drawInspector(false);
  }
  async function refresh() {
    let result;
    try {
      const q = new URLSearchParams({ offset: String(offset) });
      if (reason) q.set("reason", reason);
      result = await Promise.all([api("/imports/runs/" + runId), api(`/imports/runs/${runId}/quarantine?${q}`, { withTotal: true })]);
    } catch (e) {
      $("list-node").setAttribute("aria-busy", "false");
      setHidden("q-loading", true);
      if (!loaded) {
        setHidden("q-error", false);
        setText("q-error-t", e.status === 404 ? "Run " + runId + " does not exist." : e.message);
        if (e.status === 404) { poller.stop(); return; }
      }
      throw e;
    }
    runInfo = result[0];
    rows = result[1].body;
    total = result[1].total;
    // Handling rows can shrink the list under the current page. Step back to the page that still exists.
    if (offset > 0 && offset >= total) {
      offset = Math.max(0, Math.floor((total - 1) / PAGE) * PAGE);
      return refresh();
    }
    loaded = true;
    $("list-node").setAttribute("aria-busy", "false");
    setHidden("q-loading", true);
    setHidden("q-error", true);
    const openTotal = (runInfo.quarantine_open || []).reduce((t, r) => t + r.count, 0);
    setText("ql-count", num(total) + (reason ? " rows with this reason" : " rows") + ", " + num(openTotal) + " open in this run");
    drawFilters();
    drawRows();
    drawInspector(false);
  }
  let poller;
  document.addEventListener("DOMContentLoaded", () => {
    $("q-retry").addEventListener("click", () => poller.now());
    const go = (next) => { offset = next; selected = null; poller.now(); };
    $("pg-first").addEventListener("click", () => go(0));
    $("pg-prev").addEventListener("click", () => go(Math.max(0, offset - PAGE)));
    $("pg-next").addEventListener("click", () => go(offset + PAGE));
    $("pg-last").addEventListener("click", () => go(Math.max(0, Math.floor((total - 1) / PAGE) * PAGE)));
    $("in-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const row = rows.find((r) => r.id === selected);
      if (!row || $("in-retry").disabled) return;
      const corrected = {};
      $("in-fields").querySelectorAll("input").forEach((input) => {
        if (input.value !== input.defaultValue) corrected[input.dataset.key] = input.value;
      });
      act(row, "retry", { corrected }, "Row " + num(row.row_number) + " imported.");
    });
    $("in-dismiss").addEventListener("click", () => {
      const row = rows.find((r) => r.id === selected);
      if (row) act(row, "dismiss", {}, "Row " + num(row.row_number) + " dismissed.");
    });
    // Up and down move between rows, so the list works without a mouse.
    $("q-rows").addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const buttons = [...document.querySelectorAll(".rowbtn")];
      const at = buttons.indexOf(document.activeElement);
      const next = buttons[at + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) { e.preventDefault(); next.focus(); }
    });
    poller = poll(refresh, 2000);
  });
})();
