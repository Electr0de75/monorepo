"""Fuzzing: every parser / entry point fed with random and hostile input.

Deterministic (fixed seeds) so a failure is always reproducible.
"""

import asyncio
import base64
import os
import random
import string
import unittest
from html.parser import HTMLParser

import httpx

from ticker_scanner import evm_abi as abi
from ticker_scanner import formatting as fmt
from ticker_scanner.bot import BotUI
from ticker_scanner.chains import CHAINS
from ticker_scanner.config import EvmRpc
from ticker_scanner.dexscreener import metrics, pair_to_detection
from ticker_scanner.evm_watcher import EvmWatcher, range_hint
from ticker_scanner.models import LAUNCH, PAIR, Detection, clean_text, normalize_ticker, parse_tickers, valid_address
from ticker_scanner.solana import PumpPortalWatcher, find_token_strings, logs_for_program, new_mints
from ticker_scanner.telegram_api import TelegramAPI
from tests.helpers import FakeDexScreener, FakeEvmNode, FakeNotifier, FakeTelegram, Router, drain, make_scanner, make_settings

SEED = int(os.environ.get("FUZZ_SEED", "0"))
ITERATIONS = int(os.environ.get("FUZZ_ITERATIONS", "400"))  # FUZZ_ITERATIONS=20000 for a deep run
NASTY = ["", " ", "\x00", "<b>", "</pre>", "&amp;", '"', "'", "‮", "\n", "💥", "0x", "null", "-1",
         "9" * 80, "<a href=\"javascript:x\">", "${x}", "%s%n", "\\", "/", "@", "sc:", "sc:dy:1", "😀" * 50]


def rand_str(rng: random.Random, n: int = 30) -> str:
    alphabet = string.printable + "éèàçü€😀🐸‮​\x00"
    if rng.random() < 0.2:
        return rng.choice(NASTY)
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, n)))


def rand_value(rng: random.Random, depth: int = 0):
    r = rng.random()
    if depth > 2 or r < 0.25:
        return rand_str(rng)
    if r < 0.35:
        return rng.randint(-10 ** 20, 10 ** 20)
    if r < 0.4:
        return rng.random() * 10 ** rng.randint(0, 12)
    if r < 0.45:
        return None
    if r < 0.5:
        return rng.choice([True, False])
    if r < 0.75:
        return {rand_str(rng, 8): rand_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}
    return [rand_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]


class TelegramHtml(HTMLParser):
    """Checks a message only uses Telegram's HTML subset, well nested."""

    ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler",
               "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED:
            self.errors.append(f"tag {tag}")
        if tag == "a":
            names = [k for k, _ in attrs]
            href = dict(attrs).get("href") or ""
            if names != ["href"] or not href.startswith("https://"):
                self.errors.append(f"a attrs {attrs}")
        elif attrs:
            self.errors.append(f"attrs on {tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced {tag}")


def assert_telegram_html(test: unittest.TestCase, text: str) -> None:
    parser = TelegramHtml()
    parser.feed(text)
    parser.close()
    test.assertEqual(parser.errors, [], text[:300])
    test.assertEqual(parser.stack, [], text[:300])
    test.assertLessEqual(len(text), fmt.TELEGRAM_LIMIT)


