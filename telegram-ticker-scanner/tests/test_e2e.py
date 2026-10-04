"""End-to-end: the whole app (scanner loops + Telegram polling) against fake services."""

import asyncio
import time
import unittest

import httpx

from ticker_scanner.bot import BotUI, TelegramNotifier
from ticker_scanner.config import EvmRpc
from ticker_scanner.telegram_api import TelegramAPI
from tests.helpers import (
    FakeDexScreener, FakeEvmNode, FakeTelegram, FakeWebSocket, Router, make_scanner, make_settings,
)


def message(update_id, text):
    return {"update_id": update_id, "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": text}}


def click(update_id, data):
    return {"update_id": update_id, "callback_query": {"id": str(update_id), "from": {"id": 42}, "data": data,
                                                       "message": {"message_id": 1, "chat": {"id": 42}}}}


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_project_and_get_notified(self):
        tg, ds, node = FakeTelegram(), FakeDexScreener(), FakeEvmNode()
        router = Router(**{"api.telegram.org": tg, "api.dexscreener.com": ds, "rpc.test": node})

        async def handler(request):
            if request.url.path.endswith("getUpdates") and not tg.updates:
                await asyncio.sleep(0.02)  # long polling
            return router(request)

        sockets = []

        def connect(url):
            sockets.append(url)
            return FakeWebSocket()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            settings = make_settings(evm_rpc={"bsc": EvmRpc("wss://rpc.test", "https://rpc.test")},
                                     solana_ws_url="wss://sol.test", solana_http_url="https://sol.test",
                                     dexscreener_interval=2)
            api = TelegramAPI("1:T", client)
            scanner = make_scanner(client, settings, TelegramNotifier(api, 42, min_interval=0))
            scanner.ws_connect = connect
            bot = BotUI(api, scanner.db, scanner, settings)
            ds.search_results["MOON"] = [{
                "chainId": "solana", "dexId": "raydium", "labels": ["CPMM"], "pairAddress": "PoolX",
                "baseToken": {"address": "MintX", "symbol": "MOON", "name": "Moon"},
                "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
                "liquidity": {"usd": 30000}, "marketCap": 250000, "pairCreatedAt": int(time.time() * 1000) + 60_000,
            }]
            tg.updates = [message(1, "/nouveau"), message(2, "Projet test"), message(3, "$moon"),
                          click(4, "sc:ct:solana"), click(5, "sc:ct:bsc"), click(6, "sc:cv")]
            run = asyncio.create_task(scanner.run())
            poll = asyncio.create_task(bot.run_polling())
            try:
                for _ in range(300):
                    await asyncio.sleep(0.01)
                    if any(b["text"].startswith("<pre>") for b in tg.of("sendMessage")):
                        break
            finally:
                run.cancel()
                poll.cancel()
                await asyncio.gather(run, poll, return_exceptions=True)

            entry = scanner.db.list_entries()[0]
            self.assertEqual((entry.name, entry.tickers, entry.chains), ("Projet test", ["MOON"], ["solana", "bsc"]))
            self.assertEqual(sorted(sockets), ["wss://pumpportal.fun/api/data", "wss://rpc.test", "wss://sol.test"])
            notif = next(b for b in tg.of("sendMessage") if b["text"].startswith("<pre>"))
            self.assertIn("🟡 PAIR CREATED + 🟢 LIQ ADDED", notif["text"])
            self.assertIn("<code>MintX</code>", notif["text"])


if __name__ == "__main__":
    unittest.main()
