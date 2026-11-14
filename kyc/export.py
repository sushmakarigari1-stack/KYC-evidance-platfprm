"""Deterministic exporters (CSV / JSON / SQL) and the CSV reader used by the validator.

Reproducibility rules: rows sorted by primary key, '\n' line endings, fixed float
precision, sorted JSON keys, no timestamps / hostnames / absolute paths in any file.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

from .schema import column_names, ddl


def _fmt(col, v):
    if v is None:
        return None
    if isinstance(v, date):
        return v.isoformat()
    if col["type"] == "REAL":
        return f"{float(v):.2f}"
    return v


def _rows(T, t):
    cols = t["cols"]
    rows = sorted(T[t["name"]], key=lambda x: tuple(x[k] for k in t["order"]))
    return [[_fmt(c, r.get(c["name"])) for c in cols] for r in rows]


def _write_csv(path: Path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])


def _sql_val(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def export_operational(T, tables, out: Path):
    csv_dir, json_dir, sql_dir = out / "operational" / "csv", out / "operational" / "json", out / "operational" / "sql"
    for d in (csv_dir, json_dir, sql_dir):
        d.mkdir(parents=True, exist_ok=True)
    sql = ["-- KYC Evidence Platform - synthetic operational seed (no ground truth in this file)",
           "PRAGMA foreign_keys=ON;", ddl(tables), "BEGIN TRANSACTION;", "PRAGMA defer_foreign_keys=ON;"]
    counts = {}
    for t in tables:
        header = column_names(t)
        rows = _rows(T, t)
        counts[t["name"]] = len(rows)
        _write_csv(csv_dir / f'{t["name"]}.csv', header, rows)
        typed = []
        for r in sorted(T[t["name"]], key=lambda x: tuple(x[k] for k in t["order"])):
            rec = {}
            for c in t["cols"]:
                v = r.get(c["name"])
                rec[c["name"]] = v.isoformat() if isinstance(v, date) else (round(float(v), 2) if (v is not None and c["type"] == "REAL") else v)
            typed.append(rec)
        (json_dir / f'{t["name"]}.json').write_text(json.dumps(typed, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        batch = []
        for r in rows:
            vals = []
            for c, v in zip(t["cols"], r):
                if v is not None and c["type"] == "INTEGER":
                    v = int(v)
                elif v is not None and c["type"] == "REAL":
                    v = float(v)
                vals.append(_sql_val(v))
            batch.append("(" + ",".join(vals) + ")")
            if len(batch) == 200:
                sql.append(f'INSERT INTO {t["name"]} ({",".join(header)}) VALUES\n' + ",\n".join(batch) + ";")
                batch = []
        if batch:
            sql.append(f'INSERT INTO {t["name"]} ({",".join(header)}) VALUES\n' + ",\n".join(batch) + ";")
    sql.append("COMMIT;")
    (sql_dir / "seed.sql").write_text("\n".join(sql) + "\n", encoding="utf-8")
    return counts


GT_COLS = ["anomaly_id", "issue_type", "entity_type", "entity_id", "related_entity_id", "customer_id",
           "expected_rule_code", "severity", "description"]
EXP_COLS = ["customer_id", "expected_status", "expected_kyc_complete", "expected_completeness_pct", "expected_reason_codes"]


def export_ground_truth(truth, expected, out: Path):
    d = out / "ground_truth"
    d.mkdir(parents=True, exist_ok=True)
    _write_csv(d / "ground_truth.csv", GT_COLS, [[t.get(c) for c in GT_COLS] for t in truth])
    (d / "ground_truth.json").write_text(json.dumps(truth, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    _write_csv(d / "expected_customer_outcomes.csv", EXP_COLS,
               [[(f"{e[c]:.2f}" if c == "expected_completeness_pct" else e[c]) for c in EXP_COLS] for e in expected])
    (d / "expected_customer_outcomes.json").write_text(json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    (d / "README.txt").write_text(
        "EVALUATION ONLY. These files label the deliberately injected anomalies and the rule-derived expected\n"
        "customer outcomes. They must never be used as model/engine input features and are never read by the\n"
        "generator's decision logic or by the validator's detection rules.\n", encoding="utf-8")


def read_csv_tables(data_dir: Path, tables):
    """Load the exported operational CSVs with real types (dates, ints, floats)."""
    out = {}
    for t in tables:
        path = data_dir / "operational" / "csv" / f'{t["name"]}.csv'
        rows = []
        with path.open(newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            if rd.fieldnames != column_names(t):
                raise ValueError(f"{path.name}: header {rd.fieldnames} does not match the data dictionary")
            for rec in rd:
                row = {}
                for c in t["cols"]:
                    v = rec[c["name"]]
                    if v == "":
                        row[c["name"]] = None
                    elif c["unit"] == "date":
                        row[c["name"]] = date.fromisoformat(v)
                    elif c["type"] == "INTEGER":
                        row[c["name"]] = int(v)
                    elif c["type"] == "REAL":
                        row[c["name"]] = float(v)
                    else:
                        row[c["name"]] = v
                rows.append(row)
        out[t["name"]] = rows
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def checksums(out: Path) -> dict:
    res = {}
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            res[p.relative_to(out).as_posix()] = sha256_file(p)
    return res
