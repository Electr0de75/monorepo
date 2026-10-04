"""Settings loaded from environment variables (and a .env file if present)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .chains import CHAINS

log = logging.getLogger(__name__)

try:  # optional dependency
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env_file_path() -> str | None:
    """The .env to use: the current directory's first, else the project's."""
    for candidate in (os.path.join(os.getcwd(), ".env"), os.path.join(PROJECT_DIR, ".env")):
        if os.path.isfile(candidate):
            return candidate
    return None


def load_env_file() -> str | None:
    """Load the .env (never overriding variables already set). Returns its path."""
    path = env_file_path()
    if path and load_dotenv is not None:
        load_dotenv(path, override=False)
    return path


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
    evm_getlogs_max_range: int = 2000  # starting eth_getLogs range, lowered automatically if refused
    evm_poll_interval: float = 2.0
    evm_liquidity_check_interval: float = 6.0
    pumpportal_enabled: bool = True
    solana_ws_url: str | None = None
    solana_http_url: str | None = None
    dexscreener_interval: float = 15.0
    pending_liquidity_max_age_h: float = 72.0
    notify_flood_limit: int = 15             # alerts per entry per 10 min (0 = unlimited)
    notify_flood_bypass_liq_usd: float = 10_000.0
    results_page_size: int = 5

    @property
    def target_chat_id(self) -> int | None:
        if self.notify_chat_id is not None:
            return self.notify_chat_id
        return min(self.allowed_users) if self.allowed_users else None


def _int_set(raw: str, key: str) -> set[int]:
    out = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError:
                raise SystemExit(f"{key} invalide : « {part} » n'est pas un ID Telegram numérique") from None
    return out


def _number(env: dict[str, str], key: str, default: float, cast=float, minimum: float = 0):
    raw = (env.get(key) or "").strip()
    if not raw:
        return cast(default)
    try:
        value = cast(raw)
    except ValueError:
        raise SystemExit(f"{key} invalide : « {raw} » (nombre attendu)") from None
    if value < minimum:
        raise SystemExit(f"{key} invalide : {value} (minimum {minimum})")
    return value


def _url(env: dict[str, str], key: str, schemes: tuple[str, ...]) -> str | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    if not raw.startswith(schemes):
        raise SystemExit(f"{key} invalide : doit commencer par {' ou '.join(schemes)}")
    return raw


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
        load_env_file()
        env = dict(os.environ)

    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN manquant (voir .env.example)")

    if ":" not in token:
        raise SystemExit("TELEGRAM_BOT_TOKEN invalide (format attendu 123456:ABC…, donné par @BotFather)")

    notify_raw = env.get("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
    try:
        notify = int(notify_raw) if notify_raw else None
    except ValueError:
        raise SystemExit(f"TELEGRAM_NOTIFY_CHAT_ID invalide : « {notify_raw} » (ID numérique attendu)") from None

    evm_rpc: dict[str, EvmRpc] = {}
    for chain in CHAINS.values():
        if not chain.is_evm or not chain.env_prefix:
            continue
        ws = _url(env, f"{chain.env_prefix}_WS_URL", ("wss://", "ws://"))
        http = _url(env, f"{chain.env_prefix}_HTTP_URL", ("https://", "http://")) or _derive_http(ws)
        if ws or http:
            evm_rpc[chain.key] = EvmRpc(ws_url=ws, http_url=http)

    solana_ws = _url(env, "SOLANA_WS_URL", ("wss://", "ws://"))
    solana_http = _url(env, "SOLANA_HTTP_URL", ("https://", "http://")) or _derive_http(solana_ws)

    timezone = env.get("TIMEZONE", "Europe/Paris").strip() or "Europe/Paris"
    try:
        ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - unknown zone or no tz database (Windows without tzdata)
        log.warning("TIMEZONE « %s » inconnue, heure locale utilisée", timezone)

    return Settings(
        telegram_token=token,
        allowed_users=_int_set(env.get("TELEGRAM_ALLOWED_USERS", ""), "TELEGRAM_ALLOWED_USERS"),
        notify_chat_id=notify,
        timezone=timezone,
        db_path=env.get("DB_PATH", "ticker_scanner.db").strip() or "ticker_scanner.db",
        evm_rpc=evm_rpc,
        evm_getlogs_max_range=_number(env, "EVM_GETLOGS_MAX_RANGE", 2000, int, 1),
        evm_poll_interval=_number(env, "EVM_POLL_INTERVAL", 2, float, 0.2),
        evm_liquidity_check_interval=_number(env, "EVM_LIQUIDITY_CHECK_INTERVAL", 6, float, 1),
        pumpportal_enabled=_bool(env.get("PUMPPORTAL_ENABLED"), True),
        solana_ws_url=solana_ws,
        solana_http_url=solana_http,
        dexscreener_interval=_number(env, "DEXSCREENER_INTERVAL", 15, float, 2),
        pending_liquidity_max_age_h=_number(env, "PENDING_LIQUIDITY_MAX_AGE_H", 72, float, 1),
        notify_flood_limit=_number(env, "NOTIFY_FLOOD_LIMIT", 15, int, 0),
        notify_flood_bypass_liq_usd=_number(env, "NOTIFY_FLOOD_BYPASS_LIQ_USD", 10_000, float, 0),
    )
