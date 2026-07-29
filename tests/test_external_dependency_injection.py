from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import grpc

from mn_api.routes import models, system


class TestExternalDependencyInjection(unittest.TestCase):
    def test_network_handshake_uses_injected_remote_client_factory(self):
        class UnavailableRpcError(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

        first_remote = SimpleNamespace(
            network_handshake=Mock(side_effect=UnavailableRpcError())
        )
        second_remote = SimpleNamespace(
            network_handshake=Mock(
                return_value={
                    "node_name": "mirror_neuron@10.0.0.42",
                    "grpc_port": 50051,
                }
            )
        )
        created_targets: list[str] = []
        remotes = iter([first_remote, second_remote])

        def fake_factory(**kwargs):
            created_targets.append(kwargs["target"])
            self.assertEqual(kwargs["auth_token"], "")
            self.assertEqual(kwargs["timeout"], 10)
            return next(remotes)

        handshake = system.network_handshake_with_fallback(
            host="10.0.0.42",
            token="join-token",
            grpc_ports=[55051, 50051],
            local_host="192.168.1.9",
            remote_client_factory=fake_factory,
        )

        self.assertEqual(handshake["node_name"], "mirror_neuron@10.0.0.42")
        self.assertEqual(created_targets, ["10.0.0.42:55051", "10.0.0.42:50051"])
        second_remote.network_handshake.assert_called_once_with(
            "join-token",
            node_name="mirror_neuron@192.168.1.9",
            node_info={
                "node_name": "mirror_neuron@192.168.1.9",
                "display_name": system.handshake_node_info("192.168.1.9")["display_name"],
                "hostname": system.handshake_node_info("192.168.1.9")["hostname"],
            },
        )

    def test_network_handshake_does_not_fallback_on_non_unavailable_errors(self):
        created_targets: list[str] = []

        def fake_factory(**kwargs):
            created_targets.append(kwargs["target"])
            return SimpleNamespace(network_handshake=Mock(side_effect=RuntimeError("bad token")))

        with self.assertRaisesRegex(RuntimeError, "bad token"):
            system.network_handshake_with_fallback(
                host="10.0.0.42",
                token="join-token",
                grpc_ports=[55051, 50051],
                local_host="192.168.1.9",
                remote_client_factory=fake_factory,
            )

        self.assertEqual(created_targets, ["10.0.0.42:55051"])

    def test_stream_chat_benchmark_uses_injected_opener_and_clock(self):
        opened_requests = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
                        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                )

        def fake_opener(request, timeout):
            opened_requests.append((request, timeout))
            return FakeResponse()

        times = iter([10.0, 10.25, 11.0])

        result = models._stream_chat_benchmark(
            api_model="docker.io/ai/gemma4:E2B",
            prompt="Ready?",
            max_tokens=32,
            opener=fake_opener,
            clock=lambda: next(times),
        )

        self.assertEqual(result["sample"], "Hello world")
        self.assertEqual(result["elapsed_ms"], 1000.0)
        self.assertEqual(result["first_token_ms"], 250.0)
        self.assertGreater(result["tokens_per_second"], 0)
        request, timeout = opened_requests[0]
        self.assertEqual(timeout, 180)
        self.assertTrue(request.full_url.endswith("/engines/v1/chat/completions"))
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "docker.io/ai/gemma4:E2B")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "Ready?"}])
        self.assertTrue(payload["stream"])

    def test_installed_model_names_uses_injected_docker_and_api_lister(self):
        calls = []

        def fake_docker(args, timeout):
            calls.append((args, timeout))
            return subprocess.CompletedProcess(args, 1, "", "docker model unavailable")

        result = models._installed_model_names(
            docker_runner=fake_docker,
            api_model_lister=lambda timeout: {"docker.io/ai/gemma4:E2B"},
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["models"], {"docker.io/ai/gemma4:E2B"})
        self.assertEqual(result["warnings"], ["docker model unavailable"])
        self.assertEqual(
            calls,
            [
                (["model", "list", "--format", "json"], 60),
                (["model", "list"], 60),
            ],
        )

if __name__ == "__main__":
    unittest.main()
