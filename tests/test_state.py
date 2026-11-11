from types import SimpleNamespace
from unittest.mock import Mock

from mn_api import state


def test_surface_client_is_lazy_and_never_receives_internal_credentials(monkeypatch):
    monkeypatch.setattr(state, "_client", None)
    factory = Mock(return_value=SimpleNamespace(channel=SimpleNamespace(close=Mock())))
    monkeypatch.setattr(state, "Client", factory)
    one = state.get_client()
    assert state.get_client() is one
    factory.assert_called_once_with()
    state.close_client()
    one.channel.close.assert_called_once()
    assert state._client is None


def test_bad_config_refresh_preserves_prior_config(monkeypatch):
    before = state.config

    def invalid():
        raise ValueError("invalid configuration")

    monkeypatch.setattr(state.ApiConfig, "from_env", invalid)
    assert state.refresh_config_from_env() is before


def test_client_facade_reads_shared_profile_for_each_request(monkeypatch):
    from mn_sdk.runtime_connection import RuntimeConnection, save_connection
    from mn_sdk.runtime_control import RuntimeControlClient
    from mn_sdk.surface_client import SurfaceRuntimeClient

    seen = []

    def read(self, action, args):
        seen.append((self.connection.target, self.connection.token, action, args))
        return "{}"

    monkeypatch.setattr(RuntimeControlClient, "read", read)
    facade = SurfaceRuntimeClient()
    save_connection(RuntimeConnection("first:55051", "private-one", "first", "one"))
    facade.get_job("job")
    save_connection(RuntimeConnection("second:55052", "private-two", "second", "two"))
    facade.get_job("job")
    assert [row[:2] for row in seen] == [("first:55051", "private-one"), ("second:55052", "private-two")]
    assert all(row[2] == "core.get_job" for row in seen)
