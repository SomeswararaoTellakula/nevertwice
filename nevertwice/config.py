"""Runtime configuration, read from environment variables (and `.env`)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # python-dotenv is optional: plain environment variables work too.
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    hindsight_base_url: str
    hindsight_api_key: str
    bank_id: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_fallback_model: str
    llm_reasoning_effort: str
    host: str
    port: int
    data_dir: Path
    runtime_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        model = _env("LLM_MODEL", "openai/gpt-oss-120b")
        default_effort = "low" if "gpt-oss" in model else ""
        return cls(
            hindsight_base_url=_env("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io").rstrip("/"),
            hindsight_api_key=_env("HINDSIGHT_API_KEY"),
            bank_id=_env("HINDSIGHT_BANK_ID", "basketbolt-oncall"),
            llm_base_url=_env("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
            llm_api_key=_env("LLM_API_KEY") or _env("GROQ_API_KEY"),
            llm_model=model,
            llm_fallback_model=_env("LLM_FALLBACK_MODEL", "openai/gpt-oss-20b"),
            llm_reasoning_effort=_env("LLM_REASONING_EFFORT", default_effort).lower(),
            host=_env("HOST", "127.0.0.1"),
            port=_env_int("PORT", 8000),
            data_dir=Path(_env("NEVERTWICE_DATA_DIR", str(ROOT / "data"))),
            runtime_dir=Path(_env("NEVERTWICE_RUNTIME_DIR", str(ROOT / "runtime"))),
        )

    def problems(self) -> list[str]:
        """Human-readable configuration problems (empty list means ready)."""
        issues: list[str] = []
        if not self.llm_api_key:
            issues.append("LLM_API_KEY is not set (get a free key at https://console.groq.com/keys).")
        is_cloud = "hindsight.vectorize.io" in self.hindsight_base_url
        if is_cloud and not self.hindsight_api_key:
            issues.append("HINDSIGHT_API_KEY is not set (create one at https://ui.hindsight.vectorize.io).")
        return issues
