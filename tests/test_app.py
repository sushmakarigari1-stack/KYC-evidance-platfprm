"""End-to-end tests of the running application (HTTP server in a thread, real SQLite, stdlib only)."""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from kyc.server import serve
from kyc.service import Service

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "starter" / "operational" / "sql" / "seed.sql"
OPS, REV, ADM = "USR-OPS-001", "USR-CMP-001", "USR-ADM-001"


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = str(Path(cls.tmp.name) / "app.db")
        c = sqlite3.connect(db)
        c.executescript(SEED.read_text(encoding="utf-8"))
        c.close()
        cls.svc = Service(db, date(2026, 11, 13))
        cls.httpd = serve(cls.svc, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, actor=OPS):
        req = urllib.request.Request(self.base + path, method=method, headers={"X-Actor": actor, "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def new_customer(self, name, ctype="INDIVIDUAL", risk="LOW"):
        s, r = self.call("POST", "/api/customers", {"legal_name": name, "customer_type_id": ctype, "risk_level": risk, "country_code": "IN"})
        self.assertEqual(s, 201, r)
        return r["customer"]["customer_id"]

    def submit_verify(self, cid, dtype, number, name, issue="2026-10-01", quality=90):
        s, d = self.call("POST", "/api/customers/%s/documents" % cid, {"document_type_code": dtype, "document_number": number,
                                                                         "name_on_document": name, "issue_date": issue, "scan_quality_score": quality})
        self.assertEqual(s, 201, d)
        s, v = self.call("POST", "/api/documents/%s/verify" % d["document_id"])
        self.assertEqual(s, 201, v)
        return d, v

    def test_01_health_ui_and_unknown_routes(self):
        self.assertEqual(self.call("GET", "/api/health")[1], {"status": "UP"})
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"KYC Evidence Platform", r.read())
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.call("DELETE", "/api/customers")[0], 405)

    def test_02_onboarding_to_approval(self):
        cid = self.new_customer("Asha Test")
        self.assertEqual(self.call("GET", "/api/customers/" + cid)[1]["customer"]["status"], "PENDING")
        _, v1 = self.submit_verify(cid, "ID_PROOF", "T-ID-1", "Asha Test")
        self.assertEqual(v1["verification"]["outcome"], "VERIFIED")
        self.assertEqual(v1["decision"]["decision"], "PENDING")
        _, v2 = self.submit_verify(cid, "ADDRESS_PROOF", "T-AD-1", "Asha Test")
        self.assertEqual(v2["decision"]["decision"], "APPROVED")
        d = self.call("GET", "/api/customers/" + cid)[1]
        self.assertEqual(d["customer"]["status"], "APPROVED")
        self.assertEqual(d["evaluation"]["completeness_pct"], 100.0)
        actions = {a["action"] for a in d["audit"]}
        self.assertTrue({"CUSTOMER_ONBOARDED", "DOCUMENT_SUBMITTED", "DECISION_RECORDED"} <= actions)

    def test_03_name_mismatch_is_rejected_and_blocks_approval(self):
        cid = self.new_customer("Ravi Real")
        _, v = self.submit_verify(cid, "ID_PROOF", "T-ID-2", "Somebody Else")
        self.assertEqual(v["verification"]["outcome"], "REJECTED")
        self.assertNotEqual(self.call("GET", "/api/customers/" + cid)[1]["customer"]["status"], "APPROVED")

    def test_04_duplicates_and_same_day_resubmission(self):
        a = self.new_customer("Dup One")
        b = self.new_customer("Dup Two")
        self.submit_verify(a, "ID_PROOF", "T-ID-3", "Dup One")
        s, r = self.call("POST", "/api/customers/%s/documents" % b, {"document_type_code": "ID_PROOF", "document_number": "T-ID-3",
                                                                      "name_on_document": "Dup Two", "issue_date": "2026-10-01"})
        self.assertEqual((s, r["error"]["code"]), (409, "DUPLICATE_DOCUMENT"))
        s, r = self.call("POST", "/api/customers/%s/documents" % a, {"document_type_code": "ID_PROOF", "document_number": "T-ID-3B",
                                                                      "name_on_document": "Dup One", "issue_date": "2026-10-01"})
        self.assertEqual((s, r["error"]["code"]), (409, "SAME_DAY_RESUBMISSION"))

    def test_05_validation_and_roles(self):
        self.assertEqual(self.call("POST", "/api/customers", {"legal_name": ""})[0], 422)
        s, r = self.call("POST", "/api/customers", {"legal_name": "X", "customer_type_id": "INDIVIDUAL", "risk_level": "LOW", "country_code": "IN"}, actor=REV)
        self.assertEqual(s, 403)
        self.assertEqual(self.call("POST", "/api/admin/daily-run", actor=OPS)[0], 403)
        self.assertEqual(self.call("GET", "/api/customers/CUS-999999")[0], 404)

    def test_06_ownership_cannot_exceed_100(self):
        cid = self.new_customer("Acme Test Ltd", "BUSINESS")
        self.assertEqual(self.call("POST", "/api/customers/%s/owners" % cid, {"full_name": "A", "ownership_pct": "60"})[0], 201)
        s, r = self.call("POST", "/api/customers/%s/owners" % cid, {"full_name": "B", "ownership_pct": "50"})
        self.assertEqual(s, 422, r)
        self.assertEqual(self.call("POST", "/api/customers/%s/owners" % cid, {"full_name": "B", "ownership_pct": "40"})[0], 201)

    def test_07_review_state_machine_guards(self):
        cid = self.new_customer("Review Subject")
        self.submit_verify(cid, "ID_PROOF", "T-ID-7", "Review Subject")
        self.submit_verify(cid, "ADDRESS_PROOF", "T-AD-7", "Review Subject")
        s, r = self.call("POST", "/api/customers/%s/reviews" % cid)
        self.assertEqual(s, 201, r)
        rid = r["review"]["review_id"]
        p = "/api/reviews/%s/transition" % rid
        self.assertEqual(self.call("POST", p, {"to_status": "COMPLETED"}, actor=REV)[0], 409)  # illegal jump
        self.assertEqual(self.call("POST", p, {"to_status": "OPEN"})[0], 200)
        self.assertEqual(self.call("POST", p, {"to_status": "IN_PROGRESS"})[0], 200)
        s, r = self.call("POST", p, {"to_status": "COMPLETED", "resolution": "NO_CHANGE"})
        self.assertEqual(s, 403)  # analyst may not close
        s, r = self.call("POST", p, {"to_status": "COMPLETED"}, actor=REV)
        self.assertEqual((s, r["error"]["code"]), (422, "RESOLUTION_REQUIRED"))
        self.assertEqual(self.call("POST", p, {"to_status": "COMPLETED", "resolution": "NO_CHANGE"}, actor=REV)[0], 200)

    def test_08_alert_scan_is_idempotent_and_time_travel_works(self):
        self.assertEqual(self.call("POST", "/api/admin/alerts/scan", actor=ADM)[0], 200)
        r2 = self.call("POST", "/api/admin/alerts/scan", actor=ADM)[1]
        self.assertEqual((r2["alerts_created"], r2["alerts_closed"]), (0, 0))
        self.assertEqual(self.call("POST", "/api/system/as-of", {"as_of_date": "2026-01-01"}, actor=ADM)[0], 422)  # cannot go back
        before = self.call("GET", "/api/summary")[1]
        self.assertEqual(self.call("POST", "/api/system/as-of", {"as_of_date": "2026-12-20"}, actor=ADM)[0], 200)
        run = self.call("POST", "/api/admin/daily-run", actor=ADM)[1]
        self.assertGreater(run["document_statuses_updated"] + run["alerts_created"], 0)
        after = self.call("GET", "/api/summary")[1]
        self.assertGreaterEqual(after["documents"].get("EXPIRED", 0), before["documents"].get("EXPIRED", 0))
        again = self.call("POST", "/api/admin/daily-run", actor=ADM)[1]
        self.assertEqual((again["alerts_created"], again["document_statuses_updated"]), (0, 0))

    def test_09_operations_leave_no_integrity_or_reconciliation_defects(self):
        ex = self.call("GET", "/api/exceptions")[1]
        self.assertEqual({k: v for k, v in ex["by_category"].items() if k != "BUSINESS_RULE"}, {}, [f for f in ex["findings"] if f["category"] != "BUSINESS_RULE"])

    def test_10_report_command_snapshots_live_db(self):
        import contextlib
        import io
        from argparse import Namespace
        from kyc import cli
        out = Path(self.tmp.name) / "rep"
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.cmd_report(Namespace(db=str(Path(self.tmp.name) / "app.db"), data=str(ROOT / "data" / "starter"), as_of=None, out=str(out)))
        self.assertEqual(code, 0)
        self.assertIn("PASS", (out / "validation_summary.md").read_text())
        self.assertTrue((out / "csv" / "customer.csv").exists())


if __name__ == "__main__":
    unittest.main()
