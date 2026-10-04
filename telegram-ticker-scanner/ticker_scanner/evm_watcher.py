"""Real-time EVM watcher.

Per chain it follows three log streams:

* ``mints``  – every ERC-20 ``Transfer(from=0x0)``. It catches every new token,
  whatever the launchpad (Pons, LONG, o1, PAIR, Flap, four.meme, …) or a direct
  deployment, and the very first liquidity of any Uniswap-v2-style pair (the
  MINIMUM_LIQUIDITY burn), even for a pair created long before (stealth launch)
  or by an unknown factory.
* ``pairs``  – PairCreated / PoolCreated / Initialize on the known DEX factories.
* ``v4liq``  – ModifyLiquidity on Uniswap v4 for pools still waiting for
  liquidity (v2/v3-style pools are polled: reserves / pool balance).

Logs come from ``eth_subscribe`` when a websocket URL is configured, else from
``eth_getLogs`` polling. Pair events are processed before mint events and the
RPC calls needed once a ticker matches run in parallel.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import httpx

from . import evm_abi as abi
from .chains import Chain
from .config import EvmRpc, Settings
from .health import SourceStats
from .models import LAUNCH, LIQUIDITY, PAIR, STATUS_PAIR, Detection, valid_address
from .rpc import NODE, REVERT, HttpRpc, RpcError, WsSubscriptions, cancel_requested

if TYPE_CHECKING:
    from .scanner import Scanner

log = logging.getLogger(__name__)

BACKFILL_WINDOW_S = 600        # how far back we catch up after a disconnection
MAX_BACKFILL_BLOCKS = 20_000
RESERVES_POOL_KINDS = ("v2", "solidly")
BALANCE_POOL_KINDS = ("v3", "slipstream")
WS_STALL_TIMEOUT_S = 180
# Priority of each stream in the work queue (lower = first).
PRIORITY = {"pairs": 0, "v4liq": 0, "v3mints": 1, "mints": 1}
V4_SLOT0_PRICE_MASK = (1 << 160) - 1
V4_LIQUIDITY_MASK = (1 << 128) - 1
# How often a pool still waiting for liquidity is checked, by age.
PENDING_SCHEDULE = ((600, 6.0), (3600, 30.0), (float("inf"), 120.0))
RANGE_GROWTH_STREAK = 50       # successful eth_getLogs before trying a larger range again


class LRU(OrderedDict):
    def __init__(self, maxsize: int):
        super().__init__()
        self.maxsize = maxsize

    def put(self, key, value) -> None:
        self[key] = value
        self.move_to_end(key)
        if len(self) > self.maxsize:
            self.popitem(last=False)


class BoundedSet:
    """Membership set that forgets its oldest half when full (O(1), no per-item bookkeeping)."""

    def __init__(self, maxsize: int):
        self.half = max(1, maxsize // 2)
        self._new: set = set()
        self._old: set = set()

    def __contains__(self, key) -> bool:
        return key in self._new or key in self._old

    def __len__(self) -> int:
        return len(self._new) + len(self._old)

    def add(self, key) -> None:
        self._new.add(key)
        if len(self._new) >= self.half:
            self._old, self._new = self._new, set()

    def discard(self, key) -> None:
        self._new.discard(key)
        self._old.discard(key)


def _hex_int(value: Any) -> int:
    if not isinstance(value, str) or value in ("", "0x"):
        return 0
    try:
        return int(value, 16)
    except ValueError:
        return 0


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


_RANGE_HINT = re.compile(r"(?:up to an?|limited to an?|maximum(?: of)?|max(?:imum)? range(?: of)?)\s*([\d,]+)\s*(?:-\s*)?block",
                         re.IGNORECASE)


def range_hint(message: str) -> int | None:
    """Block range limit announced in a provider's eth_getLogs error, if any."""
    m = _RANGE_HINT.search(message)
    if not m:
        return None
    try:
        value = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if value >= 1 else None


def pending_interval(age_s: float) -> float:
    for max_age, interval in PENDING_SCHEDULE:
        if age_s < max_age:
            return interval
    return PENDING_SCHEDULE[-1][1]


