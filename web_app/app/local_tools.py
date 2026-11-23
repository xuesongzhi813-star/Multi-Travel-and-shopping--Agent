"""Call the local role environment without loading incompatible SDKs into the API."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID


from customer_support_chat.app.business.database import DatabaseSettings


PROJECT = Path(__file__).resolve().parents[2]


async def run_local_tool(operation: str, user_id: UUID, value: str, database: DatabaseSettings) -> dict:
    interpreter = PROJECT / "flight_role_validation" / ".venv" / "Scripts" / "python.exe"
    if not interpreter.is_file():
        raise RuntimeError("Local role environment is missing")
    env = os.environ.copy()
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        env.pop(key, None)
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
    payload = json.dumps({"operation": operation, "user_id": str(user_id), "value": value})

    def invoke() -> dict:
        result = subprocess.run(
            [str(interpreter), "-B", "-m", "flight_role_validation.web_worker"], cwd=PROJECT,
            env=env, input=payload, capture_output=True, encoding="utf-8", timeout=30, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return json.loads(result.stdout)

    try:
        return await asyncio.to_thread(invoke)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError("Local tool unavailable") from error
