"""Build dist/Encore-Pack.html: one file that opens by double click and carries the program, the code and the material.

Numbers come from the result files in docs/, never typed here. It stops if a test named in the text does not exist,
or if a word that must not appear does appear. Needs: pip install markdown pygments.
Usage: python scripts/build_pack.py [--final]   --final also requires every result file.
"""

import base64
import html
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import markdown
from pygments.lexers import get_lexer_for_filename
from pygments.token import Comment, Keyword, Literal, Name, String
from pygments.util import ClassNotFound

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
# A language variant swaps these (scripts/build_pack_uk.py). The English pack uses the defaults.
TEXT_DOCS = DOCS
CHIP_BUILT, CHIP_DESIGN, CHIP_DESIGN_ONLY = "Built", "Design", "Design only"
POSTPROCESS = None
LANG = "en"
OUT =ROOT / "dist" / "Encore-Pack.html"
HERE = Path(__file__).parent / "pack"
FINAL = "--final" in sys.argv
LOCAL_WORDS = HERE / "forbidden.local.txt"
FORBIDDEN = LOCAL_WORDS.read_text(encoding="utf-8").split() if LOCAL_WORDS.exists() else []
NOT_IN_ZIP = ("docs/uk/", "scripts/pack_uk/", "scripts/build_pack_uk.py", "docs/demo-uk", "docs/shots-uk/", "docs/demo.webm", "docs/replay.json", "docs/shots/", "docs/design/", "dist/", "data/")
BRAND = (
    '<svg viewBox="0 0 26 18" aria-hidden="true"><path d="M3 9H9L13 3H23" fill="none" stroke="currentColor" stroke-width="1.6"/>'
    '<path d="M9 9L13 15H23" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="3" cy="9" r="2.4" fill="var(--accent)"/>'
    '<circle cx="23" cy="3" r="2.2" fill="currentColor"/><circle cx="23" cy="15" r="2.2" fill="currentColor"/></svg>'
)
FILE_NOTES = {
    "imports/pipeline.py": "The import: one transaction per batch, lease, rollback.",
    "imports/reconcile.py": "Independent proof: reads the file again.",
    "imports/source.py": "Streaming readers for CSV, JSONL and mongoexport.",
    "imports/mapping.py": "Column map and validation of one row.",
    "imports/cutover.py": "The go or no-go report.",
    "ticketing/client.py": "All retry and timeout decisions.",
    "ticketing/resilience.py": "Backoff with jitter, token bucket, circuit breaker.",
    "ticketing/services.py": "Sync, webhooks, gate scans, offline uploads, outbox.",
    "crm/erasure.py": "Erasure during a migration, and replay after a restore.",
    "deploy/nginx.conf": "The edge cache for fans.",
    "deploy/gunicorn.conf.py": "The fix from the incident review.",
    "encore/metrics.py": "What Prometheus reads.",
    "mock_api/app.py": "The stand-in ticketing API with switchable failures.",
    "scripts/mutation_check.py": "Breaks the code on purpose and checks the tests notice.",
    "scripts/k8s_proof.py": "Deploys to a real cluster and kills things.",
    "tests/test_platform_realities.py": "Organizers, erasure, offline gates, operations.",
}


def load(name: str, default=None):
    path = DOCS / name
    if not path.exists():
        if FINAL:
            raise SystemExit(f"--final needs docs/{name}")
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def project_files() -> list[str]:
    done = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return sorted(
        path for path in done.stdout.splitlines()
        if (ROOT / path).is_file() and not path.startswith(NOT_IN_ZIP)
    )


# ------------------------------------------------------------------ code


def highlight(path: str, text: str) -> str:
    def css(token) -> str:
        if token in Comment:
            return "tc"
        if token in String or token in Literal.String:
            return "ts"
        if token in Keyword:
            return "tk"
        if token in Name.Function or token in Name.Class:
            return "tf"
        if token in Literal.Number:
            return "tn"
        return ""

    try:
        lexer = get_lexer_for_filename(path, stripnl=False, ensurenl=False)
    except ClassNotFound:
        lexer = None
    pieces = lexer.get_tokens(text) if lexer else [(None, text)]
    lines = [""]
    for token, value in pieces:
        cls = css(token) if token is not None else ""
        parts = value.split("\n")
        for index, part in enumerate(parts):
            if index:
                lines.append("")
            if part:
                escaped = html.escape(part, quote=False)
                lines[-1] += f'<span class="{cls}">{escaped}</span>' if cls else escaped
    return "".join(f'<span class="ln" data-n="{n}">{line}\n</span>' for n, line in enumerate(lines, 1))


