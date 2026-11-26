from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger("mn-api")
DISPLAY_KEYS = ("alias", "display_name", "label", "name", "role")


def build_agent_graph(
    job_id: str,
    details: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    agents = details.get("agents", []) or []
    job = details.get("job", {}) or {}
    topology = job.get("topology")
    if isinstance(topology, dict):
        manifest = {
            "agents": {
                "nodes": topology.get("nodes", []),
                "edges": topology.get("edges", []),
            },
            "metadata": topology.get("metadata", {}),
        }
    else:
        manifest = load_manifest_for_job(job)
    agent_by_id = _collect_graph_agents(agents, manifest)
    edge_counts: dict[tuple[str, str, str], dict[str, Any]] = {}
    _add_manifest_edges(edge_counts, agent_by_id, manifest)
    _add_event_edges(edge_counts, agent_by_id, events)
    _add_declared_outbound_edges(edge_counts, agent_by_id, agents)

    nodes = _graph_nodes(agent_by_id)
    edges = sorted(edge_counts.values(), key=lambda edge: (edge["source"], edge["target"], edge["message_type"]))

    return {
        "job_id": job_id,
        "graph_id": job.get("graph_id") or (details.get("summary") or {}).get("graph_id"),
        "status": job.get("status") or "unknown",
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "agent_count": len(nodes),
            "edge_count": len(edges),
            "message_count": sum(edge.get("count", 0) for edge in edges),
            "event_count": len(events),
        },
    }


