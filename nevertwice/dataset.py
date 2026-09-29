"""Seed data (BasketBolt's incident history) and how it is turned into Hindsight memories.

Each past incident becomes several retained items, deliberately split by outcome so
that recall can be scoped with tags:

* ``kind:postmortem``  – the narrative: symptoms, log signatures, timeline, root cause
* ``kind:remediation`` + ``outcome:worked|failed|made_worse`` – one item per outcome
* ``kind:change``      – notable deploys/config changes (for "what changed?" reasoning)
* ``kind:convention``  – team rules and escalation owners

Every item carries the incident id in its ``context`` and ``document_id`` so that
recalled facts can be traced back to the incident they came from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .schemas import Resolution

OUTCOME_LABEL = {
    "worked": "fix that worked",
    "failed": "attempt that did NOT work",
    "made_worse": "attempt that made things WORSE",
}


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _date_label(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y %H:%M IST")
    except ValueError:
        return iso


@dataclass
class Dataset:
    company: dict[str, Any]
    incidents: list[dict[str, Any]]
    alerts: list[dict[str, Any]]
    incident_index: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path) -> "Dataset":
        company = _load_json(data_dir / "company.json")
        incidents = _load_json(data_dir / "incidents.json")
        alerts = _load_json(data_dir / "alerts.json")
        incidents.sort(key=lambda inc: inc["started_at"])
        ds = cls(company=company, incidents=incidents, alerts=alerts)
        ds.incident_index = {inc["id"].upper(): inc for inc in incidents}
        return ds

    def alert(self, alert_id: str) -> dict[str, Any] | None:
        for alert in self.alerts:
            if alert["id"] == alert_id:
                return alert
        return None

    def max_incident_number(self) -> int:
        numbers = [int(inc["id"].split("-")[1]) for inc in self.incidents]
        return max(numbers) if numbers else 2000


# --------------------------------------------------------- memory items
def incident_memory_items(inc: dict[str, Any]) -> list[dict[str, Any]]:
    """Render one historical incident into Hindsight retain items."""
    iid, svc, sev = inc["id"], inc["service"], inc["severity"]
    base_tags = [f"service:{svc}", f"incident:{iid.lower()}", f"severity:{sev.lower()}"]
    when = _date_label(inc["started_at"])
    timeline = "; ".join(f"{t['at']} {t['event']}" for t in inc.get("timeline", []))
    actions = "; ".join(f"{a['item']} ({a['status'].upper()})" for a in inc.get("action_items", []))
    people = f"On-call responder: {inc['responder']}."
    if inc.get("incident_commander"):
        people += f" Incident commander: {inc['incident_commander']}."
    postmortem = "\n".join(
        [
            f"Postmortem {iid} ({sev}, service {svc}) started {when}, resolved {_date_label(inc['resolved_at'])}.",
            f"Title: {inc['title']}.",
            people,
            f"Detected by: {inc['detected_by']}.",
            f"Impact: {inc['impact']}",
            f"Symptoms: {'; '.join(inc.get('symptoms', []))}.",
            f"Log signature:\n{inc['logs']}",
            f"Timeline (IST): {timeline}.",
            f"Root cause of {iid}: {inc['root_cause']}",
            f"Contributing factors: {'; '.join(inc.get('contributing_factors', []))}.",
            f"Action items from {iid}: {actions}.",
        ]
    )
    items: list[dict[str, Any]] = [
        {
            "content": postmortem,
            "context": f"{iid} postmortem ({svc})",
            "timestamp": inc["started_at"],
            "document_id": f"{iid}:postmortem",
            "tags": base_tags + ["kind:postmortem"],
            "metadata": {"incident_id": iid, "service": svc, "kind": "postmortem"},
        }
    ]
    by_outcome: dict[str, list[dict[str, Any]]] = {}
    for rem in inc.get("remediations", []):
        by_outcome.setdefault(rem["outcome"], []).append(rem)
    day = inc["started_at"][:10]
    for outcome, rems in by_outcome.items():
        lines = [f"Remediation outcomes during incident {iid} on {svc} ({when}) — {OUTCOME_LABEL[outcome]}:"]
        for rem in rems:
            verdict = {"worked": "WORKED", "failed": "DID NOT WORK", "made_worse": "MADE THINGS WORSE"}[outcome]
            line = f"- At {rem.get('at', '?')} IST the team tried: {rem['action']}."
            if rem.get("command"):
                line += f" Command: {rem['command']}"
            line += f" Outcome: {verdict}. {rem['result']}"
            lines.append(line)
        at = rems[0].get("at")
        ts = f"{day}T{at}:00+05:30" if at and len(at) == 5 else inc["started_at"]
        items.append(
            {
                "content": "\n".join(lines),
                "context": f"{iid} remediation — {OUTCOME_LABEL[outcome]} ({svc})",
                "timestamp": ts,
                "document_id": f"{iid}:remediation:{outcome}",
                "tags": base_tags + ["kind:remediation", f"outcome:{outcome}"],
                "metadata": {"incident_id": iid, "service": svc, "kind": "remediation", "outcome": outcome},
            }
        )
    return items


def company_memory_items(company: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    info = company["company"]
    services = "; ".join(f"{s['name']} (team {s['team']}, owner {s['owner']}, {s['stack']}, {s['slack']})" for s in company["services"])
    people = "; ".join(f"{p['name']}: {p['role']}" for p in company["people"])
    items.append(
        {
            "content": (
                f"{info['name']} company and platform overview. {info['description']} Stack: {info['stack']} "
                f"Timezone: {info['timezone']}.\nServices: {services}.\nPeople: {people}."
            ),
            "context": "team handbook — services and owners",
            "timestamp": "2026-04-01T09:00:00+05:30",
            "document_id": "handbook:overview",
            "tags": ["kind:convention"],
            "metadata": {"kind": "handbook"},
        }
    )
    for conv in company.get("conventions", []):
        items.append(
            {
                "content": f"{conv['title']}. {conv['text']}",
                "context": f"on-call convention {conv['id']}",
                "timestamp": "2026-09-01T09:00:00+05:30",
                "document_id": f"convention:{conv['id']}",
                "tags": ["kind:convention"],
                "metadata": {"kind": "convention", "convention_id": conv["id"]},
            }
        )
    for chg in company.get("changes", []):
        items.append(
            {
                "content": f"Change {chg['id']} on {_date_label(chg['at'])} ({chg['service']}): {chg['summary']}",
                "context": f"change log {chg['id']} ({chg['service']})",
                "timestamp": chg["at"],
                "document_id": f"change:{chg['id']}",
                "tags": [f"service:{chg['service']}", "kind:change"],
                "metadata": {"kind": "change", "service": chg["service"], "change_id": chg["id"]},
            }
        )
    return items


def all_seed_items(ds: Dataset) -> list[dict[str, Any]]:
    items = company_memory_items(ds.company)
    for inc in ds.incidents:
        items.extend(incident_memory_items(inc))
    return items


def resolution_to_incident(res: Resolution, incident_id: str) -> dict[str, Any]:
    """Turn a live resolution into the same incident shape as the seed data."""
    remediations = []
    for outcome, actions in (("worked", res.worked), ("failed", res.failed), ("made_worse", res.made_worse)):
        for action in actions:
            remediations.append({"action": action, "outcome": outcome, "result": ""})
    return {
        "id": incident_id,
        "title": res.title,
        "service": res.service,
        "severity": res.severity,
        "started_at": res.started_at,
        "resolved_at": res.started_at,
        "responder": res.responder,
        "detected_by": f"Alert {res.alert_id}" if res.alert_id else "On-call report",
        "impact": "",
        "symptoms": [],
        "logs": "",
        "timeline": [],
        "root_cause": res.root_cause,
        "contributing_factors": [],
        "remediations": remediations,
        "action_items": [],
        "notes": res.notes,
        "source": "live",
    }


def resolution_memory_items(
    res: Resolution,
    incident_id: str,
    alert: dict[str, Any] | None,
    recommendation: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Items retained when an engineer resolves an incident in NeverTwice."""
    svc, sev = res.service, res.severity
    when = _date_label(res.started_at)
    base_tags = [f"service:{svc}", f"incident:{incident_id.lower()}", f"severity:{sev.lower()}"]
    parts = [
        f"Postmortem {incident_id} ({sev}, service {svc}) started {when}.",
        f"Title: {res.title}.",
        f"On-call responder: {res.responder}.",
    ]
    if alert:
        if alert.get("metrics"):
            parts.append(f"Symptoms: {alert['metrics']}.")
        if alert.get("logs"):
            parts.append(f"Log signature:\n{alert['logs'][:1500]}")
        if alert.get("recent_changes"):
            parts.append(f"Recent changes before the incident: {alert['recent_changes']}")
    parts.append(f"Root cause of {incident_id}: {res.root_cause}")
    if res.notes:
        parts.append(f"Responder notes: {res.notes}")
    items: list[dict[str, Any]] = [
        {
            "content": "\n".join(parts),
            "context": f"{incident_id} postmortem ({svc})",
            "timestamp": res.started_at,
            "document_id": f"{incident_id}:postmortem",
            "tags": base_tags + ["kind:postmortem"],
            "metadata": {"incident_id": incident_id, "service": svc, "kind": "postmortem"},
        }
    ]
    verdicts = {"worked": "WORKED", "failed": "DID NOT WORK", "made_worse": "MADE THINGS WORSE"}
    for outcome, actions in (("worked", res.worked), ("failed", res.failed), ("made_worse", res.made_worse)):
        if not actions:
            continue
        lines = [f"Remediation outcomes during incident {incident_id} on {svc} ({when}) — {OUTCOME_LABEL[outcome]}:"]
        lines += [f"- The team tried: {a}. Outcome: {verdicts[outcome]}." for a in actions]
        items.append(
            {
                "content": "\n".join(lines),
                "context": f"{incident_id} remediation — {OUTCOME_LABEL[outcome]} ({svc})",
                "timestamp": res.started_at,
                "document_id": f"{incident_id}:remediation:{outcome}",
                "tags": base_tags + ["kind:remediation", f"outcome:{outcome}"],
                "metadata": {"incident_id": incident_id, "service": svc, "kind": "remediation", "outcome": outcome},
            }
        )
    if recommendation and res.recommendation_feedback:
        verdict = {"yes": "was correct", "partly": "was partly correct", "no": "was wrong"}[res.recommendation_feedback]
        top = recommendation.get("top_action") or "(no action)"
        items.append(
            {
                "content": (
                    f"NeverTwice triage feedback for {incident_id} on {svc}: I recommended '{top}' as the first action. "
                    f"The responder {res.responder} said my recommendation {verdict}. "
                    f"The actual fix was: {'; '.join(res.worked) or 'not recorded'}."
                ),
                "context": f"{incident_id} NeverTwice feedback ({svc})",
                "timestamp": res.started_at,
                "document_id": f"{incident_id}:feedback",
                "tags": base_tags + ["kind:feedback"],
                "metadata": {"incident_id": incident_id, "service": svc, "kind": "feedback"},
            }
        )
    return items
