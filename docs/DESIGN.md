# Design decisions, implementation approach & strategy

## 1. Problem in one paragraph

Build an end-to-end, evaluable KYC evidence workflow: define what evidence each customer must provide (by customer
type and risk), generate realistic customers/documents/verifications/owners/reviews, derive completeness,
decisions, expiry alerts and audit history from rules, deliberately inject five labelled exceptions, and prove the
result with an independent validator, a separate ground truth and byte-reproducible output.

## 2. Key design decisions (and why)

| # | Decision | Why |
|---|---|---|
| D1 | **Python standard library only; CLI plus a stdlib HTTP app** | Zero installs = nothing to break on a reviewer's machine. The application layer (`service.py`, `server.py`, `static/index.html`) uses `http.server` + SQLite and the same `rules.py` as the generator and validator, so the app cannot disagree with the data. (FastAPI/SQLAlchemy/Spring designs were dropped for install-free reasons.) |
| D2 | **Rules live in one module (`rules.py`) used by generator and validator** | Guarantees "derived values come from source records, not random assignment" (brief step 6). Validator recomputes from the *exported CSV*, so it checks the deliverable, not generator memory. |
| D3 | **Schema defined once (`schema.py`)** -> DDL, CSV headers, data dictionary, enum/ID validation | No drift between documentation, constraints and checks. |
| D4 | **SQLite as the integrity engine** (in-memory) | PK/FK/CHECK/UNIQUE are enforced by a real engine at load time; also gives a `seed.sql` anyone can load. |
| D5 | **Config split**: `policy.json` (business rules) vs `generation.json` (distribution knobs) | Policy changes (requirements, lead times, review cycles) do not touch generator code; scale/probabilities tuned independently. JSON (not YAML) = stdlib. |
| D6 | **Append-only history, current-state projections** | `risk_rating`, `customer_decision`, `verification`, `review_case_event`, `alert_event`, `audit_event` are never overwritten; `customer.status/risk_level` and `review_case.status` are projections the validator reconciles against history. Renewals/resubmissions are new documents linked by `supersedes_document_id`. |
| D7 | **Event-sourced decision engine** | For each customer the generator replays candidate dates (verifications, expiry+1) and records a decision row only on state change: PENDING -> APPROVED -> ON_HOLD -> APPROVED. |
| D8 | **Ownership as N:M bridge** (`beneficial_owner` + `ownership_link`, effective-dated) | Owners can hold several businesses; percentages stored to 2 dp and reconciled in integer basis points (no float drift). |
| D9 | **Separate RNG stream per phase** (`Random("<seed>:<phase>")`) | Changing one phase never perturbs another; string seeding is hash-seed independent => stable across machines. |
| D10 | **Exact volumes by reconciliation, not luck** | Documents are planned per customer; if the natural total differs from the target the generator removes/adds only *leaf optional* records (renewals first, or adds prior-cycle superseded docs). |
| D11 | **Ground truth is a separate directory and never imported** | A unit test AST-scans `rules/generator/validate` for imports of the anomaly module and greps operational files for labels. |
| D12 | **`as_of_date` explicit, no wall clock anywhere** | Required for reproducibility; also makes boundary semantics testable. |

## 3. Domain model (16 tables)

```
customer_type 1--N customer 1--N risk_rating
document_type 1--N kyc_requirement N--1 customer_type        (requirement = type x risk x document type, effective-dated)
customer 1--N document 1--N verification                    document N--1 document (supersedes)
customer 1--N customer_decision          customer 1--N review_case 1--N review_case_event
customer 1--N alert 1--N alert_event     customer 1--N audit_event
customer(BUSINESS) N--M beneficial_owner  via ownership_link (effective-dated)
staff_user 1--N verification / review_case / review_case_event / alert_event / customer_decision
```

