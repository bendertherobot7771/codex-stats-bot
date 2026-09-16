"""CI-only signing. Private key is supplied through an Actions secret, never printed."""
import base64
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from common.releases import PROTOCOL, SCHEMA, verify, version

release_version = os.environ["GITHUB_REF_NAME"].removeprefix("v")
version(release_version)
root = Path("dist")
root.mkdir(exist_ok=True)
with tarfile.open(root / "server.tar.gz", "w:gz") as archive:
    for folder in ("server", "common"):
        for path in sorted(Path(folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.add(path, arcname=str(path).replace("\\", "/"))
assets = {}
with zipfile.ZipFile(root / "agent-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for folder in ("agent", "common"):
        for path in sorted(Path(folder).rglob("*.py")):
            archive.write(path, arcname=path.as_posix())
for name in ("server.tar.gz", "codex-stats-agent.exe", "agent-source.zip"):
    data = (root / name).read_bytes()
    assets[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
manifest = {"version": release_version, "protocol": PROTOCOL, "schema": SCHEMA,
            "min_agent_protocol": 1, "rollback_safe": True, "assets": assets}
raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
with tempfile.TemporaryDirectory() as directory:
    key = Path(directory) / "key.pem"
    key.write_text(os.environ["RELEASE_SIGNING_KEY"])
    key.chmod(0o600)
    signature = subprocess.check_output(["openssl", "dgst", "-sha256", "-sign", str(key)], input=raw)
envelope = {"payload": base64.b64encode(raw).decode(), "signature": base64.b64encode(signature).decode()}
verify(envelope)
(root / "release.json").write_text(json.dumps(envelope), encoding="utf-8")
print("Signed release", release_version)
