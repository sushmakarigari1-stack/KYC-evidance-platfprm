# Validation summary - profile `starter`

* seed: `20261022`  * as-of date: `2026-11-13`  * policy version: `2026.1`
* **Result: PASS**

## Row counts

| table | rows |
|---|---:|
| customer_type | 2 |
| staff_user | 18 |
| document_type | 6 |
| kyc_requirement | 21 |
| customer | 400 |
| risk_rating | 415 |
| document | 1,000 |
| verification | 1,014 |
| beneficial_owner | 89 |
| ownership_link | 98 |
| customer_decision | 813 |
| review_case | 150 |
| review_case_event | 599 |
| alert | 220 |
| alert_event | 414 |
| audit_event | 4,753 |

## Checks executed

Primary-key uniqueness and id formats; foreign keys (declared + polymorphic audit/alert references); required values; allowed enums; date ordering; review/alert lifecycle transitions; projection consistency (customer risk/status vs history); arithmetic reconciliation (decision completeness, risk score, ownership totals); verification-rule and document-status recomputation; alert reconciliation against configured expiry lead times; duplicate detection.

## Findings by category

| category | findings | expectation |
|---|---:|---|
| INTEGRITY | 0 | must be 0 |
| RECONCILIATION | 0 | must be 0 |
| BUSINESS_RULE | 15 | equals injected anomalies |

## Injected edge cases vs detection (evaluation step; ground truth used only here)

| issue type | injected | detected | missed | unexpected | precision | recall |
|---|---:|---:|---:|---:|---:|---:|
| EXPIRED_DOCUMENT_MARKED_VALID | 3 | 3 | 0 | 0 | 1.0 | 1.0 |
| APPROVED_WITH_MISSING_EVIDENCE | 3 | 3 | 0 | 0 | 1.0 | 1.0 |
| OWNERSHIP_OVER_100 | 3 | 3 | 0 | 0 | 1.0 | 1.0 |
| REVIEW_CLOSED_WITHOUT_RESOLUTION | 3 | 3 | 0 | 0 | 1.0 | 1.0 |
| DUPLICATE_DOCUMENT_ACROSS_CUSTOMERS | 3 | 3 | 0 | 0 | 1.0 | 1.0 |

Overall precision **1.0**, recall **1.0**, integrity/reconciliation defects **0**.

## Key distributions

* customers by status: APPROVED 83.25%, ON_HOLD 6.25%, PENDING 10.5%
* customers by risk: HIGH 2.0%, LOW 85.5%, MEDIUM 12.5%
* documents by status: EXPIRED 2.8%, PENDING_VERIFICATION 2.2%, REJECTED 2.9%, SUPERSEDED 8.4%, VALID 83.7%
* name-mismatch rejections: 14 (1.4% of documents)
* business customers missing ownership records: 4 of 50
* overdue reviews: 33 (4 still open, 29 completed late)
* alerts: DOCUMENT_EXPIRED 52, DOCUMENT_EXPIRING 135, REVIEW_OVERDUE 33
