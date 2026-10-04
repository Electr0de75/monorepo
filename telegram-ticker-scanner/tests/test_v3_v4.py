"""On-chain detection of liquidity on Uniswap v3-style pools and v4 pools (stealth launches)."""

import unittest

import httpx

from ticker_scanner import evm_abi as abi
from ticker_scanner.chains import CHAINS
from ticker_scanner.config import EvmRpc
from ticker_scanner.evm_watcher import EvmWatcher
from ticker_scanner.models import PAIR, Detection
from tests.helpers import (
    FakeDexScreener, FakeEvmNode, FakeNotifier, Router, addr_word, drain, make_scanner, make_settings, uint_word,
    v4_pool,
)

TOKEN = "0x" + "a1" * 20
OTHER_TOKEN = "0x" + "a9" * 20
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
POOL = "0x" + "c7" * 20
PANCAKE_V3 = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"
UNKNOWN_FACTORY = "0x" + "f2" * 20
ZERO = "0x" + "0" * 40
BSC_MANAGER = "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df"
BSC_PM = CHAINS["bsc"].v4_position_manager
RH_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PONS_HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
SQRT_PRICE = 79228162514264337593543950336  # price 1.0


def v3_mint(pool, tx="0xv3", block=0x300):
    return {"address": pool, "blockNumber": hex(block), "transactionHash": tx,
            "topics": [abi.V3_MINT, "0x" + addr_word("0x" + "e1" * 20), "0x" + uint_word(-600), "0x" + uint_word(600)],
            "data": "0x" + addr_word("0x" + "e1" * 20) + uint_word(10 ** 18) + uint_word(5) + uint_word(7)}


def v4_modify(pool_id, delta=10 ** 18, sender=None, tx="0xv4", block=0x400, manager=BSC_MANAGER):
    return {"address": manager, "blockNumber": hex(block), "transactionHash": tx,
            "topics": [abi.V4_MODIFY_LIQUIDITY, pool_id, "0x" + addr_word(sender or BSC_PM)],
            "data": "0x" + uint_word(-600) + uint_word(600) + uint_word(delta) + "00" * 32}


class Base(unittest.IsolatedAsyncioTestCase):
    chain = "bsc"

    async def asyncSetUp(self):
        self.node = FakeEvmNode()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(
            Router(**{"rpc.test": self.node, "api.dexscreener.com": FakeDexScreener()})))
        self.notifier = FakeNotifier()
        settings = make_settings(evm_rpc={self.chain: EvmRpc(None, "https://rpc.test")})
        self.scanner = make_scanner(self.client, settings, self.notifier)
        self.entry = self.scanner.db.create_entry("P", ["ABC"], ["bsc", "robinhood"])
        self.scanner.reload()
        self.watcher = EvmWatcher(CHAINS[self.chain], settings.evm_rpc[self.chain], self.scanner, settings,
                                  self.client)
        self.watcher.rpc.base_delay = 0
        self.node.symbols[TOKEN] = "ABC"
        self.node.names[TOKEN] = "Abc"
        self.node.symbols[OTHER_TOKEN] = "XYZ"

    async def asyncTearDown(self):
        await self.client.aclose()

    async def sent(self):
        await drain(self.scanner)
        return [(res.pool_kind, res.dex, labels) for _, res, labels, _ in self.notifier.sent]

    def calls(self, selector):
        return sum(1 for m, p in self.node.calls if m == "eth_call" and p[0]["data"].startswith(selector))


