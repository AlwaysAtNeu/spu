"""Hypervector arithmetic shared by the simulator, compiler and host runtime.

A hypervector (HV) is represented as a Python int of D bits; bit i of the int is
component i of the HV.  In memory an HV is stored as D/8 little-endian bytes, so
`int.from_bytes(mem, "little")` and `x.to_bytes(D//8, "little")` are the exact
conversions the hardware uses (AXI byte lane b of beat c holds bits [8b+7:8b] of
chunk c; chunk c holds bits [c*W + W-1 : c*W]).

The pseudo-random generator is fully specified so that host, simulator and RTL
produce bit-identical symbols from the same 32-bit seed.
"""
import numpy as np

M64 = (1 << 64) - 1
GEN_GOLDEN = 0x9E3779B97F4A7C15
GEN_MUL1 = 0xBF58476D1CE4E5B9
GEN_MUL2 = 0x94D049BB133111EB


def smix64(z: int) -> int:
    """splitmix64 finaliser (pure function on 64-bit ints)."""
    z &= M64
    z = ((z ^ (z >> 30)) * GEN_MUL1) & M64
    z = ((z ^ (z >> 27)) * GEN_MUL2) & M64
    return z ^ (z >> 31)


def gen_word(seed32: int, w: int) -> int:
    """64-bit word w of the HV generated from seed32."""
    return smix64((((seed32 & 0xFFFFFFFF) << 32) | (w & 0xFFFFFFFF)) ^ GEN_GOLDEN)


def hgen(seed32: int, D: int) -> int:
    """Generate the deterministic pseudo-random HV for a 32-bit seed (vectorised)."""
    nw = D // 64
    seed32 &= 0xFFFFFFFF
    w = np.arange(nw, dtype=np.uint64)
    with np.errstate(over="ignore"):
        z = ((np.uint64(seed32) << np.uint64(32)) | w) ^ np.uint64(GEN_GOLDEN)
        z = (z ^ (z >> np.uint64(30))) * np.uint64(GEN_MUL1)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(GEN_MUL2)
        z = z ^ (z >> np.uint64(31))
    return int.from_bytes(z.astype("<u8").tobytes(), "little")


def mask(D: int) -> int:
    return (1 << D) - 1


def rotl(x: int, k: int, D: int) -> int:
    """Rotate left by k (component i moves to (i + k) mod D)."""
    k %= D
    if k == 0:
        return x & mask(D)
    return ((x << k) | (x >> (D - k))) & mask(D)


def rotr(x: int, k: int, D: int) -> int:
    return rotl(x, (-k) % D, D)


def hmask(n: int, D: int) -> int:
    """Low n components set (clamped to [0, D])."""
    n = max(0, min(D, n))
    return (1 << n) - 1


def popcount(x: int) -> int:
    return bin(x).count("1")


def hamming(a: int, b: int) -> int:
    return popcount(a ^ b)


def similarity(a: int, b: int, D: int) -> float:
    """Normalised similarity in [-1, 1] (1 = identical, 0 = orthogonal)."""
    return 1.0 - 2.0 * hamming(a, b) / D


def to_bytes(x: int, D: int) -> bytes:
    return (x & mask(D)).to_bytes(D // 8, "little")


def from_bytes(b: bytes) -> int:
    return int.from_bytes(b, "little")


def to_bits(x: int, D: int) -> np.ndarray:
    """uint8 array of shape (D,), element i = component i."""
    return np.unpackbits(np.frombuffer(to_bytes(x, D), dtype=np.uint8), bitorder="little")


def from_bits(bits: np.ndarray) -> int:
    return int.from_bytes(np.packbits(bits.astype(np.uint8), bitorder="little").tobytes(), "little")


def to_pm1(x: int, D: int) -> np.ndarray:
    """int16 array, +1 where the component is 1 and -1 where it is 0."""
    return (to_bits(x, D).astype(np.int16) * 2) - 1


def bundle(hvs, D: int, tie_break: int = None) -> int:
    """Majority bundle.  For an even count a tie-break HV must be supplied (the
    compiler uses a dedicated random symbol), otherwise ties resolve to 0 which is
    exactly what the accumulator + threshold hardware does."""
    hvs = list(hvs)
    if tie_break is not None and len(hvs) % 2 == 0:
        hvs.append(tie_break)
    acc = np.zeros(D, dtype=np.int32)
    for h in hvs:
        acc += to_pm1(h, D)
    return from_bits(acc > 0)


def random_hv(D: int, rng: np.random.Generator) -> int:
    return int.from_bytes(rng.bytes(D // 8), "little")


def sat16(a: np.ndarray) -> np.ndarray:
    return np.clip(a, -32768, 32767).astype(np.int16)
