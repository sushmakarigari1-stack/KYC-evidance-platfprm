"""Shared helpers: config loading, dates, deterministic RNG streams, ID formats."""
from __future__ import annotations

import calendar
import json
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def load_policy() -> dict:
    return json.loads((CONFIG_DIR / "policy.json").read_text(encoding="utf-8"))


def load_generation_cfg() -> dict:
    return json.loads((CONFIG_DIR / "generation.json").read_text(encoding="utf-8"))


def parse_date(s):
    return date.fromisoformat(s) if isinstance(s, str) else s


def iso(d):
    return d.isoformat() if d is not None else None


def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def rng_for(seed: int, stream: str) -> random.Random:
    """Independent, reproducible RNG stream per generation phase.

    Seeding with a *string* uses SHA-512 internally, so it does not depend on
    PYTHONHASHSEED and is stable across runs, platforms and Python versions.
    """
    return random.Random(f"{seed}:{stream}")


def rand_date(r: random.Random, lo: date, hi: date) -> date:
    if hi < lo:
        return lo
    return lo + timedelta(days=r.randint(0, (hi - lo).days))


# ---------------------------------------------------------------- ID formats
class Ids:
    """Deterministic, zero-padded, immutable identifiers (brief section 'ID / Nomenclature')."""

    def __init__(self, year: int):
        self.year = year

    def customer(self, n):          return f"CUS-{n:06d}"
    def kyc_requirement(self, n):   return f"KYC-{self.year}-{n:06d}"
    def document(self, cid, n):     return f"DOC-{cid}-{n:03d}"
    def review(self, cid, year):    return f"REV-{cid}-{year}"
    def alert(self, n):             return f"ALT-{self.year}-{n:06d}"
    def audit(self, n):             return f"AUD-{self.year}-{n:06d}"
    def verification(self, n):      return f"VER-{n:06d}"
    def risk_rating(self, n):       return f"RSK-{n:06d}"
    def owner(self, n):             return f"BO-{n:06d}"
    def ownership_link(self, n):    return f"OWN-{n:06d}"
    def decision(self, n):          return f"DEC-{n:06d}"
    def review_event(self, n):      return f"RVE-{n:06d}"
    def alert_event(self, n):       return f"ALE-{n:06d}"
    def ground_truth(self, n):      return f"GT-{n:06d}"
    def finding(self, n):           return f"VFN-{n:06d}"


ID_REGEX = {
    "customer": r"^CUS-\d{6}$",
    "kyc_requirement": r"^KYC-\d{4}-\d{6}$",
    "document": r"^DOC-CUS-\d{6}-\d{3}$",
    "review_case": r"^REV-CUS-\d{6}-\d{4}$",
    "alert": r"^ALT-\d{4}-\d{6}$",
    "audit_event": r"^AUD-\d{4}-\d{6}$",
    "verification": r"^VER-\d{6}$",
    "risk_rating": r"^RSK-\d{6}$",
    "beneficial_owner": r"^BO-\d{6}$",
    "ownership_link": r"^OWN-\d{6}$",
    "customer_decision": r"^DEC-\d{6}$",
    "review_case_event": r"^RVE-\d{6}$",
    "alert_event": r"^ALE-\d{6}$",
}
