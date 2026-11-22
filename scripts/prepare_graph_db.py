"""Initialize the PostgreSQL schema used by the parent graph."""

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from customer_support_chat.app.business.database import read_settings
from customer_support_chat.app.workflow.checkpoints import prepare_checkpoints, read_checkpoint_schema


def main():
    try:
        settings = read_settings()
        schema = read_checkpoint_schema(settings.schema)
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(prepare_checkpoints(settings, schema))
    except Exception as error:
        print(f"Graph checkpoints: FAIL ({type(error).__name__})")
        return 1
    print("Graph checkpoints: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