IDs (zero-padded, immutable, identical across files): `CUS-000001`, `KYC-2026-000001`, `DOC-CUS-000001-001`,
`REV-CUS-000001-2026`, `ALT-2026-000001`, plus `VER-`, `RSK-`, `BO-`, `OWN-`, `DEC-`, `RVE-`, `ALE-`, `AUD-2026-`, `GT-`, `VFN-`.
Full field-level dictionary: `DATA_DICTIONARY.md` (type, required, allowed values, unit, uniqueness, validation rule).

## 4. Business rules implemented

* **Requirements** (`kyc_requirement`): INDIVIDUAL LOW = ID+ADDRESS; MEDIUM adds TAX; HIGH adds SOURCE_OF_FUNDS.
  BUSINESS = REGISTRATION+ADDRESS+OWNERSHIP_DECLARATION (+TAX at MEDIUM, +SOURCE_OF_FUNDS at HIGH) and the
  **ownership checkpoint** (links must sum to exactly 100.00) - completeness therefore *differs by customer type*.
* **Risk** = score from documented factors (country, PEP, adverse media, cash intensity, structure complexity);
  thresholds 40 / 65. Optional re-rating before the evidence cycle (history kept, previous row end-dated).
* **Verification** (deterministic precedence): expired at submission -> `EXPIRED_AT_SUBMISSION`; normalised name
  differs -> `NAME_MISMATCH`; scan quality < 35 -> `ILLEGIBLE_SCAN`; otherwise VERIFIED. Quality 35-44 first yields an
  `INCONCLUSIVE` attempt (attempt 1) before the verified attempt 2. Boundary near-matches (upper-case name, `Pvt Ltd`
  vs `Private Limited`) correctly **pass** the name check.
* **Document status** (derived): no/inconclusive verification -> PENDING_VERIFICATION; rejected -> REJECTED; verified
  successor exists -> SUPERSEDED; `expiry < as_of` -> EXPIRED; else VALID. *Expiring today is still valid.*
* **Completeness / decision**: satisfied / required requirements (2 dp). Complete -> APPROVED; incomplete -> ON_HOLD if
  previously approved, else PENDING.
* **Periodic reviews**: cycle by risk (HIGH 6, MEDIUM 12, LOW 24 months from onboarding); a seeded sample of the 2026
  due-cohort (review capacity 3,000) is scheduled 60 days ahead, opened 30 days before due, worked, escalated or
  completed (resolution mandatory). State machine SCHEDULED -> OPEN -> IN_PROGRESS -> (ESCALATED) -> COMPLETED.
* **Alerts** (idempotent `dedupe_key`): `DOCUMENT_EXPIRING` fires at the tightest crossed lead time of **90/60/30/7 days**
  (trigger = expiry - lead), `DOCUMENT_EXPIRED` on expiry+1, `REVIEW_OVERDUE` on due+1; each is resolved/superseded by
  a later renewal or completion. Lifecycle OPEN -> ACKNOWLEDGED -> RESOLVED/SUPERSEDED.
* **Audit**: one event per material change (onboarding, rating, submission, verification, ownership, decision, every
  review/alert transition), chronologically numbered, built from the final tables so injected records are covered.

## 5. Generation strategy (parent-first, derive-don't-draw)

1. reference/master -> 2. customers + risk -> 3. evidence plan (originals, resubmissions after rejection, renewals,
prior-cycle history) reconciled to the exact volume -> 4. verification attempts -> 5. owners/links -> 6. decisions ->
7. reviews -> 8. alerts -> 9. **inject anomalies** -> 10. audit -> 11. expected-outcome labels.

Randomness only drives *inputs* (who the customer is, when evidence arrives, scan quality, name typos, how long work
takes). Every *outcome* (rejected? expired? approved? alert? overdue?) is a function of those inputs through `rules.py`.
Distribution knobs are in `config/generation.json`; realism targets achieved on the full profile: customers
80% APPROVED / 9% ON_HOLD / 11% PENDING; risk 82/15/3 LOW/MEDIUM/HIGH; ~2.2% name-mismatch rejections; 4.3% of
businesses missing ownership; documents 84% VALID / 5% SUPERSEDED / 4.5% REJECTED / 3.9% EXPIRED; 510 overdue reviews.

