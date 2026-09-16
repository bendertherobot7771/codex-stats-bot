from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class QuotaSnapshot:
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: int | None = None
    source: str = "unavailable"
    captured_at: float | None = None
    reset_credits_count: int | None = None
    reset_credits_expire_at: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_codex(cls, value: dict[str, Any] | None) -> "TokenUsage":
        value = value or {}
        return cls(
            input_tokens=_integer(value.get("input_tokens")),
            cached_input_tokens=_integer(value.get("cached_input_tokens")),
            output_tokens=_integer(value.get("output_tokens")),
            reasoning_output_tokens=_integer(value.get("reasoning_output_tokens")),
            total_tokens=_integer(value.get("total_tokens")),
        )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(slots=True)
class TrackedTask:
    task_id: str
    turn_id: str
    session_id: str
    thread_id: str | None
    cwd: str | None
    started_at: float
    start_quota: QuotaSnapshot
    tokens: TokenUsage = field(default_factory=TokenUsage)
    last_quota: QuotaSnapshot | None = None
    last_heartbeat_at: float = 0
    last_activity_at: float = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TrackedTask":
        return cls(
            task_id=value["task_id"],
            turn_id=value["turn_id"],
            session_id=value["session_id"],
            thread_id=value.get("thread_id"),
            cwd=value.get("cwd"),
            started_at=float(value["started_at"]),
            start_quota=QuotaSnapshot(**value.get("start_quota", {})),
            tokens=TokenUsage(**value.get("tokens", {})),
            last_quota=(QuotaSnapshot(**value["last_quota"]) if value.get("last_quota") else None),
            last_heartbeat_at=float(value.get("last_heartbeat_at", 0)),
            last_activity_at=float(value.get("last_activity_at", value.get("last_heartbeat_at", value.get("started_at", 0)))),
        )


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
