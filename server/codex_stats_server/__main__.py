from __future__ import annotations

import logging
import signal
import threading
import os
from pathlib import Path

from .config import ServerConfig
from .database import StatsDatabase
from .http_api import create_server
from .telegram_bot import TelegramBot
from .lifecycle import Lifecycle


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = ServerConfig.from_environment()
    except (ValueError, TypeError) as error:
        raise SystemExit(f"Ошибка конфигурации: {error}") from error

    database = StatsDatabase(config.database_path)
    lifecycle = Lifecycle(database, os.environ.get("CODEX_STATS_PUBLIC_URL", ""),
                          Path(os.environ.get("CODEX_STATS_RELEASE_CACHE", "/var/cache/codex-stats/releases")),
                          local_url=os.environ.get("CODEX_STATS_LOCAL_URL", ""))
    bot = None
    if config.telegram_bot_token:
        bot = TelegramBot(
            config.telegram_bot_token,
            config.telegram_admin_chat_ids,
            config.telegram_initial_viewer_chat_ids,
            database,
            lifecycle,
        )
        bot.start()
        lifecycle.announce = bot.announce_maintenance
    else:
        def unavailable_notice(text):
            raise RuntimeError("Telegram must be configured for automatic maintenance")
        lifecycle.announce = unavailable_notice

    notifier = bot.notify_registered if bot and config.notify_completions else None
    server = create_server(config.host, config.port, database, config.agent_api_key, notifier, lifecycle)

    def stop(*_: object) -> None:
        threading.Thread(target=server.shutdown, name="http-shutdown", daemon=True).start()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, stop)

    logging.info("Codex Stats Server слушает %s:%s", config.host, config.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if bot:
            bot.stop()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
