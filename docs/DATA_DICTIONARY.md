# Data dictionary

Generated from `kyc/schema.py` (single source of truth for DDL, CSV headers and validation). Dates are ISO-8601 `yyyy-mm-dd`; `NULL` is an empty CSV cell / JSON `null`.

## Relationship summary

| table | description | cardinality / relationships | primary key |
|---|---|---|---|
| `customer_type` | Reference: customer classes that drive which evidence is required. | 1:N customer; 1:N kyc_requirement | `customer_type_id` |
| `staff_user` | Reference: internal actors (operations analyst, risk/compliance reviewer, administrator, system). | 1:N verification, review_case, review_case_event, alert_event, customer_decision | `user_id` |
| `document_type` | Reference: evidence catalogue with validity period. | 1:N kyc_requirement; 1:N document | `document_type_code` |
| `kyc_requirement` | Policy rule: which document types are mandatory for a customer type at a risk level (effective-dated, version-pinned). | N:1 customer_type; N:1 document_type | `kyc_requirement_id` |
| `customer` | Customer master (current-state projection; history lives in risk_rating, customer_decision, audit_event). | 1:N risk_rating, document, customer_decision, review_case, alert, ownership_link (business only) | `customer_id` |
| `risk_rating` | Effective-dated risk rating history (append-only; previous row is end-dated, never overwritten). | N:1 customer | `risk_rating_id` |
| `document` | Customer-specific evidence. Never overwritten: renewals/resubmissions are new rows linked via supersedes_document_id. | N:1 customer; N:1 document_type; 1:N verification; self-reference supersedes | `document_id` |
| `verification` | Append-only verification attempts for documents. | N:1 document | `verification_id` |
| `beneficial_owner` | Identity of a natural person who owns business customers (reusable across businesses). | N:M customer via ownership_link | `owner_id` |
| `ownership_link` | Bridge table: effective-dated ownership percentage of an owner in a business customer (N:M). | N:1 customer; N:1 beneficial_owner | `ownership_link_id` |
| `customer_decision` | Append-only KYC decisions with the completeness snapshot used (never overwritten). | N:1 customer | `decision_id` |
| `review_case` | Periodic review work item (current state; transitions are in review_case_event). | N:1 customer; 1:N review_case_event | `review_id` |
| `review_case_event` | Append-only review state-transition history. | N:1 review_case | `review_event_id` |
| `alert` | Alert projection with a deterministic dedupe key (idempotent processing). | N:1 customer; N:1 document or review_case; 1:N alert_event | `alert_id` |
| `alert_event` | Append-only alert lifecycle history. | N:1 alert | `alert_event_id` |
| `audit_event` | Append-only audit trail of every material state change (traceability). | N:1 customer; polymorphic entity reference | `audit_event_id` |

## `customer_type`

Reference: customer classes that drive which evidence is required.

*Primary key:* `customer_type_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `customer_type_id` | TEXT | yes | INDIVIDUAL, BUSINESS |  | yes | Must be one of the configured customer types. | Stable code of the customer class. |
| `description` | TEXT | yes |  |  | no | Non-empty. | Human description. |
| `requires_beneficial_owner` | INTEGER | yes | 0, 1 |  | no | 0/1. | 1 when ownership must be disclosed and reconcile to 100%. |

## `staff_user`

Reference: internal actors (operations analyst, risk/compliance reviewer, administrator, system).

*Primary key:* `user_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `user_id` | TEXT | yes |  |  | yes | USR-<ROLE>-nnn. | Staff or system identifier. |
| `role` | TEXT | yes | SYSTEM, OPERATIONS_ANALYST, RISK_COMPLIANCE_REVIEWER, SYSTEM_ADMINISTRATOR |  | no | Enum. | Role of the actor. |
| `display_name` | TEXT | yes |  |  | no | Non-empty; synthetic only. | Synthetic display name. |

## `document_type`

Reference: evidence catalogue with validity period.

*Primary key:* `document_type_code`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `document_type_code` | TEXT | yes | ID_PROOF, ADDRESS_PROOF, TAX_PROOF, SOURCE_OF_FUNDS, BUSINESS_REGISTRATION, OWNERSHIP_DECLARATION |  | yes | Enum from policy. | Stable code. |
| `description` | TEXT | yes |  |  | no | Non-empty. | Human description. |
| `validity_days` | INTEGER | no |  | days | no | NULL or > 0. | Days a document stays valid after issue_date; NULL = non-expiring. |

