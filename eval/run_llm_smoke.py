"""Manual real-LLM smoke test for the two planner paths.

This script is NOT part of pytest and never runs in CI (it needs a key):
it drives the real planner through the same FastAPI app and asserts the two
paths that matter:

  A. normal task  -> the model plans a tool call, the runtime validates and
     executes it, the model finishes, the run ends SUCCEEDED;
  B. restart task -> restart_service parks the run in WAITING_APPROVAL with no
     tool execution, one approve executes it exactly once, then the run ends.

Credentials come from the environment or .env; nothing is printed except the
model name, statuses and redacted error text.

Usage:
    python eval/run_llm_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import create_app  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / ".tmp"
DB_PATH = WORK_DIR / "llm-smoke.db"


def redact(text: str | None) -> str:
    """Never echo the key, whatever an exception message might contain."""
    if not text:
        return ""
    secret = settings.llm_api_key
    if secret:
        text = text.replace(secret, "***")
    return text[:400]


def fetch_trace(client: TestClient, run_id: str) -> dict:
    response = client.get(f"/runs/{run_id}/trace")
    return response.json() if response.status_code == 200 else {"error": response.text}


def summarize_trace(trace: dict) -> dict:
    steps = trace.get("steps", [])
    calls = [call for step in steps for call in step["tool_calls"]]
    return {
        "status": trace.get("run", {}).get("status"),
        "step_count": trace.get("run", {}).get("step_count"),
        "tools": [step["tool_name"] for step in steps],
        "attempts": [(call["attempt"], call["status"]) for call in calls],
        "approvals": [row["decision"] for row in trace.get("approvals", [])],
        "error": redact(trace.get("run", {}).get("error")),
    }


def run_normal(client: TestClient) -> tuple[bool, dict]:
    response = client.post("/runs", json={"goal": "查看 api 最近 10 分钟日志"})
    if response.status_code != 201:
        return False, {"http": response.status_code, "error": redact(response.text)}
    run = response.json()
    trace = summarize_trace(fetch_trace(client, run["id"]))
    ok = trace["status"] == "SUCCEEDED" and any(
        call[1] == "success" for call in trace["attempts"]
    )
    return ok, {"run_id": run["id"], **trace}


def run_restart(client: TestClient) -> tuple[bool, dict]:
    response = client.post("/runs", json={"goal": "重启 api 服务"})
    if response.status_code != 201:
        return False, {"http": response.status_code, "error": redact(response.text)}
    run = response.json()
    run_id = run["id"]
    before = summarize_trace(fetch_trace(client, run_id))

    approved_status = None
    if run["status"] == "WAITING_APPROVAL":
        approved = client.post(f"/runs/{run_id}/approve", json={"decision": "approve"})
        approved_status = approved.status_code

    after = summarize_trace(fetch_trace(client, run_id))
    ok = (
        run["status"] == "WAITING_APPROVAL"
        and before["step_count"] == 1
        and before["attempts"] == []
        and approved_status == 200
        and after["status"] == "SUCCEEDED"
        and after["step_count"] == 1
        and after["attempts"] == [(1, "success")]
        and after["approvals"] == ["approve"]
    )
    return ok, {
        "run_id": run_id,
        "before": before,
        "approve_http": approved_status,
        "after": after,
    }


def main() -> int:
    missing = [
        name
        for name, value in (
            ("LLM_BASE_URL", settings.llm_base_url),
            ("LLM_API_KEY", settings.llm_api_key),
            ("LLM_MODEL", settings.llm_model),
        )
        if not value
    ]
    if missing:
        print(f"SKIPPED: missing {', '.join(missing)}")
        return 2

    print(f"model: {settings.llm_model}")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    with TestClient(create_app(str(DB_PATH))) as client:
        normal_ok, normal = run_normal(client)
        restart_ok, restart = run_restart(client)

    print(f"normal  : {'PASS' if normal_ok else 'FAIL'} {normal}")
    print(f"restart : {'PASS' if restart_ok else 'FAIL'} {restart}")

    conn = connect(DB_PATH)
    try:
        runs = conn.execute("SELECT status FROM runs ORDER BY rowid").fetchall()
    finally:
        conn.close()
    print("run status sequence:", [row["status"] for row in runs])

    return 0 if (normal_ok and restart_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
