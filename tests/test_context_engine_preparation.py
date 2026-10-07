"""Shared v2 readiness failures must stop HTTP blueprint preparation."""

import sys
from types import ModuleType
from unittest.mock import Mock

from mn_api import blueprints


def install_launcher(monkeypatch, launcher):
    package = ModuleType("mn_cli")
    package.__path__ = []
    module = ModuleType("mn_cli.server_cmds")
    module.ensure_context_engine_runtime = launcher
    monkeypatch.setitem(sys.modules, "mn_cli", package)
    monkeypatch.setitem(sys.modules, "mn_cli.server_cmds", module)
    monkeypatch.setattr(blueprints, "runtime_process_environment", lambda: {})


def test_protocol_failure_is_failed_preparation_without_a_subprocess_retry(monkeypatch, tmp_path):
    failure = "Context Engine does not implement mirrorneuron.context.v2. Install a matching image."
    launcher = Mock(side_effect=RuntimeError(failure))
    install_launcher(monkeypatch, launcher)
    command = Mock()
    monkeypatch.setattr(blueprints.subprocess, "run", command)

    result = blueprints.ensure_context_engine_for_blueprint(tmp_path)

    assert result == {"name": "membrane-context-engine", "status": "failed", "error": failure}
    launcher.assert_called_once_with(force=False, environment={})
    command.assert_not_called()


def test_api_preparation_retains_the_verified_v2_protocol(monkeypatch, tmp_path):
    launcher = Mock(return_value={"status": "ready", "protocol": "mirrorneuron.context.v2"})
    install_launcher(monkeypatch, launcher)

    result = blueprints.ensure_context_engine_for_blueprint(tmp_path)

    assert result["status"] == "ready"
    assert result["protocol"] == "mirrorneuron.context.v2"