## `kyc_requirement`

Policy rule: which document types are mandatory for a customer type at a risk level (effective-dated, version-pinned).

*Primary key:* `kyc_requirement_id`  *Unique:* (customer_type_id, risk_level, document_type_code, policy_version)

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `kyc_requirement_id` | TEXT | yes |  |  | yes | ^KYC-\d{4}-\d{6}$ | KYC-<year>-nnnnnn. |
| `customer_type_id` | TEXT | yes | INDIVIDUAL, BUSINESS |  | no |  FK -> `customer_type(customer_type_id)`. | Customer class the rule applies to. |
| `risk_level` | TEXT | yes | LOW, MEDIUM, HIGH |  | no |  | Risk level the rule applies to. |
| `document_type_code` | TEXT | yes | ID_PROOF, ADDRESS_PROOF, TAX_PROOF, SOURCE_OF_FUNDS, BUSINESS_REGISTRATION, OWNERSHIP_DECLARATION |  | no |  FK -> `document_type(document_type_code)`. | Mandatory document type. |
| `mandatory` | INTEGER | yes | 0, 1 |  | no | Always 1 in this policy. | 1 = mandatory evidence. |
| `policy_version` | TEXT | yes |  |  | no | Non-empty. | Policy version that defined the rule. |
| `effective_from` | TEXT | yes |  | date | no |  | First day the rule applies. |
| `effective_to` | TEXT | no |  | date | no | NULL or >= effective_from. | Last day the rule applies; NULL = open-ended. |

Table checks: `effective_to IS NULL OR effective_to >= effective_from`

## `customer`

Customer master (current-state projection; history lives in risk_rating, customer_decision, audit_event).

*Primary key:* `customer_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `customer_id` | TEXT | yes |  |  | yes | ^CUS-\d{6}$ | CUS-nnnnnn, immutable. |
| `customer_type_id` | TEXT | yes | INDIVIDUAL, BUSINESS |  | no |  FK -> `customer_type(customer_type_id)`. | Customer class. |
| `legal_name` | TEXT | yes |  |  | no | Non-empty. | Synthetic legal name used for name-match checks. |
| `country_code` | TEXT | yes | IN, US, GB, SG, DE, AE, BR, NG |  | no | ISO-3166 alpha-2 subset. | Country of residence / incorporation. |
| `birth_or_incorporation_date` | TEXT | yes |  | date | no | < onboarding_date. | Date of birth (individual) or incorporation date (business). |
| `onboarding_date` | TEXT | yes |  | date | no | <= as_of_date. | Date the account relationship started. |
| `risk_level` | TEXT | yes | LOW, MEDIUM, HIGH |  | no | Equals latest risk_rating.risk_level. | Current rating = latest risk_rating row. |
| `status` | TEXT | yes | PENDING, APPROVED, ON_HOLD |  | no | Equals latest customer_decision.decision. | Current KYC status = latest customer_decision. |
| `status_since` | TEXT | yes |  | date | no | Equals latest customer_decision.decision_date. | Date of the latest decision. |

Table checks: `birth_or_incorporation_date < onboarding_date`

## `risk_rating`

Effective-dated risk rating history (append-only; previous row is end-dated, never overwritten).

*Primary key:* `risk_rating_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `risk_rating_id` | TEXT | yes |  |  | yes | ^RSK-\d{6}$ | RSK-nnnnnn. |
| `customer_id` | TEXT | yes |  |  | no |  FK -> `customer(customer_id)`. | Rated customer. |
| `risk_score` | INTEGER | yes |  | points | no | 0..200 and equals sum of score_components. | Sum of the factor points in score_components. |
| `risk_level` | TEXT | yes | LOW, MEDIUM, HIGH |  | no | Derived: <40 LOW, 40-64 MEDIUM, >=65 HIGH. | Level derived from score thresholds. |
| `score_components` | TEXT | yes |  |  | no | Parsable key=int list. | Factor=points pairs, ';' separated. |
| `rating_reason` | TEXT | yes | INITIAL, RE_RATING |  | no |  | INITIAL at onboarding or RE_RATING. |
| `effective_from` | TEXT | yes |  | date | no |  | Start of validity. |
| `effective_to` | TEXT | no |  | date | no | NULL or > effective_from; exactly one NULL per customer. | End of validity; NULL = current. |

Table checks: `effective_to IS NULL OR effective_to > effective_from`

