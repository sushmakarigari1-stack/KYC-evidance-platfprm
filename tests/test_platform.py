"""Acceptance + unit + mutation tests.  Run with:  python -m unittest discover -s tests -v   (or pytest)."""
import ast
import copy
import re
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kyc import cli, export, rules, validate as V  # noqa: E402
from kyc.common import ID_REGEX, load_generation_cfg, load_policy  # noqa: E402
from kyc.schema import build_tables  # noqa: E402

POLICY, GEN, TABLES = load_policy(), load_generation_cfg(), build_tables(load_policy())
AS_OF = date(2026, 11, 13)
TMP = tempfile.TemporaryDirectory()


def args(profile, **kw):
    base = dict(cmd="all", profile=profile, seed=GEN["default_seed"], as_of=AS_OF, customers=None, businesses=None, documents=None, reviews=None)
    base.update(kw)
    return Namespace(**base)


FULL_DIR, STARTER_DIR = Path(TMP.name) / "full", Path(TMP.name) / "starter"
FULL_MANIFEST = STARTER_MANIFEST = None


def setUpModule():
    global FULL_MANIFEST, STARTER_MANIFEST
    FULL_MANIFEST = cli.build(args("full"), FULL_DIR, quiet=True)
    STARTER_MANIFEST = cli.build(args("starter"), STARTER_DIR, quiet=True)


def tearDownModule():
    TMP.cleanup()


class TestAcceptanceFullProfile(unittest.TestCase):
    """Brief: 8,000 customers, 20,000 documents, 1,000 business entities, 3,000 review cases; seed 20261022."""

    def test_volumes_exact(self):
        c = FULL_MANIFEST["row_counts"]
        self.assertEqual(c["customer"], 8000)
        self.assertEqual(c["document"], 20000)
        self.assertEqual(c["review_case"], 3000)
        T = export.read_csv_tables(FULL_DIR, TABLES)
        self.assertEqual(sum(1 for x in T["customer"] if x["customer_type_id"] == "BUSINESS"), 1000)
        self.assertEqual(FULL_MANIFEST["meta"]["seed"], 20261022)

    def test_validation_passes_and_all_injected_cases_found(self):
        self.assertTrue(FULL_MANIFEST["validation_passed"])
        _, ev, _ = cli.validate_dir(FULL_DIR)
        self.assertEqual(ev["missed"], 0)
        self.assertEqual(ev["unexpected_business_findings"], 0)
        self.assertEqual(ev["integrity_or_reconciliation_defects"], 0)
        for code, v in ev["per_class"].items():
            self.assertGreater(v["injected"], 0, code)
            self.assertEqual(v["recall"], 1.0, code)

    def test_required_baseline_coverage_present(self):
        T = export.read_csv_tables(FULL_DIR, TABLES)
        self.assertGreater(sum(d["status"] == "EXPIRED" for d in T["document"]), 0, "expired documents")
        self.assertGreater(sum(v["reason_code"] == "NAME_MISMATCH" for v in T["verification"]), 0, "name mismatches")
        biz = {c["customer_id"] for c in T["customer"] if c["customer_type_id"] == "BUSINESS"}
        self.assertGreater(len(biz - {l["customer_id"] for l in T["ownership_link"]}), 0, "missing ownership records")
        self.assertGreater(sum(rules.is_overdue_review(r, AS_OF) for r in T["review_case"]), 0, "overdue reviews")
        self.assertGreater(sum(d["supersedes_document_id"] is not None for d in T["document"]), 0, "history kept via supersedes links")

    def test_id_conventions(self):
        T = export.read_csv_tables(FULL_DIR, TABLES)
        self.assertEqual(T["customer"][0]["customer_id"], "CUS-000001")
        self.assertEqual(T["kyc_requirement"][0]["kyc_requirement_id"], "KYC-2026-000001")
        self.assertEqual(T["alert"][0]["alert_id"], "ALT-2026-000001")
        self.assertTrue(all(re.match(r"^DOC-CUS-\d{6}-\d{3}$", d["document_id"]) for d in T["document"]))
        self.assertTrue(all(re.match(r"^REV-CUS-\d{6}-2026$", r["review_id"]) for r in T["review_case"]))
        for name, rx in ID_REGEX.items():
            pk = next(t for t in TABLES if t["name"] == name)["pk"][0]
            self.assertTrue(all(re.match(rx, r[pk]) for r in T[name]), name)
        self.assertEqual(len({d["document_id"] for d in T["document"]}), 20000)
        first = {}  # DOC-<customer-id>-001 : per-customer counter is contiguous from 001
        for d in T["document"]:
            first.setdefault(d["customer_id"], []).append(int(d["document_id"][-3:]))
        self.assertTrue(all(sorted(v) == list(range(1, len(v) + 1)) for v in first.values()))


