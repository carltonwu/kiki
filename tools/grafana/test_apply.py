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

HERE = Path(__file__).resolve().parent
_loader = importlib.machinery.SourceFileLoader("grafana_apply", str(HERE / "apply"))
_spec = importlib.util.spec_from_loader("grafana_apply", _loader)
apply = importlib.util.module_from_spec(_spec)
_loader.exec_module(apply)

TOKEN = "glsa_secretSECRET_1234"


class EchoAuth(BaseHTTPRequestHandler):
    """Answers every request with an error whose body echoes the Authorization header."""

    def do_GET(self):
        if self.path == "/html":
            body = b"<html>login</html>"
            self.send_response(200)
        else:
            body = json.dumps({"message": f"bad auth: {self.headers['Authorization']}"}).encode()
            self.send_response(int(self.path.strip("/") or 500))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TokenHandling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), EchoAuth)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

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
