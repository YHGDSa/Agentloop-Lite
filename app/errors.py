"""Exception types shared by the state machine, the registry and the tools.

They live in one module so the HTTP layer can map them to status codes later
(T3/T4) without importing the modules that raise them.
"""

from __future__ import annotations


class AgentLoopError(Exception):
    """Base class for every error this service raises on purpose."""


class InvalidTransition(AgentLoopError):
    """A run was asked to move to a status the state machine forbids."""


class RunNotFound(AgentLoopError):
    """No run exists for the given id."""


class ValidationError(AgentLoopError):
    """Tool arguments did not match the tool's JSON Schema, or the tool is unknown."""


class PlanningError(AgentLoopError):
    """The planner could not produce a usable plan: no config, bad transport, bad JSON."""


class ToolError(AgentLoopError):
    """Base class for failures reported by a tool itself, not by its caller."""


class RateLimited(ToolError):
    """The upstream asked us to back off; retrying soon is pointless."""


class PermissionDenied(ToolError):
    """The caller is not allowed to run this tool against this target."""


class UpstreamError(ToolError):
    """A dependency failed while serving the tool call."""


class ToolTimeout(ToolError):
    """The tool did not answer within the ToolSpec timeout."""
