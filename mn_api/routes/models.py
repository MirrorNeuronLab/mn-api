from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from fastapi import Body, Depends, HTTPException
from mn_sdk import (
    DOCKER_MODEL_RUNNER_HOST_API_BASE,
    add_registered_models,
    default_model_registry_path,
    dmr_api_list_models,
    docker_api_model_name,
    docker_cli_path_environment,
    docker_model_match_keys,
    docker_model_name,
    doctor_runtime_model,
    get_registered_model,
    install_runtime_model,
    list_runtime_models,
    load_model_catalog,
    merge_catalog_and_installed_models,
    model_name_candidates as sdk_model_name_candidates,
    provider_registration,
    registered_model_records,
    remove_litellm_gateway_route,
    remove_registered_model,
    remove_runtime_model,
    replace_registered_model,
    resolve_model_entry,
    show_runtime_model,
    sync_litellm_gateway,
    update_runtime_model,
)

from mn_api import state
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.schemas import (
    ModelInstallRequest,
    ModelProxyRequest,
    ModelRemoteRequest,
    ModelRemoveRequest,
    ModelUpdateRequest,
)


def list_models(installed_only: bool = True, _auth=Depends(require_auth)):
    """List runtime models using the same SDK service as the CLI."""
    try:
        return list_runtime_models(installed_only=installed_only)
    except Exception as exc:
        return handle_grpc_error(exc)


def list_model_catalog(_auth=Depends(require_auth)):
    try:
        return list_runtime_models(installed_only=False)
    except Exception as exc:
        return handle_grpc_error(exc)


def list_remote_models(_auth=Depends(require_auth)):
    try:
        remotes = [
            _remote_projection(record) for record in registered_model_records() if record.get("source") == "rest_remote"
        ]
        return {"remotes": remotes, "path": str(default_model_registry_path())}
    except Exception as exc:
        return handle_grpc_error(exc)


def add_remote_model(req: ModelRemoteRequest, _auth=Depends(require_auth)):
    try:
        remote_name = req.name or str(req.model).replace("/", "-").replace(":", "-")
        record = provider_registration(
            remote_name,
            source_model=req.api_model or req.model,
            api_model=req.api_model or req.model,
            api_base=req.base_url,
            api_key=req.api_key,
            selected_node=req.node,
            source="rest_remote",
        )
        registered = _upsert_registry_record(record)
        remote = _remote_projection(registered)
        gateway = sync_litellm_gateway(restart=True) if req.sync_gateway else None
        return {"remote": remote, "path": str(default_model_registry_path()), "gateway": gateway}
    except Exception as exc:
        return handle_grpc_error(exc)


def delete_remote_model(name: str, sync_gateway: bool = False, _auth=Depends(require_auth)):
    try:
        removed, _path = remove_registered_model(name)
        removed = _remote_projection(removed) if removed else None
        remove_litellm_gateway_route(name)
        if removed:
            remove_litellm_gateway_route(str(removed.get("model") or ""))
        gateway = sync_litellm_gateway(restart=True) if sync_gateway else None
        return {"removed": removed, "path": str(default_model_registry_path()), "gateway": gateway}
    except Exception as exc:
        return handle_grpc_error(exc)


def add_proxy_model(req: ModelProxyRequest, _auth=Depends(require_auth)):
    try:
        record = provider_registration(
            req.model_id,
            source_model=req.source_model,
            api_base=req.base_url,
            api_model=req.api_model,
            name=req.display_name,
            api_key=req.api_key,
            source="rest_proxy",
        )
        record["rest_options"] = _proxy_rest_options(req)
        proxy = _proxy_projection(_upsert_registry_record(record))
        gateway = sync_litellm_gateway(restart=True) if req.sync_gateway else None
        return {"proxy": proxy, "path": str(default_model_registry_path()), "gateway": gateway}
    except Exception as exc:
        return handle_grpc_error(exc)


def doctor_model(model_id: str, _auth=Depends(require_auth)):
    try:
        return doctor_runtime_model(model_id)
    except Exception as exc:
        return handle_grpc_error(exc)


def show_model(
    model_id: str,
    compatibility: bool = False,
    _auth=Depends(require_auth),
):
    try:
        return show_runtime_model(model_id, compatibility=compatibility)
    except Exception as exc:
        return handle_grpc_error(exc)


def install_model(model_id: str, req: ModelInstallRequest | None = None, _auth=Depends(require_auth)):
    request = req or ModelInstallRequest()
    try:
        result = install_runtime_model(
            model_id,
            backend=request.backend,
            context_size=request.context_size,
            force=request.force,
        )
        return {"status": "running", **result}
    except Exception as exc:
        return handle_grpc_error(exc)


