"""Tool behaviour: argument validation plus the timeout/permission paths the
executor drives. A malformed call never reaches a tool, and a tool failure is
recorded with one tool_calls row per attempt.
"""

from __future__ import annotations

import dataclasses

import pytest

from app import registry, tools
from app.db import connect
from app.errors import ValidationError
from app.planner import Plan
from app.state_machine import RunStatus

LEGAL_CALLS: list[tuple[str, dict]] = [
    ("get_logs", {"service": "api", "minutes": 10}),
    ("get_metrics", {"service": "db", "metric": "cpu"}),
    ("restart_service", {"service": "worker"}),
]

# (tool, args, substring the rejection message must contain)
BAD_CALLS: list[tuple[str, dict, str]] = [
    ("get_logs", {"service": "api"}, "required"),
    ("get_logs", {"service": "api", "minutes": 10, "follow": True}, "Additional properties"),
    ("get_metrics", {"service": "api", "metric": "disk"}, "is not one of"),
    ("get_metrics", {"service": "api", "metric": "cpu", "minutes": 5}, "Additional properties"),
    ("restart_service", {"service": "cache"}, "is not one of"),
    ("drop_database", {}, "unknown tool"),
]


def test_validate_args_rejects():
    for name, args in LEGAL_CALLS:
        registry.validate_args(name, args)

    # The same schema checks apply when the caller already holds the ToolSpec.
    registry.validate_args(registry.TOOLS["get_logs"], {"service": "api", "minutes": 1})

    for name, args, expected in BAD_CALLS:
        with pytest.raises(ValidationError) as rejected:
            registry.validate_args(name, args)
        assert expected in str(rejected.value), f"{name}: {rejected.value}"


def _queue_plans(monkeypatch: pytest.MonkeyPatch, plans: list[Plan]) -> None:
    """Make app.planner.plan return the scripted plans in order, with no network."""
    queue = list(plans)

    async def fake_plan(goal: str, history: list, tools_: list) -> Plan:
        assert queue, "planner was called more often than the test scripted"
        return queue.pop(0)

    monkeypatch.setattr("app.planner.plan", fake_plan)


def _calls_for(client, run_id: str) -> tuple[list, list]:
    conn = connect(client.app.state.db_path)
    try:
        steps = conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        calls = conn.execute(
            "SELECT * FROM tool_calls WHERE step_id = ? ORDER BY attempt",
            (steps[0]["id"],),
        ).fetchall()
    finally:
        conn.close()
    return steps, calls


def test_timeout_retries_then_succeeds(client, monkeypatch):
    """timeout:2 on a read-only tool -> two failed attempts, third succeeds."""
    _queue_plans(
        monkeypatch,
        [
            Plan(
                action="call_tool",
                tool="get_logs",
                args={"service": "api", "minutes": 10},
            ),
            Plan(action="finish", summary="done"),
        ],
    )
    fast = dataclasses.replace(registry.TOOLS["get_logs"], timeout_s=0.05)
    monkeypatch.setitem(registry.TOOLS, "get_logs", fast)
    tools.FAULTS["get_logs"] = "timeout:2"

    response = client.post("/runs", json={"goal": "read the api logs"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == RunStatus.SUCCEEDED.value
    assert body["step_count"] == 1

    steps, calls = _calls_for(client, body["id"])
    assert len(steps) == 1
    assert [(call["attempt"], call["status"]) for call in calls] == [
        (1, "failed"),
        (2, "failed"),
        (3, "success"),
    ]
    assert [call["error_type"] for call in calls] == [
        "ToolTimeout",
        "ToolTimeout",
        None,
    ]
    assert calls[2]["observation"]
    assert steps[0]["latency_ms"] is not None


def test_permission_denied_fails_without_retry(client, monkeypatch):
    """PermissionDenied is not retryable: exactly one failed attempt, then FAILED."""
    _queue_plans(
        monkeypatch,
        [
            Plan(
                action="call_tool",
                tool="get_logs",
                args={"service": "api", "minutes": 10},
            ),
            Plan(action="finish", summary="done"),
        ],
    )
    tools.FAULTS["get_logs"] = "permission"

    response = client.post("/runs", json={"goal": "read the api logs"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == RunStatus.FAILED.value
    assert "permission" in (body["error"] or "").lower()

    steps, calls = _calls_for(client, body["id"])
    assert len(steps) == 1
    assert len(calls) == 1
    assert calls[0]["attempt"] == 1
    assert calls[0]["status"] == "failed"
    assert calls[0]["error_type"] == "PermissionDenied"