## 6. Edge-case & ground-truth strategy

Clean baseline first; then five injectors (40 per class on `full`, 3 on `starter`), each on **distinct customers** so
labels never overlap, each touching only the records it names, each *appending* history rather than rewriting it
(new decision, new ownership link, new review event) except the single stored field that is the defect (document
`status`, document `fingerprint`). The validator proved its worth during development: an early injector version back-dated the "extra owner" link
to before the customer's approval, which made the earlier decision recompute differently; the reconciliation check
reported it as a *defect* (not as the labelled anomaly), and the injector was fixed so the link takes effect after the approval.
`ground_truth/` holds anomaly labels and rule-derived `expected_customer_outcomes`; it is evaluation-only.

## 7. Validation strategy (what proves "no issues")

Run against the exported CSVs, in three tiers: **INTEGRITY** (PK uniqueness, ID formats, required values, enums, FKs
incl. polymorphic audit/alert refs, date ordering, review/alert transition chains), **RECONCILIATION** (risk score,
decision completeness recomputed at the decision date, verification outcome vs rule, derived document status,
customer/review/alert projections vs history, alerts vs configured lead times, requirements table vs policy file),
**BUSINESS_RULE** (the five anomaly detectors). Pass = zero INTEGRITY/RECONCILIATION findings and BUSINESS_RULE
findings match ground truth exactly (precision = recall = 1.0). 13 mutation tests (plus a clean-baseline check) corrupt an export in memory and
assert each defect class is flagged, so the validator cannot silently pass.

## 8. Reproducibility strategy

Fixed seed + explicit as-of date + per-phase RNG streams + deterministic ordering (rows by PK, ties by IDs) + no
timestamps/paths in outputs + fixed float precision + `\n` line endings + SHA-256 manifest over every output file.
`python run.py repro` regenerates twice in separate directories and diffs row counts and checksums (44 files).

## 9. Trade-offs / known limits

* One evidence cycle per customer plus renewals; deep multi-year evidence history is out of scope (called out in README).
* Duplicate-document "relatedness" is approximated by shared beneficial owners (individuals are never related).
* `REVIEW_*` ids use the campaign year (2026) rather than the due year.
* No authentication, REST API or UI - intentionally out of scope for a data/rules deliverable.


## Application layer (added after review)

* **One rules module.** Completeness, verification outcome, document status and alert derivation are the same functions the generator and validator call.
* **Append-only writes.** Replacement documents supersede (never edit) older ones; a decision row is appended whenever status, counts or reason codes change; reviews/alerts move through the configured state machines; every command writes an audit event that points at the specific event row.
* **Guards in the service, proof in the validator.** Duplicate documents across customers (409), same-day resubmission (409), ownership above 100% (422), review completion without resolution (422), illegal transitions (409), role checks (403). `GET /api/exceptions` runs the full validator on live data; tests assert zero INTEGRITY/RECONCILIATION defects after a mutation sequence.
* **Business date is explicit.** `POST /api/system/as-of` moves time forward only; `daily-run` then refreshes document statuses, re-evaluates customers and scans alerts. A tighter lead-time alert (90 -> 60 -> 30 -> 7) closes the wider one as SUPERSEDED; nothing is deleted.
* **Validator refinements for live operation:** only the last decision of a day is recomputed (earlier same-day states are superseded); partial ownership is allowed while a customer is PENDING; superseded wider-lead expiry alerts are not "unexpected".
* **Limits:** single-process server, no real authentication (the `X-Actor` header selects a seeded staff user), no file upload (document metadata only) - appropriate for an assessment demo, not production.
