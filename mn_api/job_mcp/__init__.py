"""Job MCP protocol surface."""

from mn_api.runtime_mcp import JobContextProvider as JobContextProvider

from .context import (
    JobMCPNotFoundError as JobMCPNotFoundError,
    JobMCPUnavailableError as JobMCPUnavailableError,
    _current_job_id as _current_job_id,
    _first_text as _first_text,
    _reply_rpc as _reply_rpc,
)
from .guard import JobMCPGuard as JobMCPGuard
from .server import (
    AssistanceTask as AssistanceTask,
    create_job_mcp as create_job_mcp,
    job_mcp_lifespan as job_mcp_lifespan,
)
