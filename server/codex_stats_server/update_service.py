"""Root-owned, timer-driven installer; the HTTP service itself cannot replace code."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

from common.releases import REPOSITORY, asset_url, check_file, download, verify, version
from . import __version__

ROOT = Path("/opt/codex-stats-bot")
CACHE = Path(os.environ.get("CODEX_STATS_RELEASE_CACHE", "/var/cache/codex-stats/releases"))
STATE = Path("/var/lib/codex-stats-updater")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value), encoding="utf-8")
    temp.replace(path)


def control(operation: str, **fields) -> dict:
    port = int(os.environ.get("CODEX_STATS_PORT", "8765"))
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/control/updates",
                                     data=json.dumps(dict(operation=operation, **fields)).encode(),
                                     headers={"Authorization": "Bearer " + os.environ["CODEX_STATS_API_KEY"],
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def healthy(target_version: str) -> bool:
    try:
        port = int(os.environ.get("CODEX_STATS_PORT", "8765"))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            data = json.load(response)
        return data.get("status") == "ok" and data.get("version") == target_version
    except (OSError, ValueError):
        return False


def switch_release(directory: Path) -> None:
    resolved = directory.resolve()
    if not resolved.is_relative_to((ROOT / "releases").resolve()) or not resolved.is_dir():
        raise ValueError("Invalid release directory")
    link = ROOT / "current.next"
    if link.is_symlink():
        link.unlink()
    os.symlink(resolved, link)
    os.replace(link, ROOT / "current")


def extract_verified(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as bundle:
        total = 0
        seen = set()
        for entry in bundle:
            path = PurePosixPath(entry.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.name or not path.parts
                    or path.parts[0] not in ("server", "common") or not entry.isfile() or entry.name in seen):
                raise ValueError("Unsafe release archive")
            total += entry.size
            if total > 150_000_000:
                raise ValueError("Archive too large")
            seen.add(entry.name)
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = bundle.extractfile(entry)
            if stream is None:
                raise ValueError("Invalid archive file")
            target.write_bytes(stream.read())
            target.chmod(0o644)


def run_service(action: str) -> None:
    subprocess.run(["systemctl", action, "codex-stats-bot"], check=True, timeout=45)


def recover(journal: dict) -> None:
    # Admission remains closed (phase=server) until health passes or rollback completes.
    if healthy(journal["new_version"]):
        control("finish")
    else:
        run_service("stop")
        switch_release(Path(journal["previous"]))
        run_service("start")
        for _ in range(20):
            if healthy(journal["old_version"]):
                break
            time.sleep(1)
        if not healthy(journal["old_version"]):
            raise RuntimeError("Rollback health check failed; journal retained")
        control("failure", reason="Новая версия не прошла проверку запуска; выполнен откат")
        atomic_json(STATE / ("failed-" + journal["new_version"] + ".json"), journal)
    atomic_json(STATE / "transaction.json", {"complete": True})


def tick() -> None:
    journal_path = STATE / "transaction.json"
    if journal_path.exists():
        journal = json.loads(journal_path.read_text())
        if not journal.get("complete"):
            recover(journal)
            return
    # Cache release discovery for 15 minutes; still check the idle gate every minute.
    latest_file = STATE / "latest.json"
    if not latest_file.exists() or time.time() - latest_file.stat().st_mtime > 900:
        request = urllib.request.Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
                                         headers={"User-Agent": "codex-stats-updater"})
        with urllib.request.urlopen(request, timeout=30) as response:
            latest = json.load(response)
        if latest.get("draft") or latest.get("prerelease"):
            return
        release_version = latest["tag_name"].removeprefix("v")
        version(release_version)
        atomic_json(latest_file, {"version": release_version})
    release_version = json.loads(latest_file.read_text())["version"]
    if version(release_version) < version(__version__) or (STATE / f"failed-{release_version}.json").exists():
        return
    stage = CACHE / release_version
    envelope_file = stage / "release.json"
    if not envelope_file.exists():
        download(asset_url(release_version, "release.json"), envelope_file, limit=65536)
    envelope = json.loads(envelope_file.read_text())
    manifest = verify(envelope)
    if manifest["version"] != release_version:
        raise ValueError("Release version mismatch")
    for name, info in manifest["assets"].items():
        if name not in ("server.tar.gz", "codex-stats-agent.exe"):
            continue
        path = stage / name
        if not path.exists():
            download(asset_url(release_version, name), path)
        check_file(path, info)
    state = control("offer", envelope=envelope)
    if state.get("phase") in ("clients", "failed"):
        return
    state = control("claim")
    if state.get("phase") != "server":
        return
    if release_version == __version__:
        control("finish")
        return
    destination = ROOT / "releases" / release_version
    if not destination.exists():
        extract_verified(stage / "server.tar.gz", destination)
        (destination / ".verified").write_text(manifest["assets"]["server.tar.gz"]["sha256"])
    if (destination / ".verified").read_text() != manifest["assets"]["server.tar.gz"]["sha256"]:
        raise ValueError("Existing staging directory mismatch")
    database = Path(os.environ.get("CODEX_STATS_DB", "/var/lib/codex-stats-bot/codex-stats.sqlite"))
    backup_dir = STATE / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"before-{release_version}-{int(time.time())}.sqlite"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    # Verify startup imports and schema initialization on a disposable COPY, not the live database.
    probe = backup.with_suffix(".probe.sqlite")
    import shutil
    shutil.copy2(backup, probe)
    subprocess.run(["/usr/bin/python3", "-c",
                    "from pathlib import Path; from server.codex_stats_server.database import StatsDatabase; "
                    "from server.codex_stats_server.lifecycle import Lifecycle; "
                    "import sys; d=StatsDatabase(Path(sys.argv[1])); Lifecycle(d,'',Path('/tmp')); d.close()",
                    str(probe)], cwd=destination, check=True, timeout=30)
    journal = {"previous": str((ROOT / "current").resolve()), "old_version": __version__, "new_version": release_version}
    atomic_json(journal_path, journal)
    run_service("stop")
    switch_release(destination)
    run_service("start")
    for _ in range(30):
        if healthy(release_version):
            break
        time.sleep(1)
    recover(journal)


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "updater.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            tick()
            return 0
        except Exception as error:
            print("Update deferred:", type(error).__name__, flush=True)
            # No repeated automatic attempts at a failed installation; keep collecting.
            try:
                journal = json.loads((STATE / "transaction.json").read_text()) if (STATE / "transaction.json").exists() else {"complete": True}
                if journal.get("complete") and control("status").get("phase") == "server":
                    control("failure", reason="Подготовка обновления не прошла проверку")
            except Exception:
                pass
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
