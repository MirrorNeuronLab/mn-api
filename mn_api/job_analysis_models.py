"""Public job performance response. No prompts, outputs or credentials."""
from pydantic import BaseModel, Field
from typing import Literal

class AnalysisCoverage(BaseModel):
    measured_runs: int = Field(ge=0)
    total_runs: int = Field(ge=0)
    complete: bool

class AnalysisTokens(AnalysisCoverage):
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    estimated_tokens: int | None = Field(ge=0)

class AnalysisTime(AnalysisCoverage):
    total_ms: int | None = Field(ge=0)

class AnalysisRuns(BaseModel):
    total: int = Field(ge=0)
    successful: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    running: int = Field(ge=0)
    paused: int = Field(ge=0)
    other: int = Field(ge=0)
    success_rate: float | None = Field(ge=0, le=1)

class JobAnalysis(BaseModel):
    job_id: str
    snapshot_at: str
    scope: Literal["recorded_history"]
    runs: AnalysisRuns
    running_time: AnalysisTime
    tokens: AnalysisTokens
    history_complete: bool
