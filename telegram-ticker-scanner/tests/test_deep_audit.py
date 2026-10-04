"""Tests for the second (deep) audit: security, stealth launches, anti-flood."""

import asyncio
import time
import unittest

import httpx

from ticker_scanner.models import LAUNCH, PAIR, Detection, valid_address
from ticker_scanner.scanner import FloodGuard
from tests.helpers import FakeDexScreener, FakeNotifier, Router, drain, make_scanner, make_settings

TOKEN = "0x" + "a1" * 20
OLD_TOKEN = "0x" + "a2" * 20
POOL = "0x" + "b2" * 20
OLD_POOL = "0x" + "b3" * 20
SOL_MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


def ds_pair(token, pool, created_s, liq=5000.0, symbol="ABC", chain="bsc", dex="pancakeswap"):
    return {
        "chainId": chain, "dexId": dex, "labels": ["v2"], "pairAddress": pool,
        "baseToken": {"address": token, "symbol": symbol, "name": "Abc"},
        "quoteToken": {"address": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "symbol": "WBNB"},
        "liquidity": {"usd": liq}, "marketCap": 100000, "pairCreatedAt": int(created_s * 1000),
    }


class SetupMixin:
    async def asyncSetUp(self):
        self.ds = FakeDexScreener()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(Router(**{"api.dexscreener.com": self.ds})))
        self.notifier = FakeNotifier()
        self.scanner = make_scanner(self.client, make_settings(notify_flood_limit=3), self.notifier)
        self.entry = self.scanner.db.create_entry("Proj, test", ["ABC"], ["bsc", "solana"])
        self.scanner.reload()

    async def asyncTearDown(self):
        await self.client.aclose()


class ValidationTest(SetupMixin, unittest.IsolatedAsyncioTestCase):
    async def test_hostile_addresses_are_rejected(self):
        hostile = [
            '0x" onclick="x', "<script>alert(1)</script>", "0x" + "g" * 40, "", "javascript:alert(1)",
            "0x" + "a" * 41, "../../etc/passwd",
        ]
        for addr in hostile:
            await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=addr, symbol="ABC",
                                                      pair_address=POOL, dex="x"))
            await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                      pair_address=addr, dex="x"))
        await self.scanner.on_detection(Detection(chain="solana", kind=LAUNCH, token_address="Mint<b>",
                                                  symbol="ABC", dex="x"))
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self.scanner.db.results_for_entry(self.entry.id), [])

    async def test_invalid_curve_address_does_not_drop_a_launch(self):
        await self.scanner.on_detection(Detection(chain="solana", kind=LAUNCH, token_address=SOL_MINT,
                                                  symbol="ABC", pair_address="not a curve", dex="pump.fun"))
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIsNone(self.notifier.sent[0][1].pair_address)

    def test_valid_address(self):
        self.assertTrue(valid_address("evm", TOKEN))
        self.assertTrue(valid_address("evm", "0x" + "c" * 64, pool=True))
        self.assertFalse(valid_address("evm", "0x" + "c" * 64))
        self.assertTrue(valid_address("solana", SOL_MINT))
        self.assertFalse(valid_address("solana", SOL_MINT + "0"))  # 0 is not base58


class StealthLaunchTest(SetupMixin, unittest.IsolatedAsyncioTestCase):
    async def test_pre_created_pair_getting_liquidity_is_reported(self):
        long_ago = time.time() - 5 * 86400
        # 1st pass: an old unrelated ABC token exists -> snapshot, no alert
        self.ds.search_results["ABC"] = [ds_pair(OLD_TOKEN, OLD_POOL, long_ago)]
        await self.scanner.search_ticker("ABC", {"bsc"})
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent, [])
        self.assertIs(self.scanner.db.baseline_state(self.entry.id, "ABC"), True)
        # the team's pair was created days ago, liquidity lands now -> appears on DexScreener
        self.ds.search_results["ABC"].append(ds_pair(TOKEN, POOL, long_ago - 3600))
        await self.scanner.search_ticker("ABC", {"bsc"})
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0][1].pair_address, POOL)

    async def test_truncated_snapshot_does_not_spam_old_pairs(self):
        long_ago = time.time() - 5 * 86400
        crowd = [ds_pair("0x%040x" % i, "0x%040x" % (1000 + i), long_ago) for i in range(30)]
        self.ds.search_results["ABC"] = crowd
        await self.scanner.search_ticker("ABC", {"bsc"})
        self.assertIs(self.scanner.db.baseline_state(self.entry.id, "ABC"), False)
        self.ds.search_results["ABC"] = [ds_pair(TOKEN, POOL, long_ago)]  # resurfacing old pair
        await self.scanner.search_ticker("ABC", {"bsc"})
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent, [])

    async def test_ticker_edit_resets_snapshot(self):
        self.ds.search_results["ABC"] = []
        await self.scanner.search_ticker("ABC", {"bsc"})
        self.assertIsNotNone(self.scanner.db.baseline_state(self.entry.id, "ABC"))
        self.scanner.db.update_entry(self.entry.id, tickers=["ABC", "XYZ"])
        self.assertIsNone(self.scanner.db.baseline_state(self.entry.id, "ABC"))


