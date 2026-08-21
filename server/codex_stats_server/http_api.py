from __future__ import annotations

import hmac
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .database import StatsDatabase
from .reports import stats_payload, task_completed_message


LOGGER = logging.getLogger("codex_stats_http")
MAX_BODY_BYTES = 1_000_000


def create_server(
    host: str,
    port: int,
    database: StatsDatabase,
    api_key: str,
    completion_notifier: Callable[[str], None] | None = None,
) -> ThreadingHTTPServer:
    handler = _handler_factory(database, api_key, completion_notifier)
    return ThreadingHTTPServer((host, port), handler)


def _handler_factory(
    database: StatsDatabase,
    api_key: str,
    completion_notifier: Callable[[str], None] | None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodexStats/0.2.2"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json(200, {"status": "ok", "time": time.time()})
                return
            if not self._authorized():
                return
            if parsed.path == "/api/v1/stats":
                query = parse_qs(parsed.query)
                try:
                    days = int(query.get("days", ["7"])[0])
                except ValueError:
                    days = 7
                self._json(200, stats_payload(database, days))
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            if urlparse(self.path).path != "/api/v1/events":
                self._json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("Некорректный размер тела")
                event = json.loads(self.rfile.read(length))
                if not isinstance(event, dict):
                    raise ValueError("Ожидался JSON-объект")
                inserted = database.apply_event(event)
            except (ValueError, json.JSONDecodeError) as error:
                self._json(400, {"error": "invalid_event", "detail": str(error)})
                return
            if inserted and event.get("event_type") == "task_completed" and completion_notifier:
                task = next(
                    (row for row in database.recent_completed(20) if row["task_id"] == event["task_id"]),
                    None,
                )
                if task:
                    completion_notifier(task_completed_message(task))
            self._json(202, {"accepted": True, "duplicate": not inserted})

        def _authorized(self) -> bool:
            expected = f"Bearer {api_key}"
            actual = self.headers.get("Authorization", "")
            if not hmac.compare_digest(actual, expected):
                self._json(401, {"error": "unauthorized"})
                return False
            return True

        def _json(self, status: int, value: dict[str, Any]) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_string: str, *args: Any) -> None:
            LOGGER.info("%s - %s", self.address_string(), format_string % args)

    return Handler
