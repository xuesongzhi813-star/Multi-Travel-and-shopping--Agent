"""Shared public-policy question and evidence contracts."""

from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .catalog import validate_id, validate_source


FaqCategory = Literal["baggage", "disruptions", "changes_refunds", "lodging"]
FaqStatus = Literal["ok", "no_evidence", "not_published", "unavailable", "rephrase"]
FaqDegradation = Literal["reranker_timeout", "reranker_unavailable", "reranker_invalid_response"]


class PublicFaqQuestion(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    text: str = Field(min_length=1, max_length=240)
    category: FaqCategory | None = None
    language: Literal["zh-CN"] = "zh-CN"

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        text = value.strip()
        if not text or not text.isprintable():
            raise ValueError("Expected a single public policy question")
        return text


class FaqCitation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    document_id: str
    chunk_index: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=4000)
    source_url: str = Field(max_length=2048)
    applicability: str = Field(min_length=1, max_length=4000)
    checked_on: str = Field(min_length=10, max_length=10)

    @field_validator("document_id")
    @classmethod
    def check_document_id(cls, value: str) -> str:
        return validate_id(value)

    @field_validator("source_url")
    @classmethod
    def check_source(cls, value: str) -> str:
        return validate_source(value)

    @field_validator("checked_on")
    @classmethod
    def check_date(cls, value: str) -> str:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Expected a canonical verification date")
        return value


class FaqSearchResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    status: FaqStatus
    citations: tuple[FaqCitation, ...] = Field(default=(), max_length=6)
    degradation: FaqDegradation | None = None

    @model_validator(mode="after")
    def check_evidence_matches_status(self) -> Self:
        if (self.status == "ok") != bool(self.citations):
            raise ValueError("FAQ citations must match the result status")
        if self.degradation and self.status not in {"ok", "no_evidence"}:
            raise ValueError("Only completed searches may be degraded")
        return self
