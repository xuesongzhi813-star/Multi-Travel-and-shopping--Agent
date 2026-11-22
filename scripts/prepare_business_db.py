"""Prepare PostgreSQL business data explicitly."""

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from customer_support_chat.app.business.database import create_engine, local_value, migrate_business, read_settings
from customer_support_chat.app.business.demo_seed import seed_demo


async def prepare(action):
    settings = read_settings()
    engine = create_engine(settings)
    try:
        if action == "migrate":
            await migrate_business(engine, settings.schema)
        else:
            passwords = (local_value("P2_DEMO_A_PASSWORD"), local_value("P2_DEMO_B_PASSWORD"))
            if not all(passwords):
                raise ValueError("configure both P2 demo passwords in the local environment")
            await seed_demo(engine, settings.schema, passwords)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("migrate", "seed"))
    args = parser.parse_args()
    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(prepare(args.action))
    except ValueError as error:
        print(f"Business {args.action}: FAIL ({error})")
        return 1
    except Exception as error:
        print(f"Business {args.action}: FAIL ({type(error).__name__})")
        return 1
    print(f"Business {args.action}: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
