"""Import, build and publish travel FAQ releases against local project storage."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from uuid import UUID

import httpx
import pymysql


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from customer_support_chat.app.business.database import local_value
from faq_extension.catalog import load_faq_version
from faq_extension.mysql_catalog import CatalogConflict, CatalogNotFound, MySqlFaqCatalog, MySqlFaqSettings
from faq_extension.publication import FaqNotPublished, FaqPublisher, PublicationError


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"status": "invalid_request_or_conflict"}))
        self.exit(2)


def parser() -> argparse.ArgumentParser:
    cli = JsonArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    commands.add_parser("import").add_argument("path", type=Path)
    commands.add_parser("build").add_argument("version_id")
    commands.add_parser("evidence").add_argument("document_id")
    for name in ("activate", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("build_id", type=UUID)
        command.add_argument("--expected-revision", type=int, required=True)
    return cli


def execute(args, catalog: MySqlFaqCatalog, publisher: FaqPublisher) -> dict:
    command = args.command
    if command == "init":
        catalog.initialize()
        return {"status": "initialized"}
    if command == "import":
        version = load_faq_version(args.path)
        created = catalog.import_version(version)
        return {"status": "imported" if created else "already_imported", "version_id": version.version_id,
                "digest": version.digest, "document_count": len(version.documents)}
    if command == "build":
        return asdict(publisher.build(args.version_id))
    if command == "status":
        return asdict(catalog.active())
    if command == "evidence":
        try:
            evidence = publisher.evidence(args.document_id)
        except FaqNotPublished:
            return {"status": "not_published"}
        return {"status": "not_found"} if evidence is None else {"status": "ok", **asdict(evidence)}
    pointer = publisher.activate(
        args.build_id, expected_revision=args.expected_revision, rollback=command == "rollback",
    )
    return asdict(pointer)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = MySqlFaqSettings.read()
        port = int(local_value("QDRANT_PORT", "6333"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid port")
        catalog = MySqlFaqCatalog(settings)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=8, trust_env=False) as client:
            result = execute(args, catalog, FaqPublisher(catalog, client))
    except (CatalogConflict, CatalogNotFound, ValueError, OSError):
        print(json.dumps({"status": "invalid_request_or_conflict"}))
        return 2
    except (pymysql.MySQLError, PublicationError):
        print(json.dumps({"status": "storage_unavailable"}))
        return 1
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
