"""Deterministic synthetic data generator (parent tables first, then dependents).

Phases (each with its own seeded RNG stream, so adding a phase never perturbs another):
  1 reference/master  2 customers+risk  3 documents (plan -> reconcile volume -> materialise)
  4 verifications     5 beneficial owners  6 decisions (event-sourced state machine)
  7 reviews           8 alerts             9 audit trail
Anomaly injection and ground truth live in anomalies.py and run AFTER the clean baseline.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta

from . import rules
from .common import Ids, add_months, rand_date, rng_for
from .names import BUS_A, BUS_B, BUS_SUFFIX, FIRST, LAST

DOC_ORDER_KEY = "document_type_code"


class GenerationError(RuntimeError):
    pass


# ----------------------------------------------------------------- helpers
def _wchoice(r, weights: dict):
    keys = sorted(weights)
    return r.choices(keys, [weights[k] for k in keys])[0]


def _score_level(score, thresholds):
    if score >= thresholds["HIGH"]:
        return "HIGH"
    return "MEDIUM" if score >= thresholds["MEDIUM"] else "LOW"


def _tod(entity_id, action, d) -> str:
    h = int(hashlib.sha1(f"{entity_id}|{action}|{d}".encode()).hexdigest()[:8], 16) % 86400
    return f"{d.isoformat()}T{h // 3600:02d}:{(h % 3600) // 60:02d}:{h % 60:02d}Z"


# ------------------------------------------------------------------ phase 1
def build_reference(policy, ids, gen):
    ctype = [dict(r) for r in policy["customer_types"]]
    dtype = [dict(document_type_code=d["document_type_code"], description=d["description"],
                  validity_days=d["validity_days"]) for d in policy["document_types"]]
    kyc, n = [], 0
    for ct in policy["customer_types"]:
        for risk in policy["risk_levels"]:
            for dt in policy["requirements"][ct["customer_type_id"]][risk]:
                n += 1
                kyc.append(dict(kyc_requirement_id=ids.kyc_requirement(n), customer_type_id=ct["customer_type_id"],
                                risk_level=risk, document_type_code=dt, mandatory=1,
                                policy_version=policy["policy_version"],
                                effective_from=date.fromisoformat(policy["policy_effective_from"]), effective_to=None))
    staff = [dict(user_id="USR-SYS-000", role="SYSTEM", display_name="Automated KYC Processor")]
    sc = gen["staff"]
    for i in range(1, sc["operations_analysts"] + 1):
        staff.append(dict(user_id=f"USR-OPS-{i:03d}", role="OPERATIONS_ANALYST", display_name=f"{FIRST[(i * 7) % len(FIRST)]} {LAST[(i * 11) % len(LAST)]}"))
    for i in range(1, sc["compliance_reviewers"] + 1):
        staff.append(dict(user_id=f"USR-CMP-{i:03d}", role="RISK_COMPLIANCE_REVIEWER", display_name=f"{FIRST[(i * 13 + 3) % len(FIRST)]} {LAST[(i * 17 + 5) % len(LAST)]}"))
    for i in range(1, sc["administrators"] + 1):
        staff.append(dict(user_id=f"USR-ADM-{i:03d}", role="SYSTEM_ADMINISTRATOR", display_name=f"{FIRST[(i * 19 + 9) % len(FIRST)]} {LAST[(i * 23 + 1) % len(LAST)]}"))
    return ctype, dtype, kyc, staff


# ------------------------------------------------------------------ phase 2
def build_customers(P, policy, gen, ids, as_of, seed):
    r = rng_for(seed, "customers")
    cc = gen["customers"]
    n, nb = P["customers"], P["businesses"]
    if nb > n:
        raise GenerationError("businesses cannot exceed customers")
    biz = set(r.sample(range(1, n + 1), nb))
    used, customers, ratings = set(), [], []
    th = policy["risk_score_thresholds"]
    rr = 0
    for i in range(1, n + 1):
        cid, is_biz = ids.customer(i), i in biz
        ctype = "BUSINESS" if is_biz else "INDIVIDUAL"
        for attempt in range(50):
            if is_biz:
                name = f"{r.choice(BUS_A)} {r.choice(BUS_B)} {r.choice(BUS_SUFFIX)}"
            else:
                mid = f" {chr(65 + r.randint(0, 25))}" if r.random() < 0.2 else ""
                name = f"{r.choice(FIRST)}{mid} {r.choice(LAST)}"
            if name not in used:
                break
        else:  # pool exhausted -> deterministic disambiguation
            name = f"{name} {i}" if is_biz else f"{name} {chr(65 + i % 26)}"
        used.add(name)
        country = _wchoice(r, cc["country_weights"])
        onboarding = as_of - timedelta(days=r.randint(cc["onboarding_min_age_days"], cc["onboarding_max_age_days"]))
        if is_biz:
            birth = onboarding - timedelta(days=r.randint(365, 9000))
        else:
            birth = onboarding - timedelta(days=int(365.25 * r.randint(21, 78)) + r.randint(0, 364))
        comp = {"country": cc["country_points"][country]}
        if is_biz:
            comp["business_base"] = cc["business_base_points"]
            comp["complex_structure"] = cc["complex_structure_points"] if r.random() < cc["complex_structure_prob"] else 0
        else:
            comp["pep"] = cc["pep_points"] if r.random() < cc["pep_prob"] else 0
        comp["adverse_media"] = cc["adverse_media_points"] if r.random() < cc["adverse_media_prob"] else 0
        comp["cash_intensive"] = cc["cash_intensive_points"] if r.random() < cc["cash_intensive_prob"][ctype] else 0
        score = sum(comp.values())
        level = _score_level(score, th)
        # evidence collection date for the current KYC cycle (see docs/DESIGN.md)
        lo = max(onboarding + timedelta(days=1), as_of - timedelta(days=cc["evidence_window_days"]))
        base = rand_date(r, lo, as_of - timedelta(days=3))
        cust = dict(customer_id=cid, customer_type_id=ctype, legal_name=name, country_code=country,
                    birth_or_incorporation_date=birth, onboarding_date=onboarding, risk_level=level,
                    status="PENDING", status_since=onboarding, _base=base)
        customers.append(cust)
        rr += 1
        first = dict(risk_rating_id=ids.risk_rating(rr), customer_id=cid, risk_score=score, risk_level=level,
                     score_components=";".join(f"{k}={v}" for k, v in comp.items()), rating_reason="INITIAL",
                     effective_from=onboarding, effective_to=None)
        ratings.append(first)
        # optional re-rating before the evidence cycle (keeps requirement set stable afterwards)
        win_lo, win_hi = onboarding + timedelta(days=30), base - timedelta(days=1)
        if win_hi > win_lo and r.random() < cc["rerating_prob"]:
            comp2 = dict(comp)
            comp2["adverse_media"] = 0 if comp["adverse_media"] else cc["adverse_media_points"]
            s2 = sum(comp2.values())
            l2 = _score_level(s2, th)
            if l2 != level:
                when = rand_date(r, win_lo, win_hi)
                first["effective_to"] = when
                rr += 1
                ratings.append(dict(risk_rating_id=ids.risk_rating(rr), customer_id=cid, risk_score=s2, risk_level=l2,
                                    score_components=";".join(f"{k}={v}" for k, v in comp2.items()),
                                    rating_reason="RE_RATING", effective_from=when, effective_to=None))
                cust["risk_level"] = l2
    return customers, ratings


# ------------------------------------------------------------------ phase 3
def _name_variant(r, name, is_biz, kind):
    if kind == "benign":
        if is_biz:
            for a, b in (("Pvt Ltd", "Private Limited"), ("Ltd", "Limited"), ("Co", "Company")):
                if name.endswith(" " + a):
                    return name[: -len(a)] + b
        return name.upper()
    parts = name.split()
    if is_biz:
        opts = [" ".join(parts[:-1]), f"{parts[0]} {r.choice(BUS_B)} {parts[-1]}", name.replace(parts[1], parts[1][:-1])]
    else:
        last = parts[-1]
        typo = last[1] + last[0] + last[2:] if len(last) > 2 and last[0] != last[1] else last[:-1]
        opts = [f"{last} {parts[0]}", f"{parts[0][0]} {last}", " ".join(parts[:-1] + [typo]), " ".join(parts[:-1] + [r.choice(LAST)])]
    out = r.choice(opts)
    return out if rules.normalize_name(out) != rules.normalize_name(name) else name + " X"


def build_documents(P, policy, gen, customers, as_of, seed, vcfg):
    """Plan evidence per customer, reconcile to the exact target volume, return finalised docs (no ids yet)."""
    r = rng_for(seed, "documents")
    dc = gen["documents"]
    vdays = {d["document_type_code"]: d["validity_days"] for d in policy["document_types"]}
    order = {d["document_type_code"]: i for i, d in enumerate(policy["document_types"])}
    plans, seq = [], 0

    def new_doc(cust, dtype, submitted, kind, supersedes, clean):
        nonlocal seq
        seq += 1
        v = vdays[dtype]
        if clean:
            age = r.randint(0, 30)
        else:
            age = r.randint(0, dc["issue_age_max_days"][dtype])
            if v and v >= 1000 and r.random() < dc["expired_at_submission_prob"]:
                age = v + r.randint(1, 400)
        issue = submitted - timedelta(days=age)
        name, is_biz = cust["legal_name"], cust["customer_type_id"] == "BUSINESS"
        if not clean:
            x = r.random()
            if x < dc["name_variant_prob"]["mismatch"]:
                name = _name_variant(r, name, is_biz, "mismatch")
            elif x < dc["name_variant_prob"]["mismatch"] + dc["name_variant_prob"]["benign"]:
                name = _name_variant(r, name, is_biz, "benign")
        q = int(round(r.gauss(dc["quality_mean"], dc["quality_sd"])))
        q = max(60 if clean else 1, min(100, q))
        d = dict(_key=seq, customer_id=cust["customer_id"], document_type_code=dtype, name_on_document=name,
                 issue_date=issue, expiry_date=(issue + timedelta(days=v)) if v else None, submitted_on=submitted,
                 scan_quality_score=q, supersedes_key=supersedes, _kind=kind, _vdelay=r.randint(*dc["verify_delay_days"]))
        outcome, _ = rules.verification_outcome(d, cust["legal_name"], vcfg)
        d["_outcome"] = outcome
        extra = 2 if (outcome == "VERIFIED" and q < vcfg["inconclusive_below_quality"]) else 0
        d["_final_verify"] = submitted + timedelta(days=d["_vdelay"] + extra)
        plans.append(d)
        return d

    for cust in customers:
        types = policy["requirements"][cust["customer_type_id"]][cust["risk_level"]]
        omit = r.choice(types) if r.random() < dc["incomplete_customer_prob"] else None
        for dtype in types:
            if dtype == omit:
                continue
            sub = min(as_of, cust["_base"] + timedelta(days=r.randint(0, dc["submit_spread_days"])))
            d = new_doc(cust, dtype, sub, "ORIGINAL", None, False)
            depth = 0
            while d["_outcome"] == "REJECTED" and depth < 3 and r.random() < dc["resubmit_after_reject_prob"]:
                nxt = d["_final_verify"] + timedelta(days=r.randint(2, 20))
                if nxt > as_of:
                    break
                d = new_doc(cust, dtype, nxt, "RESUBMISSION", d["_key"], True)
                depth += 1
            if d["_outcome"] != "VERIFIED" or d["expiry_date"] is None:
                continue
            E = d["expiry_date"]
            if E < as_of:
                if r.random() < dc["renew_expired_prob"]:
                    s2 = max(d["submitted_on"] + timedelta(days=1), E + timedelta(days=r.randint(-30, 25)))
                    if s2 <= as_of:
                        new_doc(cust, dtype, s2, "RENEWAL", d["_key"], True)
            elif r.random() < dc["early_renew_prob"]:
                lo, hi = d["submitted_on"] + timedelta(days=30), min(as_of - timedelta(days=1), E - timedelta(days=1))
                if hi >= lo:
                    new_doc(cust, dtype, rand_date(r, lo, hi), "RENEWAL", d["_key"], True)

    # ---- reconcile to the exact target volume (documented in DESIGN.md)
    target = P["documents"]
    by_cust = {c["customer_id"]: c for c in customers}
    children = {}
    for d in plans:
        if d["supersedes_key"]:
            children.setdefault(d["supersedes_key"], []).append(d["_key"])
    if len(plans) > target:
        need = len(plans) - target
        removed = set()
        for kind in ("RENEWAL", "RESUBMISSION", "ORIGINAL"):
            if need == 0:
                break
            pool = [d for d in plans if d["_kind"] == kind and d["_key"] not in children]
            r.shuffle(pool)
            for d in pool[:need]:
                removed.add(d["_key"])
            need -= min(need, len(pool))
        if need:
            raise GenerationError("cannot reduce documents to target; lower incomplete/renewal probabilities")
        plans = [d for d in plans if d["_key"] not in removed]
    elif len(plans) < target:
        need = target - len(plans)
        hosts = [d for d in plans if d["_kind"] == "ORIGINAL" and d["supersedes_key"] is None
                 and vdays[d["document_type_code"]] and vdays[d["document_type_code"]] <= 400
                 and by_cust[d["customer_id"]]["onboarding_date"] + timedelta(days=200) <= d["submitted_on"] - timedelta(days=200)
                 and d["_outcome"] == "VERIFIED"]
        hosts.sort(key=lambda d: d["_key"])
        r.shuffle(hosts)
        if len(hosts) < need:
            raise GenerationError(f"not enough hosts ({len(hosts)}) to add {need} prior-cycle documents")
        for h in hosts[:need]:
            cust = by_cust[h["customer_id"]]
            sub = h["submitted_on"] - timedelta(days=r.randint(200, 360))
            sub = max(sub, cust["onboarding_date"])
            old = new_doc(cust, h["document_type_code"], sub, "PRIOR", None, True)
            h["supersedes_key"] = old["_key"]
    plans.sort(key=lambda d: d["_key"])
    return plans, order


def materialise_documents(plans, order, customers, ids, as_of, seed):
    """Assign DOC-<customer>-nnn ids in (submitted_on, type, key) order and fingerprints."""
    by_cust = {}
    for d in plans:
        by_cust.setdefault(d["customer_id"], []).append(d)
    key_to_id, docs = {}, []
    for cid in sorted(by_cust):
        lst = sorted(by_cust[cid], key=lambda d: (d["submitted_on"], order[d["document_type_code"]], d["_key"]))
        for n, d in enumerate(lst, 1):
            key_to_id[d["_key"]] = ids.document(cid, n)
    for d in plans:
        did = key_to_id[d["_key"]]
        fp = hashlib.sha256(f"SYN|{seed}|{did}|{d['document_type_code']}".encode()).hexdigest()
        docs.append(dict(document_id=did, customer_id=d["customer_id"], document_type_code=d["document_type_code"],
                         name_on_document=d["name_on_document"], document_fingerprint=fp,
                         issue_date=d["issue_date"], expiry_date=d["expiry_date"], submitted_on=d["submitted_on"],
                         scan_quality_score=d["scan_quality_score"], status="PENDING_VERIFICATION",
                         supersedes_document_id=key_to_id[d["supersedes_key"]] if d["supersedes_key"] else None,
                         _plan=d))
    docs.sort(key=lambda d: d["document_id"])
    return docs


# ------------------------------------------------------------------ phase 4
def build_verifications(docs, customers, staff, ids, as_of, seed, vcfg):
    r = rng_for(seed, "verifications")
    cust = {c["customer_id"]: c for c in customers}
    analysts = sorted(s["user_id"] for s in staff if s["role"] == "OPERATIONS_ANALYST")
    raw = []
    for d in docs:
        p = d["_plan"]
        outcome, reason = rules.verification_outcome(d, cust[d["customer_id"]]["legal_name"], vcfg)
        sub = d["submitted_on"]
        attempts = []
        if outcome == "VERIFIED" and d["scan_quality_score"] < vcfg["inconclusive_below_quality"]:
            attempts.append((sub + timedelta(days=1), "INCONCLUSIVE", "LOW_CONFIDENCE"))
            attempts.append((p["_final_verify"], "VERIFIED", "OK"))
        else:
            attempts.append((p["_final_verify"], outcome, reason))
        for n, (vd, oc, rs) in enumerate(attempts, 1):
            if vd > as_of:
                continue
            auto = oc == "VERIFIED" and len(attempts) == 1 and d["scan_quality_score"] >= vcfg["auto_verify_min_quality"]
            raw.append(dict(document_id=d["document_id"], attempt_no=n, verified_on=vd, outcome=oc,
                            reason_code=rs, verified_by="USR-SYS-000" if auto else r.choice(analysts)))
    raw.sort(key=lambda v: (v["verified_on"], v["document_id"], v["attempt_no"]))
    for i, v in enumerate(raw, 1):
        v["verification_id"] = ids.verification(i)
    return raw


def refresh_document_status(docs, vers, as_of):
    vers_by = group(vers, "document_id")
    by_id = {d["document_id"]: d for d in docs}
    sup = rules.superseded_verified_ids(by_id, vers_by, as_of)
    for d in docs:
        d["status"] = rules.derive_document_status(d, vers_by.get(d["document_id"], []), d["document_id"] in sup, as_of)


def group(rows, key):
    out = {}
    for x in rows:
        out.setdefault(x[key], []).append(x)
    return out


# ------------------------------------------------------------------ phase 5
def build_ownership(customers, policy, gen, ids, as_of, seed):
    r = rng_for(seed, "ownership")
    oc = gen["ownership"]
    owners, links = [], []
    pool = []
    minbps = int(oc["min_share_pct"] * 100)
    for c in customers:
        if c["customer_type_id"] != "BUSINESS":
            continue
        if r.random() < oc["missing_prob"]:
            continue
        k = int(_wchoice(r, oc["owner_count_weights"]))
        chosen = []
        for _ in range(k):
            if pool and r.random() < oc["reuse_existing_owner_prob"]:
                cand = r.choice(pool)
                if cand not in chosen:
                    chosen.append(cand)
                    continue
            o = dict(owner_id=ids.owner(len(owners) + 1),
                     full_name=f"{r.choice(FIRST)} {r.choice(LAST)}",
                     date_of_birth=date(1950, 1, 1) + timedelta(days=r.randint(0, 17000)),
                     nationality=_wchoice(r, gen["customers"]["country_weights"]))
            owners.append(o)
            pool.append(o)
            chosen.append(o)
        remaining = 10000 - minbps * len(chosen)
        cuts = sorted(r.randint(0, remaining) for _ in range(len(chosen) - 1))
        parts = [b - a for a, b in zip([0] + cuts, cuts + [remaining])]
        for o, extra in zip(chosen, parts):
            links.append(dict(ownership_link_id=ids.ownership_link(len(links) + 1), customer_id=c["customer_id"],
                              owner_id=o["owner_id"], ownership_pct=(minbps + extra) / 100.0,
                              effective_from=c["onboarding_date"], effective_to=None))
    return owners, links


# ------------------------------------------------------------------ phase 6
def build_decisions(customers, ratings, docs, vers, links, kyc, staff, policy, ids, as_of, seed):
    r = rng_for(seed, "decisions")
    analysts = sorted(s["user_id"] for s in staff if s["role"] == "OPERATIONS_ANALYST")
    reviewers = sorted(s["user_id"] for s in staff if s["role"] == "RISK_COMPLIANCE_REVIEWER")
    rat, dby, lby = group(ratings, "customer_id"), group(docs, "customer_id"), group(links, "customer_id")
    vers_by = group(vers, "document_id")
    needs_owner = {c["customer_type_id"]: bool(c["requires_beneficial_owner"]) for c in policy["customer_types"]}
    decisions = []

    def snap(c, t):
        return rules.evaluate(c, sorted(rat[c["customer_id"]], key=lambda x: x["effective_from"]), dby.get(c["customer_id"], []),
                              vers_by, lby.get(c["customer_id"], []), kyc, t, needs_owner[c["customer_type_id"]])

    def add(c, t, state, ev, by):
        decisions.append(dict(customer_id=c["customer_id"], decision_date=t, decision=state,
                              required_count=ev["required_count"], satisfied_count=ev["satisfied_count"],
                              completeness_pct=ev["completeness_pct"], reason_codes=ev["reason_codes"],
                              policy_version=policy["policy_version"], decided_by=by))

    for c in customers:
        cid = c["customer_id"]
        ev0 = snap(c, c["onboarding_date"])
        state = "PENDING"
        add(c, c["onboarding_date"], state, ev0, "USR-SYS-000")
        cand = set()
        for d in dby.get(cid, []):
            for v in vers_by.get(d["document_id"], []):
                cand.add(v["verified_on"])
            if d["expiry_date"] is not None:
                cand.add(d["expiry_date"] + timedelta(days=1))
        for t in sorted(x for x in cand if c["onboarding_date"] < x <= as_of):
            ev = snap(c, t)
            if ev["complete"]:
                new = "APPROVED"
            else:
                new = "ON_HOLD" if state in ("APPROVED", "ON_HOLD") else "PENDING"
            if new != state:
                if new == "APPROVED":
                    by = r.choice(reviewers if ev["risk_level"] == "HIGH" else analysts)
                else:
                    by = "USR-SYS-000"
                add(c, t, new, ev, by)
                state = new
    decisions.sort(key=lambda d: (d["decision_date"], d["customer_id"], 0 if d["decision"] == "PENDING" else 1))
    for i, d in enumerate(decisions, 1):
        d["decision_id"] = ids.decision(i)
    sync_customer_projection(customers, ratings, decisions)
    return decisions


def sync_customer_projection(customers, ratings, decisions):
    """customer.risk_level/status/status_since are projections of the latest history rows."""
    last_dec = {}
    for d in decisions:
        k = d["customer_id"]
        if k not in last_dec or (d["decision_date"], d["decision_id"]) > (last_dec[k]["decision_date"], last_dec[k]["decision_id"]):
            last_dec[k] = d
    cur_rat = {x["customer_id"]: x for x in ratings if x["effective_to"] is None}
    for c in customers:
        c["status"] = last_dec[c["customer_id"]]["decision"]
        c["status_since"] = last_dec[c["customer_id"]]["decision_date"]
        c["risk_level"] = cur_rat[c["customer_id"]]["risk_level"]


# ------------------------------------------------------------------ phase 7
def build_reviews(P, customers, staff, policy, gen, ids, as_of, seed):
    r = rng_for(seed, "reviews")
    rc, rg = policy["review"], gen["reviews"]
    year_start, year_end = date(rc["review_year"], 1, 1), date(rc["review_year"], 12, 31)
    reviewers = sorted(s["user_id"] for s in staff if s["role"] == "RISK_COMPLIANCE_REVIEWER")
    cands = []
    for c in customers:
        if c["status"] not in ("APPROVED", "ON_HOLD"):
            continue
        k = 1
        due = add_months(c["onboarding_date"], rc["period_months"][c["risk_level"]])
        while due < year_start:
            k += 1
            due = add_months(c["onboarding_date"], rc["period_months"][c["risk_level"]] * k)
        cands.append((due, c["customer_id"], c))
    cands.sort(key=lambda x: (x[0], x[1]))
    target = P["reviews"]
    if len(cands) < target:
        raise GenerationError(f"only {len(cands)} customers are eligible for review; target is {target}")
    in_year = [x for x in cands if x[0] <= year_end]
    if len(in_year) >= target:
        # review capacity is limited: a seeded random subset of this year's due cohort is scheduled
        chosen = sorted(r.sample(in_year, target), key=lambda x: (x[0], x[1]))
    else:
        # cohort too small: pull forward the earliest next-year dues (documented in DESIGN.md)
        chosen = cands[:target]
    reviews, events = [], []
    for due, cid, c in chosen:
        rid = ids.review(cid, rc["review_year"])
        sched = min(as_of, due - timedelta(days=rc["schedule_lead_days"]))
        sched = max(sched, c["onboarding_date"])
        assigned = r.choice(reviewers)
        rev = dict(review_id=rid, customer_id=cid, review_type="PERIODIC", risk_level_at_open=c["risk_level"],
                   due_date=due, opened_on=None, status="SCHEDULED", assigned_to=assigned, completed_on=None, resolution=None)
        evs = [(None, "SCHEDULED", sched, "USR-SYS-000", "Periodic review scheduled")]
        opened = due - timedelta(days=rc["open_lead_days"])
        if opened <= as_of:
            opened = max(opened, sched)
            rev["opened_on"] = opened
            evs.append(("SCHEDULED", "OPEN", opened, "USR-SYS-000", "Review opened"))
            start = opened + timedelta(days=r.randint(*rg["start_work_days"]))
            if start <= as_of:
                evs.append(("OPEN", "IN_PROGRESS", start, assigned, "Work started"))
                cur, cur_date = "IN_PROGRESS", start
                stuck = r.random() < rg["stuck_prob"]
                escalate = r.random() < rg["escalate_prob"]
                done = start + timedelta(days=r.randint(*rg["work_days"]))
                if escalate and not stuck:
                    esc = start + timedelta(days=r.randint(3, 15))
                    if esc <= as_of:
                        evs.append(("IN_PROGRESS", "ESCALATED", esc, assigned, "Escalated to compliance"))
                        cur, cur_date = "ESCALATED", esc
                        done = esc + timedelta(days=r.randint(*rg["escalated_extra_days"]))
                if not stuck and done <= as_of:
                    on_hold = c["status"] == "ON_HOLD"
                    if on_hold:
                        res = "EVIDENCE_REFRESH_REQUESTED"
                    elif cur == "ESCALATED":
                        res = "ESCALATED_TO_COMPLIANCE"
                    else:
                        res = r.choices(["NO_CHANGE", "EVIDENCE_REFRESH_REQUESTED", "EXIT_RECOMMENDED"], [90, 8, 2])[0]
                    evs.append((cur, "COMPLETED", done, assigned, f"Resolution: {res}"))
                    rev["completed_on"], rev["resolution"] = done, res
        rev["status"] = evs[-1][1]
        reviews.append(rev)
        for fr, to, d, actor, note in evs:
            events.append(dict(review_id=rid, from_status=fr, to_status=to, event_date=d, actor_id=actor, note=note))
    reviews.sort(key=lambda x: x["review_id"])
    events.sort(key=lambda e: (e["event_date"], e["review_id"], 0 if e["from_status"] is None else 1))
    for i, e in enumerate(events, 1):
        e["review_event_id"] = ids.review_event(i)
    return reviews, events


# ------------------------------------------------------------------ phase 8
def build_alerts(docs, vers, reviews, staff, policy, gen, ids, as_of, seed):
    r = rng_for(seed, "alerts")
    ac = gen["alerts"]
    analysts = sorted(s["user_id"] for s in staff if s["role"] == "OPERATIONS_ANALYST")
    expected = rules.derive_alerts(docs, group(vers, "document_id"), reviews, policy["alerts"], as_of)
    alerts, events = [], []
    for n, a in enumerate(expected, 1):
        aid = ids.alert(n)
        trig, closed = a["trigger_date"], a["closed_on"]
        evs = [("CREATED", trig, "USR-SYS-000")]
        horizon = closed if closed else as_of
        status = {"OPEN_LIKE": "OPEN", "RESOLVED": "RESOLVED", "SUPERSEDED": "SUPERSEDED"}[a["closure"]]
        if r.random() < ac["acknowledge_prob"]:
            ack = trig + timedelta(days=r.randint(*ac["acknowledge_after_days"]))
            if ack <= horizon:
                evs.append(("ACKNOWLEDGED", ack, r.choice(analysts)))
                if status == "OPEN":
                    status = "ACKNOWLEDGED"
        if closed:
            evs.append((a["closure"], closed, "USR-SYS-000" if a["closure"] == "SUPERSEDED" else r.choice(analysts)))
        alerts.append(dict(alert_id=aid, alert_type=a["alert_type"], customer_id=a["customer_id"],
                           document_id=a["document_id"], review_id=a["review_id"], lead_days=a["lead_days"],
                           severity=rules.alert_severity(policy["alerts"], a["alert_type"], a["lead_days"]),
                           trigger_date=trig, status=status, dedupe_key=a["dedupe_key"], resolved_on=closed))
        for et, d, actor in evs:
            events.append(dict(alert_id=aid, event_type=et, event_date=d, actor_id=actor))
    events.sort(key=lambda e: (e["event_date"], e["alert_id"], ["CREATED", "ACKNOWLEDGED", "RESOLVED", "SUPERSEDED"].index(e["event_type"])))
    for i, e in enumerate(events, 1):
        e["alert_event_id"] = ids.alert_event(i)
    return alerts, events


# ------------------------------------------------------------------ phase 9
def build_audit(T, ids):
    """Audit trail assembled from the FINAL tables, so it also covers any injected records."""
    ev = []
    sysish = lambda u: "SYSTEM" if u == "USR-SYS-000" else "STAFF"

    def add(d, etype, eid, cid, action, atype, aid, details=""):
        ev.append((_tod(eid, action, d), etype, eid, cid, action, atype, aid, details))

    for c in T["customer"]:
        add(c["onboarding_date"], "customer", c["customer_id"], c["customer_id"], "CUSTOMER_ONBOARDED", "CUSTOMER", c["customer_id"], f"type={c['customer_type_id']}")
    for x in T["risk_rating"]:
        add(x["effective_from"], "risk_rating", x["risk_rating_id"], x["customer_id"], "RISK_RATED", "SYSTEM", "USR-SYS-000", f"level={x['risk_level']};score={x['risk_score']}")
    docs = {d["document_id"]: d for d in T["document"]}
    for d in docs.values():
        add(d["submitted_on"], "document", d["document_id"], d["customer_id"], "DOCUMENT_SUBMITTED", "CUSTOMER", d["customer_id"], f"type={d['document_type_code']}")
    for v in T["verification"]:
        add(v["verified_on"], "verification", v["verification_id"], docs[v["document_id"]]["customer_id"], "VERIFICATION_RECORDED",
            sysish(v["verified_by"]), v["verified_by"], f"outcome={v['outcome']};reason={v['reason_code']}")
    for l in T["ownership_link"]:
        add(l["effective_from"], "ownership_link", l["ownership_link_id"], l["customer_id"], "OWNERSHIP_RECORDED", "CUSTOMER", l["customer_id"], f"pct={l['ownership_pct']:.2f}")
    for d in T["customer_decision"]:
        add(d["decision_date"], "customer_decision", d["decision_id"], d["customer_id"], "DECISION_RECORDED",
            sysish(d["decided_by"]), d["decided_by"], f"decision={d['decision']};completeness={d['completeness_pct']:.2f}")
    rev = {x["review_id"]: x for x in T["review_case"]}
    for e in T["review_case_event"]:
        add(e["event_date"], "review_case_event", e["review_event_id"], rev[e["review_id"]]["customer_id"], f"REVIEW_{e['to_status']}",
            sysish(e["actor_id"]), e["actor_id"], f"review={e['review_id']}")
    alt = {x["alert_id"]: x for x in T["alert"]}
    for e in T["alert_event"]:
        add(e["event_date"], "alert_event", e["alert_event_id"], alt[e["alert_id"]]["customer_id"], f"ALERT_{e['event_type']}",
            sysish(e["actor_id"]), e["actor_id"], f"alert={e['alert_id']}")
    ev.sort()
    return [dict(audit_event_id=ids.audit(i), event_ts=ts, entity_type=et, entity_id=eid, customer_id=cid, action=act,
                 actor_type=at, actor_id=aid, details=det) for i, (ts, et, eid, cid, act, at, aid, det) in enumerate(ev, 1)]
