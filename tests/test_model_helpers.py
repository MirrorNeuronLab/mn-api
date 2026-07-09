from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from mn_api import state
from mn_api.routes import models


def test_parse_model_list_accepts_json_and_text_formats():
    assert models._parse_model_list('[{"name": "ai/gemma4:e2b", "tags": ["latest"]}]') == {"ai/gemma4:e2b", "latest"}
    assert models._parse_model_list('{"models": [{"model": "ai/qwen3"}]}') == {"ai/qwen3"}
    assert models._parse_model_list('NAME ID\nai/small abc\n') == {"ai/small"}
    assert models._parse_model_list('{"name": "single"}') == {"single"}
    assert models._parse_model_list("") == set()


def test_installed_model_names_uses_docker_json_then_api_fallback():
    def docker_success(args, timeout=60):
        return subprocess.CompletedProcess(args, 0, '[{"name": "ai/gemma4"}]', "")

    assert models._installed_model_names(docker_runner=docker_success)["models"] == {"ai/gemma4"}

    def docker_failure(args, timeout=60):
        return subprocess.CompletedProcess(args, 127, "", "docker missing")

    def api_success(timeout=60):
        return {"api/model"}

    fallback = models._installed_model_names(docker_runner=docker_failure, api_model_lister=api_success)
    assert fallback["available"] is True
    assert fallback["models"] == {"api/model"}
    assert fallback["warnings"] == ["docker missing"]

    def api_failure(timeout=60):
        raise RuntimeError("api down")

    unavailable = models._installed_model_names(docker_runner=docker_failure, api_model_lister=api_failure)
    assert unavailable["available"] is False
    assert unavailable["models"] == set()
    assert unavailable["warnings"] == ["docker missing"]


def test_stream_content_and_token_estimate_helpers():
    assert models._stream_content(json.dumps({"choices": [{"delta": {"content": "hi"}}]})) == "hi"
    assert models._stream_content(json.dumps({"choices": [{"message": {"content": "done"}}]})) == "done"
    assert models._stream_content("{bad") == ""
    assert models._stream_content(json.dumps({"choices": "bad"})) == ""
    assert models._estimate_token_count("") == 0
    assert models._estimate_token_count("hello world") >= 1


def test_model_payload_handles_proxy_and_compatibility_failure(monkeypatch):
    monkeypatch.setattr(
        "mn_api.routes.models.assess_model_compatibility",
        lambda entry: (_ for _ in ()).throw(RuntimeError("no gpu")),
    )
    monkeypatch.setattr(
        "mn_api.routes.models.model_ownership_metadata",
        lambda target, installed=False, ledger=None: {"ownership": "manual"},
    )

    docker_payload = models._model_payload(
        {"id": "gemma", "model": "ai/gemma4", "provider": "docker_model_runner"},
        installed_models={"ai/gemma4"},
        ownership={"models": {}},
        node="local",
    )
    proxy_payload = models._model_payload(
        {"id": "proxy", "model": "openai/gpt-4.1", "provider": "litellm_proxy", "proxy": {"route": "x"}},
        installed_models=set(),
        ownership={},
        node="local",
    )

    assert docker_payload["compatibility"]["status"] == "unknown"
    assert docker_payload["ownership"] == "manual"
    assert proxy_payload["installed"] is True
    assert proxy_payload["status"] == "proxy"


def test_local_node_name_prefers_self_then_named_node(monkeypatch):
    monkeypatch.setattr(
        state,
        "client",
        SimpleNamespace(get_system_summary=lambda: json.dumps({"nodes": [{"name": "n1"}, {"name": "self", "self": True}]})),
    )
    assert models._local_node_name() == "self"

    monkeypatch.setattr(state, "client", SimpleNamespace(get_system_summary=lambda: json.dumps({"nodes": [{"name": "n1"}]})))
    assert models._local_node_name() == "n1"

    monkeypatch.setattr(state, "client", SimpleNamespace(get_system_summary=lambda: (_ for _ in ()).throw(RuntimeError("down"))))
    assert models._local_node_name() == "local"


def test_resolve_entry_or_external_matches_installed_alias(monkeypatch):
    monkeypatch.setattr("mn_api.routes.models.resolve_model_entry", lambda model, catalog=None: (_ for _ in ()).throw(KeyError(model)))
    monkeypatch.setattr(
        "mn_api.routes.models.merge_catalog_and_installed_models",
        lambda catalog=None, installed_models=None: [{"id": "gemma", "model": "ai/gemma4:e2b", "api_model": "gemma"}],
    )

    resolved = models._resolve_entry_or_external("gemma", catalog={}, installed_models={"ai/gemma4:e2b"})
    external = models._resolve_entry_or_external("unknown", catalog={}, installed_models=set())

    assert resolved["id"] == "gemma"
    assert external["external"] is True


def test_resolve_entry_or_external_uses_internal_normalization_for_aliases():
    catalog = {
        "hf.co/homerquan/mn-context-engine-model-v-Q4_K_M": {
            "id": "hf.co/homerquan/mn-context-engine-model-v-Q4_K_M",
            "model": "huggingface.co/homerquan/mn-context-engine-model-v-q4_k_m:latest",
            "aliases": ["huggingface.co/homerquan/mn-context-engine-model-v-q4_k_m"],
            "requirements": {},
        }
    }
    installed_models = {"huggingface.co/homerquan/mn-context-engine-model-v-q4_k_m"}

    resolved = models._resolve_entry_or_external(
        "huggingface.co/homerquan/mn-context-engine-model-v-q4_k_m",
        catalog=catalog,
        installed_models=installed_models,
    )

    assert resolved["model"] == "huggingface.co/homerquan/mn-context-engine-model-v-q4_k_m:latest"


def test_resolve_entry_or_external_uses_internal_normalization_for_aliases_with_latest():
    catalog = {
        "hf.co/homerquan/mn-context-engine-model-v-Q4_K_M": {
            "id": "hf.co/homerquan/mn-context-engine-model-v-Q4_K_M",
            "model": "huggingface.co/homerquan/mn-context-engine-model-v-q4_k_m:latest",
            "requirements": {},
        }
    }
    installed_models = {"huggingface.co/homerquan/mn-context-engine-model-v-q4_k_m"}

    resolved = models._resolve_entry_or_external(
        "huggingface.co/homerquan/mn-context-engine-model-v-q4_k_m:latest",
        catalog=catalog,
        installed_models=installed_models,
    )

    assert resolved["model"] == "huggingface.co/homerquan/mn-context-engine-model-v-q4_k_m:latest"


def test_resolve_entry_or_external_normalizes_installed_hf_domain_alias():
    catalog = {
        "huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval:Q4_K_M": {
            "id": "huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval:Q4_K_M",
            "model": "huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval:Q4_K_M",
            "provider": "docker_model_runner",
        }
    }
    installed_models = {"hf.co/jinaai/jina-embeddings-v5-text-small-retrieval:Q4_K_M"}

    resolved = models._resolve_entry_or_external(
        "huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval:Q4_K_M",
        catalog=catalog,
        installed_models=installed_models,
    )

    assert resolved["model"] == "huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval:Q4_K_M"
