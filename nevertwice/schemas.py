"""Typed models shared by the agent, the memory layer and the HTTP API."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

INCIDENT_ID_RE = re.compile(r"\bINC-\d{3,6}\b", re.IGNORECASE)
MEMORY_REF_RE = re.compile(r"\bM(\d{1,3})\b")

MAX_LOG_CHARS = 6000


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Alert(BaseModel):
    id: str = Field(default="", max_length=64)
    title: str = Field(min_length=3, max_length=300)
    service: str = Field(min_length=1, max_length=80)
    severity: str = Field(default="SEV2", max_length=10)
    fired_at: str = Field(default_factory=now_iso)
    source: str = Field(default="", max_length=300)
    metrics: str = Field(default="", max_length=1500)
    logs: str = Field(default="", max_length=MAX_LOG_CHARS * 3)
    recent_changes: str = Field(default="", max_length=1500)

    @field_validator("logs")
    @classmethod
    def _trim_logs(cls, value: str) -> str:
        value = value.strip()
        if len(value) > MAX_LOG_CHARS:
            value = value[:MAX_LOG_CHARS] + "\n...[truncated]"
        return value

    @field_validator("title", "service", "severity", "source", "metrics", "recent_changes")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


# ------------------------------------------------------------ triage report
class RecommendedAction(BaseModel):
    title: str
    command: str = ""
    why: str = ""
    evidence: list[str] = Field(default_factory=list)
    requires_approval: bool = False


class AvoidAction(BaseModel):
    title: str
    why: str = ""
    evidence: list[str] = Field(default_factory=list)


class SimilarIncident(BaseModel):
    incident_id: str
    similarity: str = ""


class TriageReport(BaseModel):
    summary: str = ""
    likely_root_cause: str = ""
    confidence: Literal["low", "medium", "high"] = "low"
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    avoid_actions: list[AvoidAction] = Field(default_factory=list)
    similar_incidents: list[SimilarIncident] = Field(default_factory=list)
    escalate_to: str = ""
    open_questions: list[str] = Field(default_factory=list)

    @classmethod
    def from_loose(cls, data: dict[str, Any]) -> "TriageReport":
        """Build a report from model output, tolerating missing or oddly-typed fields."""
        data = dict(data or {})

        def text(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, (list, tuple)):
                return "; ".join(text(v) for v in value if v is not None)
            if isinstance(value, dict):
                return "; ".join(f"{k}: {text(v)}" for k, v in value.items())
            return str(value).strip()

        def ids(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                value = re.split(r"[,\s;]+", value)
            out: list[str] = []
            for item in value if isinstance(value, (list, tuple)) else [value]:
                token = text(item).strip().strip(".,;()[]")
                if token and token.upper() not in {x.upper() for x in out}:
                    out.append(token)
            return out[:8]

        def items(value: Any) -> list[dict[str, Any]]:
            if value is None:
                return []
            if isinstance(value, (str, dict)):
                value = [value]
            result = []
            for entry in value:
                if isinstance(entry, str):
                    entry = {"title": entry}
                if isinstance(entry, dict):
                    result.append(entry)
            return result

        confidence = text(data.get("confidence")).lower()
        if confidence not in ("low", "medium", "high"):
            confidence = "medium" if confidence in ("moderate", "med") else "low"

        recommended = []
        for entry in items(data.get("recommended_actions"))[:7]:
            title = text(entry.get("title") or entry.get("action") or entry.get("step"))
            if not title:
                continue
            recommended.append(
                RecommendedAction(
                    title=title,
                    command=text(entry.get("command")),
                    why=text(entry.get("why") or entry.get("rationale") or entry.get("reason")),
                    evidence=ids(entry.get("evidence")),
                    requires_approval=bool(entry.get("requires_approval", False)),
                )
            )
        avoid = []
        for entry in items(data.get("avoid_actions"))[:6]:
            title = text(entry.get("title") or entry.get("action"))
            if not title:
                continue
            avoid.append(
                AvoidAction(
                    title=title,
                    why=text(entry.get("why") or entry.get("reason") or entry.get("rationale")),
                    evidence=ids(entry.get("evidence")),
                )
            )
        similar = []
        for entry in items(data.get("similar_incidents"))[:6]:
            incident_id = text(entry.get("incident_id") or entry.get("id") or entry.get("title"))
            if incident_id:
                similar.append(SimilarIncident(incident_id=incident_id, similarity=text(entry.get("similarity") or entry.get("why"))))
        questions = data.get("open_questions") or []
        if isinstance(questions, str):
            questions = [questions]
        return cls(
            summary=text(data.get("summary")),
            likely_root_cause=text(data.get("likely_root_cause") or data.get("root_cause")),
            confidence=confidence,  # type: ignore[arg-type]
            recommended_actions=recommended,
            avoid_actions=avoid,
            similar_incidents=similar,
            escalate_to=text(data.get("escalate_to")),
            open_questions=[text(q) for q in questions if text(q)][:5],
        )


# --------------------------------------------------------------- resolution
class Resolution(BaseModel):
    """What the responder tells NeverTwice after an incident: this is how it learns."""

    alert_id: str = Field(default="", max_length=64)
    triage_id: str = Field(default="", max_length=64)
    title: str = Field(min_length=3, max_length=300)
    service: str = Field(min_length=1, max_length=80)
    severity: str = Field(default="SEV2", max_length=10)
    started_at: str = Field(default_factory=now_iso)
    root_cause: str = Field(min_length=10, max_length=3000)
    worked: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    made_worse: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=3000)
    responder: str = Field(default="On-call engineer", max_length=100)
    recommendation_feedback: Literal["yes", "partly", "no", ""] = ""

    @field_validator("worked", "failed", "made_worse")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip()[:1000] for v in value if v and v.strip()]
        return cleaned[:10]

    @field_validator("title", "service", "root_cause", "notes", "responder")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
