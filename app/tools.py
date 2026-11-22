"""Three async mock tools and the fault-injection switch that tests drive.

Nothing here talks to a network or a real service: the payloads are fixed so a
trace is readable and a test can assert on it. ``FAULTS`` lets a test force one
failure mode per tool without touching the code under test; each tool keeps its
own call counter so ``timeout:2`` can mean "fail the first two attempts".
"""

from __future__ import annotations

import asyncio
from typing import Any

from .errors import PermissionDenied, RateLimited, UpstreamError

# Test-only switch, e.g. FAULTS["get_logs"] = "timeout:2".
FAULTS: dict[str, str] = {}

_ATTEMPTS: dict[str, int] = {}

_LOG_LINES: dict[str, list[str]] = {
    "api": [
        "2026-11-21T02:58:01Z INFO  api listening on :8080",
        "2026-11-21T02:59:12Z WARN  api p99=1840ms threshold=1200ms",
        "2026-11-21T02:59:40Z ERROR api upstream=payments status=503 rate=0.14",
        "2026-11-21T03:00:02Z INFO  api circuit breaker half-open",
    ],
    "worker": [
        "2026-11-21T02:57:30Z INFO  worker queue=demo depth=37",
        "2026-11-21T02:59:00Z WARN  worker job=reindex took 42s budget=30s",
        "2026-11-21T03:00:00Z INFO  worker job=reindex finished",
    ],
    "db": [
        "2026-11-21T02:56:00Z INFO  db checkpoint complete wal=12MB",
        "2026-11-21T02:57:20Z WARN  db slow query 1310ms table=steps",
        "2026-11-21T02:58:45Z WARN  db connections 84/100",
        "2026-11-21T02:59:30Z ERROR db disk 91% used",
        "2026-11-21T03:00:10Z INFO  db vacuum skipped, window closed",
    ],
}

_METRIC_VALUES: dict[str, float] = {"cpu": 0.82, "memory": 0.61, "qps": 412.0}


def reset_attempts() -> None:
    """Drop every per-tool counter; tests call this before each case."""
    _ATTEMPTS.clear()


def _record_attempt(tool: str) -> int:
    """Count this call (1-based) so a ``name:count`` fault can expire."""
    _ATTEMPTS[tool] = _ATTEMPTS.get(tool, 0) + 1
    return _ATTEMPTS[tool]


async def _apply_fault(tool: str, attempt: int) -> None:
    """Raise or hang according to ``FAULTS[tool]``; return when nothing is set.

    The fault value is either a bare name (every call fails) or ``name:count``
    (only the first ``count`` calls fail, the next one succeeds).
    """
    fault = FAULTS.get(tool)
    if not fault:
        return

    name, _, limit = fault.partition(":")
    if limit and attempt > int(limit):
        return

    if name == "timeout":
        # Sleep past any caller timeout; whoever wraps this in wait_for cancels us.
        await asyncio.sleep(999)
    elif name == "rate_limited":
        raise RateLimited(f"{tool}: upstream asked us to back off (attempt {attempt})")
    elif name == "permission":
        raise PermissionDenied(f"{tool}: caller lacks the required permission")
    elif name == "upstream":
        raise UpstreamError(f"{tool}: dependency returned an invalid response")
    else:
        raise ValueError(f"unknown fault {fault!r} configured for {tool}")


async def get_logs(service: str, minutes: int) -> dict[str, Any]:
    """Return the last ``minutes`` of mock log lines for one service."""
    attempt = _record_attempt("get_logs")
    await _apply_fault("get_logs", attempt)
    return {
        "service": service,
        "minutes": minutes,
        "lines": list(_LOG_LINES[service]),
    }


async def get_metrics(service: str, metric: str) -> dict[str, Any]:
    """Return one mock metric reading for one service."""
    attempt = _record_attempt("get_metrics")
    await _apply_fault("get_metrics", attempt)
    return {
        "service": service,
        "metric": metric,
        "value": _METRIC_VALUES[metric],
        "unit": "ratio" if metric != "qps" else "rps",
    }


async def restart_service(service: str) -> dict[str, Any]:
    """Pretend to restart one service; the runtime must ask a human first."""
    attempt = _record_attempt("restart_service")
    await _apply_fault("restart_service", attempt)
    return {
        "service": service,
        "restarted": True,
        "downtime_ms": 850,
        "note": f"{service} restarted by the mock backend",
    }
