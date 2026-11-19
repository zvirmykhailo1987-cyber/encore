// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Mykhailo Zvir
// Runs before the console's own scripts. Answers its API calls from a recording, so the real pages run with no server.
(function () {
  "use strict";
  const data = window.__REPLAY || { frames: {}, fallback: {} };
  const position = {};
  function reply(body, total, status) {
    const headers = { "Content-Type": "application/json" };
    if (total !== null && total !== undefined) headers["X-Total-Count"] = String(total);
    return new Response(status === 304 ? null : JSON.stringify(body), { status: status || 200, headers });
  }
  function frameFor(path) {
    const frames = data.frames[path];
    if (frames && frames.length) {
      const at = position[path] || 0;
      position[path] = Math.min(at + 1, frames.length - 1);
      return frames[at];
    }
    return data.fallback[path] || null;
  }
  window.fetch = function (url, options) {
    const path = String(url);
    const method = (options && options.method) || "GET";
    if (method !== "GET") {
      if (path === "/api/v1/imports/runs") return Promise.resolve(reply({ id: data.runId }, null, 202));
      parent.postMessage({ wrote: path }, "*");
      return Promise.resolve(reply({}, null));
    }
    const frame = frameFor(path);
    if (frame) return Promise.resolve(reply(frame.body, frame.total));
    return Promise.resolve(reply({ error: "This reply was not part of the recording." }, null, 404));
  };
  window.__go = function (path) { parent.postMessage({ go: String(path) }, "*"); };
  document.addEventListener("click", function (event) {
    const link = event.target.closest && event.target.closest("a[href]");
    if (!link) return;
    const href = link.getAttribute("href");
    if (href.charAt(0) === "#") return;
    event.preventDefault();
    if (href.charAt(0) === "/") window.__go(href);
  }, true);
  // The real console saves theme choices to storage. In the replay, the pack decides the theme.
})();
