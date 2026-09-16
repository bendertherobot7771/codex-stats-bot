"""Signed source releases; no executable replacement and no configuration rewrites."""
import json
import os
import re
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from common.releases import check_file, verify, version
from .config import APP_DIR, AgentConfig

ASSET = "agent-source.zip"


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def extract(archive: Path, destination: Path):
    """Reject traversal, ADS, Windows aliases, links and expansion bombs before writing."""
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 1000 or sum(e.file_size for e in entries) > 20_000_000:
            raise ValueError("Source archive too large")
        seen = set()
        for entry in entries:
            parts = PurePosixPath(entry.filename).parts
            key = entry.filename.lower()
            if (not parts or parts[0] not in ("agent", "common") or
                    any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p) or p.endswith(".") for p in parts) or
                    any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in parts) or
                    "\\" in entry.filename or key in seen or entry.is_dir() or
                    ((entry.external_attr >> 16) & 0o170000) == 0o120000 or
                    not entry.filename.endswith(".py")):
                raise ValueError("Unsafe source archive")
            seen.add(key)
        required = {"agent/launch.py", "agent/codex_stats_agent/__main__.py", "common/releases.py"}
        if not required <= seen:
            raise ValueError("Incomplete source archive")
        destination.mkdir(parents=True, exist_ok=False)
        for entry in entries:
            target = destination.joinpath(*PurePosixPath(entry.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(entry))


def prepare(stage: Path, root: Path, manifest: dict) -> Path:
    check_file(stage / ASSET, manifest["assets"][ASSET])
    target = root / "releases" / manifest["version"]
    marker = target / ".verified.json"
    expected = manifest["assets"][ASSET]
    if not marker.exists():
        # Incomplete directories are left for inspection, never reused as verified code.
        staging = root / "releases" / (manifest["version"] + ".staging-" + uuid.uuid4().hex)
        extract(stage / ASSET, staging)
        atomic(staging / ".verified.json", expected)
        staging.rename(target)
    if json.loads(marker.read_text()) != expected:
        raise ValueError("Installed source hash mismatch")
    console = str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable
    result = subprocess.check_output([console, "-B", str(target / "agent" / "launch.py"), "--version"],
                                     timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.decode().strip() != manifest["version"]:
        raise ValueError("Source version mismatch")
    return target


def command(root):
    return [sys.executable, "-B", str(root / "launch.py"), "watch"]


def apply_source(stage: Path, installed_version=None):
    from . import __version__
    from .updater import log_fingerprint, wait_for_exit, UpdateDeferred
    root = Path(os.environ["CODEX_STATS_INSTALL_ROOT"])
    updates = APP_DIR / "updates"
    config = AgentConfig.load()
    manifest = verify(json.loads((stage / "release.json").read_text()))
    if version(manifest["version"]) <= version(installed_version or __version__):
        raise ValueError("Downgrade forbidden")
    prepare(stage, root, manifest)
    guard = json.loads((stage / "guard.json").read_text())
    old = json.loads((root / "current.json").read_text())
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    switched = False
    exited = False
    process = None
    try:
        exited = wait_for_exit(guard["pid"])
        if not exited:
            raise UpdateDeferred("Previous collector still running")
        current_logs = json.loads(json.dumps(log_fingerprint(Path(config.codex_home))))
        if time.time() >= guard["lease_until"] or current_logs != guard["logs"]:
            raise UpdateDeferred("Activity or expired lease")
        # Persist recovery intent before switching; launcher recovers it after a reboot.
        atomic(root / "pending.json", {"previous": old, "version": manifest["version"]})
        atomic(root / "current.json", {"version": manifest["version"]})
        switched = True
        started = time.time()
        env = dict(os.environ, CODEX_STATS_UPDATE_CHILD="1")
        process = subprocess.Popen(command(root), creationflags=flags, env=env)
        deadline = started + 90
        healthy = False
        while time.time() < deadline:
            try:
                health = json.loads((updates / "health.json").read_text())
                healthy = health["version"] == manifest["version"] and health["at"] >= started
            except (OSError, ValueError, KeyError):
                pass
            if healthy or process.poll() is not None:
                break
            time.sleep(1)
        if not healthy:
            (updates / "stop-request").touch()
            process.wait(timeout=45)
            raise RuntimeError("New collector failed health check")
        (root / "pending.json").unlink()
        atomic(updates / "result.json", {"update_result": "success"})
        return 0
    except Exception as error:
        if switched and process is not None and process.poll() is None:
            atomic(updates / "result.json", {"update_result": "manual", "update_error": type(error).__name__})
            return 1  # Never force-stop a collector which may have received work.
        if switched:
            atomic(root / "current.json", old)
            (root / "pending.json").unlink(missing_ok=True)
        (updates / "stop-request").unlink(missing_ok=True)
        deferred = isinstance(error, UpdateDeferred)
        if not deferred:
            atomic(updates / f"failed-{manifest['version']}.json", {"error": type(error).__name__})
        atomic(updates / "result.json", {"update_result": "deferred" if deferred else "rollback"})
        if exited:
            subprocess.Popen(command(root), creationflags=flags)
        return 2 if deferred else 1
