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
from . import __version__


LOGGER = logging.getLogger("codex_stats_http")
MAX_BODY_BYTES = 1_000_000


def create_server(
    host: str,
    port: int,
    database: StatsDatabase,
    api_key: str,
    completion_notifier: Callable[[str], None] | None = None,
    lifecycle=None,
) -> ThreadingHTTPServer:
    handler = _handler_factory(database, api_key, completion_notifier, lifecycle)
    return ThreadingHTTPServer((host, port), handler)


def _handler_factory(
    database: StatsDatabase,
    api_key: str,
    completion_notifier: Callable[[str], None] | None,
    lifecycle=None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodexStats/0.4.0"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(30)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json(200, {"status": "ok", "time": time.time(), "version": __version__,
                                 "maintenance": bool(lifecycle and lifecycle.blocked())})
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
            path = urlparse(self.path).path
            if path != "/api/v1/enroll" and not self._authorized(admin=path == "/api/v1/control/updates"):
                return
            if path not in ("/api/v1/events", "/api/v1/enroll", "/api/v1/agent/checkin", "/api/v1/control/updates"):
                self._json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("Некорректный размер тела")
                event = json.loads(self.rfile.read(length))
                if not isinstance(event, dict):
                    raise ValueError("Ожидался JSON-объект")
                if path == "/api/v1/enroll":
                    if lifecycle is None:
                        raise ValueError("Enrollment is not configured")
                    self._json(200, lifecycle.enroll(event, self.client_address[0]))
                    return
                if path == "/api/v1/agent/checkin":
                    if lifecycle is None:
                        raise ValueError("Updates are not configured")
                    machine = getattr(self, "device_id", None) or str(event.get("machine_id", ""))
                    if not machine or len(machine) > 100:
                        raise ValueError("Invalid machine identity")
                    self._json(200, lifecycle.checkin(event, machine))
                    return
                if path == "/api/v1/control/updates":
                    if lifecycle is None:
                        raise ValueError("Updates are not configured")
                    self._json(200, lifecycle.control(event))
                    return
                if getattr(self, "device_id", None) and event.get("machine_id") != self.device_id:
                    self._json(403, {"error": "wrong_machine"})
                    return
                with database._lock:
                    if lifecycle and lifecycle.blocked():
                        self._json(503, {"error": "maintenance", "retry": True})
                        return
                    inserted = database.apply_event(event)
                    if inserted and lifecycle:
                        lifecycle.on_event(event)
            except (ValueError, json.JSONDecodeError) as error:
                self._json(400, {"error": "invalid_event", "detail": str(error)})
                return
            except (OSError, RuntimeError, KeyError):
                self._json(503, {"error": "temporarily_unavailable"})
                return
            if inserted and event.get("event_type") == "task_completed" and completion_notifier:
                task = next(
                    (row for row in database.accounting_data()[0] if row["task_id"] == event["task_id"]),
                    None,
                )
                if task:
                    completion_notifier(task_completed_message(task, database))
            self._json(202, {"accepted": True, "duplicate": not inserted})

        def _authorized(self, admin: bool = False) -> bool:
            expected = f"Bearer {api_key}"
            actual = self.headers.get("Authorization", "")
            self.device_id = None
            if hmac.compare_digest(actual, expected):
                return True
            if not admin and lifecycle and actual.startswith("Bearer "):
                self.device_id = lifecycle.authenticate(actual[7:])
                if self.device_id:
                    return True
            self._json(401, {"error": "unauthorized"})
            return False

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
