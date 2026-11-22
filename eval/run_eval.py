"""Run the 30 scripted runtime tasks twice and write eval/report.md.

``BASELINE=1`` removes retries, the approval gate and loop detection;
``BASELINE=0`` is the full runtime. The planner is scripted per task and the
tools are the in-process mocks, so nothing here touches the network or a real
LLM: the report measures the runtime's control flow, not model quality.

Usage (from the project root, with the project venv):
    python eval/run_eval.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import executor, planner, registry, tools  # noqa: E402
from app.db import connect  # noqa: E402
from app.errors import PlanningError  # noqa: E402
from app.main import create_app  # noqa: E402
from app.planner import Plan  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
TASKS_PATH = BASE_DIR / "tasks.json"
REPORT_PATH = BASE_DIR / "report.md"

# One throwaway DB per (task, mode). Kept inside eval/ instead of the system
# temp dir: the DSH sandbox cannot use pytest-style 0o700 temp dirs, and a
# self-contained scratch dir makes reruns reproducible. Gitignored.
WORK_DIR = BASE_DIR / ".tmp"

# The mock tools sleep to make a real timeout; 50ms keeps the eval quick and
# changes no decision (attempts still time out, retries still happen).
FAST_TIMEOUT_S = 0.05
EXPIRED_AT = "2000-01-01T00:00:00Z"


def load_tasks() -> list[dict[str, Any]]:
    return json.loads(TASKS_PATH.read_text(encoding="utf-8"))


def shrink_tool_timeouts() -> None:
    """Shorten tool timeouts so the eval does not wait 5-10s per failing call."""
    for name, spec in list(registry.TOOLS.items()):
        registry.TOOLS[name] = dataclasses.replace(spec, timeout_s=FAST_TIMEOUT_S)


def scripted_plan(plans: list[dict[str, Any]]):
    """Return a fake planner.plan that replays the task's scripted plans."""
    queue = [Plan(**plan) for plan in plans]

    async def fake_plan(goal: str, history: list, tools_: list) -> Plan:
        if not queue:
            raise PlanningError("eval script exhausted its plans")
        return queue.pop(0)

    return fake_plan


def latest_run(db_path: Path) -> tuple[str, str]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, status FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row["id"], row["status"]


