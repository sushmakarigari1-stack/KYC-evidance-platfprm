"""Documented business rules, shared by the generator AND the validator.

Everything that is *derived* (verification outcome, document status, completeness,
decision eligibility, expiry alerts, overdue-review alerts) is computed here from
source records.  The generator never assigns those outcomes independently.

Date convention (design doc BR): a document is *expired* when expiry_date < date;
expiring today (expiry_date == date) is still valid.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

_SUFFIX = {"pvt": "private", "ltd": "limited", "co": "company", "corp": "corporation", "llp": "llp"}


def normalize_name(name: str) -> str:
    """Case/punctuation/whitespace-insensitive comparison with corporate-suffix synonyms."""
    s = re.sub(r"[^a-z0-9 ]+", " ", name.casefold())
    return " ".join(_SUFFIX.get(tok, tok) for tok in s.split())


# ---------------------------------------------------------------- verification
def verification_outcome(doc: dict, legal_name: str, vcfg: dict):
    """Deterministic verification rule. Returns (outcome, reason_code)."""
    if doc["expiry_date"] is not None and doc["expiry_date"] < doc["submitted_on"]:
        return "REJECTED", "EXPIRED_AT_SUBMISSION"
    if normalize_name(doc["name_on_document"]) != normalize_name(legal_name):
        return "REJECTED", "NAME_MISMATCH"
    if doc["scan_quality_score"] < vcfg["min_scan_quality"]:
        return "REJECTED", "ILLEGIBLE_SCAN"
    return "VERIFIED", "OK"


def latest_verification(vers, as_of: date):
    """Latest attempt on or before `as_of` (ordered by date then attempt_no)."""
    best = None
    for v in vers:
        if v["verified_on"] <= as_of and (best is None or (v["verified_on"], v["attempt_no"]) > (best["verified_on"], best["attempt_no"])):
            best = v
    return best


def derive_document_status(doc, vers, successor_verified: bool, as_of: date) -> str:
    lv = latest_verification(vers, as_of)
    if lv is None or lv["outcome"] == "INCONCLUSIVE":
        return "PENDING_VERIFICATION"
    if lv["outcome"] == "REJECTED":
        return "REJECTED"
    if successor_verified:
        return "SUPERSEDED"
    if doc["expiry_date"] is not None and doc["expiry_date"] < as_of:
        return "EXPIRED"
    return "VALID"


def superseded_verified_ids(docs_by_id: dict, vers_by_doc: dict, as_of: date) -> set:
    """Documents that have a *verified* successor (as of `as_of`)."""
    out = set()
    for d in docs_by_id.values():
        sup = d.get("supersedes_document_id")
        if sup:
            lv = latest_verification(vers_by_doc.get(d["document_id"], []), as_of)
            if lv and lv["outcome"] == "VERIFIED":
                out.add(sup)
    return out


# ------------------------------------------------------------------ completeness
def risk_at(ratings, t: date) -> str:
    cur = None
    for r in ratings:  # sorted by effective_from
        if r["effective_from"] <= t and (r["effective_to"] is None or r["effective_to"] > t):
            cur = r
    if cur is None:  # t before first rating -> first rating applies
        cur = ratings[0]
    return cur["risk_level"]


def build_requirement_index(kyc_rows, t: date):
    """{(customer_type, risk): [document types in stable order]} for rules effective on t."""
    idx = {}
    for r in kyc_rows:
        if r["mandatory"] and r["effective_from"] <= t and (r["effective_to"] is None or r["effective_to"] >= t):
            idx.setdefault((r["customer_type_id"], r["risk_level"]), []).append(r["document_type_code"])
    return idx


def ownership_state(links, t: date):
    """(sum_in_basis_points, count) of links effective on t."""
    bps, n = 0, 0
    for l in links:
        if l["effective_from"] <= t and (l["effective_to"] is None or l["effective_to"] > t):
            bps += int(round(l["ownership_pct"] * 100))
            n += 1
    return bps, n


def evaluate(cust, ratings, docs, vers_by_doc, links, req_rows, t: date, requires_owner: bool):
    """Completeness of a customer's KYC file on date `t`, from source records only."""
    risk = risk_at(ratings, t)
    req_types = build_requirement_index(req_rows, t).get((cust["customer_type_id"], risk), [])
    reasons, doc_sat = [], 0
    for rtype in req_types:
        qualifying = False
        newest = None
        for d in docs:
            if d["document_type_code"] != rtype or d["submitted_on"] > t:
                continue
            if newest is None or d["submitted_on"] > newest["submitted_on"]:
                newest = d
            lv = latest_verification(vers_by_doc.get(d["document_id"], []), t)
            if lv and lv["outcome"] == "VERIFIED" and (d["expiry_date"] is None or d["expiry_date"] >= t):
                qualifying = True
                break
        if qualifying:
            doc_sat += 1
            continue
        if newest is None:
            reasons.append(f"MISSING_DOC:{rtype}")
        else:
            lv = latest_verification(vers_by_doc.get(newest["document_id"], []), t)
            if lv is None or lv["outcome"] == "INCONCLUSIVE":
                reasons.append(f"PENDING_VERIFICATION:{rtype}")
            elif lv["outcome"] == "REJECTED":
                reasons.append(f"REJECTED_DOC:{rtype}")
            else:
                reasons.append(f"EXPIRED_DOC:{rtype}")
    required = len(req_types)
    satisfied = doc_sat
    owner_ok = True
    if requires_owner:
        required += 1
        bps, n = ownership_state(links, t)
        if n == 0:
            owner_ok = False
            reasons.append("OWNERSHIP_MISSING")
        elif bps != 10000:
            owner_ok = False
            reasons.append("OWNERSHIP_TOTAL_NOT_100")
        else:
            satisfied += 1
    pct = round(100.0 * satisfied / required, 2) if required else 100.0
    return {
        "risk_level": risk, "required_count": required, "satisfied_count": satisfied,
        "completeness_pct": pct, "reason_codes": "|".join(reasons),
        "complete": satisfied == required, "doc_complete": doc_sat == len(req_types),
        "owner_ok": owner_ok,
    }


