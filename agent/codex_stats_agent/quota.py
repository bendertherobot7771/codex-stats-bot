from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .models import QuotaSnapshot


DEFAULT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


class QuotaError(RuntimeError):
    pass


def account_fingerprint(codex_home: Path) -> str:
    auth = _load_auth(codex_home)
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    identity = tokens.get("account_id") or _jwt_subject(tokens.get("access_token"))
    if not identity:
        raise QuotaError("В auth.json не найден стабильный идентификатор аккаунта")
    return hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:24]


def fetch_weekly_quota(codex_home: Path, timeout: float = 10.0) -> QuotaSnapshot:
    auth = _load_auth(codex_home)
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    access_token = tokens.get("access_token") or auth.get("OPENAI_API_KEY")
    account_id = tokens.get("account_id")
    if not access_token:
        raise QuotaError("В auth.json отсутствует access_token; войдите в Codex")

    request = urllib.request.Request(DEFAULT_USAGE_URL)
    request.add_header("Authorization", f"Bearer {access_token}")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "codex-stats-agent/0.1.1")
    if account_id:
        request.add_header("ChatGPT-Account-Id", str(account_id))

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise QuotaError(f"Codex usage endpoint вернул HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise QuotaError(f"Не удалось получить лимит Codex: {error}") from error

    snapshot = quota_from_usage_response(payload)
    snapshot.source = "oauth_usage_endpoint"
    snapshot.captured_at = time.time()
    return snapshot


def quota_from_usage_response(payload: dict[str, Any]) -> QuotaSnapshot:
    rate_limit = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
    windows = []
    for name in ("primary_window", "secondary_window"):
        window = rate_limit.get(name)
        if isinstance(window, dict):
            windows.append(window)

    if not windows:
        raise QuotaError("Ответ Codex не содержит primary/secondary rate-limit window")

    weekly = next(
        (item for item in windows if _integer(item.get("limit_window_seconds")) >= 86_400),
        windows[-1],
    )
    seconds = _integer(weekly.get("limit_window_seconds")) or None
    return QuotaSnapshot(
        used_percent=_float(weekly.get("used_percent", weekly.get("usage_percent"))),
        window_minutes=(seconds // 60 if seconds else None),
        resets_at=_integer(weekly.get("reset_at")) or None,
        source="usage_response",
        captured_at=time.time(),
    )


def quota_from_log_payload(rate_limits: dict[str, Any] | None) -> QuotaSnapshot | None:
    if not isinstance(rate_limits, dict):
        return None
    candidates = [rate_limits.get("primary"), rate_limits.get("secondary")]
    windows = [item for item in candidates if isinstance(item, dict)]
    if not windows:
        return None
    weekly = next(
        (item for item in windows if _integer(item.get("window_minutes")) >= 1_440),
        windows[-1],
    )
    return QuotaSnapshot(
        used_percent=_float(weekly.get("used_percent")),
        window_minutes=_integer(weekly.get("window_minutes")) or None,
        resets_at=_integer(weekly.get("resets_at")) or None,
        source="codex_session_log",
        captured_at=time.time(),
    )


def latest_logged_quota(codex_home: Path, max_files: int = 20) -> QuotaSnapshot | None:
    session_root = codex_home / "sessions"
    if not session_root.exists():
        return None
    files = sorted(session_root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)[:max_files]
    newest: tuple[float, QuotaSnapshot] | None = None
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    item = json.loads(line)
                    payload = item.get("payload") if item.get("type") == "event_msg" else None
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    snapshot = quota_from_log_payload(payload.get("rate_limits"))
                    if snapshot:
                        timestamp = _timestamp(item.get("timestamp"), path.stat().st_mtime)
                        snapshot.captured_at = timestamp
                        if newest is None or timestamp > newest[0]:
                            newest = (timestamp, snapshot)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return newest[1] if newest else None


def _load_auth(codex_home: Path) -> dict[str, Any]:
    path = codex_home / "auth.json"
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError as error:
        raise QuotaError(f"Не найден {path}; войдите в Codex") from error
    except (OSError, json.JSONDecodeError) as error:
        raise QuotaError(f"Не удалось прочитать {path}: {error}") from error
    if not isinstance(value, dict):
        raise QuotaError("auth.json имеет неожиданный формат")
    return value


def _jwt_subject(token: Any) -> str | None:
    if not isinstance(token, str):
        return None
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body).decode("utf-8"))
        return claims.get("sub")
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback
