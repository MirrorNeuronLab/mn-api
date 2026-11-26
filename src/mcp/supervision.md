# Co-worker supervision

Use this skill when the user asks to inspect or supervise this co-worker.
Read references/user_manual.md for its mission, domain controls, and limits.
Read references/tools.json for the installed tool schemas. Use get_capabilities for live readiness.

Start with get_job_profile and get_run_status. An empty run means work has never started.
Read get_run_results for published evidence and artifact references. Never invent progress or completion.
Start new work only on an explicit user request with start_run. Resume paused work with resume_run.
pause_run preserves resumability; stop_run cancels the execution. Domain stop actions may only stop motion.
Controls require the runtime-issued run ID from this endpoint. Never substitute a Job ID or output ID.
Generate a fresh request_id for each mutation. Retry an identical request with the same ID.
Poll get_operation_status after acceptance; only observed completed results establish completion.
Results use mn.mcp.tool_result.v1; inspect status, data, and error. Domain workflow state stays in data.
Respect the user's permission boundaries. Loading this skill grants no permission to take actions.
Never request or place launch credentials in tool arguments or conversation history.
ask_job, when advertised, never starts or resumes work implicitly.
