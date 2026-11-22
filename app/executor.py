"""The loop: plan one step, validate it, gate high-risk tools, run what is safe.

A state move and the rows that justify it (step, tool_call attempts, approval)
share one transaction (``with conn:``), so a crash cannot leave a run SUCCEEDED
without its evidence, nor a FAILED run without the attempt that failed it.
``transition()`` never commits; this module owns the transaction boundary.

T4 added the error taxonomy, timeouts, bounded retries for idempotent tools,
idempotency keys, loop detection and the tool_calls audit trail. T5 adds the
approval gate (pending step + WAITING_APPROVAL), approve/reject/cancel, and the
lazy approval timeout. Traces are still absent (T6).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Sequence
from typing import Any

from . import planner, registry
from .config import settings
from .errors import (
    InvalidTransition,
    PermissionDenied,
    PlanningError,
    RateLimited,
    RunNotFound,
    ToolTimeout,
    UpstreamError,
    ValidationError,
)
from .registry import TOOLS
from .state_machine import (
    TERMINAL_STATUSES,
    RunStatus,
    is_expired,
    transition,
    utc_after,
    utcnow,
)

INSERT_STEP = """
INSERT INTO steps (run_id, seq, thought, tool_name, args_json, observation,
                   error_type, latency_ms, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_TOOL_CALL = """
INSERT INTO tool_calls (step_id, idempotency_key, attempt, status,
                        error_type, observation, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

COUNT_STEP = "UPDATE runs SET step_count = step_count + 1, updated_at = ? WHERE id = ?"

INSERT_APPROVAL = """
INSERT INTO approvals (run_id, step_id, decision, note, created_at)
VALUES (?, ?, ?, ?, ?)
"""

# The one step an approval is about: highest seq still waiting for an answer.
PENDING_STEP_SQL = """
SELECT * FROM steps
WHERE run_id = ? AND observation IS NULL AND tool_name IS NOT NULL
ORDER BY seq DESC LIMIT 1
"""

# The nth retry waits BACKOFF_SECONDS[n - 1]; the last value is reused if
# MAX_ATTEMPTS ever grows past three.
BACKOFF_SECONDS = (0.2, 0.4)

# Only these say "try again"; PermissionDenied and validation never do.
RETRYABLE_ERRORS: tuple[type[Exception], ...] = (ToolTimeout, RateLimited, UpstreamError)

# Eval-only switch, default OFF: True removes retries, the approval gate and
# loop detection so eval/run_eval.py can measure the delta the runtime adds.
# Never flip this in production code paths; run_eval sets and restores it.
BASELINE = False


def idempotency_key(run_id: str, seq: int, tool: str, args: dict[str, Any]) -> str:
    """Stable key for one logical tool call: run + step + tool + canonical args."""
    payload = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{run_id}:{seq}:{tool}:{payload}".encode()).hexdigest()


def _record_step(
    conn: sqlite3.Connection,
    run_id: str,
    seq: int,
    *,
    thought: str | None,
    tool_name: str | None,
    args: dict[str, Any],
    observation: str | None,
    error_type: str | None,
    latency_ms: int | None,
    key: str = "",
    attempts: Sequence[dict[str, Any]] = (),
) -> None:
    """Write one steps row plus its tool_call attempts, then count the step.

    Everything here runs inside the caller's transaction: the step and the
    attempts can never be persisted without each other.
    """
    now = utcnow()
    cursor = conn.execute(
        INSERT_STEP,
        (
            run_id,
            seq,
            thought,
            tool_name,
            json.dumps(args, ensure_ascii=False),
            observation,
            error_type,
            latency_ms,
            now,
        ),
    )
    step_id = cursor.lastrowid
    for record in attempts:
        conn.execute(
            INSERT_TOOL_CALL,
            (
                step_id,
                key,
                record["attempt"],
                record["status"],
                record["error_type"],
                record["observation"],
                now,
            ),
        )
    conn.execute(COUNT_STEP, (now, run_id))


def _record_pending_step(
    conn: sqlite3.Connection,
    run_id: str,
    seq: int,
    *,
    thought: str | None,
    tool_name: str,
    args: dict[str, Any],
) -> int:
    """Create the step a human must approve; it counts immediately.

    The row deliberately has no observation, error_type or latency: the tool has
    not run yet, and approve/cancel/timeout fill those in later.
    """
    now = utcnow()
    cursor = conn.execute(
        INSERT_STEP,
        (
            run_id,
            seq,
            thought,
            tool_name,
            json.dumps(args, ensure_ascii=False),
            None,
            None,
            None,
            now,
        ),
    )
    conn.execute(COUNT_STEP, (now, run_id))
    return cursor.lastrowid


def _pending_step(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    """The step an approval is waiting on, if any."""
    return conn.execute(PENDING_STEP_SQL, (run_id,)).fetchone()


def expire_approval(conn: sqlite3.Connection, run: sqlite3.Row) -> None:
    """Audit the timeout, note it on the pending step, and cancel the run.

    Runs inside the caller's transaction; a lost CAS raises InvalidTransition
    and rolls the whole thing back, so no half-written timeout record survives.
    """
    pending = _pending_step(conn, run["id"])
    conn.execute(
        INSERT_APPROVAL,
        (
            run["id"],
            pending["id"] if pending is not None else None,
            "timeout",
            "approval timed out",
            utcnow(),
        ),
    )
    if pending is not None:
        conn.execute(
            "UPDATE steps SET observation = ? WHERE id = ?",
            ("approval timeout", pending["id"]),
        )
    conn.execute(
        "UPDATE runs SET approval_expires_at = NULL WHERE id = ?", (run["id"],)
    )
    transition(conn, run["id"], RunStatus.CANCELLED)


async def _run_tool(
    conn: sqlite3.Connection,
    spec: registry.ToolSpec,
    args: dict[str, Any],
    key: str,
) -> tuple[str | None, list[dict[str, Any]], Exception | None]:
    """Execute one tool with timeout + bounded retries; return (observation, attempts, error).

    A cached success for the same idempotency key short-circuits the call and is
    recorded as attempt 0 with status ``cached``.
    """
    cached = conn.execute(
        "SELECT observation FROM tool_calls "
        "WHERE idempotency_key = ? AND status = 'success' ORDER BY id LIMIT 1",
        (key,),
    ).fetchone()
    if cached is not None:
        observation = cached["observation"]
        return observation, [
            {
                "attempt": 0,
                "status": "cached",
                "error_type": None,
                "observation": observation,
            }
        ], None

    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    # BASELINE (eval only) means one shot: no retry, no backoff.
    max_attempts = 1 if BASELINE else settings.max_attempts

    for attempt in range(1, max_attempts + 1):
        try:
            result = await asyncio.wait_for(spec.fn(**args), timeout=spec.timeout_s)
        except asyncio.TimeoutError:
            last_error = ToolTimeout(
                f"{spec.name} timed out after {spec.timeout_s}s (attempt {attempt})"
            )
        except (RateLimited, PermissionDenied, UpstreamError) as exc:
            last_error = exc
        except Exception as exc:  # unclassified tool failure, never retried
            last_error = exc
        else:
            observation = json.dumps(result, ensure_ascii=False)
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "success",
                    "error_type": None,
                    "observation": observation,
                }
            )
            return observation, attempts, None

        attempts.append(
            {
                "attempt": attempt,
                "status": "failed",
                "error_type": type(last_error).__name__,
                "observation": str(last_error),
            }
        )
        retryable = isinstance(last_error, RETRYABLE_ERRORS) and spec.idempotent
        if not retryable or attempt >= max_attempts:
            return None, attempts, last_error

        await asyncio.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])

    return None, attempts, last_error


def _is_loop(history: Sequence[sqlite3.Row], tool: str | None, args: dict[str, Any]) -> bool:
    """True when the previous recorded step asked for the same tool and args."""
    if BASELINE:
        return False
    if not history:
        return False
    last = history[-1]
    if last["tool_name"] != tool:
        return False
    try:
        previous = json.loads(last["args_json"] or "{}")
    except (TypeError, ValueError):
        return False
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return json.dumps(previous, sort_keys=True, ensure_ascii=False) == canonical


def _fail_run(conn: sqlite3.Connection, run_id: str, error: str) -> None:
    """Record the reason and move RUNNING -> FAILED in the caller's transaction."""
    conn.execute(
        "UPDATE runs SET error = ?, updated_at = ? WHERE id = ? AND status = 'RUNNING'",
        (error, utcnow(), run_id),
    )
    transition(conn, run_id, RunStatus.FAILED)


async def execute_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Drive one run until it finishes, fails, or stops being RUNNING."""
    while True:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None or run["status"] != RunStatus.RUNNING.value:
            return

        if run["step_count"] >= run["max_steps"]:
            with conn:
                _fail_run(conn, run_id, "max steps exceeded")
            return

        history = conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()

        try:
            plan = await planner.plan(run["goal"], history, list(TOOLS.values()))
        except PlanningError as exc:
            # A planner that cannot produce a plan must not leave the run RUNNING.
            with conn:
                _fail_run(conn, run_id, str(exc))
            raise

        seq = run["step_count"] + 1

        if plan.action == "finish":
            with conn:
                conn.execute(
                    "UPDATE runs SET result = ?, updated_at = ? WHERE id = ?",
                    (plan.summary or "done", utcnow(), run_id),
                )
                transition(conn, run_id, RunStatus.SUCCEEDED)
            return

        spec = registry.get_tool(plan.tool or "")
        if spec is None:
            with conn:
                _record_step(
                    conn,
                    run_id,
                    seq,
                    thought=None,
                    tool_name=plan.tool,
                    args=plan.args,
                    observation="unknown tool",
                    error_type="unknown_tool",
                    latency_ms=None,
                )
            continue

        try:
            registry.validate_args(spec, plan.args)
        except ValidationError as exc:
            with conn:
                _record_step(
                    conn,
                    run_id,
                    seq,
                    thought=None,
                    tool_name=plan.tool,
                    args=plan.args,
                    observation=str(exc),
                    error_type="validation",
                    latency_ms=None,
                )
            continue

        if _is_loop(history, plan.tool, plan.args):
            # Repeating the previous step forever is a planner bug, not a tool bug.
            with conn:
                _fail_run(conn, run_id, "loop detected")
            return

        if spec.risk in ("write", "high") and not BASELINE:
            # Anything that can change the world waits for a human: create the
            # step now (it counts) and park the run; approve/cancel fills it in.
            with conn:
                _record_pending_step(
                    conn,
                    run_id,
                    seq,
                    thought=plan.reason or None,
                    tool_name=spec.name,
                    args=plan.args,
                )
                conn.execute(
                    "UPDATE runs SET status = 'WAITING_APPROVAL', "
                    "approval_expires_at = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'RUNNING'",
                    (utc_after(settings.approval_timeout_seconds), utcnow(), run_id),
                )
            return

        key = idempotency_key(run_id, seq, spec.name, plan.args)
        started = time.perf_counter()
        observation, attempts, error = await _run_tool(conn, spec, plan.args, key)
        latency_ms = int((time.perf_counter() - started) * 1000)

        if error is None:
            with conn:
                _record_step(
                    conn,
                    run_id,
                    seq,
                    thought=plan.reason or None,
                    tool_name=plan.tool,
                    args=plan.args,
                    observation=observation or "",
                    error_type=None,
                    latency_ms=latency_ms,
                    key=key,
                    attempts=attempts,
                )
            continue

        with conn:
            _record_step(
                conn,
                run_id,
                seq,
                thought=plan.reason or None,
                tool_name=plan.tool,
                args=plan.args,
                observation=str(error),
                error_type=type(error).__name__,
                latency_ms=latency_ms,
                key=key,
                attempts=attempts,
            )
            _fail_run(conn, run_id, str(error))
        return


async def approve_run(
    conn: sqlite3.Connection,
    run_id: str,
    decision: str,
    note: str | None = None,
) -> None:
    """Answer a WAITING_APPROVAL run: resume it (approve) or cancel it (reject).

    Approve never inserts a second step: it fills in the pending one, so
    ``step_count`` does not move. The RUNNING transition is the concurrency
    guard: two simultaneous approvals both see WAITING_APPROVAL, but only one
    CAS wins and the loser raises InvalidTransition (HTTP 409).
    """
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise RunNotFound(f"run {run_id} not found")
    if run["status"] != RunStatus.WAITING_APPROVAL.value:
        raise InvalidTransition(
            f"run {run_id} is {run['status']}, not {RunStatus.WAITING_APPROVAL.value}"
        )
    if is_expired(run["approval_expires_at"]):
        with conn:
            expire_approval(conn, run)
        raise InvalidTransition(f"run {run_id}: approval expired")

    pending = _pending_step(conn, run_id)
    if pending is None:
        raise InvalidTransition(f"run {run_id} has no pending step")

    if decision == "reject":
        with conn:
            conn.execute(
                INSERT_APPROVAL,
                (run_id, pending["id"], "reject", note, utcnow()),
            )
            conn.execute(
                "UPDATE steps SET observation = ? WHERE id = ?",
                ("rejected by human", pending["id"]),
            )
            conn.execute(
                "UPDATE runs SET approval_expires_at = NULL WHERE id = ?", (run_id,)
            )
            transition(conn, run_id, RunStatus.CANCELLED)
        return

    if decision != "approve":
        raise InvalidTransition(f"run {run_id}: unknown decision {decision!r}")

    with conn:
        transition(conn, run_id, RunStatus.RUNNING)
        conn.execute(
            INSERT_APPROVAL,
            (run_id, pending["id"], "approve", note, utcnow()),
        )
        conn.execute(
            "UPDATE runs SET approval_expires_at = NULL WHERE id = ?", (run_id,)
        )

    spec = registry.get_tool(pending["tool_name"])
    if spec is None:
        with conn:
            conn.execute(
                "UPDATE steps SET observation = ?, error_type = ? WHERE id = ?",
                ("unknown tool", "unknown_tool", pending["id"]),
            )
            _fail_run(conn, run_id, f"unknown tool: {pending['tool_name']}")
        return

    args = json.loads(pending["args_json"] or "{}")
    key = idempotency_key(run_id, pending["seq"], spec.name, args)
    started = time.perf_counter()
    observation, attempts, error = await _run_tool(conn, spec, args, key)
    latency_ms = int((time.perf_counter() - started) * 1000)

    with conn:
        conn.execute(
            "UPDATE steps SET observation = ?, error_type = ?, latency_ms = ? WHERE id = ?",
            (
                observation if error is None else str(error),
                None if error is None else type(error).__name__,
                latency_ms,
                pending["id"],
            ),
        )
        for record in attempts:
            conn.execute(
                INSERT_TOOL_CALL,
                (
                    pending["id"],
                    key,
                    record["attempt"],
                    record["status"],
                    record["error_type"],
                    record["observation"],
                    utcnow(),
                ),
            )
        if error is not None:
            _fail_run(conn, run_id, str(error))

    if error is None:
        await execute_run(conn, run_id)


def cancel_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Cancel a run from CREATED / RUNNING / WAITING_APPROVAL; terminals are 409."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise RunNotFound(f"run {run_id} not found")

    status = RunStatus(run["status"])
    if status in TERMINAL_STATUSES:
        raise InvalidTransition(f"run {run_id} is already {status.value}")

    if status is RunStatus.WAITING_APPROVAL:
        pending = _pending_step(conn, run_id)
        with conn:
            conn.execute(
                INSERT_APPROVAL,
                (
                    run_id,
                    pending["id"] if pending is not None else None,
                    "cancel",
                    "cancelled by user",
                    utcnow(),
                ),
            )
            if pending is not None:
                conn.execute(
                    "UPDATE steps SET observation = ? WHERE id = ?",
                    ("cancelled by user", pending["id"]),
                )
            conn.execute(
                "UPDATE runs SET approval_expires_at = NULL WHERE id = ?", (run_id,)
            )
            transition(conn, run_id, RunStatus.CANCELLED)
        return

    with conn:
        transition(conn, run_id, RunStatus.CANCELLED)


def recover_orphaned_runs(conn: sqlite3.Connection) -> int:
    """Mark every RUNNING run FAILED after a restart; never auto-resume a tool.

    A RUNNING row at startup means the process died mid-loop. If a pending step
    exists, its tool may or may not have reached the outside world, so the run
    is failed with a note that a human must reconcile it. WAITING_APPROVAL is
    left alone: it is still a valid state with approve/reject/timeout to come.
    """
    rows = conn.execute("SELECT * FROM runs WHERE status = 'RUNNING'").fetchall()
    for run in rows:
        pending = _pending_step(conn, run["id"])
        with conn:
            if pending is not None:
                conn.execute(
                    "UPDATE steps SET observation = ?, error_type = ? WHERE id = ?",
                    (
                        "orphaned at startup; approved tool may have run",
                        "OrphanedRun",
                        pending["id"],
                    ),
                )
            conn.execute(
                "UPDATE runs SET error = ?, updated_at = ? WHERE id = ? AND status = 'RUNNING'",
                (
                    "orphaned at startup; manual reconciliation required",
                    utcnow(),
                    run["id"],
                ),
            )
            transition(conn, run["id"], RunStatus.FAILED)
    return len(rows)