class FloodGuardTest(SetupMixin, unittest.IsolatedAsyncioTestCase):
    def test_guard(self):
        guard = FloodGuard(limit=2, window=10)
        self.assertEqual([guard.allow(1, t) for t in (0, 1, 2, 3)], [True, True, False, False])
        self.assertTrue(guard.allow(2, 3))  # other entries unaffected
        self.assertEqual(guard.due_summaries(5), [])
        self.assertEqual(guard.due_summaries(11), [(1, 2)])
        self.assertTrue(guard.allow(1, 12))
        self.assertTrue(FloodGuard(limit=0).allow(1, 0))

    async def test_burst_of_copies_is_grouped(self):
        for i in range(8):
            await self.scanner.on_detection(Detection(chain="bsc", kind=LAUNCH, token_address="0x%040x" % (i + 1),
                                                      symbol="ABC", dex="Flap"))
        # a copy with real liquidity always goes through
        await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                  pair_address=POOL, dex="x", has_liquidity=True,
                                                  liquidity_usd=50_000))
        await drain(self.scanner)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(len(self.notifier.sent), 4)  # 3 allowed + the liquid one
        self.assertEqual(len(self.notifier.notices), 1)
        self.assertIn("Proj, test", self.notifier.notices[0][1])  # name not mangled
        self.assertEqual(len(self.scanner.db.results_for_entry(self.entry.id)), 9)  # nothing lost
        self.scanner.flood.window = 0
        await self.scanner.flush_flood_summaries()
        self.assertIn("5 alerte(s) regroupée(s)", self.notifier.notices[-1][1])


if __name__ == "__main__":
    unittest.main()


import json  # noqa: E402

from ticker_scanner import evm_abi as abi  # noqa: E402
from ticker_scanner.chains import CHAINS  # noqa: E402
from ticker_scanner.config import EvmRpc  # noqa: E402
from ticker_scanner.evm_watcher import BoundedSet, EvmWatcher  # noqa: E402
from tests.helpers import FakeEvmNode, addr_word, uint_word  # noqa: E402

WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
LP = "0x" + "c4" * 20
UNKNOWN_FACTORY = "0x" + "f1" * 20


def burn_log(pair, tx="0xtxfl"):
    return {"address": pair, "blockNumber": "0x200", "transactionHash": tx,
            "topics": [abi.TRANSFER, abi.ZERO_TOPIC, abi.ZERO_TOPIC], "data": "0x" + uint_word(abi.MINIMUM_LIQUIDITY)}


class EvmDeepTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.node = FakeEvmNode()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(
            Router(**{"rpc.test": self.node, "api.dexscreener.com": FakeDexScreener()})))
        self.notifier = FakeNotifier()
        settings = make_settings(evm_rpc={"bsc": EvmRpc(None, "https://rpc.test")})
        self.scanner = make_scanner(self.client, settings, self.notifier)
        self.entry = self.scanner.db.create_entry("P", ["ABC"], ["bsc"])
        self.scanner.reload()
        self.watcher = EvmWatcher(CHAINS["bsc"], settings.evm_rpc["bsc"], self.scanner, settings, self.client)
        self.watcher.rpc.base_delay = 0
        self.node.symbols[TOKEN] = "ABC"
        self.node.pair_tokens[LP] = (WBNB, TOKEN, UNKNOWN_FACTORY)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_stealth_first_liquidity_on_old_pair(self):
        self.node.receipts["0xtxfl"] = {"logs": []}
        await self.watcher.handle_mint(burn_log(LP))
        await self.watcher.handle_mint(burn_log(LP))  # handled once
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)
        _, res, labels, _ = self.notifier.sent[0]
        self.assertEqual(labels, ["liq"])  # the pair existed before: only "liquidity added"
        self.assertEqual((res.pair_address, res.quote_symbol, res.pool_kind), (LP, "WBNB", "v2"))
        self.assertTrue(res.dex.startswith("DEX v2 · 0xf1f1"))

    async def test_first_liquidity_in_creation_tx_has_both_labels(self):
        self.node.pair_tokens[LP] = (WBNB, TOKEN, "0xca143ce32fe78f1f7019d7d551a6402fc5350c73")
        self.node.receipts["0xtxfl"] = {"logs": [{
            "address": "0xca143ce32fe78f1f7019d7d551a6402fc5350c73",
            "topics": [abi.V2_PAIR_CREATED, "0x" + addr_word(WBNB), "0x" + addr_word(TOKEN)],
            "data": "0x" + addr_word(LP) + uint_word(1)}]}
        await self.watcher.handle_mint(burn_log(LP))
        await drain(self.scanner)
        _, res, labels, _ = self.notifier.sent[0]
        self.assertEqual((labels, res.dex), (["pair", "liq"], "PancakeSwap v2"))

    async def test_first_liquidity_on_known_pending_pair(self):
        await self.scanner.on_detection(Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                  pair_address=LP, pool_kind="v2", dex="x"))
        await self.watcher.handle_mint(burn_log(LP))
        await drain(self.scanner)
        self.assertEqual([s[2] for s in self.notifier.sent], [["pair"], ["liq"]])

    async def test_burn_on_a_non_pair_token_is_ignored(self):
        await self.watcher.handle_mint(burn_log("0x" + "d5" * 20))
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent, [])

    async def test_getlogs_range_is_learned_then_grows_back(self):
        real = self.node.handler
        limit = {"value": 10}

        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "eth_getLogs":
                flt = body["params"][0]
                if int(flt["toBlock"], 16) - int(flt["fromBlock"], 16) + 1 > limit["value"]:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
                        "code": -32600, "message": "Under the Free tier plan, you can make eth_getLogs requests "
                                                   "with up to a 10 block range"}})
            return real(request)

        self.node.handler = handler
        self.watcher.filters = {"mints": {"topics": [abi.TRANSFER]}}
        await self.watcher._fetch_range(1, 2000)
        self.assertEqual(self.watcher.max_range, 10)  # read from the provider's message
        calls_after_learning = len(self.node.calls)
        await self.watcher._fetch_range(2001, 2100)
        self.assertEqual(len(self.node.calls) - calls_after_learning, 10)
        for _ in range(60):  # an announced limit is never probed again
            await self.watcher._fetch_range(1, 100)
        self.assertEqual(self.watcher.max_range, 10)

    async def test_guessed_range_grows_back(self):
        real = self.node.handler
        state = {"limit": 100}

        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "eth_getLogs":
                flt = body["params"][0]
                if int(flt["toBlock"], 16) - int(flt["fromBlock"], 16) + 1 > state["limit"]:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
                        "code": -32005, "message": "query returned more than 10000 results"}})
            return real(request)

        self.node.handler = handler
        self.watcher.filters = {"mints": {"topics": [abi.TRANSFER]}}
        await self.watcher._fetch_range(1, 1000)
        self.assertLessEqual(self.watcher.max_range, 100)  # learned by halving
        learned = self.watcher.max_range
        state["limit"] = 100_000  # the busy period is over
        for _ in range(60):
            await self.watcher._fetch_range(1, learned)
        self.assertGreater(self.watcher.max_range, learned)

    def test_range_hints(self):
        from ticker_scanner.evm_watcher import range_hint
        self.assertEqual(range_hint("Under the Free tier plan, you can make eth_getLogs requests with up to "
                                    "a 10 block range."), 10)
        self.assertEqual(range_hint("eth_getLogs is limited to a 10,000 block range"), 10000)
        self.assertEqual(range_hint("maximum 2000 blocks distance"), 2000)
        self.assertIsNone(range_hint("query returned more than 10000 results"))

    def test_backfill_window_follows_block_time(self):
        def blocks(key):
            return EvmWatcher(CHAINS[key], EvmRpc(None, "https://x"), self.scanner, self.scanner.settings,
                              self.client).backfill_blocks
        self.assertEqual(blocks("robinhood"), 6000)   # 10 min at 100 ms
        self.assertEqual(blocks("bsc"), 800)
        self.assertEqual(blocks("ethereum"), 100)

    def test_bounded_set(self):
        seen = BoundedSet(4)
        for k in "abcde":
            seen.add(k)
        self.assertNotIn("a", seen)
        self.assertIn("e", seen)
        self.assertLessEqual(len(seen), 4)

    async def test_rpc_calls_after_a_match_run_in_parallel(self):
        real = self.node.handler

        async def slow(request):
            body = json.loads(request.content)
            if body["method"] in ("eth_getCode", "eth_getTransactionReceipt") or \
                    (body["method"] == "eth_call" and body["params"][0]["data"] == abi.SEL_NAME):
                await asyncio.sleep(0.2)
            return real(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        watcher = EvmWatcher(CHAINS["bsc"], EvmRpc(None, "https://rpc.test"), self.scanner,
                             self.scanner.settings, client)
        started = time.monotonic()
        await watcher.handle_mint({"address": TOKEN, "blockNumber": "0x10", "transactionHash": "0xp",
                                   "topics": [abi.TRANSFER, abi.ZERO_TOPIC, "0x" + addr_word("0x" + "e" * 40)],
                                   "data": "0x" + uint_word(10 ** 18)})
        elapsed = time.monotonic() - started
        await client.aclose()
        self.assertLess(elapsed, 0.4)  # 3 slow calls in parallel, not 0.6 s in sequence
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)


