"""Opt-in visible answer streaming; final tool response remains unchanged."""
import json
import time
import uuid
import anyio


async def stream_job_reply(provider, job_id, question, conversation_id, request_id, ctx):
    request_id = request_id or str(uuid.uuid4())
    cursor = 0
    sequence = 0
    finished = False
    deadline = time.monotonic() + 90

    async def command(action):
        return await anyio.to_thread.run_sync(lambda: provider.response_stream_command(
            job_id, question, conversation_id=conversation_id, request_id=request_id,
            control={"action": action, "cursor": cursor},
        ), abandon_on_cancel=True)

    try:
        part = await command("start")
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Response stream timed out")
            if not isinstance(part, dict) or part.get("schema_version") != "mn.mcp.job_answer_stream.v1":
                raise ValueError("Unsupported response stream")
            if part.get("request_id") != request_id or part.get("failed") or part.get("cancelled"):
                raise ValueError("Response stream interrupted")
            delta = part.get("delta")
            next_cursor = part.get("cursor")
            if (not isinstance(delta, str) or len(delta.encode("utf-8")) > 4000
                    or not isinstance(next_cursor, int) or next_cursor != cursor + len(delta)
                    or next_cursor > 12000):
                raise ValueError("Invalid response stream event")
            if delta:
                sequence += 1
                await ctx.report_progress(sequence, message=json.dumps({
                    "schema_version": "mn.mcp.job_answer_delta.v1",
                    "request_id": request_id, "sequence": sequence, "delta": delta,
                }, separators=(",", ":")))
            cursor = next_cursor
            if part.get("done"):
                result = part.get("response")
                if not isinstance(result, dict) or result.get("schema_version") != "mn.mcp.job_answer.v1":
                    raise ValueError("Invalid completed response")
                finished = True
                return result
            await anyio.sleep(0.05)
            part = await command("read")
    finally:
        if not finished:
            with anyio.move_on_after(2, shield=True):
                await command("cancel")
