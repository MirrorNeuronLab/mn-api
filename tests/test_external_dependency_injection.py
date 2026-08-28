from __future__ import annotations

import json
import subprocess
import unittest

from mn_api.routes import models


class TestExternalDependencyInjection(unittest.TestCase):
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