import logging  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

from ticker_scanner.bot import BotUI  # noqa: E402
from ticker_scanner.checks import check_env_permissions  # noqa: E402
from ticker_scanner.health import SourceStats, register_secrets  # noqa: E402
from ticker_scanner.telegram_api import TelegramAPI  # noqa: E402
from tests.helpers import FakeTelegram  # noqa: E402


def tg_msg(text, user=42, update_id=1):
    return {"update_id": update_id, "message": {"from": {"id": user}, "chat": {"id": 42}, "text": text}}


def tg_cb(data, message_id=500, update_id=2):
    return {"update_id": update_id, "callback_query": {"id": "q", "from": {"id": 42}, "data": "sc:" + data,
                                                       "message": {"message_id": message_id, "chat": {"id": 42}}}}


class BotDeepTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tg = FakeTelegram()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(
            Router(**{"api.telegram.org": self.tg, "api.dexscreener.com": FakeDexScreener()})))
        self.api = TelegramAPI("1:T", self.client)
        self.settings = make_settings()
        self.scanner = make_scanner(self.client, self.settings, None)
        self.bot = BotUI(self.api, self.scanner.db, self.scanner, self.settings, bot_username="ScanBot")
        self.entry = self.scanner.db.create_entry("P", ["ABC"], ["bsc"])
        self.scanner.reload()

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_pause_is_idempotent(self):
        await self.bot.handle_update(tg_cb(f"p:{self.entry.id}:1"))
        await self.bot.handle_update(tg_cb(f"p:{self.entry.id}:1"))  # replayed click
        self.assertTrue(self.scanner.db.get_entry(self.entry.id).paused)
        await self.bot.handle_update(tg_cb(f"p:{self.entry.id}:0"))
        await self.bot.handle_update(tg_cb(f"p:{self.entry.id}:0"))
        self.assertFalse(self.scanner.db.get_entry(self.entry.id).paused)

    async def test_stale_picker_is_refused(self):
        await self.bot.handle_update(tg_cb(f"ec:{self.entry.id}", message_id=500))  # picker shown in msg 500
        await self.bot.handle_update(tg_cb("ct:solana", message_id=499))          # old picker message
        self.assertIn("plus actif", self.tg.of("answerCallbackQuery")[-1]["text"])
        await self.bot.handle_update(tg_cb("cv", message_id=500))
        self.assertEqual(self.scanner.db.get_entry(self.entry.id).chains, ["bsc"])

    async def test_group_commands_for_other_bots_are_ignored(self):
        self.assertFalse(await self.bot.handle_update(tg_msg("/scanner@OtherBot")))
        self.assertTrue(await self.bot.handle_update(tg_msg("/scanner@scanbot")))
        self.assertTrue(await self.bot.handle_update(tg_msg("/scanner")))

    async def test_refresh_cooldown(self):
        await self.bot.handle_update(tg_cb(f"rf:{self.entry.id}:0"))
        await self.bot.handle_update(tg_cb(f"rf:{self.entry.id}:0"))
        self.assertIn("Patiente", self.tg.of("answerCallbackQuery")[-1]["text"])

    async def test_offset_is_persisted_across_restarts(self):
        tg = self.tg

        async def long_polling(request):
            if request.url.path.endswith("getUpdates") and not tg.updates:
                await asyncio.sleep(0.02)  # like Telegram's long polling
            return tg.handler(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(long_polling)) as client:
            bot = BotUI(TelegramAPI("1:T", client), self.scanner.db, self.scanner, self.settings)
            tg.updates = [tg_msg("/scanner", update_id=77)]
            poll = asyncio.create_task(bot.run_polling())
            for _ in range(100):
                await asyncio.sleep(0.01)
                if self.scanner.db.get_meta("telegram_offset"):
                    break
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
            # after a restart the next getUpdates starts after update 77
            tg.sent.clear()
            poll = asyncio.create_task(bot.run_polling())
            await asyncio.sleep(0.05)
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
        self.assertEqual(self.scanner.db.get_meta("telegram_offset"), "78")
        self.assertEqual(tg.of("getUpdates")[0]["offset"], 78)

    async def test_malformed_updates_never_raise(self):
        for update in ({}, {"message": "x"}, {"callback_query": []}, {"callback_query": {"data": "sc:p"}},
                       {"callback_query": {"id": "q", "data": 5}}, {"message": {"text": "/scanner"}},
                       {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/"}}):
            self.assertIn(await self.bot.handle_update(update), (True, False))

    async def test_long_or_hostile_project_name(self):
        await self.bot.handle_update(tg_msg("/nouveau"))
        await self.bot.handle_update(tg_msg("Evil‮<b>name</b>"))
        await self.bot.handle_update(tg_msg("ABC"))
        self.assertIn("Evil&lt;b&gt;name&lt;/b&gt;", self.tg.of("sendMessage")[-1]["text"])


class SecurityOpsTest(unittest.IsolatedAsyncioTestCase):
    def test_status_errors_are_redacted(self):
        register_secrets(["supersecretapikey123456"])
        st = SourceStats("evm:bsc")
        st.error("InvalidURI: wss://bnb-mainnet.g.alchemy.com/v2/supersecretapikey123456 isn't valid")
        self.assertNotIn("supersecretapikey123456", st.last_error)
        register_secrets([])

    def test_env_permissions(self):
        if os.name != "posix":
            self.skipTest("POSIX only")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as fh:
                fh.write("TELEGRAM_BOT_TOKEN=1:x\n")
            os.chmod(path, 0o644)
            with self.assertLogs("ticker_scanner.checks", logging.WARNING):
                self.assertIn("chmod 600", check_env_permissions(path))
            os.chmod(path, 0o600)
            self.assertIsNone(check_env_permissions(path))

    async def test_embedded_mode_never_raises_into_the_host(self):
        from ticker_scanner.embed import ScannerApp

        class Boom:
            async def handle_update(self, update):
                raise RuntimeError("bug")

        app = ScannerApp(scanner=None, bot=Boom(), task=None, client=None, db=None)
        with self.assertLogs("ticker_scanner.embed", logging.ERROR):
            self.assertFalse(await app.handle_update({"update_id": 1}))

    def test_links_are_attribute_safe(self):
        from ticker_scanner.formatting import results_page
        db = make_scanner.__globals__["Database"](":memory:")
        entry = db.create_entry("P", ["ABC"], ["bsc"])
        res = db.insert_result(entry.id, Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC",
                                                   pair_address=POOL, dex="x"), "liq")
        text, _ = results_page(entry, [res], 0, 5, "UTC", None)
        self.assertNotIn('"', text.split("href=")[1].split(">")[0][1:-1])


class EnvFileTest(unittest.TestCase):
    def test_env_file_is_found_in_the_working_directory(self):
        from ticker_scanner import config
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".env"), "w") as fh:
                fh.write("TELEGRAM_BOT_TOKEN=999:fromcwd\n")
            try:
                os.chdir(d)
                self.assertEqual(os.path.realpath(config.env_file_path()), os.path.realpath(os.path.join(d, ".env")))
                saved = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                try:
                    config.load_env_file()
                    self.assertEqual(os.environ.get("TELEGRAM_BOT_TOKEN"), "999:fromcwd")
                finally:
                    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                    if saved is not None:
                        os.environ["TELEGRAM_BOT_TOKEN"] = saved
            finally:
                os.chdir(old)