class ParserFuzzTest(unittest.TestCase):
    def test_pure_parsers_never_raise(self):
        rng = random.Random(SEED + 1)
        for _ in range(ITERATIONS * 5):
            raw = rand_str(rng, 200)
            hexy = "0x" + "".join(rng.choice("0123456789abcdefg") for _ in range(rng.randint(0, 400)))
            abi.decode_string_result(hexy)
            abi.decode_string_result(raw)
            abi.words(hexy if rng.random() < 0.5 else "0x" + "00" * rng.randint(0, 100))
            find_token_strings(rng.randbytes(rng.randint(0, 300)))
            normalize_ticker(raw)
            parse_tickers(raw)
            clean_text(raw, rng.randint(1, 60))
            range_hint(raw)
            valid_address(rng.choice(["evm", "solana"]), raw, pool=rng.random() < 0.5)
            pair = rand_value(rng)
            if isinstance(pair, dict):
                pair_to_detection(pair)
                metrics(pair)

    def test_solana_log_parsing_never_raises(self):
        rng = random.Random(SEED + 2)
        program = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
        for _ in range(ITERATIONS):
            logs = []
            for _ in range(rng.randint(0, 12)):
                kind = rng.random()
                if kind < 0.2:
                    logs.append(f"Program {program} invoke [{rng.randint(1, 4)}]")
                elif kind < 0.4:
                    blob = rng.randbytes(rng.randint(0, 200))
                    logs.append("Program data: " + (base64.b64encode(blob).decode() if rng.random() < 0.8 else rand_str(rng)))
                elif kind < 0.5:
                    logs.append(f"Program {program} success")
                else:
                    logs.append(rand_str(rng, 80))
            logs_for_program(logs, program)
            new_mints({"meta": rand_value(rng), "transaction": rand_value(rng)} if rng.random() < 0.5 else
                      {"meta": {"preTokenBalances": [], "postTokenBalances": [{"mint": rand_str(rng)}],
                                "preBalances": [0], "postBalances": [1]},
                       "transaction": {"message": {"accountKeys": [{"pubkey": rand_str(rng)}]}}})


class EntryPointFuzzTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.node = FakeEvmNode()
        self.tg = FakeTelegram()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(Router(**{
            "rpc.test": self.node, "api.dexscreener.com": FakeDexScreener(), "api.telegram.org": self.tg})))
        self.notifier = FakeNotifier()
        settings = make_settings(evm_rpc={"bsc": EvmRpc(None, "https://rpc.test")}, notify_flood_limit=0)
        self.scanner = make_scanner(self.client, settings, self.notifier)
        self.entry = self.scanner.db.create_entry("Fuzz", ["ABC", "<B>", "💥"], list(CHAINS))
        self.scanner.reload()
        self.watcher = EvmWatcher(CHAINS["bsc"], settings.evm_rpc["bsc"], self.scanner, settings, self.client)
        self.watcher.rpc.base_delay = 0
        self.watcher.rpc.retries = 0
        self.bot = BotUI(TelegramAPI("1:T", self.client), self.scanner.db, self.scanner, settings)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_detections_with_garbage_are_safe(self):
        rng = random.Random(SEED + 3)
        for i in range(ITERATIONS):
            chain = rng.choice(list(CHAINS) + ["nope", ""])
            token = rng.choice(["0x%040x" % rng.getrandbits(160), "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
                                rand_str(rng)])
            det = Detection(chain=chain, kind=rng.choice([LAUNCH, PAIR, "liquidity", "???"]), token_address=token,
                            symbol=rng.choice(["ABC", "<b>", "💥", rand_str(rng)]), name=rand_str(rng, 200),
                            pair_address=rng.choice([None, "0x%040x" % rng.getrandbits(160), "0x%064x" % rng.getrandbits(256),
                                                     rand_str(rng)]),
                            dex=rand_str(rng, 100), quote_symbol=rand_str(rng), quote_address=rand_str(rng),
                            has_liquidity=rng.random() < 0.5, source=rng.choice(["onchain", "dexscreener"]),
                            liquidity_usd=rng.choice([None, rng.random() * 1e9]),
                            market_cap=rng.choice([None, rng.random() * 1e12]),
                            market_cap_note=rand_str(rng), created_at=rng.random() * 2e9)
            await self.scanner.on_detection(det)
        await drain(self.scanner)
        results = self.scanner.db.results_for_entry(self.entry.id)
        self.assertTrue(results)  # some valid ones were stored...
        for res in results:      # ...and only valid ones
            kind = CHAINS[res.chain].kind
            self.assertTrue(valid_address(kind, res.token_address), res.token_address)
            if res.pair_address:
                self.assertTrue(valid_address(kind, res.pair_address, pool=True), res.pair_address)
        # every message we would send is valid Telegram HTML
        for entry, res, labels, note in self.notifier.sent:
            assert_telegram_html(self, fmt.notification_text(entry, res, labels, note))
        for page in range(0, len(results) // 5 + 1):
            text, _ = fmt.results_page(self.entry, results, page, 5, "Europe/Paris", 0)
            assert_telegram_html(self, text)

    async def test_random_logs_never_crash_the_watcher(self):
        rng = random.Random(SEED + 4)
        factories = list(self.watcher.dex_by_address)
        topics0 = [abi.V2_PAIR_CREATED, abi.V3_POOL_CREATED, abi.V4_INITIALIZE, abi.TRANSFER, rand_str(rng)]
        for _ in range(ITERATIONS):
            lg = {
                "address": rng.choice(factories + ["0x%040x" % rng.getrandbits(160), rand_str(rng), None]),
                "topics": [rng.choice(topics0)] + ["0x%064x" % rng.getrandbits(256) for _ in range(rng.randint(0, 4))],
                "data": rng.choice(["0x" + "%064x" % rng.getrandbits(256) * rng.randint(0, 6), rand_str(rng), None]),
                "blockNumber": rng.choice([hex(rng.randint(0, 10 ** 6)), rand_str(rng), None]),
                "transactionHash": rng.choice(["0x" + "ab" * 32, None, rand_str(rng)]),
            }
            if rng.random() < 0.1:
                lg = rand_value(rng)
            for handler in (self.watcher.handle_pair, self.watcher.handle_mint, self.watcher.handle_v4_liquidity):
                if isinstance(lg, dict):
                    await handler(lg)
            self.watcher._enqueue(rng.choice(["pairs", "mints", "v4liq", "?"]), lg, track=True)

    async def test_random_telegram_updates_never_raise(self):
        rng = random.Random(SEED + 5)
        actions = ["m", "st", "n", "e", "p", "ed", "en", "et", "ec", "d", "dy", "r", "rn", "rf", "ct", "ca", "cv",
                   "cx", "noop", "zz"]
        for i in range(ITERATIONS):
            if rng.random() < 0.5:
                data = "sc:" + rng.choice(actions) + "".join(f":{rand_str(rng, 6)}" for _ in range(rng.randint(0, 3)))
                update = {"update_id": i, "callback_query": {
                    "id": rng.choice(["q", None, 5]), "from": {"id": rng.choice([42, 7, None])},
                    "data": rng.choice([data, rand_str(rng), None]),
                    "message": rng.choice([{"message_id": rng.randint(1, 600), "chat": {"id": 42}}, None, {}])}}
            elif rng.random() < 0.8:
                text = rng.choice(["/scanner", "/nouveau", "/annuler", "/scanner_etat", "/start", rand_str(rng, 60)])
                update = {"update_id": i, "message": {"from": {"id": rng.choice([42, 9])},
                                                      "chat": {"id": 42}, "text": text}}
            else:
                update = rand_value(rng) if rng.random() < 0.5 else {"update_id": i, "message": rand_value(rng)}
            if isinstance(update, dict):
                self.assertIn(await self.bot.handle_update(update), (True, False))
        for _, body in self.tg.sent:
            if body.get("parse_mode") == "HTML" and "text" in body:
                assert_telegram_html(self, body["text"])
            if "text" in body and "callback_query_id" in body and body["text"]:
                self.assertLessEqual(len(body["text"]), 200)

    async def test_random_pumpportal_messages_never_raise(self):
        rng = random.Random(SEED + 6)
        pp = PumpPortalWatcher(self.scanner)
        for _ in range(ITERATIONS):
            msg = {"txType": rng.choice(["create", "migrate", rand_str(rng)]),
                   "mint": rng.choice(["7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU", rand_str(rng), None, 5]),
                   "symbol": rng.choice(["ABC", rand_value(rng)]), "name": rand_value(rng),
                   "marketCapSol": rand_value(rng), "pool": rand_value(rng), "bondingCurveKey": rand_value(rng)}
            try:
                await pp.handle(msg)
            except Exception as exc:  # pragma: no cover - the assertion below reports it
                self.fail(f"PumpPortal.handle raised {exc!r} on {msg!r}")
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