def code_data(files: list[str]) -> dict:
    data = {}
    for path in files:
        target = ROOT / path
        if target.suffix in (".woff2", ".png", ".webm", ".ico") or target.stat().st_size > 400_000:
            continue
        try:
            text = target.read_text(encoding="utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            continue
        data[path] = {
            "html": highlight(path, text),
            "text": text,
            "lines": text.count("\n") + 1,
            "about": FILE_NOTES.get(path, ""),
        }
    return data


def source_zip(files: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(ROOT / path, f"encore/{path}")
    return buffer.getvalue()


# ------------------------------------------------------------------ text


def all_tests() -> dict[str, str]:
    tests = {}
    for path in (ROOT / "tests").glob("test_*.py"):
        for name in re.findall(r"def (test_\w+)", path.read_text(encoding="utf-8")):
            tests[name] = f"tests/{path.name}"
    return tests


def render_md(text: str, files: set[str], tests: dict[str, str]) -> str:
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    body = body.replace("<table>", '<div class="tw"><table>').replace("</table>", "</table></div>")
    body = re.sub(r'<a href="(https?://)', r'<a rel="noreferrer" target="_blank" href="\1', body)
    body = body.replace(f"<strong>{CHIP_BUILT}</strong>", f'<span class="chip built">{CHIP_BUILT}</span>')
    body = re.sub(rf"<td>(?:{CHIP_DESIGN_ONLY}|{CHIP_DESIGN})</td>", f'<td><span class="chip design">{CHIP_DESIGN}</span></td>', body)
    body = re.sub(rf"(?<![\w-]){CHIP_DESIGN}(?= \| |</td>)", f'<span class="chip design">{CHIP_DESIGN}</span>', body)
    body = re.sub(r'<pre><code class="language-bash">', '<pre class="cmd"><code>', body)

    def link_code(match: re.Match) -> str:
        word = html.unescape(match.group(1))
        if word.startswith("test_") and re.fullmatch(r"test_\w+", word):
            if word not in tests:
                if (ROOT / "tests" / f"{word}.py").exists():
                    return match.group(0)
                raise SystemExit(f"The text names a test that does not exist: {word}")
            return f'<a href="#code" data-file="{tests[word]}" data-find="def {word}"><code>{match.group(1)}</code></a>'
        if word in files:
            return f'<a href="#code" data-file="{word}"><code>{match.group(1)}</code></a>'
        short = word.rstrip("/")
        generated = FINAL or not short.endswith((".json", ".txt"))
        if generated and "/" in short and not short.startswith(("/", "http")) and not (ROOT / short).exists() and re.fullmatch(r"(docs|scripts|deploy|tests|imports|ticketing|crm|encore)/[\w./-]+", short):
            raise SystemExit(f"The text names a file that does not exist: {word}")
        return match.group(0)

    return re.sub(r"<code>([^<]+)</code>", link_code, body)


def read_doc(name: str) -> str:
    text = (TEXT_DOCS / name).read_text(encoding="utf-8")
    return re.sub(r"^# .+\n", "", text, count=1)


def lead_of(name: str) -> str:
    first = (TEXT_DOCS / name).read_text(encoding="utf-8").split("\n", 1)[0]
    return first.removeprefix("# ")


def parse_questions(files: set[str], tests: dict[str, str]) -> list[dict]:
    text = (TEXT_DOCS / "questions.md").read_text(encoding="utf-8")
    items = []
    group = ""
    for block in re.split(r"\n(?=##+ )", text):
        if block.startswith("## "):
            group = block[3:].split("\n", 1)[0].strip()
        elif block.startswith("### "):
            title, _, answer = block[4:].partition("\n")
            items.append({"group": group, "q": title.strip(), "a": render_md(answer.strip(), files, tests)})
    return items


def external_sources(names: list[str]) -> list[tuple[str, str, str]]:
    seen = {}
    for name in names:
        for label, url in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", (TEXT_DOCS / name).read_text(encoding="utf-8")):
            seen.setdefault(url, (label, lead_of(name)))
    return sorted(((label, url, where) for url, (label, where) in seen.items()), key=lambda r: r[1])


# ------------------------------------------------------------------ numbers and charts


def n(value, digits=0) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    return f"{value:,.{digits}f}"


def ms(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} ms" if value >= 10 else f"{value:g} ms"


def find(rows, name):
    for row in rows or []:
        if row.get("test") == name:
            return row
    return {}


def table(head: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    th = "".join(f'<th class="{"r" if i in right else ""}">{h}</th>' for i, h in enumerate(head))
    trs = "".join(
        "<tr>" + "".join(f'<td class="{"r" if i in right else ""}">{c}</td>' for i, c in enumerate(row)) + "</tr>" for row in rows
    )
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def fan_rows(rows, prefix_pairs):
    out = []
    for label, path, name in prefix_pairs:
        r = find(rows, name)
        if not r:
            continue
        share = r["served_from_cache"] / r["requests"] * 100 if r.get("requests") else None
        out.append([label, path, n(r.get("requests_per_second")), f'{r.get("failed_percent", 0):g}%', ms(r.get("fan_p95_ms")), ms(r.get("fan_p99_ms")), f"{share:.1f}%" if share is not None else "—"])
    return out


def gate_rows(rows, pairs):
    out = []
    for req, name in pairs:
        r = find(rows, name)
        if not r:
            continue
        admitted = r["tickets_admitted"] / (r["seconds_at_peak"] + 20)
        out.append([n(req), n(admitted), f'{r.get("gate_failed_percent", 0):g}%', ms(r.get("gate_p50_ms")), ms(r.get("gate_p95_ms")), ms(r.get("gate_p99_ms")), "equal" if r["tickets_admitted"] == r["outbox_messages"] else "NOT equal"])
    return out


FULL_FANS = [("5,000", "Django directly", "Refresh spike, 5,000 fans, no edge"), ("5,000", "Through the edge", "Refresh spike, 5,000 fans"), ("10,000", "Through the edge", "Refresh spike, 10,000 fans"), ("20,000", "Through the edge", "Refresh spike, 20,000 fans")]
LOW_FANS = [("5,000", "Django directly", "Low: 5,000 fans, Django directly"), ("5,000", "Through the edge", "Low: 5,000 fans"), ("10,000", "Through the edge", "Low: 10,000 fans"), ("20,000", "Through the edge", "Low: 20,000 fans")]
FULL_GATES = [(s, f"Doors open, 10,000 fans and {s:,} scans per second") for s in (300, 600, 1000, 1500)]
LOW_GATES = [(s, f"Low: 10,000 fans and {s} scans per second") for s in (75, 150, 300, 600)]


def log_x(value, low, high, left, width):
    import math

    return left + (math.log10(max(value, low)) - math.log10(low)) / (math.log10(high) - math.log10(low)) * width


def fan_chart(full, low) -> str:
    rows = []
    for (label, path, a), (_, _, b) in zip(FULL_FANS, LOW_FANS, strict=True):
        ra, rb = find(full, a), find(low, b)
        if ra and rb:
            rows.append((f"{label}, {'no edge' if 'directly' in path else 'edge'}", ra["fan_p95_ms"], rb["fan_p95_ms"]))
    if not rows:
        return ""
    left, width, top, gap = 150, 580, 26, 44
    low_x, high_x = 0.5, 20000
    ticks = [1, 10, 100, 1000, 10000]
    height = top + gap * len(rows) + 28
    parts = [f'<svg viewBox="0 0 800 {height}" role="img" aria-label="Fan response time at the 95th percentile, 8 cores against 2 cores, logarithmic scale">']
    for t in ticks:
        x = log_x(t, low_x, high_x, left, width)
        parts.append(f'<line class="g" x1="{x:.1f}" x2="{x:.1f}" y1="{top - 8}" y2="{height - 24}"/><text x="{x:.1f}" y="{height - 8}" text-anchor="middle">{t:,} ms</text>')
    for i, (label, a, b) in enumerate(rows):
        y = top + i * gap + 10
        xa, xb = log_x(a, low_x, high_x, left, width), log_x(b, low_x, high_x, left, width)
        parts.append(f'<text x="{left - 12}" y="{y + 4}" text-anchor="end">{label}</text>')
        parts.append(f'<line class="a" x1="{min(xa, xb):.1f}" x2="{max(xa, xb):.1f}" y1="{y}" y2="{y}"/>')
        parts.append(f'<circle class="s1" cx="{xa:.1f}" cy="{y}" r="5.5"/><circle class="s2" cx="{xb:.1f}" cy="{y}" r="5"/>')
        short = lambda v: f"{v:,.0f}" if v >= 10 else f"{v:g}"  # noqa: E731
        value = f"{short(a)} / {short(b)} ms"
        if max(xa, xb) > 620:
            parts.append(f'<text class="v" x="{min(xa, xb) - 11:.1f}" y="{y + 4}" text-anchor="end">{value}</text>')
        else:
            parts.append(f'<text class="v" x="{max(xa, xb) + 11:.1f}" y="{y + 4}">{value}</text>')
    parts.append("</svg>")
    return '<div class="chart"><div class="legend"><span><i class="k1"></i>8 cores</span><span><i class="k2"></i>2 cores, 2 GB</span><span>p95, logarithmic scale. Each pair reads 8 cores / 2 cores.</span></div>' + "".join(parts) + "</div>"


def gate_chart(full, low) -> str:
    def series(rows, pairs):
        out = []
        for req, name in pairs:
            r = find(rows, name)
            if r:
                out.append((req, r["tickets_admitted"] / (r["seconds_at_peak"] + 20)))
        return out

    a, b = series(full, FULL_GATES), series(low, LOW_GATES)
    if not a:
        return ""
    left, top, width, height = 60, 14, 690, 230
    top_value = 1600
    sx = lambda v: left + v / top_value * width  # noqa: E731
    sy = lambda v: top + height - v / 1000 * height  # noqa: E731
    parts = ['<svg viewBox="0 0 800 280" role="img" aria-label="Tickets admitted per second against scans requested, for both machines">']
    for v in (0, 250, 500, 750, 1000):
        parts.append(f'<line class="g" x1="{left}" x2="{left + width}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/><text x="{left - 8}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:,}</text>')
    for v in (0, 300, 600, 1000, 1500):
        parts.append(f'<text x="{sx(v):.1f}" y="{top + height + 18}" text-anchor="middle">{v:,}</text>')
    parts.append(f'<line class="ref" x1="{sx(0):.1f}" y1="{sy(0):.1f}" x2="{sx(1000):.1f}" y2="{sy(1000):.1f}"/>')
    parts.append(f'<text x="{sx(1010):.1f}" y="{sy(1000) + 14:.1f}">every scan admitted</text>')
    for points, line, mark in ((a, "l1", "s1"), (b, "l2", "s2")):
        path = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
        parts.append(f'<polyline class="{line}" points="{path}"/>')
        for x, y in points:
            parts.append(f'<circle class="{mark}" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4.5"/>')
        x, y = points[-1]
        parts.append(f'<text class="v" x="{sx(x) + 9:.1f}" y="{sy(y) + 4:.1f}">{y:,.0f}/s</text>')
    parts.append(f'<text x="{left + width / 2}" y="274" text-anchor="middle">Scans requested per second. Vertical axis: tickets admitted per second</text></svg>')
    return '<div class="chart"><div class="legend"><span><i class="k1"></i>8 cores</span><span><i class="k2"></i>2 cores, 2 GB</span></div>' + "".join(parts) + "</div>"


def parallel_block(parallel) -> str:
    if not parallel:
        return '<p class="caveat">Not measured yet.</p>'
    rows = [
        [str(r["organizers_at_once"]), n(r["rows_each"]), n(r["rows_per_second_all_together"]), f'{r["seconds_including_reconciliation"]:,.0f} s', str(r["discrepancies"]), str(r["cross_organizer_merges"])]
        for r in parallel["results"]
    ]
    return table(["Organizers at once", "Rows each", "Rows per second, all together", "Time", "Discrepancies", "Cross-organizer merges"], rows, {0, 1, 2, 3, 4, 5}) + f'<p class="caveat">{html.escape(parallel["machine"])}. {parallel["measured"]}.</p>'


# ------------------------------------------------------------------ replay


def inline_page(page: str, fonts: dict, statics: Path, boot_marker: str = "<!--REPLAY-BOOT-->") -> str:
    def css(path: str) -> str:
        text = (statics / path).read_text(encoding="utf-8")
        for name, data in fonts.items():
            text = text.replace(f'url("fonts/{name}")', f'url("data:font/woff2;base64,{data}")')
        return text

    def script(path: str) -> str:
        text = (statics / path).read_text(encoding="utf-8")
        text = re.sub(r"location\.href\s*=\s*([^;]+);", r"window.__go(\1);", text)
        return text.replace("</script", "<\\/script")

    page = re.sub(r'<link rel="preload"[^>]*>', "", page)
    page = re.sub(
        r'<link rel="stylesheet" href="/static/console/([\w.-]+)">', lambda m: f"<style>{css(m.group(1))}</style>", page
    )
    page = re.sub(
        r'<script src="/static/console/([\w.-]+)"></script>',
        lambda m: f"<script>{script(m.group(1))}</script>" if m.group(1) != "theme.js" else "",
        page,
    )
    shim = (HERE / "replay_shim.js").read_text(encoding="utf-8")
    page = page.replace("<head>", f"<head>{boot_marker}<script>{shim}</script>", 1)
    page = page.replace('<html lang="en">', '<html lang="en" data-theme="auto">', 1)
    return page


def replay_data(fonts: dict) -> dict | None:
    raw = load("replay.json")
    if not raw:
        return None
    statics = ROOT / "static" / "console"
    pages = {path: inline_page(body, fonts, statics) for path, body in raw["pages"].items()}
    fallback: dict = {}
    steps = []
    run_id = None
    for step in raw["steps"]:
        by_path: dict[str, list] = {}
        for frame in step["frames"]:
            by_path.setdefault(frame["path"], []).append(frame)
        thinned = {}
        for path, frames in by_path.items():
            keep = 7
            chosen = frames if len(frames) <= keep else [frames[round(i * (len(frames) - 1) / (keep - 1))] for i in range(keep)]
            thinned[path] = [{"body": f["body"], "total": f.get("total")} for f in chosen]
            fallback[path] = thinned[path][-1]
        match = re.fullmatch(r"/runs/(\d+)", step["path"])
        if match:
            run_id = int(match.group(1))
        steps.append({"key": step["key"], "title": step["title"], "path": step["path"], "page": step["path"], "note": step["note"], "frames": thinned})
    for step in steps:
        if step["page"] not in pages:
            raise SystemExit(f"No recorded page for {step['page']}")
    return {"steps": steps, "pages": pages, "fallback": fallback, "runId": run_id}


# ------------------------------------------------------------------ page


REQUIREMENTS = [
    ("Efficient, reliable data import and migration pipelines", "Streaming importer: flat memory from 1 to 10 million rows, COPY, resume after a kill, dry run, rollback, reconciliation", "Real data. Production", "#migration"),
    ("High-volume imports in production", "Measured on synthetic data, on a laptop", "<b>Production experience: none</b>", "#measured"),
    ("Moving customers from a legacy CRM", "Per-organizer isolation, go or no-go report, cutover runbook", "A real cutover. Reverse sync after first writes", "#migration"),
    ("Stronger connection to an external ticketing API", "Client, cursor sync, webhooks and outbox, tested against a stand-in with injected failures", "A real API", "#integration"),
    ("Timeouts, retries, idempotency, rate limiting", "All present, in one place, each with tests that bug injection confirms", "A shared retry budget across workers", "#integration"),
    ("Performance and reliability under load", "k6 up to 20,000 fans, gate scans until saturation, two machine sizes, one real bug found and fixed", "Production traffic, a real CDN", "#measured"),
    ("Refactoring without unnecessary rewrites", "A written approach. Decisions driven by profiling", "<b>Refactoring real legacy code: not shown.</b> All of this is new", "#delivery"),
    ("Django architecture", "Apps split by domain, 21 written decisions with costs", "A large existing codebase", "#decisions"),
    ("ORM performance", "Query count tests, query plans, COPY and raw SQL where profiling asked for them", "Production query tuning", "#delivery"),
    ("Background jobs", "Celery with separate queues, late acks, leases, reaper, automatic resume", "Scale beyond a few workers", "#operations"),
    ("Testing strategy", "Real PostgreSQL, killed processes, injected failures, injected bugs", "Real-data tests", "#proof"),
    ("Debugging production issues", "One real investigation, written up: a 4 minute hang traced into Gunicorn", "<b>A real production incident: none</b>", "#incident"),
    ("Senior or lead mindset: drive, mentor, scope", "Decisions and their costs in writing. Limits stated", "<b>Mentoring and leading a team: not shown</b>", "#how-it-was-built"),
    ("Task breakdown, estimates, progress reports", "An example of each", "A track record on a team", "#delivery"),
    ("5 to 7+ years of backend development", "—", "<b>Not met.</b> No commercial Django before this project", "#questions"),
    ("Kubernetes and operations (nice to have)", "Manifests, probes, PDB, HPA, network policy, alerts, runbooks, run on one node with failures injected", "A multi-node production cluster", "#operations"),
    ("MongoDB, Meteor (nice to have)", "Reader for mongoexport output, written from documentation and tested on fixtures", "Experience with a real system", "#migration"),
    ("Node.js, React, Elasticsearch (nice to have)", "A plan for search during migration", "<b>Not shown</b>", "#migration"),
]

NAV = [
    ("Overview", [("start", "Start here"), ("program", "The program"), ("requirements", "Against the job")]),
    ("Evidence", [("measured", "Measured"), ("proof", "Proof it works"), ("how-it-was-built", "How it was built")]),
    ("Design", [("architecture", "Architecture"), ("migration", "Migration playbook"), ("integration", "Ticketing API"), ("live-events", "Fans and gates"), ("decisions", "Decisions")]),
    ("Running it", [("operations", "Operations"), ("security-privacy", "Security and privacy"), ("incident", "Incident review"), ("delivery", "How I would work")]),
    ("Reference", [("code", "All the code"), ("questions", "Questions"), ("sources", "Sources")]),
]


def nav_html() -> str:
    out = []
    for group, items in NAV:
        out.append(f'<li class="grp">{group}</li>')
        out += [f'<li><a href="#{key}">{label}</a></li>' for key, label in items]
    return "".join(out)


def next_links(key: str) -> str:
    flat = [item for _, items in NAV for item in items]
    i = next(index for index, (k, _) in enumerate(flat) if k == key)
    prev_link = f'<a href="#{flat[i - 1][0]}">&larr; {flat[i - 1][1]}</a>' if i else "<span></span>"
    next_link = f'<a href="#{flat[i + 1][0]}">{flat[i + 1][1]} &rarr;</a>' if i + 1 < len(flat) else "<span></span>"
    return f'<div class="next">{prev_link}{next_link}</div>'


def page(key: str, title: str, body: str) -> str:
    return f'<section class="page" id="{key}" data-title="{html.escape(title)}">{body}{next_links(key)}</section>'


def doc_page(key: str, name: str, nav_title: str, files, tests) -> str:
    return page(key, nav_title, f"<h1>{html.escape(lead_of(name))}</h1>" + render_md(read_doc(name), files, tests))


def architecture_svg() -> str:
    return """<div class="diagram"><svg viewBox="0 0 960 470" role="img" aria-label="System diagram. Fans and gates reach Django through the nginx edge. Workers read exports and call the ticketing API. Everything durable is in PostgreSQL.">
<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path class="h" d="M0 0L8 4L0 8Z"/></marker><marker id="ak" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path class="h k" d="M0 0L8 4L0 8Z"/></marker></defs>
<rect class="box" x="20" y="44" width="150" height="50" rx="8"/><text x="95" y="66" text-anchor="middle">Fans</text><text class="s" x="95" y="83" text-anchor="middle">refresh event pages</text>
<rect class="box" x="20" y="124" width="150" height="50" rx="8"/><text x="95" y="146" text-anchor="middle">Gate devices</text><text class="s" x="95" y="163" text-anchor="middle">scans, offline uploads</text>
<rect class="box" x="20" y="204" width="150" height="50" rx="8"/><text x="95" y="226" text-anchor="middle">Operator console</text><text class="s" x="95" y="243" text-anchor="middle">one token</text>
<rect class="box k" x="240" y="84" width="150" height="130" rx="8"/><text x="315" y="130" text-anchor="middle">nginx edge</text><text class="s" x="315" y="150" text-anchor="middle">1 s cache for fan pages</text><text class="s" x="315" y="167" text-anchor="middle">everything else passes</text>
<path class="w k" d="M170 69H205V115H240" marker-end="url(#ak)"/><path class="w" d="M170 149H240" marker-end="url(#ah)"/><path class="w" d="M170 229H205V185H240" marker-end="url(#ah)"/>
<rect class="box" x="460" y="84" width="170" height="130" rx="8"/><text x="545" y="124" text-anchor="middle">Django + DRF</text><text class="s" x="545" y="144" text-anchor="middle">Gunicorn gthread</text><text class="s" x="545" y="161" text-anchor="middle">/metrics, /healthz, /livez</text><text class="s" x="545" y="178" text-anchor="middle">almost no fan traffic</text>
<path class="w" d="M390 149H460" marker-end="url(#ah)"/>
<rect class="store" x="700" y="84" width="240" height="130" rx="8"/><text x="820" y="116" text-anchor="middle">PostgreSQL</text><text class="s" x="820" y="136" text-anchor="middle">contacts, aliases, runs, batches</text><text class="s" x="820" y="153" text-anchor="middle">quarantine, erasure ledger</text><text class="s" x="820" y="170" text-anchor="middle">tickets, outbox, cursor, breaker</text><text class="s" x="820" y="187" text-anchor="middle">all progress is here</text>
<path class="w" d="M630 149H700" marker-end="url(#ah)"/>
<rect class="box k" x="460" y="284" width="170" height="60" rx="8"/><text x="545" y="309" text-anchor="middle">Import workers</text><text class="s" x="545" y="327" text-anchor="middle">queue: imports</text>
<rect class="box" x="460" y="384" width="170" height="60" rx="8"/><text x="545" y="409" text-anchor="middle">Ticketing workers</text><text class="s" x="545" y="427" text-anchor="middle">sync, outbox delivery</text>
<rect class="store" x="240" y="284" width="150" height="60" rx="8"/><text x="315" y="309" text-anchor="middle">Legacy export</text><text class="s" x="315" y="327" text-anchor="middle">CSV, JSONL, mongoexport</text>
<rect class="store" x="240" y="384" width="150" height="60" rx="8"/><text x="315" y="409" text-anchor="middle">Redis</text><text class="s" x="315" y="427" text-anchor="middle">task queue, short cache</text>
<rect class="box" x="700" y="284" width="240" height="60" rx="8"/><text x="820" y="309" text-anchor="middle">Beat and reaper</text><text class="s" x="820" y="327" text-anchor="middle">finds silent runs, resumes them</text>
<rect class="box" x="700" y="384" width="240" height="60" rx="8"/><text x="820" y="409" text-anchor="middle">External ticketing API</text><text class="s" x="820" y="427" text-anchor="middle">a stand-in here, with injected failures</text>
<path class="w" d="M390 314H460" marker-end="url(#ah)"/><path class="w" d="M630 314H665V180H700" marker-end="url(#ah)"/><path class="w" d="M630 414H700" marker-end="url(#ah)"/><path class="w" d="M820 284V214" marker-end="url(#ah)"/><path class="w" d="M630 400H680V200H700" marker-end="url(#ah)"/><path class="w" d="M390 414H460" marker-end="url(#ah)"/><path class="w" d="M820 384H670V344H630" marker-end="url(#ah)" stroke-dasharray="4 3"/>
<text class="s" x="684" y="378">signed webhooks arrive at Django</text>
</svg></div>"""


def import_flow_svg() -> str:
    return """<div class="diagram"><svg viewBox="0 0 960 190" role="img" aria-label="One batch of the import: read rows, validate, then in one transaction write rows, counters and checkpoint. A kill before commit leaves nothing. After the last batch the file is read again.">
<defs><marker id="ai" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path class="h" d="M0 0L8 4L0 8Z"/></marker></defs>
<rect class="box" x="10" y="52" width="130" height="62" rx="8"/><text x="75" y="78" text-anchor="middle">Read 5,000 rows</text><text class="s" x="75" y="96" text-anchor="middle">from a byte offset</text>
<rect class="box" x="175" y="52" width="130" height="62" rx="8"/><text x="240" y="78" text-anchor="middle">Validate</text><text class="s" x="240" y="96" text-anchor="middle">bad rows: quarantine</text>
<rect class="zone" x="330" y="14" width="370" height="138" rx="10"/><text class="s" x="515" y="34" text-anchor="middle">one transaction, so a kill leaves all or nothing</text>
<rect class="box k" x="345" y="52" width="108" height="62" rx="8"/><text x="399" y="88" text-anchor="middle">Rows</text>
<rect class="box k" x="461" y="52" width="108" height="62" rx="8"/><text x="515" y="88" text-anchor="middle">Counters</text>
<rect class="box k" x="577" y="52" width="108" height="62" rx="8"/><text x="631" y="88" text-anchor="middle">Checkpoint</text>
<rect class="box" x="735" y="52" width="100" height="62" rx="8"/><text x="785" y="88" text-anchor="middle">Next batch</text>
<rect class="store" x="860" y="52" width="90" height="62" rx="8"/><text x="905" y="78" text-anchor="middle">Re-read</text><text class="s" x="905" y="96" text-anchor="middle">reconcile</text>
<path class="w" d="M140 83H175" marker-end="url(#ai)"/><path class="w" d="M305 83H345" marker-end="url(#ai)"/><path class="w" d="M685 83H735" marker-end="url(#ai)"/><path class="w" d="M835 83H860" marker-end="url(#ai)"/>
<path class="w" d="M785 114V170H75V114" marker-end="url(#ai)"/><text class="s" x="430" y="184" text-anchor="middle">repeat until the file ends</text>
</svg></div>"""


def data_model() -> str:
    code = (
        "import django,json;django.setup();from django.apps import apps;"
        "print(json.dumps({m._meta.db_table:{'doc':(m.__doc__ or '').split(chr(10))[0] if not (m.__doc__ or '').startswith(m.__name__+'(') else '',"
        "'fields':[(f.name,f.get_internal_type(),bool(f.null),bool(f.unique)) for f in m._meta.concrete_fields]}"
        " for a in ('crm','imports','ticketing') for m in apps.get_app_config(a).get_models()}))"
    )
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = ROOT / ".venv" / "bin" / "python"
    done = subprocess.run(
        [str(python), "-c", code], cwd=ROOT, capture_output=True, text=True, check=True,
        env={**__import__("os").environ, "ENCORE_DEBUG": "1", "DJANGO_SETTINGS_MODULE": "encore.settings"},
    )
    models = json.loads(done.stdout)
    out = []
    for name, model in models.items():
        fields = ", ".join(f"<code>{f}</code>" for f, *_ in model["fields"] if f != "id")
        out.append([f"<code>{name}</code>", html.escape(model["doc"]) or "—", fields])
    return table(["Table", "What it is", "Columns"], out)


def build() -> None:
    tests = all_tests()
    files = project_files()
    file_set = set(files)
    code = code_data(files)
    archive = source_zip(files)
    fonts = {name: base64.b64encode((ROOT / "static/console/fonts" / name).read_bytes()).decode() for name in ("geist.woff2", "geist-mono.woff2")}

    full = load("benchmarks.json", [])
    low = load("benchmarks-low.json", [])
    parallel = load("benchmarks-parallel.json")
    k8s = load("k8s-proof.json", {})
    mutation = load("mutation-results.json")
    evidence = load("test-evidence.json")
    replay = replay_data(fonts)

    video_path = DOCS / "demo-small.webm"
    if not video_path.exists():
        video_path = DOCS / "demo.webm"
    video = base64.b64encode(video_path.read_bytes()).decode()
    hero_shot = base64.b64encode((DOCS / "shots" / "3-completed.png").read_bytes()).decode()

    top20, top10m = find(full, "Refresh spike, 20,000 fans"), None
    for row in full:
        if row.get("rows") == 10_000_000:
            top10m = row
    low20 = find(low, "Low: 20,000 fans")

    sections = []
    # ---- start
    tests_line = ""
    if evidence:
        t = evidence["tests"]
        tests_line = f'{t["passed"]} tests passing on {html.escape(evidence["platform"].split(",")[0])}, against real PostgreSQL. {t["skipped"]} skipped.'
    mutation_line = f'{mutation["caught"]} of {mutation["bugs_injected"]} injected bugs caught.' if mutation else ""
    start = f"""
<div class="hero">
  <p class="kick">ENCORE / PROJECT PACK</p>
  <h1>Moving a CRM and surviving the crowd, shown working</h1>
  <p class="lead">A working demo of the two jobs a live-events platform faces while it modernises: moving customers from a legacy CRM, and staying up against an external ticketing API and thousands of fans at once. It is built with Django, PostgreSQL, Celery and Redis.</p>
  <p class="status"><b>Status:</b> demo on synthetic data. It has never handled real customers or real traffic. AI wrote most of the code, under my direction.</p>
</div>
<div class="grid3">
  <div class="card"><p class="num">{n(top10m.get("rows") if top10m else None)}<small>rows</small></p><p>imported at {n(top10m.get("rows_per_second") if top10m else None)} rows per second with {n(top10m.get("worker_peak_mb") if top10m else None)} MB of worker memory and {n(top10m.get("discrepancies") if top10m else None)} discrepancies.</p></div>
  <div class="card"><p class="num">20,000<small>fans</small></p><p>refreshing one page: {top20.get("failed_percent", "—")}% failed, p95 {ms(top20.get("fan_p95_ms"))}. On a machine two levels smaller: {low20.get("failed_percent", "—")}% failed, p95 {ms(low20.get("fan_p95_ms"))}.</p></div>
  <div class="card"><p class="num">kill -9</p><p>at any moment loses nothing. A real process and a real Kubernetes pod were killed mid-import, then reconciled against the source.</p></div>
</div>
<p class="caveat">Laptop, synthetic data, one run per scenario. Machine details and limits are on <a href="#measured">Measured</a>. {tests_line} {mutation_line}</p>
<h2>Three ways to look at it</h2>
<div class="grid3">
  <a class="card" href="#program"><h3>Click through it, 5 minutes</h3><p>The real console, in this page, replaying a real run. Nothing to install.</p></a>
  <a class="card" href="#program"><h3>Watch it</h3><p>A recording of the whole scenario, with captions.</p></a>
  <a class="card" href="#run"><h3>Run it for real, 10 minutes</h3><p>Save the source from this page, then two commands. You need Docker.</p></a>
</div>
<h2>Where to go, by role</h2>
{table(["If you are", "Start with", "Then"], [
    ["Hiring", '<a href="#requirements">Against the job</a>', '<a href="#how-it-was-built">How it was built</a>, <a href="#questions">Questions</a>'],
    ["CTO or lead", '<a href="#migration">Migration playbook</a>', '<a href="#how-it-was-built">What a reviewer should distrust</a>, <a href="#delivery">First 90 days</a>'],
    ["Backend engineer", '<a href="#code">All the code</a>', '<a href="#decisions">Decisions</a>, <a href="#proof">Bug injection</a>, <a href="#integration">Ticketing API</a>'],
    ["Live events or product", '<a href="#live-events">Fans and gates</a>', '<a href="#migration">Migration playbook</a>'],
    ["Operations", '<a href="#operations">Operations</a>', '<a href="#incident">Incident review</a>, <a href="#proof">Kubernetes run</a>'],
    ["DPO or legal", '<a href="#security-privacy">Security and privacy</a>', '<a href="#questions">Questions</a>'],
])}
<h2>What it does not show</h2>
<p>Years of production experience. It has not met real data, real users or a real on-call night, and it is not ready for real personal data. The <a href="#requirements">requirements table</a> says plainly what is and is not covered, and <a href="#how-it-was-built">How it was built</a> lists where to distrust it.</p>
<h2>Who built it</h2>
<p>Mykhailo Zvir, <a href="mailto:zvirmykhailo1987@gmail.com">zvirmykhailo1987@gmail.com</a>. Degree in information security engineering, Lviv Polytechnic. IT lead at a district court from 2011 to 2015, running infrastructure and document automation. English: intermediate.</p>
<div class="card" style="margin-top:18px;padding:0;overflow:hidden"><img alt="The console after a finished import, with reconciliation showing 0 discrepancies" src="data:image/png;base64,{hero_shot}" style="display:block;width:100%;height:auto"></div>
"""
    sections.append(page("start", "Start here", start))

    # ---- program
    player = f"""
<h1>The program</h1>
<p class="lead">This is the real operator console: its own pages, styles and scripts. It is not connected to a server. It plays back replies recorded from a real run, so the buttons that change something move to the next step instead.</p>
<div class="player">
  <div class="steps" id="steps" role="group" aria-label="Steps of the demo"></div>
  <div class="note-bar"><p id="step-note"></p><button class="btn p" id="step-next" type="button">Next step</button></div>
  <iframe class="app" id="app" title="Encore console, replaying a recorded run"></iframe>
</div>
<p class="caveat">Recorded from the real stack by <a href="#code" data-file="scripts/record_demo.py"><code>scripts/record_demo.py</code></a>, which stops with an error if a guarantee does not hold.</p>
<h2>The recording</h2>
<video id="demo-video" controls preload="metadata"></video>
<h2 id="run">Run it for real</h2>
<p>You need Docker with Compose. Nothing leaves your machine, and all data is generated locally.</p>
<p><button class="btn p" type="button" data-save="zip-data" data-name="encore-source.zip" data-type="application/zip">Save the source code (.zip, {len(archive) // 1024:,} KB)</button></p>
<p>Unpack it, and open a terminal in the <code>encore</code> folder.</p>
<pre class="cmd"><code>export ENCORE_DEBUG=1</code></pre>
<pre class="cmd"><code>docker compose up -d --build --wait</code></pre>
<p>In PowerShell, the first line is <code>$env:ENCORE_DEBUG=1</code>. Then open <a href="http://localhost:8000" target="_blank" rel="noreferrer">localhost:8000</a>. Make a file for the start form:</p>
<pre class="cmd"><code>docker compose exec worker python manage.py gen_legacy --rows 300000 --out data/demo_contacts.csv</code></pre>
<p>Start an import in the console, and while it runs:</p>
<pre class="cmd"><code>docker compose kill -s SIGKILL worker</code></pre>
<pre class="cmd"><code>docker compose up -d worker</code></pre>
<p>After about 30 seconds the run turns to interrupted. Press Resume. When it finishes, the reconciliation panel should show 0 discrepancies. Then open Integration and switch on failures.</p>
<h3>Check my claims yourself</h3>
{table(["To check", "Command", "Time"], [
    ["The tests", "<code>python -m venv .venv</code>, <code>.venv/bin/pip install -r requirements-dev.lock</code>, <code>docker compose up -d db redis</code>, <code>ENCORE_DEBUG=1 .venv/bin/pytest</code>", "A few minutes"],
    ["The whole scenario, by script", "<code>python scripts/demo_scenario.py</code>", "A few minutes"],
    ["The tests catch bugs", "<code>python scripts/mutation_check.py</code>", "About 30 minutes"],
    ["Kubernetes", "<code>python scripts/k8s_proof.py</code>", "10 to 20 minutes"],
    ["Every load number", "<code>bash scripts/all_measurements.sh</code>", "About 2 hours"],
    ["CI, on Linux", "<code>python scripts/evidence.py</code>", "A few minutes"],
])}
<p class="caveat">The measurement scripts wipe the demo tables of the local stack.</p>
"""
    sections.append(page("program", "The program", player))

    # ---- requirements
    req = table(["The job asks for", "Shown in this demo", "Not shown"], [[r, s, f'{x}<br><a href="{link}">Details</a>'] for r, s, x, link in REQUIREMENTS])
    sections.append(page("requirements", "Against the job", f"""
<h1>Against the job</h1>
<p class="lead">Each requirement from the job description, what this demo shows for it, and what it does not. Everything in the middle column ran on synthetic data, on a laptop.</p>
{req}
"""))

    # ---- measured
    imports = sorted((r for r in full if r.get("rows")), key=lambda r: r["rows"])
    import_rows = [[n(r["rows"]), f'{float(r["file_mb"]):,.0f} MB' if r.get("file_mb") else "—", f'{r["seconds_total"] / 60:,.1f} min', n(r.get("rows_per_second")), f'{r.get("worker_peak_mb")} MB', n(r.get("quarantined")), n(r.get("discrepancies"))] for r in imports]
    low_import = [r for r in low if r.get("rows")]
    low_import_rows = [[n(r["rows"]), f'{r["seconds_total"] / 60:,.1f} min', n(r.get("rows_per_second")), f'{r.get("worker_peak_mb")} MB', n(r.get("discrepancies"))] for r in low_import]
    machine = next((r.get("machine") for r in full if r.get("machine")), "—")
    best_speed = max((r["rows_per_second_all_together"] for r in parallel["results"]), default=4000) if parallel else (top10m or {}).get("rows_per_second", 4000)
    top_rate = max((r.get("requests_per_second", 0) for r in full), default=0)
    fans_head = ["Fans at once", "Path", "Requests per second", "Failed", "p95", "p99", "From cache"]
    gates_head = ["Scans requested per second", "Tickets admitted per second", "Scans failed", "p50", "p95", "p99", "Tickets = outbox"]
    measured = f"""
<h1>Measured</h1>
<p class="lead">One run per scenario, on a laptop, on synthetic data, on {html.escape(str(next((r.get("measured") for r in full if r.get("measured")), "—")))}. These numbers show behaviour, not production capacity. The load generator shares the machine with the system. A first attempt ran while another program was rendering video on the same laptop, so it was discarded and repeated. Your numbers will differ, and every one has a script behind it.</p>
<h2>Import</h2>
{table(["Rows", "File", "Time", "Rows per second", "Worker memory, highest sample", "Quarantined", "Discrepancies"], import_rows, {0, 1, 2, 3, 4, 5, 6})}
<p class="caveat">{html.escape(str(machine))}. Includes hashing and full reconciliation. The generator breaks about 2% of rows on purpose. Ten times the data needed about the same memory, because the file is streamed.</p>
<h3>Organizers side by side</h3>
<p>One writer per organizer, so organizers can run in parallel until the one database becomes the limit.</p>
{parallel_block(parallel)}
<h2>Fans refreshing one page</h2>
<p>Each fan refreshes about every 2 seconds, for 60 seconds at the peak, asking for compressed replies. The edge is nginx with a one second cache.</p>
{fan_chart(full, low)}
<h3>8 cores, 7 GB</h3>
{table(fans_head, fan_rows(full, FULL_FANS), {0, 2, 3, 4, 5, 6})}
<h3>Two levels smaller: 2 cores, 2 GB in total</h3>
{table(fans_head, fan_rows(low, LOW_FANS), {0, 2, 3, 4, 5, 6})}
<p>Without the edge, the small machine fails. With it, the small machine holds. That is why the edge is in the design. <b>Django stopped in the middle of a spike, 5,000 fans:</b> {find(full, "Django stopped during a spike, 5,000 fans").get("failed_percent", "—")}% failed on 8 cores and {find(low, "Low: Django stopped, 5,000 fans").get("failed_percent", "—")}% on 2 cores, because the edge keeps the last known page.</p>
<h2>Gates scanning while 10,000 fans refresh</h2>
<p>Scans cannot be cached, so this is the real write limit of this setup.</p>
{gate_chart(full, low)}
<h3>8 cores, 7 GB</h3>
{table(gates_head, gate_rows(full, FULL_GATES), {0, 1, 2, 3, 4, 5})}
<h3>2 cores, 2 GB</h3>
{table(gates_head, gate_rows(low, LOW_GATES), {0, 1, 2, 3, 4, 5})}
<p class="caveat">Past the saturation point scans queue, and nothing is lost: admitted tickets match outbox messages. The few failed scans were not investigated.</p>
<h2>Import on the small machine</h2>
{table(["Rows", "Time", "Rows per second", "Worker memory", "Discrepancies"], low_import_rows, {0, 1, 2, 3, 4})}
<p class="caveat">Disk and network were not limited, so a slow disk would change this. The 10 million row import was not repeated here.</p>
<h2>Do the arithmetic for your numbers</h2>
<p>Plain arithmetic on the measured speed, not a forecast. Real rows and real hardware will differ.</p>
<div class="card"><h3>How long would an import take?</h3><div class="calc">
<label>Rows to import<input class="inp" id="c-rows" type="number" min="0" value="10000000"></label>
<label>Rows per second<input class="inp" id="c-speed" type="number" min="1" value="{best_speed:.0f}"></label>
<span></span><div><label>Time</label><output id="c-window"></output></div></div></div>
<div class="card" style="margin-top:12px"><h3>What does a crowd ask of the edge?</h3><div class="calc">
<label>Fans at once<input class="inp" id="c-fans" type="number" min="0" value="20000"></label>
<label>Seconds between refreshes<input class="inp" id="c-every" type="number" min="0.1" step="0.5" value="2"></label>
<span></span><div><label>Load</label><output id="c-rate" data-top="{top_rate}"></output><p class="caveat" id="c-rate-note" style="margin:4px 0 0"></p></div></div></div>
"""
    sections.append(page("measured", "Measured", measured))

    # ---- proof
    proof = ["<h1>Proof it works</h1>", '<p class="lead">Passing tests only say the tests pass. So bugs were injected on purpose to see if the tests notice, and real things were killed in a real cluster.</p>']
    if evidence:
        t = evidence["tests"]
        proof.append(f"""<div class="grid3">
<div class="card"><p class="num">{t["passed"]}<small>of {t["total"]}</small></p><p>tests passed, {t["failed"]} failed, {t["skipped"]} skipped, in {html.escape(evidence["platform"])}. Real PostgreSQL, {evidence["measured"]}.</p></div>
<div class="card"><p class="num">{evidence["coverage_percent"]}%<small>lines</small></p><p>Coverage only says a line ran. It is here because people ask. The bug injection below says more.</p></div>
<div class="card"><p class="num">{"clean" if evidence["audit_clean"] else "findings"}</p><p>pip-audit on the pinned dependencies. ruff {"clean" if evidence["checks"]["ruff"] else "failed"}, mypy {"clean" if evidence["checks"]["mypy"] else "failed"}, migrations {"up to date" if evidence["checks"]["migrations"] else "out of date"}.</p></div></div>""")
    if mutation:
        rows = []
        for row in mutation["results"]:
            caught = row["caught_by"].split("::")[-1] if row["caught_by"] else ""
            target = tests.get(caught)
            link = f'<a href="#code" data-file="{target}" data-find="def {caught}"><code>{caught}</code></a>' if target else f"<code>{html.escape(row['caught_by'] or '')}</code>"
            rows.append([html.escape(row["bug"]), f'<code>{row["file"]}</code>', link if row["caught"] else '<span class="chip no">Not caught</span>'])
        proof.append(f'<h2>Bugs injected on purpose</h2><p>Each row removes one guarantee from a copy of the code, then runs the whole test suite. <b>{mutation["caught"]} of {mutation["bugs_injected"]} were caught.</b> The first failing test is named.</p>' + table(["The bug", "File", "Caught by"], rows))
    if k8s.get("worker_killed"):
        w, r = k8s["worker_killed"], k8s.get("rolling_restart_under_traffic", {})
        proof.append(f"""<h2>A real Kubernetes run</h2>
<p>{html.escape(k8s["cluster"])}, {k8s["measured"]}. One node, not production. <a href="#code" data-file="scripts/k8s_proof.py"><code>scripts/k8s_proof.py</code></a> does all of this and writes <a href="#code" data-file="docs/k8s-proof.json"><code>docs/k8s-proof.json</code></a>.</p>""" + table(["Scenario", "Result"], [
            ["Deploy from nothing, with migrations", f'{k8s.get("deploy_seconds")} s'],
            [f'Import worker pod force-deleted at row {n(w["checkpoint_row_when_killed"])} of {n(w["rows_in_file"])}', f'Finished by itself {w["seconds_until_finished_after_kill"]} s later, after {w["resume_count"]} automatic resume. {n(w["rows_read"])} rows read, {w["discrepancies"]} discrepancies'],
            [f'Django rolling restart under traffic, {n(r.get("requests"))} requests', f'{r.get("failed")} failed. Replies: {html.escape(json.dumps(r.get("replies")))}'],
            ["Metrics from inside", "Reachable" if k8s.get("metrics_from_inside") else "Not reachable"],
            ["Metrics through the public edge", html.escape(k8s.get("metrics_through_public_edge", "—"))],
            ["From an unrelated pod", html.escape(k8s.get("reachable_from_unrelated_pod", "—"))],
            ["From a web pod", html.escape(k8s.get("reachable_from_web_pod", "—"))],
        ]))
    if evidence:
        by_file: dict[str, list] = {}
        for case in evidence["cases"]:
            by_file.setdefault(case["file"], []).append(case)
        items = []
        for file, cases in sorted(by_file.items()):
            lines = "".join(
                f'<li><a href="#code" data-file="{file}" data-find="def {c["name"].split("[")[0]}">{html.escape(c["name"])}</a>{" (skipped)" if c["state"] == "skipped" else ""}{" <b>FAILED</b>" if c["state"] == "failed" else ""}</li>'
                for c in cases
            )
            items.append(f'<details class="q"><summary>{file}: {len(cases)} tests</summary><div class="a"><ul style="font-size:13.5px">{lines}</ul></div></details>')
        proof.append("<h2>Every test</h2>" + "".join(items))
    sections.append(page("proof", "Proof it works", "".join(proof)))

    sections.append(doc_page("how-it-was-built", "how-it-was-built.md", "How it was built", file_set, tests))

    # ---- architecture
    arch = f"""
<h1>Architecture</h1>
<p class="lead">Two paths share one database. Reads that crowds repeat stop at the edge. Everything that matters is in PostgreSQL, so any process can die.</p>
{architecture_svg()}
<h2>One batch of the import</h2>
{import_flow_svg()}
<h2>What runs where</h2>
{table(["Folder", "What it does"], [
    ["<code>imports/</code>", "Streaming readers, validation, batch pipeline, reconciliation, rollback, go or no-go"],
    ["<code>ticketing/</code>", "Reliability primitives, API client, cursor sync, webhooks, gate scans, offline uploads, outbox"],
    ["<code>crm/</code>", "Contacts per organizer, legacy aliases, erasure ledger"],
    ["<code>console/</code>", "The operator screens"],
    ["<code>mock_api/</code>", "The stand-in ticketing API, with failures you can switch on"],
    ["<code>deploy/</code>", "nginx, Gunicorn and the Kubernetes manifests"],
    ["<code>scripts/</code>", "Demo scenario, measurements, bug injection, this pack"],
])}
<h2>Data model</h2>
<p>Generated from the Django models when this page was built.</p>
{data_model()}
<h2>The API</h2>
<p>Described in <a href="#code" data-file="docs/openapi.yaml"><code>docs/openapi.yaml</code></a>.</p>
"""
    sections.append(page("architecture", "Architecture", arch))
    sections.append(doc_page("migration", "migration-playbook.md", "Migration playbook", file_set, tests))
    sections.append(doc_page("integration", "integration-contract.md", "Ticketing API", file_set, tests))
    sections.append(doc_page("live-events", "live-events.md", "Fans and gates", file_set, tests))
    sections.append(doc_page("decisions", "decisions.md", "Decisions", file_set, tests))
    sections.append(doc_page("operations", "operations.md", "Operations", file_set, tests))
    sections.append(doc_page("security-privacy", "security-privacy.md", "Security and privacy", file_set, tests))
    sections.append(doc_page("incident", "incident-gunicorn.md", "Incident review", file_set, tests))
    sections.append(doc_page("delivery", "delivery.md", "How I would work", file_set, tests))

    # ---- code
    code_page = f"""
<h1>All the code</h1>
<p class="lead">Every file of the project, {len(code)} of them, readable here. Generated data, the video and fonts are left out.</p>
<p><button class="btn p" type="button" data-save="zip-data" data-name="encore-source.zip" data-type="application/zip">Save the source code (.zip)</button></p>
<label for="code-search">Search all files</label>
<input class="search" id="code-search" type="search" placeholder="For example: select_for_update" autocomplete="off">
<div class="results" id="code-results" hidden></div>
<div class="code-wrap">
  <nav class="tree" id="tree" aria-label="Files"></nav>
  <div class="viewer"><div class="vh"><b id="v-path"></b><span id="v-info"></span></div><div class="vbody" id="v-body" tabindex="0"></div></div>
</div>
"""
    sections.append(page("code", "All the code", code_page))

    # ---- questions
    questions = parse_questions(file_set, tests)
    groups = list(dict.fromkeys(q["group"] for q in questions))
    q_filters = '<button type="button" data-group="All" aria-pressed="true">All</button>' + "".join(f'<button type="button" data-group="{g}" aria-pressed="false">{g}</button>' for g in groups)
    q_items = "".join(
        f'<details class="q" data-group="{q["group"]}"><summary>{html.escape(q["q"])}</summary><div class="a">{q["a"]}</div></details>'
        for q in questions
    )
    sections.append(page("questions", "Questions", f"""
<h1>Questions you may have</h1>
<p class="lead">The questions a recruiter, a CTO, an engineer, operations and a DPO are likely to ask, each answered plainly, including when the answer is no.</p>
<input class="search" id="q-search" type="search" placeholder="Search the questions" aria-label="Search the questions" autocomplete="off">
<div class="filters" id="q-filters">{q_filters}</div>
<p class="caveat" id="q-count"></p>
{q_items}
<div class="empty" id="q-empty" hidden>Nothing found. Try another word, or choose All.</div>
"""))

    # ---- sources
    names = ["migration-playbook.md", "live-events.md", "integration-contract.md", "operations.md", "security-privacy.md", "delivery.md", "how-it-was-built.md", "decisions.md"]
    sources = external_sources(names)
    rows = [[f'<a rel="noreferrer" target="_blank" href="{html.escape(u)}">{html.escape(label)}</a>', html.escape(where)] for label, u, where in sources]
    sections.append(page("sources", "Sources", f"""
<h1>Sources</h1>
<p class="lead">Where the practice in this pack comes from. All were checked on 7 November 2026. These links need internet, and nothing else in this page does.</p>
<p>Standards and vendor pages can change. Where I drew my own conclusion from them, the text says "my reading". No lawyer has reviewed the legal parts.</p>
{table(["Source", "Used in"], rows)}
"""))

    css = (HERE / "pack.css").read_text(encoding="utf-8")
    for name, data in fonts.items():
        family = "Geist Mono" if "mono" in name else "Geist"
        css = f'@font-face{{font-family:"{family}";font-weight:100 900;font-display:swap;src:url("data:font/woff2;base64,{data}") format("woff2")}}\n' + css
    js = (HERE / "pack.js").read_text(encoding="utf-8")
    safe = lambda value: json.dumps(value, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "\\u003c!--")  # noqa: E731
    page_html = f"""<!doctype html>
<html lang="{LANG}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Encore project pack</title>
<meta name="description" content="A working demo of CRM migration and a resilient ticketing integration: the program, the code and all the material, in one file.">
<style>{css}</style>
</head>
<body>
<a class="skip" href="#start">Skip to content</a>
<div class="shell">
<aside class="rail">
  <a class="brand" href="#start">{BRAND}Encore</a>
  <p class="tag">Project pack. Everything here works with no internet.</p>
  <nav aria-label="Sections"><ul class="nav">{nav_html()}</ul></nav>
  <div class="rail-foot"><button class="ib" id="theme" type="button" aria-label="Switch between light and dark theme"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/></svg></button><span>Synthetic data. Not production.</span></div>
</aside>
<main id="main">
{"".join(sections)}
</main>
</div>
<script type="application/json" id="replay-data">{safe(replay or {"steps": [], "pages": {}})}</script>
<script type="application/json" id="code-data">{safe(code)}</script>
<script type="text/plain" id="zip-data">{base64.b64encode(archive).decode()}</script>
<script type="text/plain" id="video-data">{video}</script>
<script>{js}</script>
</body>
</html>
"""
    if POSTPROCESS:
        page_html = POSTPROCESS(page_html)
    # Long base64 runs are files, and contain every short word by chance.
    lowered = re.sub(r"[A-Za-z0-9+/=]{200,}", "", page_html).lower()
    for word in FORBIDDEN:
        if word in lowered:
            start_at = lowered.index(word)
            raise SystemExit(f"Forbidden word {word!r}: ...{lowered[max(0, start_at - 80):start_at + 60]}...")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page_html, encoding="utf-8", newline="\n")
    print(f"{OUT.name}: {OUT.stat().st_size / 1024 / 1024:.1f} MB; code {len(code)} files; zip {len(archive) // 1024} KB; video {len(video) * 3 // 4 // 1024} KB; {len(questions)} questions; {len(sources)} sources")


if __name__ == "__main__":
    build()
