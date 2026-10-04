"""Small Telegram Bot API client (httpx), just what the scanner needs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

NO_PREVIEW = {"is_disabled": True}


class TelegramError(Exception):
    def __init__(self, code: int | None, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


class TelegramAPI:
    def __init__(self, token: str, client: httpx.AsyncClient, base_url: str = "https://api.telegram.org"):
        self.client = client
        self.url = f"{base_url}/bot{token}"

    async def call(self, method: str, params: dict[str, Any] | None = None, http_timeout: float = 20) -> Any:
        payload = {k: v for k, v in (params or {}).items() if v is not None}
        for _ in range(4):
            resp = await self.client.post(f"{self.url}/{method}", json=payload, timeout=http_timeout)
            try:
                data = resp.json()
            except ValueError:
                raise TelegramError(resp.status_code, resp.text[:200]) from None
            if data.get("ok"):
                return data.get("result")
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if data.get("error_code") == 429 and retry_after:
                log.warning("Telegram: limite atteinte, pause %ss", retry_after)
                await asyncio.sleep(float(retry_after) + 0.5)
                continue
            raise TelegramError(data.get("error_code"), data.get("description", ""))
        raise TelegramError(429, "Too Many Requests")

    async def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            http_timeout=timeout + 15,
        )

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None,
                           parse_mode: str | None = "HTML") -> dict:
        return await self.call("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": parse_mode,
            "link_preview_options": NO_PREVIEW, "reply_markup": reply_markup,
        })

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        try:
            await self.call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                "link_preview_options": NO_PREVIEW, "reply_markup": reply_markup,
            })
        except TelegramError as exc:
            if "message is not modified" not in exc.description:
                raise

    async def answer_callback(self, callback_id: str, text: str | None = None, alert: bool = False) -> None:
        try:
            await self.call("answerCallbackQuery", {
                "callback_query_id": callback_id, "text": text, "show_alert": alert or None,
            })
        except TelegramError:
            pass  # expired callback, nothing to do
