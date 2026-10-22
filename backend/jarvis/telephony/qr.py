"""Tiny dependency-free QR code encoder (byte mode, error-correction level M, versions 1-10) -> SVG.

Enough for a SIP address like sip:jarvis@192.168.137.1:5062 so the dashboard can show a scannable code offline.
Algorithm follows ISO/IEC 18004 (structure modelled on Project Nayuki's reference implementation).
"""
from __future__ import annotations

# Level M tables, index = version (0 unused)
_ECC_PER_BLOCK = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
_NUM_BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
_FORMAT_ECL_M = 0


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver: int) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    n = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    return [6] + sorted(size - 7 - i * step for i in range(n - 1))


_MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


class QR:
    def __init__(self, text: str, version: int | None = None, mask: int | None = None):
        data = text.encode("utf-8")
        if version is None:
            version = next((v for v in range(1, 11) if len(data) + 2 <= _data_codewords(v)), None)
            if version is None:
                raise ValueError("text too long for this QR encoder")
        self.version, self.size = version, version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()
        self._draw_codewords(self._add_ecc(self._encode(data)))
        if mask is None:
            best = None
            for m in range(8):
                self._apply_mask(m)
                self._draw_format(m)
                p = self._penalty()
                if best is None or p < best[0]:
                    best = (p, m)
                self._apply_mask(m)  # XOR again to undo
            mask = best[1]
        self.mask = mask
        self._apply_mask(mask)
        self._draw_format(mask)

    # ---- encoding ----
    def _encode(self, data: bytes) -> list[int]:
        cap = _data_codewords(self.version)
        bits: list[int] = []

        def put(val: int, n: int) -> None:
            bits.extend((val >> i) & 1 for i in reversed(range(n)))
        put(0b0100, 4)
        put(len(data), 8 if self.version < 10 else 16)
        for b in data:
            put(b, 8)
        put(0, min(4, cap * 8 - len(bits)))
        put(0, (-len(bits)) % 8)
        out = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
        pad = 0xEC
        while len(out) < cap:
            out.append(pad)
            pad ^= 0xEC ^ 0x11
        return out

    def _add_ecc(self, data: list[int]) -> list[int]:
        nblocks, ecc_len = _NUM_BLOCKS[self.version], _ECC_PER_BLOCK[self.version]
        raw = _raw_modules(self.version) // 8
        nshort = nblocks - raw % nblocks
        short_len = raw // nblocks
        div = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(nblocks):
            n = short_len - ecc_len + (0 if i < nshort else 1)
            dat = data[k:k + n]
            k += n
            ecc = _rs_remainder(dat, div)
            if i < nshort:
                dat = dat + [0]
            blocks.append(dat + ecc)
        out = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != short_len - ecc_len or j >= nshort:
                    out.append(blk[i])
        return out

    # ---- drawing ----
    def _set_fn(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _draw_function_patterns(self) -> None:
        s = self.size
        for i in range(s):
            self._set_fn(6, i, i % 2 == 0)
            self._set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self._set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.version)
        last = len(pos) - 1
        for i, px in enumerate(pos):
            for j, py in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set_fn(px + dx, py + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)  # reserve the area; real bits drawn later
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = bool((bits >> i) & 1)
                a, b = s - 11 + i % 3, i // 3
                self._set_fn(a, b, bit)
                self._set_fn(b, a, bit)

    def _draw_format(self, mask: int) -> None:
        data = _FORMAT_ECL_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: bool((bits >> i) & 1)  # noqa: E731
        s = self.size
        for i in range(6):
            self._set_fn(8, i, bit(i))
        self._set_fn(8, 7, bit(6))
        self._set_fn(8, 8, bit(7))
        self._set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self._set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self._set_fn(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set_fn(8, s - 15 + i, bit(i))
        self._set_fn(8, s - 8, True)

    def _draw_codewords(self, data: list[int]) -> None:
        s, i = self.size, 0
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = s - 1 - vert if upward else vert
                    if not self.function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = bool((data[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def _apply_mask(self, mask: int) -> None:
        f = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and f(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self) -> int:
        """Simplified ISO penalty (runs, 2x2 blocks, finder-like patterns, balance). Any mask decodes; this
        just picks a clean-looking one."""
        s, m, p = self.size, self.modules, 0
        for lines in (m, [list(col) for col in zip(*m)]):
            for row in lines:
                run, prev = 0, None
                for v in row:
                    if v == prev:
                        run += 1
                    else:
                        if run >= 5:
                            p += run - 2
                        run, prev = 1, v
                if run >= 5:
                    p += run - 2
                text = "".join("1" if v else "0" for v in row)
                p += 40 * (text.count("10111010000") + text.count("00001011101"))
        for y in range(s - 1):
            for x in range(s - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    p += 3
        dark = sum(v for row in m for v in row)
        p += 10 * (abs(dark * 20 - s * s * 10) // (s * s))
        return p

    def svg(self, border: int = 4, scale: int = 8) -> str:
        n = self.size + 2 * border
        path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y in range(self.size) for x in range(self.size)
                       if self.modules[y][x])
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * scale}" height="{n * scale}" '
                f'shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#ffffff"/>'
                f'<path d="{path}" fill="#000000"/></svg>')


def qr_svg(text: str) -> str:
    return QR(text).svg()
