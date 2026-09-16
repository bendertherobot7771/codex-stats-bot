from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .api import EventQueue, ServerClient, ServerError
from .config import AgentConfig, DEFAULT_QUEUE_PATH, DEFAULT_STATE_PATH
from .models import QuotaSnapshot, TokenUsage, TrackedTask
from .quota import QuotaError, account_fingerprint, fetch_weekly_quota, latest_logged_quota, quota_from_log_payload


LOGGER = logging.getLogger("codex_stats_agent")


class CodexWatcher:
    def __init__(
        self,
        config: AgentConfig,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        queue_path: Path = DEFAULT_QUEUE_PATH,
        client: ServerClient | None = None,
    ):
        self.config = config
        self.codex_home = Path(config.codex_home)
        self.session_root = self.codex_home / "sessions"
        self.state_path = state_path
        self.queue = EventQueue(queue_path)
        self.client = client or ServerClient(config.server_url, config.api_key)
        self.offsets: dict[str, int] = {}
        self.contexts: dict[str, dict[str, Any]] = {}
        self.active_tasks: dict[str, TrackedTask] = {}
        self.latest_quota: QuotaSnapshot | None = None
        self.account_id = account_fingerprint(self.codex_home)
        self.running = True
        self.last_periodic_quota_at = 0.0
        self._load_state()

    def run(self) -> None:
        from .updater import AgentUpdater
        updater = AgentUpdater(self)
        self._reconcile_tasks()
        self.session_root.mkdir(parents=True, exist_ok=True)
        if not self.offsets:
            self._bootstrap_offsets()
        self.latest_quota = latest_logged_quota(self.codex_home)
        self._install_signal_handlers()
        LOGGER.info("Слежение запущено: пользователь=%s, ПК=%s", self.config.user_name, self.config.machine_name)
        while self.running:
            stop_request = updater.directory / "stop-request"
            if stop_request.exists():
                stop_request.unlink()
                self._save_state()
                self.queue.drain(self.client)
                break
            self.poll_once()
            self.queue.drain(self.client)
            try:
                updater.tick()
            except Exception as error:
                LOGGER.warning("Проверка обновления отложена: %s", type(error).__name__)
            time.sleep(self.config.poll_interval_seconds)

    def poll_once(self) -> int:
        handled = 0
        for path in self.session_root.rglob("*.jsonl"):
            handled += self._read_appended(path)
        now = time.time()
        live_tasks = [task for task in self.active_tasks.values() if now - task.last_activity_at <= 600]
        interval = 30 if live_tasks else 300
        if now - self.last_periodic_quota_at >= interval:
            self.last_periodic_quota_at = now
            quota = self._fresh_quota()
            if not live_tasks:
                event = {"event_id": f"{self.config.machine_id}:quota:{int(now)}",
                         "event_type": "quota_snapshot", "task_id": "quota_snapshot",
                         "account_fingerprint": self.account_id, "sent_at": now,
                         "machine_id": self.config.machine_id, "machine_name": self.config.machine_name,
                         "user_name": self.config.user_name, "quota": quota.to_dict()}
                try:
                    self.client.send(event)
                except ServerError:
                    self.queue.append(event)
            for task in live_tasks:
                task.last_quota = quota
                self._emit("task_heartbeat", task, quota=quota,
                           event_id=f"{task.task_id}:poll:{int(now)}")
        self._save_state()
        return handled

    def stop(self, *_: Any) -> None:
        self.running = False

    def _bootstrap_offsets(self) -> None:
        for path in self.session_root.rglob("*.jsonl"):
            try:
                self.offsets[str(path)] = path.stat().st_size
            except OSError:
                continue
        self._save_state()
        LOGGER.info("Первый запуск: старые задания пропущены, отслеживаются только новые события")

    def _read_appended(self, path: Path) -> int:
        key = str(path)
        try:
            size = path.stat().st_size
            offset = min(self.offsets.get(key, 0), size)
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read()
            if not chunk:
                return 0
            complete_end = chunk.rfind(b"\n")
            if complete_end < 0:
                return 0
            complete = chunk[: complete_end + 1]
            self.offsets[key] = offset + complete_end + 1
        except OSError as error:
            LOGGER.debug("Не удалось прочитать %s: %s", path, error)
            return 0

        handled = 0
        for raw in complete.splitlines():
            try:
                item = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            self._handle_item(key, item)
            handled += 1
        return handled

    def _handle_item(self, file_key: str, item: dict[str, Any]) -> None:
        if item.get("type") == "session_meta":
            payload = item.get("payload") or {}
            self.contexts[file_key] = {
                "session_id": str(payload.get("session_id") or payload.get("id") or Path(file_key).stem),
                "thread_id": payload.get("id") or payload.get("session_id"),
                "cwd": payload.get("cwd"),
                "current_turn": None,
            }
            return

        if item.get("type") != "event_msg" or not isinstance(item.get("payload"), dict):
            return
        payload = item["payload"]
        event_type = payload.get("type")
        context = self.contexts.setdefault(
            file_key,
            {"session_id": Path(file_key).stem, "thread_id": None, "cwd": None, "current_turn": None},
        )
        active = self.active_tasks.get(f"{context['session_id']}:{context.get('current_turn')}")
        if active:
            active.last_activity_at = _event_timestamp(None, item.get("timestamp"))

        if event_type == "task_started":
            self._task_started(context, payload, item)
        elif event_type == "token_count":
            self._token_count(context, payload, item)
        elif event_type == "task_complete":
            self._task_completed(context, payload, item)
        elif event_type in ("turn_aborted", "task_aborted"):
            turn = str(payload.get("turn_id") or context.get("current_turn") or "")
            self.active_tasks.pop(f"{context['session_id']}:{turn}", None)
            context["current_turn"] = None

    def _reconcile_tasks(self) -> None:
        """Retire a restored task only with terminal or superseding-turn evidence."""
        for task_id, task in list(self.active_tasks.items()):
            paths = [p for p, c in self.contexts.items()
                     if c.get("session_id") == task.session_id or Path(p).stem == task.session_id]
            terminated = False
            for path in paths:
                try:
                    with Path(path).open(encoding="utf-8") as stream:
                        for line in stream:
                            try:
                                item = json.loads(line)
                            except ValueError:
                                continue
                            payload = item.get("payload") or {}
                            if item.get("type") != "event_msg" or not isinstance(payload, dict):
                                continue
                            kind, turn = payload.get("type"), payload.get("turn_id")
                            if turn == task.turn_id and kind in ("task_complete", "turn_aborted", "task_aborted"):
                                terminated = True
                            if (kind == "task_started" and turn and turn != task.turn_id
                                    and _event_timestamp(payload.get("started_at"), item.get("timestamp")) > task.started_at):
                                terminated = True
                except OSError:
                    continue
            if terminated:
                self.active_tasks.pop(task_id, None)
        self._save_state()

    def _task_started(self, context: dict[str, Any], payload: dict[str, Any], item: dict[str, Any]) -> None:
        turn_id = str(payload.get("turn_id") or "")
        if not turn_id:
            return
        context["current_turn"] = turn_id
        task_id = f"{context['session_id']}:{turn_id}"
        if task_id in self.active_tasks:
            return
        quota = self._fresh_quota()
        task = TrackedTask(
            task_id=task_id,
            turn_id=turn_id,
            session_id=context["session_id"],
            thread_id=context.get("thread_id"),
            cwd=context.get("cwd"),
            started_at=_event_timestamp(payload.get("started_at"), item.get("timestamp")),
            start_quota=quota,
            last_activity_at=_event_timestamp(payload.get("started_at"), item.get("timestamp")),
        )
        self.active_tasks[task_id] = task
        self._emit("task_started", task, quota=quota, event_id=f"{task_id}:start")
        LOGGER.info("Начато задание %s (%s)", turn_id, context.get("cwd") or "без проекта")

    def _token_count(self, context: dict[str, Any], payload: dict[str, Any], item: dict[str, Any]) -> None:
        log_quota = quota_from_log_payload(payload.get("rate_limits"))
        if log_quota:
            log_quota.captured_at = _event_timestamp(None, item.get("timestamp"))
            self.latest_quota = log_quota
        turn_id = context.get("current_turn")
        if not turn_id:
            return
        task_id = f"{context['session_id']}:{turn_id}"
        task = self.active_tasks.get(task_id)
        if not task:
            return
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        task.tokens = TokenUsage.from_codex(info.get("total_token_usage"))
        task.last_quota = log_quota or task.last_quota
        now = time.time()
        heartbeat_interval = max(1.0, self.config.heartbeat_interval_seconds)
        if now - task.last_heartbeat_at >= heartbeat_interval:
            task.last_heartbeat_at = now
            self._emit(
                "task_heartbeat",
                task,
                quota=task.last_quota or task.start_quota,
                event_id=f"{task_id}:heartbeat:{int(now // heartbeat_interval)}",
            )

    def _task_completed(self, context: dict[str, Any], payload: dict[str, Any], item: dict[str, Any]) -> None:
        turn_id = str(payload.get("turn_id") or context.get("current_turn") or "")
        if not turn_id:
            return
        task_id = f"{context['session_id']}:{turn_id}"
        task = self.active_tasks.pop(task_id, None)
        context["current_turn"] = None
        if not task:
            return
        quota = self._fresh_quota(fallback=task.last_quota or task.start_quota)
        finished_at = _event_timestamp(payload.get("completed_at"), item.get("timestamp"))
        self._emit(
            "task_completed",
            task,
            quota=quota,
            finished_at=finished_at,
            event_id=f"{task_id}:finish",
        )
        LOGGER.info(
            "Завершено задание %s: %s токенов, недельный лимит %.2f%%",
            turn_id,
            task.tokens.total_tokens,
            quota.used_percent if quota.used_percent is not None else -1,
        )

    def _fresh_quota(self, fallback: QuotaSnapshot | None = None) -> QuotaSnapshot:
        try:
            snapshot = fetch_weekly_quota(self.codex_home)
            self.latest_quota = snapshot
            return snapshot
        except QuotaError as error:
            LOGGER.warning("Свежий лимит недоступен, используется журнал Codex: %s", error)
            logged = latest_logged_quota(self.codex_home) or self.latest_quota or fallback
            return logged or QuotaSnapshot(captured_at=time.time())

    def _emit(
        self,
        event_type: str,
        task: TrackedTask,
        *,
        quota: QuotaSnapshot,
        event_id: str,
        finished_at: float | None = None,
    ) -> None:
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "sent_at": time.time(),
            "account_fingerprint": self.account_id,
            "user_name": self.config.user_name,
            "machine_id": self.config.machine_id,
            "machine_name": self.config.machine_name,
            "task_id": task.task_id,
            "turn_id": task.turn_id,
            "session_id": task.session_id,
            "thread_id": task.thread_id,
            "cwd": task.cwd,
            "started_at": task.started_at,
            "finished_at": finished_at,
            "quota": quota.to_dict(),
            "start_quota": task.start_quota.to_dict(),
            "tokens": task.tokens.to_dict(),
        }
        try:
            self.client.send(event)
        except ServerError as error:
            LOGGER.warning("Событие поставлено в локальную очередь: %s", error)
            self.queue.append(event)

    def _load_state(self) -> None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        self.offsets = {str(key): int(offset) for key, offset in value.get("offsets", {}).items()}
        self.contexts = value.get("contexts", {})
        self.active_tasks = {
            key: TrackedTask.from_dict(task) for key, task in value.get("active_tasks", {}).items()
        }

    def _save_state(self) -> None:
        value = {
            "offsets": self.offsets,
            "contexts": self.contexts,
            "active_tasks": {key: task.to_dict() for key, task in self.active_tasks.items()},
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _install_signal_handlers(self) -> None:
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, self.stop)
                except (OSError, ValueError):
                    pass


def _event_timestamp(epoch_value: Any, iso_value: Any) -> float:
    try:
        if epoch_value is not None:
            return float(epoch_value)
    except (TypeError, ValueError):
        pass
    if isinstance(iso_value, str):
        try:
            return datetime.fromisoformat(iso_value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()
