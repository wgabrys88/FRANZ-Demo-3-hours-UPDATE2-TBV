"""
capture.py — FRANZ screenshot producer.

Produces a screenshot (real desktop or persistent sandbox canvas), applies
sandbox drawings (white, persistent) and visual marks (red, ephemeral),
resizes to output dimensions, and returns base64 PNG + applied action list
to execute.py via stdout JSON.
"""

from __future__ import annotations

import ast
import base64
import ctypes
import ctypes.wintypes
import json
import math
import struct
import sys
import zlib
from datetime import datetime
from pathlib import Path
from typing import Final

Color = tuple[int, int, int, int]
Point = tuple[int, int]

MARK_SCALE: Final = 1.8
_SRCCOPY: Final = 0x00CC0020
_CAPTUREBLT: Final = 0x40000000
_BI_RGB: Final = 0
_DIB_RGB: Final = 0
_HALFTONE: Final = 4

MARK_FILL: Final[Color] = (255, 0, 0, 200)
MARK_OUTLINE: Final[Color] = (255, 255, 255, 240)
MARK_TEXT: Final[Color] = (255, 255, 255, 255)
TRAIL_COLOR: Final[Color] = (255, 0, 0, 80)
SANDBOX_WHITE: Final[Color] = (255, 255, 255, 255)
BLACK: Final[Color] = (0, 0, 0, 255)

SANDBOX_CANVAS: Final = Path(__file__).with_name("sandbox_canvas.bmp")
SANDBOX_STATE: Final = Path(__file__).with_name("sandbox_state.json")

ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_screen_w: Final = _user32.GetSystemMetrics(0)
_screen_h: Final = _user32.GetSystemMetrics(1)


def _ms(base: int) -> int:
    return max(1, int(base * MARK_SCALE))


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD), ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG), ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD), ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD), ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG), ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.wintypes.DWORD * 3)]


def _make_bmi(w: int, h: int) -> _BITMAPINFO:
    bmi = _BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight = w, -h
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, _BI_RGB
    return bmi


def _capture_bgra(w: int, h: int) -> bytes:
    sdc = _user32.GetDC(0)
    memdc = _gdi32.CreateCompatibleDC(sdc)
    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(sdc, ctypes.byref(_make_bmi(w, h)), _DIB_RGB, ctypes.byref(bits), None, 0)
    old = _gdi32.SelectObject(memdc, hbmp)
    try:
        _gdi32.BitBlt(memdc, 0, 0, w, h, sdc, 0, 0, _SRCCOPY | _CAPTUREBLT)
        return bytes((ctypes.c_ubyte * (w * h * 4)).from_address(bits.value))
    finally:
        _gdi32.SelectObject(memdc, old)
        _gdi32.DeleteObject(hbmp)
        _gdi32.DeleteDC(memdc)
        _user32.ReleaseDC(0, sdc)


def _resize_bgra(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes:
    sdc = _user32.GetDC(0)
    src_dc = _gdi32.CreateCompatibleDC(sdc)
    dst_dc = _gdi32.CreateCompatibleDC(sdc)
    src_bmp = _gdi32.CreateCompatibleBitmap(sdc, sw, sh)
    old_src = _gdi32.SelectObject(src_dc, src_bmp)
    dst_bits = ctypes.c_void_p()
    dst_bmp = _gdi32.CreateDIBSection(sdc, ctypes.byref(_make_bmi(dw, dh)), _DIB_RGB, ctypes.byref(dst_bits), None, 0)
    old_dst = _gdi32.SelectObject(dst_dc, dst_bmp)
    try:
        _gdi32.SetDIBits(sdc, src_bmp, 0, sh, src, ctypes.byref(_make_bmi(sw, sh)), _DIB_RGB)
        _gdi32.SetStretchBltMode(dst_dc, _HALFTONE)
        _gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
        _gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, sw, sh, _SRCCOPY)
        return bytes((ctypes.c_ubyte * (dw * dh * 4)).from_address(dst_bits.value))
    finally:
        _gdi32.SelectObject(dst_dc, old_dst)
        _gdi32.SelectObject(src_dc, old_src)
        _gdi32.DeleteObject(dst_bmp)
        _gdi32.DeleteObject(src_bmp)
        _gdi32.DeleteDC(dst_dc)
        _gdi32.DeleteDC(src_dc)
        _user32.ReleaseDC(0, sdc)


