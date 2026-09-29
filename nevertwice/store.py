"""Small local state: live incidents resolved in the app and the triage log.

Hindsight is the agent's memory. This store only keeps UI bookkeeping (which alert
was triaged when, what the agent recommended, what the responder said about it) so
the Learning view can chart how recommendations improve as memory grows.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.dir = runtime_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._incidents_path = self.dir / "live_incidents.json"
        self._triage_path = self.dir / "triage_log.json"

    # ----------------------------------------------------------- io helpers
    def _read(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return default

    def _write(self, path: Path, data: Any) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # ------------------------------------------------------------ incidents
    def live_incidents(self) -> list[dict[str, Any]]:
        data = self._read(self._incidents_path, [])
        return data if isinstance(data, list) else []

    async def add_incident(self, incident: dict[str, Any]) -> None:
        async with self._lock:
            data = [i for i in self.live_incidents() if i.get("id") != incident.get("id")]
            data.append(incident)
            self._write(self._incidents_path, data)

    # --------------------------------------------------------------- triage
    def triage_log(self) -> list[dict[str, Any]]:
        data = self._read(self._triage_path, [])
        return data if isinstance(data, list) else []

    def get_triage(self, triage_id: str) -> dict[str, Any] | None:
        for entry in self.triage_log():
            if entry.get("id") == triage_id:
                return entry
        return None

    async def log_triage(self, entry: dict[str, Any]) -> None:
        async with self._lock:
            data = self.triage_log()
            data.append(entry)
            self._write(self._triage_path, data[-500:])

    async def update_triage(self, triage_id: str, **fields: Any) -> dict[str, Any] | None:
        async with self._lock:
            data = self.triage_log()
            found = None
            for entry in data:
                if entry.get("id") == triage_id:
                    entry.update(fields)
                    found = entry
            if found is not None:
                self._write(self._triage_path, data)
            return found

    async def reset(self) -> None:
        async with self._lock:
            for path in (self._incidents_path, self._triage_path):
                if path.exists():
                    path.unlink()
