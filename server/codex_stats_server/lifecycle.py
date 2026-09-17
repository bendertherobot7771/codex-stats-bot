from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from common.releases import PROTOCOL, verify, version
from . import __version__


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Lifecycle:
    def __init__(self, database, public_url: str, cache: Path, announce=None):
        self.db, self.public_url, self.cache = database, public_url.rstrip("/"), cache
        self.announce = announce or (lambda text: None)
        self.enrollment_attempts = {}
        with self.db._lock, self.db._connection as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS enrollment_codes (
                    digest TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    machine_id TEXT PRIMARY KEY, machine_name TEXT NOT NULL, token_hash TEXT UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1, seen REAL NOT NULL DEFAULT 0,
                    version TEXT NOT NULL DEFAULT '0.0.0', protocol INTEGER NOT NULL DEFAULT 1,
                    busy INTEGER NOT NULL DEFAULT 1, idle_since REAL, queued INTEGER NOT NULL DEFAULT 1,
                    update_error TEXT NOT NULL DEFAULT '', update_warning_at REAL,
                    warning_version TEXT);
            """)

    def state(self) -> dict:
        return json.loads(self.db.get_setting("update_plan", "{}"))

    def save(self, state: dict) -> None:
        self.db.set_setting("update_plan", json.dumps(state))

    def code(self, admin: int) -> str:
        code = secrets.token_urlsafe(18)
        with self.db._lock, self.db._connection as connection:
            connection.execute("DELETE FROM enrollment_codes WHERE expires < ?", (time.time(),))
            connection.execute("INSERT INTO enrollment_codes(digest,expires,created_by) VALUES(?,?,?)",
                               (hashed(code), time.time() + 900, admin))
        return code

    def instructions(self, admin: int) -> str:
        if not self.public_url:
            return "Адрес сервера ещё не настроен (CODEX_STATS_PUBLIC_URL)."
        code = self.code(admin)
        script = "https://raw.githubusercontent.com/bendertherobot7771/codex-stats-bot/v0.4.2/agent/bootstrap.ps1"
        return ("Подключение Windows-ПК\n\nКод (одноразовый, действует 15 минут):\n" + code +
                "\n\nОткройте PowerShell от обычного пользователя и вставьте команду целиком:\n\n" +
                "$p = Join-Path $env:TEMP ('codex-stats-install-' + [guid]::NewGuid() + '.ps1'); " +
                "Invoke-WebRequest '" + script + "' -OutFile $p -UseBasicParsing; " +
                "powershell -NoProfile -ExecutionPolicy Bypass -File $p -ServerUrl '" + self.public_url +
                "' -Code '" + code + "'\n\n" +
                "Установщик сам добавит отдельный Python и агент для текущего пользователя, настроит автозапуск и обновления в простое. " +
                "Нужна локальная авторизация Codex. Git и Python не требуются. " +
                "Адрес 192.168.x.x доступен только в домашней сети; для другого места нужен настроенный HTTPS-адрес сервера. " +
                "Не пересылайте код посторонним.")

    def enroll(self, data: dict, peer: str = "local") -> dict:
        with self.db._lock:
            now = time.time()
            self.enrollment_attempts = {key: [stamp for stamp in values if stamp > now - 60]
                                        for key, values in self.enrollment_attempts.items() if any(stamp > now - 60 for stamp in values)}
            if len(self.enrollment_attempts) >= 1000 and peer not in self.enrollment_attempts:
                raise ValueError("Слишком много попыток подключения; попробуйте через минуту")
            attempts = self.enrollment_attempts.setdefault(peer, [])
            if len(attempts) >= 10:
                raise ValueError("Слишком много попыток подключения; попробуйте через минуту")
            attempts.append(now)
        machine = str(data.get("machine_id", ""))
        name = str(data.get("machine_name", ""))
        if not machine or len(machine) > 100 or not name or len(name) > 200:
            raise ValueError("Invalid machine identity")
        # An installer must not consume the code if no signed release is available yet.
        envelope = self.current_release()
        token = secrets.token_urlsafe(32)
        with self.db._lock, self.db._connection as connection:
            changed = connection.execute("UPDATE enrollment_codes SET used=1 WHERE digest=? AND used=0 AND expires>?",
                                         (hashed(str(data.get("code", ""))), time.time())).rowcount
            if not changed:
                raise ValueError("Код недействителен, использован или истёк")
            if connection.execute("SELECT 1 FROM devices WHERE machine_id=?", (machine,)).fetchone():
                raise ValueError("ПК уже зарегистрирован; используйте существующую конфигурацию")
            connection.execute("INSERT INTO devices(machine_id,machine_name,token_hash) VALUES(?,?,?)",
                               (machine, name, hashed(token)))
        return {"api_key": token, "machine_id": machine, "release": envelope}

    def authenticate(self, token: str) -> str | None:
        with self.db._lock:
            row = self.db._connection.execute("SELECT machine_id FROM devices WHERE token_hash=? AND enabled=1", (hashed(token),)).fetchone()
        return str(row[0]) if row else None

    def current_release(self) -> dict:
        path = self.cache / __version__ / "release.json"
        envelope = json.loads(path.read_text())
        manifest = verify(envelope)
        if manifest["version"] != __version__:
            raise ValueError("Installed release mismatch")
        return envelope

    def devices(self) -> list[dict]:
        with self.db._lock:
            return [dict(row) for row in self.db._connection.execute(
                "SELECT machine_id,machine_name,enabled,seen,version,protocol,busy,idle_since,queued,update_error,update_warning_at,warning_version FROM devices")]

    def checkin(self, data: dict, machine: str) -> dict:
        now = time.time()
        busy = bool(data.get("busy", True))
        queued = max(0, int(data.get("queued", 1)))
        client_version = str(data.get("version", "0.0.0"))
        version(client_version)
        with self.db._lock, self.db._connection as connection:
            connection.execute("INSERT OR IGNORE INTO devices(machine_id,machine_name) VALUES(?,?)",
                               (machine, str(data.get("machine_name", machine))[:200]))
            connection.execute("""UPDATE devices SET seen=?,version=?,protocol=?,busy=?,queued=?,
                idle_since=CASE WHEN ? THEN NULL ELSE COALESCE(idle_since,?) END, update_error=? WHERE machine_id=?""",
                (now, client_version, int(data.get("protocol", 0)), int(busy), queued, int(busy or queued > 0), now,
                 str(data.get("update_error", ""))[:200], machine))
            state = self.state()
            if busy or queued:
                connection.execute("UPDATE devices SET update_warning_at=NULL,warning_version=NULL WHERE machine_id=?", (machine,))
                if state.get("phase") == "warning":
                    state.update(phase="waiting", warning_at=None)
                    self.save(state)
            device = next(d for d in self.devices() if d["machine_id"] == machine)
            ready = not busy and not queued and device["idle_since"] is not None and now - device["idle_since"] >= 300
            response = {"server_version": __version__, "phase": state.get("phase", "idle"), "update": None}
            if state.get("phase") == "clients" and ready and version(client_version) < version(state["version"]):
                response["update"] = state["envelope"]
                if data.get("begin_update") == state["version"]:
                    if device["warning_version"] != state["version"] or device["update_warning_at"] is None:
                        self.announce(f"Через 60 секунд обновится {device['machine_name']} до {state['version']}.\n"
                                      "Пожалуйста, не запускайте Codex на этом ПК до сообщения о завершении. "
                                      "Иначе точный замер лимита задания может быть недоступен.")
                        connection.execute("UPDATE devices SET update_warning_at=?,warning_version=? WHERE machine_id=?",
                                           (time.time(), state["version"], machine))
                    elif now - device["update_warning_at"] >= 60:
                        response["lease_until"] = now + 60
            if data.get("update_result"):
                result = {"success": "успешно обновлён", "deferred": "отложил обновление из-за активности; старая версия продолжает работу",
                          "blocked": "не смог запустить новый пакет агента; текущий агент не остановлен",
                          "manual": "не смог безопасно завершить обновление; требуется ручная проверка агента"}.get(data["update_result"], "вернулся к предыдущей версии после ошибки")
                suffix = "Проверьте состояние перед продолжением работы." if data["update_result"] == "manual" else "Можно продолжать работу."
                self.announce(f"{device['machine_name']} {result}. Версия {client_version}. {suffix}")
            return response

    def ready(self) -> tuple[bool, str]:
        now = time.time()
        devices = self.devices()
        if not devices:
            return False, "Нет подтверждений простоя от агентов"
        for device in devices:
            if not device["enabled"]:
                continue
            if device["seen"] < now - 90:
                # Protocol 1 is explicitly backwards compatible; an offline PC queues its events.
                if device["protocol"] != PROTOCOL:
                    return False, "Несовместимый недоступный ПК: " + device["machine_name"]
                continue
            if device["busy"] or device["queued"] or device["idle_since"] is None or now - device["idle_since"] < 300:
                return False, "Ожидается простой: " + device["machine_name"]
        # Legacy agents cannot acknowledge idleness while they are sending task activity.
        for task in self.db.active_tasks():
            if task["last_seen_at"] > now - 120 and not any(d["machine_id"] == task["machine_id"] and not d["busy"] and d["seen"] >= task["last_seen_at"] for d in devices):
                return False, "Есть активное задание: " + task["machine_name"]
        return True, "Все доступные агенты подтвердили простой"

    def on_event(self, event: dict) -> None:
        if event.get("event_type") not in ("task_started", "task_heartbeat") or float(event.get("sent_at", 0)) < time.time() - 90:
            return
        with self.db._lock, self.db._connection as connection:
            connection.execute("UPDATE devices SET busy=1,idle_since=NULL,update_warning_at=NULL,warning_version=NULL WHERE machine_id=?",
                               (event.get("machine_id"),))
            state = self.state()
            if state.get("phase") == "warning":
                state.update(phase="waiting", warning_at=None)
                self.save(state)

    def control(self, data: dict) -> dict:
        operation = data.get("operation", "status")
        with self.db._lock:
            state = self.state()
            if operation == "offer":
                manifest = verify(data["envelope"])
                if version(manifest["version"]) < version(__version__):
                    raise ValueError("Downgrade forbidden")
                if state.get("version") != manifest["version"]:
                    state = {"version": manifest["version"], "envelope": data["envelope"], "phase": "waiting"}
                    self.save(state)
            elif operation == "claim":
                ready, reason = self.ready()
                if state.get("phase") == "waiting" and ready:
                    self.announce(f"Начинается обновление системы до {state['version']} через 60 секунд.\n"
                                  "Пожалуйста, воздержитесь от работы в Codex. Задания, начатые при перезапуске агента, могут не получить точный замер лимита.\n"
                                  "Если начнётся работа до установки, обновление будет отложено.")
                    state.update(phase="warning", warning_at=time.time())
                elif state.get("phase") == "warning":
                    if not ready:
                        state.update(phase="waiting", warning_at=None)
                    elif time.time() - state["warning_at"] >= 60:
                        state.update(phase="server", claimed_at=time.time())
                state["reason"] = reason
                self.save(state)
            elif operation == "finish":
                state.update(phase="clients")
                self.save(state)
                self.announce(f"Сервер обновлён до {__version__}. Приём статистики возобновлён.\n"
                              "Windows-агенты обновятся при собственном простое; активные задания не прерываются.")
            elif operation == "failure":
                state.update(phase="failed", reason=str(data.get("reason", "Ошибка обновления"))[:200])
                self.save(state)
                self.announce("Обновление не удалось. Восстановлена предыдущая версия. Сбор статистики возобновлён.")
            return self.state()

    def blocked(self) -> bool:
        return self.state().get("phase") == "server"

    def description(self) -> str:
        state = self.state()
        lines = [f"Сервер: {__version__}", "Обновление: " + state.get("phase", "idle")]
        if state.get("version"):
            lines.append("Целевая версия: " + state["version"])
        for device in self.devices():
            status = "нет связи" if device["seen"] < time.time() - 90 else "занят" if device["busy"] else "простой"
            lines.append(f"{device['machine_name']} · {device['version']} · {status}")
            if device["update_error"]:
                lines.append("  Обновление: " + device["update_error"])
        if state.get("reason"):
            lines.append(state["reason"])
        return "\n".join(lines)
