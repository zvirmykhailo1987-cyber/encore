// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Mykhailo Zvir
// Applied before first paint, so a saved theme never flashes.
(function () {
  try {
    var saved = localStorage.getItem("encore.theme");
    if (saved === "dark" || saved === "light") document.documentElement.dataset.theme = saved;
  } catch (e) { /* Storage can be blocked. The system preference still applies. */ }
})();