def update_model(model_id: str, req: ModelUpdateRequest | None = None, _auth=Depends(require_auth)):
    request = req or ModelUpdateRequest()
    try:
        return update_runtime_model(model_id, all_models=request.all, force=request.force)
    except Exception as exc:
        return handle_grpc_error(exc)


def update_models(req: ModelUpdateRequest | None = None, _auth=Depends(require_auth)):
    request = req or ModelUpdateRequest(all=True)
    try:
        return update_runtime_model(None, all_models=request.all or True, force=request.force)
    except Exception as exc:
        return handle_grpc_error(exc)


def remove_model(model_id: str, force: bool = False, _auth=Depends(require_auth)):
    try:
        return remove_runtime_model(model_id, force=force)
    except Exception as exc:
        return handle_grpc_error(exc)


def remove_model_post(model_id: str, req: ModelRemoveRequest | None = None, _auth=Depends(require_auth)):
    request = req or ModelRemoveRequest()
    try:
        return remove_runtime_model(model_id, force=request.force)
    except Exception as exc:
        return handle_grpc_error(exc)


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


def _resolve_entry_or_external(
    model: str,
    *,
    catalog: dict[str, dict[str, Any]],
    installed_models: set[str],
) -> dict[str, Any]:
    installed_model_keys = {key for model in installed_models for key in docker_model_match_keys(model)}
    requested_keys = docker_model_match_keys(model)

    def entry_lookup_keys(entry: dict[str, Any]) -> set[str]:
        candidates = {
            str(entry.get("id") or ""),
            str(entry.get("model") or ""),
            str(entry.get("dmr_model") or ""),
            str(entry.get("api_model") or ""),
            str(entry.get("docker_model") or ""),
            *[str(alias) for alias in entry.get("aliases") or []],
        }
        return {key for candidate in candidates for key in docker_model_match_keys(candidate)}

    try:
        entry = resolve_model_entry(model, catalog=catalog)
        if not (docker_model_match_keys(docker_model_name(entry)) & installed_model_keys):
            for installed_entry in merge_catalog_and_installed_models(
                catalog=catalog, installed_models=installed_models
            ):
                if not (docker_model_match_keys(docker_model_name(installed_entry)) & installed_model_keys):
                    continue
                if requested_keys & entry_lookup_keys(installed_entry):
                    return installed_entry
        return entry
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


def _upsert_registry_record(record: dict[str, Any]) -> dict[str, Any]:
    existing = get_registered_model(str(record.get("id") or ""))
    if existing is None:
        added, _path = add_registered_models([record])
        return added[0]
    record["created_at"] = existing.get("created_at")
    updated, _path = replace_registered_model(record)
    return updated


def _remote_projection(record: dict[str, Any]) -> dict[str, Any]:
    params = (record.get("litellm_entry") or {}).get("litellm_params") or {}
    return {
        "name": str(record.get("id") or ""),
        "provider": "docker_model_runner",
        "model": str(record.get("model") or record.get("api_model") or ""),
        "api_model": str(record.get("api_model") or record.get("model") or ""),
        "base_url": str(record.get("api_base") or params.get("api_base") or ""),
        "api_key": str(params.get("api_key") or "not-needed"),
        **({"node": str(record.get("selected_node"))} if record.get("selected_node") else {}),
    }


def _proxy_projection(record: dict[str, Any]) -> dict[str, Any]:
    params = (record.get("litellm_entry") or {}).get("litellm_params") or {}
    options = record.get("rest_options") if isinstance(record.get("rest_options"), dict) else {}
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or record.get("id") or ""),
        "provider": "openai-compatible",
        "source_model": str(record.get("model") or ""),
        "api_model": str(record.get("api_model") or record.get("id") or ""),
        "base_url": str(record.get("api_base") or params.get("api_base") or ""),
        "api_key": str(params.get("api_key") or "not-needed"),
        "backend": "proxy",
        **options,
    }


def _proxy_rest_options(req: Any) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "config_path": str(getattr(req, "config_path", "") or ""),
            "litellm_config_path": str(getattr(req, "litellm_config_path", "") or ""),
            "container_name": str(getattr(req, "container_name", "") or ""),
            "image": str(getattr(req, "image", "") or ""),
            "port": getattr(req, "port", None),
            "host": str(getattr(req, "host", "") or ""),
        }.items()
        if value not in {"", None}
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
    return sdk_model_name_candidates(item)


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
