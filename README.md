# KYC Evidence Platform - working application, synthetic data, rules engine & validator

Tracks onboarding evidence, validates completeness, and manages periodic reviews and expiries for a KYC/CDD
workflow. It is a **working application** (browser UI + JSON API over SQLite) running on a **deterministic
synthetic dataset**, with a documented rules engine, an independent validator, labelled edge cases and a
separate ground truth.

* **Zero dependencies** - Python standard library only (no pip install, no Docker, no database server).
* **One command** produces CSV + JSON + SQL, ground truth, validation report, metrics and checksums.
* **Reproducible** - fixed seed `20261022`; regenerating twice gives byte-identical files (proved by `repro`).

## Run the application (nothing to install)

```bash
python run.py serve                 # starter dataset, http://127.0.0.1:8000/
python run.py serve --data data/full --db kyc_full.db     # 8,000-customer dataset
python run.py serve --reset         # throw away changes, start again from the seed
```

Open the URL. Pick who you are in the top bar (analyst / reviewer / admin), then follow the workflow:
**onboard a customer -> submit documents -> verify -> completeness is re-evaluated and an append-only decision is
recorded -> schedule/advance periodic reviews -> move the business date forward and run the daily batch to raise
expiry alerts -> check the Exceptions tab**, which runs the full validator on the live database.

| Area | Endpoints (header `X-Actor: USR-...` selects the user; roles enforced) |
|---|---|
| Customers | `GET/POST /api/customers`, `GET /api/customers/{id}`, `POST .../documents`, `.../evaluate`, `.../owners`, `.../reviews` |
| Verification | `POST /api/documents/{doc_id}/verify` |
| Reviews | `GET /api/reviews`, `POST /api/reviews/{id}/transition` (resolution mandatory to complete) |
| Alerts | `GET /api/alerts`, `POST /api/alerts/{id}/acknowledge`, `POST /api/admin/alerts/scan` (idempotent) |
| Operations | `POST /api/system/as-of`, `POST /api/admin/daily-run`, `GET /api/summary`, `/api/audit-events`, `/api/exceptions` |

Errors are JSON `{"error":{"code","message"}}` with 400/403/404/405/409/422 as appropriate (duplicate document,
invalid review transition, ownership > 100%, missing resolution, role not allowed).

Step-by-step test script, report generation and regeneration: **`docs/TESTING_GUIDE.md`**.
`python run.py report` writes validation reports and CSVs of the live app database to `reports_live/`.

## Quick start

Requires Python 3.9+ (developed and tested on 3.13; nothing to install).

```bash
python run.py all --profile full          # 8,000 customers / 20,000 documents / 3,000 reviews  (~8 s)
python run.py all --profile starter       # 400 customers / 1,000 documents / 150 reviews        (<1 s)
python -m unittest discover -s tests -v   # 44 tests (acceptance, boundaries, validator mutation tests, end-to-end HTTP app tests)
python run.py repro --profile full        # regenerate twice, compare row counts + SHA-256 of every file
python run.py trace CUS-000007            # full evidence / decision / alert / audit story for one customer
```

Generator command used for the committed datasets (also recorded in each `manifest.json`):

```bash
python run.py all --profile full --seed 20261022 --as-of 2026-11-13
```

Scale is parameterised - the same generator serves every size:
`python run.py all --profile starter --customers 2000 --businesses 250 --documents 5000 --reviews 750`.

## What you get (`data/<profile>/`)

| path | content |
|---|---|
| `operational/csv/*.csv`, `operational/json/*.json` | 16 tables - **inputs only** (no labels) |
| `operational/sql/seed.sql` | DDL (PK/FK/CHECK/UNIQUE) + data; loads with `sqlite3 kyc.db < seed.sql` |
| `ground_truth/ground_truth.csv/json` | labelled injected anomalies (issue_type, entity, expected rule) |
| `ground_truth/expected_customer_outcomes.csv/json` | rule-derived expected KYC status/completeness per customer |
| `reports/validation_summary.md`, `validation_report.json`, `validation_findings.csv` | validation results |
| `reports/metrics.json` | distributions, coverage and evaluation metrics |
| `data_dictionary.csv`, `source_manifest.json`, `manifest.json` | dictionary, provenance, seed/command/row counts/SHA-256 |

