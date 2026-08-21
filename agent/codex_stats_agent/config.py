from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "CodexStatsAgent"
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_STATE_PATH = APP_DIR / "state.json"
DEFAULT_QUEUE_PATH = APP_DIR / "queue.json"


@dataclass(slots=True)
class AgentConfig:
    server_url: str
    api_key: str
    user_name: str
    machine_id: str
    machine_name: str
    codex_home: str
    poll_interval_seconds: float = 1.0
    heartbeat_interval_seconds: float = 15.0

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "AgentConfig":
        with path.open("r", encoding="utf-8") as stream:
            return cls(**json.load(stream))

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def create_config(server_url: str, api_key: str, user_name: str, *, codex_home: str | None = None) -> AgentConfig:
    return AgentConfig(
        server_url=server_url.rstrip("/"),
        api_key=api_key,
        user_name=user_name,
        machine_id=str(uuid.uuid4()),
        machine_name=platform.node() or "unknown-windows-pc",
        codex_home=codex_home or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
    )

