"""Run the scanner inside an existing Python Telegram bot.

    from ticker_scanner.embed import start_embedded

    scanner_app = await start_embedded()            # reads the same .env
    ...
    # in the host bot, for every raw update dict:
    if await scanner_app.handle_update(update_dict):
        return                                      # it was for the scanner

python-telegram-bot: ``update.to_dict()``; aiogram 3: ``update.model_dump(mode="json", exclude_none=True)``.
The host bot keeps its own polling/webhook; the scanner never calls getUpdates.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from .bot import BotUI, TelegramNotifier
from .checks import check_rpcs, check_telegram
from .config import Settings, load_settings
from .db import Database
from .scanner import Scanner
from .telegram_api import TelegramAPI


@dataclass
class ScannerApp:
    scanner: Scanner
    bot: BotUI
    task: asyncio.Task
    client: httpx.AsyncClient
    db: Database

    async def handle_update(self, update: dict) -> bool:
        """Never raises: a scanner problem must not break the host bot's handlers."""
        try:
            return await self.bot.handle_update(update)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("scanner: update non traitée")
            return False

    async def close(self) -> None:
        self.task.cancel()
        await self.client.aclose()
        self.db.close()


async def start_embedded(settings: Settings | None = None) -> ScannerApp:
    settings = settings or load_settings()
    client = httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    db = Database(settings.db_path)
    api = TelegramAPI(settings.telegram_token, client)
    chat_id = settings.target_chat_id
    notifier = TelegramNotifier(api, chat_id) if chat_id is not None else None
    username = await check_telegram(api)
    warnings = await check_rpcs(settings, client)
    scanner = Scanner(db, settings, client, notifier)
    scanner.config_warnings = warnings
    bot = BotUI(api, db, scanner, settings, handle_start=False, bot_username=username)
    task = asyncio.create_task(scanner.run())
    return ScannerApp(scanner, bot, task, client, db)
