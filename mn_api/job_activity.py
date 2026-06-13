from __future__ import annotations

import re
from typing import Any

from mn_sdk import failure_from_event

from mn_api.run_store import first_string


MAX_COMPACT_STRING = 2000
MAX_COMPACT_LIST = 25
MAX_COMPACT_DEPTH = 5
MAX_ACTIVITY_EVENTS = 8
BLOB_KEYS = {
    "logs",
    "log",
    "stdout",
    "stderr",
    "content",
    "file_data",
    "fileData",
    "pdf_bytes",
    "pdfBytes",
    "bytes",
    "data_uri",
    "base64",
    "payloads_bytes",
    "final_artifact",
    "finalArtifact",
    "result",
}


def _compact_blob(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {
            "omitted": True,
            "type": "string",
            "chars": len(value),
            "preview": value[:200],
        }
    if isinstance(value, bytes):
        return {"omitted": True, "type": "bytes", "bytes": len(value)}
    if isinstance(value, dict):
        return {"omitted": True, "type": "object", "keys": sorted(str(key) for key in value.keys())[:25]}
    if isinstance(value, list):
        return {"omitted": True, "type": "array", "items": len(value)}
    return {"omitted": True, "type": type(value).__name__}


def compact_value(value: Any, depth: int = 0) -> Any:
    if depth > MAX_COMPACT_DEPTH:
        return _compact_blob(value)
    if isinstance(value, str):
        if len(value) > MAX_COMPACT_STRING:
            return {
                "truncated": True,
                "chars": len(value),
                "preview": value[:MAX_COMPACT_STRING],
            }
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return {"omitted": True, "type": "bytes", "bytes": len(value)}
    if isinstance(value, list):
        items = [compact_value(item, depth + 1) for item in value[:MAX_COMPACT_LIST]]
        if len(value) > MAX_COMPACT_LIST:
            items.append({"omitted_items": len(value) - MAX_COMPACT_LIST})
        return items
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in BLOB_KEYS:
                compact[key_text] = _compact_blob(item)
            else:
                compact[key_text] = compact_value(item, depth + 1)
        return compact
    return str(value)


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    failure = failure_from_event(event)
    compact = {
        "type": event.get("type"),
        "timestamp": event.get("timestamp") or event.get("ts"),
        "agent_id": event.get("agent_id") or event.get("node_id"),
        "status": event.get("status"),
    }
    if failure:
        compact["failure"] = {
            "schema_version": failure.get("schema_version"),
            "code": failure.get("code"),
            "desc": failure.get("desc"),
            "severity": failure.get("severity"),
            "details": compact_value(failure.get("details")),
            "remediation": failure.get("remediation"),
            "links": failure.get("links"),
        }
    for key in ("message", "payload", "sandbox", "error", "reason"):
        if key in event:
            compact[key] = compact_value(event[key])
    return {key: value for key, value in compact.items() if value not in (None, "")}


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _event_step_id(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    return first_string(
        payload.get("step"),
        payload.get("step_id"),
        payload.get("phase"),
        payload.get("phase_id"),
        event.get("step"),
        event.get("step_id"),
        event.get("phase"),
        event.get("phase_id"),
    )


def _event_agent_id(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    return first_string(
        payload.get("worker"),
        payload.get("agent_id"),
        payload.get("node_id"),
        event.get("worker"),
        event.get("agent_id"),
        event.get("node_id"),
    )


def _humanize_event_type(event_type: Any) -> str:
    text = re.sub(r"[_-]+", " ", str(event_type or "")).strip()
    return " ".join(text.split()).capitalize()


def _compact_activity_text(value: Any, limit: int = 320, *, prefer_tail: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    if prefer_tail:
        suffix = text[-max(limit - 15, 0) :].lstrip()
        return "[truncated] " + suffix
    return text[: max(limit - 15, 0)].rstrip() + " [truncated]"


def _event_category(event: dict[str, Any], payload: dict[str, Any], failure: dict[str, Any] | None = None) -> str:
    category = first_string(payload.get("category"), event.get("category"))
    if category in {"agent", "tool", "system", "artifact", "error"}:
        return category
    event_type = str(event.get("type") or "").lower()
    if failure or "failed" in event_type or "error" in event_type or "timed_out" in event_type or "retry" in event_type:
        return "error"
    if event_type.startswith("tool_") or "tool_call" in event_type:
        return "tool"
    if event_type in {"artifact_written"} or "artifact" in event_type:
        return "artifact"
    if (
        event_type.startswith("docker_worker_")
        or event_type.startswith("executor_")
        or event_type.startswith("workflow_")
        or event_type.startswith("sandbox_")
    ):
        return "system"
    if event_type.startswith("financial_") or event_type in {"agent_activity", "blueprint_phase_started", "blueprint_phase_completed"}:
        return "agent"
    return "system"


def _activity_message(event: dict[str, Any], *, step_id: str = "", agent_id: str = "") -> str:
    event_type = str(event.get("type") or "")
    payload = _event_payload(event)
    failure = failure_from_event(event)
    message = first_string(
        payload.get("message"),
        event.get("message"),
        payload.get("result_summary"),
        payload.get("working_on"),
        payload.get("task"),
        payload.get("reason"),
        event.get("reason"),
        payload.get("status_reason"),
        payload.get("status"),
        event.get("status"),
    )
    if message:
        return _compact_activity_text(message)
    if failure:
        return _compact_activity_text(first_string(failure.get("desc"), failure.get("code"), "Failure"))
    normalized = re.sub(r"[^a-z0-9]+", "_", event_type.lower()).strip("_")
    if normalized == "docker_worker_build_started":
        return "DockerWorker image build started"
    if normalized == "docker_worker_build_completed":
        return "DockerWorker image build completed"
    if normalized == "docker_worker_build_failed":
        return "DockerWorker image build failed"
    if normalized == "docker_worker_command_started":
        return "DockerWorker command started"
    if normalized == "docker_worker_command_completed":
        return "DockerWorker command completed"
    if normalized == "docker_worker_command_timed_out":
        return "DockerWorker command timed out"
    if normalized in {"workflow_step_attempt_completed", "sandbox_job_completed"}:
        return f"Agent completed: {agent_id or _event_agent_id(event) or 'unknown'}"
    if normalized in {"workflow_worker_started", "workflow_step_attempt_started"}:
        return f"Agent working: {agent_id or _event_agent_id(event) or 'unknown'}"
    if normalized in {"workflow_step_completed", "blueprint_phase_completed"}:
        return f"Step completed: {step_id or _event_step_id(event) or 'step'}"
    if normalized in {"workflow_step_started", "blueprint_phase_started"}:
        return f"Step started: {step_id or _event_step_id(event) or 'step'}"
    if normalized in {"workflow_step_attempt_retry_scheduled", "workflow_step_attempt_timed_out"}:
        return f"Retry pending: {step_id or _event_step_id(event) or 'step'}"
    if normalized == "workflow_step_blocked":
        return f"Blocked: {step_id or _event_step_id(event) or 'step'}"
    return _humanize_event_type(event_type or "event")


def _compact_activity_event(event: dict[str, Any], *, step_id: str = "", agent_id: str = "") -> dict[str, Any]:
    payload = _event_payload(event)
    failure = failure_from_event(event)
    category = _event_category(event, payload, failure)
    compact = {
        "timestamp": event.get("timestamp") or event.get("ts"),
        "type": event.get("type"),
        "category": category,
        "step_id": step_id or _event_step_id(event),
        "agent_id": agent_id or _event_agent_id(event),
        "status": first_string(event.get("status"), payload.get("status")),
        "message": _activity_message(event, step_id=step_id, agent_id=agent_id),
    }
    for key in ("tool_name", "target", "duration_ms", "result_summary", "details"):
        value = payload.get(key)
        if value not in (None, "", {}):
            if key == "details":
                compact[key] = compact_value(value)
            elif isinstance(value, str):
                compact[key] = _compact_activity_text(value, prefer_tail=key == "result_summary")
            else:
                compact[key] = value
    if payload:
        compact["payload"] = compact_value(payload)
    if failure:
        compact["failure"] = {
            "code": failure.get("code"),
            "desc": _compact_activity_text(failure.get("desc")),
            "severity": failure.get("severity"),
        }
    return {key: value for key, value in compact.items() if value not in (None, "")}


def _agent_ids_match(known: str, observed: str) -> bool:
    if known == observed:
        return True
    known_tail = known.split(":")[-1]
    observed_tail = observed.split(":")[-1]
    return known_tail == observed_tail or known.endswith(f":{observed}") or observed.endswith(f":{known}")


def _agent_step_id(agent_to_step: dict[str, str], agent_id: str) -> str:
    if not agent_id:
        return ""
    if agent_id in agent_to_step:
        return agent_to_step[agent_id]
    for known_agent_id, step_id in agent_to_step.items():
        if _agent_ids_match(known_agent_id, agent_id):
            return step_id
    return ""


def enrich_workflow_progress_activity(snapshot: dict[str, Any], events: list[dict[str, Any]]) -> None:
    steps = snapshot.get("steps")
    if not isinstance(steps, list) or not steps:
        return

    steps_by_id: dict[str, dict[str, Any]] = {}
    agent_to_step: dict[str, str] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = first_string(step.get("id"))
        if not step_id:
            continue
        steps_by_id[step_id] = step
        agents = step.get("agents")
        if isinstance(agents, list):
            for agent in agents:
                if not isinstance(agent, dict):
                    continue
                agent_id = first_string(agent.get("id"), agent.get("agent_id"))
                if agent_id:
                    agent_to_step[agent_id] = step_id

    step_events: dict[str, list[dict[str, Any]]] = {step_id: [] for step_id in steps_by_id}
    agent_events: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        step_id = _event_step_id(event)
        agent_id = _event_agent_id(event)
        if step_id not in steps_by_id and agent_id:
            step_id = _agent_step_id(agent_to_step, agent_id)
        if not step_id or step_id not in steps_by_id:
            continue
        compact = _compact_activity_event(event, step_id=step_id, agent_id=agent_id)
        step_events.setdefault(step_id, []).append(compact)
        if agent_id:
            agent_events.setdefault((step_id, agent_id), []).append(compact)

    for step_id, step in steps_by_id.items():
        recent = step_events.get(step_id, [])[-MAX_ACTIVITY_EVENTS:]
        if recent:
            step["recent_events"] = recent
            step["last_activity"] = recent[-1]
            step["activity_summary"] = first_string(recent[-1].get("message"), recent[-1].get("type"))
        agents = step.get("agents")
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_id = first_string(agent.get("id"), agent.get("agent_id"))
            if not agent_id:
                continue
            recent_agent_events: list[dict[str, Any]] = []
            for (event_step_id, event_agent_id), values in agent_events.items():
                if event_step_id == step_id and _agent_ids_match(agent_id, event_agent_id):
                    recent_agent_events.extend(values)
            recent_agent_events = recent_agent_events[-MAX_ACTIVITY_EVENTS:]
            if recent_agent_events:
                agent["recent_events"] = recent_agent_events
                agent["last_activity"] = recent_agent_events[-1]
                agent["activity_summary"] = first_string(
                    recent_agent_events[-1].get("message"),
                    recent_agent_events[-1].get("type"),
                )

    current_step = snapshot.get("current_step")
    if isinstance(current_step, dict):
        step_id = first_string(current_step.get("id"))
        enriched = steps_by_id.get(step_id)
        if enriched:
            snapshot["current_step"] = {**enriched, "current": current_step.get("current", enriched.get("current", True))}
