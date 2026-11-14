"""Independent validation (brief step 8) run against the EXPORTED CSV files.

Findings come in three groups:
  INTEGRITY       PK / FK / enum / null / id-format / date-order / lifecycle problems   -> must be 0
  RECONCILIATION  derived values re-computed from source rows (completeness, status,
                  risk score, alerts vs lead times, projections)                        -> must be 0
  BUSINESS_RULE   the five injected edge cases (expected to be found)

Detection never reads ground truth.  `evaluate_against_truth` is a separate, later step.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta

from . import rules
from .common import ID_REGEX, Ids
from .generator import group
from .schema import column_names

BUSINESS_CODES = ["EXPIRED_DOCUMENT_MARKED_VALID", "APPROVED_WITH_MISSING_EVIDENCE", "OWNERSHIP_OVER_100",
                  "REVIEW_CLOSED_WITHOUT_RESOLUTION", "DUPLICATE_DOCUMENT_ACROSS_CUSTOMERS"]


class Findings:
    def __init__(self):
        self.rows, self._seen = [], set()

    def add(self, code, category, severity, entity_type, entity_id, customer_id=None, detail="", related=None):
        key = (code, entity_id, related)
        if key in self._seen:
            return
        self._seen.add(key)
        self.rows.append(dict(rule_code=code, category=category, severity=severity, entity_type=entity_type,
                              entity_id=entity_id, related_entity_id=related, customer_id=customer_id, detail=detail))


def _lax_db(T, tables):
    """Constraint-free copy so duplicates/orphans can be *reported* instead of raising on load."""
    conn = sqlite3.connect(":memory:")
    for t in tables:
        cols = column_names(t)
        conn.execute(f'CREATE TABLE {t["name"]} ({",".join(cols)})')
        conn.executemany(f'INSERT INTO {t["name"]} VALUES ({",".join("?" * len(cols))})',
                         [tuple((x.isoformat() if isinstance(x, date) else x) for x in (r[c] for c in cols)) for r in T[t["name"]]])
    return conn


def validate(T, tables, policy, as_of: date):
    F = Findings()
    tb = {t["name"]: t for t in tables}
    conn = _lax_db(T, tables)
    q = lambda sql, *a: conn.execute(sql, a).fetchall()
    cust = {c["customer_id"]: c for c in T["customer"]}
    docs = {d["document_id"]: d for d in T["document"]}
    ids = Ids(policy["id_year"])

    # ---------------------------------------------------------------- INTEGRITY
    for t in tables:
        pk = ", ".join(t["pk"])
        for row in q(f'SELECT {pk}, COUNT(*) FROM {t["name"]} GROUP BY {pk} HAVING COUNT(*) > 1'):
            F.add("PK_DUPLICATE", "INTEGRITY", "ERROR", t["name"], str(row[0]), detail=f"{row[-1]} rows share the key")
        for c in t["cols"]:
            if c["required"]:
                for (v,) in q(f'SELECT {t["pk"][0]} FROM {t["name"]} WHERE {c["name"]} IS NULL OR {c["name"]} = ""'):
                    F.add("REQUIRED_VALUE_MISSING", "INTEGRITY", "ERROR", t["name"], str(v), detail=c["name"])
            if isinstance(c["allowed"], list):
                ph = ",".join("?" * len(c["allowed"]))
                for (v, bad) in q(f'SELECT {t["pk"][0]}, {c["name"]} FROM {t["name"]} WHERE {c["name"]} IS NOT NULL AND {c["name"]} NOT IN ({ph})', *c["allowed"]):
                    F.add("ENUM_VIOLATION", "INTEGRITY", "ERROR", t["name"], str(v), detail=f'{c["name"]}={bad}')
            if c["fk"]:
                ref_t, ref_c = re.match(r"(\w+)\((\w+)\)", c["fk"]).groups()
                for (v, bad) in q(f'SELECT a.{t["pk"][0]}, a.{c["name"]} FROM {t["name"]} a LEFT JOIN {ref_t} b ON a.{c["name"]} = b.{ref_c} '
                                  f'WHERE a.{c["name"]} IS NOT NULL AND b.{ref_c} IS NULL'):
                    F.add("FK_ORPHAN", "INTEGRITY", "ERROR", t["name"], str(v), detail=f'{c["name"]}={bad} -> {ref_t}')
    for tname, rx in ID_REGEX.items():
        pat = re.compile(rx)
        for r in T[tname]:
            key = r[tb[tname]["pk"][0]]
            if not pat.match(key):
                F.add("ID_FORMAT", "INTEGRITY", "ERROR", tname, key, detail=f"does not match {rx}")
    for d in T["document"]:
        if not d["document_id"].startswith(f'DOC-{d["customer_id"]}-'):
            F.add("ID_FORMAT", "INTEGRITY", "ERROR", "document", d["document_id"], d["customer_id"], "document id does not embed customer id")
    for rv in T["review_case"]:
        if not rv["review_id"].startswith(f'REV-{rv["customer_id"]}-'):
            F.add("ID_FORMAT", "INTEGRITY", "ERROR", "review_case", rv["review_id"], rv["customer_id"], "review id does not embed customer id")

    # polymorphic references
    pk_sets = {name: {r[t["pk"][0]] for r in T[name]} for name, t in tb.items()}
    for a in T["audit_event"]:
        if a["entity_type"] not in tb or a["entity_id"] not in pk_sets[a["entity_type"]]:
            F.add("AUDIT_ENTITY_ORPHAN", "INTEGRITY", "ERROR", "audit_event", a["audit_event_id"], a["customer_id"], f'{a["entity_type"]}:{a["entity_id"]}')
        if a["event_ts"][:10] > as_of.isoformat():
            F.add("DATE_ORDER", "INTEGRITY", "ERROR", "audit_event", a["audit_event_id"], a["customer_id"], "event after as_of")
    for a in T["alert"]:
        want_doc = a["alert_type"].startswith("DOCUMENT_")
        if want_doc != (a["document_id"] is not None) or (not want_doc) != (a["review_id"] is not None):
            F.add("ALERT_REFERENCE", "INTEGRITY", "ERROR", "alert", a["alert_id"], a["customer_id"], "document/review reference does not match alert_type")
    ctype_by = {c["customer_type_id"]: c for c in T["customer_type"]}
    for l in T["ownership_link"]:
        c = cust.get(l["customer_id"])
        if c and not ctype_by[c["customer_type_id"]]["requires_beneficial_owner"]:
            F.add("OWNERSHIP_ON_NON_BUSINESS", "INTEGRITY", "ERROR", "ownership_link", l["ownership_link_id"], l["customer_id"], "ownership link on a customer type that does not use them")

    # ------------------------------------------------------- date ordering
    def dord(ok, table, eid, cid, msg):
        if not ok:
            F.add("DATE_ORDER", "INTEGRITY", "ERROR", table, str(eid), cid, msg)

    vdays = {d["document_type_code"]: d["validity_days"] for d in T["document_type"]}
    for c in T["customer"]:
        dord(c["birth_or_incorporation_date"] < c["onboarding_date"] <= as_of, "customer", c["customer_id"], c["customer_id"], "birth < onboarding <= as_of")
    for d in T["document"]:
        c = cust.get(d["customer_id"])
        dord(d["submitted_on"] >= d["issue_date"] and d["submitted_on"] <= as_of, "document", d["document_id"], d["customer_id"], "issue <= submitted <= as_of")
        if c:
            dord(d["submitted_on"] >= c["onboarding_date"], "document", d["document_id"], d["customer_id"], "submitted before onboarding")
        v = vdays.get(d["document_type_code"])
        exp_ok = (d["expiry_date"] is None) if v is None else (d["expiry_date"] == d["issue_date"] + timedelta(days=v))
        dord(exp_ok, "document", d["document_id"], d["customer_id"], "expiry_date inconsistent with validity_days")
        s = d["supersedes_document_id"]
        if s and s in docs:
            o = docs[s]
            dord(o["customer_id"] == d["customer_id"] and o["document_type_code"] == d["document_type_code"] and o["submitted_on"] < d["submitted_on"],
                 "document", d["document_id"], d["customer_id"], "supersedes link must be same customer/type and older")
    vers_by = group(T["verification"], "document_id")
    for did, vs in vers_by.items():
        vs = sorted(vs, key=lambda x: x["attempt_no"])
        dord([v["attempt_no"] for v in vs] == list(range(1, len(vs) + 1)), "verification", did, docs.get(did, {}).get("customer_id"), "attempt_no not contiguous")
        for v in vs:
            d = docs.get(did)
            if d:
                dord(d["submitted_on"] <= v["verified_on"] <= as_of, "verification", v["verification_id"], d["customer_id"], "submitted <= verified <= as_of")
            dord((v["reason_code"] == "OK") == (v["outcome"] == "VERIFIED") or v["outcome"] == "INCONCLUSIVE", "verification", v["verification_id"], None, "reason_code/outcome inconsistent")
    for rt in T["risk_rating"]:
        dord(rt["effective_to"] is None or rt["effective_to"] > rt["effective_from"], "risk_rating", rt["risk_rating_id"], rt["customer_id"], "effective window")
    for l in T["ownership_link"]:
        c = cust.get(l["customer_id"])
        dord(l["effective_to"] is None or l["effective_to"] > l["effective_from"], "ownership_link", l["ownership_link_id"], l["customer_id"], "effective window")
        if c:
            dord(l["effective_from"] >= c["onboarding_date"], "ownership_link", l["ownership_link_id"], l["customer_id"], "link before onboarding")
    for dc in T["customer_decision"]:
        c = cust.get(dc["customer_id"])
        if c:
            dord(c["onboarding_date"] <= dc["decision_date"] <= as_of, "customer_decision", dc["decision_id"], dc["customer_id"], "decision date window")
    for a in T["alert"]:
        dord(a["trigger_date"] <= as_of and (a["resolved_on"] is None or a["resolved_on"] >= a["trigger_date"]), "alert", a["alert_id"], a["customer_id"], "trigger/resolved dates")
    # one current rating per customer
    cur = Counter(r["customer_id"] for r in T["risk_rating"] if r["effective_to"] is None)
    for cid in cust:
        if cur.get(cid, 0) != 1:
            F.add("RATING_HISTORY", "INTEGRITY", "ERROR", "risk_rating", cid, cid, f"{cur.get(cid, 0)} current ratings")

    # ---------------------------------------------------------- lifecycle
    trans = policy["review_transitions"]
    ev_by = group(T["review_case_event"], "review_id")
    for rv in T["review_case"]:
        evs = sorted(ev_by.get(rv["review_id"], []), key=lambda e: e["review_event_id"])
        ok = bool(evs) and evs[0]["from_status"] is None and evs[0]["to_status"] == "SCHEDULED"
        for p, n in zip(evs, evs[1:]):
            ok = ok and n["from_status"] == p["to_status"] and n["to_status"] in trans[p["to_status"]] and n["event_date"] >= p["event_date"]
        if not ok:
            F.add("LIFECYCLE_TRANSITION", "INTEGRITY", "ERROR", "review_case", rv["review_id"], rv["customer_id"], "invalid review transition chain")
        if evs and rv["status"] != evs[-1]["to_status"]:
            F.add("PROJECTION_MISMATCH", "RECONCILIATION", "ERROR", "review_case", rv["review_id"], rv["customer_id"], "status != last event")
        if (rv["status"] == "COMPLETED") != (rv["completed_on"] is not None):
            F.add("LIFECYCLE_TRANSITION", "INTEGRITY", "ERROR", "review_case", rv["review_id"], rv["customer_id"], "completed_on must be set iff COMPLETED")
        if rv["opened_on"] is not None and rv["opened_on"] > as_of:
            F.add("DATE_ORDER", "INTEGRITY", "ERROR", "review_case", rv["review_id"], rv["customer_id"], "opened after as_of")
        if rv["status"] == "SCHEDULED" and rv["opened_on"] is not None:
            F.add("LIFECYCLE_TRANSITION", "INTEGRITY", "ERROR", "review_case", rv["review_id"], rv["customer_id"], "SCHEDULED review has opened_on")
    atrans = policy["alert_transitions"]
    aev_by = group(T["alert_event"], "alert_id")
    amap = {"CREATED": "OPEN", "ACKNOWLEDGED": "ACKNOWLEDGED", "RESOLVED": "RESOLVED", "SUPERSEDED": "SUPERSEDED"}
    for al in T["alert"]:
        evs = sorted(aev_by.get(al["alert_id"], []), key=lambda e: e["alert_event_id"])
        ok = bool(evs) and evs[0]["event_type"] == "CREATED"
        state = "OPEN"
        for e in evs[1:]:
            nxt = amap[e["event_type"]]
            ok = ok and nxt in atrans[state]
            state = nxt
        for p, n in zip(evs, evs[1:]):
            ok = ok and n["event_date"] >= p["event_date"]
        if not ok:
            F.add("LIFECYCLE_TRANSITION", "INTEGRITY", "ERROR", "alert", al["alert_id"], al["customer_id"], "invalid alert event chain")
        last = amap[evs[-1]["event_type"]] if evs else None
        if last != al["status"]:
            F.add("PROJECTION_MISMATCH", "RECONCILIATION", "ERROR", "alert", al["alert_id"], al["customer_id"], "status != last alert_event")
    # config alignment: kyc_requirement table must equal the policy file
    want = {(ct, rk, dt) for ct, lv in policy["requirements"].items() for rk, lst in lv.items() for dt in lst}
    have = {(r["customer_type_id"], r["risk_level"], r["document_type_code"]) for r in T["kyc_requirement"]}
    for x in sorted(want ^ have):
        F.add("REQUIREMENT_CONFIG_MISMATCH", "RECONCILIATION", "ERROR", "kyc_requirement", "|".join(x), detail="differs from config/policy.json")

    # ------------------------------------------------------ reconciliation
    ratings_by, docs_by, links_by = group(T["risk_rating"], "customer_id"), group(T["document"], "customer_id"), group(T["ownership_link"], "customer_id")
    needs_owner = {c["customer_type_id"]: bool(c["requires_beneficial_owner"]) for c in T["customer_type"]}
    th = policy["risk_score_thresholds"]
    for rt in T["risk_rating"]:
        comp = dict(p.split("=") for p in rt["score_components"].split(";"))
        total = sum(int(v) for v in comp.values())
        lvl = "HIGH" if total >= th["HIGH"] else ("MEDIUM" if total >= th["MEDIUM"] else "LOW")
        if total != rt["risk_score"] or lvl != rt["risk_level"]:
            F.add("RISK_SCORE_MISMATCH", "RECONCILIATION", "ERROR", "risk_rating", rt["risk_rating_id"], rt["customer_id"], f"score {rt['risk_score']} vs {total}; level {rt['risk_level']} vs {lvl}")
    last_dec = {}
    for dcs in T["customer_decision"]:
        k = dcs["customer_id"]
        if k not in last_dec or (dcs["decision_date"], dcs["decision_id"]) > (last_dec[k]["decision_date"], last_dec[k]["decision_id"]):
            last_dec[k] = dcs
    for cid, c in cust.items():
        rs = sorted(ratings_by.get(cid, []), key=lambda x: x["effective_from"])
        curr = [r for r in rs if r["effective_to"] is None]
        if curr and curr[0]["risk_level"] != c["risk_level"]:
            F.add("PROJECTION_MISMATCH", "RECONCILIATION", "ERROR", "customer", cid, cid, "risk_level != current rating")
        ld = last_dec.get(cid)
        if not ld or ld["decision"] != c["status"] or ld["decision_date"] != c["status_since"]:
            F.add("PROJECTION_MISMATCH", "RECONCILIATION", "ERROR", "customer", cid, cid, "status/status_since != latest decision")
    kyc_rows = T["kyc_requirement"]

    def snap(cid, t):
        c = cust[cid]
        if not ratings_by.get(cid):
            return None  # corrupted/orphaned customer: already reported as an integrity finding
        return rules.evaluate(c, sorted(ratings_by[cid], key=lambda x: x["effective_from"]), docs_by.get(cid, []), vers_by,
                              links_by.get(cid, []), kyc_rows, t, needs_owner[c["customer_type_id"]])

    last_of_day = {}
    for dcs in T["customer_decision"]:
        k = (dcs["customer_id"], dcs["decision_date"])
        if k not in last_of_day or dcs["decision_id"] > last_of_day[k]:
            last_of_day[k] = dcs["decision_id"]
    for dcs in T["customer_decision"]:
        if dcs["customer_id"] not in cust:
            continue
        if last_of_day[(dcs["customer_id"], dcs["decision_date"])] != dcs["decision_id"]:
            continue  # intra-day intermediate state, legitimately superseded by a later same-day decision
        e = snap(dcs["customer_id"], dcs["decision_date"])
        if e is None:
            continue
        pct = round(100.0 * dcs["satisfied_count"] / dcs["required_count"], 2) if dcs["required_count"] else 100.0
        bad = (pct != dcs["completeness_pct"] or e["required_count"] != dcs["required_count"] or e["satisfied_count"] != dcs["satisfied_count"]
               or (e["reason_codes"] or "") != (dcs["reason_codes"] or ""))
        if bad:
            F.add("DECISION_RECOMPUTE_MISMATCH", "RECONCILIATION", "ERROR", "customer_decision", dcs["decision_id"], dcs["customer_id"],
                  f"stored {dcs['satisfied_count']}/{dcs['required_count']} vs recomputed {e['satisfied_count']}/{e['required_count']}")
    # verification outcome must follow the documented rule
    for did, vs in vers_by.items():
        d = docs.get(did)
        if not d or d["customer_id"] not in cust:
            continue
        oc, rs_ = rules.verification_outcome(d, cust[d["customer_id"]]["legal_name"], policy["verification"])
        final = max(vs, key=lambda x: x["attempt_no"])
        if final["outcome"] != "INCONCLUSIVE" and (final["outcome"], final["reason_code"]) != (oc, rs_):
            F.add("VERIFICATION_RULE_MISMATCH", "RECONCILIATION", "ERROR", "verification", final["verification_id"], d["customer_id"], f"stored {final['outcome']}/{final['reason_code']} vs rule {oc}/{rs_}")
    # derived document status
    sup = rules.superseded_verified_ids(docs, vers_by, as_of)
    for d in T["document"]:
        derived = rules.derive_document_status(d, vers_by.get(d["document_id"], []), d["document_id"] in sup, as_of)
        if derived != d["status"]:
            if d["status"] == "VALID" and derived == "EXPIRED":
                continue  # reported below as the business-rule finding
            F.add("DOCUMENT_STATUS_MISMATCH", "RECONCILIATION", "ERROR", "document", d["document_id"], d["customer_id"], f"stored {d['status']} vs derived {derived}")
    # expiry alerts at configured lead times + overdue-review alerts
    expected = rules.derive_alerts(T["document"], vers_by, T["review_case"], policy["alerts"], as_of)
    have_alerts = {a["dedupe_key"]: a for a in T["alert"]}
    for ex in expected:
        a = have_alerts.get(ex["dedupe_key"])
        if a is None:
            F.add("ALERT_MISSING", "RECONCILIATION", "ERROR", "alert", ex["dedupe_key"], ex["customer_id"], "expected from lead-time rule but absent")
            continue
        cls = "OPEN_LIKE" if a["status"] in ("OPEN", "ACKNOWLEDGED") else a["status"]
        sev = rules.alert_severity(policy["alerts"], ex["alert_type"], ex["lead_days"])
        if (a["trigger_date"], a["lead_days"], cls, a["severity"], a["resolved_on"]) != (ex["trigger_date"], ex["lead_days"], ex["closure"], sev, ex["closed_on"]):
            F.add("ALERT_MISMATCH", "RECONCILIATION", "ERROR", "alert", a["alert_id"], a["customer_id"], f"expected trigger={ex['trigger_date']} lead={ex['lead_days']} closure={ex['closure']}")
    exp_keys = {e["dedupe_key"] for e in expected}
    tighter = {}  # (doc, expiry) -> smallest expected lead; earlier, wider escalations may legitimately remain, closed
    for e in expected:
        if e["alert_type"] == "DOCUMENT_EXPIRING":
            kk = e["dedupe_key"].rsplit("|", 1)[0]
            tighter[kk] = min(tighter.get(kk, 10 ** 6), e["lead_days"])
    for k, a in have_alerts.items():
        if k not in exp_keys:
            if (a["alert_type"] == "DOCUMENT_EXPIRING" and a["status"] == "SUPERSEDED"
                    and tighter.get(k.rsplit("|", 1)[0], 10 ** 6) < (a["lead_days"] or 0)):
                continue
            F.add("ALERT_UNEXPECTED", "RECONCILIATION", "ERROR", "alert", a["alert_id"], a["customer_id"], "no rule produces this alert")
    # arithmetic: duplicated review per customer-year
    seen = Counter((r["customer_id"], r["review_id"][-4:]) for r in T["review_case"])
    for k, n in seen.items():
        if n > 1:
            F.add("PK_DUPLICATE", "INTEGRITY", "ERROR", "review_case", f"{k[0]}-{k[1]}", k[0], "more than one review per customer-year")
    nat = Counter((d["customer_id"], d["document_type_code"], d["submitted_on"]) for d in T["document"])
    for k, n in nat.items():
        if n > 1:
            F.add("DUPLICATE_DOCUMENT_SAME_CUSTOMER", "INTEGRITY", "ERROR", "document", "|".join(map(str, k)), k[0], "same customer/type/date submitted twice")

    # ---------------------------------------------------- BUSINESS RULES
    for d in T["document"]:
        if d["status"] == "VALID" and d["expiry_date"] is not None and d["expiry_date"] < as_of:
            F.add("EXPIRED_DOCUMENT_MARKED_VALID", "BUSINESS_RULE", "HIGH", "document", d["document_id"], d["customer_id"], f"expiry {d['expiry_date']} < {as_of}")
    for cid, c in cust.items():
        if c["status"] == "APPROVED":
            e = snap(cid, as_of)
            if e is not None and not e["doc_complete"]:
                F.add("APPROVED_WITH_MISSING_EVIDENCE", "BUSINESS_RULE", "CRITICAL", "customer", cid, cid, f"APPROVED but {e['reason_codes']}")
        if needs_owner[c["customer_type_id"]]:
            bps, n = rules.ownership_state(links_by.get(cid, []), as_of)
            if n and bps > 10000:
                F.add("OWNERSHIP_OVER_100", "BUSINESS_RULE", "HIGH", "customer", cid, cid, f"effective ownership {bps / 100:.2f}%")
            elif n and bps < 10000 and c["status"] != "PENDING":  # PENDING onboarding may still be collecting owners
                F.add("OWNERSHIP_UNDER_100", "RECONCILIATION", "ERROR", "customer", cid, cid, f"effective ownership {bps / 100:.2f}%")
            elif n == 0 and c["status"] == "APPROVED":
                F.add("APPROVED_WITHOUT_OWNERSHIP", "RECONCILIATION", "ERROR", "customer", cid, cid, "approved business has no ownership records")
    for rv in T["review_case"]:
        if rv["status"] == "COMPLETED" and not rv["resolution"]:
            F.add("REVIEW_CLOSED_WITHOUT_RESOLUTION", "BUSINESS_RULE", "HIGH", "review_case", rv["review_id"], rv["customer_id"], "COMPLETED with empty resolution")
    by_fp = defaultdict(list)
    for d in T["document"]:
        by_fp[d["document_fingerprint"]].append(d)
    owners_of = defaultdict(set)
    for l in T["ownership_link"]:
        owners_of[l["customer_id"]].add(l["owner_id"])
    for fp, ds in sorted(by_fp.items()):
        custs = sorted({d["customer_id"] for d in ds})
        if len(custs) < 2:
            if len(ds) > 1:
                F.add("DUPLICATE_DOCUMENT_SAME_CUSTOMER", "INTEGRITY", "ERROR", "document", ds[0]["document_id"], ds[0]["customer_id"], "fingerprint reused inside one customer")
            continue
        related = any(owners_of[a] & owners_of[b] for i, a in enumerate(custs) for b in custs[i + 1:])
        if related:
            continue
        ids_ = sorted(d["document_id"] for d in ds)
        F.add("DUPLICATE_DOCUMENT_ACROSS_CUSTOMERS", "BUSINESS_RULE", "HIGH", "document", ids_[0], ",".join(custs), f"fingerprint shared by {len(custs)} unrelated customers",
              related=",".join(ids_[1:]))
    conn.close()
    rows = sorted(F.rows, key=lambda x: (x["category"], x["rule_code"], x["entity_id"], x["related_entity_id"] or ""))
    for i, r in enumerate(rows, 1):
        r["finding_id"] = ids.finding(i)
    return rows


def evaluate_against_truth(findings, truth):
    """Separate evaluation step: compare detections with the injected labels."""
    by_code = defaultdict(list)
    for f in findings:
        if f["category"] == "BUSINESS_RULE":
            by_code[f["rule_code"]].append(f)
    per, tp_total, fn_total, fp_total = {}, 0, 0, 0
    for code in BUSINESS_CODES:
        t_rows = [t for t in truth if t["issue_type"] == code]
        f_rows = by_code.get(code, [])
        matched_f, tp = set(), 0
        for t in t_rows:
            tid = {t["entity_id"]} | ({t["related_entity_id"]} if t["related_entity_id"] else set())
            hit = None
            for i, f in enumerate(f_rows):
                fid = {f["entity_id"]} | (set(f["related_entity_id"].split(",")) if f["related_entity_id"] else set())
                if i not in matched_f and tid & fid:
                    hit = i
                    break
            if hit is not None:
                matched_f.add(hit)
                tp += 1
        fn, fp = len(t_rows) - tp, len(f_rows) - len(matched_f)
        per[code] = dict(injected=len(t_rows), detected=tp, missed=fn, unexpected=fp,
                         precision=round(tp / (tp + fp), 4) if tp + fp else 1.0, recall=round(tp / (tp + fn), 4) if tp + fn else 1.0)
        tp_total, fn_total, fp_total = tp_total + tp, fn_total + fn, fp_total + fp
    defects = [f for f in findings if f["category"] != "BUSINESS_RULE"]
    return dict(per_class=per, true_positives=tp_total, missed=fn_total, unexpected_business_findings=fp_total,
                integrity_or_reconciliation_defects=len(defects),
                overall_precision=round(tp_total / (tp_total + fp_total), 4) if tp_total + fp_total else 1.0,
                overall_recall=round(tp_total / (tp_total + fn_total), 4) if tp_total + fn_total else 1.0,
                passed=(fn_total == 0 and fp_total == 0 and not defects))
