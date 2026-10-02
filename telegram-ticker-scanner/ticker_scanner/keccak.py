"""Pure-Python Keccak-256 (Ethereum flavour, 0x01 padding).

Used for event topics, function selectors and Uniswap v4 storage slots, so the
scanner does not need pycryptodome / eth-hash.
"""

_MASK = (1 << 64) - 1

_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

# _ROT[x][y]
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

_RATE = 136  # bytes, for a 256-bit output


def _rol(v: int, n: int) -> int:
    n %= 64
    return ((v << n) | (v >> (64 - n))) & _MASK if n else v


def _permute(a: list[int]) -> list[int]:
    for rc in _RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [a[i] ^ d[i % 5] for i in range(25)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _ROT[x][y])
        a = [
            b[i] ^ ((~b[(i % 5 + 1) % 5 + 5 * (i // 5)] & _MASK) & b[(i % 5 + 2) % 5 + 5 * (i // 5)])
            for i in range(25)
        ]
        a[0] ^= rc
    return a


def _sponge(data: bytes, pad_byte: int) -> bytes:
    msg = bytearray(data)
    msg.append(pad_byte)
    while len(msg) % _RATE:
        msg.append(0)
    msg[-1] |= 0x80
    state = [0] * 25
    for off in range(0, len(msg), _RATE):
        block = msg[off:off + _RATE]
        for i in range(_RATE // 8):
            state[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        state = _permute(state)
    out = b"".join(state[i].to_bytes(8, "little") for i in range(4))
    return out[:32]


def keccak256(data: bytes) -> bytes:
    return _sponge(data, 0x01)


def keccak_hex(text: str) -> str:
    return "0x" + keccak256(text.encode()).hex()


def selector(signature: str) -> str:
    """4-byte function selector, e.g. selector('symbol()') == '0x95d89b41'."""
    return keccak_hex(signature)[:10]
