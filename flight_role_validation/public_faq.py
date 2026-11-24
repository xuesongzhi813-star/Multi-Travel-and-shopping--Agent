"""One public policy lookup within an authenticated role run."""

from collections.abc import Awaitable, Callable
import json

from customer_support_chat.app.tooling.executor import ToolDefinition, ToolExecutor, ToolRegistry, valid_tool_call_id
from faq_extension.contracts import FaqSearchResult, PublicFaqQuestion


FAQ_TOOL = "search_public_faq"
FAQ_FRAME_LIMIT = 128_000
FAQ_ROLES = frozenset({"flight", "hotel"})
FaqLookup = Callable[[PublicFaqQuestion], Awaitable[FaqSearchResult]]


def register_public_faq(registry: ToolRegistry, lookup: FaqLookup) -> None:
    async def search(context, question, tool_call_id):
        return (await lookup(question)).model_dump()

    registry.register(ToolDefinition(
        name=FAQ_TOOL, description="Find evidence about a host-approved public travel policy",
        arguments_model=PublicFaqQuestion, result_model=FaqSearchResult, source="builtin",
        allowed_roles=FAQ_ROLES, handler=search,
        audit_result_fields=frozenset({"status", "degradation"}),
    ))


class FaqRoleCall:
    def __init__(self, registry, question, lookup):
        self.question = question
        self.enabled = question is not None and lookup is not None
        self.result: FaqSearchResult | None = None
        if self.enabled:
            register_public_faq(registry, lookup)

    def accepts(self, call: dict) -> bool:
        return (
            self.enabled and self.result is None and call.get("name") == FAQ_TOOL
            and call.get("args") == {} and valid_tool_call_id(call.get("id"))
        )

    async def invoke(self, executor: ToolExecutor, context, call_id: str) -> str:
        if not self.enabled or self.result is not None:
            raise ValueError("FAQ tool call out of sequence")
        result = await executor.execute(context, FAQ_TOOL, self.question.model_dump(), call_id)
        self.result = (
            FaqSearchResult.model_validate_json(json.dumps(result.data))
            if result.status == "ok" and result.data is not None
            else FaqSearchResult(status="unavailable")
        )
        return self.result.model_dump_json()
