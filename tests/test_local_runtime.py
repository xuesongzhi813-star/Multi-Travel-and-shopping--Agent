from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID

import httpx


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_local_runtime import ProbeFailure, main, qdrant_probe, report_action, require_probe_value, verify_mysql_grants


class LocalRuntimeTests(unittest.TestCase):
    def test_missing_config(self):
        output = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("check_local_runtime.check_qdrant"),
            patch("check_local_runtime.check_redis"),
            redirect_stdout(output),
        ):
            result = main(["check"], env_path=Path("missing-fixture.env"))
        self.assertEqual(result, 1)
        self.assertIn("MYSQL_USER", output.getvalue())
        self.assertIn("MYSQL_PASSWORD", output.getvalue())

    def test_error_does_not_print_secret(self):
        def fail():
            raise ConnectionError("value-to-keep-out-of-logs")

        output = io.StringIO()
        with redirect_stdout(output):
            result = report_action("MySQL", fail)
        self.assertFalse(result)
        self.assertIn("MySQL: FAIL", output.getvalue())
        self.assertNotIn("value-to-keep-out-of-logs", output.getvalue())

    def test_probe_id_is_required_and_validated(self):
        for invalid in (["write"], ["cleanup", "--probe-id", "do-not-print-this-input"]):
            with self.subTest(invalid=invalid[:1]):
                stderr = io.StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                    main(invalid)
                self.assertEqual(error.exception.code, 2)
                self.assertIn("--probe-id", stderr.getvalue())
                self.assertNotIn("do-not-print-this-input", stderr.getvalue())

    def test_verify_detects_wrong_probe(self):
        require_probe_value("expected-id", "expected-id")
        for actual in (None, "other-id"):
            with self.subTest(actual=actual), self.assertRaises(ProbeFailure):
                require_probe_value(actual, "expected-id")

    def test_mysql_grants_require_literal_project_scope(self):
        principal = " TO 'fixture'@'localhost'"
        escaped = chr(96) + r"travel\_faq" + chr(96)
        verify_mysql_grants(["GRANT ALL PRIVILEGES ON " + escaped + ".*" + principal], "travel_faq")
        for scope in ("*.*", "other.*", chr(96) + "travel_faq" + chr(96) + ".*"):
            with self.subTest(scope=scope), self.assertRaises(ProbeFailure):
                verify_mysql_grants(["GRANT ALL PRIVILEGES ON " + scope + principal], "travel_faq")

    def test_mysql_literal_mode_rejects_another_schema_spelling(self):
        quote = chr(96)
        suffix = ".* TO 'fixture'@'localhost'"
        literal = "GRANT ALL PRIVILEGES ON " + quote + "travel_faq" + quote + suffix
        escaped = "GRANT ALL PRIVILEGES ON " + quote + r"travel\_faq" + quote + suffix
        verify_mysql_grants([literal], "travel_faq", literal_wildcards=True)
        with self.assertRaises(ProbeFailure):
            verify_mysql_grants([escaped], "travel_faq", literal_wildcards=True)

    def test_qdrant_verifies_after_recovery(self):
        marker = UUID("16f98a7d-0d78-4676-add7-841810b5c776")
        calls = []

        def response(request):
            calls.append(request.url.path)
            if request.url.path == "/readyz":
                return httpx.Response(503 if len(calls) == 1 else 200)
            return httpx.Response(200, json={"result": {
                "id": str(marker), "payload": {"marker": str(marker)},
            }})

        transport = httpx.MockTransport(response)
        client = httpx.Client(base_url="http://127.0.0.1", transport=transport)
        with patch("check_local_runtime.qdrant_client", return_value=client):
            qdrant_probe({}, "verify", marker)
        self.assertEqual(calls, [
            "/readyz", "/readyz", f"/collections/p1_runtime_probe_{marker.hex}/points/{marker}",
        ])
