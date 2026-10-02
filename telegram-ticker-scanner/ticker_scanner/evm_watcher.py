"""Real-time EVM watcher.

Per chain it follows three log streams:

* ``mints``  – every ERC-20 ``Transfer(from=0x0)``: catches every new token,
  whatever the launchpad (Pons, LONG, o1, PAIR, Flap, four.meme, …) or a
  direct deployment.
* ``pairs``  – PairCreated / PoolCreated / Initialize on the DEX factories.
* ``v4liq``  – ModifyLiquidity on Uniswap v4 for pools still waiting for
  liquidity (v2/v3-style pools are checked with ``balanceOf(pool)``).

Logs come from ``eth_subscribe`` when a websocket URL is configured, else from
``eth_getLogs`` polling.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING

import httpx

from . import evm_abi as abi
from .chains import Chain
from .config import EvmRpc, Settings
from .models import LAUNCH, LIQUIDITY, PAIR, Detection
from .rpc import HttpRpc, RpcError, WsSubscriptions

if TYPE_CHECKING:
    from .scanner import Scanner

log = logging.getLogger(__name__)

MAX_BACKFILL_BLOCKS = 600
BALANCE_POOL_KINDS = ("v2", "v3", "solidly", "slipstream")


class LRU(OrderedDict):
    def __init__(self, maxsize: int):
        super().__init__()
        self.maxsize = maxsize

    def put(self, key, value) -> None:
        self[key] = value
        self.move_to_end(key)
        if len(self) > self.maxsize:
            self.popitem(last=False)


def _hex_int(value: str | None) -> int:
    if not value or value == "0x":
        return 0
    try:
        return int(value, 16)
    except ValueError:
        return 0


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


class EvmWatcher:
    def __init__(self, chain: Chain, rpc_cfg: EvmRpc, scanner: "Scanner", settings: Settings,
                 http_client: httpx.AsyncClient, ws_connect=None, workers: int = 4):
        self.chain = chain
        self.scanner = scanner
        self.settings = settings
        self.rpc = HttpRpc(rpc_cfg.http_url, http_client, name=f"{chain.key}-rpc")
        self.ws_url = rpc_cfg.ws_url
        self.ws_connect = ws_connect
        self.n_workers = workers
        self.symbols: LRU = LRU(200_000)       # address -> symbol ("" = not a token)
        self.seen_mints: LRU = LRU(500_000)    # token addresses already processed
        self._inflight: dict[str, asyncio.Future] = {}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=50_000)
        self.last_block: int | None = None
        self.filters: dict[str, dict | None] = {}
        self._ws: WsSubscriptions | None = None

        self.dex_by_address: dict[str, tuple[str, str]] = {}
        for kind, table in (
            ("v2", chain.v2_factories), ("v3", chain.v3_factories), ("v4", chain.v4_pool_managers),
            ("solidly", chain.solidly_factories), ("slipstream", chain.slipstream_factories),
        ):
            for address, name in table.items():
                self.dex_by_address[address] = (kind, name)

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
        filters = {"mints": {"topics": [abi.TRANSFER, abi.ZERO_TOPIC]}}
        if topics0:
            filters["pairs"] = {"address": list(self.dex_by_address), "topics": [topics0]}
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
        mode = "websocket" if self.ws_url else "polling HTTP"
        log.info("[%s] watcher on-chain démarré (%s)", self.chain.key, mode)
        try:
            if self.ws_url:
                self._ws = WsSubscriptions(
                    self.ws_url, subscribe_method="eth_subscribe", unsubscribe_method="eth_unsubscribe",
                    on_message=self._on_ws_message, on_connect=self._backfill,
                    connect=self.ws_connect, name=self.chain.key,
                )
                for name, flt in self.filters.items():
                    self._ws.set(name, ["logs", flt] if flt else None)
                await self._ws.run()
            else:
                await self._poll_loop()
        finally:
            for t in tasks:
                t.cancel()
            log.info("[%s] watcher on-chain arrêté", self.chain.key)

    def _on_ws_message(self, name: str, lg: dict) -> None:
        self._enqueue(name, lg, track=True)

    def _enqueue(self, name: str, lg: dict, track: bool) -> None:
        if not isinstance(lg, dict) or lg.get("removed"):
            return
        if track and lg.get("blockNumber"):
            block = _hex_int(lg["blockNumber"])
            if self.last_block is None or block > self.last_block:
                self.last_block = block
        try:
            self.queue.put_nowait((name, lg))
        except asyncio.QueueFull:
            log.warning("[%s] file pleine, log ignoré", self.chain.key)

    async def _fetch_range(self, start: int, end: int) -> None:
        step = max(1, self.settings.evm_getlogs_max_range)
        a = start
        while a <= end:
            b = min(end, a + step - 1)
            for name, flt in list(self.filters.items()):
                if not flt:
                    continue
                logs = await self.rpc.call("eth_getLogs", [{**flt, "fromBlock": hex(a), "toBlock": hex(b)}])
                for lg in logs or []:
                    self._enqueue(name, lg, track=False)
            a = b + 1

    async def _backfill(self) -> None:
        """After a websocket reconnect, fetch the logs emitted while we were away."""
        if self.last_block is None:
            return
        try:
            latest = _hex_int(await self.rpc.call("eth_blockNumber"))
            start = max(self.last_block + 1, latest - MAX_BACKFILL_BLOCKS)
            if start <= latest:
                await self._fetch_range(start, latest)
        except RpcError as exc:
            log.warning("[%s] rattrapage impossible: %s", self.chain.key, exc)

    async def _poll_loop(self) -> None:
        while True:
            try:
                latest = _hex_int(await self.rpc.call("eth_blockNumber"))
                if self.last_block is None:
                    self.last_block = latest
                elif latest > self.last_block:
                    start = max(self.last_block + 1, latest - MAX_BACKFILL_BLOCKS)
                    await self._fetch_range(start, latest)
                    self.last_block = latest
            except RpcError as exc:
                log.warning("[%s] polling: %s", self.chain.key, exc)
            await asyncio.sleep(self.settings.evm_poll_interval)

    async def _worker(self) -> None:
        handlers = {"pairs": self.handle_pair, "mints": self.handle_mint, "v4liq": self.handle_v4_liquidity}
        while True:
            name, lg = await self.queue.get()
            handler = handlers.get(name)
            if handler is None:
                continue
            try:
                await handler(lg)
            except RpcError as exc:
                log.debug("[%s] rpc: %s", self.chain.key, exc)
            except Exception:  # noqa: BLE001
                log.exception("[%s] erreur sur un log %s", self.chain.key, name)

    # ---- token metadata ------------------------------------------------
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
        try:
            try:
                res = await self.rpc.call("eth_call", [{"to": address, "data": abi.SEL_SYMBOL}, "latest"])
                symbol = abi.decode_string_result(res) or ""
                self.symbols.put(address, symbol)
            except RpcError as exc:
                symbol = ""
                if not exc.transient:  # reverted: not an ERC-20
                    self.symbols.put(address, "")
            fut.set_result(symbol)
            return symbol
        finally:
            self._inflight.pop(address, None)

    async def token_name(self, address: str) -> str:
        try:
            res = await self.rpc.call("eth_call", [{"to": address, "data": abi.SEL_NAME}, "latest"])
            return abi.decode_string_result(res) or ""
        except RpcError:
            return ""

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
        if not tx_hash:
            return "Nouveau token"
        try:
            receipt = await self.rpc.call("eth_getTransactionReceipt", [tx_hash]) or {}
        except RpcError:
            return "Nouveau token"
        to = (receipt.get("to") or "").lower()
        if to in self.chain.launchpads:
            return self.chain.launchpads[to]
        for lg in receipt.get("logs", []):
            emitter = (lg.get("address") or "").lower()
            if emitter in self.chain.launchpads:
                return self.chain.launchpads[emitter]
            if emitter in self.dex_by_address:
                return f"Nouveau token · {self.dex_by_address[emitter][1]}"
        if to:
            return f"Nouveau token · via {short(to)}"
        return "Nouveau token · déploiement direct"

    # ---- handlers ------------------------------------------------------
    async def handle_mint(self, lg: dict) -> None:
        topics = lg.get("topics") or []
        if len(topics) != 3:  # ERC-721 mints have 4 topics
            return
        token = (lg.get("address") or "").lower()
        if not token or token in self.seen_mints or token in self.chain.quote_tokens:
            return
        self.seen_mints.put(token, True)
        symbol = await self.token_symbol(token)
        if token not in self.symbols:  # transient RPC failure: retry on its next mint
            self.seen_mints.pop(token, None)
            return
        if not symbol or not self.scanner.is_watched(self.chain.key, symbol):
            return
        if not await self._is_new_contract(token, lg.get("blockNumber")):
            return
        name = await self.token_name(token)
        minted_to = abi.topic_address(topics[2])
        label = await self._launchpad_label(lg.get("transactionHash"), minted_to)
        await self.scanner.on_detection(Detection(
            chain=self.chain.key, kind=LAUNCH, token_address=token, symbol=symbol, name=name,
            pool_kind="launchpad", dex=label, source="onchain", tx_hash=lg.get("transactionHash"),
        ))

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
        if not pool:
            return None
        return kind, dex, abi.topic_address(topics[1]), abi.topic_address(topics[2]), pool.lower(), None

    async def handle_pair(self, lg: dict) -> None:
        decoded = self._decode_pair(lg)
        if decoded is None:
            return
        kind, dex, token0, token1, pool, hooks = decoded
        for token, other in ((token0, token1), (token1, token0)):
            if token in self.chain.quote_tokens or token == abi.ZERO_ADDRESS:
                continue
            symbol = await self.token_symbol(token)
            if not symbol or not self.scanner.is_watched(self.chain.key, symbol):
                continue
            name = await self.token_name(token)
            quote_symbol = await self.token_symbol(other)
            tx = lg.get("transactionHash")
            has_liq = await self.liquidity_in_tx(tx, kind, pool)
            launchpad = self.chain.v4_hooks.get(hooks or "")
            await self.scanner.on_detection(Detection(
                chain=self.chain.key, kind=LAUNCH if launchpad else PAIR, token_address=token,
                symbol=symbol, name=name, pair_address=pool, pool_kind=kind,
                dex=f"{launchpad} · {dex}" if launchpad else dex,
                quote_symbol=quote_symbol or None, quote_address=other,
                has_liquidity=has_liq, source="onchain", tx_hash=tx,
            ))

    async def liquidity_in_tx(self, tx_hash: str | None, kind: str, pool: str) -> bool:
        """Was liquidity added in the very transaction that created the pool?"""
        if not tx_hash:
            return False
        try:
            receipt = await self.rpc.call("eth_getTransactionReceipt", [tx_hash]) or {}
        except RpcError:
            return False
        for lg in receipt.get("logs", []):
            topics = lg.get("topics") or []
            if not topics:
                continue
            if kind == "v4":
                if (topics[0] == abi.V4_MODIFY_LIQUIDITY and len(topics) > 1
                        and topics[1].lower() == pool and abi.word_int(lg.get("data"), 2) > 0):
                    return True
            elif (topics[0] == abi.TRANSFER and len(topics) == 3
                  and abi.topic_address(topics[2]) == pool and abi.word_uint(lg.get("data"), 0) > 0):
                return True
        return False

    async def handle_v4_liquidity(self, lg: dict) -> None:
        topics = lg.get("topics") or []
        if len(topics) < 2 or topics[0] != abi.V4_MODIFY_LIQUIDITY:
            return
        if abi.word_int(lg.get("data"), 2) <= 0:
            return
        pool_id = topics[1].lower()
        for res in self.scanner.db.find_results(self.chain.key, pool_id):
            await self.scanner.on_detection(Detection(
                chain=self.chain.key, kind=LIQUIDITY, token_address=res.token_address,
                pair_address=pool_id, source="onchain", tx_hash=lg.get("transactionHash"),
            ))
            break

    async def check_pending_liquidity(self) -> None:
        max_age = self.settings.pending_liquidity_max_age_h * 3600
        pending = self.scanner.db.pending_results(max_age, chains=[self.chain.key])
        v4_ids = sorted({r.pair_address for r in pending if r.pool_kind == "v4" and r.pair_address})
        if v4_ids and self.chain.v4_pool_managers:
            self.set_filter("v4liq", {
                "address": list(self.chain.v4_pool_managers),
                "topics": [[abi.V4_MODIFY_LIQUIDITY], v4_ids],
            })
        else:
            self.set_filter("v4liq", None)
        seen: set[tuple[str, str]] = set()
        for res in pending:
            if res.pool_kind not in BALANCE_POOL_KINDS or not res.pair_address:
                continue
            key = (res.token_address, res.pair_address)
            if key in seen:
                continue
            seen.add(key)
            data = abi.encode_address_call(abi.SEL_BALANCE_OF, res.pair_address)
            try:
                balance = _hex_int(await self.rpc.call("eth_call", [{"to": res.token_address, "data": data}, "latest"]))
            except RpcError:
                continue
            if balance > 0:
                await self.scanner.on_detection(Detection(
                    chain=self.chain.key, kind=LIQUIDITY, token_address=res.token_address,
                    pair_address=res.pair_address, source="onchain",
                ))

    async def _liquidity_loop(self) -> None:
        while True:
            try:
                await self.check_pending_liquidity()
            except Exception:  # noqa: BLE001
                log.exception("[%s] vérification de liquidité", self.chain.key)
            await asyncio.sleep(self.settings.evm_liquidity_check_interval)
