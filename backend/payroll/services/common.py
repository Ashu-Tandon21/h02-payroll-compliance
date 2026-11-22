"""Shared pieces for the service layer: the error type for broken business rules, and the audit trail."""
from __future__ import annotations

from ..models import PayrollRun, RunEvent


class RuleViolation(Exception):
    """A request that breaks a business rule (wrong state, four-eyes, stale data...). Never retried."""

    def __init__(self, message: str, code: str = "rule_violation"):
        super().__init__(message)
        self.message = message
        self.code = code


def actor_name(user) -> str:
    if user is None:
        return "system"
    full = user.get_full_name() if hasattr(user, "get_full_name") else ""
    return f"{full} ({user.username})" if full else user.username


def record(run: PayrollRun, user, action: str, message: str, **detail) -> RunEvent:
    return RunEvent.objects.create(run=run, actor=actor_name(user), action=action, message=message, detail=detail)


def is_in_group(user, group: str) -> bool:
    return bool(user) and (user.is_superuser or user.groups.filter(name=group).exists())


def require_role(user, group: str, what: str):
    if not is_in_group(user, group):
        raise RuleViolation(f"Only a {group} can {what}.", code="forbidden")
