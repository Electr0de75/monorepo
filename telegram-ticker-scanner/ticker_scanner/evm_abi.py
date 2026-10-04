"""Minimal ABI helpers for the few events/calls the scanner needs."""

from __future__ import annotations

from .keccak import keccak256, keccak_hex, selector

ZERO_ADDRESS = "0x" + "0" * 40
ZERO_TOPIC = "0x" + "0" * 64

TRANSFER = keccak_hex("Transfer(address,address,uint256)")
V2_PAIR_CREATED = keccak_hex("PairCreated(address,address,address,uint256)")
V3_POOL_CREATED = keccak_hex("PoolCreated(address,address,uint24,int24,address)")
V4_INITIALIZE = keccak_hex("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
V4_MODIFY_LIQUIDITY = keccak_hex("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
SOLIDLY_POOL_CREATED = keccak_hex("PoolCreated(address,address,bool,address,uint256)")
SLIPSTREAM_POOL_CREATED = keccak_hex("PoolCreated(address,address,int24,address)")

V2_MINT = keccak_hex("Mint(address,uint256,uint256)")
V3_MINT = keccak_hex("Mint(address,address,int24,int24,uint128,uint256,uint256)")

SEL_SYMBOL = selector("symbol()")
SEL_NAME = selector("name()")
SEL_BALANCE_OF = selector("balanceOf(address)")
SEL_TOKEN0 = selector("token0()")
SEL_TOKEN1 = selector("token1()")
SEL_FACTORY = selector("factory()")
SEL_GET_RESERVES = selector("getReserves()")
SEL_POOL_KEYS = selector("poolKeys(bytes25)")      # v4 PositionManager
SEL_EXTSLOAD = selector("extsload(bytes32)")       # v4 PoolManager raw storage read

# Uniswap v4 PoolManager storage (v4-core StateLibrary): pools[poolId] lives at
# keccak256(poolId . POOLS_SLOT); slot0 at +0 (sqrtPriceX96 in the low 160 bits),
# active liquidity at +3 (uint128).
V4_POOLS_SLOT = 6
V4_LIQUIDITY_OFFSET = 3

# Uniswap v2 (and forks) burn MINIMUM_LIQUIDITY LP tokens to the zero address on
# the very first liquidity of a pair: a unique on-chain fingerprint.
MINIMUM_LIQUIDITY = 1000


def _hex_bytes(data) -> bytes:
    """Hex string -> bytes; anything malformed (from an RPC node or a log) reads as empty."""
    if not isinstance(data, str) or data in ("", "0x"):
        return b""
    try:
        return bytes.fromhex(data[2:] if data.startswith("0x") else data)
    except ValueError:
        return b""


def words(data: str | None) -> list[bytes]:
    raw = _hex_bytes(data)
    return [raw[i:i + 32] for i in range(0, len(raw) - len(raw) % 32, 32)]


def word_uint(data: str | None, index: int) -> int:
    w = words(data)
    return int.from_bytes(w[index], "big") if index < len(w) else 0


def word_int(data: str | None, index: int) -> int:
    v = word_uint(data, index)
    return v - (1 << 256) if v >= 1 << 255 else v


def word_address(data: str | None, index: int) -> str | None:
    w = words(data)
    if index >= len(w):
        return None
    return "0x" + w[index][12:].hex()


def topic_address(topic) -> str:
    if not isinstance(topic, str):
        return ZERO_ADDRESS
    return "0x" + topic[-40:].lower()


def address_topic(address: str) -> str:
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def encode_address_call(sel: str, address: str) -> str:
    return sel + address.lower().replace("0x", "").rjust(64, "0")


def decode_string_result(result: str | None) -> str | None:
    """Decode the return value of name()/symbol(): ABI string or bytes32."""
    raw = _hex_bytes(result)
    if not raw:
        return None
    try:
        if len(raw) >= 64:
            offset = int.from_bytes(raw[:32], "big")
            if offset + 32 <= len(raw):
                length = int.from_bytes(raw[offset:offset + 32], "big")
                if offset + 32 + length <= len(raw) and length <= 256:
                    return raw[offset + 32:offset + 32 + length].decode("utf-8", "replace").strip("\x00").strip()
        if len(raw) == 32:  # bytes32 (old tokens like MKR)
            return raw.rstrip(b"\x00").decode("utf-8", "replace").strip()
    except (ValueError, UnicodeDecodeError):
        return None
    return None



def v4_state_slot(pool_id: str) -> int:
    raw = _hex_bytes(pool_id).rjust(32, b"\x00")[-32:] + V4_POOLS_SLOT.to_bytes(32, "big")
    return int.from_bytes(keccak256(raw), "big")


def encode_extsload(slot: int) -> str:
    return SEL_EXTSLOAD + (slot % (1 << 256)).to_bytes(32, "big").hex()


def encode_pool_keys(pool_id: str) -> str:
    """poolKeys(bytes25): the first 25 bytes of the pool id, left-aligned."""
    raw = _hex_bytes(pool_id).rjust(32, b"\x00")[-32:]
    return SEL_POOL_KEYS + (raw[:25] + b"\x00" * 7).hex()
