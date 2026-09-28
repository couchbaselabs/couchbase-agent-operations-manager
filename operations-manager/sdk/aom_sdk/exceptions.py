"""Exceptions raised by the Couchbase Agent Operations Manager SDK.

Every error the gateway can return over HTTP is mapped to one of these, so
calling code can `except AOMAuthenticationError` instead of inspecting an
HTTP status code by hand.
"""
from __future__ import annotations

from typing import Optional


class AOMError(Exception):
    """Base class for every error this SDK raises."""

    def __init__(self, message: str, status_code: Optional[int] = None, detail: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail if detail is not None else message

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        if self.status_code is not None:
            return f"[{self.status_code}] {super().__str__()}"
        return super().__str__()


class AOMConnectionError(AOMError):
    """Could not reach the operations manager at all (network/DNS/timeout)."""


class AOMAuthenticationError(AOMError):
    """401 - missing or invalid `Authorization: Bearer <api_key>` header."""


class AOMAuthorizationError(AOMError):
    """403 - authenticated, but this role is not allowed to do that."""


class AOMNotFoundError(AOMError):
    """404 - the tool/server/entry does not exist in the vetted catalog."""


class AOMServerError(AOMError):
    """5xx - the operations manager, or a downstream MCP server, failed."""


class AOMRateLimitError(AOMError):
    """429 - a rate limit or budget refused this call.

    `retry_after` is the number of seconds until the window this call was
    counted against rolls over, taken from the response's Retry-After
    header. `limit` names which ceiling was hit (requests, tool_calls,
    tool_calls_per_run, tokens, spend), so a caller can back off on a
    per-minute rate and give up on a daily spend budget - waiting out a
    spend ceiling means sleeping for hours.
    """

    def __init__(self, message, status_code=None, detail=None, retry_after=None, limit=None):
        super().__init__(message, status_code, detail)
        self.retry_after = retry_after
        self.limit = limit


class AOMGuardrailError(AOMError):
    """400 - the guardrails policy refused this prompt or these tool
    arguments, for personal data or a prompt-injection signal.

    Distinct from a validation error on purpose: a malformed request is a
    bug to fix, and a guardrail refusal is a policy decision to respect.
    Retrying the same payload will not help either way, but only one of
    them means the payload was the problem.
    """


class AOMScopeError(AOMAuthorizationError):
    """403 - this agent is scoped to a subset of its role's tools and the
    requested one is outside it.

    A subclass of AOMAuthorizationError so existing handlers keep working,
    and separate so a caller can tell "this role may never do that" from
    "this particular agent was narrowed".
    """


class AOMApprovalError(AOMAuthorizationError):
    """403 - a call needing human approval was refused, expired, already
    used, or presented with arguments it was not approved for."""


class AOMTimeoutError(AOMError):
    """504 - the request exceeded the appliance's own request timeout."""
