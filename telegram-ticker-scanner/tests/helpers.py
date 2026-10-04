"""Fakes for the HTTP / websocket services the scanner talks to."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from ticker_scanner import evm_abi as abi
from ticker_scanner.config import EvmRpc, Settings
from ticker_scanner.db import Database
from ticker_scanner.dexscreener import DexScreener
from ticker_scanner.scanner import Scanner


def abi_string(text: str) -> str:
    raw = text.encode()
    padded = raw + b"\x00" * ((32 - len(raw) % 32) % 32)
    return "0x" + (32).to_bytes(32, "big").hex() + len(raw).to_bytes(32, "big").hex() + padded.hex()


def addr_word(address: str) -> str:
    return address.lower().replace("0x", "").rjust(64, "0")


def uint_word(value: int) -> str:
    return (value % (1 << 256)).to_bytes(32, "big").hex()


class FakeEvmNode:
    def __init__(self):
        self.symbols: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.receipts: dict[str, dict] = {}
        self.existing_contracts: set[str] = set()
        self.balances: dict[tuple[str, str], int] = {}
        self.reserves: dict[str, tuple[int, int]] = {}
        self.pair_tokens: dict[str, tuple[str, str, str]] = {}  # pair -> (token0, token1, factory)
        self.block = 0x100
        self.logs: list[dict] = []
        self.calls: list[tuple[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body.get("params") or []
        self.calls.append((method, params))
        result: Any = None
        if method == "eth_call":
            to = params[0]["to"].lower()
            data = params[0]["data"]
            if data == abi.SEL_SYMBOL:
                if to not in self.symbols:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                                     "error": {"code": 3, "message": "execution reverted"}})
                result = abi_string(self.symbols[to])
            elif data == abi.SEL_NAME:
                result = abi_string(self.names.get(to, ""))
            elif data.startswith(abi.SEL_BALANCE_OF):
                holder = "0x" + data[-40:]
                result = "0x" + uint_word(self.balances.get((to, holder), 0))
            elif data == abi.SEL_GET_RESERVES:
                r0, r1 = self.reserves.get(to, (0, 0))
                result = "0x" + uint_word(r0) + uint_word(r1) + uint_word(0)
            elif data in (abi.SEL_TOKEN0, abi.SEL_TOKEN1, abi.SEL_FACTORY):
                if to not in self.pair_tokens:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                                     "error": {"code": 3, "message": "execution reverted"}})
                idx = {abi.SEL_TOKEN0: 0, abi.SEL_TOKEN1: 1, abi.SEL_FACTORY: 2}[data]
                result = "0x" + addr_word(self.pair_tokens[to][idx])
        elif method == "eth_getCode":
            result = "0x6080" if params[0].lower() in self.existing_contracts else "0x"
        elif method == "eth_getTransactionReceipt":
            result = self.receipts.get(params[0])
        elif method == "eth_blockNumber":
            result = hex(self.block)
        elif method == "eth_getLogs":
            flt = params[0]
            lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
            result = [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi and _matches(flt, lg)]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def _matches(flt: dict, lg: dict) -> bool:
    addrs = flt.get("address")
    if addrs:
        addrs = [addrs] if isinstance(addrs, str) else addrs
        if lg["address"].lower() not in [a.lower() for a in addrs]:
            return False
    for i, want in enumerate(flt.get("topics") or []):
        if want is None:
            continue
        options = want if isinstance(want, list) else [want]
        if i >= len(lg["topics"]) or lg["topics"][i].lower() not in [o.lower() for o in options]:
            return False
    return True


class FakeDexScreener:
    def __init__(self):
        self.search_results: dict[str, list[dict]] = {}
        self.token_pairs: dict[str, list[dict]] = {}
        self.pair_data: dict[str, dict] = {}
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(str(request.url))
        if path.startswith("/latest/dex/search"):
            q = request.url.params.get("q", "")
            return httpx.Response(200, json={"pairs": self.search_results.get(q, [])})
        if path.startswith("/latest/dex/pairs/"):
            addrs = path.rsplit("/", 1)[1].split(",")
            return httpx.Response(200, json={"pairs": [self.pair_data[a] for a in addrs if a in self.pair_data]})
        if path.startswith("/tokens/v1/"):
            addrs = path.rsplit("/", 1)[1].split(",")
            out = []
            for a in addrs:
                out.extend(self.token_pairs.get(a, []))
            return httpx.Response(200, json=out)
        return httpx.Response(404)


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.next_message_id = 100
        self.updates: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content) if request.content else {}
        self.sent.append((method, body))
        if method in ("sendMessage",):
            self.next_message_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_message_id}})
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        return httpx.Response(200, json={"ok": True, "result": True})

    def of(self, method: str) -> list[dict]:
        return [b for m, b in self.sent if m == method]


class Router:
    """Dispatches httpx requests to the right fake by host."""

    def __init__(self, **by_host):
        self.by_host = by_host

    def __call__(self, request: httpx.Request) -> httpx.Response:
        for host, fake in self.by_host.items():
            if request.url.host == host:
                return fake.handler(request)
        return httpx.Response(404)


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[Any, Any, list[str], str | None]] = []
        self.edits: list[tuple[Any, int]] = []
        self.notices: list[tuple[Any, str]] = []

    async def send(self, entry, result, labels, note):
        self.sent.append((entry, result, labels, note))
        return 1000 + len(self.sent)

    async def edit(self, entry, result, labels, note, message_id):
        self.edits.append((result, message_id))

    async def notice(self, entry, text):
        self.notices.append((entry, text))


def make_settings(**kw) -> Settings:
    base = dict(telegram_token="t", allowed_users={42}, notify_chat_id=None)
    base.update(kw)
    return Settings(**base)


def make_scanner(client: httpx.AsyncClient, settings: Settings | None = None, notifier=None) -> Scanner:
    settings = settings or make_settings(evm_rpc={"bsc": EvmRpc(None, "https://rpc.test")})
    db = Database(":memory:")
    return Scanner(db, settings, client, notifier, dexscreener=DexScreener(client, per_minute=100000))


async def drain(scanner: Scanner) -> None:
    """Run the notification worker until the queue is empty."""
    worker = asyncio.create_task(scanner._notify_worker())
    for _ in range(50):
        await asyncio.sleep(0)
        if scanner._notify_queue.empty():
            break
    await asyncio.sleep(0)
    worker.cancel()


class FakeWebSocket:
    """Async-iterable fake websocket answering subscribe requests."""

    def __init__(self, notifications_after_subscribe: list[dict] | None = None, close_after: bool = True):
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.notifications = notifications_after_subscribe or []
        self.close_after = close_after
        self.sub_counter = 0

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if "id" in msg and msg.get("method", "").endswith("ubscribe") and not msg["method"].endswith("nsubscribe"):
            self.sub_counter += 1
            sub_id = f"0xsub{self.sub_counter}"
            await self.incoming.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": sub_id}))
        elif "id" in msg:
            await self.incoming.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": True}))

    def push(self, payload: dict) -> None:
        self.incoming.put_nowait(json.dumps(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raw = await self.incoming.get()
        if raw is None:
            raise StopAsyncIteration
        return raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def close(self) -> None:
        self.incoming.put_nowait(None)
