"""Tests for tools/grafana/apply (stdlib only, no network beyond localhost).

python3 -m unittest discover -s tools/grafana
"""

import importlib.machinery
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

HERE = Path(__file__).resolve().parent
_loader = importlib.machinery.SourceFileLoader("grafana_apply", str(HERE / "apply"))
_spec = importlib.util.spec_from_loader("grafana_apply", _loader)
apply = importlib.util.module_from_spec(_spec)
_loader.exec_module(apply)

TOKEN = "glsa_secretSECRET_1234"


class EchoAuth(BaseHTTPRequestHandler):
    """Answers every request with an error whose body echoes the Authorization header."""

    away = ""  # set to another server's URL: a different origin

    def do_GET(self):
        if self.path == "/html":
            body = b"<html>login</html>"
            self.send_response(200)
        elif self.path in ("/redirect-away", "/redirect-here"):
            body = b""
            self.send_response(302)
            self.send_header("Location", self.away + "/ok" if self.path == "/redirect-away" else "/ok")
        elif self.path == "/ok":
            body = json.dumps({"auth": bool(self.headers["Authorization"])}).encode()
            self.send_response(200)
        elif self.path == "/straddle":
            # The token crosses the 500-character cut of an error body.
            body = ("x" * 490 + self.headers["Authorization"].removeprefix("Bearer ")).encode()
            self.send_response(500)
        else:
            body = json.dumps({"message": f"bad auth: {self.headers['Authorization']}"}).encode()
            self.send_response(int(self.path.strip("/") or 500))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Recorder(BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def do_GET(self):
        Recorder.seen.append(self.headers["Authorization"])
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class TokenHandling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), EchoAuth)
        cls.other = HTTPServer(("127.0.0.1", 0), Recorder)
        for server in (cls.server, cls.other):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        EchoAuth.away = f"http://127.0.0.1:{cls.other.server_port}"

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.other):
            server.shutdown()
            server.server_close()

    def assertNoToken(self, text):
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("Bearer " + TOKEN[:6], text)

    def test_trailing_newline_is_stripped(self):
        grafana = apply.Grafana(self.url, TOKEN + "\n")
        with self.assertRaises(SystemExit) as caught:
            grafana.call("GET", "/500")
        self.assertIn("HTTP 500", str(caught.exception))
        self.assertIn("Bearer <token>", str(caught.exception))
        self.assertNoToken(str(caught.exception))

    def test_invalid_header_characters_fail_without_the_token(self):
        for bad in (TOKEN[:5] + "\n" + TOKEN[5:], TOKEN + " x", "", "tökén"):
            with self.assertRaises(SystemExit) as caught:
                apply.Grafana(self.url, bad)
            self.assertNoToken(str(caught.exception))
            self.assertNotIn(bad.strip() or "\0", str(caught.exception))

    def test_response_bodies_are_redacted(self):
        grafana = apply.Grafana(self.url, TOKEN)
        with self.assertRaises(apply.Refused) as refused:
            grafana.call("GET", "/403")
        self.assertIn("<token>", str(refused.exception))
        self.assertNotIn(TOKEN, str(refused.exception))
        with self.assertRaises(SystemExit) as caught:
            grafana.call("GET", "/502")
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_connection_and_parse_errors(self):
        grafana = apply.Grafana(self.url, TOKEN)
        with self.assertRaises(SystemExit) as caught:
            grafana.call("GET", "/html")
        self.assertIn("isn't JSON", str(caught.exception))
        closed = apply.Grafana("http://127.0.0.1:9", TOKEN)
        with self.assertRaises(SystemExit) as caught:
            closed.call("GET", "/api/folders")
        self.assertNoToken(str(caught.exception))

    def test_redirects_to_another_origin_are_refused(self):
        grafana = apply.Grafana(self.url, TOKEN)
        Recorder.seen.clear()
        with self.assertRaises(SystemExit) as caught:
            grafana.call("GET", "/redirect-away")
        self.assertIn("not following it", str(caught.exception))
        self.assertNoToken(str(caught.exception))
        self.assertEqual(Recorder.seen, [])

    def test_same_origin_redirects_are_followed(self):
        self.assertEqual(apply.Grafana(self.url, TOKEN).call("GET", "/redirect-here"), {"auth": True})

    def test_redaction_happens_before_truncation(self):
        with self.assertRaises(SystemExit) as caught:
            apply.Grafana(self.url, TOKEN).call("GET", "/straddle")
        self.assertNotIn(TOKEN[:4], str(caught.exception))


class Urls(unittest.TestCase):
    def test_accepted(self):
        self.assertEqual(apply.check_url("https://kiki.grafana.net/"), "https://kiki.grafana.net")
        self.assertEqual(apply.check_url(" http://localhost:3000 "), "http://localhost:3000")
        self.assertEqual(apply.check_url("http://127.0.0.1:3000/grafana/"), "http://127.0.0.1:3000/grafana")

    def test_rejected_without_quoting_the_url(self):
        for url in (
            "http://kiki.grafana.net",
            "ftp://kiki.grafana.net",
            "https://user:SECRETPW@kiki.grafana.net",
            "https://kiki.grafana.net/?auth=SECRETPW",
            "https://kiki.grafana.net/#SECRETPW",
            "https://kiki.grafana.net:notaport",
            "kiki.grafana.net",
            "",
        ):
            with self.subTest(url=url), self.assertRaises(SystemExit) as caught:
                apply.check_url(url)
            self.assertNotIn("SECRETPW", str(caught.exception))
            self.assertNotIn("grafana.net:", str(caught.exception))


class Dotenv(unittest.TestCase):
    def test_values(self):
        cases = {
            "A=plain": "plain",
            "B=value # comment": "value",
            "C='quoted # not a comment' # comment": "quoted # not a comment",
            'D="double" # comment': "double",
            "E=no#comment": "no#comment",
            "export F=exported": "exported",
            "G=": "",
            "H=  spaced  ": "spaced",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dotenv"
            path.write_text("# header\n\n" + "\n".join(cases) + "\nnot a pair\n")
            values = apply.parse_dotenv(path)
        expected = {line.removeprefix("export ").split("=", 1)[0]: v for line, v in cases.items()}
        self.assertEqual(values, expected)


class Files(unittest.TestCase):
    def test_dashboards_and_alerts_validate(self):
        _, errors = apply.load_dashboards()
        groups, alert_errors = apply.load_alerts({})
        self.assertEqual(errors + alert_errors, [])
        self.assertTrue(groups and groups[0]["rules"])

    def test_param_override(self):
        groups, errors = apply.load_alerts({"daily_llm_spend_usd": "12.5"})
        self.assertEqual(errors, [])
        spend = next(r for r in groups[0]["rules"] if r["uid"] == "kiki-daily-llm-spend")
        threshold = spend["data"][-1]["model"]["conditions"][0]["evaluator"]["params"][0]
        self.assertEqual(threshold, 12.5)


if __name__ == "__main__":
    unittest.main()
