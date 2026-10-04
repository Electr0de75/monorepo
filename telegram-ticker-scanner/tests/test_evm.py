import asyncio
import json
import time
import unittest

import httpx

from ticker_scanner import evm_abi as abi
from ticker_scanner.chains import CHAINS
from ticker_scanner.config import EvmRpc
from ticker_scanner.evm_watcher import EvmWatcher
from ticker_scanner.models import STATUS_LAUNCH, STATUS_LIQ, STATUS_PAIR
from tests.helpers import (
    FakeDexScreener, FakeEvmNode, FakeNotifier, FakeWebSocket, Router, addr_word, drain, make_scanner,
    make_settings, uint_word,
)

BSC = CHAINS["bsc"]
RH = CHAINS["robinhood"]
PANCAKE_V2 = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"
FOUR_MEME = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
TOKEN = "0x" + "a1" * 20
PAIR = "0x" + "b2" * 20


def pair_created_log(token0, token1, pair, tx="0xtx1", block=0x101):
    return {
        "address": PANCAKE_V2, "blockNumber": hex(block), "transactionHash": tx,
        "topics": [abi.V2_PAIR_CREATED, "0x" + addr_word(token0), "0x" + addr_word(token1)],
        "data": "0x" + addr_word(pair) + uint_word(1),
    }


def transfer_log(token, frm, to, amount, tx, block=0x101):
    return {
        "address": token, "blockNumber": hex(block), "transactionHash": tx,
        "topics": [abi.TRANSFER, "0x" + addr_word(frm), "0x" + addr_word(to)],
        "data": "0x" + uint_word(amount),
    }


class EvmWatcherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.node = FakeEvmNode()
        self.ds = FakeDexScreener()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(
            Router(**{"rpc.test": self.node, "api.dexscreener.com": self.ds})))
        self.notifier = FakeNotifier()
        settings = make_settings(evm_rpc={"bsc": EvmRpc(None, "https://rpc.test"),
                                          "robinhood": EvmRpc(None, "https://rpc.test")})
        self.scanner = make_scanner(self.client, settings, self.notifier)
        self.entry = self.scanner.db.create_entry("Proj", ["ABC"], ["bsc", "robinhood"])
        self.scanner.reload()
        self.watcher = EvmWatcher(BSC, settings.evm_rpc["bsc"], self.scanner, settings, self.client)
        self.node.symbols[TOKEN] = "ABC"
        self.node.names[TOKEN] = "Abc Coin"

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_pair_without_liquidity_then_liquidity(self):
        self.node.receipts["0xtx1"] = {"to": PANCAKE_V2, "logs": []}
        await self.watcher.handle_pair(pair_created_log(TOKEN, WBNB, PAIR))
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)
        _, res, labels, _ = self.notifier.sent[0]
        self.assertEqual(labels, ["pair"])
        self.assertEqual((res.status, res.pair_address, res.quote_symbol, res.dex),
                         (STATUS_PAIR, PAIR, "WBNB", "PancakeSwap v2"))
        # symbol() of WBNB is never called (known quote token)
        self.assertNotIn(WBNB, [p[0]["to"] for m, p in self.node.calls if m == "eth_call"])

        # liquidity check: balance of the pool becomes > 0
        now = time.time()
        await self.watcher.check_pending_liquidity(now)
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)
        self.node.balances[(TOKEN, PAIR)] = 10 ** 18
        await self.watcher.check_pending_liquidity(now + 1)  # too soon: not checked again
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)
        await self.watcher.check_pending_liquidity(now + 7)
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.notifier.sent[1][2], ["liq"])
        self.assertEqual(self.scanner.db.results_for_entry(self.entry.id)[0].status, STATUS_LIQ)

        # duplicates are ignored
        await self.watcher.handle_pair(pair_created_log(TOKEN, WBNB, PAIR))
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 2)

    async def test_pair_with_liquidity_in_same_tx(self):
        self.node.receipts["0xtx1"] = {"to": "0xrouter", "logs": [transfer_log(TOKEN, "0x" + "c3" * 20, PAIR, 5, "0xtx1")]}
        await self.watcher.handle_pair(pair_created_log(WBNB, TOKEN, PAIR))
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent[0][2], ["pair", "liq"])

    async def test_unwatched_symbol_is_ignored(self):
        self.node.symbols[TOKEN] = "OTHER"
        await self.watcher.handle_pair(pair_created_log(TOKEN, WBNB, PAIR))
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent, [])

    async def test_mint_detects_launchpad_token(self):
        lg = transfer_log(TOKEN, abi.ZERO_ADDRESS, FOUR_MEME, 10 ** 27, "0xtx2")
        await self.watcher.handle_mint(lg)
        await self.watcher.handle_mint(lg)  # processed once
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)
        _, res, labels, _ = self.notifier.sent[0]
        self.assertEqual((labels, res.status, res.dex, res.name), (["launch"], STATUS_LAUNCH, "four.meme", "Abc Coin"))

    async def test_mint_unknown_launchpad_label(self):
        self.node.receipts["0xtx3"] = {"to": "0x" + "d4" * 20, "logs": []}
        await self.watcher.handle_mint(transfer_log(TOKEN, abi.ZERO_ADDRESS, "0x" + "e5" * 20, 1, "0xtx3"))
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent[0][1].dex, "Nouveau token · via 0xd4d4…d4d4")

    async def test_mint_of_old_token_is_ignored(self):
        self.node.existing_contracts.add(TOKEN)
        await self.watcher.handle_mint(transfer_log(TOKEN, abi.ZERO_ADDRESS, "0x" + "e5" * 20, 1, "0xtx4"))
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent, [])

    async def test_mint_retried_after_transient_rpc_error(self):
        self.watcher.rpc.retries = 0
        real = self.node.handler
        self.node.handler = lambda request: httpx.Response(502)
        lg = transfer_log(TOKEN, abi.ZERO_ADDRESS, FOUR_MEME, 1, "0xtx6")
        await self.watcher.handle_mint(lg)
        self.node.handler = real
        await self.watcher.handle_mint(lg)
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)

    async def test_v4_pons_hook_is_a_launch(self):
        watcher = EvmWatcher(RH, EvmRpc(None, "https://rpc.test"), self.scanner, self.scanner.settings, self.client)
        pool_id = "0x" + "77" * 32
        weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
        hook = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
        self.node.receipts["0xtx5"] = {"logs": [{
            "address": "0x8366a39cc670b4001a1121b8f6a443a643e40951", "topics": [abi.V4_MODIFY_LIQUIDITY, pool_id],
            "data": "0x" + uint_word(0) + uint_word(10) + uint_word(500) + uint_word(0),
        }]}
        await watcher.handle_pair({
            "address": "0x8366a39cc670b4001a1121b8f6a443a643e40951", "transactionHash": "0xtx5",
            "blockNumber": "0x10",
            "topics": [abi.V4_INITIALIZE, pool_id, "0x" + addr_word(weth), "0x" + addr_word(TOKEN)],
            "data": "0x" + uint_word(3000) + uint_word(60) + addr_word(hook) + uint_word(1) + uint_word(0),
        })
        await drain(self.scanner)
        _, res, labels, _ = self.notifier.sent[0]
        self.assertEqual((labels, res.dex, res.pair_address, res.chain),
                         (["launch"], "Pons · Uniswap v4", pool_id, "robinhood"))

    async def test_polling_mode_reads_logs(self):
        self.node.logs.append(pair_created_log(TOKEN, WBNB, PAIR, block=0x101))
        self.node.receipts["0xtx1"] = {"logs": []}
        for name, flt in self.watcher.base_filters().items():
            self.watcher.set_filter(name, flt)
        self.watcher.last_block = 0x100
        self.node.block = 0x105
        await self.watcher._fetch_range(0x101, 0x105)
        _, _, name, lg = self.watcher.queue.get_nowait()
        self.assertEqual(name, "pairs")
        await self.watcher.handle_pair(lg)
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)

    async def test_websocket_mode(self):
        ws = FakeWebSocket()
        watcher = EvmWatcher(BSC, EvmRpc("wss://rpc.test", "https://rpc.test"), self.scanner,
                             self.scanner.settings, self.client, ws_connect=lambda url: ws)
        self.node.receipts["0xtx1"] = {"logs": []}
        task = asyncio.create_task(watcher.run())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if ws.sub_counter >= 2:
                break
        subs = [m["params"][1] for m in ws.sent if m["method"] == "eth_subscribe"]
        self.assertIn({"topics": [abi.TRANSFER, abi.ZERO_TOPIC]}, subs)
        pair_sub = next(s for s in subs if "address" in s)
        self.assertIn(PANCAKE_V2, pair_sub["address"])
        # send a PairCreated notification on the "pairs" subscription
        sub_id = next(f"0xsub{i + 1}" for i, m in enumerate(
            [m for m in ws.sent if m["method"] == "eth_subscribe"]) if "address" in m["params"][1])
        ws.push({"jsonrpc": "2.0", "method": "eth_subscription",
                 "params": {"subscription": sub_id, "result": pair_created_log(TOKEN, WBNB, PAIR)}})
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not self.scanner._notify_queue.empty():
                break
        task.cancel()
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(watcher.last_block, 0x101)


    async def test_refused_subscription_keeps_the_others(self):
        class RefusingSocket(FakeWebSocket):
            async def send(self, raw):
                msg = json.loads(raw)
                if msg.get("method") == "eth_subscribe" and "address" not in msg["params"][1]:
                    self.sent.append(msg)
                    await self.incoming.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                                        "error": {"code": -32602, "message": "address required"}}))
                    return
                await super().send(raw)

        ws = RefusingSocket()
        watcher = EvmWatcher(BSC, EvmRpc("wss://rpc.test", "https://rpc.test"), self.scanner,
                             self.scanner.settings, self.client, ws_connect=lambda url: ws)
        task = asyncio.create_task(watcher.run())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if ws.sub_counter >= 1:
                break
        await asyncio.sleep(0.05)
        self.assertTrue(watcher._ws.connected)
        self.assertEqual(ws.sub_counter, 1)  # "pairs" subscribed, "mints" refused, no reconnect loop
        task.cancel()


if __name__ == "__main__":
    unittest.main()
