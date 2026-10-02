"""Data classes shared by the sources, the scanner and the Telegram UI."""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field

# Detection kinds
LAUNCH = "launch"        # token created on a launchpad (bonding curve)
PAIR = "pair"            # DEX pair / pool created
LIQUIDITY = "liquidity"  # liquidity added to an already known pair

# Result statuses
STATUS_LAUNCH = "launch"
STATUS_PAIR = "pair"   # pair created, no liquidity yet
STATUS_LIQ = "liq"     # liquidity added

MAX_TICKERS = 3


def normalize_ticker(raw: str | None) -> str:
    if not raw:
        return ""
    t = unicodedata.normalize("NFKC", raw).strip().lstrip("$").strip()
    return "".join(t.split()).upper()


def parse_tickers(text: str) -> list[str]:
    """'$abc, def  ghi' -> ['ABC', 'DEF', 'GHI'] (deduplicated, order kept)."""
    parts = text.replace(",", " ").replace(";", " ").replace("\n", " ").split(" ")
    out: list[str] = []
    for p in parts:
        t = normalize_ticker(p)
        if t and t not in out:
            out.append(t)
    return out


@dataclass
class Entry:
    id: int
    name: str
    tickers: list[str]
    chains: list[str]
    paused: bool
    created_at: float
    scan_since: float

    @property
    def active(self) -> bool:
        return not self.paused


@dataclass
class Result:
    id: int
    entry_id: int
    chain: str
    result_key: str
    kind: str
    status: str
    token_address: str
    symbol: str
    name: str
    pair_address: str | None
    pool_kind: str | None
    dex: str
    quote_symbol: str | None
    quote_address: str | None
    source: str
    tx_hash: str | None
    found_at: float
    liq_at: float | None
    liquidity_usd: float | None
    market_cap: float | None
    updated_at: float | None
    notify_message_id: int | None = None

    @property
    def has_liquidity(self) -> bool:
        return self.status in (STATUS_LIQ, STATUS_LAUNCH)


@dataclass
class Detection:
    chain: str
    kind: str
    token_address: str
    symbol: str = ""
    name: str = ""
    pair_address: str | None = None
    pool_kind: str | None = None  # v2 | v3 | v4 | solidly | slipstream | launchpad | dex
    dex: str = ""
    quote_symbol: str | None = None
    quote_address: str | None = None
    has_liquidity: bool = False
    source: str = ""
    tx_hash: str | None = None
    liquidity_usd: float | None = None
    market_cap: float | None = None
    market_cap_note: str | None = None  # e.g. "32.5 SOL" when USD is unknown
    created_at: float = field(default_factory=time.time)

    @property
    def result_key(self) -> str:
        if self.kind == LAUNCH:
            return f"launch:{self.token_address}"
        return self.pair_address or f"token:{self.token_address}"


# Notification labels
LABEL_LAUNCH = "launch"
LABEL_PAIR = "pair"
LABEL_LIQ = "liq"
