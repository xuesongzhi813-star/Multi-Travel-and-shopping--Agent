"""Probe loopback storage services without printing credentials or responses."""

import argparse
from collections.abc import Callable
import os
from pathlib import Path
import re
from time import monotonic, sleep
from uuid import UUID, uuid4

from dotenv import dotenv_values
import httpx
import psycopg
from psycopg import sql
import pymysql
from redis import Redis


TIMEOUT = 5
DEFAULT_ENV = Path(__file__).resolve().parents[1] / ".env"
DEFAULTS = {
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "travel",
    "POSTGRES_USER": "travel",
    "POSTGRES_PASSWORD": "",
    "QDRANT_PORT": "6333",
    "REDIS_PORT": "6379",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_DATABASE": "",
    "MYSQL_USER": "",
    "MYSQL_PASSWORD": "",
}


class ProbeFailure(Exception):
    """An expected failure with a diagnostic safe to print."""


def read_settings(env_path: Path) -> dict[str, str]:
    supplied = dotenv_values(env_path, interpolate=False, encoding="utf-8-sig")
    return {
        key: os.environ.get(key, supplied.get(key) or default)
        for key, default in DEFAULTS.items()
    }


def require(settings: dict[str, str], *names: str) -> None:
    missing = [name for name in names if not settings[name]]
    if missing:
        raise ProbeFailure("missing " + ", ".join(missing))


def port(settings: dict[str, str], name: str) -> int:
    try:
        value = int(settings[name])
    except ValueError:
        raise ProbeFailure(f"invalid {name}") from None
    if not 1 <= value <= 65535:
        raise ProbeFailure(f"invalid {name}")
    return value


def require_probe_value(actual: object, expected: str) -> None:
    if actual != expected:
        raise ProbeFailure("probe value missing or different")


def report_action(service: str, action: Callable[[], None]) -> bool:
    try:
        action()
    except ProbeFailure as error:
        print(f"{service}: FAIL ({error})")
    except Exception as error:
        print(f"{service}: FAIL ({type(error).__name__})")
    else:
        print(f"{service}: PASS")
        return True
    return False


def postgres_connection(settings: dict[str, str]):
    require(settings, "POSTGRES_PASSWORD")
    return psycopg.connect(
        host="127.0.0.1", port=port(settings, "POSTGRES_PORT"),
        dbname=settings["POSTGRES_DB"], user=settings["POSTGRES_USER"],
        password=settings["POSTGRES_PASSWORD"], connect_timeout=TIMEOUT,
        options="-c statement_timeout=5000 -c lock_timeout=3000",
    )


def check_postgres(settings: dict[str, str]) -> None:
    with postgres_connection(settings) as connection:
        if connection.execute("SELECT 1").fetchone() != (1,):
            raise ProbeFailure("query did not return expected value")


def postgres_probe(settings: dict[str, str], action: str, probe_id: UUID) -> None:
    table = sql.Identifier(f"p1_runtime_probe_{probe_id.hex}")
    marker = str(probe_id)
    with postgres_connection(settings) as connection:
        if action == "write":
            connection.execute(sql.SQL("CREATE TABLE {} (marker text NOT NULL)").format(table))
            connection.execute(sql.SQL("INSERT INTO {} VALUES (%s)").format(table), (marker,))
        elif action == "verify":
            row = connection.execute(sql.SQL("SELECT marker FROM {}").format(table)).fetchone()
            require_probe_value(row[0] if row else None, marker)
        else:
            connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))


def qdrant_client(settings: dict[str, str]) -> httpx.Client:
    return httpx.Client(
        base_url=f"http://127.0.0.1:{port(settings, 'QDRANT_PORT')}",
        timeout=TIMEOUT, trust_env=False,
    )


def check_qdrant(settings: dict[str, str]) -> None:
    with qdrant_client(settings) as client:
        client.get("/readyz").raise_for_status()


def wait_for_qdrant(client: httpx.Client) -> None:
    deadline = monotonic() + 30
    while (remaining := deadline - monotonic()) > 0:
        try:
            if client.get("/readyz", timeout=min(TIMEOUT, remaining)).is_success:
                return
        except httpx.TransportError:
            pass
        sleep(min(0.5, max(0, deadline - monotonic())))
    raise ProbeFailure("Qdrant readiness timed out after 30 seconds")