def _bgra_to_rgba(bgra: bytes) -> bytearray:
    n = len(bgra)
    out = bytearray(n)
    out[0::4], out[1::4], out[2::4], out[3::4] = bgra[2::4], bgra[1::4], bgra[0::4], b"\xff" * (n // 4)
    return out


def _rgba_to_bgra(rgba: bytes) -> bytes:
    n = len(rgba)
    out = bytearray(n)
    out[0::4], out[1::4], out[2::4], out[3::4] = rgba[2::4], rgba[1::4], rgba[0::4], b"\xff" * (n // 4)
    return bytes(out)


def _encode_png(rgba: bytes, w: int, h: int) -> bytes:
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


class Canvas:
    __slots__ = ("buf", "w", "h")

    def __init__(self, buf: bytearray, w: int, h: int) -> None:
        self.buf, self.w, self.h = buf, w, h

    def put(self, x: int, y: int, c: Color) -> None:
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        i = (y * self.w + x) << 2
        sa = c[3]
        if sa >= 255:
            self.buf[i], self.buf[i+1], self.buf[i+2], self.buf[i+3] = c[0], c[1], c[2], 255
            return
        da = 255 - sa
        self.buf[i]   = (c[0] * sa + self.buf[i]   * da) // 255
        self.buf[i+1] = (c[1] * sa + self.buf[i+1] * da) // 255
        self.buf[i+2] = (c[2] * sa + self.buf[i+2] * da) // 255
        self.buf[i+3] = 255

    def put_opaque(self, x: int, y: int, c: Color) -> None:
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        i = (y * self.w + x) << 2
        self.buf[i], self.buf[i+1], self.buf[i+2], self.buf[i+3] = c[0], c[1], c[2], 255

    def _thick(self, x: int, y: int, c: Color, t: int, opaque: bool) -> None:
        half = t >> 1
        fn = self.put_opaque if opaque else self.put
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                fn(x + dx, y + dy, c)

    def _bresenham(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int, opaque: bool) -> None:
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        sx, sy = (1 if x1 < x2 else -1), (1 if y1 < y2 else -1)
        err, x, y = dx - dy, x1, y1
        while True:
            self._thick(x, y, c, t, opaque)
            if x == x2 and y == y2:
                break
            e2 = err << 1
            if e2 > -dy:
                err -= dy; x += sx
            if e2 < dx:
                err += dx; y += sy

    def line(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int) -> None:
        self._bresenham(x1, y1, x2, y2, c, t, False)

    def line_opaque(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int) -> None:
        self._bresenham(x1, y1, x2, y2, c, t, True)

    def circle_opaque(self, cx: int, cy: int, r: int, c: Color) -> None:
        r2 = r * r
        for oy in range(-r, r + 1):
            for ox in range(-r, r + 1):
                if ox * ox + oy * oy <= r2:
                    self.put_opaque(cx + ox, cy + oy, c)

    def rect_opaque(self, x: int, y: int, w: int, h: int, c: Color) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.put_opaque(xx, yy, c)

    def circle(self, cx: int, cy: int, r: int, c: Color, filled: bool, thickness: int) -> None:
        r2o, r2i = r * r, max(0, r - thickness) ** 2
        for oy in range(-r, r + 1):
            for ox in range(-r, r + 1):
                d2 = ox * ox + oy * oy
                if (filled and d2 <= r2o) or (not filled and r2i <= d2 <= r2o):
                    self.put(cx + ox, cy + oy, c)

    def fill_polygon(self, pts: list[Point], c: Color) -> None:
        if len(pts) < 3:
            return
        ys = [p[1] for p in pts]
        n = len(pts)
        for y in range(max(0, min(ys)), min(self.h - 1, max(ys)) + 1):
            nodes: list[int] = []
            j = n - 1
            for i in range(n):
                yi, yj = pts[i][1], pts[j][1]
                if (yi < y <= yj) or (yj < y <= yi):
                    nodes.append(int(pts[i][0] + (y - yi) / (yj - yi) * (pts[j][0] - pts[i][0])))
                j = i
            nodes.sort()
            for k in range(0, len(nodes) - 1, 2):
                for x in range(max(0, nodes[k]), min(self.w - 1, nodes[k + 1]) + 1):
                    self.put(x, y, c)

    def arrow(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int) -> None:
        self.line(x1, y1, x2, y2, c, t)
        ang = math.atan2(y2 - y1, x2 - x1)
        ha, ln = math.radians(25.0), float(_ms(28))
        self.fill_polygon([
            (x2, y2),
            (int(x2 - ln * math.cos(ang - ha)), int(y2 - ln * math.sin(ang - ha))),
            (int(x2 - ln * math.cos(ang + ha)), int(y2 - ln * math.sin(ang + ha))),
        ], c)


_FONT_5X7: Final[dict[str, list[int]]] = {
    " ": [0, 0, 0, 0, 0, 0, 0],
    "0": [0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110],
    "1": [0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    "2": [0b01110, 0b10001, 0b00001, 0b00110, 0b01000, 0b10000, 0b11111],
    "3": [0b01110, 0b10001, 0b00001, 0b00110, 0b00001, 0b10001, 0b01110],
    "4": [0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010],
    "5": [0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110],
    "6": [0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110],
    "7": [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000],
    "8": [0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110],
    "9": [0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100],
    "A": [0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    "B": [0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110],
    "C": [0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110],
    "D": [0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110],
    "E": [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111],
    "F": [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000],
    "G": [0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01110],
    "H": [0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    "I": [0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    "J": [0b00111, 0b00010, 0b00010, 0b00010, 0b10010, 0b10010, 0b01100],
    "K": [0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001],
    "L": [0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111],
    "M": [0b10001, 0b11011, 0b10101, 0b10001, 0b10001, 0b10001, 0b10001],
    "N": [0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001],
    "O": [0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    "P": [0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000],
    "Q": [0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101],
    "R": [0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001],
    "S": [0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110],
    "T": [0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100],
    "U": [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    "V": [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100],
    "W": [0b10001, 0b10001, 0b10001, 0b10001, 0b10101, 0b11011, 0b10001],
    "X": [0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001],
    "Y": [0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100],
    "Z": [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111],
    ".": [0, 0, 0, 0, 0, 0b00100, 0b00100],
    ",": [0, 0, 0, 0, 0b00100, 0b00100, 0b01000],
    "!": [0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0, 0b00100],
    "?": [0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0, 0b00100],
    "-": [0, 0, 0, 0b11111, 0, 0, 0],
    ":": [0, 0b00100, 0b00100, 0, 0b00100, 0b00100, 0],
    "/": [0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0, 0],
}
_DIGITS: Final = [_FONT_5X7[str(d)] for d in range(10)]


def _draw_text(cv: Canvas, x: int, y: int, text: str, c: Color, scale: int) -> None:
    px, py = x, y
    for ch in text:
        if ch == "\n":
            py += 8 * scale; px = x; continue
        pat = _FONT_5X7.get(ch.upper())
        if pat is None:
            cv.rect_opaque(px, py, 5 * scale, 7 * scale, c)
        else:
            for row in range(7):
                bits = pat[row]
                for col in range(5):
                    if bits & (1 << (4 - col)):
                        for sy in range(scale):
                            for sx in range(scale):
                                cv.put_opaque(px + col * scale + sx, py + row * scale + sy, c)
        px += 6 * scale


def _render_digit(cv: Canvas, cx: int, cy: int, d: int, fill: Color, outline: Color, scale: int) -> None:
    gw, gh = 5 * scale, 7 * scale
    ox, oy = cx - gw // 2, cy - gh // 2
    g = _DIGITS[d]
    for ddy in (-1, 0, 1):
        for ddx in (-1, 0, 1):
            if ddx == 0 and ddy == 0:
                continue
            for ri, row in enumerate(g):
                for ci in range(5):
                    if row & (1 << (4 - ci)):
                        for sy in range(scale):
                            for sx in range(scale):
                                cv.put_opaque(ox + ci*scale + sx + ddx*2, oy + ri*scale + sy + ddy*2, outline)
    for ri, row in enumerate(g):
        for ci in range(5):
            if row & (1 << (4 - ci)):
                for sy in range(scale):
                    for sx in range(scale):
                        cv.put_opaque(ox + ci*scale + sx, oy + ri*scale + sy, fill)


def _render_number(cv: Canvas, cx: int, cy: int, n: int, fill: Color, outline: Color, scale: int) -> None:
    s = str(n)
    gw, gap = 5 * scale, scale
    tw = len(s) * gw + (len(s) - 1) * gap
    start = cx - tw // 2 + gw // 2
    for i, ch in enumerate(s):
        _render_digit(cv, start + i * (gw + gap), cy, int(ch), fill, outline, scale)


def _parse_action(line: str) -> tuple[str, list[object], dict[str, object]] | None:
    s = line.strip()
    if not s:
        return None
    try:
        node = ast.parse(s, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    args: list[object] = []
    for a in node.args:
        if not isinstance(a, ast.Constant):
            return None
        args.append(a.value)
    kwargs: dict[str, object] = {}
    for kw in node.keywords:
        if kw.arg is None or not isinstance(kw.value, ast.Constant):
            return None
        kwargs[kw.arg] = kw.value.value
    return node.func.id, args, kwargs


def _arg_int(args: list[object], kw: dict[str, object], idx: int, key: str) -> int | None:
    v = args[idx] if idx < len(args) else kw.get(key)
    if v is None:
        return None
    try:
        return int(v)  # type: ignore[arg-type]
    except Exception:
        return None


def _arg_str(args: list[object], kw: dict[str, object], idx: int, key: str) -> str | None:
    v = args[idx] if idx < len(args) else kw.get(key)
    return str(v) if v is not None else None


def _norm(v: int, extent: int) -> int:
    return int((max(0, min(1000, v)) / 1000.0) * extent)


def _bmp_write_black(path: Path, w: int, h: int) -> None:
    stride = ((w * 3 + 3) // 4) * 4
    si = stride * h
    hdr = struct.pack("<2sIHHI", b"BM", 54 + si, 0, 0, 54)
    ihdr = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, si, 2835, 2835, 0, 0)
    row = b"\x00" * stride
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(hdr + ihdr + row * h)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _bmp_load_rgba(path: Path, w: int, h: int) -> bytearray:
    try:
        data = path.read_bytes()
        if len(data) < 54 or data[:2] != b"BM":
            return bytearray()
        off = struct.unpack_from("<I", data, 10)[0]
        if struct.unpack_from("<I", data, 14)[0] < 40:
            return bytearray()
        bw, bh = struct.unpack_from("<ii", data, 18)
        planes, bpp = struct.unpack_from("<HH", data, 26)
        comp = struct.unpack_from("<I", data, 30)[0]
        if planes != 1 or comp != 0 or bpp not in (24, 32):
            return bytearray()
        ah = abs(bh)
        if bw != w or ah != h:
            return bytearray()
        bytespp = bpp // 8
        stride = ((w * bytespp + 3) // 4) * 4
        if len(data) < off + stride * h:
            return bytearray()
        out = bytearray(w * h * 4)
        top_down = bh < 0
        for y in range(h):
            sy = y if top_down else (h - 1 - y)
            row = data[off + sy * stride: off + (sy + 1) * stride]
            di = y * w * 4
            for x in range(w):
                si2 = x * bytespp
                out[di + x*4], out[di + x*4+1], out[di + x*4+2], out[di + x*4+3] = row[si2+2], row[si2+1], row[si2], 255
        return out
    except Exception:
        return bytearray()


def _bmp_save_rgba(path: Path, buf: bytes, w: int, h: int) -> None:
    stride = ((w * 3 + 3) // 4) * 4
    si = stride * h
    pad = b"\x00" * (stride - w * 3)
    out = bytearray()
    out.extend(struct.pack("<2sIHHI", b"BM", 54 + si, 0, 0, 54))
    out.extend(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, si, 2835, 2835, 0, 0))
    for y in range(h - 1, -1, -1):
        row = buf[y * w * 4: (y + 1) * w * 4]
        for x in range(w):
            i = x * 4
            out.append(row[i+2]); out.append(row[i+1]); out.append(row[i])
        out.extend(pad)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(bytes(out))
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _sandbox_state_load(reset: bool) -> dict[str, int | None]:
    if reset:
        return {"last_x": None, "last_y": None}
    try:
        o = json.loads(SANDBOX_STATE.read_text(encoding="utf-8"))
        lx, ly = o.get("last_x"), o.get("last_y")
        if isinstance(lx, int) and isinstance(ly, int):
            return {"last_x": lx, "last_y": ly}
    except Exception:
        pass
    return {"last_x": None, "last_y": None}


def _sandbox_state_save(st: dict[str, int | None]) -> None:
    tmp = SANDBOX_STATE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st), encoding="utf-8")
        tmp.replace(SANDBOX_STATE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _sandbox_load(w: int, h: int, reset: bool) -> bytearray:
    if reset:
        _bmp_write_black(SANDBOX_CANVAS, w, h)
        _sandbox_state_save({"last_x": None, "last_y": None})
    if not SANDBOX_CANVAS.is_file():
        _bmp_write_black(SANDBOX_CANVAS, w, h)
    buf = _bmp_load_rgba(SANDBOX_CANVAS, w, h)
    if not buf:
        _bmp_write_black(SANDBOX_CANVAS, w, h)
        return bytearray(b"\x00\x00\x00\xff" * (w * h))
    return buf


def _sandbox_apply(buf: bytearray, w: int, h: int, actions: list[str], reset: bool) -> tuple[bool, list[str]]:
    cv = Canvas(buf, w, h)
    dirty = False
    applied: list[str] = []
    st = _sandbox_state_load(reset)

    for line in actions:
        parsed = _parse_action(line)
        if parsed is None:
            continue
        name, args, kw = parsed
        if name == "click":
            name = "left_click"
        if name in ("screenshot", "timestamp"):
            continue

        if name == "drag":
            x1, y1 = _arg_int(args, kw, 0, "x1"), _arg_int(args, kw, 1, "y1")
            x2, y2 = _arg_int(args, kw, 2, "x2"), _arg_int(args, kw, 3, "y2")
            if None in (x1, y1, x2, y2):
                continue
            px1, py1 = _norm(x1, w), _norm(y1, h)  # type: ignore[arg-type]
            px2, py2 = _norm(x2, w), _norm(y2, h)  # type: ignore[arg-type]
            cv.line_opaque(px1, py1, px2, py2, SANDBOX_WHITE, 4)
            st["last_x"], st["last_y"] = px2, py2
            dirty = True; applied.append(line)
        elif name in ("left_click", "double_left_click"):
            x, y = _arg_int(args, kw, 0, "x"), _arg_int(args, kw, 1, "y")
            if x is None or y is None:
                continue
            px, py = _norm(x, w), _norm(y, h)
            cv.circle_opaque(px, py, 6, SANDBOX_WHITE)
            st["last_x"], st["last_y"] = px, py
            dirty = True; applied.append(line)
        elif name == "right_click":
            x, y = _arg_int(args, kw, 0, "x"), _arg_int(args, kw, 1, "y")
            if x is None or y is None:
                continue
            px, py = _norm(x, w), _norm(y, h)
            cv.rect_opaque(px - 6, py - 4, 12, 8, SANDBOX_WHITE)
            st["last_x"], st["last_y"] = px, py
            dirty = True; applied.append(line)
        elif name == "type":
            t = _arg_str(args, kw, 0, "text")
            if t is None:
                continue
            lx, ly = st.get("last_x"), st.get("last_y")
            if not isinstance(lx, int) or not isinstance(ly, int):
                continue
            cv.line_opaque(lx + 6, ly - 7, lx + 6, ly + 7, SANDBOX_WHITE, 2)
            _draw_text(cv, lx + 9, ly - 6, t, SANDBOX_WHITE, 2)
            dirty = True; applied.append(line)

    if dirty:
        _sandbox_state_save(st)
    return dirty, applied


def _apply_marks(buf: bytearray, w: int, h: int, actions: list[str]) -> None:
    cv = Canvas(buf, w, h)
    entries: list[tuple[str, list[object], dict[str, object]]] = []
    for line in actions:
        parsed = _parse_action(line)
        if parsed is None:
            continue
        name, args, kw = parsed
        if name == "click":
            name = "left_click"
        entries.append((name, args, kw))

    positions: list[Point | None] = []
    for name, args, kw in entries:
        if name in ("left_click", "right_click", "double_left_click"):
            x0, y0 = _arg_int(args, kw, 0, "x"), _arg_int(args, kw, 1, "y")
            positions.append((_norm(x0, w), _norm(y0, h)) if x0 is not None and y0 is not None else None)  # type: ignore[arg-type]
        elif name == "drag":
            x1, y1 = _arg_int(args, kw, 0, "x1"), _arg_int(args, kw, 1, "y1")
            positions.append((_norm(x1, w), _norm(y1, h)) if x1 is not None and y1 is not None else None)  # type: ignore[arg-type]
        else:
            positions.append(None)

    prev: Point | None = None
    trail_t = _ms(4)
    for i, (name, args, kw) in enumerate(entries):
        if name in ("timestamp", "screenshot", "type"):
            continue
        pos = positions[i]
        if pos and prev and abs(pos[0] - prev[0]) + abs(pos[1] - prev[1]) > _ms(30):
            cv.line(prev[0], prev[1], pos[0], pos[1], TRAIL_COLOR, trail_t)
        if pos:
            if name == "drag":
                x2, y2 = _arg_int(args, kw, 2, "x2"), _arg_int(args, kw, 3, "y2")
                prev = (_norm(x2, w), _norm(y2, h)) if x2 is not None and y2 is not None else pos  # type: ignore[arg-type]
            else:
                prev = pos

    n = 1
    lcp_x: int | None = None
    lcp_y: int | None = None
    ot = _ms(3)

    for _, (name, args, kw) in enumerate(entries):
        match name:
            case "left_click":
                x0, y0 = _arg_int(args, kw, 0, "x"), _arg_int(args, kw, 1, "y")
                if x0 is None or y0 is None:
                    continue
                x, y = _norm(x0, w), _norm(y0, h)
                cv.circle(x, y, _ms(32), MARK_OUTLINE, True, ot)
                cv.circle(x, y, _ms(28), MARK_FILL, True, ot)
                _render_number(cv, x, y, n, MARK_TEXT, BLACK, _ms(4))
                lcp_x, lcp_y = x, y; n += 1
            case "right_click":
                x0, y0 = _arg_int(args, kw, 0, "x"), _arg_int(args, kw, 1, "y")
                if x0 is None or y0 is None:
                    continue
                x, y = _norm(x0, w), _norm(y0, h)
                r = _ms(32)
                pts: list[Point] = [(x, y-r), (x+r, y), (x, y+r), (x-r, y)]
                cv.fill_polygon(pts, MARK_FILL)
                for a, b in zip(pts, pts[1:] + pts[:1]):
                    cv.line(a[0], a[1], b[0], b[1], MARK_OUTLINE, ot)
                _render_number(cv, x, y, n, MARK_TEXT, BLACK, _ms(3))
                lcp_x, lcp_y = x, y; n += 1
            case "double_left_click":
                x0, y0 = _arg_int(args, kw, 0, "x"), _arg_int(args, kw, 1, "y")
                if x0 is None or y0 is None:
                    continue
                x, y = _norm(x0, w), _norm(y0, h)
                rt = _ms(4)
                cv.circle(x, y, _ms(36), MARK_OUTLINE, False, rt)
                cv.circle(x, y, _ms(24), MARK_OUTLINE, False, rt)
                cv.circle(x, y, _ms(18), MARK_FILL, True, ot)
                _render_number(cv, x, y, n, MARK_TEXT, BLACK, _ms(3))
                lcp_x, lcp_y = x, y; n += 1
            case "drag":
                x1, y1 = _arg_int(args, kw, 0, "x1"), _arg_int(args, kw, 1, "y1")
                x2, y2 = _arg_int(args, kw, 2, "x2"), _arg_int(args, kw, 3, "y2")
                if None in (x1, y1, x2, y2):
                    continue
                px1, py1 = _norm(x1, w), _norm(y1, h)  # type: ignore[arg-type]
                px2, py2 = _norm(x2, w), _norm(y2, h)  # type: ignore[arg-type]
                cv.arrow(px1, py1, px2, py2, MARK_FILL, _ms(6))
                cv.circle(px1, py1, _ms(20), MARK_OUTLINE, True, ot)
                cv.circle(px1, py1, _ms(16), MARK_FILL, True, ot)
                _render_number(cv, px1, py1, n, MARK_TEXT, BLACK, _ms(3))
                cv.circle(px2, py2, _ms(20), MARK_OUTLINE, False, _ms(4))
                cv.circle(px2, py2, _ms(16), MARK_FILL, False, _ms(3))
                lcp_x, lcp_y = px2, py2; n += 1
            case "type":
                t = _arg_str(args, kw, 0, "text")
                if t is None or lcp_x is None or lcp_y is None:
                    continue
                tw = len(t) * 12
                ux, uy = lcp_x + _ms(9), lcp_y + _ms(10)
                cv.line(ux, uy, ux + tw, uy, MARK_FILL, _ms(4))
                cv.line(ux, uy + _ms(2), ux + tw, uy + _ms(2), MARK_OUTLINE, _ms(2))
                _render_number(cv, ux + tw + _ms(12), uy - _ms(4), n, MARK_TEXT, BLACK, _ms(3))
                n += 1
            case "timestamp" | "screenshot":
                stamp = datetime.now().strftime("CAPTURED %Y-%m-%d %H:%M:%S")
                sc = _ms(3)
                cw, ch = 6 * sc, 7 * sc
                stw = len(stamp) * cw
                tx, ty = (w - stw) // 2, int(h * 0.85)
                px2, py2 = _ms(8), _ms(6)
                backdrop: Color = (0, 0, 0, 140)
                for yy in range(ty - py2, ty + ch + py2):
                    for xx in range(tx - px2, tx + stw + px2):
                        cv.put(xx, yy, backdrop)
                _draw_text(cv, tx, ty, stamp, MARK_TEXT, sc)


def capture(
    actions: list[str], width: int, height: int,
    marks: bool, sandbox: bool, sandbox_reset: bool,
) -> tuple[str, list[str]]:
    sw, sh = _screen_w, _screen_h
    applied = list(actions)
    if sandbox:
        base = _sandbox_load(sw, sh, sandbox_reset)
        dirty, applied = _sandbox_apply(base, sw, sh, actions, sandbox_reset)
        if dirty:
            _bmp_save_rgba(SANDBOX_CANVAS, bytes(base), sw, sh)
        rgba = bytearray(base)
    else:
        rgba = _bgra_to_rgba(_capture_bgra(sw, sh))
    if marks and actions:
        _apply_marks(rgba, sw, sh, actions)
    dw = sw if width <= 0 else width
    dh = sh if height <= 0 else height
    if (dw, dh) != (sw, sh):
        rgba = _bgra_to_rgba(_resize_bgra(_rgba_to_bgra(bytes(rgba)), sw, sh, dw, dh))
    return base64.b64encode(_encode_png(bytes(rgba), dw, dh)).decode("ascii"), applied


def main() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    raw_actions = req.get("actions", [])
    actions = [str(a) for a in raw_actions] if isinstance(raw_actions, list) else []
    b64, applied = capture(
        actions, int(req.get("width", 0)), int(req.get("height", 0)),
        bool(req.get("marks", True)), bool(req.get("sandbox", False)),
        bool(req.get("sandbox_reset", False)),
    )
    sys.stdout.write(json.dumps({"screenshot_b64": b64, "applied": applied}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