def _collect_graph_agents(
    agents: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    agent_by_id: dict[str, dict[str, Any]] = {}
    for agent in agents:
        agent_id = agent.get("agent_id") or agent.get("node_id")
        if not agent_id:
            continue
        normalized_id = str(agent_id)
        normalized = _graph_agent_defaults(normalized_id)
        normalized.update(agent)
        _normalize_infrastructure_agent(normalized_id, normalized)
        agent_by_id[normalized_id] = normalized

    for node in _manifest_agent_nodes(manifest):
        node_id = node.get("node_id") or node.get("agent_id")
        if not node_id:
            continue
        normalized_id = str(node_id)
        existing = agent_by_id.setdefault(
            normalized_id,
            {
                "agent_id": normalized_id,
                "agent_type": node.get("agent_type") or "unknown",
                "type": node.get("type") or "unknown",
                "status": "declared",
                "assigned_node": "unassigned",
                "processed_messages": 0,
                "mailbox_depth": 0,
            },
        )
        _copy_display_metadata(existing, node)
    return agent_by_id


def _ensure_graph_edge(
    edge_counts: dict[tuple[str, str, str], dict[str, Any]],
    agent_by_id: dict[str, dict[str, Any]],
    source: str,
    target: str,
    message_type: str,
    source_event: str,
    *,
    edge_id: str | None = None,
) -> dict[str, Any]:
    ensure_graph_agent(agent_by_id, source)
    ensure_graph_agent(agent_by_id, target)
    key = (source, target, message_type)
    return edge_counts.setdefault(
        key,
        {
            "id": edge_id or f"{source}->{target}:{message_type}",
            "source": source,
            "target": target,
            "message_type": message_type,
            "count": 0,
            "last_seen_at": None,
            "source_event": source_event,
        },
    )


def _add_manifest_edges(
    edge_counts: dict[tuple[str, str, str], dict[str, Any]],
    agent_by_id: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
) -> None:
    for edge in _manifest_agent_edges(manifest):
        source = edge.get("from_node")
        target = edge.get("to_node")
        if source and target:
            _ensure_graph_edge(
                edge_counts,
                agent_by_id,
                source,
                target,
                edge.get("message_type") or "*",
                "manifest",
                edge_id=edge.get("edge_id"),
            )


def _add_event_edges(
    edge_counts: dict[tuple[str, str, str], dict[str, Any]],
    agent_by_id: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
) -> None:
    for event in events:
        message = event_message_summary(event)
        if not message:
            continue
        source = message.get("from")
        target = message.get("to") or event.get("agent_id")
        if not source or not target:
            continue
        existing = _ensure_graph_edge(
            edge_counts,
            agent_by_id,
            source,
            target,
            message.get("type") or event.get("type") or "message",
            "agent_message_received",
        )
        existing["count"] += 1
        existing["last_seen_at"] = event.get("timestamp") or existing["last_seen_at"]
        if existing.get("source_event") == "manifest":
            existing["source_event"] = "manifest+events"


def _add_declared_outbound_edges(
    edge_counts: dict[tuple[str, str, str], dict[str, Any]],
    agent_by_id: dict[str, dict[str, Any]],
    agents: list[dict[str, Any]],
) -> None:
    for agent in agents:
        source = agent.get("agent_id") or agent.get("node_id")
        for target in (agent.get("metadata") or {}).get("outbound_edges") or []:
            if source and target:
                _ensure_graph_edge(edge_counts, agent_by_id, source, target, "*", "outbound_edges")


def _graph_nodes(agent_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for agent_id, agent in sorted(agent_by_id.items()):
        node = {
            "id": agent_id,
            "label": _graph_agent_label(agent_id, agent),
            "agent_type": agent.get("agent_type") or "unknown",
            "type": agent.get("type") or "unknown",
            "status": agent.get("status") or "unknown",
            "assigned_node": _graph_assigned_node(agent_id, agent),
            "processed_messages": agent.get("processed_messages", 0),
            "mailbox_depth": agent.get("mailbox_depth", 0),
        }
        for key in ("alias", "display_name", "role"):
            if agent.get(key) not in (None, ""):
                node[key] = str(agent[key])
        nodes.append(node)
    return nodes


def load_manifest_for_job(job: dict[str, Any]) -> dict[str, Any]:
    manifest_ref = job.get("manifest_ref") if isinstance(job.get("manifest_ref"), dict) else {}
    manifest_path = manifest_ref.get("manifest_path")
    if not isinstance(manifest_path, str) or not manifest_path:
        return {}

    path = Path(manifest_path)
    if not path.is_file():
        return {}

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.exception("Failed to load manifest for graph from %s", manifest_path)
        return {}

    return manifest if isinstance(manifest, dict) else {}


def _manifest_agent_nodes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        return []
    nodes: list[dict[str, Any]] = []
    agents = manifest.get("agents") if isinstance(manifest.get("agents"), dict) else {}
    agent_nodes = agents.get("nodes") if isinstance(agents.get("nodes"), list) else []
    nodes.extend(node for node in agent_nodes if isinstance(node, dict))
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    templates = metadata.get("agent_templates") if isinstance(metadata.get("agent_templates"), dict) else {}
    template_nodes = templates.get("nodes") if isinstance(templates.get("nodes"), list) else []
    nodes.extend(node for node in template_nodes if isinstance(node, dict))
    return nodes


def _manifest_agent_edges(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        return []
    agents = manifest.get("agents") if isinstance(manifest.get("agents"), dict) else {}
    agent_edges = agents.get("edges") if isinstance(agents.get("edges"), list) else []
    return [edge for edge in agent_edges if isinstance(edge, dict)]


def event_message_summary(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload")
    if event.get("type") == "agent_message_received" and isinstance(payload, dict):
        return payload

    if event.get("type") in {"backpressure_signal", "delivery_failed", "backpressure_rejected"} and isinstance(payload, dict):
        return payload

    message = event.get("message")
    if isinstance(message, dict):
        envelope = message.get("envelope")
        if isinstance(envelope, dict):
            return envelope
        return message

    return None


def ensure_graph_agent(agent_by_id: dict[str, dict[str, Any]], agent_id: str) -> None:
    agent_by_id.setdefault(agent_id, _graph_agent_defaults(agent_id))


def _graph_agent_defaults(agent_id: str) -> dict[str, Any]:
    if agent_id == "runtime":
        return {
            "agent_id": agent_id,
            "agent_type": "system",
            "type": "runtime",
            "status": "observed",
            "assigned_node": "system/runtime",
            "processed_messages": 0,
            "mailbox_depth": 0,
            "display_name": "System Runtime",
            "role": "Runtime infrastructure",
        }
    return {
        "agent_id": agent_id,
        "agent_type": "external",
        "type": "message",
        "status": "observed",
        "assigned_node": "unknown",
        "processed_messages": 0,
        "mailbox_depth": 0,
    }


def _normalize_infrastructure_agent(agent_id: str, agent: dict[str, Any]) -> None:
    if agent_id != "runtime":
        return
    agent["agent_type"] = "system"
    agent["type"] = "runtime"
    agent.setdefault("display_name", "System Runtime")
    agent.setdefault("role", "Runtime infrastructure")
    if agent.get("assigned_node") in (None, "", "unknown", "unassigned"):
        agent["assigned_node"] = "system/runtime"


def _copy_display_metadata(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in DISPLAY_KEYS:
        if source.get(key) not in (None, "") and target.get(key) in (None, ""):
            target[key] = source[key]


def _graph_agent_label(agent_id: str, agent: dict[str, Any]) -> str:
    for key in ("alias", "display_name", "label", "role"):
        value = agent.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() != "unknown":
            return value.strip()
    if agent_id == "runtime":
        return "System Runtime"
    return agent_id


def _graph_assigned_node(agent_id: str, agent: dict[str, Any]) -> str:
    value = agent.get("assigned_node")
    if isinstance(value, str) and value.strip() and value.strip().lower() not in {"unknown", "unassigned"}:
        return value.strip()
    if agent_id == "runtime":
        return "system/runtime"
    return "unassigned"
