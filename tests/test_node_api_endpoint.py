import pytest
from fastapi import HTTPException

from mn_api.job_ui_routing import node_api_base_url


def node(**endpoint):
    return {"address": "10.0.4.32", "hardware": {"http_api": {
        "schema_version": "mn.http_api.endpoint.v1", "enabled": True, "scheme": "http", "port": 54007,
        **endpoint,
    }}}


@pytest.mark.parametrize("host,authority", [("10.0.4.32", "10.0.4.32"), ("spark.local", "spark.local"), ("::1", "[::1]")])
def test_registered_peer_api_endpoint_uses_its_own_port(host, authority):
    value = node(scheme="https")
    value["address"] = host
    assert node_api_base_url(value) == f"https://{authority}:54007/api/v1"


@pytest.mark.parametrize("change", [
    {"enabled": False}, {"scheme": "file"}, {"schema_version": "other"},
    {"port": True}, {"port": "54007"}, {"port": 0}, {"port": 65536},
])
def test_invalid_peer_api_advertisement_fails_closed(change):
    with pytest.raises(HTTPException) as error:
        node_api_base_url(node(**change))
    assert error.value.status_code == 503


@pytest.mark.parametrize("value", [None, {}, {"address": "10.0.4.32"}, {"address": "10.0.4.32", "hardware": []}])
def test_missing_peer_endpoint_is_not_replaced_with_the_callers_port(value):
    with pytest.raises(HTTPException) as error:
        node_api_base_url(value)
    assert error.value.status_code == 503


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "user@host", "host/path", "host?query", "[invalid"])
def test_endpoint_never_takes_an_untrusted_host_or_url(host):
    value = node()
    value["address"] = host
    with pytest.raises(HTTPException) as error:
        node_api_base_url(value)
    assert error.value.status_code == 503
