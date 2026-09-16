from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import urllib.request
from pathlib import Path

REPOSITORY = "bendertherobot7771/codex-stats-bot"
PROTOCOL = 1
SCHEMA = 1
PUBLIC_MODULUS = (
    "C7ADFC51A1D0B207EA8C166CF747C793C6104C48D29C2CDCC5F69A2CF30C35C3F10BA3C2CCB43BB8E1D2FC188DD3979143A862B764CFC361C28B2E900A508937FF249912B26573AD304B5D870A31C8427E92E119B81F32A3DAC4FB302465C809C3A78D519046EE45BBB754B23E0243621149A56D5B8C10187E5E60A92DEFDC85EAA1BD6989A79C46E6CEE8BFCCEB139EA83880DB85AF738B4CD8034928F8BB2B52257A95294BFC29E0C0C26B367F4ABFCC802F3B12BA21017A66D07DEC70050C59BDCE7A3D439E6E87642018DBF52E11DD66C011DC00E91901A9C39C3358AE4BD3AB050F25690F9638C851216545E0A7FE8B282D6421384BC2077383802A6C77638C65D44CE0287040CAFB0B6662AC3A19897C7204D2B75EA151048DC9DD30A0C6A1C53E2BD543B5347589EABAE09CDA914C89334AD25B3C2EBDED76E32C34570BD6F4102D2C7AD5E5FF393DC9D32139D072C4C42448535994E398F3AF1E3F3991B2A7E28BD0A7CD44E2DEA378844D2AE23C92E2FC29D086163E747D9E56E7B3"
)


def version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", value):
        raise ValueError("Invalid stable version")
    return tuple(map(int, value.split(".")))


def verify(envelope: dict) -> dict:
    """Strict RSASSA-PKCS1-v1_5 SHA-256 verification with a pinned 3072-bit key."""
    raw = base64.b64decode(envelope["payload"], validate=True)
    signature = base64.b64decode(envelope["signature"], validate=True)
    if len(raw) > 32768 or len(signature) != 384:
        raise ValueError("Invalid signature size")
    n = int(PUBLIC_MODULUS, 16)
    value = int.from_bytes(signature, "big")
    if value >= n:
        raise ValueError("Invalid signature")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(raw).digest()
    expected = b"\x00\x01" + b"\xff" * (384 - len(digest_info) - 3) + b"\x00" + digest_info
    actual = pow(value, 65537, n).to_bytes(384, "big")
    if not hmac.compare_digest(actual, expected):
        raise ValueError("Release signature verification failed")
    manifest = json.loads(raw)
    version(manifest["version"])
    if manifest.get("protocol") != PROTOCOL or manifest.get("schema") != SCHEMA or manifest.get("rollback_safe") is not True:
        raise ValueError("Release requires manual compatibility/migration review")
    if manifest.get("min_agent_protocol", 999) > PROTOCOL:
        raise ValueError("Incompatible agents")
    names = ["server.tar.gz", "codex-stats-agent.exe"]
    if "agent-source.zip" in manifest["assets"]:
        names.append("agent-source.zip")
    for name in names:
        item = manifest["assets"][name]
        if not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or not 0 < item["size"] <= 150_000_000:
            raise ValueError("Invalid asset metadata")
    return manifest


def check_file(path: Path, info: dict) -> None:
    if path.stat().st_size != info["size"] or hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
        raise ValueError("Release artifact checksum mismatch")


def download(url: str, destination: Path, *, limit: int = 150_000_000) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "codex-stats-updater"})
    with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as stream:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("Download too large")
            stream.write(chunk)
    temporary.replace(destination)


def asset_url(release_version: str, name: str) -> str:
    version(release_version)
    if name not in ("release.json", "server.tar.gz", "codex-stats-agent.exe", "agent-source.zip"):
        raise ValueError("Invalid asset name")
    return f"https://github.com/{REPOSITORY}/releases/download/v{release_version}/{name}"
