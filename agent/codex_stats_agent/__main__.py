from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path

from . import __version__
from .config import APP_DIR, AgentConfig, DEFAULT_CONFIG_PATH, create_config
from .quota import QuotaError, account_fingerprint, fetch_weekly_quota, latest_logged_quota
from .watcher import CodexWatcher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Локальный Windows-агент статистики Codex")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="создать локальный config.json")
    configure.add_argument("--server-url", required=True)
    configure.add_argument("--api-key", required=True)
    configure.add_argument("--user-name", required=True)
    configure.add_argument("--codex-home")

    subparsers.add_parser("watch", help="постоянно отслеживать новые задания Codex")
    subparsers.add_parser("status", help="проверить конфигурацию, авторизацию и недельный лимит")
    enrollment = subparsers.add_parser("enroll", help="подключиться по одноразовому коду из Telegram")
    enrollment.add_argument("--server-url", required=True)
    enrollment.add_argument("--code", required=True)
    update = subparsers.add_parser("apply-update", help=argparse.SUPPRESS)
    update.add_argument("--stage", required=True, type=Path)
    source_update = subparsers.add_parser("apply-source-update", help=argparse.SUPPRESS)
    source_update.add_argument("--stage", required=True, type=Path)
    subparsers.add_parser("stop", help="запросить безопасную остановку агента")
    legacy = subparsers.add_parser("legacy-upgrade", help="однократное обновление 0.3.0 в простое")
    legacy.add_argument("--stage", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "stop":
        directory = APP_DIR / "updates"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stop-request").touch()
        return 0
    if args.command == "apply-source-update":
        import os
        from .source_update import apply_source, command
        from .instance import single_instance
        from .updater import wait_for_exit
        import json
        import subprocess
        root = Path(os.environ["CODEX_STATS_INSTALL_ROOT"])
        with single_instance(root / "update.lock"):
            try:
                return apply_source(args.stage)
            except Exception:
                guard = json.loads((args.stage / "guard.json").read_text())
                if wait_for_exit(guard["pid"]):
                    subprocess.Popen(command(root), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return 1
    if args.command == "enroll":
        from .updater import enroll
        enroll(args.server_url, args.code, args.config)
        print("Компьютер подключён. Конфигурация сохранена.")
        return 0
    if args.command == "apply-update":
        from .updater import apply_update
        return apply_update(args.stage)
    if args.command == "legacy-upgrade":
        from .updater import legacy_upgrade
        return legacy_upgrade(args.stage)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.StreamHandler(sys.stdout) if sys.stdout is not None else RotatingFileHandler(
        APP_DIR / "agent.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler],
    )

    if args.command == "configure":
        config = create_config(args.server_url, args.api_key, args.user_name, codex_home=args.codex_home)
        try:
            existing = AgentConfig.load(args.config)
        except (FileNotFoundError, TypeError, ValueError, OSError):
            existing = None
        if existing:
            config.machine_id = existing.machine_id
            config.machine_name = existing.machine_name
            if not args.codex_home:
                config.codex_home = existing.codex_home
        config.save(args.config)
        print(f"Конфигурация сохранена: {args.config}")
        print(f"ПК: {config.machine_name} ({config.machine_id})")
        return 0

    try:
        config = AgentConfig.load(args.config)
    except (FileNotFoundError, TypeError, ValueError) as error:
        print(f"Ошибка конфигурации: {error}. Сначала выполните configure.", file=sys.stderr)
        return 2

    if args.command == "status":
        codex_home = Path(config.codex_home)
        try:
            fingerprint = account_fingerprint(codex_home)
            quota = fetch_weekly_quota(codex_home)
        except QuotaError as error:
            fingerprint = "недоступен"
            quota = latest_logged_quota(codex_home)
            print(f"Прямой запрос лимита не выполнен: {error}")
        print(f"Пользователь: {config.user_name}")
        print(f"Компьютер: {config.machine_name} ({config.machine_id})")
        print(f"Аккаунт: {fingerprint}")
        if quota and quota.used_percent is not None:
            print(f"Недельный лимит использован: {quota.used_percent:.2f}%")
            print(f"Источник: {quota.source}; сброс: {quota.resets_at or 'неизвестно'}")
            return 0
        print("Снимок недельного лимита не найден", file=sys.stderr)
        return 3

    from .instance import single_instance
    with single_instance(APP_DIR / "agent.lock"):
        CodexWatcher(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
