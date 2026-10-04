import time
import unittest

import httpx

from ticker_scanner.bot import BotUI, TelegramNotifier
from ticker_scanner.models import LABEL_LIQ, LABEL_PAIR, PAIR, Detection
from ticker_scanner.telegram_api import TelegramAPI
from tests.helpers import FakeDexScreener, FakeTelegram, Router, drain, make_scanner, make_settings

USER = 42
CHAT = 42


def msg(text: str, user: int = USER) -> dict:
    return {"update_id": 1, "message": {"from": {"id": user}, "chat": {"id": CHAT}, "text": text, "message_id": 5}}


def cb(data: str, message_id: int = 500) -> dict:
    return {"update_id": 2, "callback_query": {"id": "q", "from": {"id": USER}, "data": "sc:" + data,
                                               "message": {"message_id": message_id, "chat": {"id": CHAT}}}}


class BotTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tg = FakeTelegram()
        self.ds = FakeDexScreener()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(
            Router(**{"api.telegram.org": self.tg, "api.dexscreener.com": self.ds})))
        self.api = TelegramAPI("TOKEN", self.client)
        settings = make_settings()
        self.scanner = make_scanner(self.client, settings, TelegramNotifier(self.api, CHAT, min_interval=0))
        self.bot = BotUI(self.api, self.scanner.db, self.scanner, settings)

    async def asyncTearDown(self):
        await self.client.aclose()

    def last_text(self, method: str = None) -> str:
        for m, body in reversed(self.tg.sent):
            if "text" in body and (method is None or m == method):
                return body["text"]
        return ""

    def buttons(self) -> list[dict]:
        for m, body in reversed(self.tg.sent):
            if body.get("reply_markup"):
                return [b for row in body["reply_markup"]["inline_keyboard"] for b in row]
        return []

    async def create(self, name="Projet X", tickers="$abc, def"):
        await self.bot.handle_update(msg("/nouveau"))
        await self.bot.handle_update(msg(name))
        await self.bot.handle_update(msg(tickers))
        picker = self.tg.next_message_id  # clicks come from the picker message itself
        await self.bot.handle_update(cb("ct:solana", picker))
        await self.bot.handle_update(cb("ct:robinhood", picker))
        await self.bot.handle_update(cb("cv", picker))
        return self.scanner.db.list_entries()[-1]

    async def test_unknown_user_gets_his_id_only_when_unconfigured(self):
        self.bot.settings.allowed_users = set()
        await self.bot.handle_update(msg("/start", user=7))
        self.assertIn("<code>7</code>", self.last_text())
        self.bot.settings.allowed_users = {USER}
        count = len(self.tg.sent)
        await self.bot.handle_update(msg("/scanner", user=8))
        self.assertEqual(len(self.tg.sent), count)

    async def test_create_entry_flow(self):
        entry = await self.create()
        self.assertEqual((entry.name, entry.tickers, entry.chains), ("Projet X", ["ABC", "DEF"], ["solana", "robinhood"]))
        self.assertIn("Projet créé", self.last_text())
        self.assertTrue(self.scanner.is_watched("robinhood", "ABC"))
        self.assertNotIn(USER, self.bot.sessions)

    async def test_validation(self):
        await self.bot.handle_update(msg("/nouveau"))
        await self.bot.handle_update(msg("P"))
        await self.bot.handle_update(msg("A B C D"))
        self.assertIn("entre 1 et 3 tickers", self.last_text())
        await self.bot.handle_update(msg("A"))
        picker = self.tg.next_message_id
        await self.bot.handle_update(cb("cv", picker))
        answer = self.tg.of("answerCallbackQuery")[-1]
        self.assertTrue(answer["show_alert"])
        await self.bot.handle_update(cb("ca:evm", picker))
        await self.bot.handle_update(cb("cv", picker))
        entry = self.scanner.db.list_entries()[-1]
        self.assertIn("bsc", entry.chains)
        self.assertNotIn("solana", entry.chains)

    async def test_pause_edit_delete(self):
        entry = await self.create()
        await self.bot.handle_update(cb(f"p:{entry.id}"))
        self.assertTrue(self.scanner.db.get_entry(entry.id).paused)
        self.assertFalse(self.scanner.is_watched("solana", "ABC"))
        self.assertIn("En pause", self.last_text("editMessageText"))
        await self.bot.handle_update(cb(f"p:{entry.id}"))
        self.assertTrue(self.scanner.is_watched("solana", "ABC"))

        await self.bot.handle_update(cb(f"en:{entry.id}"))
        await self.bot.handle_update(msg("Nouveau nom"))
        await self.bot.handle_update(cb(f"et:{entry.id}"))
        await self.bot.handle_update(msg("XYZ"))
        await self.bot.handle_update(cb(f"ec:{entry.id}"))
        await self.bot.handle_update(cb("ct:bsc"))
        await self.bot.handle_update(cb("cv"))
        e = self.scanner.db.get_entry(entry.id)
        self.assertEqual((e.name, e.tickers, e.chains), ("Nouveau nom", ["XYZ"], ["solana", "bsc", "robinhood"]))

        await self.bot.handle_update(cb(f"d:{entry.id}"))
        self.assertIn("Supprimer", self.last_text("editMessageText"))
        await self.bot.handle_update(cb(f"dy:{entry.id}"))
        self.assertIsNone(self.scanner.db.get_entry(entry.id))
        self.assertIn("Scanner de tickers", self.last_text("editMessageText"))

    async def test_notification_results_and_refresh(self):
        entry = await self.create()
        token = "0x" + "a1" * 20
        pool = "0x" + "b2" * 20
        await self.scanner.on_detection(Detection(
            chain="robinhood", kind=PAIR, token_address=token, symbol="ABC", name="Abc", pair_address=pool,
            pool_kind="v2", dex="Uniswap v2", quote_symbol="WETH", has_liquidity=True, liquidity_usd=1000))
        await drain(self.scanner)
        notif = self.tg.of("sendMessage")[-1]
        self.assertTrue(notif["text"].startswith("<pre>🟡 PAIR CREATED + 🟢 LIQ ADDED"))
        self.assertEqual(notif["parse_mode"], "HTML")
        urls = [b["url"] for row in notif["reply_markup"]["inline_keyboard"] for b in row if "url" in b]
        self.assertEqual(urls, [f"https://dexscreener.com/robinhood/{pool}",
                                f"https://www.defined.fi/robinhood/{pool}",
                                f"https://gmgn.ai/robinhood/token/{token}"])

        # results button from the notification sends a new message
        await self.bot.handle_update(cb(f"rn:{entry.id}"))
        self.assertIn("$ABC", self.last_text("sendMessage"))
        self.assertIn(f"<code>{token}</code>", self.last_text("sendMessage"))

        # refresh updates market caps and edits the list in place
        self.ds.token_pairs[token] = [{
            "chainId": "robinhood", "dexId": "uniswap", "pairAddress": pool, "pairCreatedAt": int(time.time() * 1000),
            "baseToken": {"address": token, "symbol": "ABC"}, "quoteToken": {"symbol": "WETH"},
            "liquidity": {"usd": 55000}, "marketCap": 1_250_000,
        }]
        await self.bot.handle_update(cb(f"rf:{entry.id}:0", message_id=777))
        edit = self.tg.of("editMessageText")[-1]
        self.assertEqual(edit["message_id"], 777)
        self.assertIn("MC $1.25M", edit["text"])
        self.assertIn("Mis à jour", edit["text"])

    async def test_foreign_updates_are_left_to_the_host_bot(self):
        self.assertFalse(await self.bot.handle_update(msg("/stats")))
        self.assertFalse(await self.bot.handle_update(msg("hello")))
        foreign = cb("x")
        foreign["callback_query"]["data"] = "tweet:123"
        self.assertFalse(await self.bot.handle_update(foreign))
        self.assertTrue(await self.bot.handle_update(msg("/scanner")))
        embedded = BotUI(self.api, self.scanner.db, self.scanner, self.bot.settings, handle_start=False)
        self.assertFalse(await embedded.handle_update(msg("/start")))
        self.assertTrue(await embedded.handle_update(msg("/scanner")))

    async def test_notifier_edit(self):
        entry = await self.create()
        res = self.scanner.db.insert_result(entry.id, Detection(
            chain="solana", kind=PAIR, token_address="Mint", symbol="ABC", pair_address="Pool", dex="Raydium"),
            "liq")
        notifier = TelegramNotifier(self.api, CHAT, min_interval=0)
        mid = await notifier.send(entry, res, [LABEL_PAIR, LABEL_LIQ], None)
        await notifier.edit(entry, res, [LABEL_PAIR, LABEL_LIQ], None, mid)
        self.assertEqual(self.tg.of("editMessageText")[-1]["message_id"], mid)


if __name__ == "__main__":
    unittest.main()
