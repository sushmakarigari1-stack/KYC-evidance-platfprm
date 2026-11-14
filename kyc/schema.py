"""Single source of truth for the data model.

From this one definition we derive: the SQLite DDL (PK/FK/CHECK/UNIQUE), the
data dictionary (type, required, allowed values, units, uniqueness, validation
rule) and the enum/ID checks used by the validator.  Nothing is described twice.
"""
from __future__ import annotations

from .common import ID_REGEX


def C(name, typ, desc, required=True, allowed=None, unit="", unique=False, rule="", fk=None):
    return dict(name=name, type=typ, desc=desc, required=required, allowed=allowed,
                unit=unit, unique=unique, rule=rule, fk=fk)


def build_tables(policy: dict) -> list[dict]:
    E = policy["enums"]
    risk = policy["risk_levels"]
    ctypes = [c["customer_type_id"] for c in policy["customer_types"]]
    dtypes = [d["document_type_code"] for d in policy["document_types"]]
    DATE = "TEXT"  # ISO-8601 yyyy-mm-dd (sorts lexicographically)

    T = []

    def table(name, desc, pk, cols, rel="", checks=(), uniques=(), order=None):
        T.append(dict(name=name, desc=desc, pk=pk, cols=cols, rel=rel,
                      checks=list(checks), uniques=[list(u) for u in uniques],
                      order=order or pk))

    table("customer_type", "Reference: customer classes that drive which evidence is required.",
          ["customer_type_id"], [
              C("customer_type_id", "TEXT", "Stable code of the customer class.", allowed=ctypes, unique=True, rule="Must be one of the configured customer types."),
              C("description", "TEXT", "Human description.", rule="Non-empty."),
              C("requires_beneficial_owner", "INTEGER", "1 when ownership must be disclosed and reconcile to 100%.", allowed=[0, 1], rule="0/1."),
          ], rel="1:N customer; 1:N kyc_requirement")

    table("staff_user", "Reference: internal actors (operations analyst, risk/compliance reviewer, administrator, system).",
          ["user_id"], [
              C("user_id", "TEXT", "Staff or system identifier.", unique=True, rule="USR-<ROLE>-nnn."),
              C("role", "TEXT", "Role of the actor.", allowed=E["staff_role"], rule="Enum."),
              C("display_name", "TEXT", "Synthetic display name.", rule="Non-empty; synthetic only."),
          ], rel="1:N verification, review_case, review_case_event, alert_event, customer_decision")

    table("document_type", "Reference: evidence catalogue with validity period.",
          ["document_type_code"], [
              C("document_type_code", "TEXT", "Stable code.", allowed=dtypes, unique=True, rule="Enum from policy."),
              C("description", "TEXT", "Human description.", rule="Non-empty."),
              C("validity_days", "INTEGER", "Days a document stays valid after issue_date; NULL = non-expiring.", required=False, unit="days", rule="NULL or > 0."),
          ], rel="1:N kyc_requirement; 1:N document")

    table("kyc_requirement", "Policy rule: which document types are mandatory for a customer type at a risk level (effective-dated, version-pinned).",
          ["kyc_requirement_id"], [
              C("kyc_requirement_id", "TEXT", "KYC-<year>-nnnnnn.", unique=True, rule=ID_REGEX["kyc_requirement"]),
              C("customer_type_id", "TEXT", "Customer class the rule applies to.", fk="customer_type(customer_type_id)", allowed=ctypes),
              C("risk_level", "TEXT", "Risk level the rule applies to.", allowed=risk),
              C("document_type_code", "TEXT", "Mandatory document type.", fk="document_type(document_type_code)", allowed=dtypes),
              C("mandatory", "INTEGER", "1 = mandatory evidence.", allowed=[0, 1], rule="Always 1 in this policy."),
              C("policy_version", "TEXT", "Policy version that defined the rule.", rule="Non-empty."),
              C("effective_from", DATE, "First day the rule applies.", unit="date"),
              C("effective_to", DATE, "Last day the rule applies; NULL = open-ended.", required=False, unit="date", rule="NULL or >= effective_from."),
          ], rel="N:1 customer_type; N:1 document_type",
          checks=["effective_to IS NULL OR effective_to >= effective_from"],
          uniques=[("customer_type_id", "risk_level", "document_type_code", "policy_version")])

    table("customer", "Customer master (current-state projection; history lives in risk_rating, customer_decision, audit_event).",
          ["customer_id"], [
              C("customer_id", "TEXT", "CUS-nnnnnn, immutable.", unique=True, rule=ID_REGEX["customer"]),
              C("customer_type_id", "TEXT", "Customer class.", fk="customer_type(customer_type_id)", allowed=ctypes),
              C("legal_name", "TEXT", "Synthetic legal name used for name-match checks.", rule="Non-empty."),
              C("country_code", "TEXT", "Country of residence / incorporation.", allowed=E["country_code"], rule="ISO-3166 alpha-2 subset."),
              C("birth_or_incorporation_date", DATE, "Date of birth (individual) or incorporation date (business).", unit="date", rule="< onboarding_date."),
              C("onboarding_date", DATE, "Date the account relationship started.", unit="date", rule="<= as_of_date."),
              C("risk_level", "TEXT", "Current rating = latest risk_rating row.", allowed=risk, rule="Equals latest risk_rating.risk_level."),
              C("status", "TEXT", "Current KYC status = latest customer_decision.", allowed=E["customer_status"], rule="Equals latest customer_decision.decision."),
              C("status_since", DATE, "Date of the latest decision.", unit="date", rule="Equals latest customer_decision.decision_date."),
          ], rel="1:N risk_rating, document, customer_decision, review_case, alert, ownership_link (business only)",
          checks=["birth_or_incorporation_date < onboarding_date"])

    table("risk_rating", "Effective-dated risk rating history (append-only; previous row is end-dated, never overwritten).",
          ["risk_rating_id"], [
              C("risk_rating_id", "TEXT", "RSK-nnnnnn.", unique=True, rule=ID_REGEX["risk_rating"]),
              C("customer_id", "TEXT", "Rated customer.", fk="customer(customer_id)"),
              C("risk_score", "INTEGER", "Sum of the factor points in score_components.", unit="points", rule="0..200 and equals sum of score_components."),
              C("risk_level", "TEXT", "Level derived from score thresholds.", allowed=risk, rule=f"Derived: <{policy['risk_score_thresholds']['MEDIUM']} LOW, {policy['risk_score_thresholds']['MEDIUM']}-{policy['risk_score_thresholds']['HIGH'] - 1} MEDIUM, >={policy['risk_score_thresholds']['HIGH']} HIGH."),
              C("score_components", "TEXT", "Factor=points pairs, ';' separated.", rule="Parsable key=int list."),
              C("rating_reason", "TEXT", "INITIAL at onboarding or RE_RATING.", allowed=E["rating_reason"]),
              C("effective_from", DATE, "Start of validity.", unit="date"),
              C("effective_to", DATE, "End of validity; NULL = current.", required=False, unit="date", rule="NULL or > effective_from; exactly one NULL per customer."),
          ], rel="N:1 customer", checks=["effective_to IS NULL OR effective_to > effective_from"])

    table("document", "Customer-specific evidence. Never overwritten: renewals/resubmissions are new rows linked via supersedes_document_id.",
          ["document_id"], [
              C("document_id", "TEXT", "DOC-<customer_id>-nnn.", unique=True, rule=ID_REGEX["document"]),
              C("customer_id", "TEXT", "Owner of the evidence.", fk="customer(customer_id)", rule="Prefix of document_id equals customer_id."),
              C("document_type_code", "TEXT", "Type of evidence.", fk="document_type(document_type_code)", allowed=dtypes),
              C("name_on_document", "TEXT", "Name printed on the document (used for name matching).", rule="Non-empty."),
              C("document_fingerprint", "TEXT", "SHA-256 of the synthetic document number (no raw numbers stored).", rule="64 hex chars; unique across customers."),
              C("issue_date", DATE, "Date the issuer issued the document.", unit="date"),
              C("expiry_date", DATE, "Expiry date; NULL for non-expiring types.", required=False, unit="date", rule="NULL iff validity_days is NULL; else issue_date + validity_days."),
              C("submitted_on", DATE, "Date the customer submitted the document.", unit="date", rule=">= issue_date and >= customer.onboarding_date."),
              C("scan_quality_score", "INTEGER", "Image/scan quality used by verification rules.", allowed="0..100", unit="score 0-100"),
              C("status", "TEXT", "Document lifecycle status as of as_of_date (derived from verification + expiry + supersession).", allowed=E["document_status"], rule="Equals derived status."),
              C("supersedes_document_id", "TEXT", "Older document this one replaces (renewal / resubmission).", required=False, fk="document(document_id)", rule="Same customer and type; older submitted_on."),
          ], rel="N:1 customer; N:1 document_type; 1:N verification; self-reference supersedes",
          checks=["expiry_date IS NULL OR expiry_date >= issue_date", "submitted_on >= issue_date", "scan_quality_score BETWEEN 0 AND 100"])

    table("verification", "Append-only verification attempts for documents.",
          ["verification_id"], [
              C("verification_id", "TEXT", "VER-nnnnnn.", unique=True, rule=ID_REGEX["verification"]),
              C("document_id", "TEXT", "Verified document.", fk="document(document_id)"),
              C("attempt_no", "INTEGER", "1-based attempt counter per document.", unit="count", rule="Contiguous from 1 per document."),
              C("verified_on", DATE, "Date of the attempt.", unit="date", rule=">= document.submitted_on."),
              C("outcome", "TEXT", "Result of the attempt.", allowed=E["verification_outcome"]),
              C("reason_code", "TEXT", "Reason behind the outcome.", allowed=E["verification_reason"], rule="OK iff VERIFIED."),
              C("verified_by", "TEXT", "Staff/system actor.", fk="staff_user(user_id)"),
          ], rel="N:1 document", uniques=[("document_id", "attempt_no")], order=["verification_id"])

    table("beneficial_owner", "Identity of a natural person who owns business customers (reusable across businesses).",
          ["owner_id"], [
              C("owner_id", "TEXT", "BO-nnnnnn.", unique=True, rule=ID_REGEX["beneficial_owner"]),
              C("full_name", "TEXT", "Synthetic person name.", rule="Non-empty."),
              C("date_of_birth", DATE, "Synthetic date of birth.", unit="date"),
              C("nationality", "TEXT", "Nationality.", allowed=E["country_code"]),
          ], rel="N:M customer via ownership_link")

    table("ownership_link", "Bridge table: effective-dated ownership percentage of an owner in a business customer (N:M).",
          ["ownership_link_id"], [
              C("ownership_link_id", "TEXT", "OWN-nnnnnn.", unique=True, rule=ID_REGEX["ownership_link"]),
              C("customer_id", "TEXT", "Business customer.", fk="customer(customer_id)", rule="customer_type = BUSINESS."),
              C("owner_id", "TEXT", "Beneficial owner.", fk="beneficial_owner(owner_id)"),
              C("ownership_pct", "REAL", "Share held.", unit="percent (2 dp)", rule="> 0 and <= 100; effective links per customer must sum to 100.00."),
              C("effective_from", DATE, "Start of validity.", unit="date"),
              C("effective_to", DATE, "End of validity; NULL = current.", required=False, unit="date", rule="NULL or > effective_from."),
          ], rel="N:1 customer; N:1 beneficial_owner",
          checks=["ownership_pct > 0 AND ownership_pct <= 100", "effective_to IS NULL OR effective_to > effective_from"],
          uniques=[("customer_id", "owner_id", "effective_from")])

    table("customer_decision", "Append-only KYC decisions with the completeness snapshot used (never overwritten).",
          ["decision_id"], [
              C("decision_id", "TEXT", "DEC-nnnnnn.", unique=True, rule=ID_REGEX["customer_decision"]),
              C("customer_id", "TEXT", "Decided customer.", fk="customer(customer_id)"),
              C("decision_date", DATE, "Effective date of the decision.", unit="date", rule=">= customer.onboarding_date."),
              C("decision", "TEXT", "PENDING / APPROVED / ON_HOLD.", allowed=E["decision"]),
              C("required_count", "INTEGER", "Requirements checked (documents + ownership for business).", unit="count"),
              C("satisfied_count", "INTEGER", "Requirements satisfied on decision_date.", unit="count", rule="<= required_count."),
              C("completeness_pct", "REAL", "100 * satisfied_count / required_count.", unit="percent (2 dp)", rule="Recomputed from source records by the validator."),
              C("reason_codes", "TEXT", "Pipe-separated unmet-requirement codes; empty when complete.", required=False, rule="MISSING_DOC:<type>|EXPIRED_DOC:<type>|REJECTED_DOC:<type>|PENDING_VERIFICATION:<type>|OWNERSHIP_MISSING|OWNERSHIP_TOTAL_NOT_100."),
              C("policy_version", "TEXT", "Policy version applied.", rule="Non-empty."),
              C("decided_by", "TEXT", "Staff/system actor.", fk="staff_user(user_id)"),
          ], rel="N:1 customer",
          checks=["satisfied_count <= required_count", "completeness_pct BETWEEN 0 AND 100"])

    table("review_case", "Periodic review work item (current state; transitions are in review_case_event).",
          ["review_id"], [
              C("review_id", "TEXT", "REV-<customer_id>-<year>.", unique=True, rule=ID_REGEX["review_case"]),
              C("customer_id", "TEXT", "Reviewed customer.", fk="customer(customer_id)"),
              C("review_type", "TEXT", "Review trigger.", allowed=E["review_type"]),
              C("risk_level_at_open", "TEXT", "Risk level used to schedule the review.", allowed=risk),
              C("due_date", DATE, "Contractual due date = onboarding_date + k * risk period.", unit="date"),
              C("opened_on", DATE, "Date opened; NULL while SCHEDULED.", required=False, unit="date", rule="<= as_of_date."),
              C("status", "TEXT", "Current state = last review_case_event.to_status.", allowed=E["review_status"]),
              C("assigned_to", "TEXT", "Assigned reviewer.", required=False, fk="staff_user(user_id)"),
              C("completed_on", DATE, "Completion date.", required=False, unit="date", rule="Set iff status = COMPLETED; >= opened_on."),
              C("resolution", "TEXT", "Outcome; mandatory when COMPLETED.", required=False, allowed=E["review_resolution"], rule="NOT NULL when status = COMPLETED."),
          ], rel="N:1 customer; 1:N review_case_event",
          checks=["completed_on IS NULL OR opened_on IS NULL OR completed_on >= opened_on"],
          uniques=[("customer_id", "review_id")])

    table("review_case_event", "Append-only review state-transition history.",
          ["review_event_id"], [
              C("review_event_id", "TEXT", "RVE-nnnnnn.", unique=True, rule=ID_REGEX["review_case_event"]),
              C("review_id", "TEXT", "Review.", fk="review_case(review_id)"),
              C("from_status", "TEXT", "Previous state; NULL for the creation event.", required=False, allowed=E["review_status"]),
              C("to_status", "TEXT", "New state.", allowed=E["review_status"], rule="Must be an allowed transition from from_status."),
              C("event_date", DATE, "Date of the transition.", unit="date"),
              C("actor_id", "TEXT", "Staff/system actor.", fk="staff_user(user_id)"),
              C("note", "TEXT", "Free-text note.", required=False),
          ], rel="N:1 review_case")

    table("alert", "Alert projection with a deterministic dedupe key (idempotent processing).",
          ["alert_id"], [
              C("alert_id", "TEXT", "ALT-<year>-nnnnnn.", unique=True, rule=ID_REGEX["alert"]),
              C("alert_type", "TEXT", "Alert class.", allowed=E["alert_type"]),
              C("customer_id", "TEXT", "Affected customer.", fk="customer(customer_id)"),
              C("document_id", "TEXT", "Document (expiry alerts).", required=False, fk="document(document_id)", rule="Set iff alert_type is DOCUMENT_*."),
              C("review_id", "TEXT", "Review (overdue alerts).", required=False, fk="review_case(review_id)", rule="Set iff alert_type = REVIEW_OVERDUE."),
              C("lead_days", "INTEGER", "Configured lead time that fired (DOCUMENT_EXPIRING only).", required=False, unit="days", allowed=[90, 60, 30, 7]),
              C("severity", "TEXT", "Severity mapped from lead time / type.", allowed=E["alert_severity"]),
              C("trigger_date", DATE, "Date the alert was raised.", unit="date", rule="Recomputed from document expiry / review due date and configured lead times."),
              C("status", "TEXT", "Current state = last alert_event.", allowed=E["alert_status"]),
              C("dedupe_key", "TEXT", "Unique business key: type|entity|cycle[|lead].", unique=True, rule="Unique."),
              C("resolved_on", DATE, "Date resolved/superseded.", required=False, unit="date"),
          ], rel="N:1 customer; N:1 document or review_case; 1:N alert_event")

    table("alert_event", "Append-only alert lifecycle history.",
          ["alert_event_id"], [
              C("alert_event_id", "TEXT", "ALE-nnnnnn.", unique=True, rule=ID_REGEX["alert_event"]),
              C("alert_id", "TEXT", "Alert.", fk="alert(alert_id)"),
              C("event_type", "TEXT", "CREATED / ACKNOWLEDGED / RESOLVED / SUPERSEDED.", allowed=E["alert_event_type"], rule="Must follow alert_transitions."),
              C("event_date", DATE, "Date of the event.", unit="date"),
              C("actor_id", "TEXT", "Staff/system actor.", fk="staff_user(user_id)"),
          ], rel="N:1 alert")

    table("audit_event", "Append-only audit trail of every material state change (traceability).",
          ["audit_event_id"], [
              C("audit_event_id", "TEXT", "AUD-<year>-nnnnnn in chronological order.", unique=True, rule=ID_REGEX["audit_event"]),
              C("event_ts", "TEXT", "UTC timestamp (deterministic synthetic time of day).", unit="ISO-8601 UTC", rule="Date part <= as_of_date."),
              C("entity_type", "TEXT", "Type of the affected record.", rule="Known table name."),
              C("entity_id", "TEXT", "Primary key of the affected record.", rule="Resolves to an existing row (polymorphic reference, validated in code)."),
              C("customer_id", "TEXT", "Customer the event belongs to.", fk="customer(customer_id)"),
              C("action", "TEXT", "What happened.", rule="Non-empty verb phrase."),
              C("actor_type", "TEXT", "Who acted.", allowed=E["actor_type"]),
              C("actor_id", "TEXT", "Actor identifier (customer id, staff id or SYSTEM).", rule="Non-empty."),
              C("details", "TEXT", "Short machine-readable detail.", required=False),
          ], rel="N:1 customer; polymorphic entity reference")
    return T


