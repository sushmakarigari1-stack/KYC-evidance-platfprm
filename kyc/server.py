"""Zero-dependency HTTP server: JSON API under /api + single-page browser UI at /."""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import ApiError, Service

UI_PATH = Path(__file__).resolve().parent / "static" / "index.html"


def make_routes(s: Service):
    """(method, regex, handler(match, query, body, actor) -> (status, payload))."""
    Q = lambda q, k, d="": q.get(k, [d])[0]
    return [
        ("GET", r"/api/health", lambda m, q, b, a: (200, {"status": "UP"})),
        ("GET", r"/api/system", lambda m, q, b, a: (200, s.system())),
        ("POST", r"/api/system/as-of", lambda m, q, b, a: (200, s.set_as_of(b.get("as_of_date"), a))),
        ("GET", r"/api/staff", lambda m, q, b, a: (200, s.staff())),
        ("GET", r"/api/summary", lambda m, q, b, a: (200, s.summary())),
        ("GET", r"/api/customers", lambda m, q, b, a: (200, s.list_customers(Q(q, "search"), Q(q, "status"), Q(q, "customer_type"), Q(q, "risk_level"), Q(q, "limit", "50"), Q(q, "offset", "0")))),
        ("POST", r"/api/customers", lambda m, q, b, a: (201, s.create_customer(b, a))),
        ("GET", r"/api/customers/(CUS-\d{6})", lambda m, q, b, a: (200, s.customer_detail(m[0]))),
        ("POST", r"/api/customers/(CUS-\d{6})/documents", lambda m, q, b, a: (201, s.submit_document(m[0], b, a))),
        ("POST", r"/api/customers/(CUS-\d{6})/evaluate", lambda m, q, b, a: (200, s.evaluate_customer(m[0], a))),
        ("POST", r"/api/customers/(CUS-\d{6})/owners", lambda m, q, b, a: (201, s.add_owner(m[0], b, a))),
        ("POST", r"/api/customers/(CUS-\d{6})/reviews", lambda m, q, b, a: (201, s.create_review(m[0], a))),
        ("POST", r"/api/documents/(DOC-CUS-\d{6}-\d{3})/verify", lambda m, q, b, a: (201, s.verify_document(m[0], a))),
        ("GET", r"/api/reviews", lambda m, q, b, a: (200, s.list_reviews(Q(q, "status"), Q(q, "overdue") == "true", Q(q, "limit", "100")))),
        ("GET", r"/api/reviews/(REV-CUS-\d{6}-\d{4})", lambda m, q, b, a: (200, s.review_detail(m[0]))),
        ("POST", r"/api/reviews/(REV-CUS-\d{6}-\d{4})/transition", lambda m, q, b, a: (200, s.transition_review(m[0], b, a))),
        ("GET", r"/api/alerts", lambda m, q, b, a: (200, s.list_alerts(Q(q, "status"), Q(q, "alert_type"), Q(q, "limit", "100")))),
        ("POST", r"/api/alerts/(ALT-\d{4}-\d{6})/acknowledge", lambda m, q, b, a: (200, s.acknowledge_alert(m[0], a))),
        ("POST", r"/api/admin/alerts/scan", lambda m, q, b, a: (200, s.scan_alerts(a))),
        ("POST", r"/api/admin/daily-run", lambda m, q, b, a: (200, s.daily_run(a))),
        ("GET", r"/api/audit-events", lambda m, q, b, a: (200, s.list_audit(Q(q, "customer_id"), Q(q, "limit", "100")))),
        ("GET", r"/api/exceptions", lambda m, q, b, a: (200, s.exceptions())),
    ]


def make_handler(service: Service):
    routes = [(m, re.compile("^" + p + "$"), fn) for m, p, fn in make_routes(service)]

    class Handler(BaseHTTPRequestHandler):
        server_version = "KYCEvidence/1.0"

        def log_message(self, fmt, *args):  # quiet by default
            if getattr(self.server, "verbose", False):
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _send(self, status, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send(200, UI_PATH.read_bytes(), "text/html")
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n).decode("utf-8"))
                        if not isinstance(body, dict):
                            raise ValueError
                    except ValueError:
                        return self._send(400, {"error": {"code": "BAD_JSON", "message": "request body must be a JSON object"}})
            actor = self.headers.get("X-Actor")
            path_known = False
            for m, rx, fn in routes:
                mt = rx.match(url.path)
                if not mt:
                    continue
                path_known = True
                if m != method:
                    continue
                try:
                    with service.lock:
                        status, payload = fn(mt.groups(), parse_qs(url.query), body, actor)
                    return self._send(status, payload)
                except ApiError as e:
                    return self._send(e.status, {"error": {"code": e.code, "message": e.message}})
                except Exception as e:  # never leak internals
                    sys.stderr.write(f"internal error on {method} {url.path}: {e!r}\n")
                    return self._send(500, {"error": {"code": "INTERNAL", "message": "unexpected server error"}})
            if path_known:
                return self._send(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": f"{method} not allowed here"}})
            self._send(404, {"error": {"code": "NOT_FOUND", "message": f"no route {url.path}"}})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_PATCH(self):
            self._dispatch("PATCH")

    return Handler


def serve(service: Service, host="127.0.0.1", port=8000, verbose=False):
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    httpd.verbose = verbose
    return httpd
