"""Recognize explicit user authorization to store hotel preferences."""

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Literal
from unicodedata import normalize

from customer_support_chat.app.business.preferences import PreferencePatch


@dataclass(frozen=True)
class PreferenceCommand:
    kind: Literal["ordinary", "invalid", "save"]
    changes: PreferencePatch | None = None


BUDGET_MENTION = re.compile(r"预算|总价")
TOTAL_BUDGET = re.compile(
    r"(?:总预算\s*(?:不超过|最多|上限(?:为)?|为|是)?|"
    r"(?:整段(?:住宿)?|住宿)?总价\s*(?:不超过|最多|上限(?:为)?))"
    r"\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)\s*(?:瑞郎|瑞士法郎|CHF)",
    re.IGNORECASE | re.ASCII,
)
FIELD_PREFIX = r"(?:我\s*)?(?:以后\s*)?(?:优先|偏好|想住|住|选择)?\s*"
ROOM = re.compile(FIELD_PREFIX + r"(?:标准房|standard)", re.IGNORECASE)
AIRPORT = re.compile(FIELD_PREFIX + r"(?:机场附近|机场酒店|机场区域)")
CITY = re.compile(FIELD_PREFIX + r"(?:市区|市中心)")


def parse_total_chf_minor(clause: str) -> int | None:
    match = TOTAL_BUDGET.fullmatch(normalize("NFKC", clause).strip())
    if match is None:
        return None
    amount = int(Decimal(match[1]) * 100)
    return amount if 1 <= amount <= 1000000 else None


def parse_preference_command(content: str) -> PreferenceCommand:
    text = normalize("NFKC", content).strip()
    if "请记住" not in text:
        return PreferenceCommand("ordinary")
    if not text.startswith("请记住"):
        return PreferenceCommand("invalid")

    content = text.removeprefix("请记住").lstrip(" :,").rstrip("。.!！").strip()
    changes: PreferencePatch = {}
    for part in re.split(r"[,、]", content):
        clause = part.strip()
        budget = parse_total_chf_minor(clause)
        if ROOM.fullmatch(clause) and "room_type" not in changes:
            changes["room_type"] = "standard"
        elif AIRPORT.fullmatch(clause) and "area" not in changes:
            changes["area"] = "airport"
        elif CITY.fullmatch(clause) and "area" not in changes:
            changes["area"] = "city"
        elif budget is not None and "max_total_chf_minor" not in changes:
            changes["max_total_chf_minor"] = budget
        else:
            return PreferenceCommand("invalid")
    return PreferenceCommand("save", changes)


def saved_preferences_reply(changes: PreferencePatch) -> str:
    details = []
    if "room_type" in changes:
        details.append("标准房")
    if "area" in changes:
        details.append("机场附近" if changes["area"] == "airport" else "市区")
    budget = changes.get("max_total_chf_minor")
    if budget is not None:
        amount = format(Decimal(budget).scaleb(-2), "f").rstrip("0").rstrip(".")
        details.append(f"整段总预算 {amount} 瑞郎")
    return "已保存住宿偏好：" + "、".join(details) + "。"
