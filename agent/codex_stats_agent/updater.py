from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from common.releases import PROTOCOL, asset_url, check_file, download, verify, version
from . import __version__
from .config import APP_DIR, DEFAULT_CONFIG_PATH, AgentConfig, create_config


class UpdateDeferred(RuntimeError):
    pass


def rpc(config: AgentConfig, path: str, data: dict) -> dict:
    request = urllib.request.Request(config.server_url + path, data=json.dumps(data).encode(),
                                     headers={"Authorization": "Bearer " + config.api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def enroll(server_url: str, code: str, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    if config_path.exists():
        raise ValueError("Агент уже настроен; автообновление сохранит существующую конфигурацию")
    config = create_config(server_url, "", os.environ.get("USERNAME", "Windows user"))
    reply = rpc(config, "/api/v1/enroll", {"code": code, "machine_id": config.machine_id, "machine_name": config.machine_name})
    verify(reply["release"])
    config.api_key = reply["api_key"]
    config.save(config_path)


def log_fingerprint(codex_home: Path) -> dict[str, tuple[int, int]]:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in (codex_home / "sessions").rglob("*.jsonl")}


class AgentUpdater:
    def __init__(self, watcher):
        self.watcher = watcher
        self.last_check = 0.0
        self.directory = APP_DIR / "updates"

    def tick(self) -> None:
        if time.time() - self.last_check < 15:
            return
        self.last_check = time.time()
        watcher = self.watcher
        data = {"machine_id": watcher.config.machine_id, "machine_name": watcher.config.machine_name,
                "version": __version__, "protocol": PROTOCOL, "busy": bool(watcher.active_tasks),
                "queued": len(watcher.queue._load())}
        result_file = self.directory / "result.json"
        if result_file.exists():
            data.update(json.loads(result_file.read_text()))
        reply = rpc(watcher.config, "/api/v1/agent/checkin", data)
        if result_file.exists():
            result_file.unlink()
        self.directory.mkdir(parents=True, exist_ok=True)
        health = {"version": __version__, "at": time.time()}
        temporary = self.directory / "health.tmp"
        temporary.write_text(json.dumps(health))
        temporary.replace(self.directory / "health.json")
        envelope = reply.get("update")
        if not envelope or not getattr(sys, "frozen", False):
            return
        manifest = verify(envelope)
        target_version = manifest["version"]
        if version(target_version) <= version(__version__) or data["busy"] or data["queued"]:
            return
        failed = self.directory / f"failed-{target_version}.json"
        if failed.exists():
            return
        stage = self.directory / target_version
        binary = stage / "codex-stats-agent.exe"
        if not binary.exists():
            download(asset_url(target_version, binary.name), binary)
        check_file(binary, manifest["assets"][binary.name])
        # Re-read logs after the potentially slow download before asking for a lease.
        watcher.poll_once()
        watcher.queue.drain(watcher.client)
        if watcher.active_tasks or watcher.queue._load():
            return
        data["begin_update"] = target_version
        reply = rpc(watcher.config, "/api/v1/agent/checkin", data)
        if reply.get("lease_until", 0) <= time.time():
            return
        (stage / "release.json").write_text(json.dumps(envelope))
        (stage / "guard.json").write_text(json.dumps({"logs": log_fingerprint(watcher.codex_home),
                                                     "lease_until": reply["lease_until"], "pid": os.getpid()}))
        helper = stage / "update-helper.exe"
        shutil.copy2(sys.executable, helper)
        watcher._save_state()
        subprocess.Popen([str(helper), "apply-update", "--stage", str(stage)], creationflags=subprocess.CREATE_NO_WINDOW)
        watcher.stop()


def apply_update(stage: Path, installed_version: str | None = None) -> int:
    try:
        return _apply_update(stage, installed_version)
    except Exception:
        # Even a corrupt staging file must not leave the collector stopped.
        try:
            guard = json.loads((stage / "guard.json").read_text())
            if wait_for_exit(int(guard.get("pid", 0))):
                target = Path(os.environ["LOCALAPPDATA"]) / "CodexStatsAgent" / "codex-stats-agent.exe"
                subprocess.Popen([str(target), "watch"], creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
        return 1


def _apply_update(stage: Path, installed_version: str | None = None) -> int:
    config = AgentConfig.load()
    manifest = verify(json.loads((stage / "release.json").read_text()))
    if version(manifest["version"]) <= version(installed_version or __version__):
        raise ValueError("Downgrade forbidden")
    binary = stage / "codex-stats-agent.exe"
    check_file(binary, manifest["assets"][binary.name])
    guard = json.loads((stage / "guard.json").read_text())
    target = Path(os.environ["LOCALAPPDATA"]) / "CodexStatsAgent" / "codex-stats-agent.exe"
    backup = target.with_name("codex-stats-agent.previous.exe")
    update_dir = APP_DIR / "updates"
    flags = subprocess.CREATE_NO_WINDOW
    switched = False
    old_exited = False
    try:
        old_exited = wait_for_exit(int(guard.get("pid", 0)))
        if not old_exited:
            raise RuntimeError("Previous agent did not exit; no forced stop")
        # Wait for the old one-file executable to exit; never terminate Codex or the agent forcibly.
        deadline = min(time.time() + 45, guard["lease_until"])
        while time.time() < deadline:
            current = json.loads(json.dumps(log_fingerprint(Path(config.codex_home))))
            if current != guard["logs"]:
                raise UpdateDeferred("Activity appeared during update")
            try:
                shutil.copy2(target, backup)
                staged = target.with_suffix(".new.exe")
                shutil.copy2(binary, staged)
                os.replace(staged, target)
                switched = True
                break
            except PermissionError:
                time.sleep(1)
        if not switched:
            raise UpdateDeferred("Could not replace executable while idle")
        started = time.time()
        process = subprocess.Popen([str(target), "watch"], creationflags=flags)
        deadline = time.time() + 90
        healthy = False
        while time.time() < deadline:
            try:
                health = json.loads((update_dir / "health.json").read_text())
                healthy = health["version"] == manifest["version"] and health["at"] >= started
            except (OSError, ValueError, KeyError):
                pass
            if healthy:
                break
            if process.poll() is not None:
                break
            time.sleep(1)
        if not healthy:
            # Request a graceful stop so the new agent flushes its durable state.
            (update_dir / "stop-request").touch()
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                raise RuntimeError("New agent did not stop safely; manual recovery required")
            os.replace(backup, target)
            switched = False
            raise RuntimeError("New agent failed its health check")
        (update_dir / "result.json").write_text(json.dumps({"update_result": "success"}))
        return 0
    except Exception as error:
        deferred = isinstance(error, UpdateDeferred)
        if not deferred:
            (update_dir / f"failed-{manifest['version']}.json").write_text(json.dumps({"error": type(error).__name__}))
        (update_dir / "result.json").write_text(json.dumps({"update_result": "deferred" if deferred else "rollback", "update_error": type(error).__name__}))
        if not switched and old_exited:
            subprocess.Popen([str(target), "watch"], creationflags=flags)
        return 2 if deferred else 1


def wait_for_exit(pid: int) -> bool:
    if not pid or os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87  # Process already exited.
    try:
        return kernel.WaitForSingleObject(handle, 45000) == 0
    finally:
        kernel.CloseHandle(handle)


def legacy_upgrade(stage: Path) -> int:
    """One-time 0.3.0 migration; publish read-only idle proof before replacing anything."""
    from .watcher import CodexWatcher
    from .api import ServerClient
    manifest = verify(json.loads((stage / "release.json").read_text()))
    if manifest["version"] != "0.4.0":
        raise ValueError("Legacy migration is only for 0.3.0 -> 0.4.0")
    binary = stage / "codex-stats-agent.exe"
    check_file(binary, manifest["assets"][binary.name])
    target = Path(os.environ["LOCALAPPDATA"]) / "CodexStatsAgent" / "codex-stats-agent.exe"
    installed = subprocess.check_output([str(target), "--version"], creationflags=subprocess.CREATE_NO_WINDOW).decode().strip()
    if installed == "0.4.0":
        clear_legacy_startup(stage)
        return 0
    if installed != "0.3.0":
        raise ValueError("Installed agent is not the supported legacy version")
    config = AgentConfig.load()
    idle_since = None
    while True:
        try:
            watcher = CodexWatcher(config)
            watcher._save_state = lambda: None  # Do not race the running legacy agent's state writer.
            watcher._reconcile_tasks()
            busy, queued = bool(watcher.active_tasks), len(watcher.queue._load())
            idle_since = None if busy or queued else (idle_since or time.time())
            now = time.time()
            # Legacy v0.3 accepts quota_snapshot and ignores the extra maintenance metadata.
            event = {"event_id": f"{config.machine_id}:maintenance:{int(now)}", "event_type": "quota_snapshot",
                     "task_id": "maintenance", "account_fingerprint": watcher.account_id,
                     "machine_id": config.machine_id, "machine_name": config.machine_name,
                     "user_name": config.user_name, "sent_at": now, "quota": {},
                     "maintenance_probe": {"busy": busy, "queued": queued, "idle_since": idle_since}}
            ServerClient(config.server_url, config.api_key).send(event)
            data = {"machine_id": config.machine_id, "machine_name": config.machine_name,
                    "version": installed, "protocol": PROTOCOL, "busy": busy, "queued": queued,
                    "begin_update": manifest["version"]}
            reply = rpc(config, "/api/v1/agent/checkin", data)
            if (idle_since and now - idle_since >= 300 and reply.get("lease_until", 0) > now):
                logs = log_fingerprint(watcher.codex_home)
                # This one-time step is necessary because 0.3 has no graceful update command.
                # Only our idle legacy collector is stopped; never Codex itself.
                backup = stage / "legacy-data-backup"
                backup.mkdir(exist_ok=True)
                for name in ("config.json", "state.json", "queue.json"):
                    source = APP_DIR / name
                    if source.exists():
                        shutil.copy2(source, backup / name)
                if log_fingerprint(watcher.codex_home) != logs:
                    continue
                quoted = str(target).replace("'", "''")
                command = ("Get-CimInstance Win32_Process -Filter \"Name = 'codex-stats-agent.exe'\" | "
                           "Where-Object { $_.ExecutablePath -eq '" + quoted + "' } | "
                           "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
                subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                               check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                (stage / "guard.json").write_text(json.dumps({"logs": logs, "lease_until": reply["lease_until"], "pid": 0}))
                result = apply_update(stage, installed_version=installed)
                if result != 2:
                    clear_legacy_startup(stage)
                    if result:
                        try:
                            rpc(config, "/api/v1/agent/checkin", dict(data, update_result="rollback", update_error="Первоначальное обновление не удалось"))
                        except Exception:
                            pass
                    return result
        except Exception as error:
            print("Legacy upgrade waiting:", type(error).__name__, flush=True)
        time.sleep(15)


def clear_legacy_startup(stage: Path) -> None:
    if os.name != "nt":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, "CodexStatsLegacyUpgrade")
            if str(stage).lower() in value.lower() and "legacy-launch.vbs" in value.lower():
                winreg.DeleteValue(key, "CodexStatsLegacyUpgrade")
    except FileNotFoundError:
        pass
