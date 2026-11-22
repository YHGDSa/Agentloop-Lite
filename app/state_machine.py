"""The run state machine: six statuses and the one legal way to move between them.

The database is the source of truth, so a transition is a compare-and-set
UPDATE rather than an in-memory assignment. Two callers racing for the same run
(a retry and an approval, say) cannot both win: the loser sees ``rowcount == 0``
and gets ``InvalidTransition`` instead of silently overwriting the winner.

The caller owns the transaction: ``transition()`` never commits or rolls back,
so "move the status" and "write the step/approval row" can land in one atomic
unit, and a stale move cannot roll back work it never started.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum

from .errors import InvalidTransition


class RunStatus(str, Enum):
    """Every status a run can hold; the values match the ``runs.status`` CHECK."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
)

# The only edges the runtime may take. A status missing from a value cannot be
# reached from that value; terminal statuses map to an empty set on purpose.
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def utcnow() -> str:
    """Timestamp for ``created_at``/``updated_at``: UTC ISO 8601, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_after(seconds: int) -> str:
    """A timestamp ``seconds`` in the future, in the same format as ``utcnow()``."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def is_expired(value: str | None) -> bool:
    """True when a deadline has passed.

    Parses ISO 8601 so ``...Z`` and ``...+00:00`` compare correctly; a value we
    cannot parse is treated as "not expired" rather than crashing a request.
    """
    if not value:
        return False
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


def can_transition(from_status: RunStatus, to_status: RunStatus) -> bool:
    """True when the table allows this edge; a pure check, no database access."""
    return to_status in ALLOWED_TRANSITIONS[from_status]


def transition(
    conn: sqlite3.Connection, run_id: str, to_status: RunStatus | str
) -> None:
    """Move one run to ``to_status``, or raise ``InvalidTransition``.

    The read of the current status picks the ``WHERE status = ?`` guard, and the
    UPDATE itself decides the race: ``rowcount != 1`` means the row vanished or
    somebody else moved it in the meantime, so the caller's plan is stale.
    Committing is the caller's job; see the module docstring.
    """
    try:
        target = RunStatus(to_status)
    except ValueError as exc:
        raise InvalidTransition(
            f"run {run_id}: {to_status!r} is not a valid run status"
        ) from exc

    row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise InvalidTransition(f"run {run_id} does not exist")

    current = RunStatus(row["status"])
    if not can_transition(current, target):
        allowed = ", ".join(sorted(status.value for status in ALLOWED_TRANSITIONS[current]))
        raise InvalidTransition(
            f"run {run_id}: {current.value} -> {target.value} is not allowed "
            f"(allowed from {current.value}: {allowed or 'none, terminal'})"
        )

    cursor = conn.execute(
        "UPDATE runs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
        (target.value, utcnow(), run_id, current.value),
    )
    if cursor.rowcount != 1:
        raise InvalidTransition(
            f"run {run_id}: expected one row to move {current.value} -> "
            f"{target.value}, updated {cursor.rowcount}"
        )
