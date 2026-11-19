// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Mykhailo Zvir

(function () {
  "use strict";
  const { setAttr, setHidden, $, h, s, icon, setText, setKids, setNum, num, isNum, bytes, duration, clock, niceMax, api, poll, toast,
    confirmAction, showDetail, statusChip, fileName, fitBoard, fatal, clearFatal, DASH } = Encore;
  const runId = document.body.dataset.runId;
  const staleSeconds = Number(document.body.dataset.staleSeconds) || 30;
  let working = false, reportSaved = false;
  const OUTCOMES = ["created", "merged", "updated", "skipped", "quarantined"];
  // Where each outcome's ribbon lands, and how tall its node is.
  const TARGETS = { created: [102, 112], merged: [196, 52], updated: [260, 52], skipped: [324, 52], quarantined: [388, 52] };
  let poller, loaded = false, last = null, acting = false;

  const stale = (run) => run.status === "running" && isNum(run.heartbeat_age_seconds) && run.heartbeat_age_seconds > staleSeconds;
  function drawRibbons(run) {
    const read = run.rows_read;
    const g = $("ribbons");
    setKids(g, OUTCOMES.map((k) => run["rows_" + k]), () => {
      if (!read) return [];
      const total = 150, x0 = 618, x1 = 826, m = (x0 + x1) / 2;
      let top = 155;
      return OUTCOMES.map((k) => {
        const count = run["rows_" + k];
        const left = count / read * total;
        const y0 = top, y1 = top + left;
        top = y1;
        if (!count) return null;
        const [ty, maxH] = TARGETS[k];
        const th = Math.min(Math.max(left, 1.5), maxH);
        return s("path", {
          d: `M${x0} ${y0.toFixed(2)}C${m} ${y0.toFixed(2)} ${m} ${ty - th / 2} ${x1} ${ty - th / 2}L${x1} ${ty + th / 2}C${m} ${ty + th / 2} ${m} ${y1.toFixed(2)} ${x0} ${y1.toFixed(2)}Z`,
          fill: k === "quarantined" ? "var(--accent)" : "url(#rib)",
        });
      }).filter(Boolean);
    });
    const live = run.status === "running" && !stale(run);
    setAttr("readbar", "class", live ? "bar live" : read ? "bar" : "bar none");
    ["w-src", "w-run"].forEach((id) => setAttr(id, "class", live ? "w live" : "w"));
    ["p1", "p2", "p3"].forEach((id) => setAttr(id, "class", live ? "port live" : "port"));
    setAttr("w-q", "class", run.rows_quarantined ? "w-q on" : "w-q");
    setAttr("w-rec", "class", run.reconciling ? "w live" : "w");
    setAttr("p-rec", "class", run.reconciling ? "port live" : "port");
  }
  function drawDots(run) {
    const p = isNum(run.progress) ? Math.min(1, run.progress) : 0;
    const on = run.rows_read || p ? Math.max(Math.ceil(p * 80), p > 0 ? 1 : 0) : 0;
    setKids($("dots"), [on, run.status], () => Array.from({ length: 80 }, (_, i) =>
      h("i", { class: (i < on ? "on" : "") + (i === on - 1 && p < 1 ? " last" : "") })));
    document.documentElement.dataset.runStatus = run.status;
  }
  function drawThroughput(run) {
    const svg = $("thr"), batches = [...(run.batches || [])].reverse();
    const values = batches.map((b) => b.rows_per_second).filter(isNum);
    setHidden("thr-e", values.length > 0);
    setText("thr-e", run.batches_committed ? "Per-batch timings were not recorded for this run." : "No committed batches yet.");
    setAttr(svg, "visibility", values.length ? "visible" : "hidden");
    const box = svg.parentElement;
    const W = box.offsetWidth || 540, H = box.offsetHeight || 110;
    setKids(svg, [batches.map((b) => b.rows_per_second), W, H], () => {
      if (!values.length) return [];
      const L = 54, B = 16, T = 4, hi = niceMax(Math.max(...values));
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      const n = Math.max(batches.length - 1, 1);
      const X = (i) => L + i / n * (W - L - 6), Y = (v) => H - B - v / hi * (H - B - T);
      const out = [];
      [0, hi / 2, hi].forEach((v) => {
        out.push(s("line", { x1: L, x2: W, y1: Y(v), y2: Y(v), stroke: "var(--line)" }));
        out.push(s("text", { x: L - 7, y: Y(v) + 4, "text-anchor": "end", class: "ax" }, num(Math.round(v))));
      });
      batches.forEach((b, i) => {
        if (!isNum(b.rows_per_second)) return;
        const end = i === batches.length - 1;
        out.push(s("circle", { cx: X(i).toFixed(1), cy: Y(b.rows_per_second).toFixed(1), r: end ? 3.2 : 1.8, fill: end ? "var(--accent)" : "var(--ink2)" }));
      });
      const first = batches[0], lastB = batches[batches.length - 1];
      out.push(s("text", { x: L, y: H - 2, class: "ax" }, "batch " + num(first.number)));
      if (lastB !== first) out.push(s("text", { x: W - 6, y: H - 2, "text-anchor": "end", class: "ax" }, num(lastB.number)));
      return out;
    });
  }
  function drawLog(run) {
    const items = (run.batches || []).slice(0, 4);
    setHidden("log-e", items.length > 0);
    setText("log-e", run.batches_committed ? "This run has no per-batch journal." : "Nothing committed yet.");
    setKids($("log"), items.map((b) => b.number), () => items.map((b) => h("li", null,
      h("b", null, "Batch " + num(b.number) + " committed"),
      h("span", null, "row " + num(b.last_row) + " · byte " + num(b.last_offset)))));
  }
  function drawQuarantine(run) {
    const rows = run.quarantine_open || [];
    const shown = rows.slice(0, 6);
    const openTotal = rows.reduce((sum, r) => sum + r.count, 0);
    setText("q-open", num(openTotal) + " open");
    const none = rows.length === 0;
    setHidden("q-none", !none);
    setText("q-none-t", run.rows_quarantined ? "All quarantined rows are handled." : "No rows quarantined.");
    const mq = rows.length ? Math.max(...rows.map((r) => r.count)) : 1;
    setKids($("q-list"), shown, () => {
      const out = shown.map((r) => {
        const on = Math.max(1, Math.round(r.count / mq * 40));
        return h("div", { class: "qr" },
          h("span", { class: "nm", title: r.reason }, r.reason),
          h("span", { class: "tr", "aria-hidden": "true" }, Array.from({ length: 40 }, (_, i) => h("i", { class: i < on ? "on" : "" }))),
          h("span", { class: "vv" }, num(r.count)));
      });
      if (rows.length > shown.length) out.push(h("p", { class: "hint" }, "and " + (rows.length - shown.length) + " more reasons"));
      return out;
    });
    const latest = run.quarantine_latest;
    setText("q-last", latest ? num(latest.row) + " · " + latest.reason : DASH);
    setAttr("q-last", "title", latest ? latest.reason : "");
  }
  function drawLedger(run) {
    const sum = OUTCOMES.reduce((t, k) => t + run["rows_" + k], 0);
    setNum("a-read", run.rows_read);
    setText("read-l", num(run.rows_read));
    OUTCOMES.forEach((k) => {
      setNum("a-" + k, run["rows_" + k]);
      setText("s-" + k, run.rows_read ? (run["rows_" + k] / run.rows_read * 100).toFixed(1) + "%" : DASH);
    });
    setNum("a-sum", sum);
    setNum("a-diff", run.rows_read - sum);
    $("sum-chip").classList.toggle("off", run.rows_read !== sum);
  }
  function setCheck(id, v) { setText(id, isNum(v) ? num(v) : DASH); $(id).parentElement.classList.toggle("bad", isNum(v) && v !== 0); }
  function drawRecon(run) {
    const finished = run.status === "completed" || run.status === "rolled_back";
    const report = run.reconciliation;
    const show = !run.dry_run && finished && report;
    setHidden("r-zero", !show);
    let wait = "";
    if (run.dry_run) wait = "A dry run writes nothing, so there is nothing to reconcile.";
    else if (run.status === "failed") wait = "This run failed before it finished. Resume it, and it reconciles when it finishes.";
    else if (!show) wait = "Reconciles when the run finishes.";
    setHidden("r-wait", !wait);
    setText("r-wait-t", wait);
    let scope = " ";
    if (show && run.status === "rolled_back") scope = "Report from before the rollback. These rows have since been restored.";
    else if (show && run.reconciling) scope = "Checking again. This is the last saved report.";
    else if (show && reportSaved) scope = "New report saved. Source file against the database.";
    else if (show) scope = "Source file against the database.";
    setText("r-scope", scope);
    $("recon").classList.toggle("stale", Boolean(show && run.reconciling));
    const r = show ? report : {};
    const c = r.checks || {};
    if (show) {
      setNum("r-n", r.discrepancies);
      setText("r-word", r.discrepancies === 1 ? "discrepancy" : "discrepancies");
      setKids($("r-bal"), [r.balanced], () => [icon(r.balanced ? "i-uspikh" : "i-uvaha"), r.balanced ? "Balanced" : "Does not balance"]);
      $("r-zero").classList.toggle("bad", !r.balanced);
    }
    setText("r-source", num(r.source_rows));
    setText("r-read", num(r.rows_read));
    setCheck("r-c1", c.source_rows_vs_rows_read);
    setCheck("r-c2", c.counters_vs_rows_read);
    setCheck("r-c3", c.contacts_written_vs_created);
    setCheck("r-c4", c.aliases_written_vs_created_plus_merged);
    setCheck("r-miss", r.missing_in_target);
    setCheck("r-mis", r.field_mismatches);
    setCheck("r-inv", r.invalid_rows_not_in_quarantine);
  }
  function note(run) {
    const el = $("note");
    let text = "", more = false;
    const age = run.heartbeat_age_seconds;
    const rechecking = run.status === "completed" && run.reconciling;
    if (rechecking) {
      text = "Reconciling. This reads the whole source file again. The last report stays on screen until the new one is saved.";
    } else if (stale(run)) {
      text = "No heartbeat for " + Math.round(age) + " s. The reaper will mark this run interrupted.";
    } else if (run.status === "running" && run.pause_requested) {
      text = "Pause requested. The run stops after the batch in progress.";
    } else if (run.status === "interrupted") {
      text = "The worker stopped. Committed batches are safe, and a resume continues from the checkpoint.";
    } else if (run.status === "failed") {
      text = run.error ? run.error.split("\n")[0] : "The run failed and reported no error text.";
      more = !!run.error;
    } else if (run.status === "paused") {
      text = "Paused at the checkpoint. Resume continues from there.";
    } else if (run.status === "pending") {
      text = "Queued. Waiting for a worker to pick this run up.";
    } else if (run.status === "rolled_back") {
      text = "Rolled back. Every change this run made has been restored.";
    }
    setHidden(el, !text);
    el.querySelector("use").setAttribute("href", rechecking ? "#i-den" : "#i-uvaha");
    setText("note-t", text);
    setHidden("note-more", !more);
    el.classList.toggle("quiet", rechecking || run.status === "pending" || run.status === "rolled_back" || run.status === "paused");
  }
  const ACTIONS = {
    pause: { label: "Pause" },
    resume: { label: "Resume", primary: true, icon: "i-tsykl", done: "Resume requested." },
    reconcile: { label: "Reconcile again" },
    rollback: { label: "Roll back", icon: "i-skasuvaty", confirm: ["Roll back this run?", "Every contact this run changed goes back to its earlier state. This cannot be undone from the console.", "Roll back"], done: "Run rolled back." },
  };
  function actionList(run) {
    if (run.dry_run) {
      if (["interrupted", "paused", "failed", "pending"].includes(run.status)) return ["resume"];
      return run.status === "running" ? ["pause"] : [];
    }
    return { running: ["pause"], paused: ["resume", "rollback"], interrupted: ["resume", "rollback"], failed: ["resume", "rollback"], completed: ["reconcile", "rollback"], pending: ["resume"], rolled_back: [] }[run.status] || [];
  }
  async function doAction(name) {
    const a = ACTIONS[name];
    if (a.confirm && !(await confirmAction(...a.confirm))) return;
    if (acting) return;
    acting = true;
    try {
      await api(`/imports/runs/${runId}/${name}`, { method: "POST", body: {} });
      if (name === "reconcile") toast("Reconciliation started.");
      else if (a.done) toast(a.done);
      else if (name === "pause") toast("Pause requested.");
      poller.now();
    } catch (e) {
      toast(a.label + " failed: " + e.message);
    }
    acting = false;
    if (last) drawActions(last);
  }
  function drawActions(run) {
    const list = actionList(run);
    const el = $("actions");
    const paused = run.pause_requested && run.status === "running";
    setKids(el, [list, paused, working], () => {
      if (!list.length) return h("span", { class: "hint" }, "No actions for a " + (run.status === "rolled_back" ? "rolled back" : run.status) + " run.");
      return list.map((name) => {
        const a = ACTIONS[name];
        const label = name === "resume" && run.status === "pending" ? "Start" : a.label;
        const b = h("button", { class: "btn" + (a.primary ? " p" : ""), type: "button", disabled: (name === "pause" && paused) || working, "aria-busy": working && name === "reconcile" ? "true" : null, title: working && name === "rollback" ? "Available when reconciliation finishes" : null }, a.icon ? icon(a.icon) : null, working && name === "reconcile" ? "Reconciling" : label);
        b.addEventListener("click", () => doAction(name));
        return b;
      });
    });
  }
  function finishedMessage(report) {
    if (!report) return "Reconciliation finished and saved.";
    const word = report.discrepancies === 1 ? " discrepancy" : " discrepancies";
    return "Reconciliation finished and saved: " + num(report.discrepancies) + word + ", " + (report.balanced ? "balanced." : "does not balance.");
  }
  function draw(run) {
    last = run;
    // The server owns this state, so a page reload during a long reconciliation still shows it.
    if (working && !run.reconciling) {
      reportSaved = !run.reconcile_error;
      toast(run.reconcile_error ? "Reconciliation failed: " + run.reconcile_error + ". The last saved report stays." : finishedMessage(run.reconciliation), 7000);
    }
    if (run.reconciling) reportSaved = false;
    working = Boolean(run.reconciling);
    const size = run.source_size;
    setText("f-file", fileName(run.source_path));
    setAttr("f-file", "title", run.source_path);
    setText("f-size", size ? bytes(size) : DASH);
    setText("f-fmt", run.source_format);
    setText("f-org", run.organizer);
    setText("f-sys", run.source_system);
    setText("f-sha", run.source_sha256 ? run.source_sha256.slice(0, 8) + "…" + run.source_sha256.slice(-4) : DASH);
    setAttr("f-sha", "title", run.source_sha256 || "");
    const unm = run.unmapped_columns || [];
    setText("f-unm", !run.header || !run.header.length ? DASH : unm.length ? (unm.length > 1 ? unm.length + " columns" : unm[0]) : "none");
    setAttr("f-unm", "title", unm.join(", "));
    setKids($("chips"), [run.status], () => statusChip(run.status));
    setHidden("dry-tag", !run.dry_run);
    setText("pct", isNum(run.progress) ? (Math.min(1, run.progress) * 100).toFixed(1) : DASH);
    setHidden("pct-u", !isNum(run.progress));
    setText("pct-of", size ? bytes(run.checkpoint_offset) + " of " + bytes(size) + " read" : "Source size not known yet");
    drawDots(run);
    setText("ck-row", num(run.checkpoint_row));
    setText("ck-off", num(run.checkpoint_offset));
    setText("speed", isNum(run.speed_rows_per_second) ? num(run.speed_rows_per_second) + " rows/s" : DASH);
    setText("left", run.status === "completed" ? "finished" : isNum(run.seconds_left) && !stale(run) ? duration(run.seconds_left) : DASH);
    setText("batches", num(run.batches_committed));
    setText("hb", ["running", "interrupted"].includes(run.status) && isNum(run.heartbeat_age_seconds) ? duration(run.heartbeat_age_seconds) + " ago" : DASH);
    drawLedger(run);
    drawRibbons(run);
    drawThroughput(run);
    drawLog(run);
    drawQuarantine(run);
    drawRecon(run);
    note(run);
    setText("w-status", statusLabel(run.status));
    setText("w-started", clock(run.started_at));
    setText("w-finished", clock(run.finished_at));
    setText("w-pause", run.status === "running" || run.status === "paused" ? (run.pause_requested ? "yes" : "no") : DASH);
    setText("w-resume", "Resumes " + num(run.resume_count));
    drawActions(run);
  }
  const statusLabel = (st) => ({ rolled_back: "Rolled back" }[st] || st.charAt(0).toUpperCase() + st.slice(1));
  async function refresh() {
    let run;
    try {
      run = await api("/imports/runs/" + runId);
    } catch (e) {
      if (e.status === 404) {
        poller && poller.stop();
        fatal("Run " + runId + " does not exist.", "It may have been removed, or the link is wrong.");
        return;
      }
      if (!loaded) fatal("This run did not load.", e.message, () => poller.now());
      throw e;
    }
    if (!loaded) { loaded = true; clearFatal($("board")); $("board").setAttribute("aria-busy", "false"); }
    draw(run);
  }
  document.addEventListener("DOMContentLoaded", () => {
    fitBoard($("board"));
    $("note-more").addEventListener("click", () => { if (last) showDetail("Error from the run", last.error); });
    poller = poll(refresh, 2000);
    addEventListener("resize", () => { if (last) drawThroughput(last); });
  });
})();
