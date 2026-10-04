"""Tests for the robustness fixes (audit)."""

import asyncio
import json
import logging
import time
import unittest
from unittest.mock import patch

import httpx

from ticker_scanner import evm_abi as abi
from ticker_scanner.bot import BotUI, TelegramNotifier
from ticker_scanner.chains import CHAINS
from ticker_scanner.checks import RedactingFormatter, check_rpcs, secrets_of
from ticker_scanner.config import EvmRpc, load_settings
from ticker_scanner.db import SCHEMA_VERSION, Database
from ticker_scanner.dexscreener import DexScreener, metrics, pair_to_detection
from ticker_scanner.evm_watcher import EvmWatcher, pending_interval
from ticker_scanner.models import PAIR, STATUS_PAIR, Detection, clean_text
from ticker_scanner.rpc import NETWORK, NODE, RATE_LIMIT, REVERT, HttpRpc, RpcError, WsSubscriptions, classify_error
from ticker_scanner.solana import PumpPortalWatcher, SolanaLaunchpadWatcher, new_mints
from ticker_scanner.telegram_api import TelegramAPI, TelegramError
from tests.helpers import (
    FakeDexScreener, FakeEvmNode, FakeNotifier, FakeTelegram, FakeWebSocket, Router, addr_word, drain,
    make_scanner, make_settings, uint_word,
)

BSC = CHAINS["bsc"]
TOKEN = "0x" + "a1" * 20
PAIR_ADDR = "0x" + "b2" * 20
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"


