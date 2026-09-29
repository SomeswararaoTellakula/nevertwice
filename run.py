"""Start NeverTwice:  python run.py   (then open http://127.0.0.1:8000)"""

from __future__ import annotations

import logging
import sys

import uvicorn

from nevertwice.config import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    problems = settings.problems()
    if problems:
        print("\nConfiguration needs attention:")
        for p in problems:
            print(f"  - {p}")
        print("Edit .env (copy .env.example) and restart. The UI will also show these.\n")
    print(f"NeverTwice -> http://{settings.host}:{settings.port}  (memory bank: {settings.bank_id})")
    uvicorn.run("nevertwice.server:create_app", factory=True, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
