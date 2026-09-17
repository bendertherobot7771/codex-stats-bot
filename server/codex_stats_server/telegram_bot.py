from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.error
import urllib.request
from typing import Any

from .database import StatsDatabase
from .reports import telegram_active, telegram_last, telegram_week, telegram_weeks


LOGGER = logging.getLogger("codex_stats_telegram")


class TelegramBot:
    def __init__(
        self,
        token: str,
        admin_chat_ids: set[int],
        initial_viewer_chat_ids: set[int],
        database: StatsDatabase,
        lifecycle=None,
    ):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.database = database
        self.lifecycle = lifecycle
        for chat_id in admin_chat_ids:
            database.ensure_bot_user(chat_id, "admin", "Администратор")
        for chat_id in initial_viewer_chat_ids - admin_chat_ids:
            database.ensure_bot_user(chat_id, "viewer")
        self.outgoing: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.running = True
        self.thread = threading.Thread(target=self._run, name="telegram-bot", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.running = False

    def notify_registered(self, text: str) -> None:
        for chat_id in self.database.notification_chats():
            self._send(chat_id, text)

    def _run(self) -> None:
        offset = int(self.database.get_setting("telegram_offset", "0") or 0)
        try:
            self._request("setMyCommands", {"commands": _telegram_commands()})
        except Exception as error:
            LOGGER.warning("Не удалось обновить меню команд Telegram: %s", error)
        while self.running:
            self._flush_outgoing()
            try:
                updates = self._request(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 20,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in updates.get("result", []):
                    offset = max(offset, int(update["update_id"]) + 1)
                    self.database.set_setting("telegram_offset", str(offset))
                    self._handle_update(update)
            except Exception as error:  # network loop must stay alive
                LOGGER.warning("Ошибка Telegram polling: %s", error)
                threading.Event().wait(3)

    def _handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback(callback)
            return
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = int(chat.get("id", 0))
        text = str(message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if not chat_id:
            return
        if command == "/whoami":
            self._send(chat_id, f"Telegram chat ID: {chat_id}")
            return
        user = self.database.bot_user(chat_id)
        if not user:
            self._send(chat_id, f"Доступ запрещён. Ваш chat ID: {chat_id}")
            return

        if command == "/start":
            self.database.register_chat(chat_id)
            response, markup = "Уведомления Codex Stats включены.\n" + _help(user["role"]), None
        elif command == "/stats":
            response, markup = telegram_week(self.database), None
        elif command == "/weeks":
            response, markup = telegram_weeks(self.database, _integer_argument(text, 1))
        elif command == "/week":
            response, markup = telegram_week(self.database, _integer_argument(text, 1)), None
        elif command == "/active":
            response, markup = telegram_active(self.database), None
        elif command == "/last":
            response, markup = telegram_last(self.database), None
        elif command == "/users" and user["role"] == "admin":
            response, markup = self._users_message()
        elif command in ("/install", "/install_local") or (command == "/addpc" and user["role"] == "admin"):
            markup = None
            arguments = text.split()[1:]
            if command == "/install_local" and not arguments:
                arguments = ["local"]
            if chat_id < 0 or chat.get("type", "private") != "private":
                response = "Для получения личного кода установки напишите /install боту в личные сообщения."
            elif arguments not in ([], ["local"]):
                response = "Формат: /install — интернет; /install local — домашняя сеть сервера."
            elif not self.lifecycle:
                response = "Установка пока не настроена. Обратитесь к администратору."
            else:
                response = self.lifecycle.instructions(chat_id, local=arguments == ["local"])
        elif command == "/updates" and self.lifecycle:
            response, markup = self.lifecycle.description(), None
        elif command == "/adduser" and user["role"] == "admin":
            response, markup = self._add_user(chat_id, text), None
        elif command == "/removeuser" and user["role"] == "admin":
            response, markup = self._remove_user(text), None
        else:
            response, markup = _help(user["role"]), None
        self._send(chat_id, response, markup)

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = int(chat.get("id", 0))
        data = str(callback.get("data") or "")
        user = self.database.bot_user(chat_id)
        if not user:
            self.outgoing.put(("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Нет доступа"}))
            return
        if data.startswith("week:"):
            index = _safe_int(data.removeprefix("week:"), 1)
            markup = {"inline_keyboard": [[{"text": "← К списку", "callback_data": "weeks:1"}]]}
            self._edit_or_send(message, chat_id, telegram_week(self.database, index), markup)
        elif data.startswith("weeks:"):
            page = _safe_int(data.removeprefix("weeks:"), 1)
            text, markup = telegram_weeks(self.database, page)
            self._edit_or_send(message, chat_id, text, markup)
        elif data.startswith("remove:") and user["role"] == "admin":
            target = _safe_int(data.removeprefix("remove:"), 0)
            result = self.database.disable_bot_user(target)
            text, markup = self._users_message()
            prefix = "Участник удалён.\n" if result else "Удалить администратора нельзя.\n"
            self._edit_or_send(message, chat_id, prefix + text, markup)
        self.outgoing.put(("answerCallbackQuery", {"callback_query_id": callback_id}))

    def _add_user(self, admin_id: int, text: str) -> str:
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            return "Формат: /adduser CHAT_ID [имя]"
        chat_id = int(parts[1])
        name = parts[2].strip()[:200] if len(parts) > 2 else None
        self.database.ensure_bot_user(chat_id, "viewer", name, admin_id)
        return f"Участник {chat_id} добавлен. Теперь он может открыть /stats и /weeks, подключить свой ПК через /install."

    def _remove_user(self, text: str) -> str:
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            return "Формат: /removeuser CHAT_ID"
        if self.database.disable_bot_user(int(parts[1])):
            return "Участник удалён."
        return "Участник не найден или является администратором."

    def _users_message(self) -> tuple[str, dict[str, Any] | None]:
        users = self.database.bot_users()
        lines = ["Доступ к боту:"]
        buttons = []
        for user in users:
            name = f" · {user['display_name']}" if user.get("display_name") else ""
            lines.append(f"• {user['chat_id']} · {user['role']}{name}")
            if user["role"] != "admin":
                buttons.append(
                    [
                        {
                            "text": f"Удалить {user['chat_id']}",
                            "callback_data": f"remove:{user['chat_id']}",
                        }
                    ]
                )
        return "\n".join(lines), ({"inline_keyboard": buttons} if buttons else None)

    def _edit_or_send(
        self,
        message: dict[str, Any],
        chat_id: int,
        text: str,
        markup: dict[str, Any] | None = None,
    ) -> None:
        message_id = message.get("message_id")
        if message_id:
            payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
            if markup:
                payload["reply_markup"] = markup
            self.outgoing.put(("editMessageText", payload))
        else:
            self._send(chat_id, text, markup)

    def _send(self, chat_id: int, text: str, markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if markup:
            payload["reply_markup"] = markup
        self.outgoing.put(("sendMessage", payload))

    def _flush_outgoing(self) -> None:
        while True:
            try:
                method, payload = self.outgoing.get_nowait()
            except queue.Empty:
                return
            try:
                self._request(method, payload)
            except Exception as error:
                LOGGER.warning("Не удалось выполнить запрос Telegram: %s", error)

    def announce_maintenance(self, text: str) -> None:
        # Do not begin maintenance if Telegram could not deliver the warning.
        chats = self.database.notification_chats()
        if not chats:
            chats = [u["chat_id"] for u in self.database.bot_users() if u["role"] == "admin"]
        if not chats:
            raise RuntimeError("No maintenance notification recipient")
        for chat in chats:
            self._request("sendMessage", {"chat_id": chat, "text": text})

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


def _help(role: str) -> str:
    lines = [
        "Команды:",
        "/stats — текущая неделя по компьютерам",
        "/weeks — вся недельная история",
        "/week N — выбранная неделя",
        "/active — активные задания",
        "/last — последние задания",
        "/whoami — показать Telegram chat ID",
        "/updates — версии ПК и состояние автообновления",
        "/install — код и установка Windows-ПК через интернет",
        "/install_local — установка ПК в локальной сети сервера (также /install local)",
    ]
    if role == "admin":
        lines.extend(
            [
                "/users — участники и кнопки удаления",
                "/addpc — код и инструкция подключения Windows-ПК",
                "/adduser CHAT_ID [имя] — дать доступ",
                "/removeuser CHAT_ID — отозвать доступ",
            ]
        )
    return "\n".join(lines)


def _telegram_commands() -> list[dict[str, str]]:
    return [
        {"command": "stats", "description": "текущая неделя по компьютерам"},
        {"command": "weeks", "description": "вся недельная история"},
        {"command": "active", "description": "активные задания"},
        {"command": "last", "description": "последние задания"},
        {"command": "install", "description": "Установить агент — интернет (глобальная сеть)"},
        {"command": "install_local", "description": "Установить агент — локальная сеть"},
        {"command": "users", "description": "управление участниками (админ)"},
        {"command": "addpc", "description": "подключить Windows-ПК (админ)"},
        {"command": "updates", "description": "версии и автообновление"},
        {"command": "whoami", "description": "показать Telegram chat ID"},
        {"command": "help", "description": "справка"},
    ]


def _integer_argument(text: str, default: int) -> int:
    parts = text.split(maxsplit=1)
    return _safe_int(parts[1], default) if len(parts) > 1 else default


def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:
        return default
