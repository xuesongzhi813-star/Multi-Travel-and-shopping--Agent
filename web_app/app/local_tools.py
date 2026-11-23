"""Call the role worker without loading incompatible SDKs into the API."""

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

from customer_support_chat.app.business.database import DatabaseSettings, local_value


PROJECT = Path(__file__).resolve().parents[2]
ROLE_PYTHON = PROJECT / "flight_role_validation" / ".venv" / "Scripts" / "python.exe"


class LocalToolError(RuntimeError):
    """The worker failed before producing a validated tool response."""


class LocalToolTimeout(LocalToolError):
    """The worker exceeded its time budget."""


@dataclass
class FlightSettings:
    mode: str
    api_key: str = field(repr=False)
    remaining_runs: int

    @classmethod
    def read(cls):
        mode = local_value("TRAVEL_FLIGHT_MODE", "direct")
        if mode not in {"direct", "offline", "deepseek"}:
            raise ValueError("TRAVEL_FLIGHT_MODE must be direct, offline or deepseek")
        if mode != "deepseek":
            return cls(mode, "", 0)
        api_key = local_value("DEEPSEEK_API_KEY").strip()
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required in deepseek mode")
        try:
            limit = int(local_value("TRAVEL_DEEPSEEK_RUN_LIMIT", "3"))
        except ValueError:
            limit = 0
        if not 1 <= limit <= 20:
            raise ValueError("TRAVEL_DEEPSEEK_RUN_LIMIT must be between 1 and 20")
        return cls(mode, api_key, limit)


async def run_local_tool(
    operation: str, user_id: UUID, value: str | dict, database: DatabaseSettings, request_id: UUID,
    *, flight_mode: str = "direct", api_key: str = "", thread_id: UUID | None = None,
    deadline: float | None = None,
) -> dict:
    if not ROLE_PYTHON.is_file():
        raise LocalToolError("Local role environment is missing")
    env = os.environ.copy()
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        env.pop(key, None)
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key
    env.update(
        PYTHONUTF8="1",
        LANGCHAIN_TRACING_V2="false",
        LANGSMITH_TRACING="false",
        P2_DB_SCHEMA=database.schema,
        POSTGRES_DB=database.name,
        POSTGRES_USER=database.user,
        POSTGRES_PASSWORD=database.password,
        POSTGRES_PORT=str(database.port),
    )
    timeout = (60 if api_key else 30) if deadline is None else deadline - asyncio.get_running_loop().time()
    if timeout <= 0:
        raise LocalToolTimeout("Local tool timed out before launch")
    payload = json.dumps({
        "operation": operation, "user_id": str(user_id), "value": value,
        "request_id": str(request_id), "flight_mode": flight_mode,
        "thread_id": str(thread_id) if thread_id else None, "budget_seconds": max(0.1, timeout - 1),
    })

    return await run_role_process(env, payload, timeout=timeout)


async def run_role_process(env: dict[str, str], payload: str, *, timeout: float) -> dict:
    try:
        process = subprocess.Popen(
            [str(ROLE_PYTHON), "-B", "-m", "flight_role_validation.web_worker"], cwd=PROJECT,
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        raise LocalToolError("Local tool unavailable") from error

    communication = asyncio.create_task(asyncio.to_thread(process.communicate, payload))
    try:
        async with asyncio.timeout(timeout):
            stdout, _ = await asyncio.shield(communication)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.poll() is None:
            process.kill()
        await asyncio.shield(communication)
        if isinstance(error, asyncio.CancelledError):
            raise
        raise LocalToolTimeout("Local tool timed out") from error
    except (OSError, ValueError) as error:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait)
        raise LocalToolError("Local tool unavailable") from error

    if process.returncode:
        raise LocalToolError("Local tool unavailable")
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError("Unexpected role worker output")
    except ValueError as error:
        raise LocalToolError("Invalid local tool output") from error
    return result
