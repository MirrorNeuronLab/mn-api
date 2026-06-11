from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from mn_api import state
from mn_api.dependencies import require_auth
from mn_sdk import (
    DOCKER_MODEL_RUNNER_HOST_API_BASE,
    assess_model_compatibility,
    dmr_api_list_models,
    docker_api_model_name,
    docker_cli_path_environment,
    docker_model_match_keys,
    docker_model_name,
    load_model_catalog,
    load_model_ownership,
    merge_catalog_and_installed_models,
    model_ownership_metadata,
    resolve_model_entry,
)


router = APIRouter(prefix="/api/v1")


@router.get("/models")
def list_models(_auth=Depends(require_auth)):
    """List Docker Model Runner models installed on this runtime node."""
    catalog = load_model_catalog()
    ownership = load_model_ownership()
    docker_state = _installed_model_names()
    node = _local_node_name()

    entries = merge_catalog_and_installed_models(
        catalog=catalog,
        installed_models=docker_state["models"],
        ownership=ownership,
    )
    installed_model_keys = {key for model in docker_state["models"] for key in docker_model_match_keys(model)}
    models = [
        _model_payload(entry, installed_models=docker_state["models"], ownership=ownership, node=node)
        for entry in entries
        if docker_model_match_keys(docker_model_name(entry)) & installed_model_keys
    ]

    return {
        "models": models,
        "node": node,
        "runner_available": docker_state["available"],
        "warnings": docker_state["warnings"],
    }


@router.post("/models/{model_id:path}/benchmark")
def benchmark_model(
    model_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    _auth=Depends(require_auth),
):
    """Run a small remote benchmark through Docker Model Runner."""
    requested = (model_id or "").strip()
    if not requested:
        raise HTTPException(status_code=400, detail="model id is required")

    catalog = load_model_catalog()
    installed = _installed_model_names()["models"]
    entry = _resolve_entry_or_external(requested, catalog=catalog, installed_models=installed)
    target = docker_model_name(entry)
    installed_model_keys = {key for model in installed for key in docker_model_match_keys(model)}
    if not (docker_model_match_keys(target) & installed_model_keys):
        raise HTTPException(status_code=404, detail=f"{requested} is not installed")
    if str(entry.get("provider") or "docker_model_runner") != "docker_model_runner":
        raise HTTPException(status_code=400, detail=f"{requested} does not expose a Docker Model Runner chat endpoint")

    body = payload or {}
    prompt = str(body.get("prompt") or "Reply with one concise sentence about local model readiness.")
    max_tokens = _bounded_int(body.get("max_tokens"), default=96, minimum=16, maximum=512)
    node = _local_node_name()
    result = _stream_chat_benchmark(
        api_model=docker_api_model_name(entry),
        prompt=prompt,
        max_tokens=max_tokens,
    )
    return {
        "model": entry.get("id") or target,
        "name": entry.get("name") or entry.get("id") or target,
        "docker_model": target,
        "api_model": docker_api_model_name(entry),
        "node": node,
        **result,
    }


def _model_payload(
    entry: dict[str, Any],
    *,
    installed_models: set[str],
    ownership: dict[str, Any],
    node: str,
) -> dict[str, Any]:
    target = docker_model_name(entry)
    installed_model_keys = {key for model in installed_models for key in docker_model_match_keys(model)}
    installed = bool(docker_model_match_keys(target) & installed_model_keys)
    compatibility = None
    if str(entry.get("provider") or "docker_model_runner") == "docker_model_runner":
        try:
            compatibility = assess_model_compatibility(entry).to_dict()
        except Exception as exc:
            compatibility = {
                "status": "unknown",
                "ok": False,
                "message": str(exc),
            }

    return {
        "id": entry.get("id") or target,
        "name": entry.get("name") or entry.get("id") or target,
        "provider": entry.get("provider", "docker_model_runner"),
        "model": target,
        "docker_model": target,
        "api_model": entry.get("api_model") or target,
        "backend": entry.get("backend", "unknown"),
        "installed": installed,
        "node": node if installed else "",
        "nodes": [node] if installed else [],
        "compatibility": compatibility,
        **model_ownership_metadata(target, installed=installed, ledger=ownership),
    }


def _resolve_entry_or_external(
    model: str,
    *,
    catalog: dict[str, dict[str, Any]],
    installed_models: set[str],
) -> dict[str, Any]:
    try:
        return resolve_model_entry(model, catalog=catalog)
    except KeyError:
        pass

    for entry in merge_catalog_and_installed_models(catalog=catalog, installed_models=installed_models):
        candidates = {
            str(entry.get("id") or ""),
            str(entry.get("model") or ""),
            str(entry.get("api_model") or ""),
            str(entry.get("docker_model") or ""),
        }
        model_keys = docker_model_match_keys(model)
        candidate_keys = {key for candidate in candidates for key in docker_model_match_keys(candidate)}
        if model_keys & candidate_keys:
            return entry

    return {
        "id": model,
        "name": model,
        "provider": "docker_model_runner",
        "model": model,
        "api_model": model,
        "backend": "unknown",
        "requirements": {},
        "external": True,
    }


