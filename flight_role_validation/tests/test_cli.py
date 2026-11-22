from contextlib import redirect_stdout
import io
from unittest import TestCase
from unittest.mock import patch

from flight_role_validation.__main__ import main


class CliTests(TestCase):
    def test_default_never_loads_live_client(self):
        output = io.StringIO()
        with patch("flight_role_validation.__main__.open_live_model") as live, redirect_stdout(output):
            self.assertEqual(main([]), 0)
        live.assert_not_called()
        self.assertIn('"provider_requests": 0', output.getvalue())
        self.assertIn('"arrival_date_local": "2026-12-15"', output.getvalue())
