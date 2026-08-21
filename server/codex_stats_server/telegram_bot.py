from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .database import StatsDatabase
from .reports import telegram_active, telegram_last, telegram_stats


LOGGER = logging.getLogger("codex_stats_telegram")


class TelegramBot:
    def __init__(self, token: str, allowed_chat_ids: set[int], database: StatsDatabase):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.allowed_chat_ids = allowed_chat_ids
        self.database = database
        self.outgoing: queue.Queue[tuple[int, str]] = queue.Queue()
        self.running = True
        self.thread = threading.Thread(target=self._run, name="telegram-bot", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.running = False

    def notify_registered(self, text: str) -> None:
        for chat_id in self.database.registered_chats():
            if chat_id in self.allowed_chat_ids:
                self.outgoing.put((chat_id, text))

    def _run(self) -> None:
        offset = int(self.database.get_setting("telegram_offset", "0") or 0)
        while self.running:
            self._flush_outgoing()
            try:
                updates = self._request("getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": ["message"]})
                for update in updates.get("result", []):
                    offset = max(offset, int(update["update_id"]) + 1)
                    self.database.set_setting("telegram_offset", str(offset))
                    self._handle_update(update)
            except Exception as error:  # network loop must stay alive
                LOGGER.warning("Ошибка Telegram polling: %s", error)
                threading.Event().wait(3)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = int(chat.get("id", 0))
        text = str(message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if not chat_id:
            return
        if command == "/whoami":
            self.outgoing.put((chat_id, f"Telegram chat ID: {chat_id}"))
            return
        if chat_id not in self.allowed_chat_ids:
            self.outgoing.put((chat_id, f"Доступ запрещён. Ваш chat ID: {chat_id}"))
            return
        if command == "/start":
            self.database.register_chat(chat_id)
            response = "Уведомления Codex Stats включены.\n" + _help()
        elif command == "/stats":
            response = telegram_stats(self.database)
        elif command == "/active":
            response = telegram_active(self.database)
        elif command == "/last":
            response = telegram_last(self.database)
        else:
            response = _help()
        self.outgoing.put((chat_id, response))

    def _flush_outgoing(self) -> None:
        while True:
            try:
                chat_id, text = self.outgoing.get_nowait()
            except queue.Empty:
                return
            try:
                self._request("sendMessage", {"chat_id": chat_id, "text": text})
            except Exception as error:
                LOGGER.warning("Не удалось отправить сообщение Telegram: %s", error)

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Telegram API недоступен: {error}") from error
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API отклонил запрос: {result}")
        return result


def _help() -> str:
    return (
        "Команды:\n"
        "/stats — статистика за 7 дней\n"
        "/active — активные задания\n"
        "/last — последние задания\n"
        "/whoami — показать Telegram chat ID\n"
        "/help — эта справка"
    )

