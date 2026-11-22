"""Run lifecycle checks: the app boots, the state machine refuses bad moves,
and the executor turns a planner script into recorded steps."""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

import pytest

from app import planner, registry, tools
from app.db import connect, init_db
from app.errors import InvalidTransition, PlanningError
from app.executor import recover_orphaned_runs
from app.planner import Plan
from app.state_machine import RunStatus, is_expired, transition

INSERT_RUN = (
    "INSERT INTO runs (id, goal, status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
CREATED_AT = "2026-11-21T00:00:00+00:00"


def _status(conn: sqlite3.Connection, run_id: str) -> str:
    row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row is not None
    return row["status"]


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_transition_rejects_invalid(client):
    """CREATED -> RUNNING is legal; SUCCEEDED -> RUNNING must raise and change nothing."""
    conn = connect(client.app.state.db_path)
    try:
        conn.execute(
            INSERT_RUN,
            ("run-transition", "check api error rate", RunStatus.CREATED.value, CREATED_AT, CREATED_AT),
        )
        conn.commit()

        # transition() leaves the transaction open on purpose: the caller decides
        # when the status move and its side effects become durable together.
        transition(conn, "run-transition", RunStatus.RUNNING)
        conn.commit()
        assert _status(conn, "run-transition") == RunStatus.RUNNING.value

        transition(conn, "run-transition", RunStatus.SUCCEEDED)
        conn.commit()
        assert _status(conn, "run-transition") == RunStatus.SUCCEEDED.value

        with pytest.raises(InvalidTransition):
            transition(conn, "run-transition", RunStatus.RUNNING)

        with pytest.raises(InvalidTransition):
            transition(conn, "run-transition", "BOGUS")

        assert _status(conn, "run-transition") == RunStatus.SUCCEEDED.value
    finally:
        conn.close()


def _queue_plans(monkeypatch: pytest.MonkeyPatch, plans: list[Plan]) -> None:
    """Make app.planner.plan return the scripted plans in order, with no network."""
    queue = list(plans)

    async def fake_plan(goal: str, history: list[Any], tools: list[Any]) -> Plan:
        assert queue, "planner was called more often than the test scripted"
        return queue.pop(0)

    monkeypatch.setattr(planner, "plan", fake_plan)


def test_normal_run_succeeds(client, monkeypatch):
    """get_logs -> get_metrics -> finish: SUCCEEDED with two recorded steps."""
    _queue_plans(
        monkeypatch,
        [
            Plan(
                action="call_tool",
                tool="get_logs",
                args={"service": "api", "minutes": 10},
                reason="read the api logs first",
            ),
            Plan(
                action="call_tool",
                tool="get_metrics",
                args={"service": "api", "metric": "cpu"},
                reason="then check cpu",
            ),
            Plan(action="finish", summary="api is healthy"),
        ],
    )

    response = client.post("/runs", json={"goal": "查看 api 最近 10 分钟日志和 CPU"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == RunStatus.SUCCEEDED.value
    assert body["step_count"] == 2
    assert body["result"] == "api is healthy"
    assert body["error"] is None

    conn = connect(client.app.state.db_path)
    try:
        steps = conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (body["id"],)
        ).fetchall()
    finally:
        conn.close()
    assert [step["tool_name"] for step in steps] == ["get_logs", "get_metrics"]
    assert [step["seq"] for step in steps] == [1, 2]
    assert all(step["error_type"] is None for step in steps)
    assert all(step["observation"] for step in steps)
    assert all(step["latency_ms"] is not None for step in steps)

    fetched = client.get(f"/runs/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == RunStatus.SUCCEEDED.value
    assert fetched.json()["step_count"] == 2


def test_unknown_tool_is_recorded(client, monkeypatch):
    """An unknown tool takes one step and the run keeps going until finish."""
    _queue_plans(
        monkeypatch,
        [
            Plan(action="call_tool", tool="drop_database", args={}),
            Plan(action="finish", summary="done"),
        ],
    )

    response = client.post("/runs", json={"goal": "drop the database"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == RunStatus.SUCCEEDED.value
    assert body["step_count"] == 1

    conn = connect(client.app.state.db_path)
    try:
        step = conn.execute(
            "SELECT * FROM steps WHERE run_id = ?", (body["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert step is not None
    assert step["tool_name"] == "drop_database"
    assert step["error_type"] == "unknown_tool"
    assert step["observation"] == "unknown tool"


def test_loop_detection_fails(client, monkeypatch):
    """The same tool + args twice in a row: FAILED, one step, tool runs once."""
    repeated = Plan(
        action="call_tool",
        tool="get_logs",
        args={"service": "api", "minutes": 5},
    )
    _queue_plans(monkeypatch, [repeated, repeated])

    response = client.post("/runs", json={"goal": "loop forever"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == RunStatus.FAILED.value
    assert "loop detected" in (body["error"] or "")
    assert body["step_count"] == 1

    conn = connect(client.app.state.db_path)
    try:
        steps = conn.execute(
            "SELECT * FROM steps WHERE run_id = ?", (body["id"],)
        ).fetchall()
        calls = conn.execute(
            "SELECT * FROM tool_calls WHERE step_id = ?", (steps[0]["id"],)
        ).fetchall()
    finally:
        conn.close()
    assert len(steps) == 1
    assert len(calls) == 1


def test_planning_error_marks_run_failed(client, monkeypatch):
    """A planner failure returns 503 and leaves the run FAILED, not RUNNING."""
    async def boom(goal: str, history: list, tools: list) -> Plan:
        raise PlanningError("bad plan")

    monkeypatch.setattr(planner, "plan", boom)

    response = client.post("/runs", json={"goal": "anything"})

    assert response.status_code == 503
    assert "bad plan" in response.json()["detail"]

    conn = connect(client.app.state.db_path)
    try:
        run = conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert run is not None
    assert run["status"] == RunStatus.FAILED.value
    assert "bad plan" in (run["error"] or "")


def _fetch(client, sql: str, params: tuple = ()) -> list:
    conn = connect(client.app.state.db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _create_waiting_run(client, monkeypatch) -> dict:
    """Create a run whose first plan is a high-risk restart, so it parks."""
    _queue_plans(
        monkeypatch,
        [
            Plan(
                action="call_tool",
                tool="restart_service",
                args={"service": "api"},
                reason="restart the api",
            ),
            Plan(action="finish", summary="restarted"),
        ],
    )
    response = client.post("/runs", json={"goal": "restart api"})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == RunStatus.WAITING_APPROVAL.value
    return body


def test_high_risk_waits_approval(client, monkeypatch):
    """restart_service parks the run: pending step, no tool call, no approval yet."""
    body = _create_waiting_run(client, monkeypatch)

    assert body["step_count"] == 1
    assert body["approval_expires_at"]

    steps = _fetch(client, "SELECT * FROM steps WHERE run_id = ?", (body["id"],))
    assert len(steps) == 1
    assert steps[0]["tool_name"] == "restart_service"
    assert steps[0]["observation"] is None
    assert steps[0]["error_type"] is None
    assert steps[0]["latency_ms"] is None

    assert _fetch(client, "SELECT * FROM approvals WHERE run_id = ?", (body["id"],)) == []
    calls = _fetch(
        client,
        "SELECT tc.* FROM tool_calls tc JOIN steps s ON s.id = tc.step_id "
        "WHERE s.run_id = ?",
        (body["id"],),
    )
    assert calls == []


def test_approve_resumes(client, monkeypatch):
    """Approve fills the pending step, runs the tool, and the loop finishes."""
    body = _create_waiting_run(client, monkeypatch)

    response = client.post(
        f"/runs/{body['id']}/approve", json={"decision": "approve"}
    )

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == RunStatus.SUCCEEDED.value
    assert run["step_count"] == 1
    assert run["approval_expires_at"] is None

    steps = _fetch(client, "SELECT * FROM steps WHERE run_id = ?", (body["id"],))
    assert len(steps) == 1  # approve updates the pending step, never inserts
    assert steps[0]["observation"]
    assert steps[0]["error_type"] is None

    approvals = _fetch(client, "SELECT * FROM approvals WHERE run_id = ?", (body["id"],))
    assert [row["decision"] for row in approvals] == ["approve"]

    calls = _fetch(
        client,
        "SELECT tc.* FROM tool_calls tc JOIN steps s ON s.id = tc.step_id "
        "WHERE s.run_id = ?",
        (body["id"],),
    )
    assert [(call["attempt"], call["status"]) for call in calls] == [(1, "success")]


def test_reject_cancels(client, monkeypatch):
    """Reject cancels the run, notes the pending step and never runs the tool."""
    body = _create_waiting_run(client, monkeypatch)

    response = client.post(
        f"/runs/{body['id']}/approve",
        json={"decision": "reject", "note": "not during peak hours"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == RunStatus.CANCELLED.value

    steps = _fetch(client, "SELECT * FROM steps WHERE run_id = ?", (body["id"],))
    assert "rejected" in (steps[0]["observation"] or "")

    approvals = _fetch(client, "SELECT * FROM approvals WHERE run_id = ?", (body["id"],))
    assert [row["decision"] for row in approvals] == ["reject"]
    assert approvals[0]["note"] == "not during peak hours"

    calls = _fetch(
        client,
        "SELECT tc.* FROM tool_calls tc JOIN steps s ON s.id = tc.step_id "
        "WHERE s.run_id = ?",
        (body["id"],),
    )
    assert calls == []


def test_approval_timeout_cancels(client, monkeypatch):
    """A deadline in the past is turned into CANCELLED by the next GET."""
    body = _create_waiting_run(client, monkeypatch)

    conn = connect(client.app.state.db_path)
    try:
        conn.execute(
            "UPDATE runs SET approval_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", body["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/runs/{body['id']}")

    assert response.status_code == 200
    assert response.json()["status"] == RunStatus.CANCELLED.value

    approvals = _fetch(client, "SELECT * FROM approvals WHERE run_id = ?", (body["id"],))
    assert [row["decision"] for row in approvals] == ["timeout"]

    steps = _fetch(client, "SELECT * FROM steps WHERE run_id = ?", (body["id"],))
    assert "timeout" in (steps[0]["observation"] or "")

    calls = _fetch(
        client,
        "SELECT tc.* FROM tool_calls tc JOIN steps s ON s.id = tc.step_id "
        "WHERE s.run_id = ?",
        (body["id"],),
    )
    assert calls == []


def test_second_approve_returns_409(client, monkeypatch):
    """Once the run leaves WAITING_APPROVAL, a second approve is a conflict."""
    body = _create_waiting_run(client, monkeypatch)

    first = client.post(f"/runs/{body['id']}/approve", json={"decision": "approve"})
    second = client.post(f"/runs/{body['id']}/approve", json={"decision": "approve"})

    assert first.status_code == 200
    assert first.json()["status"] == RunStatus.SUCCEEDED.value
    assert second.status_code == 409

    approvals = _fetch(client, "SELECT * FROM approvals WHERE run_id = ?", (body["id"],))
    assert [row["decision"] for row in approvals] == ["approve"]


def test_cancel_waiting_run(client, monkeypatch):
    """Cancel while waiting: audit row, pending step note, tool never runs."""
    body = _create_waiting_run(client, monkeypatch)

    response = client.post(f"/runs/{body['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == RunStatus.CANCELLED.value

    approvals = _fetch(client, "SELECT * FROM approvals WHERE run_id = ?", (body["id"],))
    assert [row["decision"] for row in approvals] == ["cancel"]

    steps = _fetch(client, "SELECT * FROM steps WHERE run_id = ?", (body["id"],))
    assert "cancelled" in (steps[0]["observation"] or "")

    calls = _fetch(
        client,
        "SELECT tc.* FROM tool_calls tc JOIN steps s ON s.id = tc.step_id "
        "WHERE s.run_id = ?",
        (body["id"],),
    )
    assert calls == []


def test_max_steps_failed(client, monkeypatch):
    """Six steps without a finish trip the max_steps guard."""
    _queue_plans(
        monkeypatch,
        [
            Plan(
                action="call_tool",
                tool="get_logs",
                args={"service": "api", "minutes": minutes},
            )
            for minutes in range(1, 7)
        ],
    )

    response = client.post("/runs", json={"goal": "read six windows"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == RunStatus.FAILED.value
    assert "max steps" in (body["error"] or "")
    assert body["step_count"] == 6

    calls = _fetch(
        client,
        "SELECT tc.* FROM tool_calls tc JOIN steps s ON s.id = tc.step_id "
        "WHERE s.run_id = ?",
        (body["id"],),
    )
    assert len(calls) == 6


def test_non_idempotent_tool_is_not_retried(client, monkeypatch):
    """restart_service times out once and fails: a restart is not retried."""
    monkeypatch.setitem(
        registry.TOOLS,
        "restart_service",
        dataclasses.replace(registry.TOOLS["restart_service"], timeout_s=0.05),
    )
    tools.FAULTS["restart_service"] = "timeout"

    body = _create_waiting_run(client, monkeypatch)

    response = client.post(
        f"/runs/{body['id']}/approve", json={"decision": "approve"}
    )

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == RunStatus.FAILED.value
    assert "timed out" in (run["error"] or "")

    calls = _fetch(
        client,
        "SELECT tc.* FROM tool_calls tc JOIN steps s ON s.id = tc.step_id "
        "WHERE s.run_id = ?",
        (body["id"],),
    )
    assert len(calls) == 1
    assert calls[0]["attempt"] == 1
    assert calls[0]["status"] == "failed"
    assert calls[0]["error_type"] == "ToolTimeout"


def test_is_expired_accepts_z():
    """Deadlines parse as timestamps, so ...Z and ...+00:00 both work."""
    assert is_expired("2000-01-01T00:00:00Z") is True
    assert is_expired("2999-01-01T00:00:00+00:00") is False
    assert is_expired(None) is False
    assert is_expired("not-a-timestamp") is False


def test_orphaned_running_run_is_failed(tmp_path):
    """A RUNNING run found at startup is failed, never resumed or retried."""
    db = tmp_path / "orphan.db"
    init_db(db)
    conn = connect(db)
    try:
        now = "2026-11-21T00:00:00+00:00"
        conn.execute(
            INSERT_RUN,
            ("run-orphan", "restart api", RunStatus.RUNNING.value, now, now),
        )
        conn.execute(
            "INSERT INTO steps (run_id, seq, tool_name, args_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("run-orphan", 1, "restart_service", '{"service": "api"}', now),
        )
        conn.commit()

        recovered = recover_orphaned_runs(conn)

        run = conn.execute(
            "SELECT * FROM runs WHERE id = ?", ("run-orphan",)
        ).fetchone()
        step = conn.execute(
            "SELECT * FROM steps WHERE run_id = ?", ("run-orphan",)
        ).fetchone()
        calls = conn.execute("SELECT * FROM tool_calls").fetchall()
    finally:
        conn.close()

    assert recovered == 1
    assert run["status"] == RunStatus.FAILED.value
    assert "orphaned" in (run["error"] or "")
    assert "orphaned" in (step["observation"] or "")
    assert step["error_type"] == "OrphanedRun"
    assert calls == []


def test_trace_after_approve(client, monkeypatch):
    """Trace shows the approved step, its successful attempt and the decision."""
    body = _create_waiting_run(client, monkeypatch)
    approved = client.post(
        f"/runs/{body['id']}/approve", json={"decision": "approve"}
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == RunStatus.SUCCEEDED.value

    response = client.get(f"/runs/{body['id']}/trace")

    assert response.status_code == 200
    trace = response.json()
    assert trace["run"]["status"] == RunStatus.SUCCEEDED.value
    assert len(trace["steps"]) == 1
    step = trace["steps"][0]
    assert step["tool_name"] == "restart_service"
    assert step["args"] == {"service": "api"}
    assert step["observation"]
    assert [call["status"] for call in step["tool_calls"]] == ["success"]
    assert [approval["decision"] for approval in trace["approvals"]] == ["approve"]


def test_trace_after_timeout(client, monkeypatch):
    """Trace applies lazy expiry; an unknown id has no trace (404 folded here)."""
    body = _create_waiting_run(client, monkeypatch)

    conn = connect(client.app.state.db_path)
    try:
        conn.execute(
            "UPDATE runs SET approval_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", body["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/runs/{body['id']}/trace")

    assert response.status_code == 200
    trace = response.json()
    assert trace["run"]["status"] == RunStatus.CANCELLED.value
    assert [approval["decision"] for approval in trace["approvals"]] == ["timeout"]
    assert "timeout" in (trace["steps"][0]["observation"] or "")
    assert trace["steps"][0]["tool_calls"] == []

    assert client.get("/runs/missing-id/trace").status_code == 404


def _stub_model(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> None:
    """Stub only the network hop: plan(), parse_plan() and the executor stay real.

    This is how the "model returns JSON" path is tested offline: instead of
    replacing planner.plan, we replace planner._chat with canned completions.
    """
    fake_settings = dataclasses.replace(
        planner.settings,
        llm_base_url="http://stub.local/v1",
        llm_api_key="test-key-not-used",
        llm_model="stub-model",
    )
    monkeypatch.setattr(planner, "settings", fake_settings)
    queue = list(replies)

    async def fake_chat(messages: list[dict]) -> str:
        assert queue, "planner asked for more completions than scripted"
        return queue.pop(0)

    monkeypatch.setattr(planner, "_chat", fake_chat)


def test_planner_json_path_runs_tool_and_finishes(client, monkeypatch):
    """A model-shaped JSON answer is parsed, validated, executed, then finished."""
    _stub_model(
        monkeypatch,
        [
            '{"action":"call_tool","tool":"get_logs",'
            '"args":{"service":"api","minutes":10},"reason":"查日志"}',
            '{"action":"finish","summary":"日志正常"}',
        ],
    )

    response = client.post("/runs", json={"goal": "查看 api 最近 10 分钟日志"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == RunStatus.SUCCEEDED.value
    assert body["step_count"] == 1

    trace = client.get(f"/runs/{body['id']}/trace").json()
    step = trace["steps"][0]
    assert step["tool_name"] == "get_logs"
    assert [call["status"] for call in step["tool_calls"]] == ["success"]


def test_planner_json_path_restart_waits_then_runs_once(client, monkeypatch):
    """Model-shaped restart plan parks the run; approve executes it exactly once."""
    _stub_model(
        monkeypatch,
        [
            '{"action":"call_tool","tool":"restart_service",'
            '"args":{"service":"api"},"reason":"重启"}',
            '{"action":"finish","summary":"已重启"}',
        ],
    )

    created = client.post("/runs", json={"goal": "重启 api 服务"})
    assert created.status_code == 201
    run = created.json()
    assert run["status"] == RunStatus.WAITING_APPROVAL.value
    assert run["step_count"] == 1

    before = client.get(f"/runs/{run['id']}/trace").json()
    assert before["steps"][0]["tool_calls"] == []

    approved = client.post(f"/runs/{run['id']}/approve", json={"decision": "approve"})
    assert approved.status_code == 200
    assert approved.json()["status"] == RunStatus.SUCCEEDED.value
    assert approved.json()["step_count"] == 1

    after = client.get(f"/runs/{run['id']}/trace").json()
    calls = after["steps"][0]["tool_calls"]
    assert [(call["attempt"], call["status"]) for call in calls] == [(1, "success")]
    assert [row["decision"] for row in after["approvals"]] == ["approve"]