# --------------------------------------------------------------------- alerts
def derive_alerts(docs, vers_by_doc, reviews, alert_cfg, as_of: date):
    """Expected alerts from source records + configured lead times (used by generator AND validator).

    Returns dicts with: dedupe_key, alert_type, customer_id, document_id, review_id,
    lead_days, trigger_date, closure ('OPEN_LIKE'|'RESOLVED'|'SUPERSEDED'), closed_on.
    """
    leads = sorted(alert_cfg["expiry_lead_days"])
    docs_by_id = {d["document_id"]: d for d in docs}
    succ_verified_on = {}  # doc_id -> date its verified successor was verified
    for d in docs:
        sup = d.get("supersedes_document_id")
        if sup:
            lv = latest_verification(vers_by_doc.get(d["document_id"], []), as_of)
            if lv and lv["outcome"] == "VERIFIED":
                succ_verified_on[sup] = lv["verified_on"]
    out = []
    for d in docs:
        if d["expiry_date"] is None:
            continue
        lv = latest_verification(vers_by_doc.get(d["document_id"], []), as_of)
        if not lv or lv["outcome"] != "VERIFIED":
            continue
        E, vdate = d["expiry_date"], lv["verified_on"]
        succ = succ_verified_on.get(d["document_id"])
        crossed = [L for L in leads if E - timedelta(days=L) <= as_of]
        if crossed:
            L = min(crossed)
            trig = max(E - timedelta(days=L), vdate)
            if trig <= min(as_of, E) and not (succ is not None and succ < trig):
                if succ is not None and succ <= E:
                    closure, closed_on = "RESOLVED", succ
                elif E < as_of:
                    closure, closed_on = "SUPERSEDED", E + timedelta(days=1)
                else:
                    closure, closed_on = "OPEN_LIKE", None
                out.append(dict(dedupe_key=f"DOCUMENT_EXPIRING|{d['document_id']}|{E.isoformat()}|{L}",
                                alert_type="DOCUMENT_EXPIRING", customer_id=d["customer_id"],
                                document_id=d["document_id"], review_id=None, lead_days=L,
                                trigger_date=trig, closure=closure, closed_on=closed_on))
        if E < as_of and not (succ is not None and succ <= E):
            trig = max(E + timedelta(days=1), vdate)
            if succ is not None:
                closure, closed_on = "RESOLVED", max(succ, trig)
            else:
                closure, closed_on = "OPEN_LIKE", None
            out.append(dict(dedupe_key=f"DOCUMENT_EXPIRED|{d['document_id']}|{E.isoformat()}",
                            alert_type="DOCUMENT_EXPIRED", customer_id=d["customer_id"],
                            document_id=d["document_id"], review_id=None, lead_days=None,
                            trigger_date=trig, closure=closure, closed_on=closed_on))
    for r in reviews:
        if r["due_date"] < as_of and not (r["status"] == "COMPLETED" and r["completed_on"] is not None and r["completed_on"] <= r["due_date"]):
            if r["status"] == "COMPLETED" and r["completed_on"] is not None:
                closure, closed_on = "RESOLVED", r["completed_on"]
            else:
                closure, closed_on = "OPEN_LIKE", None
            out.append(dict(dedupe_key=f"REVIEW_OVERDUE|{r['review_id']}|{r['due_date'].isoformat()}",
                            alert_type="REVIEW_OVERDUE", customer_id=r["customer_id"],
                            document_id=None, review_id=r["review_id"], lead_days=None,
                            trigger_date=r["due_date"] + timedelta(days=1), closure=closure, closed_on=closed_on))
    out.sort(key=lambda a: (a["trigger_date"], a["dedupe_key"]))
    return out


def alert_severity(alert_cfg, alert_type, lead_days):
    sev = alert_cfg["severity"]
    if alert_type == "DOCUMENT_EXPIRING":
        return sev[str(lead_days)]
    return sev["EXPIRED"] if alert_type == "DOCUMENT_EXPIRED" else sev["REVIEW_OVERDUE"]


def is_overdue_review(r, as_of: date) -> bool:
    if r["due_date"] >= as_of:
        return False
    return not (r["status"] == "COMPLETED" and r["completed_on"] is not None and r["completed_on"] <= r["due_date"])