## `document`

Customer-specific evidence. Never overwritten: renewals/resubmissions are new rows linked via supersedes_document_id.

*Primary key:* `document_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `document_id` | TEXT | yes |  |  | yes | ^DOC-CUS-\d{6}-\d{3}$ | DOC-<customer_id>-nnn. |
| `customer_id` | TEXT | yes |  |  | no | Prefix of document_id equals customer_id. FK -> `customer(customer_id)`. | Owner of the evidence. |
| `document_type_code` | TEXT | yes | ID_PROOF, ADDRESS_PROOF, TAX_PROOF, SOURCE_OF_FUNDS, BUSINESS_REGISTRATION, OWNERSHIP_DECLARATION |  | no |  FK -> `document_type(document_type_code)`. | Type of evidence. |
| `name_on_document` | TEXT | yes |  |  | no | Non-empty. | Name printed on the document (used for name matching). |
| `document_fingerprint` | TEXT | yes |  |  | no | 64 hex chars; unique across customers. | SHA-256 of the synthetic document number (no raw numbers stored). |
| `issue_date` | TEXT | yes |  | date | no |  | Date the issuer issued the document. |
| `expiry_date` | TEXT | no |  | date | no | NULL iff validity_days is NULL; else issue_date + validity_days. | Expiry date; NULL for non-expiring types. |
| `submitted_on` | TEXT | yes |  | date | no | >= issue_date and >= customer.onboarding_date. | Date the customer submitted the document. |
| `scan_quality_score` | INTEGER | yes | 0..100 | score 0-100 | no |  | Image/scan quality used by verification rules. |
| `status` | TEXT | yes | PENDING_VERIFICATION, VALID, REJECTED, EXPIRED, SUPERSEDED |  | no | Equals derived status. | Document lifecycle status as of as_of_date (derived from verification + expiry + supersession). |
| `supersedes_document_id` | TEXT | no |  |  | no | Same customer and type; older submitted_on. FK -> `document(document_id)`. | Older document this one replaces (renewal / resubmission). |

Table checks: `expiry_date IS NULL OR expiry_date >= issue_date`; `submitted_on >= issue_date`; `scan_quality_score BETWEEN 0 AND 100`

## `verification`

Append-only verification attempts for documents.

*Primary key:* `verification_id`  *Unique:* (document_id, attempt_no)

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `verification_id` | TEXT | yes |  |  | yes | ^VER-\d{6}$ | VER-nnnnnn. |
| `document_id` | TEXT | yes |  |  | no |  FK -> `document(document_id)`. | Verified document. |
| `attempt_no` | INTEGER | yes |  | count | no | Contiguous from 1 per document. | 1-based attempt counter per document. |
| `verified_on` | TEXT | yes |  | date | no | >= document.submitted_on. | Date of the attempt. |
| `outcome` | TEXT | yes | VERIFIED, REJECTED, INCONCLUSIVE |  | no |  | Result of the attempt. |
| `reason_code` | TEXT | yes | OK, NAME_MISMATCH, ILLEGIBLE_SCAN, EXPIRED_AT_SUBMISSION, LOW_CONFIDENCE |  | no | OK iff VERIFIED. | Reason behind the outcome. |
| `verified_by` | TEXT | yes |  |  | no |  FK -> `staff_user(user_id)`. | Staff/system actor. |

## `beneficial_owner`

Identity of a natural person who owns business customers (reusable across businesses).

*Primary key:* `owner_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `owner_id` | TEXT | yes |  |  | yes | ^BO-\d{6}$ | BO-nnnnnn. |
| `full_name` | TEXT | yes |  |  | no | Non-empty. | Synthetic person name. |
| `date_of_birth` | TEXT | yes |  | date | no |  | Synthetic date of birth. |
| `nationality` | TEXT | yes | IN, US, GB, SG, DE, AE, BR, NG |  | no |  | Nationality. |

## `ownership_link`

Bridge table: effective-dated ownership percentage of an owner in a business customer (N:M).

