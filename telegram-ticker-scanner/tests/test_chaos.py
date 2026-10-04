"""Chaos test: the whole app against services that fail at random.

RPC, DexScreener and Telegram fail ~30 % of the time (5xx / 429 / network
errors) with random latency, websockets drop without warning and replay
events. The planted launch must be notified exactly once and nothing may crash.
"""

import asyncio
import json
import random
import time
import unittest
from unittest.mock import patch

import httpx

from ticker_scanner import evm_abi as abi
from ticker_scanner.bot import BotUI, TelegramNotifier
from ticker_scanner.config import EvmRpc
from ticker_scanner.rpc import WsSubscriptions
from ticker_scanner.telegram_api import TelegramAPI
from tests.helpers import FakeDexScreener, FakeEvmNode, FakeTelegram, FakeWebSocket, Router, addr_word, make_scanner, \
    make_settings, uint_word

TOKEN = "0x" + "a1" * 20
PAIR = "0x" + "b2" * 20
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
PANCAKE_V2 = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"


def pair_created():
    return {"address": PANCAKE_V2, "blockNumber": "0x101", "transactionHash": "0xtx1",
            "topics": [abi.V2_PAIR_CREATED, "0x" + addr_word(TOKEN), "0x" + addr_word(WBNB)],
            "data": "0x" + addr_word(PAIR) + uint_word(1)}


class ChaosSocket(FakeWebSocket):
    """Answers subscriptions, replays the pair event and noise, then dies at random."""

    def __init__(self, rng):
        super().__init__()
        self.rng = rng
        self.subs = {}
        asyncio.get_running_loop().create_task(self._chaos())

    async def send(self, raw):
        msg = json.loads(raw)
        await super().send(raw)
        if msg.get("method") == "eth_subscribe":
            self.subs[f"0xsub{self.sub_counter}"] = msg["params"][1]

    async def _chaos(self):
        for _ in range(self.rng.randint(3, 15)):
            await asyncio.sleep(self.rng.random() * 0.03)
            for sub_id, flt in list(self.subs.items()):
                if "address" in flt:
                    log = pair_created()  # the same event, again and again
                else:
                    log = {"address": "0x%040x" % self.rng.getrandbits(160), "blockNumber": "0x102",
                           "transactionHash": "0x2", "data": "0x" + uint_word(1),
                           "topics": [abi.TRANSFER, abi.ZERO_TOPIC, "0x" + addr_word(WBNB)]}
                self.push({"jsonrpc": "2.0", "method": "eth_subscription",
                           "params": {"subscription": sub_id, "result": log}})
        if self.rng.random() < 0.5:
            self.push({"garbage": True})
            self.incoming.put_nowait("not json {")
        self.close()  # drop the connection


class ChaosTest(unittest.IsolatedAsyncioTestCase):
    async def test_exactly_once_under_chaos(self):
        rng = random.Random(42)
        tg, ds, node = FakeTelegram(), FakeDexScreener(), FakeEvmNode()
        node.symbols[TOKEN] = "ABC"
        node.names[TOKEN] = "Abc"
        node.receipts["0xtx1"] = {"logs": [{"address": PAIR, "topics": [abi.V2_MINT, "0x" + addr_word(WBNB)],
                                            "data": "0x" + uint_word(1) + uint_word(1)}]}
        node.reserves[PAIR] = (10 ** 18, 10 ** 18)
        ds.search_results["ABC"] = [{
            "chainId": "bsc", "dexId": "pancakeswap", "labels": ["v2"], "pairAddress": PAIR,
            "baseToken": {"address": TOKEN, "symbol": "ABC", "name": "Abc"},
            "quoteToken": {"address": WBNB, "symbol": "WBNB"}, "liquidity": {"usd": 9000},
            "marketCap": 50000, "pairCreatedAt": int(time.time() * 1000) + 60_000}]
        router = Router(**{"api.telegram.org": tg, "api.dexscreener.com": ds, "rpc.test": node})
        failures = {"count": 0}

        async def chaotic(request):
            await asyncio.sleep(rng.random() * 0.01)
            if request.url.path.endswith("getUpdates"):
                await asyncio.sleep(0.02)
                return router(request)
            roll = rng.random()
            if roll < 0.1:
                failures["count"] += 1
                raise httpx.ConnectError("chaos")
            if roll < 0.2:
                failures["count"] += 1
                return httpx.Response(502)
            if roll < 0.3:
                failures["count"] += 1
                if request.url.host == "api.telegram.org":
                    return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "slow down",
                                                     "parameters": {"retry_after": 0}})
                return httpx.Response(429)
            return router(request)

        with patch.object(WsSubscriptions, "reconnect_delay", 0.01), \
                patch("ticker_scanner.scanner.NOTIFY_RETRY_DELAYS_S", (0.01,) * 8), \
                patch("ticker_scanner.scanner.SUPERVISOR_FIRST_DELAY_S", 0.01):
            async with httpx.AsyncClient(transport=httpx.MockTransport(chaotic)) as client:
                settings = make_settings(evm_rpc={"bsc": EvmRpc("wss://rpc.test", "https://rpc.test")},
                                         dexscreener_interval=2, pumpportal_enabled=False)
                api = TelegramAPI("1:T", client)
                scanner = make_scanner(client, settings, TelegramNotifier(api, 42, min_interval=0))
                scanner.ds.limiter.interval = 0
                scanner.ws_connect = lambda url: ChaosSocket(rng)
                scanner.db.create_entry("Chaos", ["ABC"], ["bsc"])
                scanner.reload()
                bot = BotUI(api, scanner.db, scanner, settings)
                run = asyncio.create_task(scanner.run())
                poll = asyncio.create_task(bot.run_polling())
                await asyncio.sleep(4)
                self.assertFalse(run.done(), run.exception() if run.done() else None)
                self.assertFalse(poll.done())
                run.cancel()
                poll.cancel()
                await asyncio.gather(run, poll, return_exceptions=True)

        notifs = [b for m, b in tg.sent if m == "sendMessage" and TOKEN in b.get("text", "")]
        self.assertGreater(failures["count"], 5)  # the chaos really happened (count depends on timing)
        self.assertEqual(len(notifs), 1, [n["text"][:80] for n in notifs])
        self.assertIn("🟡 PAIR CREATED + 🟢 LIQ ADDED", notifs[0]["text"])
        self.assertEqual(len(scanner.db.results_for_entry(1)), 1)


if __name__ == "__main__":
    unittest.main()
