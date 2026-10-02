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
from dataclasses import dataclass

import httpx

from .bot import BotUI, TelegramNotifier
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
        return await self.bot.handle_update(update)

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
    scanner = Scanner(db, settings, client, notifier)
    bot = BotUI(api, db, scanner, settings, handle_start=False)
    task = asyncio.create_task(scanner.run())
    return ScannerApp(scanner, bot, task, client, db)
