"""Load immutable, attributed travel FAQ releases."""

from dataclasses import asdict, dataclass, fields
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


CATEGORIES = frozenset({"baggage", "disruptions", "changes_refunds", "lodging"})
SOURCE_HOSTS = frozenset({"www.swiss.com", "www.zuerich.com"})
IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


@dataclass(frozen=True)
class FaqDocument:
    id: str
    category: str
    language: str
    question: str
    answer: str
    applicability: str
    source_url: str
    checked_on: date

    def to_json(self) -> dict:
        return asdict(self) | {"checked_on": self.checked_on.isoformat()}


@dataclass(frozen=True)
class FaqVersion:
    version_id: str
    digest: str
    documents: tuple[FaqDocument, ...]


def validate_id(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid catalog identifier")
    return value


def validate_source(url: str) -> str:
    if any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError("Invalid public source URL")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in SOURCE_HOSTS or not parsed.path.startswith("/"):
        raise ValueError("Expected an official HTTPS source")
    if parsed.query or parsed.fragment:
        raise ValueError("Use the canonical public source URL")
    return url


def unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def digest_json(payload: object) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def parse_faq_version(payload: object) -> FaqVersion:
    if not isinstance(payload, dict) or set(payload) != {"version_id", "documents"}:
        raise ValueError("Expected a catalog version and documents")
    version_id = validate_id(payload["version_id"])
    rows = payload["documents"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected FAQ documents")
    expected = {field.name for field in fields(FaqDocument)}
    documents = {}
    for item in rows:
        if (
            not isinstance(item, dict) or set(item) != expected
            or any(not isinstance(value, str) or not value.strip() for value in item.values())
        ):
            raise ValueError("Invalid FAQ fields")
        document_id = validate_id(item["id"])
        if document_id in documents or item["category"] not in CATEGORIES or item["language"] != "zh-CN":
            raise ValueError("Invalid FAQ ID, category or language")
        validate_source(item["source_url"])
        checked_on = date.fromisoformat(item["checked_on"])
        if checked_on.isoformat() != item["checked_on"]:
            raise ValueError("Expected ISO calendar date")
        documents[document_id] = FaqDocument(**(item | {"checked_on": checked_on}))
    ordered = tuple(documents[key] for key in sorted(documents))
    return FaqVersion(version_id, digest_json([item.to_json() for item in ordered]), ordered)


def load_faq_version(path: Path) -> FaqVersion:
    return parse_faq_version(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys))
