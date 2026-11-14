"""Orchestration: generate -> inject -> label.  Returns operational tables and (separate) ground truth."""
from __future__ import annotations

from datetime import date

from . import anomalies, generator as g
from .common import Ids, load_generation_cfg, load_policy, parse_date
from .schema import build_tables


def resolve_params(profile: str, gen: dict, overrides: dict | None = None) -> dict:
    if profile not in gen["profiles"]:
        raise ValueError(f"unknown profile {profile!r}; choose from {sorted(gen['profiles'])}")
    P = dict(gen["profiles"][profile])
    for k, v in (overrides or {}).items():
        if v is not None:
            P[k] = int(v)
    return P


def generate_dataset(profile="full", seed=None, as_of=None, overrides=None):
    policy, gen = load_policy(), load_generation_cfg()
    seed = gen["default_seed"] if seed is None else int(seed)
    as_of = parse_date(as_of or gen["default_as_of_date"])
    P = resolve_params(profile, gen, overrides)
    ids = Ids(policy["id_year"])
    vcfg = policy["verification"]

    ctype, dtype, kyc, staff = g.build_reference(policy, ids, gen)
    customers, ratings = g.build_customers(P, policy, gen, ids, as_of, seed)
    plans, order = g.build_documents(P, policy, gen, customers, as_of, seed, vcfg)
    docs = g.materialise_documents(plans, order, customers, ids, as_of, seed)
    vers = g.build_verifications(docs, customers, staff, ids, as_of, seed, vcfg)
    g.refresh_document_status(docs, vers, as_of)
    owners, links = g.build_ownership(customers, policy, gen, ids, as_of, seed)
    decisions = g.build_decisions(customers, ratings, docs, vers, links, kyc, staff, policy, ids, as_of, seed)
    reviews, revents = g.build_reviews(P, customers, staff, policy, gen, ids, as_of, seed)
    alerts, aevents = g.build_alerts(docs, vers, reviews, staff, policy, gen, ids, as_of, seed)

    T = {"customer_type": ctype, "staff_user": staff, "document_type": dtype, "kyc_requirement": kyc,
         "customer": customers, "risk_rating": ratings, "document": docs, "verification": vers,
         "beneficial_owner": owners, "ownership_link": links, "customer_decision": decisions,
         "review_case": reviews, "review_case_event": revents, "alert": alerts, "alert_event": aevents}

    truth = anomalies.inject(T, kyc, policy, gen, P, ids, as_of, seed)
    g.sync_customer_projection(customers, ratings, T["customer_decision"])
    T["audit_event"] = g.build_audit(T, ids)
    expected = anomalies.expected_outcomes(T, kyc, policy, as_of)
    meta = dict(profile=profile, seed=seed, as_of_date=as_of.isoformat(), params=P,
                policy_version=policy["policy_version"])
    return T, truth, expected, meta


def tables_spec():
    return build_tables(load_policy())