class V3Test(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.node.pair_tokens[POOL] = (WBNB, TOKEN, PANCAKE_V3)

    def test_filters_listen_to_all_v3_mints_and_the_v4_manager(self):
        filters = self.watcher.base_filters()
        self.assertEqual(filters["v3mints"], {"topics": [abi.V3_MINT]})
        self.assertEqual(filters["v4liq"]["address"], [BSC_MANAGER])

    async def test_first_liquidity_on_an_old_pool_is_reported(self):
        await self.watcher.handle_v3_mint(v3_mint(POOL))
        self.assertEqual(await self.sent(), [("v3", "PancakeSwap v3", ["liq"])])
        res = self.notifier.sent[0][1]
        self.assertEqual((res.pair_address, res.quote_symbol, res.token_address), (POOL, "WBNB", TOKEN))

    async def test_established_pool_is_ignored_and_inspected_once(self):
        self.node.prev_balances[(TOKEN, POOL)] = 10 ** 20  # the pool already had liquidity
        await self.watcher.handle_v3_mint(v3_mint(POOL))
        await self.watcher.handle_v3_mint(v3_mint(POOL, tx="0xv3b"))
        self.assertEqual(await self.sent(), [])
        self.assertEqual(self.calls(abi.SEL_TOKEN0), 1)

    async def test_pool_created_in_the_same_tx(self):
        self.node.receipts["0xv3"] = {"logs": [{
            "address": PANCAKE_V3, "topics": [abi.V3_POOL_CREATED, "0x" + addr_word(WBNB), "0x" + addr_word(TOKEN),
                                              "0x" + uint_word(2500)],
            "data": "0x" + uint_word(50) + addr_word(POOL)}]}
        await self.watcher.handle_v3_mint(v3_mint(POOL))
        self.assertEqual(await self.sent(), [("v3", "PancakeSwap v3", ["pair", "liq"])])

    async def test_known_pending_pool_gets_its_liquidity(self):
        await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                  pair_address=POOL, pool_kind="v3", dex="PancakeSwap v3"))
        await self.watcher.handle_v3_mint(v3_mint(POOL))
        self.assertEqual([labels for *_, labels in await self.sent()], [["pair"], ["liq"]])

    async def test_unknown_factory_still_detected(self):
        self.node.pair_tokens[POOL] = (TOKEN, WBNB, UNKNOWN_FACTORY)
        await self.watcher.handle_v3_mint(v3_mint(POOL))
        sent = await self.sent()
        self.assertTrue(sent[0][1].startswith("DEX v3 · 0xf2f2"))

    async def test_unwatched_pool_and_non_pools(self):
        self.node.pair_tokens[POOL] = (WBNB, OTHER_TOKEN, PANCAKE_V3)
        await self.watcher.handle_v3_mint(v3_mint(POOL))
        await self.watcher.handle_v3_mint(v3_mint("0x" + "d8" * 20))  # token0() reverts: not a pool
        await self.watcher.handle_v3_mint({"address": POOL, "topics": [abi.V3_MINT]})  # malformed
        self.assertEqual(await self.sent(), [])

    async def test_rpc_failure_is_retried_on_next_event(self):
        real = self.node.handler
        self.watcher.rpc.retries = 0
        self.node.handler = lambda request: httpx.Response(503)
        await self.watcher.handle_v3_mint(v3_mint(POOL))
        self.node.handler = real
        await self.watcher.handle_v3_mint(v3_mint(POOL, tx="0xv3c"))
        self.assertEqual(len(await self.sent()), 1)


