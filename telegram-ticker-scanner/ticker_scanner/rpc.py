"""JSON-RPC over HTTP (httpx) and websocket subscriptions with auto-reconnect."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

try:  # optional: ~3x faster JSON decoding of the websocket firehose (pip install orjson)
    from orjson import loads as json_loads  # type: ignore
except ImportError:  # pragma: no cover
    json_loads = json.loads

# Error kinds
NETWORK = "network"        # transport error, HTTP 5xx: retry later
RATE_LIMIT = "rate_limit"  # HTTP 429 or provider quota message: retry later
REVERT = "revert"          # the contract call reverted: a definitive answer
NODE = "node"              # any other JSON-RPC error (bad range, too many results…)

_RATE_LIMIT_HINTS = ("rate limit", "too many requests", "compute units", "capacity", "exceeded the",
                     "request limit", "quota")


class RpcError(Exception):
    def __init__(self, message: str, *, kind: str = NETWORK):
        super().__init__(message)
        self.kind = kind

    @property
    def transient(self) -> bool:
        return self.kind in (NETWORK, RATE_LIMIT)


def classify_error(err: Any) -> str:
    code = err.get("code") if isinstance(err, dict) else None
    message = str(err.get("message", err) if isinstance(err, dict) else err).lower()
    if code == 3 or "revert" in message:
        return REVERT
    if code == 429 or any(h in message for h in _RATE_LIMIT_HINTS):
        return RATE_LIMIT
    return NODE


class HttpRpc:
    def __init__(self, url: str, client: httpx.AsyncClient, *, retries: int = 3, name: str = "rpc",
                 base_delay: float = 0.5):
        self.url = url
        self.client = client
        self.retries = retries
        self.name = name
        self.base_delay = base_delay
        self._ids = itertools.count(1)
        self.last_ok: float | None = None

    async def call(self, method: str, params: list | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        delay = self.base_delay
        node_retry_done = False
        for attempt in range(self.retries + 1):
            try:
                try:
                    resp = await self.client.post(self.url, json=payload, timeout=20)
                except httpx.HTTPError as exc:
                    raise RpcError(f"{method}: {type(exc).__name__}", kind=NETWORK) from exc
                if resp.status_code == 429:
                    raise RpcError(f"{method}: HTTP 429", kind=RATE_LIMIT)
                if resp.status_code >= 500:
                    raise RpcError(f"{method}: HTTP {resp.status_code}", kind=NETWORK)
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise RpcError(f"{method}: réponse HTTP {resp.status_code} illisible", kind=NETWORK) from exc
                if isinstance(body, dict) and body.get("error"):
                    err = body["error"]
                    msg = err.get("message", err) if isinstance(err, dict) else err
                    raise RpcError(f"{method}: {msg}", kind=classify_error(err))
                self.last_ok = time.time()
                return body.get("result") if isinstance(body, dict) else None
            except RpcError as exc:
                if exc.kind == REVERT or attempt >= self.retries:
                    raise
                if exc.kind == NODE:
                    # One retry (load-balanced nodes can lag), then it is an answer.
                    if node_retry_done:
                        raise
                    node_retry_done = True
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


def cancel_requested() -> bool:
    """True when the current task has been asked to stop (Python 3.11+)."""
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    return bool(cancelling and cancelling())


async def wait_result(awaitable, timeout: float | None):
    """Like asyncio.wait_for, but never swallows a cancellation of the caller.

    (asyncio.wait_for before Python 3.12 can lose a cancel() that races with
    the awaited result, which would leave a stopped watcher running.)
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if not done:
        task.cancel()
        raise asyncio.TimeoutError
    return task.result()


async def next_message(iterator, timeout: float | None):
    """Next websocket message; TimeoutError when the connection went silent."""
    return await wait_result(iterator.__anext__(), timeout)


OnMessage = Callable[[str, Any], Awaitable[None] | None]


