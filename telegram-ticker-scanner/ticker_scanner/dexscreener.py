"""DexScreener API client + conversion of its pairs into detections."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from .chains import DEXSCREENER_TO_CHAIN, norm_address
from .models import LAUNCH, PAIR, Detection

log = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"
BATCH = 30

# dexIds DexScreener uses for bonding-curve launchpads
LAUNCHPAD_DEX_IDS = {
    "pumpfun", "pump.fun", "launchlab", "letsbonk", "bonk", "moonshot", "boop", "believe", "bags",
    "heaven", "jupstudio", "stonkfun", "fourmeme", "four.meme", "flap", "pons", "long", "o1", "pair",
    "hoodfun", "bankr", "clanker", "zora",
}
# dexIds of regular AMMs (anything else seen for a token that already has a
# launch result is treated as that launch, not as a new pair)
KNOWN_DEX_IDS = {
    "uniswap", "pancakeswap", "sushiswap", "raydium", "orca", "meteora", "pumpswap", "aerodrome",
    "velodrome", "traderjoe", "quickswap", "camelot", "balancer", "curve", "biswap", "thena",
    "baseswap", "alienbase", "fluxbeam", "lifinity", "phoenix", "openbook", "solidly",
}
DEX_NAMES = {
    "uniswap": "Uniswap", "pancakeswap": "PancakeSwap", "sushiswap": "SushiSwap", "raydium": "Raydium",
    "orca": "Orca", "meteora": "Meteora", "pumpswap": "PumpSwap", "pumpfun": "pump.fun",
    "aerodrome": "Aerodrome", "launchlab": "LaunchLab", "fourmeme": "four.meme", "moonshot": "Moonshot",
}


class RateLimiter:
    def __init__(self, per_minute: int):
        self.interval = 60.0 / per_minute
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class DexScreener:
    def __init__(self, client: httpx.AsyncClient, per_minute: int = 250):
        self.client = client
        self.limiter = RateLimiter(per_minute)

    async def _get(self, path: str) -> Any:
        for attempt in range(3):
            await self.limiter.wait()
            try:
                resp = await self.client.get(BASE_URL + path, timeout=20)
            except httpx.HTTPError as exc:
                log.debug("dexscreener %s: %s", path, exc)
                await asyncio.sleep(1 + attempt * 2)
                continue
            if resp.status_code == 429:
                await asyncio.sleep(5 + attempt * 5)
                continue
            if resp.status_code != 200:
                log.debug("dexscreener %s: HTTP %s", path, resp.status_code)
                return None
            try:
                return resp.json()
            except ValueError:
                return None
        return None

    async def search(self, query: str) -> list[dict]:
        data = await self._get(f"/latest/dex/search?q={quote(query)}")
        return (data or {}).get("pairs") or []

    async def pairs(self, chain_id: str, addresses: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(addresses), BATCH):
            chunk = ",".join(addresses[i:i + BATCH])
            data = await self._get(f"/latest/dex/pairs/{chain_id}/{chunk}")
            out.extend((data or {}).get("pairs") or [])
        return out

    async def tokens(self, chain_id: str, addresses: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(addresses), BATCH):
            chunk = ",".join(addresses[i:i + BATCH])
            data = await self._get(f"/tokens/v1/{chain_id}/{chunk}")
            if isinstance(data, list):
                out.extend(data)
        return out


def dex_label(pair: dict) -> str:
    dex_id = (pair.get("dexId") or "").lower()
    name = DEX_NAMES.get(dex_id, dex_id or "DEX")
    labels = [lbl for lbl in (pair.get("labels") or []) if isinstance(lbl, str)]
    return f"{name} {' '.join(labels)}".strip()


def is_launchpad_pair(pair: dict) -> bool:
    dex_id = (pair.get("dexId") or "").lower()
    labels = {str(lbl).upper() for lbl in (pair.get("labels") or [])}
    return dex_id in LAUNCHPAD_DEX_IDS or "DBC" in labels


def is_known_dex(pair: dict) -> bool:
    return (pair.get("dexId") or "").lower() in KNOWN_DEX_IDS


def metrics(pair: dict) -> tuple[float | None, float | None]:
    liq = (pair.get("liquidity") or {}).get("usd")
    mc = pair.get("marketCap") or pair.get("fdv")
    return (float(liq) if liq is not None else None, float(mc) if mc is not None else None)


def pair_to_detection(pair: dict, *, side: str = "baseToken", force_launch: bool = False) -> Detection | None:
    chain = DEXSCREENER_TO_CHAIN.get(pair.get("chainId") or "")
    token = pair.get(side) or {}
    other = pair.get("quoteToken" if side == "baseToken" else "baseToken") or {}
    if chain is None or not token.get("address") or not pair.get("pairAddress"):
        return None
    liq, mc = metrics(pair)
    created_ms = pair.get("pairCreatedAt")
    launch = force_launch or is_launchpad_pair(pair)
    return Detection(
        chain=chain,
        kind=LAUNCH if launch else PAIR,
        token_address=norm_address(chain, token["address"]),
        symbol=token.get("symbol") or "",
        name=token.get("name") or "",
        pair_address=norm_address(chain, pair["pairAddress"]),
        pool_kind="launchpad" if launch else "dex",
        dex=dex_label(pair),
        quote_symbol=other.get("symbol"),
        quote_address=norm_address(chain, other.get("address")),
        has_liquidity=bool(liq and liq > 0),
        source="dexscreener",
        liquidity_usd=liq,
        market_cap=mc,
        created_at=(created_ms / 1000.0) if created_ms else 0.0,
    )


def best_pair(pairs: list[dict], pair_address: str | None, chain: str) -> dict | None:
    """The pair matching ``pair_address`` if listed, else the most liquid one."""
    if not pairs:
        return None
    if pair_address:
        for p in pairs:
            if norm_address(chain, p.get("pairAddress")) == pair_address:
                return p
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
