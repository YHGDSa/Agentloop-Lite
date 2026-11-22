"""The tool registry: the single source of truth for what the agent may call.

``TOOLS`` drives ``GET /tools``, the planner's prompt (T3) and the argument
check, so a tool cannot exist in one place and be described in another.
Every schema sets ``additionalProperties: false``: a model that invents an
extra field gets rejected instead of having it silently ignored.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import jsonschema

from . import tools
from .errors import ValidationError

RiskLevel = Literal["read", "write", "high"]

SERVICES = ["api", "worker", "db"]


@dataclass(frozen=True)
class ToolSpec:
    """Everything the runtime needs to plan, validate, gate and audit a tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    risk: RiskLevel
    idempotent: bool
    timeout_s: float
    fn: Callable[..., Awaitable[dict[str, Any]]]


GET_LOGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "enum": SERVICES, "description": "Target service."},
        "minutes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 60,
            "description": "How far back to read, in minutes.",
        },
    },
    "required": ["service", "minutes"],
    "additionalProperties": False,
}

GET_METRICS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "enum": SERVICES, "description": "Target service."},
        "metric": {
            "type": "string",
            "enum": ["cpu", "memory", "qps"],
            "description": "Which metric to read.",
        },
    },
    "required": ["service", "metric"],
    "additionalProperties": False,
}

RESTART_SERVICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "enum": SERVICES, "description": "Target service."}
    },
    "required": ["service"],
    "additionalProperties": False,
}

TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="get_logs",
            description="Read the last N minutes of logs for one service.",
            input_schema=GET_LOGS_SCHEMA,
            risk="read",
            idempotent=True,
            timeout_s=5.0,
            fn=tools.get_logs,
        ),
        ToolSpec(
            name="get_metrics",
            description="Read one metric (cpu, memory or qps) for one service.",
            input_schema=GET_METRICS_SCHEMA,
            risk="read",
            idempotent=True,
            timeout_s=5.0,
            fn=tools.get_metrics,
        ),
        ToolSpec(
            name="restart_service",
            description="Restart one service; high risk, so it needs human approval.",
            input_schema=RESTART_SERVICE_SCHEMA,
            risk="high",
            idempotent=False,
            timeout_s=10.0,
            fn=tools.restart_service,
        ),
    )
}


def get_tool(name: str) -> ToolSpec | None:
    """Look a tool up by name, or None when the model made the name up."""
    return TOOLS.get(name)


def require_tool(name: str) -> ToolSpec:
    """Like ``get_tool`` but turns an unknown name into ``ValidationError``."""
    spec = TOOLS.get(name)
    if spec is None:
        known = ", ".join(sorted(TOOLS))
        raise ValidationError(f"unknown tool {name!r} (known: {known})")
    return spec


def validate_args(spec: ToolSpec | str, args: dict[str, Any]) -> None:
    """Raise ``ValidationError`` unless ``args`` match the tool's JSON Schema.

    A tool name is accepted as well, so a caller holding only the planner's
    string gets the same unknown-tool and schema errors from one place.
    """
    resolved = require_tool(spec) if isinstance(spec, str) else spec
    try:
        jsonschema.validate(instance=args, schema=resolved.input_schema)
    except jsonschema.ValidationError as exc:
        where = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise ValidationError(f"{resolved.name}: {exc.message} (at {where})") from exc
