"""Application service layer: the working KYC evidence workflow on top of SQLite.

Every operation reuses kyc/rules.py (the same rules the generator and validator use), is
append-only for history (documents, verifications, decisions, review/alert events, audit),
and writes an audit event.  Roles are checked per operation (no authentication - out of scope).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

from . import rules, validate as V
from .common import Ids, add_months, load_policy, parse_date
from .generator import group
from .schema import build_tables, column_names
from .store import date_columns

ADMIN = "SYSTEM_ADMINISTRATOR"
ANALYST, REVIEWER = "OPERATIONS_ANALYST", "RISK_COMPLIANCE_REVIEWER"


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Service:
    def __init__(self, db_path: str, as_of: date):
        self.policy = load_policy()
        self.tables = build_tables(self.policy)
        self.tb = {t["name"]: t for t in self.tables}
        self.ids = Ids(self.policy["id_year"])
        self.as_of = as_of
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.vdays = {d["document_type_code"]: d["validity_days"] for d in self.policy["document_types"]}
        self.needs_owner = {c["customer_type_id"]: bool(c["requires_beneficial_owner"]) for c in self.policy["customer_types"]}

    # ------------------------------------------------------------ db helpers
    def q(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def one(self, sql, params=()):
        r = self.q(sql, params)
        return r[0] if r else None

    def typed(self, table, where="", params=(), sql=None):
        dcols = set(date_columns(self.tb[table]))
        rows = self.q(sql or f"SELECT * FROM {table} {where}", params)
        for r in rows:
            for c in dcols:
                if r.get(c) is not None:
                    r[c] = date.fromisoformat(r[c])
        return rows

    def insert(self, table, row):
        cols = column_names(self.tb[table])
        vals = [(row[c].isoformat() if isinstance(row.get(c), date) else row.get(c)) for c in cols]
        self.conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    def next_id(self, table, pk, prefix, width=6):
        r = self.one(f"SELECT {pk} AS k FROM {table} WHERE {pk} LIKE ? ORDER BY {pk} DESC LIMIT 1", (prefix + "%",))
        n = int(r["k"][len(prefix):]) + 1 if r else 1
        return f"{prefix}{n:0{width}d}"

    def audit(self, cid, etype, eid, action, atype, aid, details=""):
        ts = f"{self.as_of.isoformat()}T{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z"
        self.insert("audit_event", dict(audit_event_id=self.next_id("audit_event", "audit_event_id", f"AUD-{self.policy['id_year']}-"),
                                        event_ts=ts, entity_type=etype, entity_id=eid, customer_id=cid, action=action,
                                        actor_type=atype, actor_id=aid, details=details))

    def actor(self, actor_id, roles):
        if not actor_id:
            raise ApiError(400, "ACTOR_REQUIRED", "send the acting staff user in the X-Actor header")
        s = self.one("SELECT * FROM staff_user WHERE user_id=?", (actor_id,))
        if not s:
            raise ApiError(400, "UNKNOWN_ACTOR", f"unknown staff user {actor_id}")
        if s["role"] not in set(roles) | {ADMIN}:
            raise ApiError(403, "FORBIDDEN", f"role {s['role']} may not perform this action (needs one of {sorted(set(roles) | {ADMIN})})")
        return s

    def tx(self, fn):
        with self.lock:
            try:
                out = fn()
                self.conn.commit()
                return out
            except Exception:
                self.conn.rollback()
                raise

    # --------------------------------------------------------- file / rules
    def _file(self, cid):
        c = self.typed("customer", "WHERE customer_id=?", (cid,))
        if not c:
            raise ApiError(404, "NOT_FOUND", f"customer {cid} not found")
        return dict(
            customer=c[0],
            ratings=sorted(self.typed("risk_rating", "WHERE customer_id=?", (cid,)), key=lambda x: x["effective_from"]),
            docs=self.typed("document", "WHERE customer_id=?", (cid,)),
            vers=group(self.typed("verification", sql="SELECT v.* FROM verification v JOIN document d ON d.document_id=v.document_id WHERE d.customer_id=?", params=(cid,)), "document_id"),
            links=self.typed("ownership_link", "WHERE customer_id=?", (cid,)),
        )

    def _kyc(self):
        return self.typed("kyc_requirement")

    def _evaluate(self, f, t):
        return rules.evaluate(f["customer"], f["ratings"], f["docs"], f["vers"], f["links"], self._kyc(), t,
                              self.needs_owner[f["customer"]["customer_type_id"]])

    def _refresh_statuses(self, f):
        by_id = {d["document_id"]: d for d in f["docs"]}
        sup = rules.superseded_verified_ids(by_id, f["vers"], self.as_of)
        for d in f["docs"]:
            st = rules.derive_document_status(d, f["vers"].get(d["document_id"], []), d["document_id"] in sup, self.as_of)
            if st != d["status"]:
                self.conn.execute("UPDATE document SET status=? WHERE document_id=?", (st, d["document_id"]))
                d["status"] = st

    def _decide(self, f, actor_id):
        """Append a decision only if the state changes (never overwrites). Returns the decision row or None."""
        c, cid = f["customer"], f["customer"]["customer_id"]
        ev = self._evaluate(f, self.as_of)
        state = c["status"]
        new = "APPROVED" if ev["complete"] else ("ON_HOLD" if state in ("APPROVED", "ON_HOLD") else "PENDING")
        last = self.one("SELECT * FROM customer_decision WHERE customer_id=? ORDER BY decision_date DESC, decision_id DESC LIMIT 1", (cid,))
        same = last and (last["satisfied_count"], last["required_count"], last["reason_codes"] or "") == (ev["satisfied_count"], ev["required_count"], ev["reason_codes"] or "")
        if new == state and same:
            return None  # nothing changed: the latest decision still describes reality
        row = dict(decision_id=self.next_id("customer_decision", "decision_id", "DEC-"), customer_id=cid, decision_date=self.as_of,
                   decision=new, required_count=ev["required_count"], satisfied_count=ev["satisfied_count"],
                   completeness_pct=ev["completeness_pct"], reason_codes=ev["reason_codes"],
                   policy_version=self.policy["policy_version"], decided_by=actor_id)
        self.insert("customer_decision", row)
        self.conn.execute("UPDATE customer SET status=?, status_since=? WHERE customer_id=?", (new, self.as_of.isoformat(), cid))
        c["status"], c["status_since"] = new, self.as_of
        self.audit(cid, "customer_decision", row["decision_id"], "DECISION_RECORDED", "STAFF" if actor_id != "USR-SYS-000" else "SYSTEM",
                   actor_id, f"decision={new};completeness={ev['completeness_pct']:.2f}")
        return row

    # ------------------------------------------------------------- queries
    def staff(self):
        return self.q("SELECT user_id, role, display_name FROM staff_user ORDER BY user_id")

    def system(self):
        return {"as_of_date": self.as_of.isoformat(), "policy_version": self.policy["policy_version"],
                "expiry_lead_days": self.policy["alerts"]["expiry_lead_days"]}

    def set_as_of(self, value, actor_id):
        self.actor(actor_id, [])
        d = parse_date(value)
        mx = self.one("SELECT MAX(d) AS m FROM (SELECT MAX(decision_date) d FROM customer_decision UNION SELECT MAX(verified_on) FROM verification)")
        if mx and mx["m"] and d < date.fromisoformat(mx["m"]):
            raise ApiError(422, "DATE_IN_PAST", f"as-of date cannot move before existing history ({mx['m']})")
        self.as_of = d
        return self.system()

    def summary(self):
        a = self.as_of.isoformat()
        c = lambda sql, *p: self.one(sql, p)["n"]
        soon = (self.as_of + timedelta(days=30)).isoformat()
        return {
            "as_of_date": a,
            "customers": {r["status"]: r["n"] for r in self.q("SELECT status, COUNT(*) n FROM customer GROUP BY status")},
            "customers_total": c("SELECT COUNT(*) n FROM customer"),
            "customers_by_risk": {r["risk_level"]: r["n"] for r in self.q("SELECT risk_level, COUNT(*) n FROM customer GROUP BY risk_level")},
            "documents": {r["status"]: r["n"] for r in self.q("SELECT status, COUNT(*) n FROM document GROUP BY status")},
            "documents_expiring_30d": c("SELECT COUNT(*) n FROM document WHERE status='VALID' AND expiry_date IS NOT NULL AND expiry_date>=? AND expiry_date<=?", a, soon),
            "reviews": {r["status"]: r["n"] for r in self.q("SELECT status, COUNT(*) n FROM review_case GROUP BY status")},
            "reviews_overdue": c("SELECT COUNT(*) n FROM review_case WHERE due_date<? AND (status<>'COMPLETED' OR completed_on>due_date)", a),
            "alerts_open": c("SELECT COUNT(*) n FROM alert WHERE status IN ('OPEN','ACKNOWLEDGED')"),
            "alerts_by_type": {r["alert_type"]: r["n"] for r in self.q("SELECT alert_type, COUNT(*) n FROM alert WHERE status IN ('OPEN','ACKNOWLEDGED') GROUP BY alert_type")},
            "audit_events": c("SELECT COUNT(*) n FROM audit_event"),
        }

    def list_customers(self, search="", status="", ctype="", risk="", limit=50, offset=0):
        where, p = [], []
        if search:
            where.append("(customer_id LIKE ? OR legal_name LIKE ?)")
            p += [f"%{search}%", f"%{search}%"]
        for col, v in (("status", status), ("customer_type_id", ctype), ("risk_level", risk)):
            if v:
                where.append(f"{col}=?")
                p.append(v)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.one(f"SELECT COUNT(*) n FROM customer {w}", p)["n"]
        rows = self.q(f"SELECT * FROM customer {w} ORDER BY customer_id LIMIT ? OFFSET ?", p + [min(int(limit), 500), int(offset)])
        return {"total": total, "items": rows}

    def customer_detail(self, cid):
        f = self._file(cid)
        ev = self._evaluate(f, self.as_of)
        req_types = rules.build_requirement_index(self._kyc(), self.as_of).get((f["customer"]["customer_type_id"], ev["risk_level"]), [])
        failing = {r.split(":")[1]: r.split(":")[0] for r in ev["reason_codes"].split("|") if ":" in r}
        checklist = [{"document_type": t, "satisfied": t not in failing, "reason": failing.get(t)} for t in req_types]
        if self.needs_owner[f["customer"]["customer_type_id"]]:
            bps, n = rules.ownership_state(f["links"], self.as_of)
            checklist.append({"document_type": "BENEFICIAL_OWNERSHIP_100PCT", "satisfied": ev["owner_ok"],
                              "reason": None if ev["owner_ok"] else ("OWNERSHIP_MISSING" if n == 0 else f"OWNERSHIP_TOTAL_{bps / 100:.2f}")})
        s = lambda t, w, p: self.q(f"SELECT * FROM {t} {w}", p)
        return {
            "customer": self.one("SELECT * FROM customer WHERE customer_id=?", (cid,)),
            "evaluation": {k: ev[k] for k in ("risk_level", "required_count", "satisfied_count", "completeness_pct", "reason_codes", "complete")},
            "checklist": checklist,
            "ratings": s("risk_rating", "WHERE customer_id=? ORDER BY effective_from", (cid,)),
            "documents": s("document", "WHERE customer_id=? ORDER BY document_id", (cid,)),
            "verifications": self.q("SELECT v.* FROM verification v JOIN document d ON d.document_id=v.document_id WHERE d.customer_id=? ORDER BY v.verification_id", (cid,)),
            "ownership": self.q("SELECT l.*, o.full_name FROM ownership_link l JOIN beneficial_owner o USING(owner_id) WHERE l.customer_id=?", (cid,)),
            "decisions": s("customer_decision", "WHERE customer_id=? ORDER BY decision_date, decision_id", (cid,)),
            "reviews": s("review_case", "WHERE customer_id=?", (cid,)),
            "alerts": s("alert", "WHERE customer_id=? ORDER BY trigger_date", (cid,)),
            "audit": s("audit_event", "WHERE customer_id=? ORDER BY audit_event_id", (cid,)),
        }

    def list_reviews(self, status="", overdue=False, limit=100):
        where, p = [], []
        if status:
            where.append("status=?")
            p.append(status)
        if overdue:
            where.append("due_date<? AND (status<>'COMPLETED' OR completed_on>due_date)")
            p.append(self.as_of.isoformat())
        w = ("WHERE " + " AND ".join(where)) if where else ""
        return self.q(f"SELECT * FROM review_case {w} ORDER BY due_date, review_id LIMIT ?", p + [min(int(limit), 500)])

    def review_detail(self, rid):
        r = self.one("SELECT * FROM review_case WHERE review_id=?", (rid,))
        if not r:
            raise ApiError(404, "NOT_FOUND", f"review {rid} not found")
        return {"review": r, "events": self.q("SELECT * FROM review_case_event WHERE review_id=? ORDER BY review_event_id", (rid,))}

    def list_alerts(self, status="", atype="", limit=100):
        where, p = [], []
        if status == "ACTIVE":
            where.append("status IN ('OPEN','ACKNOWLEDGED')")
        elif status:
            where.append("status=?")
            p.append(status)
        if atype:
            where.append("alert_type=?")
            p.append(atype)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        return self.q(f"SELECT * FROM alert {w} ORDER BY trigger_date DESC, alert_id LIMIT ?", p + [min(int(limit), 500)])

    def list_audit(self, cid="", limit=100):
        if cid:
            return self.q("SELECT * FROM audit_event WHERE customer_id=? ORDER BY audit_event_id DESC LIMIT ?", (cid, min(int(limit), 500)))
        return self.q("SELECT * FROM audit_event ORDER BY audit_event_id DESC LIMIT ?", (min(int(limit), 500),))

    def exceptions(self):
        """Run the full validator on the live database: integrity / reconciliation defects + business-rule exceptions."""
        with self.lock:
            T = {}
            for t in self.tables:
                T[t["name"]] = self.typed(t["name"], "ORDER BY " + ",".join(t["order"]))
            findings = V.validate(T, self.tables, self.policy, self.as_of)
        from collections import Counter
        return {"as_of_date": self.as_of.isoformat(), "by_category": dict(Counter(f["category"] for f in findings)),
                "by_rule": dict(Counter(f["rule_code"] for f in findings)), "findings": findings}

    # ------------------------------------------------------------ commands
    def create_customer(self, body, actor_id):
        self.actor(actor_id, [ANALYST])
        ctype, risk = body.get("customer_type_id"), body.get("risk_level")
        name = (body.get("legal_name") or "").strip()
        if ctype not in self.needs_owner or risk not in self.policy["risk_levels"] or not name:
            raise ApiError(422, "VALIDATION", "legal_name, customer_type_id (INDIVIDUAL|BUSINESS) and risk_level (LOW|MEDIUM|HIGH) are required")
        country = body.get("country_code", "IN")
        if country not in self.policy["enums"]["country_code"]:
            raise ApiError(422, "VALIDATION", f"country_code must be one of {self.policy['enums']['country_code']}")
        try:
            birth = parse_date(body.get("birth_or_incorporation_date") or "1990-01-01")
        except ValueError:
            raise ApiError(422, "VALIDATION", "birth_or_incorporation_date must be yyyy-mm-dd")
        if birth >= self.as_of:
            raise ApiError(422, "VALIDATION", "birth/incorporation date must be before the as-of date")
        th = self.policy["risk_score_thresholds"]
        score = {"LOW": 10, "MEDIUM": th["MEDIUM"], "HIGH": th["HIGH"]}[risk]

        def run():
            cid = self.next_id("customer", "customer_id", "CUS-")
            self.insert("customer", dict(customer_id=cid, customer_type_id=ctype, legal_name=name, country_code=country,
                                         birth_or_incorporation_date=birth, onboarding_date=self.as_of, risk_level=risk,
                                         status="PENDING", status_since=self.as_of))
            self.insert("risk_rating", dict(risk_rating_id=self.next_id("risk_rating", "risk_rating_id", "RSK-"), customer_id=cid, risk_score=score,
                                            risk_level=risk, score_components=f"manual={score}", rating_reason="INITIAL",
                                            effective_from=self.as_of, effective_to=None))
            f = self._file(cid)
            ev = self._evaluate(f, self.as_of)
            self.insert("customer_decision", dict(decision_id=self.next_id("customer_decision", "decision_id", "DEC-"), customer_id=cid,
                                                  decision_date=self.as_of, decision="PENDING", required_count=ev["required_count"],
                                                  satisfied_count=ev["satisfied_count"], completeness_pct=ev["completeness_pct"],
                                                  reason_codes=ev["reason_codes"], policy_version=self.policy["policy_version"], decided_by="USR-SYS-000"))
            self.audit(cid, "customer", cid, "CUSTOMER_ONBOARDED", "STAFF", actor_id, f"type={ctype}")
            return cid
        return self.customer_detail(self.tx(run))

    def submit_document(self, cid, body, actor_id=None):
        """Customer portal action: a new document row; any previous document of the type is superseded, never edited."""
        dtype = body.get("document_type_code")
        if dtype not in self.vdays:
            raise ApiError(422, "VALIDATION", f"document_type_code must be one of {sorted(self.vdays)}")
        f = self._file(cid)
        number, name = (body.get("document_number") or "").strip(), (body.get("name_on_document") or "").strip()
        if not number or not name:
            raise ApiError(422, "VALIDATION", "document_number and name_on_document are required")
        try:
            issue = parse_date(body.get("issue_date"))
        except (ValueError, TypeError):
            raise ApiError(422, "VALIDATION", "issue_date must be yyyy-mm-dd")
        if issue > self.as_of:
            raise ApiError(422, "VALIDATION", "issue_date cannot be after the as-of date")
        q_ = int(body.get("scan_quality_score", 80))
        if not 0 <= q_ <= 100:
            raise ApiError(422, "VALIDATION", "scan_quality_score must be 0..100")
        fp = hashlib.sha256(f"{dtype}|{number}".encode()).hexdigest()
        other = self.one("SELECT document_id, customer_id FROM document WHERE document_fingerprint=? AND customer_id<>?", (fp, cid))
        if other:
            raise ApiError(409, "DUPLICATE_DOCUMENT", f"this document is already on file for another customer ({other['customer_id']}, {other['document_id']})")
        if self.one("SELECT 1 x FROM document WHERE document_fingerprint=? AND customer_id=?", (fp, cid)):
            raise ApiError(409, "DUPLICATE_DOCUMENT", "this document was already submitted for this customer")
        v = self.vdays[dtype]

        def run():
            prev = [d for d in f["docs"] if d["document_type_code"] == dtype]
            prev.sort(key=lambda d: (d["submitted_on"], d["document_id"]))
            if prev and prev[-1]["submitted_on"] >= self.as_of:
                raise ApiError(409, "SAME_DAY_RESUBMISSION", f"a {dtype} was already submitted on {self.as_of}; verify it, or resubmit on a later business date")
            did = f"DOC-{cid}-{len(f['docs']) + 1:03d}"
            while self.one("SELECT 1 x FROM document WHERE document_id=?", (did,)):
                did = f"DOC-{cid}-{int(did[-3:]) + 1:03d}"
            row = dict(document_id=did, customer_id=cid, document_type_code=dtype, name_on_document=name, document_fingerprint=fp,
                       issue_date=issue, expiry_date=(issue + timedelta(days=v)) if v else None, submitted_on=self.as_of,
                       scan_quality_score=q_, status="PENDING_VERIFICATION", supersedes_document_id=prev[-1]["document_id"] if prev else None)
            self.insert("document", row)
            self.audit(cid, "document", did, "DOCUMENT_SUBMITTED", "CUSTOMER", cid, f"type={dtype}")
            return did
        did = self.tx(run)
        return self.one("SELECT * FROM document WHERE document_id=?", (did,))

    def verify_document(self, doc_id, actor_id):
        s = self.actor(actor_id, [ANALYST])
        d = self.typed("document", "WHERE document_id=?", (doc_id,))
        if not d:
            raise ApiError(404, "NOT_FOUND", f"document {doc_id} not found")
        d = d[0]

        def run():
            f = self._file(d["customer_id"])
            prior = f["vers"].get(doc_id, [])
            if prior and any(v["outcome"] in ("VERIFIED", "REJECTED") for v in prior):
                raise ApiError(409, "ALREADY_VERIFIED", "document already has a final verification; submit a new document to replace it")
            outcome, reason = rules.verification_outcome(d, f["customer"]["legal_name"], self.policy["verification"])
            if outcome == "VERIFIED" and not prior and d["scan_quality_score"] < self.policy["verification"]["inconclusive_below_quality"]:
                outcome, reason = "INCONCLUSIVE", "LOW_CONFIDENCE"
            row = dict(verification_id=self.next_id("verification", "verification_id", "VER-"), document_id=doc_id, attempt_no=len(prior) + 1,
                       verified_on=self.as_of, outcome=outcome, reason_code=reason, verified_by=s["user_id"])
            self.insert("verification", row)
            f["vers"].setdefault(doc_id, []).append(dict(row))
            self.audit(d["customer_id"], "verification", row["verification_id"], "VERIFICATION_RECORDED", "STAFF", s["user_id"], f"outcome={outcome};reason={reason}")
            self._refresh_statuses(f)
            dec = self._decide(f, s["user_id"])
            return {"verification": row, "decision": dec}
        return self.tx(run)

    def evaluate_customer(self, cid, actor_id):
        s = self.actor(actor_id, [ANALYST, REVIEWER])

        def run():
            f = self._file(cid)
            self._refresh_statuses(f)
            dec = self._decide(f, s["user_id"])
            return dec
        dec = self.tx(run)
        return {"decision_recorded": dec, "detail": self.customer_detail(cid)}

    def add_owner(self, cid, body, actor_id):
        self.actor(actor_id, [ANALYST])
        f = self._file(cid)
        if not self.needs_owner[f["customer"]["customer_type_id"]]:
            raise ApiError(422, "VALIDATION", "beneficial owners apply to BUSINESS customers only")
        try:
            pct = round(float(body.get("ownership_pct")), 2)
        except (TypeError, ValueError):
            raise ApiError(422, "VALIDATION", "ownership_pct must be a number")
        if not 0 < pct <= 100:
            raise ApiError(422, "VALIDATION", "ownership_pct must be > 0 and <= 100")
        cur_bps, _ = rules.ownership_state(f["links"], self.as_of)
        if cur_bps + int(round(pct * 100)) > 10000:
            raise ApiError(422, "OWNERSHIP_OVER_100", f"effective ownership would be {(cur_bps + pct * 100) / 100:.2f}% (> 100)")
        name = (body.get("full_name") or "").strip()
        if not name:
            raise ApiError(422, "VALIDATION", "full_name is required")

        def run():
            oid = body.get("owner_id")
            if oid and not self.one("SELECT 1 x FROM beneficial_owner WHERE owner_id=?", (oid,)):
                raise ApiError(404, "NOT_FOUND", f"owner {oid} not found")
            if not oid:
                oid = self.next_id("beneficial_owner", "owner_id", "BO-")
                self.insert("beneficial_owner", dict(owner_id=oid, full_name=name, date_of_birth=parse_date(body.get("date_of_birth") or "1980-01-01"),
                                                     nationality=body.get("nationality", f["customer"]["country_code"])))
            lid = self.next_id("ownership_link", "ownership_link_id", "OWN-")
            self.insert("ownership_link", dict(ownership_link_id=lid, customer_id=cid, owner_id=oid, ownership_pct=pct, effective_from=self.as_of, effective_to=None))
            self.audit(cid, "ownership_link", lid, "OWNERSHIP_RECORDED", "STAFF", actor_id, f"pct={pct:.2f}")
            self._decide(self._file(cid), actor_id)
            return lid
        self.tx(run)
        return self.customer_detail(cid)

    def create_review(self, cid, actor_id):
        s = self.actor(actor_id, [ANALYST, REVIEWER])
        f = self._file(cid)
        rc = self.policy["review"]
        if f["customer"]["status"] not in ("APPROVED", "ON_HOLD"):
            raise ApiError(409, "NOT_ELIGIBLE", "only APPROVED / ON_HOLD customers have periodic reviews")
        rid = self.ids.review(cid, rc["review_year"])
        if self.one("SELECT 1 x FROM review_case WHERE review_id=?", (rid,)):
            raise ApiError(409, "REVIEW_EXISTS", f"{rid} already exists")
        period, ys = rc["period_months"][f["customer"]["risk_level"]], date(rc["review_year"], 1, 1)
        k, due = 1, add_months(f["customer"]["onboarding_date"], period)
        while due < ys:
            k += 1
            due = add_months(f["customer"]["onboarding_date"], period * k)
        reviewer = self.one("SELECT user_id FROM staff_user WHERE role=? ORDER BY user_id LIMIT 1", (REVIEWER,))["user_id"]

        def run():
            self.insert("review_case", dict(review_id=rid, customer_id=cid, review_type="PERIODIC", risk_level_at_open=f["customer"]["risk_level"],
                                            due_date=due, opened_on=None, status="SCHEDULED", assigned_to=reviewer, completed_on=None, resolution=None))
            self._review_event(rid, cid, None, "SCHEDULED", s["user_id"], "Periodic review scheduled")
        self.tx(run)
        return self.review_detail(rid)

    def _review_event(self, rid, cid, fr, to, actor_id, note):
        eid = self.next_id("review_case_event", "review_event_id", "RVE-")
        self.insert("review_case_event", dict(review_event_id=eid, review_id=rid, from_status=fr, to_status=to,
                                              event_date=self.as_of, actor_id=actor_id, note=note))
        self.audit(cid, "review_case_event", eid, f"REVIEW_{to}", "STAFF", actor_id, f"review={rid}")

    def transition_review(self, rid, body, actor_id):
        to = body.get("to_status")
        roles = [REVIEWER] if to in ("ESCALATED", "COMPLETED") else [ANALYST, REVIEWER]
        s = self.actor(actor_id, roles)
        r = self.typed("review_case", "WHERE review_id=?", (rid,))
        if not r:
            raise ApiError(404, "NOT_FOUND", f"review {rid} not found")
        r = r[0]
        if to not in self.policy["review_transitions"].get(r["status"], []):
            raise ApiError(409, "INVALID_TRANSITION", f"{r['status']} -> {to} is not allowed (allowed: {self.policy['review_transitions'].get(r['status'], [])})")
        res = body.get("resolution")
        if to == "COMPLETED" and res not in self.policy["enums"]["review_resolution"]:
            raise ApiError(422, "RESOLUTION_REQUIRED", f"a review cannot be completed without a resolution (one of {self.policy['enums']['review_resolution']})")

        def run():
            sets, p = ["status=?"], [to]
            if to == "OPEN":
                sets.append("opened_on=?")
                p.append(self.as_of.isoformat())
            if to == "COMPLETED":
                sets += ["completed_on=?", "resolution=?"]
                p += [self.as_of.isoformat(), res]
            self.conn.execute(f"UPDATE review_case SET {','.join(sets)} WHERE review_id=?", p + [rid])
            self._review_event(rid, r["customer_id"], r["status"], to, s["user_id"], body.get("note") or (f"Resolution: {res}" if res else ""))
        self.tx(run)
        return self.review_detail(rid)

    def acknowledge_alert(self, aid, actor_id):
        s = self.actor(actor_id, [ANALYST, REVIEWER])
        a = self.one("SELECT * FROM alert WHERE alert_id=?", (aid,))
        if not a:
            raise ApiError(404, "NOT_FOUND", f"alert {aid} not found")
        if "ACKNOWLEDGED" not in self.policy["alert_transitions"][a["status"]]:
            raise ApiError(409, "INVALID_TRANSITION", f"alert is {a['status']}; cannot acknowledge")

        def run():
            self.conn.execute("UPDATE alert SET status='ACKNOWLEDGED' WHERE alert_id=?", (aid,))
            self._alert_event(aid, a["customer_id"], "ACKNOWLEDGED", self.as_of, s["user_id"])
        self.tx(run)
        return self.one("SELECT * FROM alert WHERE alert_id=?", (aid,))

    def _alert_event(self, aid, cid, et, d, actor_id):
        eid = self.next_id("alert_event", "alert_event_id", "ALE-")
        self.insert("alert_event", dict(alert_event_id=eid, alert_id=aid, event_type=et, event_date=d, actor_id=actor_id))
        self.audit(cid, "alert_event", eid, f"ALERT_{et}", "SYSTEM" if actor_id == "USR-SYS-000" else "STAFF", actor_id, f"alert={aid}")

    # ------------------------------------------------------- batch processing
    def scan_alerts(self, actor_id):
        """Idempotent expiry / overdue-review alert processor (dedupe_key)."""
        self.actor(actor_id, [])

        def run():
            docs, vers = self.typed("document"), group(self.typed("verification"), "document_id")
            reviews = self.typed("review_case")
            expected = rules.derive_alerts(docs, vers, reviews, self.policy["alerts"], self.as_of)
            have = {a["dedupe_key"]: a for a in self.q("SELECT * FROM alert")}
            created = closed = 0
            for ex in expected:
                a = have.get(ex["dedupe_key"])
                if a is None:
                    aid = self.next_id("alert", "alert_id", f"ALT-{self.policy['id_year']}-")
                    status = {"OPEN_LIKE": "OPEN", "RESOLVED": "RESOLVED", "SUPERSEDED": "SUPERSEDED"}[ex["closure"]]
                    self.insert("alert", dict(alert_id=aid, alert_type=ex["alert_type"], customer_id=ex["customer_id"], document_id=ex["document_id"],
                                              review_id=ex["review_id"], lead_days=ex["lead_days"],
                                              severity=rules.alert_severity(self.policy["alerts"], ex["alert_type"], ex["lead_days"]),
                                              trigger_date=ex["trigger_date"], status=status, dedupe_key=ex["dedupe_key"], resolved_on=ex["closed_on"]))
                    self._alert_event(aid, ex["customer_id"], "CREATED", ex["trigger_date"], "USR-SYS-000")
                    if ex["closed_on"]:
                        self._alert_event(aid, ex["customer_id"], ex["closure"], ex["closed_on"], "USR-SYS-000")
                    created += 1
                elif a["status"] in ("OPEN", "ACKNOWLEDGED") and ex["closure"] != "OPEN_LIKE":
                    self.conn.execute("UPDATE alert SET status=?, resolved_on=? WHERE alert_id=?", (ex["closure"], ex["closed_on"].isoformat(), a["alert_id"]))
                    self._alert_event(a["alert_id"], a["customer_id"], ex["closure"], ex["closed_on"], "USR-SYS-000")
                    closed += 1
            # a tighter lead time replaces the earlier escalation for the same document expiry (closed, never deleted)
            for ex in expected:
                if ex["alert_type"] != "DOCUMENT_EXPIRING":
                    continue
                prefix = ex["dedupe_key"].rsplit("|", 1)[0] + "|"
                for a in self.q("SELECT * FROM alert WHERE dedupe_key LIKE ? AND status IN ('OPEN','ACKNOWLEDGED') AND lead_days>?", (prefix + "%", ex["lead_days"])):
                    self.conn.execute("UPDATE alert SET status='SUPERSEDED', resolved_on=? WHERE alert_id=?", (ex["trigger_date"].isoformat(), a["alert_id"]))
                    self._alert_event(a["alert_id"], a["customer_id"], "SUPERSEDED", ex["trigger_date"], "USR-SYS-000")
                    closed += 1
            return {"alerts_created": created, "alerts_closed": closed, "expected_alerts": len(expected)}
        return self.tx(run)

    def daily_run(self, actor_id):
        """Business-date batch: refresh document statuses, re-evaluate every customer, scan alerts."""
        self.actor(actor_id, [])

        def run():
            changed = decided = 0
            for c in self.q("SELECT customer_id FROM customer ORDER BY customer_id"):
                f = self._file(c["customer_id"])
                before = {d["document_id"]: d["status"] for d in f["docs"]}
                self._refresh_statuses(f)
                changed += sum(1 for d in f["docs"] if before[d["document_id"]] != d["status"])
                decided += 1 if self._decide(f, "USR-SYS-000") else 0
            return {"document_statuses_updated": changed, "decisions_recorded": decided}
        out = self.tx(run)
        out.update(self.scan_alerts(actor_id))
        out["as_of_date"] = self.as_of.isoformat()
        return out
