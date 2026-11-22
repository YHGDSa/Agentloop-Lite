"""HTTP routes: tool catalogue, run lifecycle, and the approval endpoints.

The run endpoints are async because the executor awaits the planner and the
tools; the connection comes from the same per-request dependency the rest of
the app uses.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from fastapi import APIRouter, Depends, HTTPException

from .config import settings
from .db import get_db
from .errors import (
    InvalidTransition,
    PlanningError,
    RunNotFound,
    ValidationError,
)
from .executor import approve_run, cancel_run, execute_run, expire_approval
from .registry import TOOLS
from .schemas import (
    ApprovalDecision,
    ApprovalTrace,
    RunCreate,
    RunResponse,
    StepTrace,
    ToolCallTrace,
    ToolInfo,
    TraceResponse,
)
from .state_machine import RunStatus, is_expired, transition, utcnow

router = APIRouter()


@router.get("/tools", response_model=list[ToolInfo], tags=["tools"])
def list_tools() -> list[ToolInfo]:
    """Describe the callable tools, in registration order.

    ``fn`` stays in the process: the model needs the contract, not the callable.
    """
    return [
        ToolInfo(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            risk=spec.risk,
            idempotent=spec.idempotent,
            timeout_s=spec.timeout_s,
        )
        for spec in TOOLS.values()
    ]


@router.post("/runs", response_model=RunResponse, status_code=201, tags=["runs"])
async def create_run(
    body: RunCreate, conn: sqlite3.Connection = Depends(get_db)
) -> RunResponse:
    """Create a run and execute it synchronously until it stops."""
    run_id = uuid.uuid4().hex
    now = utcnow()

    try:
        with conn:
            conn.execute(
                "INSERT INTO runs (id, goal, status, step_count, max_steps, "
                "created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?)",
                (
                    run_id,
                    body.goal,
                    RunStatus.CREATED.value,
                    settings.max_steps,
                    now,
                    now,
                ),
            )
            transition(conn, run_id, RunStatus.RUNNING)
        await execute_run(conn, run_id)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PlanningError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _to_response(row)


@router.get("/runs/{run_id}", response_model=RunResponse, tags=["runs"])
def get_run(
    run_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> RunResponse:
    """Return one run, or 404 when the id was never issued."""
    row = _load_run_with_expiry(conn, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return _to_response(row)


@router.get("/runs/{run_id}/trace", response_model=TraceResponse, tags=["runs"])
def get_trace(
    run_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> TraceResponse:
    """Read-only view: the run, its steps with every attempt, and decisions.

    It also applies the lazy expiry, so reading a trace of a stale
    WAITING_APPROVAL run shows CANCELLED and its timeout record.
    """
    run = _load_run_with_expiry(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    steps: list[StepTrace] = []
    for step in conn.execute(
        "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall():
        calls = conn.execute(
            "SELECT * FROM tool_calls WHERE step_id = ? ORDER BY attempt",
            (step["id"],),
        ).fetchall()
        steps.append(
            StepTrace(
                seq=step["seq"],
                thought=step["thought"],
                tool_name=step["tool_name"],
                args=_parse_args(step["args_json"]),
                observation=step["observation"],
                error_type=step["error_type"],
                latency_ms=step["latency_ms"],
                created_at=step["created_at"],
                tool_calls=[
                    ToolCallTrace(
                        attempt=call["attempt"],
                        status=call["status"],
                        error_type=call["error_type"],
                        observation=call["observation"],
                        created_at=call["created_at"],
                    )
                    for call in calls
                ],
            )
        )

    approvals = [
        ApprovalTrace(
            decision=row["decision"],
            note=row["note"],
            step_id=row["step_id"],
            created_at=row["created_at"],
        )
        for row in conn.execute(
            "SELECT * FROM approvals WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    ]

    return TraceResponse(run=_to_response(run), steps=steps, approvals=approvals)


@router.post("/runs/{run_id}/approve", response_model=RunResponse, tags=["runs"])
async def approve(
    run_id: str,
    body: ApprovalDecision,
    conn: sqlite3.Connection = Depends(get_db),
) -> RunResponse:
    """Approve (resume and execute) or reject (cancel) a waiting run."""
    try:
        await approve_run(conn, run_id, body.decision, body.note)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PlanningError as exc:
        # Resuming can hit an unconfigured/broken planner too.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    row = _load_run(conn, run_id)
    return _to_response(row)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse, tags=["runs"])
def cancel(
    run_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> RunResponse:
    """Cancel a run that has not reached a terminal status yet."""
    try:
        cancel_run(conn, run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    row = _load_run(conn, run_id)
    return _to_response(row)


def _load_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def _parse_args(raw: str | None) -> dict:
    """Best-effort JSON object for a stored args_json; never raise inside a GET."""
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_run_with_expiry(
    conn: sqlite3.Connection, run_id: str
) -> sqlite3.Row | None:
    """Load a run, lazily turning an expired approval into CANCELLED first.

    Both GET /runs/{id} and GET /runs/{id}/trace read through here, so a run
    cannot look "still waiting" just because nobody polled the approve endpoint.
    """
    row = _load_run(conn, run_id)
    if row is None:
        return None

    if row["status"] == RunStatus.WAITING_APPROVAL.value and is_expired(
        row["approval_expires_at"]
    ):
        try:
            with conn:
                expire_approval(conn, row)
        except InvalidTransition:
            pass  # another request approved, rejected or expired it first
        row = _load_run(conn, run_id)
    return row


def _to_response(row: sqlite3.Row) -> RunResponse:
    return RunResponse(
        id=row["id"],
        goal=row["goal"],
        status=row["status"],
        step_count=row["step_count"],
        result=row["result"],
        error=row["error"],
        approval_expires_at=row["approval_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
