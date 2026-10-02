"""Entry point: ``python -m ticker_scanner``."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from .bot import BotUI, TelegramNotifier
from .config import load_settings
from .db import Database
from .scanner import Scanner
from .telegram_api import TelegramAPI


async def main() -> None:
    settings = load_settings()
    if not settings.allowed_users:
        logging.warning("TELEGRAM_ALLOWED_USERS vide : envoie /start au bot pour obtenir ton ID")
    db = Database(settings.db_path)
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    async with httpx.AsyncClient(limits=limits) as client:
        api = TelegramAPI(settings.telegram_token, client)
        chat_id = settings.target_chat_id
        notifier = TelegramNotifier(api, chat_id) if chat_id is not None else None
        scanner = Scanner(db, settings, client, notifier)
        bot = BotUI(api, db, scanner, settings)
        rt = [k for k, v in bot.realtime().items() if v]
        logging.info("Temps réel configuré pour : %s", ", ".join(rt) or "aucune chaîne (DexScreener seul)")
        scanner_task = asyncio.create_task(scanner.run())
        bot_task = asyncio.create_task(bot.run_polling())
        try:
            # The menu may stop (token used elsewhere); notifications keep running.
            await scanner_task
        finally:
            bot_task.cancel()
            db.close()


def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