class V4Test(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pool_id, raw = v4_pool(ZERO, TOKEN)  # native BNB / ABC
        self.node.pool_keys[(BSC_PM, self.pool_id[2:52])] = raw
        self.slot = abi.v4_state_slot(self.pool_id)
        self.node.storage[(BSC_MANAGER, self.slot)] = SQRT_PRICE
        self.node.prev_storage[(BSC_MANAGER, self.slot)] = SQRT_PRICE  # initialized long ago...
        self.node.prev_storage[(BSC_MANAGER, self.slot + 3)] = 0      # ...but never had liquidity

    async def test_first_liquidity_on_an_old_v4_pool(self):
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id))
        self.assertEqual(await self.sent(), [("v4", "Uniswap v4", ["liq"])])
        res = self.notifier.sent[0][1]
        self.assertEqual((res.pair_address, res.quote_symbol), (self.pool_id, "BNB"))

    async def test_established_v4_pool_is_ignored(self):
        self.node.prev_storage[(BSC_MANAGER, self.slot + 3)] = 10 ** 18
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id))
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id, tx="0xv4b"))
        self.assertEqual(await self.sent(), [])
        self.assertEqual(self.calls(abi.SEL_POOL_KEYS), 1)

    async def test_pool_initialized_in_the_same_tx(self):
        self.node.prev_storage[(BSC_MANAGER, self.slot)] = 0
        self.node.receipts["0xv4"] = {"logs": [{"address": BSC_MANAGER, "topics": [
            abi.V4_INITIALIZE, self.pool_id, "0x" + addr_word(ZERO), "0x" + addr_word(TOKEN)], "data": "0x"}]}
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id))
        self.assertEqual(await self.sent(), [("v4", "Uniswap v4", ["pair", "liq"])])

    async def test_removals_are_ignored(self):
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id, delta=-5))
        self.assertEqual(await self.sent(), [])

    async def test_pool_key_from_initialize_cache(self):
        del self.node.pool_keys[(BSC_PM, self.pool_id[2:52])]
        await self.watcher.handle_pair({  # an Initialize seen earlier (token not watched at that time)
            "address": BSC_MANAGER, "blockNumber": "0x10", "transactionHash": "0xinit",
            "topics": [abi.V4_INITIALIZE, self.pool_id, "0x" + addr_word(ZERO), "0x" + addr_word(TOKEN)],
            "data": "0x" + uint_word(3000) + uint_word(60) + addr_word(ZERO) + uint_word(SQRT_PRICE) + uint_word(0)})
        await drain(self.scanner)
        self.notifier.sent.clear()
        self.scanner.db.conn.execute("DELETE FROM results")
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id))
        self.assertEqual(len(await self.sent()), 1)
        self.assertEqual(self.calls(abi.SEL_POOL_KEYS), 0)

    async def test_spoofed_pool_key_is_rejected(self):
        del self.node.pool_keys[(BSC_PM, self.pool_id[2:52])]
        fake_locker = "0x" + "66" * 20
        # claims the pool is BNB/ABC but with other parameters: does not hash to this pool id
        _, wrong_raw = v4_pool(ZERO, TOKEN, fee=500)
        self.node.pool_keys[(fake_locker, self.pool_id[2:52])] = wrong_raw
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id, sender=fake_locker))
        self.assertEqual(await self.sent(), [])

    async def test_inconsistent_storage_layout_never_guesses(self):
        self.node.storage[(BSC_MANAGER, self.slot)] = 0  # we read nothing for a live pool
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id))
        self.assertEqual(await self.sent(), [])
        self.assertTrue(self.watcher._v4_layout_warned)

    async def test_known_pending_v4_pool(self):
        await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                  pair_address=self.pool_id, pool_kind="v4", dex="Uniswap v4"))
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id))
        self.assertEqual([labels for *_, labels in await self.sent()], [["pair"], ["liq"]])

    async def test_foreign_manager_is_ignored(self):
        await self.watcher.handle_v4_liquidity(v4_modify(self.pool_id, manager="0x" + "99" * 20))
        self.assertEqual(await self.sent(), [])


class V4LaunchpadHookTest(Base):
    chain = "robinhood"

    async def test_first_liquidity_on_a_pons_pool_is_a_launch(self):
        rh_pm = CHAINS["robinhood"].v4_position_manager
        weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
        pool_id, raw = v4_pool(weth, TOKEN, hooks=PONS_HOOK) if weth < TOKEN else v4_pool(TOKEN, weth, hooks=PONS_HOOK)
        self.node.pool_keys[(rh_pm, pool_id[2:52])] = raw
        slot = abi.v4_state_slot(pool_id)
        self.node.storage[(RH_MANAGER, slot)] = SQRT_PRICE
        await self.watcher.handle_v4_liquidity(v4_modify(pool_id, sender=rh_pm, manager=RH_MANAGER))
        sent = await self.sent()
        self.assertEqual(sent, [("v4", "Pons · Uniswap v4", ["launch"])])


if __name__ == "__main__":
    unittest.main()
