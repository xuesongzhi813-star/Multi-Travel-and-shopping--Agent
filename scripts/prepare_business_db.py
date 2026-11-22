"""Prepare the PostgreSQL business schema explicitly."""

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from customer_support_chat.app.business.database import create_engine, migrate_business, read_settings


async def prepare():
    settings = read_settings()
    engine = create_engine(settings)
    try:
        await migrate_business(engine, settings.schema)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("migrate",))
    parser.parse_args()
    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(prepare())
    except Exception as error:
        print(f"Business migration: FAIL ({type(error).__name__})")
        return 1
    print("Business migration: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
