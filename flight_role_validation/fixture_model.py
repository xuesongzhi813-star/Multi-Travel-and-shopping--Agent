"""Scripted model for deterministic P4 validation without a network client."""

import asyncio

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedFlightModel(BaseChatModel):
    replies: list[AIMessage]
    delay: float = 0
    received: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-flight"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        index = len(self.received)
        self.received.append(list(messages))
        if index >= len(self.replies):
            raise AssertionError("Unexpected model request")
        return ChatResult(generations=[ChatGeneration(message=self.replies[index])])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._generate(messages, stop, run_manager, **kwargs)


def tool_reply(name: str, call_id: str, **arguments) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "id": call_id, "args": arguments}])


def synthetic_replies() -> list[AIMessage]:
    return [
        tool_reply("lookup_flight", "lookup-a1", reference="DEMO-A1"),
        tool_reply("lookup_flight", "lookup-a2", reference="DEMO-A2"),
        tool_reply("HandoffCandidate", "structured-final", reference="DEMO-A1"),
    ]