def qdrant_probe(settings: dict[str, str], action: str, probe_id: UUID) -> None:
    collection = f"/collections/p1_runtime_probe_{probe_id.hex}"
    marker = str(probe_id)
    with qdrant_client(settings) as client:
        if action == "write":
            client.put(collection, json={"vectors": {"size": 2, "distance": "Cosine"}}).raise_for_status()
            client.put(
                f"{collection}/points", params={"wait": "true"},
                json={"points": [{"id": marker, "vector": [1.0, 0.0], "payload": {"marker": marker}}]},
            ).raise_for_status()
        elif action == "verify":
            wait_for_qdrant(client)
            response = client.get(f"{collection}/points/{marker}").raise_for_status()
            point = response.json()["result"]
            require_probe_value(point["id"], marker)
            require_probe_value(point["payload"]["marker"], marker)
        else:
            response = client.delete(collection)
            if response.status_code != 404:
                response.raise_for_status()


def check_redis(settings: dict[str, str]) -> None:
    key = f"travel:p1:probe:{uuid4().hex}"
    with Redis(
        host="127.0.0.1", port=port(settings, "REDIS_PORT"),
        socket_connect_timeout=TIMEOUT, socket_timeout=TIMEOUT, decode_responses=True,
    ) as client:
        if not client.ping():
            raise ProbeFailure("no PONG")
        if not client.set(key, "probe", nx=True, ex=30):
            raise ProbeFailure("probe key already exists")
        try:
            require_probe_value(client.get(key), "probe")
            if not 0 < client.ttl(key) <= 30:
                raise ProbeFailure("probe TTL missing or outside expected range")
        finally:
            client.delete(key)


def verify_mysql_grants(grants: list[str], database: str, *, literal_wildcards: bool = False) -> None:
    exact = database.replace("\\", "\\\\").replace("_", r"\_").replace("%", r"\%")
    scope = database if literal_wildcards else exact
    quoted_scope = chr(96) + scope.replace(chr(96), chr(96) * 2) + chr(96)
    pattern = re.compile(
        r"^GRANT ([A-Z, ]+) ON " + re.escape(quoted_scope) + r"\.\* TO ",
        re.IGNORECASE,
    )
    permissions = set()
    for grant in grants:
        if match := pattern.match(grant):
            permissions.update(item.strip().upper() for item in match[1].split(","))
    required = {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "INDEX", "CREATE TEMPORARY TABLES"}
    if "ALL PRIVILEGES" not in permissions and not required <= permissions:
        raise ProbeFailure("direct grants on the project schema do not cover the FAQ migration and temporary probe")


def check_mysql(settings: dict[str, str]) -> None:
    require(settings, "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")
    if settings["MYSQL_HOST"].lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise ProbeFailure("MYSQL_HOST must be a loopback address")
    if settings["MYSQL_USER"].lower() == "root":
        raise ProbeFailure("MYSQL_USER must be a dedicated project account")
    with pymysql.connect(
        host=settings["MYSQL_HOST"], port=port(settings, "MYSQL_PORT"),
        database=settings["MYSQL_DATABASE"], user=settings["MYSQL_USER"], password=settings["MYSQL_PASSWORD"],
        connect_timeout=TIMEOUT, read_timeout=TIMEOUT, write_timeout=TIMEOUT,
    ) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT @@GLOBAL.partial_revokes")
        literal_wildcards = bool(cursor.fetchone()[0])
        cursor.execute("SHOW GRANTS FOR CURRENT_USER")
        verify_mysql_grants(
            [row[0] for row in cursor.fetchall()], settings["MYSQL_DATABASE"],
            literal_wildcards=literal_wildcards,
        )
        cursor.execute("CREATE TEMPORARY TABLE p1_runtime_probe (marker varchar(36) NOT NULL)")
        marker = str(uuid4())
        cursor.execute("INSERT INTO p1_runtime_probe (marker) VALUES (%s)", (marker,))
        cursor.execute("SELECT marker FROM p1_runtime_probe")
        row = cursor.fetchone()
        require_probe_value(row[0] if row else None, marker)


def main(argv: list[str] | None = None, *, env_path: Path = DEFAULT_ENV) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "write", "verify", "cleanup"))
    parser.add_argument("--probe-id", metavar="UUID")
    args = parser.parse_args(argv)
    probe_id = None
    if args.action == "check":
        if args.probe_id:
            parser.error("--probe-id is only used with write, verify or cleanup")
    else:
        try:
            probe_id = UUID(args.probe_id or "")
        except ValueError:
            parser.error("--probe-id must be a valid UUID")

    settings = read_settings(env_path)
    if args.action == "check":
        actions = {
            "PostgreSQL": lambda: check_postgres(settings),
            "Qdrant": lambda: check_qdrant(settings),
            "Redis": lambda: check_redis(settings),
            "MySQL": lambda: check_mysql(settings),
        }
    else:
        actions = {
            "PostgreSQL": lambda: postgres_probe(settings, args.action, probe_id),
            "Qdrant": lambda: qdrant_probe(settings, args.action, probe_id),
        }
    results = [report_action(service, action) for service, action in actions.items()]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
