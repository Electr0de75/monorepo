"""Settings loaded from environment variables (and a .env file if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .chains import CHAINS

try:  # optional dependency
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


@dataclass
class EvmRpc:
    ws_url: str | None
    http_url: str | None

    @property
    def usable(self) -> bool:
        return bool(self.http_url)


@dataclass
class Settings:
    telegram_token: str
    allowed_users: set[int]
    notify_chat_id: int | None
    timezone: str = "Europe/Paris"
    db_path: str = "ticker_scanner.db"
    evm_rpc: dict[str, EvmRpc] = field(default_factory=dict)
    evm_getlogs_max_range: int = 10
    evm_poll_interval: float = 2.0
    evm_liquidity_check_interval: float = 6.0
    pumpportal_enabled: bool = True
    solana_ws_url: str | None = None
    solana_http_url: str | None = None
    dexscreener_interval: float = 15.0
    pending_liquidity_max_age_h: float = 72.0
    results_page_size: int = 5

    @property
    def target_chat_id(self) -> int | None:
        if self.notify_chat_id is not None:
            return self.notify_chat_id
        return min(self.allowed_users) if self.allowed_users else None


def _int_set(raw: str) -> set[int]:
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "oui", "on"}


def _derive_http(ws_url: str | None) -> str | None:
    if not ws_url:
        return None
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://"):]
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://"):]
    return None


def load_settings(env: dict[str, str] | None = None) -> Settings:
    if env is None:
        if load_dotenv is not None:
            load_dotenv()
        env = dict(os.environ)

    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN manquant (voir .env.example)")

    notify = env.get("TELEGRAM_NOTIFY_CHAT_ID", "").strip()

    evm_rpc: dict[str, EvmRpc] = {}
    for chain in CHAINS.values():
        if not chain.is_evm or not chain.env_prefix:
            continue
        ws = env.get(f"{chain.env_prefix}_WS_URL", "").strip() or None
        http = env.get(f"{chain.env_prefix}_HTTP_URL", "").strip() or _derive_http(ws)
        if ws or http:
            evm_rpc[chain.key] = EvmRpc(ws_url=ws, http_url=http)

    solana_ws = env.get("SOLANA_WS_URL", "").strip() or None
    solana_http = env.get("SOLANA_HTTP_URL", "").strip() or _derive_http(solana_ws)

    return Settings(
        telegram_token=token,
        allowed_users=_int_set(env.get("TELEGRAM_ALLOWED_USERS", "")),
        notify_chat_id=int(notify) if notify else None,
        timezone=env.get("TIMEZONE", "Europe/Paris").strip() or "Europe/Paris",
        db_path=env.get("DB_PATH", "ticker_scanner.db").strip() or "ticker_scanner.db",
        evm_rpc=evm_rpc,
        evm_getlogs_max_range=int(env.get("EVM_GETLOGS_MAX_RANGE", "10") or 10),
        evm_poll_interval=float(env.get("EVM_POLL_INTERVAL", "2") or 2),
        evm_liquidity_check_interval=float(env.get("EVM_LIQUIDITY_CHECK_INTERVAL", "6") or 6),
        pumpportal_enabled=_bool(env.get("PUMPPORTAL_ENABLED"), True),
        solana_ws_url=solana_ws,
        solana_http_url=solana_http,
        dexscreener_interval=float(env.get("DEXSCREENER_INTERVAL", "15") or 15),
        pending_liquidity_max_age_h=float(env.get("PENDING_LIQUIDITY_MAX_AGE_H", "72") or 72),
    )
