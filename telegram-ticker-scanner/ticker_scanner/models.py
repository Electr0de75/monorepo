"""Data classes shared by the sources, the scanner and the Telegram UI."""

from __future__ import annotations

import re
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

# Address formats. Everything coming from DexScreener / PumpPortal / RPC nodes
# is validated against these before it is stored, linked or displayed.
_EVM_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_EVM_POOL_ID = re.compile(r"^0x[0-9a-f]{64}$")  # Uniswap v4 pool ids
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def valid_address(kind: str, value: str | None, *, pool: bool = False) -> bool:
    """`kind` is the chain kind ("evm" | "solana"); EVM values must be lowercased already."""
    if not isinstance(value, str):
        return False
    if kind == "evm":
        return bool(_EVM_ADDRESS.match(value) or (pool and _EVM_POOL_ID.match(value)))
    return bool(_SOLANA_ADDRESS.match(value))


# Bidi overrides / isolates can visually reorder a message (spoofing).
_BIDI = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C)}


def clean_text(raw: str | None, max_len: int) -> str:
    """Strip control and bidi characters, collapse whitespace, truncate."""
    if not raw or not isinstance(raw, (str, int, float)):
        return ""
    chars = []
    for ch in str(raw):
        if ch in _BIDI:
            continue
        chars.append(" " if unicodedata.category(ch) == "Cc" else ch)
    text = " ".join("".join(chars).split())
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def normalize_ticker(raw: str | None) -> str:
    if not raw or not isinstance(raw, str):
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
    # Liquidity landed on a pair that existed before we saw it: notify 🟢 only.
    pre_existing_pair: bool = False
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