class WsSubscriptions:
    """Keeps a set of named subscriptions alive on one websocket.

    ``set(name, params)`` adds/replaces/removes (params=None) a subscription at
    any time; the connection re-subscribes everything after a reconnect and
    calls ``on_connect`` so callers can backfill what they missed. When
    ``stall_timeout`` is set, a connection that stays silent that long is
    considered dead and re-opened (some providers stop streaming without
    closing the socket).
    """

    reconnect_delay = 1.0

    def __init__(self, url: str, *, subscribe_method: str, unsubscribe_method: str,
                 on_message: OnMessage, on_connect: Callable[[], Awaitable[None]] | None = None,
                 connect: Callable[[str], Any] | None = None, name: str = "ws",
                 stall_timeout: float | None = None, on_state: Callable[[bool], None] | None = None):
        self.url = url
        self.subscribe_method = subscribe_method
        self.unsubscribe_method = unsubscribe_method
        self.on_message = on_message
        self.on_connect = on_connect
        self.connect = connect or default_ws_connect
        self.name = name
        self.stall_timeout = stall_timeout
        self.on_state = on_state
        self.specs: dict[str, list | None] = {}
        self._dirty = asyncio.Event()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._side_tasks: set[asyncio.Task] = set()
        self.connected = False
        self.connections = 0
        self._session_messages = 0

    def set(self, name: str, params: list | None) -> None:
        if self.specs.get(name) != params:
            self.specs[name] = params
            self._dirty.set()

    def _set_connected(self, value: bool) -> None:
        self.connected = value
        if self.on_state is not None:
            self.on_state(value)

    async def run(self) -> None:
        backoff = self.reconnect_delay
        while True:
            started = time.monotonic()
            self._session_messages = 0
            try:
                async with self.connect(self.url) as ws:
                    self.connections += 1
                    log.info("[%s] websocket connecté", self.name)
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                if cancel_requested():
                    raise asyncio.CancelledError from exc
                log.warning("[%s] websocket coupé (%s: %s), reconnexion dans %.0fs",
                            self.name, type(exc).__name__, exc, backoff)
            finally:
                self._set_connected(False)
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("websocket closed"))
                self._pending.clear()
            if self._session_messages or time.monotonic() - started > 60:
                backoff = self.reconnect_delay  # the connection was useful: reconnect right away
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _request(self, ws, method: str, params: list) -> Any:
        req_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
            return await wait_result(fut, 30)
        finally:
            self._pending.pop(req_id, None)

    async def _reader(self, ws, routes: dict[Any, str]) -> None:
        try:
            await self._read_loop(ws, routes)
        finally:
            # Unblock requests waiting for an answer that will never come.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("websocket closed"))

    async def _read_loop(self, ws, routes: dict[Any, str]) -> None:
        iterator = ws.__aiter__()
        while True:
            try:
                raw = await next_message(iterator, self.stall_timeout)
            except StopAsyncIteration:
                raise ConnectionError("websocket fermé par le serveur") from None
            except asyncio.TimeoutError:
                raise ConnectionError(f"aucun message depuis {self.stall_timeout:.0f}s") from None
            try:
                msg = json_loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg and msg["id"] in self._pending:
                fut = self._pending.pop(msg["id"])
                if not fut.done():
                    if msg.get("error"):
                        fut.set_exception(RpcError(str(msg["error"]), kind=classify_error(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))
                continue
            params = msg.get("params") or {}
            name = routes.get(params.get("subscription")) if isinstance(params, dict) else None
            if name is None:
                continue
            self._session_messages += 1
            try:
                res = self.on_message(name, params.get("result"))
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                log.exception("[%s] erreur de traitement", self.name)

    async def _session(self, ws) -> None:
        routes: dict[Any, str] = {}
        active: dict[str, tuple[Any, list]] = {}
        reader = asyncio.create_task(self._reader(ws, routes))
        try:
            self._set_connected(True)
            if self.on_connect is not None:
                task = asyncio.create_task(self.on_connect())
                self._side_tasks.add(task)
                task.add_done_callback(self._side_tasks.discard)
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
                try:
                    done, _ = await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
                if reader in done:
                    reader.result()
                    return
        finally:
            reader.cancel()