# ------------------------------------------------------------------ DDL
def _sql_in(vals):
    return ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in vals)


def ddl(tables: list[dict]) -> str:
    out = []
    for t in tables:
        lines = []
        for c in t["cols"]:
            s = f'  {c["name"]} {c["type"]}'
            if c["required"]:
                s += " NOT NULL"
            allowed = c["allowed"]
            if isinstance(allowed, list):
                s += f' CHECK ({c["name"]} IN ({_sql_in(allowed)}))' if c["required"] else \
                     f' CHECK ({c["name"]} IS NULL OR {c["name"]} IN ({_sql_in(allowed)}))'
            lines.append(s)
        lines.append(f'  PRIMARY KEY ({", ".join(t["pk"])})')
        for c in t["cols"]:
            if c["fk"]:
                lines.append(f'  FOREIGN KEY ({c["name"]}) REFERENCES {c["fk"]}')
        for u in t["uniques"]:
            lines.append(f'  UNIQUE ({", ".join(u)})')
        for ck in t["checks"]:
            lines.append(f"  CHECK ({ck})")
        out.append(f'CREATE TABLE {t["name"]} (\n' + ",\n".join(lines) + "\n);")
    return "\n\n".join(out) + "\n"


def column_names(table: dict) -> list[str]:
    return [c["name"] for c in table["cols"]]
