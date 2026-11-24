"""Travel FAQ package with lazy imports for the legacy updater API."""

from importlib import import_module


_EXPORTS = {
    "FAQUpdateService": ".update_service",
    "parse_document": ".document_parser",
    "DataSourceManager": ".data_source",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _EXPORTS.keys())
