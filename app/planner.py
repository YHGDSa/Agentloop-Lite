"""The planner: one LLM call that must return a JSON plan.

The model is asked for exactly two shapes and nothing else. Anything that is
not one of them gets one repair request; a second bad answer raises
``PlanningError`` so the executor stops instead of looping on garbage.

Tests never reach the network: they monkeypatch ``app.planner.plan`` (see
``tests/test_runs.py``). The real call is only used by the manual demo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from .config import settings
from .errors import PlanningError
from .registry import ToolSpec

PlanAction = Literal["call_tool", "finish"]

SYSTEM_PROMPT = (
    "You are the planner of a small ops agent. You see the goal, the steps "
    "already taken and the tools you may call.\n"
    "Reply with exactly one JSON object and nothing else: no prose, no markdown "
    "fence, no trailing text.\n"
    "To call a tool:\n"
    '{"action":"call_tool","tool":"<tool name>","args":{...},"reason":"<short reason>"}\n'
    "To end the run:\n"
    '{"action":"finish","summary":"<what was done>"}\n'
    "Only use tools from the list, and pass exactly the arguments their "
    "input_schema allows."
)

REPAIR_PROMPT = (
    "That answer could not be used: {error}. "
    "Reply again with exactly one JSON object and nothing else."
)


@dataclass
class Plan:
    """One planner decision: either call a tool once, or finish the run."""

    action: PlanAction
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    summary: str | None = None


def build_messages(
    goal: str, history: list[Any], tools: list[ToolSpec]
) -> list[dict[str, str]]:
    """Render goal + history + tool contracts into a two-message chat prompt."""
    tool_lines = [
        f"- {spec.name} (risk={spec.risk}): {spec.description}\n"
        f"  input_schema: {json.dumps(spec.input_schema, ensure_ascii=False)}"
        for spec in tools
    ]
    step_lines = []
    for step in history:
        observation = (step["observation"] or "").replace("\n", " ")[:300]
        step_lines.append(
            f"{step['seq']}. {step['tool_name'] or '-'} args={step['args_json'] or '{}'} "
            f"-> {observation} (error_type={step['error_type'] or 'none'})"
        )

    tool_block = "\n".join(tool_lines)
    step_block = "\n".join(step_lines) if step_lines else "none"
    user = (
        f"Goal: {goal}\n\n"
        f"Tools:\n{tool_block}\n\n"
        f"Steps so far:\n{step_block}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def strip_code_fence(text: str) -> str:
    """Drop a leading ```/```json line and a trailing ``` line if present."""
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_plan(text: str) -> Plan:
    """Turn one model answer into a Plan, or raise ValueError with the reason."""
    payload = json.loads(strip_code_fence(text))
    if not isinstance(payload, dict):
        raise ValueError("plan must be a JSON object")

    action = payload.get("action")
    if action == "call_tool":
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ValueError("call_tool needs a non-empty 'tool' string")
        args = payload.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("call_tool 'args' must be a JSON object")
        reason = payload.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError("call_tool 'reason' must be a string")
        return Plan(action="call_tool", tool=tool, args=args, reason=reason)

    if action == "finish":
        summary = payload.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("finish 'summary' must be a string")
        return Plan(action="finish", summary=summary)

    raise ValueError(f"unknown action {action!r}, expected 'call_tool' or 'finish'")


async def _chat(messages: list[dict[str, str]]) -> str:
    """POST one OpenAI-compatible chat completion and return its text."""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 512,
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PlanningError(f"planner request to {url} failed: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise PlanningError(f"planner response was not JSON: {exc}") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise PlanningError(
            f"planner response had no choices[0].message.content: {exc}"
        ) from exc
    if not isinstance(content, str):
        raise PlanningError("planner response content was not text")
    return content


async def plan(goal: str, history: list[Any], tools: list[ToolSpec]) -> Plan:
    """Ask the model for one Plan, retrying once with a repair request."""
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        raise PlanningError(
            "LLM not configured: set LLM_BASE_URL/LLM_API_KEY/LLM_MODEL"
        )

    messages = build_messages(goal, history, tools)
    answer = await _chat(messages)
    try:
        return parse_plan(answer)
    except ValueError as first_error:
        repair = [
            *messages,
            {"role": "assistant", "content": answer},
            {"role": "user", "content": REPAIR_PROMPT.format(error=first_error)},
        ]
        second = await _chat(repair)
        try:
            return parse_plan(second)
        except ValueError as second_error:
            raise PlanningError(
                f"planner did not return a usable plan after one repair: {second_error}"
            ) from second_error
