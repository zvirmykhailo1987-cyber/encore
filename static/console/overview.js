(function () {
  "use strict";
  const { who, setAttr, setHidden, $, h, setText, num, bytes, clock, api, poll, statusChip, dryChip, fileName, setKids, DASH } = Encore;
  let everLoaded = false;

  function progress(run) {
    if (!run.source_size) return null;
    return Math.min(1, run.checkpoint_offset / run.source_size);
  }
  function miniDots(p) {
    const on = p === null ? 0 : Math.round(p * 10);
    return h("span", { class: "mdots shkala", role: "img", "data-r": p === null ? "" : p.toFixed(3), "aria-label": p === null ? "Source size not known" : (p * 100).toFixed(1) + " percent of the source read" },
      Array.from({ length: 10 }, (_, i) => h("i", { class: i < on ? "on" : "" })));
  }
  function row(run) {
    const url = "/runs/" + run.id;
    const p = progress(run);
    const link = h("a", { class: "rowlink m", href: url }, "Run " + run.id);
    const tr = h("tr", { "data-id": run.id });
    const pct = p === null ? DASH : (p * 100).toFixed(1) + "%";
    tr.append(
      h("td", null, link),
      h("td", { class: "src", "data-l": "Source" }, fileName(run.source_path)),
      h("td", null, h("span", { class: "chips" }, statusChip(run.status), run.dry_run ? dryChip() : null)),
      h("td", { "data-l": "Source read" }, h("span", { class: "pcell" }, miniDots(p), h("span", { class: "m" }, pct))),
      h("td", { class: "r m", "data-l": "Rows read" }, num(run.rows_read)),
      h("td", { class: "r m", "data-l": "Created" }, num(run.rows_created)),
      h("td", { class: "r m", "data-l": "Quarantined" }, num(run.rows_quarantined)),
      h("td", { class: "m nw", "data-l": "Started" }, clock(run.started_at || run.created_at)),
    );
    tr.addEventListener("click", (e) => { if (e.target !== link) location.href = url; });
    return tr;
  }
  function loadError(e) {
    $("runs-node").setAttribute("aria-busy", "false");
    setHidden("runs-loading", true);
    if (!everLoaded) {
      setHidden("runs-wrap", true);
      setHidden("runs-empty", true);
      setHidden("runs-error", false);
      setText("runs-error-t", e.message);
    }
  }
  async function refresh() {
    let runs;
    try {
      runs = await api("/imports/runs");
    } catch (e) {
      loadError(e);
      throw e;
    }
    everLoaded = true;
    $("runs-node").setAttribute("aria-busy", "false");
    setHidden("runs-loading", true);
    setHidden("runs-error", true);
    setHidden("runs-empty", runs.length > 0);
    setHidden("runs-wrap", runs.length === 0);
    setText("runs-count", runs.length ? (runs.length === 50 ? "latest 50" : runs.length + (runs.length === 1 ? " run" : " runs")) : " ");
    const body = $("runs");
    // Rows are rebuilt only when their own data moved, so a focused link keeps its place between refreshes.
    const existing = new Map([...body.children].map((tr) => [tr.dataset.id, tr]));
    let cursor = body.firstElementChild;
    runs.forEach((run) => {
      const sig = JSON.stringify([run.status, run.dry_run, run.source_path, run.checkpoint_offset, run.source_size, run.rows_read, run.rows_created, run.rows_quarantined, run.started_at]);
      let tr = existing.get(String(run.id));
      if (!tr || tr.dataset.sig !== sig) {
        const next = row(run);
        next.dataset.sig = sig;
        if (tr) { tr.replaceWith(next); if (cursor === tr) cursor = next; }
        tr = next;
      }
      existing.delete(String(run.id));
      if (tr === cursor) cursor = cursor.nextElementSibling;
      else body.insertBefore(tr, cursor);
    });
    existing.forEach((tr) => tr.remove());
    refreshConnector();
  }
  // An organizer key imports for its own organizer and cannot see the shared connector, so the screen says so.
  async function applyScope() {
    let w;
    try { w = await who(); } catch (e) { return; }
    const org = $("organizer");
    const scoped = w.scope === "organizer";
    org.readOnly = scoped;
    if (scoped) org.value = w.organizer;
    setText("organizer-hint", scoped ? "This key imports for " + w.organizer + " only." : "Contacts of one organizer never merge with those of another.");
    setHidden("link-body", w.scope !== "platform");
    setHidden("link-scope", w.scope === "platform");
  }
  async function refreshConnector() {
    if (!$("link-scope").hidden) return;
    try {
      const c = await api("/connector/health");
      setText("c-breaker", { closed: "Closed", open: "Open", half_open: "Half-open" }[c.breaker] || c.breaker);
      setText("c-lag", num(c.cursor_lag));
      setText("c-outbox", num(c.outbox_pending));
      setText("c-dl", num(c.dead_letters_open));
    } catch (e) {
      ["c-breaker", "c-lag", "c-outbox", "c-dl"].forEach((id) => setText(id, DASH));
    }
  }
  // What choosing a file leads to. Only said while there is a file to choose.
  function flowHint() {
    if ($("source").disabled) return;
    setText("source-hint", $("dry").checked ? "A dry run appears in Import runs. Nothing is written." : "A new run appears in Import runs.");
  }
  function pulseWire() {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const wire = $("ov-wire");
    wire.classList.remove("pulse");
    void wire.getBoundingClientRect();
    wire.classList.add("pulse");
  }
  async function loadSources() {
    const sel = $("source");
    try {
      const files = await api("/imports/sources");
      setKids(sel, files, () => (files.length
        ? files.map((f) => h("option", { value: f.name }, f.name + ", " + bytes(f.size)))
        : h("option", { value: "" }, "No files found")));
      sel.disabled = files.length === 0;
      $("start-btn").disabled = files.length === 0;
      if (files.length) flowHint();
      else {
        let folder = "the data folder";
        try { const w = await who(); if (w.scope === "organizer") folder = "data/" + w.organizer + "/"; } catch (e) { /* The generic wording still holds. */ }
        setText("source-hint", "Put a .csv or .jsonl file in " + folder + ", then reload this page.");
      }
    } catch (e) {
      setKids(sel, ["err"], () => h("option", { value: "" }, "Files did not load"));
      sel.disabled = true;
      $("start-btn").disabled = true;
      setText("source-hint", e.message + " Reload the page to try again.");
    }
  }
  document.addEventListener("DOMContentLoaded", () => {
    applyScope();
    addEventListener("encore-key", () => { applyScope(); loadSources(); });
    loadSources();
    const poller = poll(refresh, 2000);
    $("runs-retry").addEventListener("click", () => poller.now());
    $("ov-wire").addEventListener("animationend", () => $("ov-wire").classList.remove("pulse"));
    ["source", "dry"].forEach((id) => $(id).addEventListener("change", () => { flowHint(); pulseWire(); }));
    $("start").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("start-btn"), err = $("start-err");
      err.hidden = true;
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      try {
        const run = await api("/imports/runs", { method: "POST", body: { source: $("source").value, organizer: $("organizer").value.trim(), dry_run: $("dry").checked } });
        location.href = "/runs/" + run.id;
      } catch (ex) {
        setText(err, ex.message);
        err.hidden = false;
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
      }
    });
  });
})();