class EvmWatcher:
    def __init__(self, chain: Chain, rpc_cfg: EvmRpc, scanner: "Scanner", settings: Settings,
                 http_client: httpx.AsyncClient, ws_connect=None, workers: int = 4,
                 stats: SourceStats | None = None):
        self.chain = chain
        self.scanner = scanner
        self.settings = settings
        self.rpc = HttpRpc(rpc_cfg.http_url, http_client, name=f"{chain.key}-rpc")
        self.ws_url = rpc_cfg.ws_url
        self.ws_connect = ws_connect
        self.n_workers = workers
        self.stats = stats or SourceStats(f"evm:{chain.key}")
        self.stats.mode = "websocket" if self.ws_url else "polling HTTP"
        self.symbols: LRU = LRU(50_000)                 # address -> symbol ("" = not a token)
        self.seen_mints = BoundedSet(200_000)           # token contracts already processed
        self.seen_first_liq = BoundedSet(50_000)        # v2 pairs whose first liquidity was handled
        self.seen_v3_pools = BoundedSet(100_000)        # v3 pools already inspected
        self.seen_v4_pools = BoundedSet(100_000)        # v4 pool ids already inspected
        self.v4_keys: LRU = LRU(50_000)                 # v4 pool id -> (currency0, currency1, hooks)
        self._v4_layout_warned = False
        self._receipts: LRU = LRU(256)
        self._inflight: dict[str, asyncio.Future] = {}
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=50_000)
        self._seq = itertools.count()
        self._dropped = 0
        self.last_block: int | None = None
        self.filters: dict[str, dict | None] = {}
        self._ws: WsSubscriptions | None = None
        self._next_check: dict[int, float] = {}
        self.max_range = max(1, settings.evm_getlogs_max_range)
        self._range_cap = self.max_range  # hard limit announced by the provider, if any
        self._range_streak = 0

        self.dex_by_address: dict[str, tuple[str, str]] = {}
        for kind, table in (
            ("v2", chain.v2_factories), ("v3", chain.v3_factories), ("v4", chain.v4_pool_managers),
            ("solidly", chain.solidly_factories), ("slipstream", chain.slipstream_factories),
        ):
            for address, name in table.items():
                self.dex_by_address[address] = (kind, name)

    @property
    def backfill_blocks(self) -> int:
        return min(MAX_BACKFILL_BLOCKS, max(100, int(BACKFILL_WINDOW_S / max(self.chain.block_time, 0.05))))

    # ---- filters -------------------------------------------------------
    def base_filters(self) -> dict[str, dict]:
        topics0 = []
        if self.chain.v2_factories:
            topics0.append(abi.V2_PAIR_CREATED)
        if self.chain.v3_factories:
            topics0.append(abi.V3_POOL_CREATED)
        if self.chain.v4_pool_managers:
            topics0.append(abi.V4_INITIALIZE)
        if self.chain.solidly_factories:
            topics0.append(abi.SOLIDLY_POOL_CREATED)
        if self.chain.slipstream_factories:
            topics0.append(abi.SLIPSTREAM_POOL_CREATED)
        filters = {
            "mints": {"topics": [abi.TRANSFER, abi.ZERO_TOPIC]},
            # every liquidity add on any v3-style pool (Uniswap, PancakeSwap, Aerodrome CL, forks)
            "v3mints": {"topics": [abi.V3_MINT]},
        }
        if topics0:
            filters["pairs"] = {"address": list(self.dex_by_address), "topics": [topics0]}
        if self.chain.v4_pool_managers:
            # every liquidity change on the Uniswap v4 singleton
            filters["v4liq"] = {"address": list(self.chain.v4_pool_managers), "topics": [abi.V4_MODIFY_LIQUIDITY]}
        return filters

    def set_filter(self, name: str, flt: dict | None) -> None:
        if self.filters.get(name) == flt:
            return
        self.filters[name] = flt
        if self._ws is not None:
            self._ws.set(name, ["logs", flt] if flt else None)

    # ---- main loop -----------------------------------------------------
    async def run(self) -> None:
        for name, flt in self.base_filters().items():
            self.set_filter(name, flt)
        tasks = [asyncio.create_task(self._worker()) for _ in range(self.n_workers)]
        tasks.append(asyncio.create_task(self._liquidity_loop()))
        log.info("[%s] watcher on-chain démarré (%s)", self.chain.key, self.stats.mode)
        try:
            if self.ws_url:
                self._ws = WsSubscriptions(
                    self.ws_url, subscribe_method="eth_subscribe", unsubscribe_method="eth_unsubscribe",
                    on_message=self._on_ws_message, on_connect=self._backfill,
                    connect=self.ws_connect, name=self.chain.key, stall_timeout=WS_STALL_TIMEOUT_S,
                    on_state=self._on_ws_state,
                )
                for name, flt in self.filters.items():
                    self._ws.set(name, ["logs", flt] if flt else None)
                await self._ws.run()
            else:
                await self._poll_loop()
        finally:
            self.stats.connected = False
            for t in tasks:
                t.cancel()
            log.info("[%s] watcher on-chain arrêté", self.chain.key)

    def _on_ws_state(self, connected: bool) -> None:
        self.stats.connected = connected
        if not connected:
            self.stats.error("websocket déconnecté")

    def _on_ws_message(self, name: str, lg: dict) -> None:
        self._enqueue(name, lg, track=True)

    def _enqueue(self, name: str, lg: Any, track: bool) -> None:
        if not isinstance(lg, dict) or lg.get("removed"):
            return
        self.stats.event()
        if track and lg.get("blockNumber"):
            block = _hex_int(lg["blockNumber"])
            if self.last_block is None or block > self.last_block:
                self.last_block = block
        try:
            self.queue.put_nowait((PRIORITY.get(name, 1), next(self._seq), name, lg))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped in (1, 10, 100) or self._dropped % 1000 == 0:
                log.warning("[%s] file pleine, %d logs ignorés", self.chain.key, self._dropped)

    async def _get_logs(self, flt: dict, start: int, end: int) -> list[dict]:
        """eth_getLogs that splits a range the node refuses and learns the provider's limit."""
        try:
            logs = await self.rpc.call("eth_getLogs", [{**flt, "fromBlock": hex(start), "toBlock": hex(end)}])
        except RpcError as exc:
            if exc.kind != NODE:
                raise  # network / rate limit: retry the same range later
            if start >= end:
                log.warning("[%s] eth_getLogs refusé pour le bloc %d, ignoré: %s", self.chain.key, start, exc)
                self.stats.error(str(exc))
                return []
            self._range_streak = 0
            hint = range_hint(str(exc))
            if hint and hint < end - start + 1:
                # The provider told us its limit ("up to a 10 block range"): use it exactly, for good.
                self._range_cap = min(self._range_cap, hint)
                self.max_range = min(self.max_range, hint)
                out: list[dict] = []
                for a in range(start, end + 1, hint):
                    out += await self._get_logs(flt, a, min(end, a + hint - 1))
                return out
            self.max_range = max(1, min(self.max_range, (end - start + 1) // 2))
            mid = (start + end) // 2
            return await self._get_logs(flt, start, mid) + await self._get_logs(flt, mid + 1, end)
        self._range_streak += 1
        if self._range_streak >= RANGE_GROWTH_STREAK and self.max_range < self._range_cap:
            self.max_range = min(self._range_cap, self.max_range * 2)  # limit was guessed: probe upwards
            self._range_streak = 0
        return logs if isinstance(logs, list) else []

    async def _fetch_range(self, start: int, end: int) -> None:
        a = start
        while a <= end:
            b = min(end, a + self.max_range - 1)
            for name, flt in list(self.filters.items()):
                if not flt:
                    continue
                for lg in await self._get_logs(flt, a, b):
                    self._enqueue(name, lg, track=False)
            a = b + 1

    def _range_start(self, since: int, latest: int) -> int:
        start = since + 1
        if latest - start > self.backfill_blocks:
            skipped = latest - self.backfill_blocks - start
            log.warning("[%s] retard de %d blocs, les plus anciens sont ignorés", self.chain.key, skipped)
            start = latest - self.backfill_blocks
        return start

    async def _backfill(self) -> None:
        """After a websocket reconnect, fetch the logs emitted while we were away."""
        since = self.last_block  # snapshot before live logs move it forward
        if since is None:
            return
        try:
            latest = _hex_int(await self.rpc.call("eth_blockNumber"))
            if latest > since:
                await self._fetch_range(self._range_start(since, latest), latest)
        except RpcError as exc:
            log.warning("[%s] rattrapage impossible: %s", self.chain.key, exc)
            self.stats.error(f"rattrapage: {exc}")

    async def _poll_loop(self) -> None:
        while True:
            try:
                latest = _hex_int(await self.rpc.call("eth_blockNumber"))
                self.stats.connected = True
                if self.last_block is None:
                    self.last_block = latest
                elif latest > self.last_block:
                    await self._fetch_range(self._range_start(self.last_block, latest), latest)
                    self.last_block = latest
            except RpcError as exc:
                if cancel_requested():
                    raise asyncio.CancelledError from exc
                self.stats.connected = False
                self.stats.error(str(exc))
                log.warning("[%s] polling: %s", self.chain.key, exc)
            await asyncio.sleep(self.settings.evm_poll_interval)

    async def _worker(self) -> None:
        handlers = {"pairs": self.handle_pair, "mints": self.handle_mint, "v4liq": self.handle_v4_liquidity,
                    "v3mints": self.handle_v3_mint}
        while True:
            _, _, name, lg = await self.queue.get()
            handler = handlers.get(name)
            if handler is None:
                continue
            try:
                await handler(lg)
            except RpcError as exc:
                log.debug("[%s] rpc: %s", self.chain.key, exc)
            except Exception:  # noqa: BLE001
                log.exception("[%s] erreur sur un log %s", self.chain.key, name)

    # ---- chain reads ---------------------------------------------------
    async def _call(self, to: str, data: str) -> str | None:
        return await self.rpc.call("eth_call", [{"to": to, "data": data}, "latest"])

    async def token_symbol(self, address: str) -> str:
        address = address.lower()
        if address in self.chain.quote_tokens:
            return self.chain.quote_tokens[address]
        if address == abi.ZERO_ADDRESS:
            return self.chain.native_symbol
        if address in self.symbols:
            return self.symbols[address]
        if address in self._inflight:
            return await self._inflight[address]
        fut = asyncio.get_running_loop().create_future()
        self._inflight[address] = fut
        symbol = ""
        try:
            symbol = abi.decode_string_result(await self._call(address, abi.SEL_SYMBOL)) or ""
            self.symbols.put(address, symbol)
        except RpcError as exc:
            if exc.kind == REVERT:  # symbol() reverted: not an ERC-20
                self.symbols.put(address, "")
        finally:
            # Always release the other workers waiting for this address.
            self._inflight.pop(address, None)
            if not fut.done():
                fut.set_result(symbol)
        return symbol

    async def token_name(self, address: str) -> str:
        try:
            return abi.decode_string_result(await self._call(address, abi.SEL_NAME)) or ""
        except RpcError:
            return ""

    async def _address_call(self, to: str, data: str) -> str | None:
        try:
            address = abi.word_address(await self._call(to, data), 0)
        except RpcError:
            return None
        return address if valid_address("evm", address) else None

    async def receipt(self, tx_hash: str | None) -> dict:
        """Transaction receipt, cached: several handlers need the same one."""
        if not tx_hash:
            return {}
        if tx_hash in self._receipts:
            return self._receipts[tx_hash]
        try:
            rec = await self.rpc.call("eth_getTransactionReceipt", [tx_hash])
        except RpcError:
            return {}
        if isinstance(rec, dict):
            self._receipts.put(tx_hash, rec)
            return rec
        return {}

    async def _is_new_contract(self, address: str, block_hex: str | None) -> bool:
        """False when the contract already existed before this block (old token minting)."""
        block = _hex_int(block_hex)
        if block <= 0:
            return True
        try:
            code = await self.rpc.call("eth_getCode", [address, hex(block - 1)])
        except RpcError:
            return True
        return not code or code == "0x"

    async def _launchpad_label(self, tx_hash: str | None, minted_to: str | None) -> str:
        if minted_to and minted_to in self.chain.launchpads:
            return self.chain.launchpads[minted_to]
        receipt = await self.receipt(tx_hash)
        if not receipt:
            return "Nouveau token"
        to = (receipt.get("to") or "").lower()
        if to in self.chain.launchpads:
            return self.chain.launchpads[to]
        for lg in receipt.get("logs") or []:
            emitter = (lg.get("address") or "").lower()
            if emitter in self.chain.launchpads:
                return self.chain.launchpads[emitter]
            if emitter in self.dex_by_address:
                return f"Nouveau token · {self.dex_by_address[emitter][1]}"
        if valid_address("evm", to):
            return f"Nouveau token · via {short(to)}"
        return "Nouveau token · déploiement direct"

    # ---- handlers ------------------------------------------------------
    async def handle_mint(self, lg: dict) -> None:
        topics = lg.get("topics") or []
        if len(topics) != 3:  # ERC-721 mints have 4 topics
            return
        token = (lg.get("address") or "").lower()
        if not valid_address("evm", token) or token in self.chain.quote_tokens:
            return
        minted_to = abi.topic_address(topics[2])
        if minted_to == abi.ZERO_ADDRESS and abi.word_uint(lg.get("data"), 0) == abi.MINIMUM_LIQUIDITY:
            if token not in self.seen_first_liq:
                self.seen_first_liq.add(token)
                await self.handle_first_liquidity(token, lg)
            return
        if token in self.seen_mints:
            return
        self.seen_mints.add(token)
        symbol = await self.token_symbol(token)
        if token not in self.symbols:  # RPC failure: retry on its next mint
            self.seen_mints.discard(token)
            return
        if not symbol or not self.scanner.is_watched(self.chain.key, symbol):
            return
        tx = lg.get("transactionHash")
        is_new, name, label = await asyncio.gather(
            self._is_new_contract(token, lg.get("blockNumber")),
            self.token_name(token),
            self._launchpad_label(tx, minted_to),
        )
        if not is_new:
            return
        self.stats.detections += 1
        await self.scanner.on_detection(Detection(
            chain=self.chain.key, kind=LAUNCH, token_address=token, symbol=symbol, name=name,
            pool_kind="launchpad", dex=label, source="onchain", tx_hash=tx,
        ))

    # ---- stealth launches: liquidity on pools created before we watched ----
    async def _watched_side(self, token0: str, token1: str) -> tuple[str, str, str] | None:
        """(token, other, symbol) when one side of a pool is a watched ticker."""
        sides = [(t, o) for t, o in ((token0, token1), (token1, token0))
                 if t not in self.chain.quote_tokens and t != abi.ZERO_ADDRESS]
        symbols = await asyncio.gather(*(self.token_symbol(t) for t, _ in sides))
        for (token, other), symbol in zip(sides, symbols):
            if symbol and self.scanner.is_watched(self.chain.key, symbol):
                return token, other, symbol
        return None

    async def _known_pool(self, key: str, lg: dict) -> bool:
        """A pool we already report: its liquidity event is just a 🟢 for a pending pair."""
        results = self.scanner.db.find_results(self.chain.key, key)
        if not results:
            return False
        pending = next((r for r in results if r.status == STATUS_PAIR), None)
        if pending is not None:
            await self.scanner.on_detection(Detection(
                chain=self.chain.key, kind=LIQUIDITY, token_address=pending.token_address, pair_address=key,
                source="onchain", tx_hash=lg.get("transactionHash")))
        return True

    async def _pool_tokens(self, pool: str) -> tuple[str, str] | None:
        """token0/token1 of a v2/v3 pool; None if it is not a pool; RpcError if the node failed."""
        results = await asyncio.gather(self._call(pool, abi.SEL_TOKEN0), self._call(pool, abi.SEL_TOKEN1),
                                       return_exceptions=True)
        for res in results:
            if isinstance(res, RpcError):
                if res.kind == REVERT:
                    return None
                raise res
            if isinstance(res, BaseException):
                raise res
        token0, token1 = abi.word_address(results[0], 0), abi.word_address(results[1], 0)
        if not (valid_address("evm", token0) and valid_address("evm", token1)) or token0 == token1:
            return None
        return token0, token1

    async def _report_stealth(self, lg: dict, *, pool: str, token: str, other: str, symbol: str,
                              pool_kind: str, dex: str, created_now: bool, kind: str = PAIR) -> None:
        name, quote_symbol = await asyncio.gather(self.token_name(token), self.token_symbol(other))
        self.stats.detections += 1
        await self.scanner.on_detection(Detection(
            chain=self.chain.key, kind=kind, token_address=token, symbol=symbol, name=name,
            pair_address=pool, pool_kind=pool_kind, dex=dex, quote_symbol=quote_symbol or None,
            quote_address=other, has_liquidity=True, pre_existing_pair=not created_now,
            source="onchain", tx_hash=lg.get("transactionHash"),
        ))

    async def handle_first_liquidity(self, pair: str, lg: dict) -> None:
        """First liquidity of a v2-style pair (MINIMUM_LIQUIDITY burn), any factory, any age."""
        if await self._known_pool(pair, lg):
            return
        try:
            tokens = await self._pool_tokens(pair)
        except RpcError:
            self.seen_first_liq.discard(pair)  # retry on the next event
            return
        if tokens is None:
            return
        match = await self._watched_side(*tokens)
        if match is None:
            return
        token, other, symbol = match
        factory, receipt = await asyncio.gather(self._address_call(pair, abi.SEL_FACTORY),
                                                self.receipt(lg.get("transactionHash")))
        created_now = any(
            (x.get("topics") or [None])[0] == abi.V2_PAIR_CREATED and abi.word_address(x.get("data"), 0) == pair
            for x in receipt.get("logs") or [])
        dex = self.dex_by_address.get(factory or "", ("v2", f"DEX v2 · {short(factory or pair)}"))[1]
        await self._report_stealth(lg, pool=pair, token=token, other=other, symbol=symbol, pool_kind="v2",
                                   dex=dex, created_now=created_now)

    async def handle_v3_mint(self, lg: dict) -> None:
        """Liquidity added to a v3-style pool: report its first liquidity, whatever its age or factory."""
        topics = lg.get("topics") or []
        if len(topics) != 4 or topics[0] != abi.V3_MINT:
            return
        pool = (lg.get("address") or "").lower()
        if not valid_address("evm", pool):
            return
        if await self._known_pool(pool, lg):
            return
        if pool in self.seen_v3_pools:
            return
        self.seen_v3_pools.add(pool)
        try:
            tokens = await self._pool_tokens(pool)
        except RpcError:
            self.seen_v3_pools.discard(pool)
            return
        if tokens is None:
            return
        match = await self._watched_side(*tokens)
        if match is None:
            return
        token, other, symbol = match
        block = _hex_int(lg.get("blockNumber"))
        if not await self._v3_was_empty(pool, tokens, block):
            return  # an established pool, not a launch
        factory, receipt = await asyncio.gather(self._address_call(pool, abi.SEL_FACTORY),
                                                self.receipt(lg.get("transactionHash")))
        created_now = False
        for x in receipt.get("logs") or []:
            t0 = (x.get("topics") or [None])[0]
            if (t0 == abi.V3_POOL_CREATED and abi.word_address(x.get("data"), 1) == pool) or \
                    (t0 == abi.SLIPSTREAM_POOL_CREATED and abi.word_address(x.get("data"), 0) == pool):
                created_now = True
        kind, dex = self.dex_by_address.get(factory or "", ("v3", f"DEX v3 · {short(factory or pool)}"))
        await self._report_stealth(lg, pool=pool, token=token, other=other, symbol=symbol,
                                   pool_kind=kind if kind in BALANCE_POOL_KINDS else "v3", dex=dex,
                                   created_now=created_now)

    async def _v3_was_empty(self, pool: str, tokens: tuple[str, str], block: int) -> bool:
        """The pool held none of its two tokens before this block: this is its first liquidity."""
        if block <= 0:
            return False
        before = hex(block - 1)
        try:
            balances = await asyncio.gather(*(
                self.rpc.call("eth_call", [{"to": t, "data": abi.encode_address_call(abi.SEL_BALANCE_OF, pool)},
                                           before]) for t in tokens))
        except RpcError:
            return False  # state not available: let DexScreener confirm instead of guessing
        return all(_hex_int(b) == 0 for b in balances)

    async def handle_v4_liquidity(self, lg: dict) -> None:
        """Liquidity added on the v4 PoolManager: pending pools, and first liquidity of any pool."""
        topics = lg.get("topics") or []
        if len(topics) < 2 or topics[0] != abi.V4_MODIFY_LIQUIDITY:
            return
        if abi.word_int(lg.get("data"), 2) <= 0:
            return
        manager = (lg.get("address") or "").lower()
        pool_id = topics[1].lower() if isinstance(topics[1], str) else ""
        if manager not in self.chain.v4_pool_managers or not valid_address("evm", pool_id, pool=True):
            return
        if await self._known_pool(pool_id, lg):
            return
        if pool_id in self.seen_v4_pools:
            return
        self.seen_v4_pools.add(pool_id)
        sender = abi.topic_address(topics[2]) if len(topics) > 2 else None
        key = self.v4_keys.get(pool_id) or await self._v4_pool_key(manager, pool_id, sender, lg)
        if key is None:
            return
        currency0, currency1, hooks = key
        match = await self._watched_side(currency0, currency1)
        if match is None:
            return
        token, other, symbol = match
        if not await self._v4_was_empty(manager, pool_id, _hex_int(lg.get("blockNumber"))):
            return
        receipt = await self.receipt(lg.get("transactionHash"))
        created_now = any((x.get("topics") or [None])[0] == abi.V4_INITIALIZE
                          and len(x.get("topics") or []) > 1 and str(x["topics"][1]).lower() == pool_id
                          for x in receipt.get("logs") or [])
        launchpad = self.chain.v4_hooks.get(hooks or "")
        dex = self.chain.v4_pool_managers[manager]
        await self._report_stealth(lg, pool=pool_id, token=token, other=other, symbol=symbol, pool_kind="v4",
                                   dex=f"{launchpad} · {dex}" if launchpad else dex, created_now=created_now,
                                   kind=LAUNCH if launchpad else PAIR)

    async def _v4_pool_key(self, manager: str, pool_id: str, sender: str | None,
                           lg: dict) -> tuple[str, str, str] | None:
        """Currencies + hooks of a v4 pool, verified: keccak(abi.encode(PoolKey)) must equal the pool id."""
        candidates = [a for a in (self.chain.v4_position_manager, sender) if a and valid_address("evm", a)]
        for contract in dict.fromkeys(candidates):
            try:
                data = await self._call(contract, abi.encode_pool_keys(pool_id))
            except RpcError:
                continue
            raw = abi._hex_bytes(data)[:160]  # noqa: SLF001
            if len(raw) == 160 and "0x" + abi.keccak256(raw).hex() == pool_id:
                key = (abi.word_address(data, 0), abi.word_address(data, 1), abi.word_address(data, 4))
                self.v4_keys.put(pool_id, key)
                return key
        # Pools created by custom lockers: their Initialize, if recent enough to fetch cheaply.
        block = _hex_int(lg.get("blockNumber"))
        if block and self.max_range >= 1000:
            try:
                logs = await self._get_logs({"address": manager, "topics": [abi.V4_INITIALIZE, pool_id]},
                                            max(0, block - self.backfill_blocks), block)
            except RpcError:
                logs = []
            for init in logs:
                decoded = self._decode_pair(init)
                if decoded is not None:
                    key = (decoded[2], decoded[3], decoded[5])
                    self.v4_keys.put(pool_id, key)
                    return key
        return None

    async def _v4_was_empty(self, manager: str, pool_id: str, block: int) -> bool:
        """No active liquidity (or not even initialized) right before this block, read from storage."""
        if block <= 0:
            return False
        slot = abi.v4_state_slot(pool_id)
        before = hex(block - 1)
        try:
            now_slot0, prev_slot0, prev_liquidity = await asyncio.gather(
                self.rpc.call("eth_call", [{"to": manager, "data": abi.encode_extsload(slot)}, "latest"]),
                self.rpc.call("eth_call", [{"to": manager, "data": abi.encode_extsload(slot)}, before]),
                self.rpc.call("eth_call", [{"to": manager, "data": abi.encode_extsload(
                    slot + abi.V4_LIQUIDITY_OFFSET)}, before]),
            )
        except RpcError:
            return False
        if abi.word_uint(now_slot0, 0) & V4_SLOT0_PRICE_MASK == 0:
            # A pool receiving liquidity is initialized: if we read nothing, the storage layout
            # is not the one we expect (fork/upgrade). Never guess.
            if not self._v4_layout_warned:
                self._v4_layout_warned = True
                log.warning("[%s] lecture du stockage v4 incohérente, détection furtive v4 désactivée",
                            self.chain.key)
            return False
        if abi.word_uint(prev_slot0, 0) & V4_SLOT0_PRICE_MASK == 0:
            return True  # initialized in this very block
        return abi.word_uint(prev_liquidity, 0) & V4_LIQUIDITY_MASK == 0

    def _decode_pair(self, lg: dict) -> tuple[str, str, str, str, str, str | None] | None:
        """-> (pool_kind, dex, token0, token1, pool_address_or_id, hooks)"""
        emitter = (lg.get("address") or "").lower()
        if emitter not in self.dex_by_address:
            return None
        kind, dex = self.dex_by_address[emitter]
        topics = lg.get("topics") or []
        if len(topics) < 3:
            return None
        t0 = topics[0]
        data = lg.get("data")
        if kind == "v2" and t0 == abi.V2_PAIR_CREATED:
            pool = abi.word_address(data, 0)
        elif kind == "v3" and t0 == abi.V3_POOL_CREATED:
            pool = abi.word_address(data, 1)
        elif kind in ("solidly", "slipstream") and t0 in (abi.SOLIDLY_POOL_CREATED, abi.SLIPSTREAM_POOL_CREATED):
            pool = abi.word_address(data, 0)
        elif kind == "v4" and t0 == abi.V4_INITIALIZE and len(topics) >= 4:
            hooks = abi.word_address(data, 2)
            return kind, dex, abi.topic_address(topics[2]), abi.topic_address(topics[3]), topics[1].lower(), hooks
        else:
            return None
        if not pool or pool == abi.ZERO_ADDRESS:
            return None
        return kind, dex, abi.topic_address(topics[1]), abi.topic_address(topics[2]), pool.lower(), None

    async def handle_pair(self, lg: dict) -> None:
        decoded = self._decode_pair(lg)
        if decoded is None:
            return
        kind, dex, token0, token1, pool, hooks = decoded
        if kind == "v4":
            self.v4_keys.put(pool, (token0, token1, hooks))
        candidates = [(t, o) for t, o in ((token0, token1), (token1, token0))
                      if t not in self.chain.quote_tokens and t != abi.ZERO_ADDRESS]
        symbols = await asyncio.gather(*(self.token_symbol(t) for t, _ in candidates))
        for (token, other), symbol in zip(candidates, symbols):
            if not symbol or not self.scanner.is_watched(self.chain.key, symbol):
                continue
            tx = lg.get("transactionHash")
            name, quote_symbol, has_liq = await asyncio.gather(
                self.token_name(token), self.token_symbol(other), self.liquidity_in_tx(tx, kind, pool))
            launchpad = self.chain.v4_hooks.get(hooks or "")
            self.stats.detections += 1
            await self.scanner.on_detection(Detection(
                chain=self.chain.key, kind=LAUNCH if launchpad else PAIR, token_address=token,
                symbol=symbol, name=name, pair_address=pool, pool_kind=kind,
                dex=f"{launchpad} · {dex}" if launchpad else dex,
                quote_symbol=quote_symbol or None, quote_address=other,
                has_liquidity=has_liq, source="onchain", tx_hash=tx,
            ))

    async def liquidity_in_tx(self, tx_hash: str | None, kind: str, pool: str) -> bool:
        """Was liquidity added in the very transaction that created the pool?"""
        receipt = await self.receipt(tx_hash)
        for lg in receipt.get("logs") or []:
            topics = lg.get("topics") or []
            if not topics:
                continue
            emitter = (lg.get("address") or "").lower()
            if kind == "v4":
                if (topics[0] == abi.V4_MODIFY_LIQUIDITY and len(topics) > 1
                        and topics[1].lower() == pool and abi.word_int(lg.get("data"), 2) > 0):
                    return True
            elif emitter == pool and topics[0] in (abi.V2_MINT, abi.V3_MINT):
                return True
        return False

    async def _has_liquidity(self, res) -> bool:
        if res.pool_kind in RESERVES_POOL_KINDS:
            # Real two-sided reserves: tokens merely sent to the pair do not count.
            data = await self._call(res.pair_address, abi.SEL_GET_RESERVES)
            return abi.word_uint(data, 0) > 0 and abi.word_uint(data, 1) > 0
        data = abi.encode_address_call(abi.SEL_BALANCE_OF, res.pair_address)
        return _hex_int(await self._call(res.token_address, data)) > 0

    async def check_pending_liquidity(self, now: float | None = None) -> None:
        now = now or time.time()
        max_age = self.settings.pending_liquidity_max_age_h * 3600
        pending = self.scanner.db.pending_results(max_age, chains=[self.chain.key])
        live_ids = {r.id for r in pending}
        for stale in [rid for rid in self._next_check if rid not in live_ids]:
            del self._next_check[stale]
        due, seen = [], set()
        for res in pending:
            if res.pool_kind not in RESERVES_POOL_KINDS + BALANCE_POOL_KINDS or not res.pair_address:
                continue
            if self._next_check.get(res.id, 0) > now:
                continue
            self._next_check[res.id] = now + pending_interval(now - res.found_at)
            if (res.token_address, res.pair_address) not in seen:
                seen.add((res.token_address, res.pair_address))
                due.append(res)

        async def check(res) -> None:
            try:
                liquid = await self._has_liquidity(res)
            except RpcError:
                return
            if liquid:
                await self.scanner.on_detection(Detection(
                    chain=self.chain.key, kind=LIQUIDITY, token_address=res.token_address,
                    pair_address=res.pair_address, source="onchain",
                ))

        for i in range(0, len(due), 8):  # bounded parallelism
            await asyncio.gather(*(check(r) for r in due[i:i + 8]))

    async def _liquidity_loop(self) -> None:
        while True:
            try:
                await self.check_pending_liquidity()
            except Exception:  # noqa: BLE001
                log.exception("[%s] vérification de liquidité", self.chain.key)
            await asyncio.sleep(self.settings.evm_liquidity_check_interval)
