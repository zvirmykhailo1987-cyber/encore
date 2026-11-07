"""Generate a synthetic legacy CRM export with known defects and a manifest of what was injected."""

import csv
import json
import random
from pathlib import Path

from django.core.management.base import BaseCommand

FIRST = "Anna Marco Ivan Sofia Luca Olena Peter Maria Jan Eva Tomas Clara Noah Lena Pavlo Giulia".split()
LAST = "Rossi Novak Kovalenko Schmidt Bianchi Wozniak Meyer Horvat Dubois Costa Silva Ferrari Bauer".split()
TOWNS = ["Krakow", "Milano", "Lviv", "Wien", "Praha", "Berlin", "Gdansk", "Torino", "", "Bologna"]
HEADER = [
    "CUST_ID",
    "EMAIL_ADDR",
    "MOBILE",
    "FNAME",
    "LNAME",
    "TOWN",
    "OPT_IN",
    "CREATED",
    "LOYALTY_TIER_OLD",
]


def generate(path: Path, rows: int, seed: int = 7, dirty: float = 0.02, duplicate: float = 0.03) -> dict:
    rng = random.Random(seed)
    manifest: dict = {"rows": rows, "seed": seed, "injected": {}}
    injected = manifest["injected"]

    def count(name: str) -> None:
        injected[name] = injected.get(name, 0) + 1

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for index in range(1, rows + 1):
            first = rng.choice(FIRST)
            last = rng.choice(LAST)
            email: str = f"{first}.{last}.{index}@example.test".lower()
            mobile = f"+39{rng.randint(300000000, 399999999)}"
            town = rng.choice(TOWNS)
            opt_in = rng.choice(["Y", "N", "1", "0", ""])
            created = f"{rng.randint(2012, 2024)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
            legacy_id = f"C{index:09d}"
            roll = rng.random()
            if roll < dirty:
                defect = rng.choice(
                    ["invalid_email", "bad_date", "missing_id", "invalid_opt_in", "no_channel"]
                )
                if defect == "invalid_email":
                    email = "not-an-email"
                elif defect == "bad_date":
                    created = "31/31/2019"
                elif defect == "missing_id":
                    legacy_id = ""
                elif defect == "invalid_opt_in":
                    opt_in = "maybe"
                else:
                    email = ""
                    mobile = ""
                count(defect)
            elif roll < dirty + duplicate and index > 1:
                other = rng.randint(1, index - 1)
                email = f"DUP.{other}@EXAMPLE.TEST"
                count("shared_email")
            if index % 1000 == 0:
                town = 'Milano,\nCentro "Storico"'
                count("quoted_multiline")
            writer.writerow([legacy_id, email, mobile, first, last, town, opt_in, created, rng.choice("ABC")])
    return manifest


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser) -> None:
        parser.add_argument("--rows", type=int, default=100_000)
        parser.add_argument("--out", default="data/legacy_contacts.csv")
        parser.add_argument("--seed", type=int, default=7)

    def handle(self, *args, **options) -> None:
        path = Path(options["out"])
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = generate(path, options["rows"], options["seed"])
        path.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps(manifest))
