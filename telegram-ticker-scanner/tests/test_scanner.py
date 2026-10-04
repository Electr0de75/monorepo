import asyncio
import time
import unittest
from unittest.mock import patch

import httpx

from ticker_scanner.config import EvmRpc
from ticker_scanner.models import LAUNCH, PAIR, STATUS_LIQ, Detection
from tests.helpers import FakeDexScreener, FakeNotifier, Router, drain, make_scanner, make_settings

TOKEN = "0x" + "a1" * 20
POOL = "0x" + "b2" * 20


def ds_pair(created_s: float, liq: float | None = 5000, dex="pancakeswap", labels=("v2",), pair=POOL,
            symbol="ABC", chain="bsc", token=TOKEN):
    return {
        "chainId": chain, "dexId": dex, "labels": list(labels), "pairAddress": pair,
        "baseToken": {"address": token.upper().replace("0X", "0x") if token.startswith("0x") else token,
                      "symbol": symbol, "name": "Abc"},
        "quoteToken": {"address": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "symbol": "WBNB"},
        "liquidity": {"usd": liq} if liq is not None else None, "marketCap": 100000, "fdv": 100000,
        "pairCreatedAt": int(created_s * 1000),
    }


class ScannerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ds = FakeDexScreener()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(Router(**{"api.dexscreener.com": self.ds})))
        self.notifier = FakeNotifier()
        self.scanner = make_scanner(self.client, make_settings(), self.notifier)
        self.entry = self.scanner.db.create_entry("Proj", ["ABC", "DEF"], ["bsc", "solana"])
        self.scanner.reload()

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_watch_index(self):
        self.assertTrue(self.scanner.is_watched("bsc", "$abc"))
        self.assertFalse(self.scanner.is_watched("base", "ABC"))
        self.assertEqual(self.scanner.ticker_index(), {"ABC": {"bsc", "solana"}, "DEF": {"bsc", "solana"}})
        self.scanner.db.update_entry(self.entry.id, paused=True)
        self.scanner.reload()
        self.assertFalse(self.scanner.is_watched("bsc", "ABC"))

    async def test_dexscreener_new_pair_with_liquidity(self):
        await self.scanner.handle_dexscreener_pair(ds_pair(time.time()), "ABC", {"bsc"})
        await drain(self.scanner)
        _, res, labels, _ = self.notifier.sent[0]
        self.assertEqual((labels, res.token_address, res.dex, res.market_cap), (["pair", "liq"], TOKEN, "PancakeSwap v2", 100000))

    async def test_dexscreener_old_pairs_are_ignored(self):
        await self.scanner.handle_dexscreener_pair(ds_pair(time.time() - 86400), "ABC", {"bsc"})
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent, [])

    async def test_dexscreener_launchpad_dex_id(self):
        await self.scanner.handle_dexscreener_pair(
            ds_pair(time.time(), dex="pumpfun", labels=(), pair="4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf", chain="solana", token="7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"),
            "ABC", {"solana"})
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent[0][2], ["launch"])

    async def test_launch_then_unknown_dex_id_is_same_launch(self):
        await self.scanner.on_detection(Detection(chain="bsc", kind=LAUNCH, token_address=TOKEN, symbol="ABC",
                                                  dex="Nouveau token", pool_kind="launchpad"))
        await self.scanner.handle_dexscreener_pair(ds_pair(time.time(), dex="somepad", labels=()), "ABC", {"bsc"})
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)

    async def test_paused_entry_gets_nothing(self):
        self.scanner.db.update_entry(self.entry.id, paused=True)
        self.scanner.reload()
        await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                  pair_address=POOL, dex="x"))
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent, [])

    async def test_pending_pair_gets_liquidity_from_dexscreener(self):
        await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                  pair_address=POOL, pool_kind="v2", dex="PancakeSwap v2"))
        self.ds.pair_data[POOL] = ds_pair(time.time(), liq=0)
        await self.scanner.check_pending_on_dexscreener()
        self.ds.pair_data[POOL] = ds_pair(time.time(), liq=8000)
        await self.scanner.check_pending_on_dexscreener()
        await drain(self.scanner)
        self.assertEqual([s[2] for s in self.notifier.sent], [["pair"], ["liq"]])
        res = self.scanner.db.results_for_entry(self.entry.id)[0]
        self.assertEqual((res.status, res.liquidity_usd), (STATUS_LIQ, 8000))

    async def test_refresh_updates_market_caps(self):
        await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                  pair_address=POOL, dex="x", has_liquidity=True))
        self.ds.token_pairs[TOKEN] = [ds_pair(time.time(), liq=42000)]
        await self.scanner.refresh_entry(self.entry.id)
        res = self.scanner.db.results_for_entry(self.entry.id)[0]
        self.assertEqual((res.liquidity_usd, res.market_cap), (42000, 100000))

    async def test_notification_is_completed_with_market_cap(self):
        self.ds.token_pairs[TOKEN] = [ds_pair(time.time(), liq=42000)]
        with patch("ticker_scanner.scanner.FOLLOWUP_DELAYS_S", (0,)):
            await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                      pair_address=POOL, dex="x", has_liquidity=True))
            await drain(self.scanner)
            for _ in range(20):
                await asyncio.sleep(0.01)
        self.assertEqual(len(self.notifier.edits), 1)
        self.assertEqual(self.notifier.edits[0][0].market_cap, 100000)

    async def test_sources_follow_entries(self):
        scanner = make_scanner(self.client, make_settings(evm_rpc={"bsc": EvmRpc(None, "https://rpc.test")},
                                                          pumpportal_enabled=True), self.notifier)
        self.assertEqual(scanner._desired_sources(), {})
        scanner.db.create_entry("P", ["ABC"], ["bsc", "solana", "polygon"])
        scanner.reload()
        self.assertEqual(sorted(scanner._desired_sources()), ["evm:bsc", "solana:pumpportal"])


if __name__ == "__main__":
    unittest.main()
