import asyncio
import base64
import json
import os
import unittest
from unittest.mock import patch

import httpx

from ticker_scanner.solana import (
    PumpPortalWatcher, SolanaLaunchpadWatcher, b58decode, find_token_strings, logs_for_program, new_mints,
)
from tests.helpers import FakeDexScreener, FakeNotifier, Router, drain, make_scanner, make_settings

LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
DBC = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def borsh(text: str) -> bytes:
    raw = text.encode()
    return len(raw).to_bytes(4, "little") + raw


def event_blob(name: str, symbol: str, uri: str, prefix: bytes = b"") -> bytes:
    return os.urandom(8) + prefix + borsh(name) + borsh(symbol) + borsh(uri) + os.urandom(64)


class FakeSolanaRpc:
    def __init__(self):
        self.txs: dict[str, dict] = {}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body["method"])
        result = self.txs.get(body["params"][0]) if body["method"] == "getTransaction" else None
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def tx_with_mint(mint: str, instructions: list[dict] | None = None) -> dict:
    return {
        "meta": {
            "preTokenBalances": [],
            "postTokenBalances": [{"mint": "So11111111111111111111111111111111111111112"}, {"mint": mint}],
            "innerInstructions": [],
        },
        "transaction": {"message": {"instructions": instructions or []}},
    }


class ParsingTest(unittest.TestCase):
    def test_b58(self):
        raw = os.urandom(32)
        self.assertEqual(b58decode(b58encode(raw)), raw)
        self.assertEqual(b58decode(b58encode(b"\x00\x00ab")), b"\x00\x00ab")

    def test_find_token_strings(self):
        found = find_token_strings(event_blob("Abc Coin", "ABC", "https://ipfs.io/x", prefix=b"\x06"))
        self.assertIn(("Abc Coin", "ABC"), [(f.name, f.symbol) for f in found])
        self.assertEqual(find_token_strings(os.urandom(200)), [])

    def test_logs_for_program(self):
        blob = event_blob("Abc", "ABC", "u")
        logs = [
            "Program ComputeBudget111111111111111111111111111111 invoke [1]",
            "Program ComputeBudget111111111111111111111111111111 success",
            f"Program {LAUNCHLAB} invoke [1]",
            "Program log: Instruction: InitializeV2",
            "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]",
            "Program log: Instruction: InitializeMint2",
            "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA consumed 100 of 200 compute units",
            "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA success",
            f"Program data: {base64.b64encode(blob).decode()}",
            f"Program {LAUNCHLAB} consumed 1000 of 200000 compute units",
            f"Program {LAUNCHLAB} success",
        ]
        plogs = logs_for_program(logs, LAUNCHLAB)
        self.assertEqual(plogs.instructions, {"InitializeV2"})
        self.assertEqual(plogs.data, [blob])

    def test_new_mints(self):
        self.assertEqual(new_mints(tx_with_mint(MINT)), [MINT])


class SolanaSourcesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rpc = FakeSolanaRpc()
        self.ds = FakeDexScreener()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(
            Router(**{"sol.test": self.rpc, "api.dexscreener.com": self.ds})))
        self.notifier = FakeNotifier()
        self.scanner = make_scanner(self.client, make_settings(), self.notifier)
        self.scanner.db.create_entry("Proj", ["ABC"], ["solana"])
        self.scanner.reload()
        self.watcher = SolanaLaunchpadWatcher("wss://sol.test", "https://sol.test", self.scanner, self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    def logs(self, program: str, ix: str, blob: bytes | None) -> list[str]:
        lines = [f"Program {program} invoke [1]", f"Program log: Instruction: {ix}"]
        if blob is not None:
            lines.append(f"Program data: {base64.b64encode(blob).decode()}")
        return lines + [f"Program {program} success"]

    async def test_launchlab_event(self):
        self.rpc.txs["sig1"] = tx_with_mint(MINT)
        await self.watcher.analyze(LAUNCHLAB, "sig1", self.logs(LAUNCHLAB, "InitializeV2",
                                                                event_blob("Abc Coin", "abc", "https://x")))
        await drain(self.scanner)
        _, res, labels, _ = self.notifier.sent[0]
        self.assertEqual((labels, res.token_address, res.symbol, res.dex),
                         (["launch"], MINT, "abc", "LaunchLab (bonk.fun / StonkFun)"))

    async def test_unwatched_event_costs_no_rpc_call(self):
        await self.watcher.analyze(LAUNCHLAB, "sig2", self.logs(LAUNCHLAB, "InitializeV2",
                                                                event_blob("Other", "XYZ", "https://x")))
        await self.watcher.analyze(LAUNCHLAB, "sig3", self.logs(LAUNCHLAB, "BuyExactIn", None))
        self.assertEqual(self.rpc.calls, [])

    async def test_dbc_reads_instruction_data(self):
        data = b58encode(event_blob("Abc Coin", "ABC", "https://x"))
        self.rpc.txs["sig4"] = tx_with_mint(MINT, [{"programId": DBC, "data": data, "accounts": []}])
        await self.watcher.analyze(DBC, "sig4", self.logs(DBC, "InitializeVirtualPoolWithSplToken", None))
        await drain(self.scanner)
        self.assertEqual(self.notifier.sent[0][1].token_address, MINT)
        self.assertEqual(self.rpc.calls, ["getTransaction"])

    async def test_pumpportal_create_and_migration(self):
        pp = PumpPortalWatcher(self.scanner)
        await pp.handle({"txType": "create", "mint": MINT, "symbol": "ABC", "name": "Abc", "pool": "pump",
                         "bondingCurveKey": "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf", "marketCapSol": 31.5, "signature": "s"})
        await pp.handle({"txType": "create", "mint": "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1", "symbol": "NOPE", "pool": "pump"})
        await drain(self.scanner)
        self.assertEqual(len(self.notifier.sent), 1)
        _, res, labels, note = self.notifier.sent[0]
        self.assertEqual((labels, res.dex, note), (["launch"], "pump.fun", "31.5 SOL"))
        # migration -> the new PumpSwap pool is looked up on DexScreener
        self.ds.token_pairs[MINT] = [{
            "chainId": "solana", "dexId": "pumpswap", "pairAddress": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2", "pairCreatedAt": 1,
            "baseToken": {"address": MINT, "symbol": "ABC", "name": "Abc"},
            "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
            "liquidity": {"usd": 25000}, "marketCap": 70000,
        }]
        with patch("ticker_scanner.scanner.NEW_PAIR_LOOKUP_DELAYS_S", (0,)):
            await pp.handle({"txType": "migrate", "mint": MINT})
            for _ in range(20):
                await asyncio.sleep(0.01)
        await drain(self.scanner)
        _, res, labels, _ = self.notifier.sent[1]
        self.assertEqual((labels, res.pair_address, res.market_cap), (["pair", "liq"], "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2", 70000))


if __name__ == "__main__":
    unittest.main()
