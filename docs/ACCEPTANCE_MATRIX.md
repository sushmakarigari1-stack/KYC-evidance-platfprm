# Acceptance matrix - brief requirement -> implementation -> evidence

| # | Requirement (brief) | Implementation | Evidence |
|---|---|---|---|
| 1 | Master data first: Customer, CustomerType, KYCRequirement, Document, Verification | `generator.build_reference` then customers/documents/verifications; parents always precede children | `kyc/pipeline.py` order; FK enforcement on load (`store.to_db`) |
| 2 | Dependent records use valid parent IDs | SQLite FK constraints + validator FK checks incl. polymorphic refs | `FK_ORPHAN`, `AUDIT_ENTITY_ORPHAN` = 0; test `test_fk_orphan` |
| 3 | Requirements by customer type and risk | `kyc_requirement` (21 rules, KYC-2026-nnnnnn) from `config/policy.json`; business adds ownership checkpoint | `REQUIREMENT_CONFIG_MISMATCH` = 0 |
| 4 | Documents with issue/expiry dates and verification status | `document` + append-only `verification`; status derived | `DOCUMENT_STATUS_MISMATCH` = 0 |
| 5 | Beneficial owners for business customers | `beneficial_owner` + `ownership_link` (N:M, effective-dated) | `OWNERSHIP_ON_NON_BUSINESS` = 0; ownership sums reconcile |
| 6 | Completeness calculated | `rules.evaluate` (documents + ownership) | `DECISION_RECOMPUTE_MISMATCH` = 0 |
| 7 | Periodic reviews scheduled | risk-cycle schedule, `review_case` + `review_case_event` state machine | `LIFECYCLE_TRANSITION` = 0 |
| 8 | Expiry alerts + audit events | `rules.derive_alerts` (lead times 90/60/30/7), audit trail from final tables | `ALERT_MISSING/MISMATCH/UNEXPECTED` = 0; boundary tests |
| 9 | State changes persisted as events; no overwriting | append-only decision/verification/review/alert/audit tables; `supersedes_document_id` | design D6; projection checks |
| 10 | Outcomes derived from documented rules, not random | all outcomes via `rules.py`; shared by generator and validator | AST test: no ground-truth import; recompute checks |
| 11 | Data dictionary (type, required, allowed, units, uniqueness, validation) | generated from `schema.py` | `docs/DATA_DICTIONARY.md`, `data_dictionary.csv` |
| 12 | PK for every entity, FK for every relationship; cardinality documented; bridge for N:M | DDL from schema; relationship table in dictionary | `ownership_link` bridge |
| 13 | Deterministic IDs, zero padded, consistent across files | `common.Ids`, regex validation | `ID_FORMAT` = 0; `test_id_conventions` |
| 14 | Volume: 8,000 customers / 20,000 documents / 1,000 business / 3,000 reviews | exact reconciliation in generator | `test_volumes_exact` |
| 15 | Include expired docs, name mismatches, missing ownership, overdue reviews | natural baseline exceptions | `test_required_baseline_coverage_present`; `metrics.json` |
| 16 | Fixed seed 20261022, scale parameterised, same generator for starter/full | `--profile/--customers/...` | `test_scale_is_parameterised_with_exact_counts` |
| 17 | Edge cases: expired-but-valid; approved-missing-evidence; ownership > 100; review closed w/o resolution; duplicate doc across customers | `kyc/anomalies.py` (40 each on full) | 200/200 detected, 0 unexpected |
| 18 | Derived values from source (totals, scores, statuses, dates) | `rules.py` | reconciliation tier of validator |
| 19 | Separate ground_truth.csv/json, never leaked | `data/<p>/ground_truth/` | `test_no_leak_into_operational_files`, `test_logic_modules_never_import_truth` |
| 20 | Validation: PK, FK, enums, date order, arithmetic, lifecycle, duplicates | `kyc/validate.py` | `reports/validation_summary.md`; 13 mutation tests |
| 21 | Export CSV/JSON/SQL + README, dictionary, command, seed, source manifest, validation summary | `export.py`, `reports.py`, README | `data/<p>/` |
| 22 | Regenerate twice and compare counts/checksums | `python run.py repro` | `docs/REPRODUCIBILITY.md` |
| 23 | Acceptance: completeness matches type; expiry alerts at lead times; ownership reconciles; every exception traceable | rules + validator + `python run.py trace <customer> --truth` | tests + findings link rule, entity and customer ids |
| 24 | Lawful provenance | fully synthetic, `source_manifest.json` | no external data |
| 25 | Working end-to-end workflow (onboard, evidence, verification, completeness decision, reviews, expiry alerts, audit, exceptions) | `python run.py serve` (UI + API) | `tests/test_app.py` (9 HTTP tests) |
