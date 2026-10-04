"""Startup checks and log hygiene."""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import parse_qsl, urlsplit

import httpx

from .chains import CHAINS
from .config import Settings
from .health import redact, register_secrets
from .rpc import HttpRpc, RpcError
from .telegram_api import TelegramAPI, TelegramError

log = logging.getLogger(__name__)


async def check_telegram(api: TelegramAPI) -> str | None:
    """Bot username, or None if Telegram is unreachable. Exits on an invalid token."""
    try:
        me = await api.get_me()
    except TelegramError as exc:
        if exc.code in (401, 404):
            raise SystemExit("TELEGRAM_BOT_TOKEN refusé par Telegram : vérifie le token (@BotFather)") from None
        log.warning("Telegram : %s", exc)
        return None
    except httpx.HTTPError as exc:
        log.warning("Telegram injoignable au démarrage (%s), nouvel essai automatique", type(exc).__name__)
        return None
    username = (me or {}).get("username")
    log.info("Bot Telegram : @%s", username)
    return username


async def check_rpcs(settings: Settings, client: httpx.AsyncClient) -> list[str]:
    """Verify every configured RPC answers for the right chain.

    A RPC pointing to another chain (e.g. an Ethereum URL in BSC_WS_URL) is
    removed from ``settings.evm_rpc`` so it cannot produce wrong detections.
    Returns human-readable warnings for the status page.
    """
    warnings: list[str] = []

    async def check_evm(key: str) -> None:
        chain = CHAINS[key]
        rpc = settings.evm_rpc[key]
        try:
            chain_id = int(await HttpRpc(rpc.http_url, client, retries=1, name=f"{key}-check").call("eth_chainId"), 16)
        except (RpcError, TypeError, ValueError) as exc:
            warnings.append(f"{chain.label} : RPC injoignable au démarrage ({exc}), nouvel essai automatique")
            return
        if chain.chain_id is not None and chain_id != chain.chain_id:
            warnings.append(
                f"{chain.label} : le RPC répond pour la chaîne {chain_id} au lieu de {chain.chain_id}, "
                f"temps réel désactivé (vérifie {chain.env_prefix}_WS_URL / {chain.env_prefix}_HTTP_URL)"
            )
            settings.evm_rpc.pop(key, None)

    async def check_solana() -> None:
        try:
            await HttpRpc(settings.solana_http_url, client, retries=1, name="solana-check").call("getVersion")
        except RpcError as exc:
            warnings.append(f"Solana : RPC injoignable au démarrage ({exc}), nouvel essai automatique")

    jobs = [check_evm(key) for key in list(settings.evm_rpc)]
    if settings.solana_http_url:
        jobs.append(check_solana())
    await asyncio.gather(*jobs)
    for w in warnings:
        log.warning(w)
    return warnings


def check_env_permissions(path: str = ".env") -> str | None:
    """Warn when the .env holding the bot token is readable by other users (POSIX)."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if os.name == "posix" and mode & 0o077:
        msg = f"{path} est lisible par d'autres utilisateurs : lance « chmod 600 {path} »"
        log.warning(msg)
        return msg
    return None


def secrets_of(settings: Settings) -> list[str]:
    """Token and API keys found in the configuration (to mask them in logs)."""
    secrets = {settings.telegram_token}
    urls = [settings.solana_ws_url, settings.solana_http_url]
    for rpc in settings.evm_rpc.values():
        urls += [rpc.ws_url, rpc.http_url]
    for url in filter(None, urls):
        parts = urlsplit(url)
        secrets.update(seg for seg in parts.path.split("/") if len(seg) >= 16)
        secrets.update(v for _, v in parse_qsl(parts.query) if len(v) >= 8)
        if parts.password:
            secrets.add(parts.password)
    return sorted((s for s in secrets if s and len(s) >= 8), key=len, reverse=True)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str] | None = None):
        super().__init__(fmt)
        if secrets is not None:
            register_secrets(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def install_redaction(secrets: list[str]) -> None:
    register_secrets(secrets)
    for handler in logging.getLogger().handlers:
        fmt = handler.formatter._fmt if handler.formatter else "%(message)s"  # noqa: SLF001
        handler.setFormatter(RedactingFormatter(fmt, secrets))