class TestStarterAndScaling(unittest.TestCase):
    def test_starter_passes(self):
        self.assertTrue(STARTER_MANIFEST["validation_passed"])
        self.assertEqual(STARTER_MANIFEST["row_counts"]["document"], 1000)

    def test_scale_is_parameterised_with_exact_counts(self):
        with tempfile.TemporaryDirectory() as d:
            m = cli.build(args("starter", customers=200, businesses=30, documents=500, reviews=60), Path(d) / "x", quiet=True)
            self.assertEqual((m["row_counts"]["customer"], m["row_counts"]["document"], m["row_counts"]["review_case"]), (200, 500, 60))
            self.assertTrue(m["validation_passed"])


class TestReproducibility(unittest.TestCase):
    def test_same_seed_same_checksums(self):
        with tempfile.TemporaryDirectory() as d:
            a = cli.build(args("starter"), Path(d) / "a", quiet=True)
            b = cli.build(args("starter"), Path(d) / "b", quiet=True)
        self.assertEqual(a["row_counts"], b["row_counts"])
        self.assertEqual(a["sha256"], b["sha256"])

    def test_different_seed_differs(self):
        with tempfile.TemporaryDirectory() as d:
            a = cli.build(args("starter"), Path(d) / "a", quiet=True)
            b = cli.build(args("starter", seed=7), Path(d) / "b", quiet=True)
        self.assertNotEqual(a["sha256"]["operational/csv/customer.csv"], b["sha256"]["operational/csv/customer.csv"])