Docs: [`docs/DESIGN.md`](docs/DESIGN.md) (design decisions & strategy) - [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) -
[`docs/ACCEPTANCE_MATRIX.md`](docs/ACCEPTANCE_MATRIX.md) (requirement -> implementation -> evidence) -
[`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md).

## Latest verified results (full profile, seed 20261022, as-of 2026-11-13)

| check | result |
|---|---|
| volumes | 8,000 customers - 1,000 business - 20,000 documents - 3,000 review cases (exact) |
| validation | PASS - 0 integrity defects, 0 reconciliation defects |
| injected edge cases | 200 injected (40 per class) - 200 detected - 0 missed - 0 false positives |
| reproducibility | two runs, 44 files, identical row counts and SHA-256 |
| tests | 34 passed |

## How it works (one paragraph)

Reference data -> customers & risk ratings -> evidence documents -> verification attempts -> beneficial owners ->
decisions -> reviews -> alerts -> **injected anomalies** -> audit trail. Every derived value (verification outcome,
document status, completeness, KYC decision, expiry alerts, overdue alerts, risk level) is computed by one shared
module (`kyc/rules.py`) from source records, never assigned randomly. History is append-only (renewals are new
documents linked by `supersedes_document_id`; decisions, ratings, review/alert transitions and audit events are
never overwritten). The validator re-reads the **exported CSV files** and re-derives everything independently.
See [`docs/DESIGN.md`](docs/DESIGN.md).

## Edge cases (labelled, injected after a clean baseline)

| issue_type | how it is injected | detected by |
|---|---|---|
| `EXPIRED_DOCUMENT_MARKED_VALID` | stored status of a naturally expired, verified document flipped to VALID | status vs expiry date |
| `APPROVED_WITH_MISSING_EVIDENCE` | an APPROVED decision appended for a customer whose mandatory evidence is incomplete | recomputed completeness at as-of |
| `OWNERSHIP_OVER_100` | extra owner link appended after approval without rebalancing | effective ownership sum > 100.00 |
| `REVIEW_CLOSED_WITHOUT_RESOLUTION` | valid transition to COMPLETED appended with NULL resolution | COMPLETED + empty resolution |
| `DUPLICATE_DOCUMENT_ACROSS_CUSTOMERS` | document fingerprint copied to an unrelated customer's document | fingerprint shared across customers |

The baseline also contains *natural* exceptions (not anomalies): expired documents, name-mismatch rejections,
illegible scans, expired-at-submission documents, low-confidence (inconclusive) verifications, businesses with
missing ownership records, ON_HOLD customers, overdue reviews, expiring/expired/overdue alerts.

## Lawful provenance

100% synthetic. **No external data, no web access, no real personal or confidential data.** Names are random
combinations from invented pools; identity numbers are never stored (only SHA-256 of synthetic strings). Public
KYC/CDD concepts were used for schema inspiration only. Details: `data/<profile>/source_manifest.json`.

## Layout

```
run.py                 entry point
config/policy.json     business policy: requirements by customer type & risk, review cycles, alert lead times, enums, state machines
config/generation.json scale profiles + distribution knobs (probabilities, windows)
kyc/                   schema.py  rules.py  generator.py  anomalies.py  validate.py  export.py  reports.py  cli.py ...
                       service.py (workflow commands/queries)  server.py (HTTP API)  static/index.html (browser UI)
tests/                 acceptance, boundary and mutation tests
data/starter, data/full  committed outputs
docs/                  design, dictionary, acceptance matrix, reproducibility
```

## Assumptions & limits

* `as_of_date` is explicit (default 2026-11-13) so results never depend on the wall clock. Expired means `expiry_date < as_of`.
* Documents represent the *current KYC evidence cycle* (latest collection, renewals, resubmissions); older archived evidence is out of scope. This is what makes 20,000 documents for 8,000 customers realistic.
* Review ids follow `REV-<customer>-2026` (one review per customer per campaign year); if the 2026 due-cohort is smaller than the target, the earliest next-year dues are pulled forward. The shipped datasets have all 3,000 reviews due in 2026.
* The uploaded Java/Spring design and the earlier FastAPI draft are intentionally **not** part of this build: the brief asks for a working data/rules/validation solution, and a zero-dependency CLI is the lowest-risk way to run it anywhere.
