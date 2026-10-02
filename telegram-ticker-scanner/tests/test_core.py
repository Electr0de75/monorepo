import hashlib
import os
import time
import unittest

from ticker_scanner import evm_abi as abi
from ticker_scanner.chains import CHAINS, DEXSCREENER_TO_CHAIN, norm_address
from ticker_scanner.config import load_settings
from ticker_scanner.db import Database
from ticker_scanner.formatting import fmt_usd, links, notification_text, results_page
from ticker_scanner.keccak import _sponge, keccak256, keccak_hex, selector
from ticker_scanner.models import (
    LABEL_LIQ, LABEL_PAIR, PAIR, STATUS_LIQ, STATUS_PAIR, Detection, normalize_ticker, parse_tickers,
)
from tests.helpers import abi_string


class KeccakTest(unittest.TestCase):
    def test_vectors(self):
        self.assertEqual(keccak256(b"").hex(), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(selector("transfer(address,uint256)"), "0xa9059cbb")
        self.assertEqual(selector("symbol()"), "0x95d89b41")
        self.assertEqual(keccak_hex("Transfer(address,address,uint256)"),
                         "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")

    def test_same_permutation_as_sha3(self):
        # SHA3-256 only differs by the padding byte, so this checks the permutation.
        for n in (0, 1, 135, 136, 137, 300):
            data = os.urandom(n)
            self.assertEqual(_sponge(data, 0x06), hashlib.sha3_256(data).digest())

    def test_event_topics(self):
        self.assertEqual(abi.V2_PAIR_CREATED, "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9")
        self.assertEqual(abi.V3_POOL_CREATED, "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118")
        self.assertEqual(abi.V4_INITIALIZE, "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438")


class AbiTest(unittest.TestCase):
    def test_decode_string(self):
        self.assertEqual(abi.decode_string_result(abi_string("PEPE")), "PEPE")
        self.assertEqual(abi.decode_string_result("0x" + b"MKR".ljust(32, b"\x00").hex()), "MKR")
        self.assertIsNone(abi.decode_string_result("0x"))

    def test_words(self):
        data = "0x" + "00" * 12 + "ab" * 20 + (5).to_bytes(32, "big").hex()
        self.assertEqual(abi.word_address(data, 0), "0x" + "ab" * 20)
        self.assertEqual(abi.word_uint(data, 1), 5)
        self.assertEqual(abi.word_int("0x" + "ff" * 32, 0), -1)


class ModelsTest(unittest.TestCase):
    def test_tickers(self):
        self.assertEqual(normalize_ticker(" $pepe "), "PEPE")
        self.assertEqual(parse_tickers("$abc, def  abc;ghi"), ["ABC", "DEF", "GHI"])

    def test_chain_registry(self):
        self.assertEqual(DEXSCREENER_TO_CHAIN["robinhood"], "robinhood")
        self.assertTrue(CHAINS["bsc"].has_onchain_config)
        self.assertFalse(CHAINS["polygon"].has_onchain_config)
        self.assertEqual(norm_address("bsc", "0xABC"), "0xabc")
        self.assertEqual(norm_address("solana", "AbC"), "AbC")
        for chain in CHAINS.values():
            for table in (chain.v2_factories, chain.v3_factories, chain.launchpads, chain.quote_tokens):
                for address in table:
                    self.assertEqual(len(address), 42, address)

    def test_settings(self):
        s = load_settings({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "12, 34",
                           "BSC_WS_URL": "wss://bnb.example/v2/k", "SOLANA_WS_URL": "wss://sol.example/?api-key=k"})
        self.assertEqual(s.target_chat_id, 12)
        self.assertEqual(s.evm_rpc["bsc"].http_url, "https://bnb.example/v2/k")
        self.assertEqual(s.solana_http_url, "https://sol.example/?api-key=k")


class DbTest(unittest.TestCase):
    def test_entry_and_results(self):
        db = Database(":memory:")
        entry = db.create_entry("Proj", ["ABC"], ["bsc"])
        det = Detection(chain="bsc", kind=PAIR, token_address="0xt", symbol="ABC", pair_address="0xp",
                        pool_kind="v2", dex="PancakeSwap v2")
        res = db.insert_result(entry.id, det, STATUS_PAIR)
        self.assertEqual(db.count_results(entry.id), (1, 0))
        self.assertEqual(len(db.pending_results(3600)), 1)
        db.mark_liquidity(res.id, 1000.0, None)
        self.assertEqual(db.get_result_by_id(res.id).status, STATUS_LIQ)
        self.assertEqual(db.pending_results(3600), [])
        old_since = db.get_entry(entry.id).scan_since
        time.sleep(0.01)
        db.update_entry(entry.id, tickers=["XYZ"])
        self.assertGreater(db.get_entry(entry.id).scan_since, old_since)
        db.update_entry(entry.id, paused=True)
        self.assertEqual(db.active_entries(), [])
        db.delete_entry(entry.id)
        self.assertIsNone(db.get_result_by_id(res.id))


class FormattingTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.entry = self.db.create_entry("Mon <projet>", ["ABC"], ["bsc", "solana"])
        det = Detection(chain="bsc", kind=PAIR, token_address="0x" + "1" * 40, symbol="ABC", name="Abc Coin",
                        pair_address="0x" + "2" * 40, pool_kind="v2", dex="PancakeSwap v2", quote_symbol="WBNB",
                        liquidity_usd=12345, market_cap=987654)
        self.res = self.db.insert_result(self.entry.id, det, STATUS_LIQ)

    def test_usd(self):
        self.assertEqual(fmt_usd(None), "—")
        self.assertEqual(fmt_usd(950), "$950")
        self.assertEqual(fmt_usd(12345), "$12.35K")
        self.assertEqual(fmt_usd(2_000_000), "$2M")

    def test_notification(self):
        text = notification_text(self.entry, self.res, [LABEL_PAIR, LABEL_LIQ])
        self.assertTrue(text.startswith("<pre>🟡 PAIR CREATED + 🟢 LIQ ADDED"))
        self.assertIn("Mon &lt;projet&gt;", text)
        self.assertIn("MC     : $987.65K", text)
        self.assertIn(f"<code>{self.res.token_address}</code>", text)

    def test_links(self):
        names = dict(links(self.res))
        self.assertEqual(names["DexScreener"], f"https://dexscreener.com/bsc/{self.res.pair_address}")
        self.assertEqual(names["GMGN"], f"https://gmgn.ai/bsc/token/{self.res.token_address}")
        self.assertIn("defined.fi/bsc/", names["Defined"])

    def test_results_page(self):
        text, markup = results_page(self.entry, [self.res] * 7, 1, 5, "Europe/Paris", time.time())
        self.assertIn("<b>6.</b>", text)
        self.assertNotIn("<b>5.</b>", text)
        self.assertEqual(markup["inline_keyboard"][0][1]["text"], "2/2")
        self.assertTrue(all(len(b["callback_data"]) <= 64 for row in markup["inline_keyboard"] for b in row
                            if "callback_data" in b))


if __name__ == "__main__":
    unittest.main()
