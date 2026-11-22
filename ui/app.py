"""Streamlit single page: run the agent in-process and inspect its trace.

Two modes:
  * 脚本演示（默认）: the planner is replaced by a scripted replay, so the page
    works offline and never needs an API key;
  * 真实 LLM: only offered when LLM_BASE_URL / LLM_API_KEY / LLM_MODEL are
    configured; it uses the real planner through the same FastAPI app.

The UI talks to the app through TestClient (no separate uvicorn needed) and
shows only runtime facts: statuses, steps, tool attempts and approvals. Keys
are never printed, never typed into the page, and never stored in a trace.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Streamlit puts ui/ on sys.path first; put the project root ahead of it so
# `import app` resolves to the real package, not to this file.
if sys.path and sys.path[0] != str(PROJECT_ROOT):
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import planner, registry, tools  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.planner import Plan  # noqa: E402

UI_DIR = Path(__file__).resolve().parent
UI_DB = UI_DIR / "demo.db"

# The retry demo would otherwise block for two real 5s timeouts; the runtime
# behaviour (two failures, then success) is unchanged.
DEMO_TIMEOUT_S = 0.2

SCENARIOS: dict[str, dict] = {
    "normal": {
        "label": "① 正常排查（get_logs → get_metrics）",
        "plans": [
            {
                "action": "call_tool",
                "tool": "get_logs",
                "args": {"service": "api", "minutes": 10},
                "reason": "先看日志",
            },
            {
                "action": "call_tool",
                "tool": "get_metrics",
                "args": {"service": "api", "metric": "cpu"},
                "reason": "再看 cpu",
            },
            {"action": "finish", "summary": "日志和 CPU 都正常"},
        ],
        "faults": {},
    },
    "restart": {
        "label": "② 高危重启（restart_service，需要审批）",
        "plans": [
            {
                "action": "call_tool",
                "tool": "restart_service",
                "args": {"service": "api"},
                "reason": "重启 api",
            },
            {"action": "finish", "summary": "已重启"},
        ],
        "faults": {},
    },
    "retry": {
        "label": "③ 超时重试（get_logs timeout:2）",
        "plans": [
            {
                "action": "call_tool",
                "tool": "get_logs",
                "args": {"service": "api", "minutes": 10},
                "reason": "查日志",
            },
            {"action": "finish", "summary": "重试后拿到日志"},
        ],
        "faults": {"get_logs": "timeout:2"},
    },
}


def llm_configured() -> bool:
    """True only when all three LLM settings are present (never prints them)."""
    return bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model)


def _fake_planner(plans: list[dict]):
    """Replay the scripted plans; keep finishing if the script runs out."""
    queue = [Plan(**plan) for plan in plans]

    async def fake_plan(goal: str, history: list, tools_: list) -> Plan:
        if not queue:
            return Plan(action="finish", summary="done")
        return queue.pop(0)

    return fake_plan


def _fetch_trace(client: TestClient, run_id: str) -> dict:
    response = client.get(f"/runs/{run_id}/trace")
    return response.json() if response.status_code == 200 else {"error": response.text}


def run_scripted(goal: str, scenario: str) -> dict:
    """Run one scripted scenario against the real app; return run + trace."""
    script = SCENARIOS[scenario]
    original_plan = planner.plan
    original_logs = registry.TOOLS["get_logs"]

    planner.plan = _fake_planner(script["plans"])
    tools.FAULTS.clear()
    tools.reset_attempts()
    tools.FAULTS.update(script["faults"])
    if scenario == "retry":
        registry.TOOLS["get_logs"] = dataclasses.replace(
            original_logs, timeout_s=DEMO_TIMEOUT_S
        )
    try:
        with TestClient(create_app(str(UI_DB))) as client:
            response = client.post("/runs", json={"goal": goal})
            if response.status_code != 201:
                return {"error": response.text, "status_code": response.status_code}
            run = response.json()
            trace = _fetch_trace(client, run["id"])
    finally:
        planner.plan = original_plan
        registry.TOOLS["get_logs"] = original_logs
        tools.FAULTS.clear()
        tools.reset_attempts()

    return {"run": run, "trace": trace, "mode": "scripted", "scenario": scenario}


def run_real(goal: str) -> dict:
    """Run with the real planner; failures are returned, never raised."""
    with TestClient(create_app(str(UI_DB))) as client:
        response = client.post("/runs", json={"goal": goal})
        if response.status_code != 201:
            return {"error": response.text, "status_code": response.status_code}
        run = response.json()
        trace = _fetch_trace(client, run["id"])
    return {"run": run, "trace": trace, "mode": "real"}


def answer_approval(
    run_id: str, decision: str, mode: str, scenario: str | None
) -> dict:
    """Approve (and resume) or reject a waiting run."""
    original_plan = planner.plan
    if mode == "scripted" and decision == "approve":
        # The suspended scenario only has the closing plan left.
        planner.plan = _fake_planner([{"action": "finish", "summary": "done"}])
    try:
        with TestClient(create_app(str(UI_DB))) as client:
            response = client.post(
                f"/runs/{run_id}/approve", json={"decision": decision}
            )
            if response.status_code != 200:
                return {"error": response.text, "status_code": response.status_code}
            run = response.json()
            trace = _fetch_trace(client, run_id)
    finally:
        planner.plan = original_plan
    return {"run": run, "trace": trace, "mode": mode, "scenario": scenario}


def render_trace(trace: dict) -> None:
    """Render run status, steps with attempts, and approvals (no secrets)."""
    if "error" in trace:
        st.error(trace["error"])
        return

    run = trace["run"]
    st.markdown(
        f"**run** `{run['id']}` · status `{run['status']}` · steps `{run['step_count']}`"
    )
    if run.get("result"):
        st.success(run["result"])
    if run.get("error"):
        st.error(run["error"])

    for step in trace["steps"]:
        title = f"step {step['seq']} · {step['tool_name'] or '-'} · {step['error_type'] or 'ok'}"
        with st.expander(title, expanded=step["seq"] == 1):
            st.json(step["args"])
            st.code(step["observation"] or "(empty)")
            st.caption(f"latency={step['latency_ms']} ms")
            for call in step["tool_calls"]:
                st.write(
                    f"attempt {call['attempt']} · {call['status']} · "
                    f"{call['error_type'] or 'no error'}"
                )

    if trace["approvals"]:
        st.markdown("**approvals**")
        for row in trace["approvals"]:
            st.write(f"{row['decision']} @ {row['created_at']}")


def main() -> None:
    st.set_page_config(page_title="AgentLoop-lite", page_icon="🛠️", layout="wide")
    st.title("AgentLoop-lite · 最小 Agent 运行时")
    st.caption("状态机 · 工具治理 · 审批（HITL）· Trace · 30 题运行时评测")

    configured = llm_configured()
    st.caption(f"Real LLM: {'configured' if configured else 'not configured'}")

    mode = st.radio(
        "运行模式",
        ["脚本演示（默认）", "真实 LLM"],
        index=0,
        horizontal=True,
        disabled=not configured,
        help="真实模式需要本地 .env 里的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL",
    )

    goal = st.text_input("目标", value="查看 api 最近 10 分钟日志和 CPU")

    if mode.startswith("脚本"):
        columns = st.columns(len(SCENARIOS))
        for column, (key, script) in zip(columns, SCENARIOS.items()):
            if column.button(script["label"], use_container_width=True):
                st.session_state["last"] = run_scripted(goal, key)
    else:
        if st.button("运行（真实 LLM）", type="primary"):
            st.session_state["last"] = run_real(goal)

    result = st.session_state.get("last")
    if result and result.get("run"):
        run = result["run"]
        if run.get("status") == "WAITING_APPROVAL":
            st.warning("run 正在等待人工审批：高危工具还未执行")
            left, right = st.columns(2)
            if left.button("✅ Approve", use_container_width=True):
                st.session_state["last"] = answer_approval(
                    run["id"], "approve", result.get("mode", "scripted"), result.get("scenario")
                )
                st.rerun()
            if right.button("⛔ Reject", use_container_width=True):
                st.session_state["last"] = answer_approval(
                    run["id"], "reject", result.get("mode", "scripted"), result.get("scenario")
                )
                st.rerun()

        if result.get("trace"):
            st.subheader("Trace")
            render_trace(result["trace"])

    if result and result.get("error"):
        st.error(result["error"])
        st.caption(f"HTTP {result.get('status_code')}")

    st.divider()
    st.caption("默认演示为脚本模式；真实模式需要本地 .env")


if __name__ == "__main__":
    main()
