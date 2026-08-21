from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class EventQueue:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            values = self._load()
            if not any(item.get("event_id") == event.get("event_id") for item in values):
                values.append(event)
                self._save(values)

    def drain(self, sender: "ServerClient") -> int:
        with self._lock:
            values = self._load()
            sent = 0
            while values:
                try:
                    sender.send(values[0])
                except ServerError:
                    break
                values.pop(0)
                sent += 1
            self._save(values)
            return sent

    def _load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return []

    def _save(self, values: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class ServerError(RuntimeError):
    pass


class ServerClient:
    def __init__(self, server_url: str, api_key: str, timeout: float = 10.0):
        self.url = server_url.rstrip("/") + "/api/v1/events"
        self.api_key = api_key
        self.timeout = timeout

    def send(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=data, method="POST")
        request.add_header("Authorization", f"Bearer {self.api_key}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status not in (200, 201, 202):
                    raise ServerError(f"Сервер вернул HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError) as error:
            raise ServerError(f"Сервер недоступен: {error}") from error

