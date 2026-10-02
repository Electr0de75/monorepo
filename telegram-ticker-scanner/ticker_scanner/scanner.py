"""Orchestrator: matches detections with the entries, stores results, notifies.

It also starts/stops the real-time sources depending on which chains the
active entries need, polls DexScreener, tracks pending liquidity and keeps
market caps fresh.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

import httpx

from .chains import CHAINS, norm_address
from .config import Settings
from .db import Database
from .dexscreener import DexScreener, best_pair, is_known_dex, metrics, pair_to_detection
from .evm_watcher import EvmWatcher
from .models import (
    LABEL_LAUNCH, LABEL_LIQ, LABEL_PAIR, LAUNCH, LIQUIDITY, PAIR, STATUS_LAUNCH, STATUS_LIQ, STATUS_PAIR,
    Detection, Entry, Result, normalize_ticker,
)
from .solana import PumpPortalWatcher, SolanaLaunchpadWatcher

log = logging.getLogger(__name__)

# DexScreener pairs created before the entry (minus this margin) are ignored.
SCAN_MARGIN_S = 600
SYNC_INTERVAL_S = 10
DS_LIQUIDITY_INTERVAL_S = 20
ENRICH_INTERVAL_S = 120
ENRICH_MAX_AGE_S = 24 * 3600
FOLLOWUP_DELAYS_S = (20, 60, 180)
NEW_PAIR_LOOKUP_DELAYS_S = (3, 10, 30, 90)


class Notifier(Protocol):
    async def send(self, entry: Entry, result: Result, labels: list[str], note: str | None) -> int | None: ...

    async def edit(self, entry: Entry, result: Result, labels: list[str], note: str | None,
                   message_id: int) -> None: ...


class Scanner:
    def __init__(self, db: Database, settings: Settings, http_client: httpx.AsyncClient,
                 notifier: Notifier | None = None, *, dexscreener: DexScreener | None = None,
                 ws_connect=None):
        self.db = db
        self.settings = settings
        self.http = http_client
        self.notifier = notifier
        self.ds = dexscreener or DexScreener(http_client)
        self.ws_connect = ws_connect
        self._lock = asyncio.Lock()
        self._entries: dict[int, Entry] = {}
        self._watch: dict[str, set[str]] = {}
        self._sources: dict[str, asyncio.Task] = {}
        self._notify_queue: asyncio.Queue = asyncio.Queue()
        # result id -> (telegram message id, labels, note) of its last notification
        self._last_notif: dict[int, tuple[int, list[str], str | None]] = {}
        self._background: set[asyncio.Task] = set()
        self._sync_now = asyncio.Event()
        self.reload()

    # ---- watch index ---------------------------------------------------
    def reload(self) -> None:
        """Call after any entry change (create/edit/pause/delete)."""
        entries = self.db.active_entries()
        self._entries = {e.id: e for e in entries}
        watch: dict[str, set[str]] = {}
        for e in entries:
            for chain in e.chains:
                watch.setdefault(chain, set()).update(e.tickers)
        self._watch = watch
        self._sync_now.set()

    def is_watched(self, chain: str, symbol: str | None) -> bool:
        tickers = self._watch.get(chain)
        return bool(tickers) and normalize_ticker(symbol) in tickers

    def desired_chains(self) -> set[str]:
        return {c for c, t in self._watch.items() if t}

    def ticker_index(self) -> dict[str, set[str]]:
        index: dict[str, set[str]] = {}
        for chain, tickers in self._watch.items():
            for t in tickers:
                index.setdefault(t, set()).add(chain)
        return index

    def source_names(self) -> list[str]:
        return sorted(name for name, task in self._sources.items() if not task.done())

    # ---- detections ----------------------------------------------------
    async def on_detection(self, det: Detection) -> None:
        det.token_address = norm_address(det.chain, det.token_address)
        det.pair_address = norm_address(det.chain, det.pair_address)
        det.quote_address = norm_address(det.chain, det.quote_address)
        async with self._lock:
            if det.kind == LIQUIDITY:
                self._on_liquidity(det)
                return
            symbol = normalize_ticker(det.symbol)
            for entry in list(self._entries.values()):
                if det.chain not in entry.chains or symbol not in entry.tickers:
                    continue
                if det.created_at < entry.scan_since - SCAN_MARGIN_S:
                    continue
                existing = self.db.get_result(entry.id, det.chain, det.result_key)
                if existing is None:
                    if det.kind == LAUNCH:
                        status, labels = STATUS_LAUNCH, [LABEL_LAUNCH]
                    elif det.has_liquidity:
                        status, labels = STATUS_LIQ, [LABEL_PAIR, LABEL_LIQ]
                    else:
                        status, labels = STATUS_PAIR, [LABEL_PAIR]
                    result = self.db.insert_result(entry.id, det, status)
                    log.info("[%s] %s %s %s (%s)", entry.name, labels, det.symbol, det.chain, det.dex)
                    self._queue_notification(entry, result, labels, det.market_cap_note)
                elif existing.status == STATUS_PAIR and det.has_liquidity:
                    self.db.mark_liquidity(existing.id, det.liquidity_usd, det.market_cap)
                    result = self.db.get_result_by_id(existing.id)
                    self._queue_notification(entry, result, [LABEL_LIQ], None)
                elif det.liquidity_usd is not None or det.market_cap is not None:
                    self.db.update_metrics(existing.id, det.liquidity_usd, det.market_cap)

    def _on_liquidity(self, det: Detection) -> None:
        key = det.pair_address or f"token:{det.token_address}"
        for res in self.db.find_results(det.chain, key):
            entry = self._entries.get(res.entry_id)
            if entry is None or res.status != STATUS_PAIR:
                continue
            self.db.mark_liquidity(res.id, det.liquidity_usd, det.market_cap)
            log.info("[%s] liquidité ajoutée %s %s", entry.name, res.symbol, res.chain)
            self._queue_notification(entry, self.db.get_result_by_id(res.id), [LABEL_LIQ], None)

    def _queue_notification(self, entry: Entry, result: Result, labels: list[str], note: str | None) -> None:
        self._notify_queue.put_nowait((entry, result, labels, note))

    async def _notify_worker(self) -> None:
        while True:
            entry, result, labels, note = await self._notify_queue.get()
            if self.notifier is None:
                continue
            try:
                message_id = await self.notifier.send(entry, result, labels, note)
            except Exception:  # noqa: BLE001
                log.exception("envoi de notification")
                continue
            if message_id:
                self.db.set_notify_message(result.id, message_id)
                self._last_notif[result.id] = (message_id, labels, note)
            if result.has_liquidity and result.market_cap is None:
                self._spawn(self._followup(result.id))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _followup(self, result_id: int) -> None:
        """Fill liquidity / market cap from DexScreener and update the notification."""
        for delay in FOLLOWUP_DELAYS_S:
            await asyncio.sleep(delay)
            res = self.db.get_result_by_id(result_id)
            if res is None:
                return
            pair = await self._lookup_pair(res)
            if pair is None:
                continue
            liq, mc = metrics(pair)
            if liq is None and mc is None:
                continue
            self.db.update_metrics(res.id, liq, mc)
            last = self._last_notif.get(res.id)
            entry = self.db.get_entry(res.entry_id)
            if last and entry and self.notifier is not None:
                try:
                    await self.notifier.edit(entry, self.db.get_result_by_id(res.id), last[1], last[2], last[0])
                except Exception:  # noqa: BLE001
                    log.debug("édition de notification impossible", exc_info=True)
            return

    async def _lookup_pair(self, res: Result) -> dict | None:
        chain = CHAINS.get(res.chain)
        if chain is None:
            return None
        pairs = await self.ds.tokens(chain.dexscreener, [res.token_address])
        pairs = [p for p in pairs
                 if norm_address(res.chain, (p.get("baseToken") or {}).get("address")) == res.token_address]
        return best_pair(pairs, res.pair_address, res.chain)

    def lookup_new_pairs_soon(self, chain: str, token: str) -> None:
        """After a launchpad migration, look for the new AMM pool on DexScreener."""
        self._spawn(self._lookup_new_pairs(chain, token))

    async def _lookup_new_pairs(self, chain_key: str, token: str) -> None:
        chain = CHAINS[chain_key]
        token = norm_address(chain_key, token)
        for delay in NEW_PAIR_LOOKUP_DELAYS_S:
            await asyncio.sleep(delay)
            found = False
            for p in await self.ds.tokens(chain.dexscreener, [token]):
                if norm_address(chain_key, (p.get("baseToken") or {}).get("address")) != token:
                    continue
                if not is_known_dex(p):
                    continue
                det = pair_to_detection(p)
                if det is not None:
                    det.kind, det.created_at = PAIR, time.time()
                    await self.on_detection(det)
                    found = True
            if found:
                return

    # ---- DexScreener ---------------------------------------------------
    async def handle_dexscreener_pair(self, pair: dict, ticker: str, chains: set[str]) -> None:
        base = pair.get("baseToken") or {}
        if normalize_ticker(base.get("symbol")) != ticker:
            return
        det = pair_to_detection(pair)
        if det is None or det.chain not in chains:
            return
        if det.kind == PAIR and not is_known_dex(pair) and self.db.has_launch(det.chain, det.token_address):
            det.kind, det.pool_kind = LAUNCH, "launchpad"
        await self.on_detection(det)

    async def _dexscreener_loop(self) -> None:
        while True:
            started = time.monotonic()
            for ticker, chains in self.ticker_index().items():
                try:
                    pairs = await self.ds.search(ticker)
                    for pair in pairs:
                        await self.handle_dexscreener_pair(pair, ticker, chains)
                except Exception:  # noqa: BLE001
                    log.exception("dexscreener: recherche %s", ticker)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self.settings.dexscreener_interval - elapsed))

    async def check_pending_on_dexscreener(self) -> None:
        pending = self.db.pending_results(self.settings.pending_liquidity_max_age_h * 3600)
        by_chain: dict[str, set[str]] = {}
        for res in pending:
            if res.pair_address:
                by_chain.setdefault(res.chain, set()).add(res.pair_address)
        for chain_key, addresses in by_chain.items():
            chain = CHAINS.get(chain_key)
            if chain is None:
                continue
            for pair in await self.ds.pairs(chain.dexscreener, sorted(addresses)):
                liq, mc = metrics(pair)
                if liq and liq > 0:
                    await self.on_detection(Detection(
                        chain=chain_key, kind=LIQUIDITY,
                        token_address=(pair.get("baseToken") or {}).get("address") or "",
                        pair_address=pair.get("pairAddress"), liquidity_usd=liq, market_cap=mc,
                        source="dexscreener",
                    ))

    async def refresh_results(self, results: list[Result]) -> None:
        """Update liquidity / market cap of the given results (Refresh button, enricher)."""
        by_chain: dict[str, list[Result]] = {}
        for res in results:
            by_chain.setdefault(res.chain, []).append(res)
        for chain_key, items in by_chain.items():
            chain = CHAINS.get(chain_key)
            if chain is None:
                continue
            tokens = sorted({r.token_address for r in items})
            pairs = await self.ds.tokens(chain.dexscreener, tokens)
            by_token: dict[str, list[dict]] = {}
            for p in pairs:
                addr = norm_address(chain_key, (p.get("baseToken") or {}).get("address"))
                by_token.setdefault(addr, []).append(p)
            for res in items:
                candidates = by_token.get(res.token_address, [])
                pair = best_pair(candidates, res.pair_address, chain_key)
                if pair is None:
                    continue
                liq, mc = metrics(pair)
                same_pair = norm_address(chain_key, pair.get("pairAddress")) == res.pair_address
                if res.status == STATUS_PAIR:
                    if same_pair and liq and liq > 0:
                        await self.on_detection(Detection(
                            chain=chain_key, kind=LIQUIDITY, token_address=res.token_address,
                            pair_address=res.pair_address, liquidity_usd=liq, market_cap=mc,
                            source="dexscreener",
                        ))
                    continue
                self.db.update_metrics(res.id, liq, mc)

    async def refresh_entry(self, entry_id: int) -> None:
        await self.refresh_results(self.db.results_for_entry(entry_id))

    # ---- background loops ----------------------------------------------
    async def _every(self, interval: float, fn: Callable[[], Coroutine[Any, Any, Any]], what: str) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await fn()
            except Exception:  # noqa: BLE001
                log.exception("%s", what)

    async def _enrich(self) -> None:
        await self.refresh_results([
            r for r in self.db.recent_liquid_results(ENRICH_MAX_AGE_S) if r.entry_id in self._entries
        ])

    def _desired_sources(self) -> dict[str, Callable[[], Coroutine[Any, Any, None]]]:
        desired: dict[str, Callable[[], Coroutine[Any, Any, None]]] = {}
        chains = self.desired_chains()
        for key in chains:
            chain = CHAINS.get(key)
            rpc = self.settings.evm_rpc.get(key)
            if chain is None or not chain.is_evm or not chain.has_onchain_config or rpc is None or not rpc.usable:
                continue
            desired[f"evm:{key}"] = (
                lambda chain=chain, rpc=rpc: EvmWatcher(
                    chain, rpc, self, self.settings, self.http, ws_connect=self.ws_connect).run()
            )
        if "solana" in chains:
            if self.settings.pumpportal_enabled:
                desired["solana:pumpportal"] = lambda: PumpPortalWatcher(self, connect=self.ws_connect).run()
            if self.settings.solana_ws_url and self.settings.solana_http_url:
                desired["solana:launchpads"] = lambda: SolanaLaunchpadWatcher(
                    self.settings.solana_ws_url, self.settings.solana_http_url, self, self.http,
                    include_pumpfun=not self.settings.pumpportal_enabled, connect=self.ws_connect,
                ).run()
        return desired

    def sync_sources(self) -> None:
        desired = self._desired_sources()
        for name, task in list(self._sources.items()):
            if name not in desired or task.done():
                if task.done() and not task.cancelled() and task.exception() is not None:
                    log.error("source %s arrêtée: %r — redémarrage", name, task.exception())
                task.cancel()
                del self._sources[name]
        for name, factory in desired.items():
            if name not in self._sources:
                self._sources[name] = asyncio.create_task(factory(), name=name)

    async def _sources_loop(self) -> None:
        while True:
            self.sync_sources()
            self._sync_now.clear()
            try:
                await asyncio.wait_for(self._sync_now.wait(), timeout=SYNC_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        loops = [
            self._notify_worker(),
            self._sources_loop(),
            self._dexscreener_loop(),
            self._every(DS_LIQUIDITY_INTERVAL_S, self.check_pending_on_dexscreener, "dexscreener: liquidité"),
            self._every(ENRICH_INTERVAL_S, self._enrich, "dexscreener: market caps"),
        ]
        tasks = [asyncio.create_task(c) for c in loops]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks + list(self._sources.values()) + list(self._background):
                t.cancel()
