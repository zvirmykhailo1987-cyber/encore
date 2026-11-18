"""Record query plans for the heaviest paths, on whatever the database holds now. Writes docs/query-plans.txt.

Run right after a large import, so the tables are full.
"""

import datetime as dt
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUERIES = [
    ("Find contacts by email, once per batch: 5,000 emails at most in real use", """
        SELECT * FROM crm_contact WHERE organizer = 'demo' AND email = ANY(
          ARRAY(SELECT email FROM crm_contact WHERE email IS NOT NULL ORDER BY id DESC LIMIT 5000));"""),
    ("Find aliases by legacy id, once per batch", """
        SELECT a.*, c.* FROM crm_contactalias a JOIN crm_contact c ON c.id = a.contact_id
        WHERE a.organizer = 'demo' AND a.src_uid = ANY(
          ARRAY(SELECT src_uid FROM crm_contactalias ORDER BY id DESC LIMIT 5000));"""),
    ("Quarantine page, deep in the list", """
        SELECT * FROM imports_quarantinerow WHERE run_id = (SELECT max(id) FROM imports_importrun)
        ORDER BY row_number LIMIT 200 OFFSET 100000;"""),
    ("Quarantine summary on the run screen", """
        SELECT reason, count(*) FROM imports_quarantinerow
        WHERE run_id = (SELECT max(id) FROM imports_importrun) AND status = 'open' GROUP BY reason;"""),
    ("Contacts per organizer, for the go or no-go report", """
        SELECT count(*) FROM crm_contact WHERE organizer = 'demo';"""),
    ("Opt-ins without proof, for the go or no-go report", """
        SELECT count(*) FROM crm_contact
        WHERE organizer = 'demo' AND marketing_opt_in AND consent_recorded_at IS NULL;"""),
]


def psql(statement: str) -> str:
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "encore", "-tA", "-c", statement],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout


def main() -> None:
    rows = psql("select count(*) from crm_contact").strip()
    lines = [f"Query plans, {dt.date.today().isoformat()}, on {int(rows):,} contacts. Synthetic data, laptop.", ""]
    for title, sql in QUERIES:
        lines += [f"=== {title}", " ".join(sql.split()), "", psql(f"EXPLAIN (ANALYZE, BUFFERS) {sql}").strip(), ""]
    (ROOT / "docs" / "query-plans.txt").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print("\n".join(lines)[:3000])


if __name__ == "__main__":
    main()
