import asyncio
from types import SimpleNamespace
import pytest
from mn_api.job_reply_stream import stream_job_reply


def run_parts(parts, ctx=None):
    actions = []
    def command(job, question, **kwargs):
        action = kwargs["control"]["action"]
        actions.append(action)
        if action == "cancel":
            return {}
        return parts.pop(0)
    async def report(*args, **kwargs):
        pass
    async def run():
        return await stream_job_reply(SimpleNamespace(response_stream_command=command), "job", "question", None, "r", ctx or SimpleNamespace(report_progress=report))
    return run, actions


def part(**overrides):
    return {"schema_version": "mn.mcp.job_answer_stream.v1", "request_id": "r",
            "delta": "", "cursor": 0, "done": False, **overrides}


@pytest.mark.parametrize("bad", [
    {}, part(request_id="other"), part(failed=True), part(cancelled=True),
    part(cursor=-1), part(delta="x", cursor=0), part(delta="x" * 4001, cursor=4001),
    part(done=True, response={"schema_version": "wrong"}),
])
def test_invalid_stream_is_cancelled_without_returning_an_answer(bad):
    run, actions = run_parts([bad])
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert actions[-1] == "cancel"


def test_empty_stream_replay_returns_final_result_without_duplicate_deltas():
    response = {"schema_version": "mn.mcp.job_answer.v1", "answer": "Already answered"}
    async def unexpected(*args, **kwargs):
        pytest.fail("Replays must not emit duplicate answer deltas")
    run, actions = run_parts([part(done=True, response=response)], SimpleNamespace(report_progress=unexpected))
    assert asyncio.run(run()) == response
    assert actions == ["start"]


def test_stream_deadline_cancels_runtime(monkeypatch):
    clock = iter([0, 91])
    # Keep the event loop's shared time module clock intact.
    import mn_api.job_reply_stream as module
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock, 91)))
    run, actions = run_parts([part()])
    with pytest.raises(TimeoutError):
        asyncio.run(run())
    assert actions[-1] == "cancel"
