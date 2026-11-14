"""Labelled edge-case injection (brief step 5) and ground-truth / expected-outcome labels (step 7).

Principles
* Injection runs AFTER a fully valid baseline exists.
* Every injection touches only the records named in its ground-truth row; history is
  appended, never overwritten (new decision / new ownership link / new review event),
  except the single `status` / `fingerprint` / projection field that *is* the defect.
* Each class targets distinct customers so labels never overlap.
* The decision engine and validator never import or read ground truth.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import rules
from .common import rng_for
from .generator import group

ISSUE_TYPES = [
    "EXPIRED_DOCUMENT_MARKED_VALID",
    "APPROVED_WITH_MISSING_EVIDENCE",
    "OWNERSHIP_OVER_100",
    "REVIEW_CLOSED_WITHOUT_RESOLUTION",
    "DUPLICATE_DOCUMENT_ACROSS_CUSTOMERS",
]


def inject(T, kyc, policy, gen, P, ids, as_of, seed):
    r = rng_for(seed, "anomalies")
    k = P["anomalies_per_class"]
    used, raw = set(), []
    docs_by_cust, ratings_by = group(T["document"], "customer_id"), group(T["risk_rating"], "customer_id")
    links_by, vers_by = group(T["ownership_link"], "customer_id"), group(T["verification"], "document_id")
    cust = {c["customer_id"]: c for c in T["customer"]}
    needs_owner = {c["customer_type_id"]: bool(c["requires_beneficial_owner"]) for c in policy["customer_types"]}
    analysts = sorted(s["user_id"] for s in T["staff_user"] if s["role"] == "OPERATIONS_ANALYST")

    def ev(cid, t):
        c = cust[cid]
        return rules.evaluate(c, sorted(ratings_by[cid], key=lambda x: x["effective_from"]), docs_by_cust.get(cid, []),
                              vers_by, links_by.get(cid, []), kyc, t, needs_owner[c["customer_type_id"]])

    def pick(cands, n):
        cands = [x for x in cands]
        r.shuffle(cands)
        out = []
        for key, cid in cands:
            if cid in used:
                continue
            used.add(cid)
            out.append(key)
            if len(out) == n:
                break
        return out

    # 1 -- expired document marked valid (flip stored status of a naturally expired, verified document)
    cands = sorted((d["document_id"], d["customer_id"]) for d in T["document"] if d["status"] == "EXPIRED")
    for did in pick(cands, k):
        d = next(x for x in T["document"] if x["document_id"] == did)
        d["status"] = "VALID"
        raw.append((ISSUE_TYPES[0], "document", did, None, d["customer_id"], "HIGH",
                    f"expiry_date {d['expiry_date']} < as_of {as_of} but status stored as VALID"))

    # 2 -- approved although mandatory document evidence is missing (new APPROVED decision appended)
    last = {}
    for dcs in T["customer_decision"]:
        if dcs["customer_id"] not in last or dcs["decision_id"] > last[dcs["customer_id"]]["decision_id"]:
            last[dcs["customer_id"]] = dcs
    cands = []
    for c in sorted(T["customer"], key=lambda x: x["customer_id"]):
        e0 = ev(c["customer_id"], as_of)
        # business customers must otherwise be clean (ownership reconciled) so that the label is unambiguous
        if c["status"] == "PENDING" and not e0["doc_complete"] and e0["owner_ok"]:
            cands.append((c["customer_id"], c["customer_id"]))
    for cid in pick(cands, k):
        t = max(last[cid]["decision_date"], as_of - timedelta(days=r.randint(0, 5)))
        e = ev(cid, t)
        if e["doc_complete"]:
            used.discard(cid)
            continue
        new = dict(decision_id=ids.decision(len(T["customer_decision"]) + 1), customer_id=cid, decision_date=t,
                   decision="APPROVED", required_count=e["required_count"], satisfied_count=e["satisfied_count"],
                   completeness_pct=e["completeness_pct"], reason_codes=e["reason_codes"],
                   policy_version=policy["policy_version"], decided_by=r.choice(analysts))
        T["customer_decision"].append(new)
        cust[cid]["status"], cust[cid]["status_since"] = "APPROVED", t
        raw.append((ISSUE_TYPES[1], "customer", cid, new["decision_id"], cid, "CRITICAL",
                    f"APPROVED with completeness {e['completeness_pct']:.2f}% ({e['reason_codes']})"))

    # 3 -- ownership percentages exceed 100% (a new owner link is appended without rebalancing)
    cands = [(c["customer_id"], c["customer_id"]) for c in sorted(T["customer"], key=lambda x: x["customer_id"])
             if c["customer_type_id"] == "BUSINESS" and c["status"] == "APPROVED" and last[c["customer_id"]]["decision_date"] < as_of
             and ev(c["customer_id"], as_of)["owner_ok"]]
    for cid in pick(cands, k):
        pct = float(r.randint(5, 25))
        owner = dict(owner_id=ids.owner(len(T["beneficial_owner"]) + 1), full_name=f"Added Owner {len(T['beneficial_owner']) + 1}",
                     date_of_birth=date(1975, 6, 15), nationality=cust[cid]["country_code"])
        T["beneficial_owner"].append(owner)
        # takes effect strictly AFTER the latest approval, so the approval was valid when it was taken
        eff = max(last[cid]["decision_date"] + timedelta(days=1), as_of - timedelta(days=r.randint(1, 30)))
        link = dict(ownership_link_id=ids.ownership_link(len(T["ownership_link"]) + 1), customer_id=cid,
                    owner_id=owner["owner_id"], ownership_pct=pct, effective_from=eff, effective_to=None)
        T["ownership_link"].append(link)
        links_by.setdefault(cid, []).append(link)
        raw.append((ISSUE_TYPES[2], "customer", cid, link["ownership_link_id"], cid, "HIGH",
                    f"effective ownership total = {100 + pct:.2f}% (> 100)"))

    # 4 -- review closed without resolution (state transition appended; resolution deliberately left NULL)
    revs = {x["review_id"]: x for x in T["review_case"]}
    last_ev = {}
    for e in T["review_case_event"]:
        if e["review_id"] not in last_ev or e["review_event_id"] > last_ev[e["review_id"]]["review_event_id"]:
            last_ev[e["review_id"]] = e
    cands = sorted((x["review_id"], x["customer_id"]) for x in T["review_case"]
                   if x["status"] in ("IN_PROGRESS", "ESCALATED") and x["due_date"] >= as_of)
    for rid in pick(cands, k):
        rv = revs[rid]
        t = max(last_ev[rid]["event_date"], as_of - timedelta(days=r.randint(0, 3)))
        T["review_case_event"].append(dict(review_event_id=ids.review_event(len(T["review_case_event"]) + 1), review_id=rid,
                                           from_status=rv["status"], to_status="COMPLETED", event_date=t,
                                           actor_id=rv["assigned_to"], note="Closed"))
        rv["status"], rv["completed_on"], rv["resolution"] = "COMPLETED", t, None
        raw.append((ISSUE_TYPES[3], "review_case", rid, None, rv["customer_id"], "HIGH", "status COMPLETED but resolution is NULL"))

    # 5 -- same document (fingerprint) used by two unrelated customers
    cands = sorted((d["document_id"], d["customer_id"]) for d in T["document"]
                   if d["document_type_code"] == "ID_PROOF" and cust[d["customer_id"]]["customer_type_id"] == "INDIVIDUAL"
                   and d["document_id"] in vers_by)
    picked = pick(cands, 2 * k)
    by_id = {d["document_id"]: d for d in T["document"]}
    for a, b in zip(picked[0::2], picked[1::2]):
        by_id[b]["document_fingerprint"] = by_id[a]["document_fingerprint"]
        raw.append((ISSUE_TYPES[4], "document", b, a, by_id[b]["customer_id"], "HIGH",
                    f"fingerprint shared with {a} (customer {by_id[a]['customer_id']})"))

    truth = []
    for n, (itype, etype, eid, rel, cid, sev, desc) in enumerate(raw, 1):
        truth.append(dict(anomaly_id=ids.ground_truth(n), issue_type=itype, entity_type=etype, entity_id=eid,
                          related_entity_id=rel, customer_id=cid, expected_rule_code=itype, severity=sev, description=desc))
    return truth


def expected_outcomes(T, kyc, policy, as_of):
    """Rule-derived label per customer (evaluation only): what the KYC file SHOULD look like on as_of.

    expected_status: APPROVED when the file is complete; otherwise ON_HOLD if the customer was ever
    legitimately approved (a decision taken with 100% completeness), else PENDING.
    """
    ratings_by, docs_by = group(T["risk_rating"], "customer_id"), group(T["document"], "customer_id")
    links_by, vers_by = group(T["ownership_link"], "customer_id"), group(T["verification"], "document_id")
    dec_by = group(T["customer_decision"], "customer_id")
    needs_owner = {c["customer_type_id"]: bool(c["requires_beneficial_owner"]) for c in policy["customer_types"]}
    out = []
    for c in sorted(T["customer"], key=lambda x: x["customer_id"]):
        cid = c["customer_id"]
        rs = sorted(ratings_by[cid], key=lambda x: x["effective_from"])
        e = rules.evaluate(c, rs, docs_by.get(cid, []), vers_by, links_by.get(cid, []), kyc, as_of,
                           needs_owner[c["customer_type_id"]])
        ever = any(d["decision"] == "APPROVED" and d["completeness_pct"] == 100.0 for d in dec_by[cid])
        status = "APPROVED" if e["complete"] else ("ON_HOLD" if ever else "PENDING")
        out.append(dict(customer_id=cid, expected_status=status, expected_kyc_complete=int(e["complete"]),
                        expected_completeness_pct=e["completeness_pct"], expected_reason_codes=e["reason_codes"]))
    return out
