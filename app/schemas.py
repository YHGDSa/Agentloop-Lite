"""Pydantic models for the HTTP contract.

They describe the wire shape only; the duplicate validation rules live in the
tool registry's JSON Schemas, which are the source of truth for tool calls.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness payload: the process can answer HTTP."""

    ok: bool = True


class RunCreate(BaseModel):
    """Body of POST /runs: the user goal, 1-500 characters."""

    goal: str = Field(min_length=1, max_length=500)


class ToolInfo(BaseModel):
    """Public description of a tool; the callable is never serialised."""

    name: str
    description: str
    input_schema: dict[str, Any]
    risk: str
    idempotent: bool
    timeout_s: float


class ApprovalDecision(BaseModel):
    """Body of POST /runs/{id}/approve: answer a WAITING_APPROVAL run."""

    decision: Literal["approve", "reject"]
    note: str | None = None


class RunResponse(BaseModel):
    """Wire shape of one run: what POST /runs returns and GET /runs/{id} repeats."""

    id: str
    goal: str
    status: str
    step_count: int
    result: str | None = None
    error: str | None = None
    approval_expires_at: str | None = None
    created_at: str
    updated_at: str


class ToolCallTrace(BaseModel):
    """One attempt of one tool call, as shown in the trace."""

    attempt: int
    status: str
    error_type: str | None = None
    observation: str | None = None
    created_at: str


class StepTrace(BaseModel):
    """One planner iteration and the tool attempts it produced."""

    seq: int
    thought: str | None = None
    tool_name: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    observation: str | None = None
    error_type: str | None = None
    latency_ms: int | None = None
    created_at: str
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)


class ApprovalTrace(BaseModel):
    """One human (or timeout) decision recorded against a run."""

    decision: str
    note: str | None = None
    step_id: int | None = None
    created_at: str


class TraceResponse(BaseModel):
    """Read-only view of a run: status, steps with attempts, and decisions."""

    run: RunResponse
    steps: list[StepTrace] = Field(default_factory=list)
    approvals: list[ApprovalTrace] = Field(default_factory=list)
