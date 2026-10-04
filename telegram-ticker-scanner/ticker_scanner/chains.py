"""Chain registry.

Every chain is scanned through DexScreener. A chain that also has an RPC URL in
the .env (``<ENV_PREFIX>_WS_URL`` / ``<ENV_PREFIX>_HTTP_URL``) and DEX factories
below is additionally watched on-chain in real time.

Adding an EVM chain = adding one ``Chain(...)`` entry. Addresses must be
checksummed or lowercase; they are lowercased at load time.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Chain:
    key: str
    label: str
    kind: str  # "evm" | "solana"
    dexscreener: str
    native_symbol: str = "ETH"
    chain_id: int | None = None  # checked against eth_chainId at startup
    block_time: float = 2.0      # seconds, sizes the reconnect backfill window
    gmgn: str | None = None
    defined: str | None = None
    env_prefix: str | None = None
    # address -> DEX name
    v2_factories: dict[str, str] = field(default_factory=dict)
    v3_factories: dict[str, str] = field(default_factory=dict)
    v4_pool_managers: dict[str, str] = field(default_factory=dict)
    # Solidly forks (Aerodrome v2) and Slipstream (Aerodrome CL)
    solidly_factories: dict[str, str] = field(default_factory=dict)
    slipstream_factories: dict[str, str] = field(default_factory=dict)
    # Known launchpad contracts, only used to name the launchpad in the
    # notification (every new token is detected, launchpad known or not).
    launchpads: dict[str, str] = field(default_factory=dict)
    # Uniswap v4 hooks that belong to a launchpad (pool init = launch)
    v4_hooks: dict[str, str] = field(default_factory=dict)
    # Quote tokens (never the "new" token): address -> symbol
    quote_tokens: dict[str, str] = field(default_factory=dict)

    @property
    def is_evm(self) -> bool:
        return self.kind == "evm"

    @property
    def has_onchain_config(self) -> bool:
        return bool(
            self.v2_factories or self.v3_factories or self.v4_pool_managers
            or self.solidly_factories or self.slipstream_factories or self.launchpads
        )


def _lower(d: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in d.items()}


def _evm(**kw) -> Chain:
    for name in (
        "v2_factories", "v3_factories", "v4_pool_managers", "solidly_factories",
        "slipstream_factories", "launchpads", "v4_hooks", "quote_tokens",
    ):
        if name in kw:
            kw[name] = _lower(kw[name])
    return Chain(kind="evm", **kw)


PANCAKE_V3_FACTORY = "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"

_CHAINS: list[Chain] = [
    Chain(
        key="solana", label="Solana", kind="solana",
        dexscreener="solana", native_symbol="SOL", gmgn="sol", defined="sol",
    ),
    _evm(
        key="bsc", label="BSC", dexscreener="bsc", native_symbol="BNB", chain_id=56, block_time=0.75, gmgn="bsc", defined="bsc", env_prefix="BSC",
        v2_factories={
            "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73": "PancakeSwap v2",
            "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6": "Uniswap v2",
        },
        v3_factories={
            PANCAKE_V3_FACTORY: "PancakeSwap v3",
            "0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7": "Uniswap v3",
        },
        v4_pool_managers={"0x28e2Ea090877bF75740558f6BFB36A5ffeE9e9dF": "Uniswap v4"},
        launchpads={
            "0x5c952063c7fc8610FFDB798152D69F0B9550762b": "four.meme",
            "0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0": "Flap",
        },
        quote_tokens={
            "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c": "WBNB",
            "0x55d398326f99059fF775485246999027B3197955": "USDT",
            "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d": "USDC",
            "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56": "BUSD",
            "0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409": "FDUSD",
            "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d": "USD1",
        },
    ),
    _evm(
        key="robinhood", label="Robinhood", dexscreener="robinhood", chain_id=4663, block_time=0.1, gmgn="robinhood",
        defined="robinhood", env_prefix="ROBINHOOD",
        v2_factories={"0x8bcEaA40B9AcdfAedF85AdF4FF01F5Ad6517937f": "Uniswap v2"},
        v3_factories={"0x1f7d7550b1b028f7571e69a784071f0205fd2efa": "Uniswap v3"},
        v4_pool_managers={"0x8366a39cc670b4001a1121b8f6a443a643e40951": "Uniswap v4"},
        launchpads={
            "0x26605f322f7fF986f381bB9A6e3f5DAb0bEaEb09": "Flap",
            "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e": "Pons",
            "0x3711cea4feade896c913c68f01eda97cb06d1a42": "Pons",
            "0xe33e9e479df8802cb0866d5d05258bec4cf62948": "Pons",
            "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB": "Pons",
            "0x0c37a24F5D23A486FA692d1500881d698B1F77a4": "Pons",
            "0x8660A7F019C7943b0b0A91B8E39AFf3b6DB6Ae62": "PAIR",
            # LONG, o1, Bankr, hood.fun, Pools.trade: no public address found;
            # their tokens are still caught by the generic "new token" detector.
        },
        v4_hooks={"0xe5e702641ea86f4ae6cc3cdaed2b886f976be044": "Pons"},
        quote_tokens={"0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73": "WETH"},
    ),
    _evm(
        key="ethereum", label="Ethereum", dexscreener="ethereum", chain_id=1, block_time=12, gmgn="eth", defined="eth",
        env_prefix="ETHEREUM",
        v2_factories={
            "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f": "Uniswap v2",
            "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac": "SushiSwap",
        },
        v3_factories={
            "0x1F98431c8aD98523631AE4a59f267346ea31F984": "Uniswap v3",
            PANCAKE_V3_FACTORY: "PancakeSwap v3",
        },
        v4_pool_managers={"0x000000000004444c5dc75cB358380D2e3dE08A90": "Uniswap v4"},
        quote_tokens={
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": "WETH",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": "USDC",
            "0xdAC17F958D2ee523a2206206994597C13D831ec7": "USDT",
        },
    ),
    _evm(
        key="base", label="Base", dexscreener="base", chain_id=8453, block_time=2, gmgn="base", defined="base",
        env_prefix="BASE",
        v2_factories={"0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6": "Uniswap v2"},
        v3_factories={
            "0x33128a8fC17869897dcE68Ed026d694621f6FDfD": "Uniswap v3",
            PANCAKE_V3_FACTORY: "PancakeSwap v3",
        },
        v4_pool_managers={"0x498581fF718922c3f8e6A244956aF099B2652b2b": "Uniswap v4"},
        solidly_factories={"0x420DD381b31aEf6683db6B902084cB0FFECe40Da": "Aerodrome"},
        slipstream_factories={"0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A": "Aerodrome CL"},
        quote_tokens={
            "0x4200000000000000000000000000000000000006": "WETH",
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": "USDC",
        },
    ),
    _evm(
        key="arbitrum", label="Arbitrum", dexscreener="arbitrum", chain_id=42161, block_time=0.25, defined="arb",
        env_prefix="ARBITRUM",
        v2_factories={
            "0xf1D7CC64Fb4452F05c498126312eBE29f30Fbcf9": "Uniswap v2",
            "0xc35DADB65012eC5796536bD9864eD8773aBc74C4": "SushiSwap",
        },
        v3_factories={
            "0x1F98431c8aD98523631AE4a59f267346ea31F984": "Uniswap v3",
            PANCAKE_V3_FACTORY: "PancakeSwap v3",
        },
        v4_pool_managers={"0x360E68faCcca8cA495c1B759Fd9EEe466db9FB32": "Uniswap v4"},
        quote_tokens={
            "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1": "WETH",
            "0xaf88d065e77c8cC2239327C5EDb3A432268e5831": "USDC",
            "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9": "USDT",
        },
    ),
    # EVM chains scanned through DexScreener only (add factories + env_prefix
    # above to get real-time on-chain detection on them too).
    _evm(key="polygon", label="Polygon", dexscreener="polygon", native_symbol="POL"),
    _evm(key="avalanche", label="Avalanche", dexscreener="avalanche", native_symbol="AVAX"),
    _evm(key="optimism", label="Optimism", dexscreener="optimism"),
    _evm(key="unichain", label="Unichain", dexscreener="unichain"),
    _evm(key="sonic", label="Sonic", dexscreener="sonic"),
    _evm(key="abstract", label="Abstract", dexscreener="abstract"),
    _evm(key="hyperevm", label="HyperEVM", dexscreener="hyperevm"),
    _evm(key="monad", label="Monad", dexscreener="monad", gmgn="monad"),
    _evm(key="linea", label="Linea", dexscreener="linea"),
    _evm(key="blast", label="Blast", dexscreener="blast"),
    _evm(key="mantle", label="Mantle", dexscreener="mantle"),
    _evm(key="berachain", label="Berachain", dexscreener="berachain"),
    _evm(key="zksync", label="zkSync", dexscreener="zksync"),
]

CHAINS: dict[str, Chain] = {c.key: c for c in _CHAINS}
CHAIN_ORDER: list[str] = [c.key for c in _CHAINS]
EVM_KEYS: list[str] = [c.key for c in _CHAINS if c.is_evm]
DEXSCREENER_TO_CHAIN: dict[str, str] = {c.dexscreener: c.key for c in _CHAINS}

# Solana quote mints (never the "new" token).
SOLANA_QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}


def chain_label(key: str) -> str:
    c = CHAINS.get(key)
    return c.label if c else key


def norm_address(chain_key: str, address: str | None) -> str | None:
    """EVM addresses/pool ids are case-insensitive; Solana base58 is not."""
    if address is None:
        return None
    c = CHAINS.get(chain_key)
    if c is not None and c.is_evm:
        return address.lower()
    return address


# Solana launchpad programs watched through an RPC websocket (Helius or any
# Solana RPC with logsSubscribe). Value: (display name, instructions that mean
# "a token was created" -> the transaction is fetched to read name/symbol when
# the program's logs do not already contain them).
# pump.fun / bonk.fun are also covered for free by PumpPortal.
SOLANA_LAUNCHPAD_PROGRAMS: dict[str, tuple[str, frozenset[str]]] = {
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": (
        "LaunchLab (bonk.fun / StonkFun)",
        frozenset({"Initialize", "InitializeV2", "InitializeWithToken2022"}),
    ),
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": (
        "Meteora DBC (Bags / Believe / Jup Studio…)",
        frozenset({"InitializeVirtualPoolWithSplToken", "InitializeVirtualPoolWithToken2022"}),
    ),
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG": ("Moonshot", frozenset({"TokenMint"})),
    "boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4": ("Boop", frozenset({"CreateToken", "CreateTokenFallback"})),
}
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPFUN_CREATE_IXS = frozenset({"Create", "CreateV2"})