class TestGroundTruthSeparation(unittest.TestCase):
    def test_no_leak_into_operational_files(self):
        op = STARTER_DIR / "operational"
        for p in op.rglob("*"):
            if p.is_file():
                text = p.read_text(encoding="utf-8")
                self.assertNotIn("ground_truth", text.lower(), p.name)
                for code in ("EXPIRED_DOCUMENT_MARKED_VALID", "APPROVED_WITH_MISSING_EVIDENCE", "OWNERSHIP_OVER_100", "DUPLICATE_DOCUMENT_ACROSS"):
                    self.assertNotIn(code, text, p.name)
        for t in TABLES:
            for c in t["cols"]:
                self.assertNotIn("anomaly", c["name"])
                self.assertNotIn("issue_type", c["name"])
        self.assertTrue((STARTER_DIR / "ground_truth" / "ground_truth.csv").exists())

    def test_logic_modules_never_import_truth(self):
        for mod in ("rules.py", "generator.py", "validate.py"):
            tree = ast.parse((ROOT / "kyc" / mod).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [a.name for a in node.names]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                for n in names:
                    self.assertNotIn("anomalies", n, mod)


class TestRulesBoundaries(unittest.TestCase):
    leads = POLICY["alerts"]

    def _doc(self, expiry, vdate=None):
        d = dict(document_id="DOC-CUS-000001-001", customer_id="CUS-000001", document_type_code="ADDRESS_PROOF", name_on_document="A B",
                 issue_date=expiry - timedelta(days=365), expiry_date=expiry, submitted_on=expiry - timedelta(days=365), scan_quality_score=90,
                 status="VALID", supersedes_document_id=None)
        v = dict(verification_id="VER-000001", document_id=d["document_id"], attempt_no=1, verified_on=vdate or d["submitted_on"], outcome="VERIFIED", reason_code="OK", verified_by="USR-SYS-000")
        return d, {d["document_id"]: [v]}

    def alerts(self, offset):
        d, v = self._doc(AS_OF + timedelta(days=offset))
        return rules.derive_alerts([d], v, [], self.leads, AS_OF)

    def test_lead_time_boundaries(self):
        self.assertEqual(self.alerts(91), [])                                             # just outside the longest lead
        self.assertEqual([a["lead_days"] for a in self.alerts(90)], [90])                 # exactly 90 days -> fires
        self.assertEqual([a["lead_days"] for a in self.alerts(61)], [90])
        self.assertEqual([a["lead_days"] for a in self.alerts(60)], [60])
        self.assertEqual([a["lead_days"] for a in self.alerts(30)], [30])
        self.assertEqual([a["lead_days"] for a in self.alerts(8)], [30])
        self.assertEqual([a["lead_days"] for a in self.alerts(7)], [7])
        a = self.alerts(90)[0]
        self.assertEqual(a["trigger_date"], AS_OF)                                         # trigger = expiry - lead

    def test_expiring_today_is_not_expired(self):
        types = [a["alert_type"] for a in self.alerts(0)]
        self.assertEqual(types, ["DOCUMENT_EXPIRING"])

    def test_expired_yesterday_raises_expired_alert(self):
        types = sorted(a["alert_type"] for a in self.alerts(-1))
        self.assertEqual(types, ["DOCUMENT_EXPIRED", "DOCUMENT_EXPIRING"])

    def test_renewal_before_lead_window_suppresses_alert(self):
        d, v = self._doc(AS_OF + timedelta(days=20))
        new = dict(d, document_id="DOC-CUS-000001-002", supersedes_document_id=d["document_id"], submitted_on=AS_OF - timedelta(days=100), issue_date=AS_OF - timedelta(days=100),
                   expiry_date=AS_OF + timedelta(days=265))
        v[new["document_id"]] = [dict(v[d["document_id"]][0], verification_id="VER-000002", document_id=new["document_id"], verified_on=AS_OF - timedelta(days=99))]
        al = rules.derive_alerts([d, new], v, [], self.leads, AS_OF)
        self.assertEqual(al, [])

    def test_name_normalisation(self):
        self.assertEqual(rules.normalize_name("Apex Traders Pvt Ltd"), rules.normalize_name("APEX TRADERS PRIVATE LIMITED"))
        self.assertNotEqual(rules.normalize_name("Aarav Sharma"), rules.normalize_name("Sharma Aarav"))

    def test_verification_rule_precedence(self):
        v = POLICY["verification"]
        base = dict(expiry_date=None, submitted_on=AS_OF, name_on_document="A B", scan_quality_score=80)
        self.assertEqual(rules.verification_outcome(base, "A B", v), ("VERIFIED", "OK"))
        self.assertEqual(rules.verification_outcome(dict(base, name_on_document="A C"), "A B", v), ("REJECTED", "NAME_MISMATCH"))
        self.assertEqual(rules.verification_outcome(dict(base, scan_quality_score=34), "A B", v), ("REJECTED", "ILLEGIBLE_SCAN"))
        self.assertEqual(rules.verification_outcome(dict(base, scan_quality_score=35), "A B", v), ("VERIFIED", "OK"))   # boundary
        self.assertEqual(rules.verification_outcome(dict(base, expiry_date=AS_OF - timedelta(days=1)), "A B", v), ("REJECTED", "EXPIRED_AT_SUBMISSION"))

    def test_ownership_must_be_exactly_100(self):
        L = lambda pct: dict(effective_from=date(2020, 1, 1), effective_to=None, ownership_pct=pct)
        self.assertEqual(rules.ownership_state([L(33.33), L(33.33), L(33.34)], AS_OF), (10000, 3))
        self.assertEqual(rules.ownership_state([L(60), L(40.01)], AS_OF)[0], 10001)
        self.assertEqual(rules.ownership_state([], AS_OF), (0, 0))


class TestValidatorSensitivity(unittest.TestCase):
    """Corrupt a clean export in memory; the validator must flag each defect class (no silent passes)."""

    @classmethod
    def setUpClass(cls):
        cls.base = export.read_csv_tables(STARTER_DIR, TABLES)

    def run_v(self, mutate):
        T = copy.deepcopy(self.base)
        mutate(T)
        return {f["rule_code"] for f in V.validate(T, TABLES, POLICY, AS_OF)}

    def test_clean_baseline_has_only_expected_codes(self):
        codes = self.run_v(lambda T: None)
        self.assertTrue(codes <= set(V.BUSINESS_CODES), codes)

    def test_pk_duplicate(self):
        self.assertIn("PK_DUPLICATE", self.run_v(lambda T: T["customer"].append(dict(T["customer"][0]))))

    def test_fk_orphan(self):
        def m(T): T["document"][0]["customer_id"] = "CUS-999999"
        self.assertIn("FK_ORPHAN", self.run_v(m))

    def test_enum_violation(self):
        def m(T): T["customer"][0]["status"] = "BOGUS"
        self.assertIn("ENUM_VIOLATION", self.run_v(m))

    def test_id_format(self):
        def m(T): T["customer"][0]["customer_id"] = "CUST-1"
        self.assertIn("ID_FORMAT", self.run_v(m))

    def test_date_order(self):
        def m(T):
            d = next(x for x in T["document"] if x["expiry_date"])
            d["expiry_date"] = d["issue_date"] - timedelta(days=1)
        self.assertIn("DATE_ORDER", self.run_v(m))

    def test_decision_arithmetic(self):
        def m(T): T["customer_decision"][0]["satisfied_count"] += 1
        self.assertIn("DECISION_RECOMPUTE_MISMATCH", self.run_v(m))

    def test_ownership_under_100(self):
        def m(T):
            l = T["ownership_link"][0]
            l["ownership_pct"] = round(l["ownership_pct"] - 1.0, 2) if l["ownership_pct"] > 2 else 0.5
        self.assertTrue({"OWNERSHIP_UNDER_100", "OWNERSHIP_OVER_100"} & self.run_v(m))

    def test_missing_alert(self):
        def m(T): T["alert"] = T["alert"][1:]
        self.assertTrue({"ALERT_MISSING", "PROJECTION_MISMATCH"} & self.run_v(m))

    def test_wrong_lead_time_alert(self):
        def m(T):
            a = next(x for x in T["alert"] if x["alert_type"] == "DOCUMENT_EXPIRING")
            a["trigger_date"] = a["trigger_date"] + timedelta(days=3)
        self.assertIn("ALERT_MISMATCH", self.run_v(m))

    def test_invalid_review_transition(self):
        def m(T):
            e = next(x for x in T["review_case_event"] if x["to_status"] == "OPEN")
            e["to_status"] = "COMPLETED"
        self.assertIn("LIFECYCLE_TRANSITION", self.run_v(m))

    def test_projection_drift(self):
        def m(T):
            c = next(x for x in T["customer"] if x["status"] == "APPROVED")
            c["status"] = "ON_HOLD"
        self.assertIn("PROJECTION_MISMATCH", self.run_v(m))

    def test_document_status_drift(self):
        def m(T):
            d = next(x for x in T["document"] if x["status"] == "REJECTED")
            d["status"] = "VALID"
        self.assertIn("DOCUMENT_STATUS_MISMATCH", self.run_v(m))

    def test_audit_orphan(self):
        def m(T): T["audit_event"][0]["entity_id"] = "DOC-CUS-999999-001"
        self.assertIn("AUDIT_ENTITY_ORPHAN", self.run_v(m))


class TestOutputs(unittest.TestCase):
    def test_all_formats_and_documents_exist(self):
        for sub in ("operational/csv/customer.csv", "operational/json/customer.json", "operational/sql/seed.sql",
                    "ground_truth/ground_truth.csv", "ground_truth/ground_truth.json", "reports/validation_summary.md",
                    "reports/metrics.json", "source_manifest.json", "manifest.json", "data_dictionary.csv"):
            self.assertTrue((STARTER_DIR / sub).exists(), sub)

    def test_sql_seed_loads_into_sqlite_with_constraints(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript((STARTER_DIR / "operational" / "sql" / "seed.sql").read_text(encoding="utf-8"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM customer").fetchone()[0], 400)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()

    def test_data_dictionary_covers_every_column(self):
        text = (ROOT / "docs" / "DATA_DICTIONARY.md").read_text(encoding="utf-8") if (ROOT / "docs" / "DATA_DICTIONARY.md").exists() else ""
        if not text:
            self.skipTest("run `python run.py dictionary` first")
        for t in TABLES:
            for c in t["cols"]:
                self.assertIn(f"`{c['name']}`", text)


if __name__ == "__main__":
    unittest.main()