def rpc_reply(body_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": body_id}
    payload["error" if error else "result"] = error or result
    return httpx.Response(200, json=payload)


class SequenceTransport:
    """httpx handler returning the queued responses in order (then repeating the last)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls += 1
        item = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return rpc_reply(body["id"], **item)


class RpcTest(unittest.IsolatedAsyncioTestCase):
    def test_classify(self):
        self.assertEqual(classify_error({"code": 3, "message": "execution reverted"}), REVERT)
        self.assertEqual(classify_error({"code": -32000, "message": "execution reverted: nope"}), REVERT)
        self.assertEqual(classify_error({"code": -32005, "message": "rate limit exceeded"}), RATE_LIMIT)
        self.assertEqual(classify_error({"code": 429, "message": "Your app has exceeded its compute units"}),
                         RATE_LIMIT)
        self.assertEqual(classify_error({"code": -32602, "message": "block range is too wide"}), NODE)

    async def call(self, *responses, retries=3):
        transport = SequenceTransport(*responses)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            rpc = HttpRpc("https://rpc.test", client, retries=retries, base_delay=0)
            try:
                return await rpc.call("eth_call", []), transport.calls
            except RpcError as exc:
                return exc, transport.calls

    async def test_retries(self):
        res, calls = await self.call(httpx.Response(502), httpx.Response(429), {"result": "0x1"})
        self.assertEqual((res, calls), ("0x1", 3))
        res, calls = await self.call(httpx.ConnectError("boom"), {"result": "0x2"})
        self.assertEqual((res, calls), ("0x2", 2))
        res, calls = await self.call({"error": {"code": 3, "message": "execution reverted"}})
        self.assertEqual((res.kind, calls), (REVERT, 1))  # a revert is an answer: no retry
        res, calls = await self.call({"error": {"code": -32000, "message": "header not found"}}, {"result": "0x3"})
        self.assertEqual((res, calls), ("0x3", 2))  # node error: retried once
        res, calls = await self.call({"error": {"code": -32602, "message": "range too wide"}})
        self.assertEqual((res.kind, calls), (NODE, 2))
        res, calls = await self.call(httpx.Response(200, text="<html>cloudflare</html>"), retries=1)
        self.assertEqual((res.kind, calls), (NETWORK, 2))


class WsTest(unittest.IsolatedAsyncioTestCase):
    async def test_silent_connection_is_reopened(self):
        sockets = []

        def connect(url):
            sockets.append(FakeWebSocket())
            return sockets[-1]

        ws = WsSubscriptions("wss://x", subscribe_method="eth_subscribe", unsubscribe_method="eth_unsubscribe",
                             on_message=lambda n, r: None, connect=connect, stall_timeout=0.05)
        ws.reconnect_delay = 0.01
        ws.set("a", ["logs", {}])
        task = asyncio.create_task(ws.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ws.connections >= 2:
                break
        task.cancel()
        self.assertGreaterEqual(ws.connections, 2)
        # it re-subscribed on the new connection
        self.assertTrue(any(m.get("method") == "eth_subscribe" for m in sockets[1].sent))

    async def test_pending_request_released_when_socket_dies(self):
        class DeadSocket(FakeWebSocket):
            async def send(self, raw):
                self.sent.append(json.loads(raw))
                self.close()  # dies before answering

        ws = WsSubscriptions("wss://x", subscribe_method="eth_subscribe", unsubscribe_method="eth_unsubscribe",
                             on_message=lambda n, r: None, connect=lambda url: DeadSocket())
        ws.set("a", ["logs", {}])
        started = time.monotonic()
        with self.assertRaises(ConnectionError):
            async with DeadSocket() as sock:
                await ws._session(sock)
        self.assertLess(time.monotonic() - started, 5)  # did not wait for the 30 s timeout


class CancellationTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_racing_with_result_is_not_swallowed(self):
        from ticker_scanner.rpc import wait_result
        for _ in range(20):
            fut = asyncio.get_running_loop().create_future()

            async def caller():
                return await wait_result(fut, 5)

            task = asyncio.create_task(caller())
            await asyncio.sleep(0)
            fut.set_exception(ConnectionError("socket closed"))  # result and cancel at the same time
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_paused_project_really_stops_its_watcher(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                Router(**{"rpc.test": FakeEvmNode(), "api.dexscreener.com": FakeDexScreener()}))) as client:
            settings = make_settings(evm_rpc={"bsc": EvmRpc("wss://rpc.test", "https://rpc.test")},
                                     pumpportal_enabled=False)
            scanner = make_scanner(client, settings, FakeNotifier())
            scanner.ws_connect = lambda url: FakeWebSocket()
            entry = scanner.db.create_entry("P", ["ABC"], ["bsc"])
            scanner.reload()
            scanner.sync_sources()
            task = scanner._sources["evm:bsc"]
            await asyncio.sleep(0.05)
            scanner.db.update_entry(entry.id, paused=True)
            scanner.reload()
            scanner.sync_sources()
            await asyncio.wait({task}, timeout=2)
            self.assertTrue(task.done())
            self.assertEqual(scanner.source_names(), [])


class EvmHardeningTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.node = FakeEvmNode()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(
            Router(**{"rpc.test": self.node, "api.dexscreener.com": FakeDexScreener()})))
        self.notifier = FakeNotifier()
        settings = make_settings(evm_rpc={"bsc": EvmRpc(None, "https://rpc.test")}, evm_getlogs_max_range=100)
        self.scanner = make_scanner(self.client, settings, self.notifier)
        self.scanner.db.create_entry("P", ["ABC"], ["bsc"])
        self.scanner.reload()
        self.watcher = EvmWatcher(BSC, settings.evm_rpc["bsc"], self.scanner, settings, self.client)
        self.watcher.rpc.base_delay = 0
        self.node.symbols[TOKEN] = "ABC"

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_getlogs_range_is_split_when_refused(self):
        real = self.node.handler
        for b in (10, 55, 99):
            self.node.logs.append({"address": "0x" + "c3" * 20, "blockNumber": hex(b), "transactionHash": "0x1",
                                   "topics": [abi.TRANSFER, abi.ZERO_TOPIC, "0x" + addr_word(WBNB)], "data": "0x"})

        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "eth_getLogs":
                flt = body["params"][0]
                if int(flt["toBlock"], 16) - int(flt["fromBlock"], 16) > 20:
                    return rpc_reply(body["id"], error={"code": -32602, "message": "block range too wide"})
            return real(request)

        self.node.handler = handler
        logs = await self.watcher._get_logs({"topics": [abi.TRANSFER]}, 1, 100)
        self.assertEqual(sorted(int(lg["blockNumber"], 16) for lg in logs), [10, 55, 99])

    async def test_getlogs_network_error_is_raised_not_skipped(self):
        self.watcher.rpc.retries = 0
        self.node.handler = lambda request: httpx.Response(503)
        with self.assertRaises(RpcError):
            await self.watcher._get_logs({"topics": [abi.TRANSFER]}, 1, 100)

    async def test_symbol_waiters_never_hang(self):
        started = asyncio.Event()

        async def broken_call(method, params=None):
            started.set()
            await asyncio.sleep(0.01)
            raise RuntimeError("unexpected")

        self.watcher.rpc.call = broken_call
        first = asyncio.create_task(self.watcher.token_symbol(TOKEN))
        await started.wait()
        second = asyncio.create_task(self.watcher.token_symbol(TOKEN))  # waits on the in-flight future
        with self.assertRaises(RuntimeError):
            await first
        self.assertEqual(await asyncio.wait_for(second, 1), "")

    async def test_rate_limited_symbol_is_not_cached_as_non_token(self):
        real = self.node.handler
        self.node.handler = lambda request: rpc_reply(json.loads(request.content)["id"],
                                                      error={"code": -32005, "message": "rate limit exceeded"})
        self.watcher.rpc.retries = 0
        lg = {"address": TOKEN, "blockNumber": "0x101", "transactionHash": "0x9",
              "topics": [abi.TRANSFER, abi.ZERO_TOPIC, "0x" + addr_word(WBNB)], "data": "0x" + uint_word(1)}
        await self.watcher.handle_mint(lg)
        self.assertNotIn(TOKEN, self.watcher.symbols)
        self.node.handler = real
        await self.watcher.handle_mint(lg)
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)

    async def test_backfill_uses_block_before_reconnect(self):
        self.watcher.last_block = 100
        fetched = []

        async def fake_fetch(start, end):
            fetched.append((start, end))

        real_call = self.watcher.rpc.call

        async def call(method, params=None):
            if method == "eth_blockNumber":
                self.watcher.last_block = 150  # live logs arrived meanwhile
                return hex(160)
            return await real_call(method, params)

        self.watcher.rpc.call = call
        self.watcher._fetch_range = fake_fetch
        await self.watcher._backfill()
        self.assertEqual(fetched, [(101, 160)])

    async def test_pairs_are_processed_before_mints(self):
        self.watcher._enqueue("mints", {"blockNumber": "0x1"}, track=False)
        self.watcher._enqueue("mints", {"blockNumber": "0x2"}, track=False)
        self.watcher._enqueue("pairs", {"blockNumber": "0x3"}, track=False)
        self.assertEqual(self.watcher.queue.get_nowait()[2], "pairs")

    def test_pending_schedule(self):
        self.assertEqual(pending_interval(60), 6.0)
        self.assertEqual(pending_interval(1800), 30.0)
        self.assertEqual(pending_interval(86400), 120.0)


class SolanaHardeningTest(unittest.IsolatedAsyncioTestCase):
    def test_new_mint_is_the_created_account(self):
        tx = {
            "meta": {
                "preTokenBalances": [], "preBalances": [10, 0, 5, 0],
                "postTokenBalances": [{"mint": "XStockMint"}, {"mint": "NewMint"}],
                "postBalances": [9, 2039280, 5, 1461600],
            },
            "transaction": {"message": {"accountKeys": [
                {"pubkey": "Payer"}, {"pubkey": "Vault"}, {"pubkey": "XStockMint"}, {"pubkey": "NewMint"}]}},
        }
        self.assertEqual(new_mints(tx)[0], "NewMint")

    async def test_transaction_fetch_is_retried(self):
        responses = [None, None, {"meta": {}}]

        def handler(request):
            body = json.loads(request.content)
            return rpc_reply(body["id"], result=responses.pop(0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scanner = make_scanner(client, make_settings(), FakeNotifier())
            watcher = SolanaLaunchpadWatcher("wss://sol.test", "https://sol.test", scanner, client)
            watcher.TX_FETCH_DELAYS_S = (0, 0, 0, 0)
            self.assertEqual(await watcher._get_tx("sig"), {"meta": {}})

    async def test_pumpportal_reconnects_when_silent(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
            scanner = make_scanner(client, make_settings(), FakeNotifier())
            pp = PumpPortalWatcher(scanner, connect=lambda url: FakeWebSocket())
            pp.STALL_TIMEOUT_S = 0.05
            pp.reconnect_delay = 0.01
            task = asyncio.create_task(pp.run())
            for _ in range(100):
                await asyncio.sleep(0.01)
                if pp.connections >= 2:
                    break
            task.cancel()
            self.assertGreaterEqual(pp.connections, 2)
            self.assertIn("aucun message", pp.stats.last_error)


class ScannerHardeningTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(Router(**{"api.dexscreener.com": FakeDexScreener()})))
        self.scanner = make_scanner(self.client, make_settings(), None)
        self.entry = self.scanner.db.create_entry("P", ["ABC"], ["bsc"])
        self.scanner.reload()

    async def asyncTearDown(self):
        await self.client.aclose()

    def detection(self, **kw):
        base = dict(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC", pair_address=PAIR_ADDR, dex="x")
        base.update(kw)
        return Detection(**base)

    async def test_notification_retried_then_delivered(self):
        class Flaky(FakeNotifier):
            def __init__(self):
                super().__init__()
                self.failures = 2

            async def send(self, entry, result, labels, note):
                if self.failures:
                    self.failures -= 1
                    raise httpx.ConnectError("down")
                return await super().send(entry, result, labels, note)

        self.scanner.notifier = Flaky()
        with patch("ticker_scanner.scanner.NOTIFY_RETRY_DELAYS_S", (0, 0, 0)):
            await self.scanner.on_detection(self.detection())
            worker = asyncio.create_task(self.scanner._notify_worker())
            for _ in range(100):
                await asyncio.sleep(0.01)
                if self.scanner.notifier.sent:
                    break
            worker.cancel()
        self.assertEqual(len(self.scanner.notifier.sent), 1)
        self.assertEqual((self.scanner.notifications_sent, self.scanner.notifications_failed), (1, 0))

    async def test_forbidden_chat_is_not_retried(self):
        class Forbidden(FakeNotifier):
            calls = 0

            async def send(self, entry, result, labels, note):
                Forbidden.calls += 1
                raise TelegramError(403, "Forbidden: bot was blocked by the user")

        self.scanner.notifier = Forbidden()
        with patch("ticker_scanner.scanner.NOTIFY_RETRY_DELAYS_S", (0, 0, 0)):
            await self.scanner.on_detection(self.detection())
            await drain(self.scanner)
        self.assertEqual((Forbidden.calls, self.scanner.notifications_failed), (1, 1))

    async def test_supervisor_restarts_crashed_loop(self):
        runs = []

        async def loop():
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("crash")

        with patch("ticker_scanner.scanner.SUPERVISOR_FIRST_DELAY_S", 0):
            await asyncio.wait_for(self.scanner._supervise("test", loop), 1)
        self.assertEqual(len(runs), 2)

    async def test_hostile_token_names_are_cleaned(self):
        self.scanner.notifier = FakeNotifier()
        await self.scanner.on_detection(self.detection(name="Good‮Coin\nLine" + "x" * 100))
        res = self.scanner.db.results_for_entry(self.entry.id)[0]
        self.assertEqual(res.name[:13], "GoodCoin Line")
        self.assertLessEqual(len(res.name), 48)

    async def test_dexscreener_garbage_is_ignored(self):
        self.assertEqual(metrics({"liquidity": "lots", "marketCap": "n/a"}), (None, None))
        self.assertIsNone(pair_to_detection({"chainId": "bsc", "baseToken": "oops", "pairAddress": "0x1"}))
        await self.scanner.handle_dexscreener_pair({"chainId": "bsc", "baseToken": None}, "ABC", {"bsc"})

    async def test_dexscreener_failure_is_reported(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
            self.assertIsNone(await DexScreener(client, per_minute=100000).search("ABC"))


class NotifierFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        self.api = TelegramAPI("1:T", self.client)
        self.notifier = TelegramNotifier(self.api, 42, min_interval=0)
        self.fail_with = None

    async def asyncTearDown(self):
        await self.client.aclose()

    def handler(self, request):
        body = json.loads(request.content)
        self.calls.append(body)
        if self.fail_with and len(self.calls) == 1:
            return httpx.Response(400, json={"ok": False, "error_code": 400, "description": self.fail_with})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async def test_parse_error_falls_back_to_plain_text(self):
        self.fail_with = "Bad Request: can't parse entities: unclosed tag"
        self.assertEqual(await self.notifier.send_test(), 7)
        self.assertNotIn("parse_mode", self.calls[1])
        self.assertNotIn("<pre>", self.calls[1]["text"])

    async def test_bad_button_url_falls_back_without_links(self):
        self.fail_with = "Bad Request: BUTTON_URL_INVALID"
        self.assertEqual(await self.notifier.send_test(), 7)
        self.assertNotIn("reply_markup", self.calls[1])  # only url buttons: keyboard dropped

    async def test_forbidden_is_raised(self):
        self.fail_with = "Forbidden: bot was blocked by the user"
        with self.assertRaises(TelegramError):
            await self.notifier.send_test()


class StatusPageTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_page_and_test_notification(self):
        tg = FakeTelegram()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                Router(**{"api.telegram.org": tg, "api.dexscreener.com": FakeDexScreener()}))) as client:
            api = TelegramAPI("1:T", client)
            settings = make_settings()
            scanner = make_scanner(client, settings, TelegramNotifier(api, 42, min_interval=0))
            scanner.config_warnings = ["BSC : mauvais chain id"]
            bot = BotUI(api, scanner.db, scanner, settings)
            msg = {"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/scanner_etat"}}
            self.assertTrue(await bot.handle_update(msg))
            text = tg.of("sendMessage")[-1]["text"]
            self.assertIn("État du scanner", text)
            self.assertIn("dexscreener", text)
            self.assertIn("mauvais chain id", text)
            cb = {"update_id": 2, "callback_query": {"id": "q", "from": {"id": 42}, "data": "sc:tn",
                                                     "message": {"message_id": 5, "chat": {"id": 42}}}}
            await bot.handle_update(cb)
            self.assertTrue(tg.of("sendMessage")[-1]["text"].startswith("<pre>🧪 NOTIF DE TEST"))
            self.assertEqual(tg.of("answerCallbackQuery")[-1]["text"], "🔔 Notif de test envoyée")


class ChecksTest(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_chain_rpc_is_disabled(self):
        def handler(request):
            body = json.loads(request.content)
            chain_id = {"bsc.test": "0x1", "rh.test": hex(4663)}[request.url.host]
            return rpc_reply(body["id"], result=chain_id)

        settings = make_settings(evm_rpc={"bsc": EvmRpc(None, "https://bsc.test"),
                                          "robinhood": EvmRpc(None, "https://rh.test")})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            warnings = await check_rpcs(settings, client)
        self.assertNotIn("bsc", settings.evm_rpc)
        self.assertIn("robinhood", settings.evm_rpc)
        self.assertIn("chaîne 1 au lieu de 56", warnings[0])

    def test_secrets_are_masked(self):
        settings = load_settings({
            "TELEGRAM_BOT_TOKEN": "123456:SECRETTOKENVALUE",
            "BSC_WS_URL": "wss://bnb-mainnet.g.alchemy.com/v2/abcdefghijklmnop1234",
            "SOLANA_WS_URL": "wss://mainnet.helius-rpc.com/?api-key=11111111-2222-3333",
        })
        fmt = RedactingFormatter("%(message)s", secrets_of(settings))
        record = logging.LogRecord("x", logging.INFO, "", 0,
                                   "url %s token %s key %s", ("https://bnb-mainnet.g.alchemy.com/v2/abcdefghijklmnop1234",
                                                               "123456:SECRETTOKENVALUE", "11111111-2222-3333"), None)
        out = fmt.format(record)
        self.assertNotIn("abcdefghijklmnop1234", out)
        self.assertNotIn("SECRETTOKENVALUE", out)
        self.assertNotIn("11111111-2222-3333", out)


class ConfigAndDbTest(unittest.TestCase):
    def test_config_errors_are_explicit(self):
        base = {"TELEGRAM_BOT_TOKEN": "1:A"}
        for bad, needle in [({"TELEGRAM_ALLOWED_USERS": "bob"}, "TELEGRAM_ALLOWED_USERS"),
                            ({"DEXSCREENER_INTERVAL": "fast"}, "DEXSCREENER_INTERVAL"),
                            ({"DEXSCREENER_INTERVAL": "0"}, "minimum"),
                            ({"BSC_WS_URL": "https://x"}, "wss://"),
                            ({"TELEGRAM_NOTIFY_CHAT_ID": "@me"}, "TELEGRAM_NOTIFY_CHAT_ID"),
                            ({"TELEGRAM_BOT_TOKEN": ""}, "manquant")]:
            with self.assertRaises(SystemExit) as ctx:
                load_settings({**base, **bad})
            self.assertIn(needle, str(ctx.exception))

    def test_db_insert_is_idempotent_and_versioned(self):
        db = Database(":memory:")
        self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        entry = db.create_entry("P", ["ABC"], ["bsc"])
        det = Detection(chain="bsc", kind=PAIR, token_address=TOKEN, symbol="ABC", pair_address=PAIR_ADDR, dex="x")
        first = db.insert_result(entry.id, det, STATUS_PAIR)
        second = db.insert_result(entry.id, det, STATUS_PAIR)
        self.assertEqual(first.id, second.id)

    def test_clean_text(self):
        self.assertEqual(clean_text("a‮b\x00c", 10), "ab c")
        self.assertEqual(clean_text("🐸 Pepe", 10), "🐸 Pepe")
        self.assertEqual(clean_text("x" * 20, 5), "xxxx…")


if __name__ == "__main__":
    unittest.main()


class UiLimitsTest(unittest.TestCase):
    def test_main_menu_is_paginated(self):
        from ticker_scanner.formatting import main_menu
        db = Database(":memory:")
        entries = [db.create_entry(f"P{i}", ["ABC"], ["bsc"]) for i in range(25)]
        _, markup = main_menu(entries, {}, [], page=2)
        buttons = [b for row in markup["inline_keyboard"] for b in row]
        self.assertIn("3/3", [b["text"] for b in buttons])
        self.assertEqual(sum(1 for b in buttons if b["callback_data"].startswith("sc:e:")), 5)

    def test_results_page_never_exceeds_telegram_limit(self):
        from ticker_scanner.formatting import TELEGRAM_LIMIT, results_page
        db = Database(":memory:")
        entry = db.create_entry("N" * 40, ["A" * 20, "B" * 20, "C" * 20], ["robinhood", "bsc", "solana"])
        results = []
        for i in range(10):
            det = Detection(chain="robinhood", kind=PAIR, token_address="0x%040x" % i, symbol=clean_text("S" * 99, 24),
                            name=clean_text("N" * 99, 48), pair_address="0x" + "%064x" % i, pool_kind="v4",
                            dex=clean_text("D" * 99, 60), quote_symbol="Q" * 24, liquidity_usd=1e9, market_cap=1e12)
            results.append(db.insert_result(entry.id, det, "liq"))
        for size in (5, 10):
            text, _ = results_page(entry, results, 0, size, "Europe/Paris", time.time())
            self.assertLessEqual(len(text), TELEGRAM_LIMIT)


class PickerInputTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_during_chain_picking_gets_guidance(self):
        tg = FakeTelegram()
        async with httpx.AsyncClient(transport=httpx.MockTransport(Router(**{"api.telegram.org": tg}))) as client:
            api = TelegramAPI("1:T", client)
            settings = make_settings()
            scanner = make_scanner(client, settings, None)
            bot = BotUI(api, scanner.db, scanner, settings)
            for text in ("/nouveau", "Projet", "ABC", "bsc"):
                await bot.handle_update({"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": text}})
            self.assertIn("boutons ci-dessus", tg.of("sendMessage")[-1]["text"])


class PollingConflictTest(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, replies):
        sent = []

        def handler(request):
            method = request.url.path.rsplit("/", 1)[1]
            sent.append(method)
            if method == "getUpdates":
                reply = replies.pop(0) if replies else None
                if reply is None:
                    raise asyncio.CancelledError  # end of scenario
                return httpx.Response(200 if reply == "ok" else 409, json=(
                    {"ok": True, "result": []} if reply == "ok"
                    else {"ok": False, "error_code": 409, "description": reply}))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = TelegramAPI("1:T", client)
            settings = make_settings()
            scanner = make_scanner(client, settings, None)
            bot = BotUI(api, scanner.db, scanner, settings)
            with patch("ticker_scanner.bot.CONFLICT_RETRY_S", 0):
                try:
                    await asyncio.wait_for(bot.run_polling(), 2)
                    return "stopped", sent
                except asyncio.CancelledError:
                    return "still polling", sent

    async def test_restart_conflict_is_retried(self):
        outcome, sent = await self.run_with(
            ["Conflict: terminated by other getUpdates request", "ok"])
        self.assertEqual(outcome, "still polling")
        self.assertNotIn("sendMessage", sent)

    async def test_webhook_conflict_stops_menu_and_warns(self):
        outcome, sent = await self.run_with(["Conflict: can't use getUpdates method while webhook is active"])
        self.assertEqual(outcome, "stopped")
        self.assertIn("sendMessage", sent)

    async def test_persistent_conflict_gives_up(self):
        outcome, sent = await self.run_with(["Conflict: terminated by other getUpdates request"] * 4)
        self.assertEqual((outcome, sent.count("getUpdates")), ("stopped", 4))