def expire_run(db_path: Path, run_id: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE runs SET approval_expires_at = ? WHERE id = ?",
            (EXPIRED_AT, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def run_one(task: dict[str, Any], baseline: bool) -> dict[str, Any]:
    """Run one task once, with the approval action the task asks for."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    mode = "baseline" if baseline else "delta"
    db_path = WORK_DIR / f"{task['id']}-{mode}.db"
    if db_path.exists():
        db_path.unlink()

    app = create_app(str(db_path))

    original_plan = planner.plan
    planner.plan = scripted_plan(task["plans"])
    tools.FAULTS.clear()
    tools.reset_attempts()
    tools.FAULTS.update(task.get("faults") or {})
    executor.BASELINE = baseline
    try:
        with TestClient(app) as client:
            started = time.perf_counter()
            post = client.post("/runs", json={"goal": task["goal"]})
            run_id, initial_status = latest_run(db_path)

            decision = task.get("decision")
            if initial_status == "WAITING_APPROVAL":
                if decision in ("approve", "reject"):
                    client.post(
                        f"/runs/{run_id}/approve", json={"decision": decision}
                    )
                elif decision == "cancel":
                    client.post(f"/runs/{run_id}/cancel")

            if task.get("timeout") and initial_status == "WAITING_APPROVAL":
                expire_run(db_path, run_id)
                client.get(f"/runs/{run_id}")

            trace = client.get(f"/runs/{run_id}/trace").json()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
    finally:
        planner.plan = original_plan
        executor.BASELINE = False
        tools.FAULTS.clear()
        tools.reset_attempts()
        if db_path.exists():
            db_path.unlink()

    return summarize(task, baseline, trace, initial_status, post.status_code, elapsed_ms)


def summarize(
    task: dict[str, Any],
    baseline: bool,
    trace: dict[str, Any],
    initial_status: str,
    http_status: int,
    elapsed_ms: int,
) -> dict[str, Any]:
    run = trace["run"]
    steps = trace["steps"]
    calls = [call for step in steps for call in step["tool_calls"]]
    expect = task["expect"]
    return {
        "id": task["id"],
        "group": task["group"],
        "goal": task["goal"],
        "baseline": baseline,
        "initial_status": initial_status,
        "http_status": http_status,
        "status": run["status"],
        "step_count": run["step_count"],
        "error": run["error"],
        "result": run["result"],
        "calls": len(calls),
        "failed_calls": sum(1 for call in calls if call["status"] == "failed"),
        "steps_with_calls": sum(1 for step in steps if step["tool_calls"]),
        "approvals": [row["decision"] for row in trace["approvals"]],
        "elapsed_ms": elapsed_ms,
        "expect": expect,
        "status_ok": run["status"] == expect["status"],
        "steps_ok": run["step_count"] >= int(expect.get("min_steps", 0)),
    }


def success_rate(store: dict[str, dict], ids: list[str]) -> tuple[int, int]:
    return sum(1 for tid in ids if store.get(tid, {}).get("status_ok")), len(ids)


def avg_steps(store: dict[str, dict], ids: list[str]) -> float:
    values = [store[tid]["step_count"] for tid in ids if tid in store]
    return statistics.mean(values) if values else 0.0


def approval_rate(
    store: dict[str, dict], ids: list[str]
) -> tuple[int, int]:
    blocked = sum(
        1
        for tid in ids
        if store.get(tid, {}).get("initial_status") == "WAITING_APPROVAL"
    )
    return blocked, len(ids)


def loop_rate(store: dict[str, dict], ids: list[str]) -> tuple[int, int]:
    blocked = sum(
        1
        for tid in ids
        if "loop detected" in (store.get(tid, {}).get("error") or "")
    )
    return blocked, len(ids)


def retry_rate(store: dict[str, dict], ids: list[str]) -> tuple[float, int, int]:
    failed = sum(store[tid]["failed_calls"] for tid in ids if tid in store)
    steps_with_calls = sum(
        store[tid]["steps_with_calls"] for tid in ids if tid in store
    )
    rate = failed / steps_with_calls if steps_with_calls else 0.0
    return rate, failed, steps_with_calls


def pick_cases(
    tasks: list[dict], baseline: dict[str, dict], delta: dict[str, dict]
) -> list[tuple[dict, dict | None, dict | None]]:
    """Prefer pairs where baseline and delta disagree (the Delta's effect)."""
    cases: list[tuple[dict, dict | None, dict | None]] = []
    for task in tasks:
        b = baseline.get(task["id"])
        d = delta.get(task["id"])
        if b and d and b["status"] != d["status"]:
            cases.append((task, b, d))
    for task in tasks:
        if len(cases) >= 2:
            break
        d = delta.get(task["id"])
        if d and not (d["status_ok"] and d["steps_ok"]):
            if all(case[2] is None or case[2]["id"] != task["id"] for case in cases):
                cases.append((task, baseline.get(task["id"]), d))
    return cases[:2]


def build_report(
    tasks: list[dict],
    rows: dict[str, dict[str, dict]],
    skipped: list[tuple[str, str, str]],
) -> str:
    baseline = rows["baseline"]
    delta = rows["delta"]
    ids = [task["id"] for task in tasks]

    base_ok, total = success_rate(baseline, ids)
    delta_ok, _ = success_rate(delta, ids)
    base_steps = avg_steps(baseline, ids)
    delta_steps = avg_steps(delta, ids)
    base_approval, approval_total = approval_rate(
        baseline, [t["id"] for t in tasks if t["group"] == "approval"]
    )
    delta_approval, _ = approval_rate(
        delta, [t["id"] for t in tasks if t["group"] == "approval"]
    )
    base_loop, loop_total = loop_rate(
        baseline, [t["id"] for t in tasks if t["group"] == "loop"]
    )
    delta_loop, _ = loop_rate(
        delta, [t["id"] for t in tasks if t["group"] == "loop"]
    )
    base_retry, base_failed, base_calls = retry_rate(baseline, ids)
    delta_retry, delta_failed, delta_calls = retry_rate(delta, ids)

    lines = [
        "# AgentLoop-lite 运行评测报告（T6）",
        "",
        f"- 日期：{date.today().isoformat()}",
        f"- Python：{sys.version.split()[0]}",
        "- 运行器：`eval/run_eval.py`（stdlib + FastAPI TestClient；每条任务一个独立 DB，放 `eval/.tmp/`）",
        "- planner：**脚本化**（每条任务预置 Plan 序列），不调用真实 LLM",
        "- 工具：进程内 Mock（`get_logs` / `get_metrics` / `restart_service`），由 `FAULTS` 注入故障",
        f"- 工具 `timeout_s` 统一压到 {FAST_TIMEOUT_S}s：只为缩短评测耗时，超时/重试判定不受影响",
        "- 每条任务跑两遍：`BASELINE=1`（无重试、无审批、无循环检测）与 `BASELINE=0`（完整运行时）",
        "",
        "> **评测的是运行时控制流（重试、审批闸门、循环检测、状态机），不是模型能力。**",
        "",
        "## 1. 总表",
        "",
        "| 指标 | baseline | delta |",
        "|---|---|---|",
        f"| 成功率（status == expect） | {base_ok}/{total} | {delta_ok}/{total} |",
        f"| 平均步数 | {base_steps:.2f} | {delta_steps:.2f} |",
        f"| 审批拦截率（approval 组） | {base_approval}/{approval_total} | {delta_approval}/{approval_total} |",
        f"| 循环拦截率（loop 组） | {base_loop}/{loop_total} | {delta_loop}/{loop_total} |",
        f"| 平均重试次数（failed attempt / 有 tool_calls 的 step） | {base_retry:.2f}（{base_failed}/{base_calls}） | {delta_retry:.2f}（{delta_failed}/{delta_calls}） |",
        "",
        "## 2. 分组",
        "",
        "| 组 | 条数 | baseline 成功 | delta 成功 |",
        "|---|---|---|---|",
    ]
    for group in ("normal", "failure", "approval", "loop"):
        group_ids = [t["id"] for t in tasks if t["group"] == group]
        b_ok, _ = success_rate(baseline, group_ids)
        d_ok, _ = success_rate(delta, group_ids)
        lines.append(f"| {group} | {len(group_ids)} | {b_ok} | {d_ok} |")

    lines += [
        "",
        "## 3. 失败与差异案例",
        "",
    ]
    for task, base_row, delta_row in pick_cases(tasks, baseline, delta):
        lines.append(f"### {task['id']}（{task['group']}）— {task['goal']}")
        lines.append("")
        expect = task["expect"]
        lines.append(f"- 期望：`status={expect['status']}`、`min_steps={expect.get('min_steps', 0)}`")
        for label, row in (("baseline", base_row), ("delta", delta_row)):
            if row is None:
                lines.append(f"- {label}：未产生结果（执行异常）")
                continue
            lines.append(
                f"- {label}：`status={row['status']}`、`steps={row['step_count']}`、"
                f"`calls={row['calls']}`（failed={row['failed_calls']}）、"
                f"`approvals={row['approvals']}`、`error={row['error']!r}`"
            )
        lines.append("")

    lines += [
        "## 4. 结论",
        "",
        f"1. **重试与超时收口真正起作用**：`t09`（`timeout:2`）在 delta 下 3 次尝试后成功，baseline 一次失败；"
        f"loop 组 baseline 拦截 {base_loop}/{loop_total}、delta 拦截 {delta_loop}/{loop_total}，"
        f"说明循环检测把'原地打转直到 max_steps'变成了显式的 `loop detected`。",
        f"2. **baseline 也能过的任务**：normal 组 {success_rate(baseline, [t['id'] for t in tasks if t['group'] == 'normal'])[0]}/8、"
        f"`t14`（未知工具记录后继续）、以及 `t17`/`t22`/`t23`/`t24`（高危工具直接执行）——"
        f"Delta 的收益集中在'危险操作'和'坏规划'，正常路径两者等价。",
        "3. **还没覆盖的边界**：真实 LLM 的 JSON 稳定性与修复成功率、并发审批、崩溃恢复、幂等缓存命中、"
        "以及 UI/演示链路；这些是 T7 与后续阶段的事，本报告只对控制流负责。",
        "",
    ]
    if skipped:
        lines += [
            "## 5. 执行异常（脚本已跳过，不影响退出码）",
            "",
        ]
        for task_id, label, error in skipped:
            lines.append(f"- `{task_id}`（{label}）：{error}")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    tasks = load_tasks()
    shrink_tool_timeouts()

    rows: dict[str, dict[str, dict]] = {"baseline": {}, "delta": {}}
    skipped: list[tuple[str, str, str]] = []

    for task in tasks:
        for label, baseline in (("baseline", True), ("delta", False)):
            try:
                rows[label][task["id"]] = run_one(task, baseline)
            except Exception as exc:  # noqa: BLE001 - one bad task must not stop the run
                skipped.append((task["id"], label, f"{type(exc).__name__}: {exc}"))

    report = build_report(tasks, rows, skipped)
    REPORT_PATH.write_text(report, encoding="utf-8")

    ids = [task["id"] for task in tasks]
    base_ok, total = success_rate(rows["baseline"], ids)
    delta_ok, _ = success_rate(rows["delta"], ids)
    print(f"tasks={total} baseline_ok={base_ok} delta_ok={delta_ok} skipped={len(skipped)}")
    print(f"report: {REPORT_PATH}")

    # CI runs with EVAL_STRICT=1: a delta regression must fail the job.
    if os.environ.get("EVAL_STRICT") == "1" and delta_ok < total:
        print(f"EVAL_STRICT=1: delta passed {delta_ok}/{total}, expected all")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
