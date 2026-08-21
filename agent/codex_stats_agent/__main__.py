from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import AgentConfig, DEFAULT_CONFIG_PATH, create_config
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if args.command == "configure":
        config = create_config(args.server_url, args.api_key, args.user_name, codex_home=args.codex_home)
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

    CodexWatcher(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

