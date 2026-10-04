"""Solana sources.

* PumpPortal (free, no key): every pump.fun / bonk.fun token creation and
  pump.fun migrations.
* Launchpad programs through any Solana RPC websocket (Helius free tier is
  enough): LaunchLab (bonk.fun, StonkFun…), Meteora DBC (Bags, Believe…),
  Moonshot, Boop. Name/symbol are read generically from the program events or
  from the creation instruction, so no program-specific decoder is needed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from .chains import PUMPFUN_CREATE_IXS, PUMPFUN_PROGRAM, SOLANA_LAUNCHPAD_PROGRAMS, SOLANA_QUOTE_MINTS
from .health import SourceStats
from .models import LAUNCH, Detection
from .rpc import (
    HttpRpc, RpcError, WsSubscriptions, cancel_requested, default_ws_connect, json_loads, next_message,
)

if TYPE_CHECKING:
    from .scanner import Scanner

log = logging.getLogger(__name__)

PUMPPORTAL_URL = "wss://pumpportal.fun/api/data"
PUMPPORTAL_POOLS = {"pump": "pump.fun", "bonk": "bonk.fun"}

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58decode(text: str) -> bytes:
    num = 0
    for ch in text:
        num = num * 58 + _B58_INDEX[ch]
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + raw


# ---- generic borsh string scanning ------------------------------------
@dataclass(frozen=True)
class TokenStrings:
    name: str
    symbol: str
    uri: str


def _borsh_string_at(blob: bytes, i: int, max_len: int) -> tuple[str, int] | None:
    if i + 4 > len(blob):
        return None
    n = int.from_bytes(blob[i:i + 4], "little")
    if n < 1 or n > max_len or i + 4 + n > len(blob):
        return None
    try:
        text = blob[i + 4:i + 4 + n].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.isprintable():
        return None
    return text, i + 4 + n


def find_token_strings(blob: bytes) -> list[TokenStrings]:
    """Find consecutive borsh strings shaped like (name, symbol, uri)."""
    out = []
    for i in range(0, max(0, len(blob) - 4)):
        first = _borsh_string_at(blob, i, 64)
        if not first:
            continue
        second = _borsh_string_at(blob, first[1], 32)
        if not second:
            continue
        third = _borsh_string_at(blob, second[1], 400)
        if not third:
            continue
        out.append(TokenStrings(first[0].strip("\x00 "), second[0].strip("\x00 "), third[0]))
    return out


_INVOKE = re.compile(r"^Program (\w+) invoke \[\d+\]$")
_EXIT = re.compile(r"^Program (\w+) (success|failed.*|consumed .*)$")
_IX = re.compile(r"^Program log: Instruction: (\w+)")


@dataclass
class ProgramLogs:
    instructions: set[str] = field(default_factory=set)
    data: list[bytes] = field(default_factory=list)


def logs_for_program(logs: list[str], program: str) -> ProgramLogs:
    """Instruction names and `Program data:` payloads emitted by `program`."""
    stack: list[str] = []
    out = ProgramLogs()
    for line in logs if isinstance(logs, list) else []:
        if not isinstance(line, str):
            continue
        m = _INVOKE.match(line)
        if m:
            stack.append(m.group(1))
            continue
        m = _EXIT.match(line)
        if m and m.group(2).startswith(("success", "failed")):
            if stack and stack[-1] == m.group(1):
                stack.pop()
            continue
        if not stack or stack[-1] != program:
            continue
        m = _IX.match(line)
        if m:
            out.instructions.add(m.group(1))
        elif line.startswith("Program data: "):
            try:
                out.data.append(base64.b64decode(line[len("Program data: "):]))
            except ValueError:
                pass
    return out


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def created_accounts(tx: dict) -> set[str]:
    """Accounts that did not exist before this transaction (0 lamports before, > 0 after)."""
    meta = _dict(_dict(tx).get("meta"))
    pre, post = _list(meta.get("preBalances")), _list(meta.get("postBalances"))
    keys = _list(_dict(_dict(_dict(tx).get("transaction")).get("message")).get("accountKeys"))
    out = set()
    for i, key in enumerate(keys):
        pubkey = key.get("pubkey") if isinstance(key, dict) else key
        if (isinstance(pubkey, str) and i < len(pre) and i < len(post)
                and pre[i] == 0 and isinstance(post[i], (int, float)) and post[i] > 0):
            out.add(pubkey)
    return out


def new_mints(tx: dict) -> list[str]:
    """Candidate mints of the token created by `tx`, best first.

    The new token's mint account is created in the transaction, which tells it
    apart from the quote mint (SOL, USD1, or a stock token on StonkFun).
    """
    meta = _dict(_dict(tx).get("meta"))
    pre = {_dict(b).get("mint") for b in _list(meta.get("preTokenBalances"))}
    post: list[str] = []
    for b in _list(meta.get("postTokenBalances")):
        mint = _dict(b).get("mint")
        if isinstance(mint, str) and mint not in post and mint not in SOLANA_QUOTE_MINTS:
            post.append(mint)
    created = created_accounts(tx)
    fresh = [m for m in post if m not in pre]
    ranked = [m for m in fresh if m in created] + [m for m in fresh if m not in created]
    return ranked or post


def instruction_blobs(tx: dict, program: str) -> list[bytes]:
    msg = _dict(_dict(_dict(tx).get("transaction")).get("message"))
    ixs = list(_list(msg.get("instructions")))
    for inner in _list(_dict(_dict(tx).get("meta")).get("innerInstructions")):
        ixs.extend(_list(_dict(inner).get("instructions")))
    blobs = []
    for ix in ixs:
        ix = _dict(ix)
        if ix.get("programId") == program and isinstance(ix.get("data"), str):
            try:
                blobs.append(b58decode(ix["data"]))
            except (KeyError, ValueError, OverflowError):
                pass
    return blobs


# ---- PumpPortal ---------------------------------------------------------
class PumpPortalWatcher:
    # pump.fun creates tokens every few seconds: silence means a dead stream.
    STALL_TIMEOUT_S = 120
    reconnect_delay = 1.0

    def __init__(self, scanner: "Scanner", connect=None, url: str = PUMPPORTAL_URL,
                 stats: SourceStats | None = None):
        self.scanner = scanner
        self.connect = connect or default_ws_connect
        self.url = url
        self.stats = stats or SourceStats("solana:pumpportal")
        self.stats.mode = "websocket"
        self.connections = 0

    async def run(self) -> None:
        backoff = self.reconnect_delay
        log.info("[solana] PumpPortal démarré")
        while True:
            started = time.monotonic()
            received = 0
            try:
                async with self.connect(self.url) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    self.connections += 1
                    self.stats.connected = True
                    iterator = ws.__aiter__()
                    while True:
                        try:
                            raw = await next_message(iterator, self.STALL_TIMEOUT_S)
                        except StopAsyncIteration:
                            raise ConnectionError("fermé par le serveur") from None
                        except asyncio.TimeoutError:
                            raise ConnectionError(f"aucun message depuis {self.STALL_TIMEOUT_S}s") from None
                        self.stats.event()
                        received += 1
                        try:
                            msg = json_loads(raw)
                        except ValueError:
                            continue
                        if not isinstance(msg, dict):
                            continue
                        try:
                            await self.handle(msg)
                        except Exception:  # noqa: BLE001
                            log.exception("[solana] PumpPortal: message illisible")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if cancel_requested():
                    raise asyncio.CancelledError from exc
                self.stats.error(str(exc))
                log.warning("[solana] PumpPortal coupé (%s), reconnexion dans %.0fs", exc, backoff)
            finally:
                self.stats.connected = False
            if received or time.monotonic() - started > 60:
                backoff = self.reconnect_delay  # the stream was alive: reconnect right away
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def handle(self, msg: dict) -> None:
        tx_type = msg.get("txType")
        mint = msg.get("mint")
        if not isinstance(mint, str) or not mint:
            return
        if tx_type == "create":
            symbol = msg.get("symbol") if isinstance(msg.get("symbol"), str) else ""
            if not self.scanner.is_watched("solana", symbol):
                return
            try:
                mc_sol = float(msg.get("marketCapSol") or 0)
            except (TypeError, ValueError, OverflowError):
                mc_sol = 0.0
            if not math.isfinite(mc_sol) or mc_sol < 0:
                mc_sol = 0.0
            pool = msg.get("pool") if isinstance(msg.get("pool"), str) else "pump"
            self.stats.detections += 1
            await self.scanner.on_detection(Detection(
                chain="solana", kind=LAUNCH, token_address=mint, symbol=symbol,
                name=msg.get("name") if isinstance(msg.get("name"), str) else "",
                pair_address=msg.get("bondingCurveKey") if isinstance(msg.get("bondingCurveKey"), str) else None,
                pool_kind="launchpad", dex=PUMPPORTAL_POOLS.get(pool, pool), source="pumpportal",
                tx_hash=msg.get("signature") if isinstance(msg.get("signature"), str) else None,
                market_cap_note=f"{mc_sol:.1f} SOL" if mc_sol else None,
            ))
        elif tx_type in ("migrate", "migration"):
            if self.scanner.db.has_launch("solana", mint):
                self.scanner.lookup_new_pairs_soon("solana", mint)


# ---- Launchpad programs through an RPC websocket ------------------------
class SolanaLaunchpadWatcher:
    STALL_TIMEOUT_S = 180
    TX_FETCH_DELAYS_S = (0.0, 0.7, 2.0, 5.0)

    def __init__(self, ws_url: str, http_url: str, scanner: "Scanner", http_client: httpx.AsyncClient,
                 *, include_pumpfun: bool = False, connect=None, workers: int = 3,
                 stats: SourceStats | None = None):
        self.scanner = scanner
        self.stats = stats or SourceStats("solana:launchpads")
        self.stats.mode = "websocket"
        self.rpc = HttpRpc(http_url, http_client, name="solana-rpc")
        self.ws_url = ws_url
        self.connect = connect
        self.n_workers = workers
        self.programs: dict[str, tuple[str, frozenset[str]]] = dict(SOLANA_LAUNCHPAD_PROGRAMS)
        if include_pumpfun:
            self.programs[PUMPFUN_PROGRAM] = ("pump.fun", PUMPFUN_CREATE_IXS)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=20_000)

    async def run(self) -> None:
        ws = WsSubscriptions(
            self.ws_url, subscribe_method="logsSubscribe", unsubscribe_method="logsUnsubscribe",
            on_message=self._on_message, connect=self.connect, name="solana",
            stall_timeout=self.STALL_TIMEOUT_S, on_state=self._on_state,
        )
        for program in self.programs:
            ws.set(program, [{"mentions": [program]}, {"commitment": "confirmed"}])
        tasks = [asyncio.create_task(self._worker()) for _ in range(self.n_workers)]
        log.info("[solana] launchpads surveillés: %s", ", ".join(n for n, _ in self.programs.values()))
        try:
            await ws.run()
        finally:
            for t in tasks:
                t.cancel()

    def _on_state(self, connected: bool) -> None:
        self.stats.connected = connected
        if not connected:
            self.stats.error("websocket déconnecté")

    def _on_message(self, program: str, result: dict) -> None:
        self.stats.event()
        value = (result or {}).get("value") or {} if isinstance(result, dict) else {}
        if value.get("err") or not value.get("signature"):
            return
        try:
            self.queue.put_nowait((program, value["signature"], value.get("logs") or []))
        except asyncio.QueueFull:
            log.warning("[solana] file pleine, transaction ignorée")

    async def _worker(self) -> None:
        while True:
            program, signature, logs = await self.queue.get()
            try:
                await self.analyze(program, signature, logs)
            except RpcError as exc:
                log.debug("[solana] rpc: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("[solana] erreur d'analyse %s", signature)

    async def analyze(self, program: str, signature: str, logs: list[str]) -> None:
        label, create_ixs = self.programs.get(program, (program[:8], frozenset()))
        plogs = logs_for_program(logs, program)
        found = [s for blob in plogs.data for s in find_token_strings(blob)]
        tx = None
        if not found:
            if not (plogs.instructions & create_ixs):
                return
            tx = await self._get_tx(signature)
            if not tx:
                return
            found = [s for blob in instruction_blobs(tx, program) for s in find_token_strings(blob)]
        matches = [s for s in found if self.scanner.is_watched("solana", s.symbol)]
        if not matches:
            return
        if tx is None:
            tx = await self._get_tx(signature)
            if not tx:
                return
        mints = new_mints(tx)
        if not mints:
            log.info("[solana] %s: ticker %s trouvé mais mint introuvable (%s)", label, matches[0].symbol, signature)
            return
        hit = matches[0]
        self.stats.detections += 1
        await self.scanner.on_detection(Detection(
            chain="solana", kind=LAUNCH, token_address=mints[0], symbol=hit.symbol, name=hit.name,
            pool_kind="launchpad", dex=label, source="solana-rpc", tx_hash=signature,
        ))

    async def _get_tx(self, signature: str) -> dict | None:
        # Right after the notification the node may not serve the transaction yet.
        for delay in self.TX_FETCH_DELAYS_S:
            if delay:
                await asyncio.sleep(delay)
            tx = await self.rpc.call("getTransaction", [signature, {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed",
            }])
            if tx:
                return tx
        log.info("[solana] transaction %s introuvable", signature)
        return None
