"""One-time idle-only migration of the existing 0.3 server layout."""
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

from common.releases import verify
from .database import StatsDatabase
from .lifecycle import Lifecycle
from .telegram_bot import TelegramBot
from .update_service import CACHE, ROOT, STATE, atomic_json, control, healthy


def idle_proofs(events: list[dict], now: float) -> bool:
    latest, proofs, work = {}, {}, {}
    for event in events:
        machine = event.get("machine_id")
        if not machine:
            continue
        stamp = float(event.get("sent_at") or 0)
        latest[machine] = max(latest.get(machine, 0), stamp)
        if event.get('event_type') in ('task_started','task_heartbeat','task_completed'):
            work[machine] = max(work.get(machine,0),stamp)
        if "maintenance_probe" in event and stamp > proofs.get(machine, (0, {}))[0]:
            proofs[machine] = (stamp, event["maintenance_probe"])
    if not proofs:
        return False
    for machine, stamp in latest.items():
        if stamp < now - 90:
            continue  # Offline protocol-1 collectors remain compatible after this additive upgrade.
        seen, proof = proofs.get(machine, (0, {}))
        if (seen < now - 45 or work.get(machine,0) > seen or proof.get("busy", True) or proof.get("queued", 1)
                or proof.get("idle_since") is None or now - proof["idle_since"] < 300):
            return False
    return True


def main() -> int:
    enabled = STATE / "legacy-migration.enabled"
    if not enabled.exists():
        return 0
    state_path = STATE / "legacy-migration.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("phase") in ("complete", "failed"):
        return 0
    db_path = Path(os.environ["CODEX_STATS_DB"])
    db = StatsDatabase(db_path)
    bot = TelegramBot(os.environ["TELEGRAM_BOT_TOKEN"], {8461749755}, set(), db)
    _, events = db.accounting_data()
    if not idle_proofs(events, time.time()):
        atomic_json(state_path, {"phase": "waiting"})
        db.close()
        return 0
    if state.get("phase") != "warning":
        bot.announce_maintenance("Через 60 секунд начнётся установка системы автообновления 0.4.0.\n"
                                 "Пожалуйста, пока не запускайте задания Codex. При новой активности установка будет отложена.")
        atomic_json(state_path, {"phase": "warning", "at": time.time()})
        db.close()
        return 0
    if time.time() - state["at"] < 60:
        db.close()
        return 0
    envelope = json.loads((CACHE / "0.4.0" / "release.json").read_text())
    manifest = verify(envelope)
    if manifest["version"] != "0.4.0":
        raise ValueError("Wrong migration release")
    release_root = ROOT / "releases" / "0.4.0"
    unit_path = Path('/etc/systemd/system/codex-stats-bot.service')
    previous_unit = STATE / 'legacy-service.backup'
    if not previous_unit.exists():
        shutil.copy2(unit_path, previous_unit)
    db.close()
    backup = STATE / ('legacy-before-' + str(int(time.time())) + '.sqlite')
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    try:
        subprocess.run(['systemctl','stop','codex-stats-bot'],check=True)
        current = ROOT / 'current'
        if not current.exists():
            current.symlink_to(release_root)
        if current.resolve() != release_root:
            raise ValueError('Unexpected current link')
        anchor = Path('/opt/codex-stats-updater')
        if not anchor.exists():
            anchor.mkdir()
            for folder in ('server','common'):
                shutil.copytree(release_root/folder,anchor/folder)
        for name in ('codex-stats-bot.service','codex-stats-updater.service','codex-stats-updater.timer'):
            shutil.copy2(release_root/'server'/name,Path('/etc/systemd/system')/name)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['systemctl','start','codex-stats-bot'],check=True)
        for _ in range(20):
            if healthy('0.4.0'): break
            time.sleep(1)
        if not healthy('0.4.0'):
            raise RuntimeError('New server did not start')
        control('offer',envelope=envelope)
        control('finish')
        subprocess.run(['systemctl','enable','--now','codex-stats-updater.timer'],check=True)
        atomic_json(state_path,{'phase':'complete'})
        subprocess.run(['systemctl','disable','--now','codex-stats-legacy.timer'],check=True)
    except Exception:
        subprocess.run(['systemctl','stop','codex-stats-bot'],check=True)
        shutil.copy2(previous_unit,unit_path)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['systemctl','start','codex-stats-bot'],check=True)
        atomic_json(state_path,{'phase':'failed'})
        db = StatsDatabase(db_path)
        bot = TelegramBot(os.environ["TELEGRAM_BOT_TOKEN"],{8461749755},set(),db)
        bot.announce_maintenance('Первоначальное обновление не удалось. Восстановлен сервер 0.3.0, статистика сохранена.')
        db.close()
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
