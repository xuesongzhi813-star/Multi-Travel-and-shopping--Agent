import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


def compose_config(password=None):
    with TemporaryDirectory() as directory:
        env_file = Path(directory) / "fixture.env"
        env_file.write_text(
            "" if password is None else f"POSTGRES_PASSWORD={password}\n", encoding="utf-8",
        )
        clean_env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("POSTGRES_", "QDRANT_", "REDIS_", "COMPOSE_"))
        }
        return subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "-f", "compose.local.yml",
             "config", "--format", "json"],
            cwd=ROOT, env=clean_env, capture_output=True, text=True, timeout=15, check=False,
        )


class LocalComposeTests(unittest.TestCase):
    def resolved(self):
        result = compose_config("test-placeholder-not-a-secret")
        self.assertEqual(result.returncode, 0, "P1 Compose configuration was not resolved")
        return json.loads(result.stdout)

    def test_local_ports_and_mounts(self):
        config = self.resolved()
        services = config["services"]
        self.assertEqual(set(services), {"postgres", "qdrant", "redis"})
        for service, port in {"postgres": 5432, "qdrant": 6333, "redis": 6379}.items():
            with self.subTest(service=service):
                binding, = services[service]["ports"]
                self.assertEqual(binding["host_ip"], "127.0.0.1")
                self.assertEqual(binding["target"], port)
                self.assertEqual(int(binding["published"]), port)
        for service, source, target in (
            ("postgres", "postgres-data", "/var/lib/postgresql"),
            ("qdrant", "qdrant-data", "/qdrant/storage"),
        ):
            with self.subTest(service=service):
                volume, = services[service]["volumes"]
                self.assertEqual((volume["type"], volume["source"], volume["target"]), ("volume", source, target))

    def test_images_are_pinned(self):
        for name, service in self.resolved()["services"].items():
            with self.subTest(service=name):
                self.assertRegex(service["image"], r"@sha256:[a-f0-9]{64}$")

    def test_password_is_required(self):
        result = compose_config()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("POSTGRES_PASSWORD", result.stderr, "P1 Compose must reject an unset password")
