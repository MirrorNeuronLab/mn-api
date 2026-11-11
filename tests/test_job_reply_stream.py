import asyncio
import json
from types import SimpleNamespace
import pytest
from mn_api.job_reply_stream import stream_job_reply


def test_reply_stream_preserves_order_and_completed_response():
    calls = []
    progress = []

    def command(*args, **kwargs):
        calls.append(kwargs)
        cursor = kwargs["control"]["cursor"]
        return {
            "schema_version": "mn.mcp.job_answer_stream.v1",
            "request_id": "known",
            "delta": "hello" if not cursor else " world",
            "cursor": 5 if not cursor else 11,
            "done": bool(cursor),
            "response": {"schema_version": "mn.mcp.job_answer.v1", "answer": "hello world"},
        }

    async def report(sequence, message):
        progress.append((sequence, json.loads(message)))

    result = asyncio.run(
        stream_job_reply(
            SimpleNamespace(response_stream_command=command),
            "job",
            "question",
            None,
            "known",
            SimpleNamespace(report_progress=report),
            assistance_task={"goal": "respond"},
        )
    )
    assert result["answer"] == "hello world" and [v[0] for v in progress] == [1, 2]
    assert [v["control"]["action"] for v in calls] == ["start", "read"]
    assert calls[0]["assistance_task"] == {"goal": "respond"}


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "old"},
        {"request_id": "different"},
        {"failed": True},
        {"cancelled": True},
        {"delta": "x" * 4001},
        {"cursor": 42},
        {"response": {}},
    ],
)
def test_invalid_or_interrupted_reply_cancels_runtime_stream(change):
    calls = []

    def command(*args, **kwargs):
        calls.append(kwargs["control"]["action"])
        return {
            "schema_version": "mn.mcp.job_answer_stream.v1",
            "request_id": "known",
            "delta": "",
            "cursor": 0,
            "done": True,
            "response": {"schema_version": "mn.mcp.job_answer.v1"},
            **change,
        }

    with pytest.raises(ValueError):
        asyncio.run(
            stream_job_reply(SimpleNamespace(response_stream_command=command), "job", "question", None, "known", None)
        )
    assert calls == ["start", "cancel"]
