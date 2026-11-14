"""Command line: one entry point for generate / validate / reproduce / trace."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import export, pipeline, reports, validate as V
from .common import ROOT, load_generation_cfg, load_policy, parse_date
from .schema import build_tables, ddl

DEFAULT_OUT = ROOT / "data"


def _command_string(a) -> str:
    parts = [f"python run.py {a.cmd} --profile {a.profile} --seed {a.seed} --as-of {a.as_of}"]
    for k in ("customers", "businesses", "documents", "reviews"):
        if getattr(a, k, None) is not None:
            parts.append(f"--{k} {getattr(a, k)}")
    return " ".join(parts)


def build(a, out_dir: Path, quiet=False) -> dict:
    """Generate -> export -> validate (from the exported CSV) -> reports -> manifest."""
    policy = load_policy()
    tables = build_tables(policy)
    overrides = {k: getattr(a, k, None) for k in ("customers", "businesses", "documents", "reviews")}
    T, truth, expected, meta = pipeline.generate_dataset(a.profile, a.seed, a.as_of, overrides)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    # 1) the schema must accept the data (PK/FK/CHECK/UNIQUE enforced by SQLite)
    from .store import to_db
    to_db(T, tables).close()
    counts = export.export_operational(T, tables, out_dir)
    export.export_ground_truth(truth, expected, out_dir)
    # 2) validate the exported files, not in-memory state
    findings, evaluation, metrics = validate_dir(out_dir, meta=meta, truth=truth)
    cmd = _command_string(a)
    reports.write_json(out_dir / "reports" / "metrics.json", metrics)
    reports.write_json(out_dir / "reports" / "validation_report.json", {"evaluation": evaluation, "findings": findings})
    (out_dir / "reports" / "validation_summary.md").write_text(reports.summary_markdown(meta, counts, metrics, findings, evaluation), encoding="utf-8")
    export._write_csv(out_dir / "reports" / "validation_findings.csv",
                      ["finding_id", "rule_code", "category", "severity", "entity_type", "entity_id", "related_entity_id", "customer_id", "detail"],
                      [[f[c] for c in ("finding_id", "rule_code", "category", "severity", "entity_type", "entity_id", "related_entity_id", "customer_id", "detail")] for f in findings])
    reports.write_json(out_dir / "source_manifest.json", reports.source_manifest(meta, cmd))
    reports.data_dictionary_csv(tables, out_dir / "data_dictionary.csv")
    sums = export.checksums(out_dir)
    manifest = {"meta": meta, "generator_command": cmd, "row_counts": counts, "ground_truth_rows": len(truth),
                "validation_passed": evaluation["passed"], "sha256": sums}
    reports.write_json(out_dir / "manifest.json", manifest)
    if not quiet:
        print(f"[{a.profile}] seed={meta['seed']} as_of={meta['as_of_date']} -> {out_dir}")
        print("  rows:", ", ".join(f"{k}={v:,}" for k, v in counts.items() if k in ("customer", "document", "review_case", "alert", "audit_event")),
              f"| business={sum(1 for c in T['customer'] if c['customer_type_id'] == 'BUSINESS'):,}")
        print(f"  validation: {'PASS' if evaluation['passed'] else 'FAIL'} | injected={len(truth)} detected={evaluation['true_positives']} "
              f"missed={evaluation['missed']} unexpected={evaluation['unexpected_business_findings']} defects={evaluation['integrity_or_reconciliation_defects']}")
    return manifest


def validate_dir(data_dir: Path, meta=None, truth=None):
    policy = load_policy()
    tables = build_tables(policy)
    if meta is None:
        meta = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))["meta"]
    as_of = parse_date(meta["as_of_date"])
    T = export.read_csv_tables(data_dir, tables)
    findings = V.validate(T, tables, policy, as_of)
    if truth is None:
        truth = json.loads((data_dir / "ground_truth" / "ground_truth.json").read_text(encoding="utf-8"))
    evaluation = V.evaluate_against_truth(findings, truth)
    metrics = reports.compute_metrics(T, as_of, policy)
    return findings, evaluation, metrics


def cmd_validate(a):
    d = Path(a.data)
    findings, evaluation, _ = validate_dir(d)
    from collections import Counter
    print(f"findings by rule: {dict(Counter(f['rule_code'] for f in findings))}")
    print(json.dumps(evaluation, indent=1))
    return 0 if evaluation["passed"] else 1


def cmd_repro(a):
    """Regenerate twice with the same seed (independent processes' worth of state) and compare row counts + checksums."""
    runs = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(2):
            runs.append(build(a, Path(tmp) / f"run{i + 1}", quiet=True))
    m1, m2 = runs
    same_counts = m1["row_counts"] == m2["row_counts"]
    diff = sorted(k for k in m1["sha256"] if m1["sha256"][k] != m2["sha256"].get(k)) + sorted(set(m2["sha256"]) - set(m1["sha256"]))
    ok = same_counts and not diff and m1["sha256"].keys() == m2["sha256"].keys()
    res = {"profile": a.profile, "seed": a.seed, "as_of_date": str(a.as_of), "runs": 2, "row_counts_identical": same_counts,
           "files_compared": len(m1["sha256"]), "files_with_different_checksum": diff, "reproducible": ok,
           "row_counts": m1["row_counts"], "sample_checksums": {k: m1["sha256"][k] for k in sorted(m1["sha256"]) if k.startswith("operational/csv/") and k.split("/")[-1] in ("customer.csv", "document.csv", "audit_event.csv")}}
    print(json.dumps({k: v for k, v in res.items() if k not in ("row_counts", "sample_checksums")}, indent=1))
    if a.write:
        p = Path(a.write)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(res, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if ok else 1


def cmd_trace(a):
    """Show the full audit trail for a customer, built from the exported SQL seed (no extra dependencies)."""
    d = Path(a.data)
    conn = sqlite3.connect(":memory:")
    conn.executescript((d / "operational" / "sql" / "seed.sql").read_text(encoding="utf-8"))
    cid = a.customer
    row = conn.execute("SELECT customer_id, legal_name, customer_type_id, risk_level, status, status_since FROM customer WHERE customer_id=?", (cid,)).fetchone()
    if not row:
        print(f"unknown customer {cid}")
        return 1
    print("CUSTOMER", row)
    sections = [
        ("RISK RATINGS", "SELECT risk_rating_id, risk_score, risk_level, effective_from, effective_to FROM risk_rating WHERE customer_id=? ORDER BY effective_from"),
        ("DOCUMENTS", "SELECT document_id, document_type_code, issue_date, expiry_date, status, supersedes_document_id FROM document WHERE customer_id=? ORDER BY document_id"),
        ("VERIFICATIONS", "SELECT v.verification_id, v.document_id, v.attempt_no, v.verified_on, v.outcome, v.reason_code FROM verification v JOIN document d USING(document_id) WHERE d.customer_id=? ORDER BY v.verification_id"),
        ("OWNERSHIP", "SELECT ownership_link_id, owner_id, ownership_pct, effective_from, effective_to FROM ownership_link WHERE customer_id=?"),
        ("DECISIONS", "SELECT decision_id, decision_date, decision, completeness_pct, reason_codes FROM customer_decision WHERE customer_id=? ORDER BY decision_date, decision_id"),
        ("REVIEWS", "SELECT review_id, due_date, status, completed_on, resolution FROM review_case WHERE customer_id=?"),
        ("ALERTS", "SELECT alert_id, alert_type, lead_days, trigger_date, status, resolved_on FROM alert WHERE customer_id=? ORDER BY trigger_date"),
        ("AUDIT TRAIL", "SELECT audit_event_id, event_ts, action, entity_id, details FROM audit_event WHERE customer_id=? ORDER BY audit_event_id"),
    ]
    for title, sql in sections:
        print(f"\n-- {title}")
        for r in conn.execute(sql, (cid,)):
            print("  ", r)
    gt = d / "ground_truth" / "ground_truth.json"
    if a.truth and gt.exists():
        print("\n-- GROUND TRUTH LABELS (evaluation only)")
        for t in json.loads(gt.read_text(encoding="utf-8")):
            if t["customer_id"] == cid or cid in (t["customer_id"] or ""):
                print("  ", t["anomaly_id"], t["issue_type"], t["entity_id"], "|", t["description"])
    return 0


def cmd_serve(a):
    """Start the application on a working copy of the SQL seed (created on first run)."""
    import shutil as _sh
    from .server import serve
    from .service import Service

    data = Path(a.data)
    db = Path(a.db)
    if a.reset and db.exists():
        db.unlink()
    if not db.exists():
        seed = data / "operational" / "sql" / "seed.sql"
        if not seed.exists():
            print(f"{seed} not found - generating the {data.name} dataset first ...")
            ns = argparse.Namespace(cmd="all", profile=data.name if data.name in ("starter", "full") else "starter",
                                    seed=load_generation_cfg()["default_seed"], as_of=parse_date(load_generation_cfg()["default_as_of_date"]),
                                    customers=None, businesses=None, documents=None, reviews=None)
            build(ns, data, quiet=True)
        conn = sqlite3.connect(str(db))
        conn.executescript(seed.read_text(encoding="utf-8"))
        conn.close()
        print(f"created working database {db} from {seed}")
    as_of = parse_date(json.loads((data / "manifest.json").read_text(encoding="utf-8"))["meta"]["as_of_date"])
    svc = Service(str(db), as_of)
    httpd = serve(svc, a.host, a.port, verbose=a.verbose)
    print(f"KYC Evidence Platform running at http://{a.host}:{a.port}/   (business date {as_of}; Ctrl+C to stop)")
    print(f"API examples: http://{a.host}:{a.port}/api/summary   http://{a.host}:{a.port}/api/customers/CUS-000001")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_report(a):
    """Snapshot the live application database: validation reports + CSV export of every table."""
    import csv
    from .service import Service

    data = Path(a.data)
    as_of = parse_date(a.as_of) if a.as_of else parse_date(json.loads((data / "manifest.json").read_text(encoding="utf-8"))["meta"]["as_of_date"])
    if not Path(a.db).exists():
        print(f"database {a.db} not found - run `python run.py serve` first and make some changes")
        return 2
    svc = Service(a.db, as_of)
    row = svc.one("SELECT MAX(d) m FROM (SELECT MAX(decision_date) d FROM customer_decision UNION SELECT MAX(verified_on) FROM verification UNION SELECT MAX(submitted_on) FROM document)")
    if row and row["m"] and as_of < parse_date(row["m"]):
        as_of = parse_date(row["m"])  # the app was moved forward: report at the latest business date
        svc.set_as_of(as_of.isoformat(), "USR-ADM-001")
    out = Path(a.out)
    (out / "csv").mkdir(parents=True, exist_ok=True)
    ex = svc.exceptions()
    (out / "validation_report.json").write_text(json.dumps(ex, indent=1, default=str), encoding="utf-8")
    cols = ["finding_id", "rule_code", "category", "severity", "entity_type", "entity_id", "related_entity_id", "customer_id", "detail"]
    with open(out / "validation_findings.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(ex["findings"])
    summ = svc.summary()
    lines = [f"# Live application report @ {ex['as_of_date']}", "",
             f"* Customers: {summ['customers_total']} {summ['customers']}", f"* Documents: {summ['documents']}",
             f"* Reviews: {summ['reviews']} (overdue {summ['reviews_overdue']})", f"* Active alerts: {summ['alerts_open']} {summ['alerts_by_type']}",
             f"* Audit events: {summ['audit_events']}", "", "## Validator findings by category", ""]
    lines += [f"* {k}: {v}" for k, v in sorted(ex["by_category"].items())] or ["* none"]
    lines += ["", "## By rule", ""] + [f"* {k}: {v}" for k, v in sorted(ex["by_rule"].items())]
    bad = {k: v for k, v in ex["by_category"].items() if k != "BUSINESS_RULE"}
    lines += ["", "**RESULT:** " + ("PASS - no integrity or reconciliation defects (business-rule exceptions are listed for follow-up)." if not bad else f"FAIL - {bad}")]
    (out / "validation_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for t in svc.tables:
        names = [c["name"] for c in t["columns"]] if "columns" in t else None
        rows = svc.q(f"SELECT * FROM {t['name']} ORDER BY " + ",".join(t["order"]))
        with open(out / "csv" / f"{t['name']}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            hdr = list(rows[0].keys()) if rows else (names or [])
            w.writerow(hdr)
            w.writerows([[r[k] for k in hdr] for r in rows])
    print((out / "validation_summary.md").read_text(encoding="utf-8"))
    print(f"written to {out}/ (validation_summary.md, validation_findings.csv, validation_report.json, csv/<table>.csv)")
    return 0 if not bad else 1


def cmd_dictionary(a):
    tables = build_tables(load_policy())
    p = ROOT / "docs" / "DATA_DICTIONARY.md"
    p.write_text(reports.data_dictionary_markdown(tables), encoding="utf-8")
    print(f"wrote {p}")
    return 0


def main(argv=None):
    gen = load_generation_cfg()
    ap = argparse.ArgumentParser(prog="run.py", description="KYC Evidence Platform - synthetic data generator & validator (stdlib only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--profile", default="full", choices=sorted(gen["profiles"]))
        p.add_argument("--seed", type=int, default=gen["default_seed"])
        p.add_argument("--as-of", default=gen["default_as_of_date"], type=parse_date, dest="as_of")
        for k in ("customers", "businesses", "documents", "reviews"):
            p.add_argument(f"--{k}", type=int, default=None, help=f"override profile {k} count")

    p = sub.add_parser("all", help="generate + export + validate + reports (default workflow)")
    common(p)
    p.add_argument("--out", default=None, help="output directory (default: data/<profile>)")
    p = sub.add_parser("generate", help="same as 'all' (kept for clarity)")
    common(p)
    p.add_argument("--out", default=None)
    p = sub.add_parser("validate", help="validate an existing export directory")
    p.add_argument("--data", required=True)
    p = sub.add_parser("repro", help="regenerate twice with the same seed and compare row counts + SHA-256 checksums")
    common(p)
    p.add_argument("--write", default=None, help="write the reproducibility report (json) to this path")
    p = sub.add_parser("trace", help="print everything known about one customer")
    p.add_argument("customer")
    p.add_argument("--data", default=str(DEFAULT_OUT / "full"))
    p.add_argument("--truth", action="store_true", help="also show ground-truth labels (evaluation only)")
    sub.add_parser("dictionary", help="regenerate docs/DATA_DICTIONARY.md")
    p = sub.add_parser("report", help="validation report + CSV export of the LIVE application database")
    p.add_argument("--db", default=str(ROOT / "kyc_app.db"))
    p.add_argument("--data", default=str(DEFAULT_OUT / "starter"), help="dataset the db was seeded from (for the default as-of date)")
    p.add_argument("--as-of", default=None)
    p.add_argument("--out", default=str(ROOT / "reports_live"))
    p = sub.add_parser("serve", help="run the working application (browser UI + JSON API) on a generated dataset")
    p.add_argument("--data", default=str(DEFAULT_OUT / "starter"), help="dataset directory (default data/starter; use data/full for the full set)")
    p.add_argument("--db", default=str(ROOT / "kyc_app.db"), help="working SQLite file (created from seed.sql if missing)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reset", action="store_true", help="discard the working database and start again from the seed")
    p.add_argument("--verbose", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd in ("all", "generate"):
        out = Path(a.out) if a.out else DEFAULT_OUT / a.profile
        m = build(a, out)
        return 0 if m["validation_passed"] else 1
    return {"validate": cmd_validate, "repro": cmd_repro, "trace": cmd_trace, "dictionary": cmd_dictionary, "serve": cmd_serve, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
