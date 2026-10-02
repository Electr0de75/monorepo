"""JSON-RPC over HTTP (httpx) and websocket subscriptions with auto-reconnect."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)


class RpcError(Exception):
    def __init__(self, message: str, *, transient: bool = True):
        super().__init__(message)
        self.transient = transient


class HttpRpc:
    def __init__(self, url: str, client: httpx.AsyncClient, *, retries: int = 3, name: str = "rpc"):
        self.url = url
        self.client = client
        self.retries = retries
        self.name = name
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        delay = 0.5
        for attempt in range(self.retries + 1):
            try:
                resp = await self.client.post(self.url, json=payload, timeout=20)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RpcError(f"HTTP {resp.status_code}")
                body = resp.json()
                if body.get("error"):
                    err = body["error"]
                    msg = err.get("message", err) if isinstance(err, dict) else err
                    # A node error (revert, bad params) is an answer, not a transient failure.
                    raise RpcError(f"{method}: {msg}", transient=False)
                return body.get("result")
            except RpcError as exc:
                if not exc.transient or attempt >= self.retries:
                    raise
            except (httpx.HTTPError, ValueError) as exc:
                if attempt >= self.retries:
                    raise RpcError(f"{method}: {exc}") from exc
            await asyncio.sleep(delay)
            delay *= 2
        raise RpcError(f"{method}: unreachable")


def default_ws_connect(url: str):
    """Open a websocket with the `websockets` package (imported lazily)."""
    try:
        from websockets.asyncio.client import connect
    except ImportError:  # websockets < 13
        from websockets import connect  # type: ignore
    return connect(url, ping_interval=20, ping_timeout=30, max_size=2 ** 24, open_timeout=20)


OnMessage = Callable[[str, Any], Awaitable[None] | None]


class WsSubscriptions:
    """Keeps a set of named subscriptions alive on one websocket.

    ``set(name, params)`` adds/replaces/removes (params=None) a subscription at
    any time; the connection re-subscribes everything after a reconnect and
    calls ``on_connect`` so callers can backfill what they missed.
    """

    def __init__(self, url: str, *, subscribe_method: str, unsubscribe_method: str,
                 on_message: OnMessage, on_connect: Callable[[], Awaitable[None]] | None = None,
                 connect: Callable[[str], Any] | None = None, name: str = "ws"):
        self.url = url
        self.subscribe_method = subscribe_method
        self.unsubscribe_method = unsubscribe_method
        self.on_message = on_message
        self.on_connect = on_connect
        self.connect = connect or default_ws_connect
        self.name = name
        self.specs: dict[str, list | None] = {}
        self._dirty = asyncio.Event()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self.connected = False

    def set(self, name: str, params: list | None) -> None:
        if self.specs.get(name) != params:
            self.specs[name] = params
            self._dirty.set()

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with self.connect(self.url) as ws:
                    backoff = 1.0
                    log.info("[%s] websocket connecté", self.name)
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                log.warning("[%s] websocket coupé (%s), reconnexion dans %.0fs", self.name, exc, backoff)
            finally:
                self.connected = False
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("websocket closed"))
                self._pending.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _request(self, ws, method: str, params: list) -> Any:
        req_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout=30)

    async def _reader(self, ws, routes: dict[Any, str]) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "id" in msg and msg["id"] in self._pending:
                fut = self._pending.pop(msg["id"])
                if not fut.done():
                    if msg.get("error"):
                        fut.set_exception(RpcError(str(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))
                continue
            params = msg.get("params") or {}
            sub = params.get("subscription")
            name = routes.get(sub)
            if name is None:
                continue
            try:
                res = self.on_message(name, params.get("result"))
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                log.exception("[%s] erreur de traitement", self.name)
        raise ConnectionError("websocket fermé par le serveur")

    async def _session(self, ws) -> None:
        routes: dict[Any, str] = {}
        active: dict[str, tuple[Any, list]] = {}
        reader = asyncio.create_task(self._reader(ws, routes))
        try:
            self.connected = True
            if self.on_connect is not None:
                asyncio.create_task(self.on_connect())
            while True:
                self._dirty.clear()
                for name, params in list(self.specs.items()):
                    cur = active.get(name)
                    if cur is not None and cur[1] == params:
                        continue
                    if cur is not None:
                        routes.pop(cur[0], None)
                        active.pop(name)
                        if cur[0] is not None:
                            try:
                                await self._request(ws, self.unsubscribe_method, [cur[0]])
                            except RpcError:
                                pass
                    if params is not None:
                        try:
                            sub_id = await self._request(ws, self.subscribe_method, params)
                        except RpcError as exc:
                            # Refused by the provider: report it once, keep the other subscriptions.
                            log.error("[%s] abonnement %s refusé par le RPC: %s", self.name, name, exc)
                            active[name] = (None, params)
                            continue
                        active[name] = (sub_id, params)
                        routes[sub_id] = name
                waiter = asyncio.create_task(self._dirty.wait())
                done, _ = await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    waiter.cancel()
                    reader.result()
                    return
        finally:
            reader.cancel()
