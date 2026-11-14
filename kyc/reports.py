"""Measurable outputs: distribution metrics, validation summary, data dictionary, source manifest."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path

from . import rules
from .common import CONFIG_DIR
from .generator import group


def _dist(counter, total=None):
    total = total or sum(counter.values()) or 1
    return {k: {"count": v, "pct": round(100.0 * v / total, 2)} for k, v in sorted(counter.items())}


def _pcts(d):
    return ", ".join("%s %s%%" % (k, v["pct"]) for k, v in d.items())


def _counts(d):
    return ", ".join("%s %s" % (k, v["count"]) for k, v in d.items())


def compute_metrics(T, as_of: date, policy) -> dict:
    C = Counter
    cust = T["customer"]
    docs = T["document"]
    ver = T["verification"]
    ok_reasons = C(v["reason_code"] for v in ver)
    final_rej = [v for v in ver if v["outcome"] == "REJECTED"]
    biz = [c for c in cust if c["customer_type_id"] == "BUSINESS"]
    links_by = group(T["ownership_link"], "customer_id")
    bps = {}
    for cid in (c["customer_id"] for c in biz):
        bps[cid] = rules.ownership_state(links_by.get(cid, []), as_of)
    dec_latest = {}
    for d in T["customer_decision"]:
        k = d["customer_id"]
        if k not in dec_latest or (d["decision_date"], d["decision_id"]) > (dec_latest[k]["decision_date"], dec_latest[k]["decision_id"]):
            dec_latest[k] = d
    bucket = C()
    for d in dec_latest.values():
        p = d["completeness_pct"]
        bucket["100%" if p == 100 else ("75-99%" if p >= 75 else ("50-74%" if p >= 50 else "<50%"))] += 1
    overdue = [r for r in T["review_case"] if rules.is_overdue_review(r, as_of)]
    return {
        "as_of_date": as_of.isoformat(),
        "row_counts": {k: len(v) for k, v in T.items()},
        "customers": {
            "by_type": _dist(C(c["customer_type_id"] for c in cust)),
            "by_risk": _dist(C(c["risk_level"] for c in cust)),
            "by_status": _dist(C(c["status"] for c in cust)),
            "by_country": _dist(C(c["country_code"] for c in cust)),
            "latest_decision_completeness": _dist(bucket),
        },
        "documents": {
            "by_type": _dist(C(d["document_type_code"] for d in docs)),
            "by_status": _dist(C(d["status"] for d in docs)),
            "superseded_chain_links": sum(1 for d in docs if d["supersedes_document_id"]),
            "expired_documents": sum(1 for d in docs if d["status"] == "EXPIRED"),
            "avg_documents_per_customer": round(len(docs) / max(len(cust), 1), 3),
        },
        "verification": {
            "attempts": len(ver),
            "by_outcome": _dist(C(v["outcome"] for v in ver)),
            "by_reason": _dist(ok_reasons),
            "name_mismatch_rejections": ok_reasons.get("NAME_MISMATCH", 0),
            "name_mismatch_rate_pct": round(100.0 * ok_reasons.get("NAME_MISMATCH", 0) / max(len(docs), 1), 2),
            "rejections_total": len(final_rej),
        },
        "ownership": {
            "business_customers": len(biz),
            "with_ownership_records": sum(1 for c in biz if bps[c["customer_id"]][1] > 0),
            "missing_ownership_records": sum(1 for c in biz if bps[c["customer_id"]][1] == 0),
            "reconciling_to_100": sum(1 for c in biz if bps[c["customer_id"]] [0] == 10000),
            "over_100": sum(1 for c in biz if bps[c["customer_id"]][0] > 10000),
            "owners_total": len(T["beneficial_owner"]),
            "owners_in_multiple_businesses": sum(1 for n in C(l["owner_id"] for l in T["ownership_link"]).values() if n > 1),
        },
        "reviews": {
            "by_status": _dist(C(r["status"] for r in T["review_case"])),
            "overdue_total": len(overdue),
            "overdue_still_open": sum(1 for r in overdue if r["status"] != "COMPLETED"),
            "completed_late": sum(1 for r in overdue if r["status"] == "COMPLETED"),
            "resolution_mix": _dist(C(r["resolution"] or "NONE" for r in T["review_case"] if r["status"] == "COMPLETED")),
        },
        "alerts": {
            "by_type": _dist(C(a["alert_type"] for a in T["alert"])),
            "by_status": _dist(C(a["status"] for a in T["alert"])),
            "expiring_by_lead_days": _dist(C(str(a["lead_days"]) for a in T["alert"] if a["alert_type"] == "DOCUMENT_EXPIRING")),
            "configured_lead_days": policy["alerts"]["expiry_lead_days"],
        },
        "audit": {"events": len(T["audit_event"]), "by_action": _dist(C(a["action"] for a in T["audit_event"]))},
    }


def summary_markdown(meta, counts, metrics, findings, evaluation, repro=None) -> str:
    cat = Counter(f["category"] for f in findings)
    lines = [f"# Validation summary - profile `{meta['profile']}`", "",
             f"* seed: `{meta['seed']}`  * as-of date: `{meta['as_of_date']}`  * policy version: `{meta['policy_version']}`",
             f"* **Result: {'PASS' if evaluation['passed'] else 'FAIL'}**", "",
             "## Row counts", "", "| table | rows |", "|---|---:|"]
    lines += [f"| {k} | {v:,} |" for k, v in counts.items()]
    lines += ["", "## Checks executed", "",
              "Primary-key uniqueness and id formats; foreign keys (declared + polymorphic audit/alert references); required values; allowed enums; "
              "date ordering; review/alert lifecycle transitions; projection consistency (customer risk/status vs history); "
              "arithmetic reconciliation (decision completeness, risk score, ownership totals); verification-rule and document-status recomputation; "
              "alert reconciliation against configured expiry lead times; duplicate detection.", "",
              "## Findings by category", "", "| category | findings | expectation |", "|---|---:|---|",
              f"| INTEGRITY | {cat.get('INTEGRITY', 0)} | must be 0 |",
              f"| RECONCILIATION | {cat.get('RECONCILIATION', 0)} | must be 0 |",
              f"| BUSINESS_RULE | {cat.get('BUSINESS_RULE', 0)} | equals injected anomalies |", "",
              "## Injected edge cases vs detection (evaluation step; ground truth used only here)", "",
              "| issue type | injected | detected | missed | unexpected | precision | recall |", "|---|---:|---:|---:|---:|---:|---:|"]
    for k, v in evaluation["per_class"].items():
        lines.append(f"| {k} | {v['injected']} | {v['detected']} | {v['missed']} | {v['unexpected']} | {v['precision']} | {v['recall']} |")
    lines += ["", f"Overall precision **{evaluation['overall_precision']}**, recall **{evaluation['overall_recall']}**, "
              f"integrity/reconciliation defects **{evaluation['integrity_or_reconciliation_defects']}**.", "",
              "## Key distributions", "",
              "* customers by status: " + _pcts(metrics["customers"]["by_status"]),
              "* customers by risk: " + _pcts(metrics["customers"]["by_risk"]),
              "* documents by status: " + _pcts(metrics["documents"]["by_status"]),
              f"* name-mismatch rejections: {metrics['verification']['name_mismatch_rejections']} ({metrics['verification']['name_mismatch_rate_pct']}% of documents)",
              f"* business customers missing ownership records: {metrics['ownership']['missing_ownership_records']} of {metrics['ownership']['business_customers']}",
              f"* overdue reviews: {metrics['reviews']['overdue_total']} ({metrics['reviews']['overdue_still_open']} still open, {metrics['reviews']['completed_late']} completed late)",
              "* alerts: " + _counts(metrics["alerts"]["by_type"]), ""]
    return "\n".join(lines)


def data_dictionary_markdown(tables) -> str:
    out = ["# Data dictionary", "",
           "Generated from `kyc/schema.py` (single source of truth for DDL, CSV headers and validation). "
           "Dates are ISO-8601 `yyyy-mm-dd`; `NULL` is an empty CSV cell / JSON `null`.", ""]
    out += ["## Relationship summary", "", "| table | description | cardinality / relationships | primary key |", "|---|---|---|---|"]
    for t in tables:
        out.append(f"| `{t['name']}` | {t['desc']} | {t['rel']} | `{', '.join(t['pk'])}` |")
    for t in tables:
        out += ["", f"## `{t['name']}`", "", t["desc"], "", f"*Primary key:* `{', '.join(t['pk'])}`"
                + (f"  *Unique:* {'; '.join('(' + ', '.join(u) + ')' for u in t['uniques'])}" if t["uniques"] else ""), "",
                "| field | type | required | allowed values | unit | unique | validation rule | description |", "|---|---|---|---|---|---|---|---|"]
        for c in t["cols"]:
            allowed = c["allowed"] if isinstance(c["allowed"], str) else (", ".join(map(str, c["allowed"])) if c["allowed"] else "")
            fk = f" FK -> `{c['fk']}`." if c["fk"] else ""
            out.append(f"| `{c['name']}` | {c['type']} | {'yes' if c['required'] else 'no'} | {allowed} | {c['unit']} | "
                       f"{'yes' if (c['unique'] or t['pk'] == [c['name']]) else 'no'} | {c['rule']}{fk} | {c['desc']} |")
        if t["checks"]:
            out += ["", "Table checks: " + "; ".join(f"`{c}`" for c in t["checks"])]
    return "\n".join(out) + "\n"


def data_dictionary_csv(tables, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["table", "field", "type", "required", "allowed_values", "unit", "unique", "validation_rule", "foreign_key", "description"])
        for t in tables:
            for c in t["cols"]:
                allowed = c["allowed"] if isinstance(c["allowed"], str) else "|".join(map(str, c["allowed"])) if c["allowed"] else ""
                w.writerow([t["name"], c["name"], c["type"], "yes" if c["required"] else "no", allowed, c["unit"],
                            "yes" if (c["unique"] or t["pk"] == [c["name"]]) else "no", c["rule"], c["fk"] or "", c["desc"]])


def source_manifest(meta, command: str) -> dict:
    def h(p):
        return hashlib.sha256((CONFIG_DIR / p).read_bytes()).hexdigest()

    return {
        "provenance": "100% synthetic. No external or public datasets, no web access, no real personal or confidential data were used.",
        "external_sources": [],
        "licence_notes": "Not applicable (no third-party data). Names are random combinations from small invented pools; any resemblance to real persons or companies is coincidental. "
                         "Identity numbers are never stored - only SHA-256 fingerprints of synthetic strings.",
        "public_reference_use": "Schema inspiration only (generic KYC/CDD concepts: risk-based due diligence, beneficial ownership, periodic review). No data copied.",
        "generator": {"command": command, "seed": meta["seed"], "as_of_date": meta["as_of_date"], "profile": meta["profile"],
                      "parameters": meta["params"], "python_stdlib_only": True},
        "config_files_sha256": {"config/policy.json": h("policy.json"), "config/generation.json": h("generation.json")},
    }


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, sort_keys=True, indent=1) + "\n", encoding="utf-8")