def _stream_chat_benchmark(
    *,
    api_model: str,
    prompt: str,
    max_tokens: int,
    opener: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    open_url = opener or urllib.request.urlopen
    now = clock or time.perf_counter
    started = now()
    first_token_at: float | None = None
    chunks: list[str] = []
    request = urllib.request.Request(
        f"{DOCKER_MODEL_RUNNER_HOST_API_BASE.rstrip('/')}/chat/completions",
        data=json.dumps(
            {
                "model": api_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": max_tokens,
                "stream": True,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with open_url(request, timeout=180) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                data = line[5:].strip() if line.startswith("data:") else line
                if data == "[DONE]":
                    break
                content = _stream_content(data)
                if not content:
                    continue
                if first_token_at is None:
                    first_token_at = now()
                chunks.append(content)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise HTTPException(
            status_code=502,
            detail=f"Docker Model Runner benchmark failed: {detail or exc.reason}",
        ) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Docker Model Runner benchmark failed: {exc}",
        ) from exc

    finished = now()
    text = "".join(chunks)
    token_count = _estimate_token_count(text)
    generation_seconds = max(finished - (first_token_at or started), 0.001)
    return {
        "elapsed_ms": round((finished - started) * 1000, 1),
        "first_token_ms": round((first_token_at - started) * 1000, 1) if first_token_at else None,
        "generated_tokens": token_count,
        "tokens_per_second": round(token_count / generation_seconds, 2) if text else 0,
        "sample": text.strip(),
        "estimated": True,
    }


def _stream_content(data: str) -> str:
    try:
        decoded = json.loads(data)
    except json.JSONDecodeError:
        return ""
    choices = decoded.get("choices") if isinstance(decoded, dict) else None
    if not isinstance(choices, list):
        return ""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            return delta["content"]
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _estimate_token_count(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, round(len(stripped) / 4))


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _local_node_name() -> str:
    try:
        summary = json.loads(state.client.get_system_summary())
    except Exception:
        return "local"
    nodes = summary.get("nodes") if isinstance(summary, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return "local"
    for node in nodes:
        if isinstance(node, dict) and node.get("self"):
            return str(node.get("name") or "local")
    for node in nodes:
        if isinstance(node, dict) and node.get("name"):
            return str(node["name"])
    return "local"


def _installed_model_names(
    *,
    docker_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    api_model_lister: Callable[..., set[str]] | None = None,
) -> dict[str, Any]:
    run_docker = docker_runner or _docker
    list_api_models = api_model_lister or dmr_api_list_models
    result = run_docker(["model", "list", "--format", "json"], timeout=60)
    if result.returncode != 0:
        result = run_docker(["model", "list"], timeout=60)
    if result.returncode == 0:
        return {
            "available": True,
            "models": _parse_model_list(result.stdout or ""),
            "warnings": [],
        }
    detail = (result.stderr or result.stdout or "").strip()
    try:
        return {
            "available": True,
            "models": list_api_models(timeout=60),
            "warnings": [detail] if detail else [],
        }
    except Exception as exc:
        return {
            "available": False,
            "models": set(),
            "warnings": [detail or str(exc) or "Docker Model Runner model list is not available."],
        }


def _parse_model_list(output: str) -> set[str]:
    names: set[str] = set()
    stripped = output.strip()
    if not stripped:
        return names
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list):
        for item in decoded:
            if isinstance(item, dict):
                names.update(_model_name_candidates(item))
            elif isinstance(item, str):
                names.add(item)
        return names
    if isinstance(decoded, dict):
        items = decoded.get("models") if isinstance(decoded.get("models"), list) else [decoded]
        for item in items:
            if isinstance(item, dict):
                names.update(_model_name_candidates(item))
        return names

    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("name", "model")):
            continue
        if line.startswith("{"):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                item = {}
            if isinstance(item, dict):
                names.update(_model_name_candidates(item))
                continue
        names.add(line.split()[0])
    return names


def _model_name_candidates(item: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key, value in item.items():
        lowered = key.lower()
        if lowered in {"name", "model", "id", "ref", "repository"} and isinstance(value, str):
            names.add(value)
        elif lowered in {"tags", "names"} and isinstance(value, list):
            names.update(str(tag) for tag in value if tag)
    return names


def _docker(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    command = ["docker", *args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=docker_cli_path_environment(),
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))
