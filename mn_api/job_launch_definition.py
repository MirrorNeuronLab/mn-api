"""Keep owner-local credential injection bound to the prepared Job's models."""

from fastapi import HTTPException


def _model_bindings(runtime):
    bindings = runtime.get("bindings", {})
    if not isinstance(bindings, dict):
        raise ValueError("Invalid model bindings")
    models = {}
    for step, binding in bindings.items():
        workers = binding.get("workers", [])
        if not isinstance(workers, list):
            raise ValueError("Invalid worker bindings")
        for worker in workers:
            model = worker.get("model")
            if model is None:
                continue
            identity = worker.get("id") or worker.get("alias")
            key = (step, identity)
            if not isinstance(identity, str) or not isinstance(model, str) or key in models:
                raise ValueError("Invalid model identity")
            models[key] = model
    return models


def require_prepared_launch_models(job, manifest):
    """Credential delivery is not authorization to replace model bindings.

    Core exposes the prepared step/worker/model identities in its workflow
    definition. Its public projection intentionally omits private recipe data.
    Compare the same identities from the SDK-expanded owner catalog before
    dispatch; deliberate changes go through conditional Job preparation first.
    """
    definition = job.get("workflow_definition")
    if not isinstance(definition, dict) or not isinstance(definition.get("runtime"), dict):
        raise HTTPException(409, "The prepared co-worker definition is unavailable. Prepare the co-worker again.")
    try:
        prepared = _model_bindings(definition["runtime"])
        candidate = _model_bindings(manifest.get("runtime", {}))
    except (AttributeError, TypeError, ValueError):
        prepared, candidate = None, None
    if prepared is None or prepared != candidate:
        raise HTTPException(409,
            "This computer's blueprint models differ from the prepared co-worker. "
            "Update its blueprint catalog and prepare the co-worker again.")
