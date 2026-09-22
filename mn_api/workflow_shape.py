"""Public Job workflow shape and progress-only projections."""

from __future__ import annotations

from typing import Any

from mn_sdk.workflow_progress_graph import workflow_graph_from_manifest


def definition_shape(job_id: str, definition: dict[str, Any]) -> dict[str, Any]:
    workflow = definition.get("workflow") if isinstance(definition.get("workflow"), dict) else {}
    runtime = definition.get("runtime") if isinstance(definition.get("runtime"), dict) else {}
    bindings = runtime.get("bindings") if isinstance(runtime.get("bindings"), dict) else {}
    graph = workflow_graph_from_manifest({"workflow": workflow})
    steps = []
    for raw in workflow.get("steps") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        step_id = str(raw["id"])
        binding = bindings.get(raw.get("run") or step_id) or bindings.get(step_id)
        steps.append({
            "id": step_id,
            "label": str(raw.get("label") or step_id.replace("_", " ").title()),
            "goal": str(raw.get("goal") or raw.get("action") or ""),
            "agents": _declared_agents(binding, raw),
        })
    return {
        "job_id": job_id,
        "source": "definition",
        "run_id": None,
        "workflow_id": str(workflow.get("workflow_id") or ""),
        "graph_revision": None,
        "steps": steps,
        "edges": graph["edges"],
        "layers": graph["layers"],
    }


def latest_run_shape(job_id: str, run_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    steps = []
    for raw in snapshot.get("steps") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        steps.append({
            "id": str(raw["id"]),
            "label": str(raw.get("label") or ""),
            "goal": str(raw.get("goal") or ""),
            "agents": [_agent(agent) for agent in raw.get("agents") or [] if isinstance(agent, dict)],
        })
    return {
        "job_id": job_id,
        "source": "latest_run",
        "run_id": run_id,
        "workflow_id": str(snapshot.get("workflow_id") or ""),
        "graph_revision": snapshot.get("graph_revision"),
        "steps": steps,
        "edges": [edge for edge in snapshot.get("edges") or [] if isinstance(edge, dict)],
        "layers": [layer for layer in snapshot.get("layers") or [] if isinstance(layer, list)],
    }


def dag_view(shape: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: shape[key] for key in ("job_id", "source", "run_id", "workflow_id", "graph_revision")},
        "nodes": [{"id": step["id"], "label": step["label"]} for step in shape["steps"]],
        "edges": shape["edges"],
        "layers": shape["layers"],
    }


def steps_view(shape: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: shape[key] for key in ("job_id", "source", "run_id", "workflow_id", "graph_revision")},
        "steps": shape["steps"],
    }


_PROGRESS_FIELDS = {
    "id", "status", "current", "done_count", "running_count", "idle_count",
    "ready_count", "failed_count", "total_count", "live", "elapsed_seconds",
    "started_at", "ended_at", "last_event_at", "retry_at", "deadline_at",
    "heartbeat_deadline_at", "attempt", "attempt_id", "status_reason", "failure",
    "progress", "progress_source", "items_done", "items_total", "tokens_used",
    "mailbox_depth", "activity_summary", "last_activity", "recent_events", "agents",
}


def progress_only(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in snapshot.items()
              if key not in {"steps", "current_step", "edges", "layers", "description", "name", "workflow_kind", "graph_revision"}}
    result["steps"] = [_progress_item(step) for step in snapshot.get("steps") or [] if isinstance(step, dict)]
    current = snapshot.get("current_step")
    result["current_step"] = _progress_item(current) if isinstance(current, dict) else None
    return result


def _progress_item(item: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in item.items() if key in _PROGRESS_FIELDS and key != "agents"}
    if isinstance(item.get("agents"), list):
        result["agents"] = [_progress_item(agent) for agent in item["agents"] if isinstance(agent, dict)]
    return result


def _declared_agents(binding: Any, step: dict[str, Any]) -> list[dict[str, str]]:
    binding = binding if isinstance(binding, dict) else {}
    workers = binding.get("workers")
    if not isinstance(workers, list):
        workers = [binding["worker"]] if isinstance(binding.get("worker"), dict) else []
    if not workers:
        ids = step.get("agent_ids") or ([step["agent_id"]] if step.get("agent_id") else [])
        workers = [{"id": agent_id} for agent_id in ids]
    return [_agent(worker) for worker in workers if isinstance(worker, dict)]


def _agent(raw: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(raw.get("id") or raw.get("node_id") or ""),
        "display_name": str(raw.get("display_name") or raw.get("alias") or raw.get("label") or raw.get("name") or ""),
        "role": str(raw.get("role") or raw.get("working_on") or ""),
        "model": str(raw.get("model") or raw.get("uses") or ""),
    }