*Primary key:* `ownership_link_id`  *Unique:* (customer_id, owner_id, effective_from)

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `ownership_link_id` | TEXT | yes |  |  | yes | ^OWN-\d{6}$ | OWN-nnnnnn. |
| `customer_id` | TEXT | yes |  |  | no | customer_type = BUSINESS. FK -> `customer(customer_id)`. | Business customer. |
| `owner_id` | TEXT | yes |  |  | no |  FK -> `beneficial_owner(owner_id)`. | Beneficial owner. |
| `ownership_pct` | REAL | yes |  | percent (2 dp) | no | > 0 and <= 100; effective links per customer must sum to 100.00. | Share held. |
| `effective_from` | TEXT | yes |  | date | no |  | Start of validity. |
| `effective_to` | TEXT | no |  | date | no | NULL or > effective_from. | End of validity; NULL = current. |

Table checks: `ownership_pct > 0 AND ownership_pct <= 100`; `effective_to IS NULL OR effective_to > effective_from`

## `customer_decision`

Append-only KYC decisions with the completeness snapshot used (never overwritten).

*Primary key:* `decision_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `decision_id` | TEXT | yes |  |  | yes | ^DEC-\d{6}$ | DEC-nnnnnn. |
| `customer_id` | TEXT | yes |  |  | no |  FK -> `customer(customer_id)`. | Decided customer. |
| `decision_date` | TEXT | yes |  | date | no | >= customer.onboarding_date. | Effective date of the decision. |
| `decision` | TEXT | yes | PENDING, APPROVED, ON_HOLD |  | no |  | PENDING / APPROVED / ON_HOLD. |
| `required_count` | INTEGER | yes |  | count | no |  | Requirements checked (documents + ownership for business). |
| `satisfied_count` | INTEGER | yes |  | count | no | <= required_count. | Requirements satisfied on decision_date. |
| `completeness_pct` | REAL | yes |  | percent (2 dp) | no | Recomputed from source records by the validator. | 100 * satisfied_count / required_count. |
| `reason_codes` | TEXT | no |  |  | no | MISSING_DOC:<type>|EXPIRED_DOC:<type>|REJECTED_DOC:<type>|PENDING_VERIFICATION:<type>|OWNERSHIP_MISSING|OWNERSHIP_TOTAL_NOT_100. | Pipe-separated unmet-requirement codes; empty when complete. |
| `policy_version` | TEXT | yes |  |  | no | Non-empty. | Policy version applied. |
| `decided_by` | TEXT | yes |  |  | no |  FK -> `staff_user(user_id)`. | Staff/system actor. |

Table checks: `satisfied_count <= required_count`; `completeness_pct BETWEEN 0 AND 100`

## `review_case`

Periodic review work item (current state; transitions are in review_case_event).

*Primary key:* `review_id`  *Unique:* (customer_id, review_id)

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `review_id` | TEXT | yes |  |  | yes | ^REV-CUS-\d{6}-\d{4}$ | REV-<customer_id>-<year>. |
| `customer_id` | TEXT | yes |  |  | no |  FK -> `customer(customer_id)`. | Reviewed customer. |
| `review_type` | TEXT | yes | PERIODIC |  | no |  | Review trigger. |
| `risk_level_at_open` | TEXT | yes | LOW, MEDIUM, HIGH |  | no |  | Risk level used to schedule the review. |
| `due_date` | TEXT | yes |  | date | no |  | Contractual due date = onboarding_date + k * risk period. |
| `opened_on` | TEXT | no |  | date | no | <= as_of_date. | Date opened; NULL while SCHEDULED. |
| `status` | TEXT | yes | SCHEDULED, OPEN, IN_PROGRESS, ESCALATED, COMPLETED |  | no |  | Current state = last review_case_event.to_status. |
| `assigned_to` | TEXT | no |  |  | no |  FK -> `staff_user(user_id)`. | Assigned reviewer. |
| `completed_on` | TEXT | no |  | date | no | Set iff status = COMPLETED; >= opened_on. | Completion date. |
| `resolution` | TEXT | no | NO_CHANGE, EVIDENCE_REFRESH_REQUESTED, ESCALATED_TO_COMPLIANCE, EXIT_RECOMMENDED |  | no | NOT NULL when status = COMPLETED. | Outcome; mandatory when COMPLETED. |

Table checks: `completed_on IS NULL OR opened_on IS NULL OR completed_on >= opened_on`

## `review_case_event`

Append-only review state-transition history.

*Primary key:* `review_event_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `review_event_id` | TEXT | yes |  |  | yes | ^RVE-\d{6}$ | RVE-nnnnnn. |
| `review_id` | TEXT | yes |  |  | no |  FK -> `review_case(review_id)`. | Review. |
| `from_status` | TEXT | no | SCHEDULED, OPEN, IN_PROGRESS, ESCALATED, COMPLETED |  | no |  | Previous state; NULL for the creation event. |
| `to_status` | TEXT | yes | SCHEDULED, OPEN, IN_PROGRESS, ESCALATED, COMPLETED |  | no | Must be an allowed transition from from_status. | New state. |
| `event_date` | TEXT | yes |  | date | no |  | Date of the transition. |
| `actor_id` | TEXT | yes |  |  | no |  FK -> `staff_user(user_id)`. | Staff/system actor. |
| `note` | TEXT | no |  |  | no |  | Free-text note. |

