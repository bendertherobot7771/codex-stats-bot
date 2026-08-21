from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ServerConfig:
    host: str
    port: int
    database_path: Path
    agent_api_key: str
    telegram_bot_token: str | None
    telegram_allowed_chat_ids: set[int]
    notify_completions: bool

    @classmethod
    def from_environment(cls) -> "ServerConfig":
        api_key = os.environ.get("CODEX_STATS_API_KEY", "").strip()
        if not api_key or api_key == "change-me":
            raise ValueError("Задайте надёжный CODEX_STATS_API_KEY")
        return cls(
            host=os.environ.get("CODEX_STATS_HOST", "0.0.0.0"),
            port=int(os.environ.get("CODEX_STATS_PORT", "8765")),
            database_path=Path(os.environ.get("CODEX_STATS_DB", "data/codex-stats.sqlite")),
            agent_api_key=api_key,
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_allowed_chat_ids=_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
            notify_completions=_boolean(os.environ.get("TELEGRAM_NOTIFY_COMPLETIONS", "true")),
        )


def _chat_ids(value: str) -> set[int]:
    result = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result


def _boolean(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}

