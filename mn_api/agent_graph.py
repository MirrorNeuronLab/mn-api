from __future__ import annotations

from typing import Any, Dict, Optional
import json
import logging
from pathlib import Path


logger = logging.getLogger("mn-api")
DISPLAY_KEYS = ("alias", "display_name", "label", "name", "role")


def build_agent_graph(job_id: str, details: Dict[str, Any], events: list[Dict[str, Any]]):
    agents = details.get("agents", []) or []
    job = details.get("job", {}) or {}
    manifest = job.get("topology") or load_manifest_for_job(job)
    agent_by_id: Dict[str, Dict[str, Any]] = {}

    for agent in agents:
        agent_id = agent.get("agent_id") or agent.get("node_id")
        if agent_id:
            normalized = _graph_agent_defaults(str(agent_id))
            normalized.update(agent)
            _normalize_infrastructure_agent(str(agent_id), normalized)
            agent_by_id[str(agent_id)] = normalized

    for node in _manifest_agent_nodes(manifest):
        node_id = node.get("node_id") or node.get("agent_id")
        if node_id:
            existing = agent_by_id.setdefault(
                str(node_id),
                {
                    "agent_id": str(node_id),
                    "agent_type": node.get("agent_type") or "unknown",
                    "type": node.get("type") or "unknown",
                    "status": "declared",
                    "assigned_node": "unassigned",
                    "processed_messages": 0,
                    "mailbox_depth": 0,
                },
            )
            _copy_display_metadata(existing, node)

    edge_counts: Dict[tuple[str, str, str], Dict[str, Any]] = {}

    for edge in manifest.get("edges", []) if isinstance(manifest, dict) else []:
        source = edge.get("from_node")
        target = edge.get("to_node")
        message_type = edge.get("message_type") or "*"
        if not source or not target:
            continue

        ensure_graph_agent(agent_by_id, source)
        ensure_graph_agent(agent_by_id, target)
        key = (source, target, message_type)
        edge_counts.setdefault(
            key,
            {
                "id": edge.get("edge_id") or f"{source}->{target}:{message_type}",
                "source": source,
                "target": target,
                "message_type": message_type,
                "count": 0,
                "last_seen_at": None,
                "source_event": "manifest",
            },
        )

    for event in events:
        message = event_message_summary(event)
        if not message:
            continue

        source = message.get("from")
        target = message.get("to") or event.get("agent_id")
        message_type = message.get("type") or event.get("type") or "message"

        if not source or not target:
            continue

        ensure_graph_agent(agent_by_id, source)
        ensure_graph_agent(agent_by_id, target)
        key = (source, target, message_type)
        existing = edge_counts.setdefault(
            key,
            {
                "id": f"{source}->{target}:{message_type}",
                "source": source,
                "target": target,
                "message_type": message_type,
                "count": 0,
                "last_seen_at": None,
                "source_event": "agent_message_received",
            },
        )
        existing["count"] += 1
        existing["last_seen_at"] = event.get("timestamp") or existing["last_seen_at"]
        if existing.get("source_event") == "manifest":
            existing["source_event"] = "manifest+events"

    for agent in agents:
        source = agent.get("agent_id") or agent.get("node_id")
        outbound_edges = (agent.get("metadata") or {}).get("outbound_edges") or []
        for target in outbound_edges:
            if not source or not target:
                continue
            ensure_graph_agent(agent_by_id, source)
            ensure_graph_agent(agent_by_id, target)
            key = (source, target, "*")
            edge_counts.setdefault(
                key,
                {
                    "id": f"{source}->{target}:*",
                    "source": source,
                    "target": target,
                    "message_type": "*",
                    "count": 0,
                    "last_seen_at": None,
                    "source_event": "outbound_edges",
                },
            )

    nodes = []
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


def load_manifest_for_job(job: Dict[str, Any]) -> Dict[str, Any]:
    manifest_ref = job.get("manifest_ref") or {}
    manifest_path = manifest_ref.get("manifest_path")
    if not manifest_path:
        return {}

    path = Path(manifest_path)
    if not path.is_file():
        return {}

    try:
        manifest = json.loads(path.read_text())
    except Exception:
        logger.exception("Failed to load manifest for graph from %s", manifest_path)
        return {}

    return manifest if isinstance(manifest, dict) else {}


def _manifest_agent_nodes(manifest: Dict[str, Any]) -> list[Dict[str, Any]]:
    if not isinstance(manifest, dict):
        return []
    nodes: list[Dict[str, Any]] = []
    root_nodes = manifest.get("nodes")
    if isinstance(root_nodes, list):
        nodes.extend(node for node in root_nodes if isinstance(node, dict))
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    templates = metadata.get("agent_templates") if isinstance(metadata.get("agent_templates"), dict) else {}
    template_nodes = templates.get("nodes") if isinstance(templates.get("nodes"), list) else []
    nodes.extend(node for node in template_nodes if isinstance(node, dict))
    return nodes


def event_message_summary(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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


def ensure_graph_agent(agent_by_id: Dict[str, Dict[str, Any]], agent_id: str):
    agent_by_id.setdefault(agent_id, _graph_agent_defaults(agent_id))


def _graph_agent_defaults(agent_id: str) -> Dict[str, Any]:
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


def _normalize_infrastructure_agent(agent_id: str, agent: Dict[str, Any]) -> None:
    if agent_id != "runtime":
        return
    agent["agent_type"] = "system"
    agent["type"] = "runtime"
    agent.setdefault("display_name", "System Runtime")
    agent.setdefault("role", "Runtime infrastructure")
    if agent.get("assigned_node") in (None, "", "unknown", "unassigned"):
        agent["assigned_node"] = "system/runtime"


def _copy_display_metadata(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    for key in DISPLAY_KEYS:
        if source.get(key) not in (None, "") and target.get(key) in (None, ""):
            target[key] = source[key]


def _graph_agent_label(agent_id: str, agent: Dict[str, Any]) -> str:
    for key in ("alias", "display_name", "label", "role"):
        value = agent.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() != "unknown":
            return value.strip()
    if agent_id == "runtime":
        return "System Runtime"
    return agent_id


def _graph_assigned_node(agent_id: str, agent: Dict[str, Any]) -> str:
    value = agent.get("assigned_node")
    if isinstance(value, str) and value.strip() and value.strip().lower() not in {"unknown", "unassigned"}:
        return value.strip()
    if agent_id == "runtime":
        return "system/runtime"
    return "unassigned"