## `alert`

Alert projection with a deterministic dedupe key (idempotent processing).

*Primary key:* `alert_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `alert_id` | TEXT | yes |  |  | yes | ^ALT-\d{4}-\d{6}$ | ALT-<year>-nnnnnn. |
| `alert_type` | TEXT | yes | DOCUMENT_EXPIRING, DOCUMENT_EXPIRED, REVIEW_OVERDUE |  | no |  | Alert class. |
| `customer_id` | TEXT | yes |  |  | no |  FK -> `customer(customer_id)`. | Affected customer. |
| `document_id` | TEXT | no |  |  | no | Set iff alert_type is DOCUMENT_*. FK -> `document(document_id)`. | Document (expiry alerts). |
| `review_id` | TEXT | no |  |  | no | Set iff alert_type = REVIEW_OVERDUE. FK -> `review_case(review_id)`. | Review (overdue alerts). |
| `lead_days` | INTEGER | no | 90, 60, 30, 7 | days | no |  | Configured lead time that fired (DOCUMENT_EXPIRING only). |
| `severity` | TEXT | yes | LOW, MEDIUM, HIGH |  | no |  | Severity mapped from lead time / type. |
| `trigger_date` | TEXT | yes |  | date | no | Recomputed from document expiry / review due date and configured lead times. | Date the alert was raised. |
| `status` | TEXT | yes | OPEN, ACKNOWLEDGED, RESOLVED, SUPERSEDED |  | no |  | Current state = last alert_event. |
| `dedupe_key` | TEXT | yes |  |  | yes | Unique. | Unique business key: type|entity|cycle[|lead]. |
| `resolved_on` | TEXT | no |  | date | no |  | Date resolved/superseded. |

## `alert_event`

Append-only alert lifecycle history.

*Primary key:* `alert_event_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `alert_event_id` | TEXT | yes |  |  | yes | ^ALE-\d{6}$ | ALE-nnnnnn. |
| `alert_id` | TEXT | yes |  |  | no |  FK -> `alert(alert_id)`. | Alert. |
| `event_type` | TEXT | yes | CREATED, ACKNOWLEDGED, RESOLVED, SUPERSEDED |  | no | Must follow alert_transitions. | CREATED / ACKNOWLEDGED / RESOLVED / SUPERSEDED. |
| `event_date` | TEXT | yes |  | date | no |  | Date of the event. |
| `actor_id` | TEXT | yes |  |  | no |  FK -> `staff_user(user_id)`. | Staff/system actor. |

## `audit_event`

Append-only audit trail of every material state change (traceability).

*Primary key:* `audit_event_id`

| field | type | required | allowed values | unit | unique | validation rule | description |
|---|---|---|---|---|---|---|---|
| `audit_event_id` | TEXT | yes |  |  | yes | ^AUD-\d{4}-\d{6}$ | AUD-<year>-nnnnnn in chronological order. |
| `event_ts` | TEXT | yes |  | ISO-8601 UTC | no | Date part <= as_of_date. | UTC timestamp (deterministic synthetic time of day). |
| `entity_type` | TEXT | yes |  |  | no | Known table name. | Type of the affected record. |
| `entity_id` | TEXT | yes |  |  | no | Resolves to an existing row (polymorphic reference, validated in code). | Primary key of the affected record. |
| `customer_id` | TEXT | yes |  |  | no |  FK -> `customer(customer_id)`. | Customer the event belongs to. |
| `action` | TEXT | yes |  |  | no | Non-empty verb phrase. | What happened. |
| `actor_type` | TEXT | yes | CUSTOMER, STAFF, SYSTEM |  | no |  | Who acted. |
| `actor_id` | TEXT | yes |  |  | no | Non-empty. | Actor identifier (customer id, staff id or SYSTEM). |
| `details` | TEXT | no |  |  | no |  | Short machine-readable detail. |
