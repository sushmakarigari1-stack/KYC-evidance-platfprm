# Validation summary - profile `full`

* seed: `20261022`  * as-of date: `2026-11-13`  * policy version: `2026.1`
* **Result: PASS**

## Row counts

| table | rows |
|---|---:|
| customer_type | 2 |
| staff_user | 18 |
| document_type | 6 |
| kyc_requirement | 21 |
| customer | 8,000 |
| risk_rating | 8,352 |
| document | 20,000 |
| verification | 20,344 |
| beneficial_owner | 1,839 |
| ownership_link | 2,059 |
| customer_decision | 16,101 |
| review_case | 3,000 |
| review_case_event | 11,510 |
| alert | 4,323 |
| alert_event | 7,911 |
| audit_event | 94,277 |

## Checks executed

Primary-key uniqueness and id formats; foreign keys (declared + polymorphic audit/alert references); required values; allowed enums; date ordering; review/alert lifecycle transitions; projection consistency (customer risk/status vs history); arithmetic reconciliation (decision completeness, risk score, ownership totals); verification-rule and document-status recomputation; alert reconciliation against configured expiry lead times; duplicate detection.

## Findings by category

| category | findings | expectation |
|---|---:|---|
| INTEGRITY | 0 | must be 0 |
| RECONCILIATION | 0 | must be 0 |
| BUSINESS_RULE | 200 | equals injected anomalies |

## Injected edge cases vs detection (evaluation step; ground truth used only here)

| issue type | injected | detected | missed | unexpected | precision | recall |
|---|---:|---:|---:|---:|---:|---:|
| EXPIRED_DOCUMENT_MARKED_VALID | 40 | 40 | 0 | 0 | 1.0 | 1.0 |
| APPROVED_WITH_MISSING_EVIDENCE | 40 | 40 | 0 | 0 | 1.0 | 1.0 |
| OWNERSHIP_OVER_100 | 40 | 40 | 0 | 0 | 1.0 | 1.0 |
| REVIEW_CLOSED_WITHOUT_RESOLUTION | 40 | 40 | 0 | 0 | 1.0 | 1.0 |
| DUPLICATE_DOCUMENT_ACROSS_CUSTOMERS | 40 | 40 | 0 | 0 | 1.0 | 1.0 |

Overall precision **1.0**, recall **1.0**, integrity/reconciliation defects **0**.

## Key distributions

* customers by status: APPROVED 79.96%, ON_HOLD 8.66%, PENDING 11.38%
* customers by risk: HIGH 3.33%, LOW 82.12%, MEDIUM 14.55%
* documents by status: EXPIRED 3.85%, PENDING_VERIFICATION 2.02%, REJECTED 4.52%, SUPERSEDED 5.36%, VALID 84.25%
* name-mismatch rejections: 438 (2.19% of documents)
* business customers missing ownership records: 43 of 1000
* overdue reviews: 510 (86 still open, 424 completed late)
* alerts: DOCUMENT_EXPIRED 1029, DOCUMENT_EXPIRING 2784, REVIEW_OVERDUE 510
