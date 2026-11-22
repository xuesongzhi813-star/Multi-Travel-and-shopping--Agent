from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_local_runtime import ProbeFailure, main, report_action, require_probe_value, verify_mysql_grants


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
