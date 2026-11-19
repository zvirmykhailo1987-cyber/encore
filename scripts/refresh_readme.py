# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Rewrite the measured tables in README.md from the result files, so the README cannot drift from them."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
full = {r["test"]: r for r in json.loads((ROOT / "docs/benchmarks.json").read_text(encoding="utf-8"))}
low = {r["test"]: r for r in json.loads((ROOT / "docs/benchmarks-low.json").read_text(encoding="utf-8"))}
parallel = json.loads((ROOT / "docs/benchmarks-parallel.json").read_text(encoding="utf-8"))


def ms(v):
    return f"{v:,.0f} ms" if v >= 10 else f"{v:g} ms"


def fan(fans, path, r):
    share = r["served_from_cache"] / r["requests"] * 100
    return f"| {fans} | {path} | {r['requests_per_second']:,} | {r['failed_percent']:g}% | {ms(r['fan_p95_ms'])} | {ms(r['fan_p99_ms'])} | {share:.1f}% |"


def gate(n, r):
    return f"| {n:,} | {r['tickets_admitted'] / 80:,.0f} | {r['gate_failed_percent']:g}% | {ms(r['gate_p50_ms'])} | {ms(r['gate_p95_ms'])} | {ms(r['gate_p99_ms'])} |"


HEAD_F = "| Fans at once | Path | Requests per second | Failed | p95 | p99 | From cache |\n|---:|---|---:|---:|---:|---:|---:|\n"
HEAD_G = "| Scans requested per second | Tickets admitted per second | Scans failed | Gate p50 | Gate p95 | Gate p99 |\n|---:|---:|---:|---:|---:|---:|\n"
imports = sorted((r for r in full.values() if r.get("rows")), key=lambda r: r["rows"])
import_table = (
    "| Rows | File | Time | Rows per second | Worker memory, highest sample | Quarantined | Discrepancies |\n|---:|---:|---:|---:|---:|---:|---:|\n"
    + "\n".join(
        f"| {r['rows']:,} | {float(r['file_mb']):,.0f} MB | {r['seconds_total'] / 60:.1f} min | {r['rows_per_second']:,} | {r['worker_peak_mb']} MB | {r['quarantined']:,} | {r['discrepancies']} |"
        for r in imports
    )
)
fans_full = HEAD_F + "\n".join(
    [
        fan("5,000", "Django directly", full["Refresh spike, 5,000 fans, no edge"]),
        fan("5,000", "Through the edge", full["Refresh spike, 5,000 fans"]),
        fan("10,000", "Through the edge", full["Refresh spike, 10,000 fans"]),
        fan("20,000", "Through the edge", full["Refresh spike, 20,000 fans"]),
    ]
)
fans_low = HEAD_F + "\n".join(
    [
        fan("5,000", "Django directly", low["Low: 5,000 fans, Django directly"]),
        fan("5,000", "Through the edge", low["Low: 5,000 fans"]),
        fan("10,000", "Through the edge", low["Low: 10,000 fans"]),
        fan("20,000", "Through the edge", low["Low: 20,000 fans"]),
    ]
)
gates_full = HEAD_G + "\n".join(
    gate(n, full[f"Doors open, 10,000 fans and {n:,} scans per second"]) for n in (300, 600, 1000, 1500)
)
gates_low = HEAD_G + "\n".join(
    gate(n, low[f"Low: 10,000 fans and {n} scans per second"]) for n in (75, 150, 300, 600)
)
sat = [
    full[f"Doors open, 10,000 fans and {n:,} scans per second"]["tickets_admitted"] / 80 for n in (1000, 1500)
]
parallel_table = (
    "| Organizers at once | Rows each | Rows per second, all together | Discrepancies | Cross-organizer merges |\n|---:|---:|---:|---:|---:|\n"
    + "\n".join(
        f"| {r['organizers_at_once']} | {r['rows_each']:,} | {r['rows_per_second_all_together']:,} | {r['discrepancies']} | {r['cross_organizer_merges']} |"
        for r in parallel["results"]
    )
)

path = ROOT / "README.md"
s = path.read_text(encoding="utf-8")
tables = re.compile(r"(?:^\|.*\n)+", re.M)


def replace_after(text: str, marker: str, table: str) -> str:
    at = text.index(marker)
    found = tables.search(text, at)
    return text[: found.start()] + table.rstrip("\n") + "\n" + text[found.end() :]


s = replace_after(s, "**Import, including hashing and full reconciliation**", import_table)
s = replace_after(s, "**Fans refreshing one event page", fans_full)
s = replace_after(s, "**Gates scanning tickets while 10,000 fans refresh**", gates_full)
s = re.sub(
    r"Scans saturate at about [\d,]+ to [\d,]+ per second",
    f"Scans saturate at about {min(sat):,.0f} to {max(sat):,.0f} per second",
    s,
)
s = replace_after(s, "**The same tests on a machine two levels smaller:**", fans_low)
marker = fans_low.splitlines()[-1]
s = replace_after(s, marker, gates_low) if False else s
second = s.index(
    "| Scans requested per second", s.index("**The same tests on a machine two levels smaller:**")
)
found = tables.search(s, second)
s = s[: found.start()] + gates_low + "\n" + s[found.end() :]
imp = low["Import 1,000,000 rows"]
s = re.sub(
    r"Importing 1,000,000 rows took [\d.]+ minutes at [\d,]+ rows per second with \d+ MB",
    f"Importing 1,000,000 rows took {imp['seconds_total'] / 60:.1f} minutes at {imp['rows_per_second']:,} rows per second with {imp['worker_peak_mb']} MB",
    s,
)
marker = "**Organizers side by side**"
if marker in s:
    s = replace_after(s, marker, parallel_table)
s = re.sub(
    r"One run on a laptop, under Docker Desktop \(8 CPUs and 7 GB in the VM\), on synthetic data, on [\w ]+\.",
    f"One run on a laptop, under Docker Desktop (8 CPUs and 7 GB in the VM), on synthetic data, on {imports[0]['measured']}.",
    s,
)
path.write_text(s, encoding="utf-8", newline="\n")
print("README refreshed")
