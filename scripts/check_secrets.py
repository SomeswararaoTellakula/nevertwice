"""Run before pushing to GitHub:  python scripts/check_secrets.py

Scans every project file except .env (which git already ignores) for real API keys and fails if it finds one,
e.g. keys pasted into .env.example. Use --fix to blank the keys in .env.example automatically.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_RE = re.compile(r"\b(hsk_[A-Za-z0-9_]{16,}|gsk_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "runtime"}


def main() -> int:
    fix = "--fix" in sys.argv
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name == ".env" or any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if KEY_RE.search(line):
                found.append((path.relative_to(ROOT), n))
    example = ROOT / ".env.example"
    if fix and example.exists():
        text = example.read_text(encoding="utf-8")
        cleaned = re.sub(r"^(HINDSIGHT_API_KEY|LLM_API_KEY|GROQ_API_KEY)=.*$", r"\1=", text, flags=re.M)
        if cleaned != text:
            example.write_text(cleaned, encoding="utf-8")
            print("Blanked the keys in .env.example (your .env is untouched).")
            found = [f for f in found if str(f[0]) != ".env.example"]
    if found:
        print("Real API keys found — do NOT push until they are removed:")
        for path, n in found:
            print(f"  {path}:{n}")
        print("Blank .env.example automatically with:  python scripts/check_secrets.py --fix")
        return 1
    print("No API keys outside .env — safe to push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
