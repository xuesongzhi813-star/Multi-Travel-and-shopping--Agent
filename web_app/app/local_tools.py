"""Call the role worker without loading incompatible SDKs into the API."""

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

from customer_support_chat.app.business.database import DatabaseSettings, local_value
from faq_extension.contracts import FaqSearchResult, PublicFaqQuestion
from flight_role_validation.public_faq import FAQ_FRAME_LIMIT, FAQ_ROLES, FaqLookup


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
    faq_question: PublicFaqQuestion | None = None, faq_lookup: FaqLookup | None = None,
) -> dict:
    if not ROLE_PYTHON.is_file():
        raise LocalToolError("Local role environment is missing")
    env = os.environ.copy()
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY", "SILICONFLOW_API_KEY"):
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
    startup = {
        "operation": operation, "user_id": str(user_id), "value": value,
        "request_id": str(request_id), "flight_mode": flight_mode,
        "thread_id": str(thread_id) if thread_id else None, "budget_seconds": max(0.1, timeout - 1),
    }
    if faq_question is not None:
        if operation not in FAQ_ROLES or flight_mode != "offline" or faq_lookup is None:
            raise LocalToolError("FAQ is unavailable for this role")
        startup["faq_question"] = faq_question.model_dump(mode="json")
        return await run_faq_role_process(
            env, json.dumps(startup), timeout=timeout, question=faq_question, lookup=faq_lookup,
        )
    return await run_role_process(env, json.dumps(startup), timeout=timeout)


def _start_role_process(env: dict[str, str]):
    try:
        return subprocess.Popen(
            [str(ROLE_PYTHON), "-B", "-m", "flight_role_validation.web_worker"], cwd=PROJECT,
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        raise LocalToolError("Local tool unavailable") from error


async def run_role_process(env: dict[str, str], payload: str, *, timeout: float) -> dict:
    process = _start_role_process(env)
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


async def _send_frame(pipe, frame: str) -> None:
    def write() -> None:
        pipe.write(frame + "\n")
        pipe.flush()

    if len(frame) >= FAQ_FRAME_LIMIT:
        raise LocalToolError("Local role frame is too large")
    await asyncio.to_thread(write)


async def _read_frame(pipe) -> dict:
    line = await asyncio.to_thread(pipe.readline, FAQ_FRAME_LIMIT + 1)
    if not line.endswith("\n") or len(line) > FAQ_FRAME_LIMIT:
        raise LocalToolError("Incomplete local role frame")
    decoded = json.loads(line)
    if not isinstance(decoded, dict):
        raise LocalToolError("Invalid local role frame")
    return decoded


def _stop_bridge(process) -> None:
    if process.poll() is None:
        process.kill()
        process.wait()
    for pipe in (process.stdin, process.stdout):
        try:
            pipe.close()
        except OSError:
            pass


async def run_faq_role_process(
    env: dict[str, str], payload: str, *, timeout: float, question: PublicFaqQuestion, lookup: FaqLookup,
) -> dict:
    operation = json.loads(payload)["operation"]
    question_json = question.model_dump(mode="json")
    process = _start_role_process(env)
    try:
        async with asyncio.timeout(timeout):
            await _send_frame(process.stdin, payload)
            frame = await _read_frame(process.stdout)
            evidence = None
            if frame.get("type") == "faq_request":
                if frame != {"type": "faq_request", "role": operation, "question": question_json}:
                    raise LocalToolError("Local role changed the approved FAQ question")
                try:
                    async with asyncio.timeout(min(5, max(0.1, timeout - 2))):
                        evidence = await lookup(question)
                except Exception:
                    evidence = FaqSearchResult(status="unavailable")
                await _send_frame(process.stdin, json.dumps({
                    "type": "faq_evidence", "result": evidence.model_dump(mode="json"),
                }, ensure_ascii=False))
                frame = await _read_frame(process.stdout)
            if set(frame) != {"type", "result"} or frame["type"] != "result" or not isinstance(frame["result"], dict):
                raise LocalToolError("Invalid local role result")
            if await asyncio.to_thread(process.stdout.readline, 1) or await asyncio.to_thread(process.wait):
                raise LocalToolError("Local role did not finish cleanly")
            # Only evidence accepted by the host can be projected into the chat.
            result = dict(frame["result"])
            result["faq"] = evidence.model_dump(mode="json") if evidence is not None else None
            return result
    except TimeoutError:
        raise LocalToolTimeout("Local tool timed out") from None
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise LocalToolError("Invalid local FAQ exchange") from None
    finally:
        await asyncio.to_thread(_stop_bridge, process)
