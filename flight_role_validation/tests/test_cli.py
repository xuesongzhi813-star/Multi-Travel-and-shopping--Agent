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

    def test_live_deadline_covers_database_setup(self):
        import asyncio
        from contextlib import asynccontextmanager
        import json

        @asynccontextmanager
        async def slow_repository(*args, **kwargs):
            await asyncio.sleep(0.2)
            self.fail("setup continued after the live deadline")
            yield

        output = io.StringIO()
        with (patch.dict("os.environ", {"DEEPSEEK_API_KEY": "synthetic-key"}),
              patch("flight_role_validation.__main__.LIVE_TIMEOUT", 0.03, create=True),
              patch("flight_role_validation.__main__.temporary_repository", slow_repository),
              patch("flight_role_validation.__main__.open_live_model") as live,
              redirect_stdout(output)):
            self.assertEqual(main(["--live"]), 1)
        live.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual((report["error"], report["provider_requests"]), ("timeout", 0))
