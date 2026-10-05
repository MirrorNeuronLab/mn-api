"""API-owned background model installation through SDK preparation boundaries."""

from __future__ import annotations

from typing import Any

from mn_sdk import (
    add_registered_models,
    assess_model_compatibility,
    dmr_registration,
    docker_model_name,
    docker_model_runner_endpoint,
    get_registered_model,
    resolve_model_entry,
    sync_litellm_gateway,
)
from mn_sdk.model_catalog import select_default_model_entry
from mn_sdk.model_service import install_runtime_model, is_proxy_model_entry

from mn_api.blueprints import (
    install_runtime_cluster_model_for_api,
    resolve_runtime_cluster_model_for_api,
    sync_runtime_model_gateways_for_api,
)


def install_model(model_id: str, options: dict[str, Any], progress) -> dict[str, Any]:
    backend = options.get("backend", "auto")
    context_size = options.get("context_size")
    force = options.get("force", False)
    placements: dict[str, dict[str, Any] | None] = {}

    def placement(entry: dict[str, Any]) -> dict[str, Any] | None:
        key = str(entry["id"])
        if key not in placements:
            # Selection walks the shared default chain; placement evaluates
            # this exact candidate instead of substituting its fallback.
            placements[key] = resolve_runtime_cluster_model_for_api(
                requirement={"model": key, "required": True},
                entry={**entry, "fallback_model": ""},
            )
        return placements[key]

    def compatible(entry: dict[str, Any]) -> bool:
        return (
            is_proxy_model_entry(entry)
            or bool(placement(entry))
            or assess_model_compatibility(entry, backend=backend, force=force).ok
        )

    entry = (
        select_default_model_entry(is_compatible=compatible)
        if model_id.strip().lower() == "default"
        else resolve_model_entry(model_id)
    )
    model = str(entry["id"])
    progress(stage="model_install", label="Preparing model", detail=f"Preparing {model}")
    if is_proxy_model_entry(entry):
        if backend != "auto" or context_size is not None or force:
            raise ValueError("DMR backend, context, and force options cannot be used with a provider model")
        gateway = sync_litellm_gateway(restart=True)
        return {"status": "ready", "model": model, "reused": True, "gateway": gateway}

    cluster = placement(entry)
    if cluster:
        prepared = install_runtime_cluster_model_for_api(
            requirement={"model": model, "required": True},
            entry=entry,
            model={"id": model, "model": docker_model_name(entry)},
            cluster=cluster,
            backend=backend,
            context_size=context_size,
            force=force,
        )
        endpoint = prepared["endpoint"]
        result = prepared["install"]
        node = str(cluster["node"])
    else:
        result = install_runtime_model(model, backend=backend, context_size=context_size, force=force)
        node = "local"
        endpoint = docker_model_runner_endpoint(entry, node=node, source="local-dmr")

    endpoints = sync_runtime_model_gateways_for_api({"endpoints": {model: endpoint}})
    if get_registered_model(model) is None:
        add_registered_models([dmr_registration(entry, selected_node=node)])
    return {"status": "ready", "model": model, "node": node, "install": result, "endpoints": endpoints}
