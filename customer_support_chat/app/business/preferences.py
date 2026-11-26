"""Shared values for user-managed hotel preferences."""

from typing import Literal, TypedDict


class PreferencePatch(TypedDict, total=False):
    room_type: Literal["standard"] | None
    area: Literal["airport", "city"] | None
    max_total_chf_minor: int | None


class PreferenceValues(TypedDict):
    room_type: Literal["standard"] | None
    area: Literal["airport", "city"] | None
    max_total_chf_minor: int | None
