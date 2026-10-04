"""Entry point: ``python -m ticker_scanner``."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import httpx

from .bot import BotUI, TelegramNotifier
from .checks import check_rpcs, check_telegram, install_redaction, secrets_of
from .config import load_dotenv, load_settings
from .db import Database
from .scanner import Scanner
from .telegram_api import TelegramAPI

log = logging.getLogger("ticker_scanner")


async def main() -> None:
    settings = load_settings()
    install_redaction(secrets_of(settings))
    if not settings.allowed_users:
        log.warning("TELEGRAM_ALLOWED_USERS vide : envoie /start au bot pour obtenir ton ID")
    db = Database(settings.db_path)
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    async with httpx.AsyncClient(limits=limits) as client:
        api = TelegramAPI(settings.telegram_token, client)
        await check_telegram(api)
        warnings = await check_rpcs(settings, client)
        chat_id = settings.target_chat_id
        if chat_id is None:
            log.warning("Aucun chat de notification : renseigne TELEGRAM_ALLOWED_USERS ou TELEGRAM_NOTIFY_CHAT_ID")
        notifier = TelegramNotifier(api, chat_id) if chat_id is not None else None
        scanner = Scanner(db, settings, client, notifier)
        scanner.config_warnings = warnings
        bot = BotUI(api, db, scanner, settings)
        rt = [k for k, v in bot.realtime().items() if v]
        log.info("Temps réel configuré pour : %s", ", ".join(rt) or "aucune chaîne (DexScreener seul)")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        scanner_task = asyncio.create_task(scanner.run())
        # The menu may stop (token used elsewhere); notifications keep running.
        bot_task = asyncio.create_task(bot.run_polling())
        stop_task = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({scanner_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            log.info("Arrêt du scanner…")
            for task in (scanner_task, bot_task, stop_task):
                task.cancel()
            await asyncio.gather(scanner_task, bot_task, stop_task, return_exceptions=True)
            db.close()


def run() -> None:
    if load_dotenv is not None:
        load_dotenv()  # so LOG_LEVEL from .env applies
    level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(
        level=level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
