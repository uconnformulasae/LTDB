#!/usr/bin/env python3
"""
viewmarq.py - drive an AutomationDirect ViewMarq LED message display over
Ethernet without the ViewMarq Configuration Software.

Built and tested against an MD4-0212T (2 rows x 12 characters, 72 x 16 LEDs,
firmware 1.66). Uses only the Python standard library (Python 3.8+).

    python viewmarq.py                      interactive shell (type `help`)
    python viewmarq.py discover             find displays on every network adapter
    python viewmarq.py text "Hello world"   text on the top row
    python viewmarq.py leds "all red"       light LEDs
    python viewmarq.py --help               everything else

README.md is the user guide; PROTOCOL_NOTES.md documents how the display was
reverse engineered and how every constant below was found.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import select
import shlex
import socket
import struct
import sys
import time
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Protocol constants (PROTOCOL_NOTES.md explains where each one comes from)
# ---------------------------------------------------------------------------

MODBUS_PORT = 502
MODBUS_UNIT = 255             # unit id AutomationDirect's own examples use
DISCOVERY_PORT = 9560         # UDP "MATX" protocol behind the software's Browse button
DISCOVERY_MAGIC = b"MATX"


def _reg(modicon):
    """Modicon register number (4xxxxx) -> zero-based Modbus address."""
    return modicon - 400001


REG_STRVAR = _reg(400200)     # 16 string variables, 50 registers (100 chars) apart
REG_COMMAND = _reg(411000)    # ASCII command buffer, 256 registers = 512 bytes
REG_STATUS = _reg(411500)     # reply to the last command ("OK", "E21: ...")
REG_MODEL = _reg(445000)
REG_ASCII_VERSION = _reg(445010)
REG_FW_VERSION = _reg(445020)
REG_MAC = _reg(445050)
REG_GLOBAL = _reg(445100)     # 15 regs: name[8] heartbeat options tcp-port idle id 200 500
REG_NETWORK = _reg(445200)    # 8 regs: ip, mask, gateway, dns (2 regs each)
REG_PORT1 = _reg(445300)      # 9 regs: RS-232 settings
REG_PORT2 = _reg(445400)      # 7 regs: RS-485 settings
# Configuration is written to undocumented mirrors of the blocks above and then
# committed. This is the exact sequence of the software's "Write Configuration".
REG_CFG_GLOBAL = _reg(412100)
REG_CFG_NETWORK = _reg(412200)
REG_CFG_PORT1 = _reg(412300)
REG_CFG_PORT2 = _reg(412400)
REG_CFG_COMMIT = _reg(465500)
CFG_COMMIT_WORDS = ((0xFFFF, 0x0008), (0xFFFF, 0x03E3, 0x9289))

MAX_COMMAND = 511             # characters; plus the CR terminator fills the 512 byte buffer
MAX_ELEMENTS = 31             # text/variable elements on screen (the 32nd gives E6)
MIN_COMMAND_GAP = 0.10        # AutomationDirect: send command strings >= 100 ms apart
WRITE_CHUNK_REGS = 100        # registers per Modbus write (same as the ViewMarq software)
STRVAR_COUNT = 16
STRVAR_LEN = 100

OPTION_FLAGS = {
    0x0001: "startup banner",
    0x0008: "heartbeat error on comm loss",
    0x0010: "ASCII reply disabled (serial)",
    0x0020: "DHCP",
    0x0040: "word swap (numeric variables)",
    0x0080: "byte swap (strings)",
    0x0100: "diagnostic LED",
    0x0200: "orientation fixed",
    0x0400: "fixed upside down",
    0x0800: "show syntax errors on display",
}
OPT_DHCP, OPT_BYTE_SWAP = 0x0020, 0x0080
OPT_ORIENT_FIXED, OPT_UPSIDE_DOWN = 0x0200, 0x0400

COLORS = {"green": "GRN", "red": "RED", "amber": "AMB", "orange": "AMB", "yellow": "AMB"}
COLOR_NAMES = {"GRN": "green", "RED": "red", "AMB": "amber"}
MTN_MODE = {"GRN": 0, "RED": 1, "AMB": 2}       # <MTN 0/1/2> = solid green/red/amber
BLINKS = {"none": "N", "steady": "N", "no": "N", "slow": "S", "medium": "M", "fast": "F"}
SPEEDS = {"slow": "S", "medium": "M", "fast": "F"}
ALIGNS = {"left": "LJ", "center": "CJ", "centre": "CJ", "right": "RJ"}
LED_OFF_WORDS = ("off", "black", "dark", "clear")
# Marquee speed in LEDs (pixels) per second. The firmware's scroll timer table is
# 22 / 17 / 12 ms per step for slow / medium / fast. `calibrate` measures yours.
DEFAULT_PPS = {"slow": 1000 / 22, "medium": 1000 / 17, "fast": 1000 / 12}

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".viewmarq.json")
FIRMWARE_DIR = r"C:\Program Files (x86)\AutomationDirect\Viewmarq Configuration Software\Firmware"


class ViewMarqError(Exception):
    """Anything the user should see as a plain error message."""


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Font:
    cs: int          # <CS n> number
    name: str
    width: int       # glyph width in LEDs
    height: int      # glyph height in LEDs
    advance: int     # horizontal pitch: glyph plus spacing (fonts are fixed width)


FONTS = {f.cs: f for f in (
    Font(0, "normal", 5, 7, 6),       # "2 inch"            12 chars per row (default)
    Font(1, "full", 5, 8, 6),         # "2 inch full line"  uses the 8th LED row
    Font(2, "small", 5, 5, 6),        # "1.25 inch compact"
    Font(3, "tall", 5, 14, 6),        # "4 inch narrow"     both rows, 12 chars
    Font(4, "wide", 10, 14, 12),      # "4 inch"            both rows, 6 chars
    Font(5, "widefull", 10, 16, 12),  # "4 inch full line"  both rows, 6 chars
)}
FONT_BY_NAME = {f.name: f for f in FONTS.values()}
# Glyph heights of every character set, used when picking LED stamps below.
CS_HEIGHT = {0: 7, 1: 8, 2: 5, 3: 14, 4: 14, 5: 16, 6: 21, 7: 21, 8: 28, 9: 28, 10: 32}


def resolve_font(value, big=False):
    if isinstance(value, Font):
        return value
    key = str(value).strip().lower()
    font = FONTS.get(int(key)) if key.isdigit() else FONT_BY_NAME.get(key)
    if font is None:
        raise ViewMarqError(f"unknown font {value!r}; choose from {', '.join(FONT_BY_NAME)} (or 0-5)")
    return font


def text_width(nchars, font):
    """LEDs covered by nchars characters (the spacing after the last one doesn't count)."""
    return nchars * font.advance - (font.advance - font.width) if nchars else 0


def capacity(width, font):
    """Characters that fit in `width` LEDs."""
    return (width + font.advance - font.width) // font.advance


# Solid blocks of lit LEDs found inside built-in glyphs (from the firmware's font
# bitmaps). A 1-element window clips a glyph down to any rectangle up to its
# block size, which lets one screen show many LED groups in several colours.
# (character set, character, block x in glyph, block y in glyph, block w, block h)
STAMPS = (
    (2, "%", 0, 0, 2, 2),
    (2, "#", 0, 1, 5, 1),
    (1, "1", 3, 0, 1, 8),
    (4, "X", 3, 5, 4, 4),
    (4, "2", 0, 13, 10, 1),
    (5, "#", 1, 5, 9, 2),
    (5, "1", 4, 0, 2, 16),
    (8, "$", 6, 0, 3, 28),
    (8, "8", 4, 10, 7, 7),
    (8, "0", 0, 8, 4, 12),
    (7, "2", 0, 18, 15, 3),
    (8, "R", 0, 12, 11, 4),
)

# ---------------------------------------------------------------------------
# Display geometry
# ---------------------------------------------------------------------------

@dataclass
class Geometry:
    rows: int = 2       # text rows ("tiers"), 8 LEDs tall each
    chars: int = 12     # 2-inch characters per row, 6 LEDs wide each

    @property
    def width(self):
        return self.chars * 6

    @property
    def height(self):
        return self.rows * 8

    def row_window(self, row):
        return (0, (row - 1) * 8, self.width - 1, row * 8 - 1)

    @classmethod
    def from_model(cls, model):
        m = re.search(r"MD\d-(\d\d)(\d\d)", model or "")
        return cls(int(m.group(1)), int(m.group(2))) if m else cls()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_CHAR_MAP = {"\u00b0": "`", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
             "\u2013": "-", "\u2014": "-", "\u2026": "...", "\t": " ", "\n": " ", "\r": " "}


def to_display_ascii(text):
    """Map text onto the display's character set (printable ASCII; ` shows as a degree sign)."""
    out = []
    for ch in text:
        ch = _CHAR_MAP.get(ch, ch)
        if len(ch) != 1 or 32 <= ord(ch) < 127:
            out.append(ch)
            continue
        plain = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
        out.append(plain or "?")
    return "".join(out)


_TAG = re.compile(r"\{\{|\}\}|\{([A-Za-z]+)\}")


def parse_markup(text, color, blink):
    """Turn "{red}ALARM{green} ok" into cells: [(char, color tag, blink tag), ...]."""
    cells, cur_c, cur_b, pos = [], color, blink, 0
    for m in _TAG.finditer(text):
        cells += [(ch, cur_c, cur_b) for ch in text[pos:m.start()]]
        pos = m.end()
        tok, name = m.group(0), (m.group(1) or "").lower()
        if tok in ("{{", "}}"):
            cells.append((tok[0], cur_c, cur_b))
        elif name in COLORS:
            cur_c = COLORS[name]
        elif name in BLINKS:
            cur_b = BLINKS[name]
        elif name == "blink":
            cur_b = "M"
        elif name in ("reset", "default"):
            cur_c, cur_b = color, blink
        else:
            cells += [(ch, cur_c, cur_b) for ch in tok]
    cells += [(ch, cur_c, cur_b) for ch in text[pos:]]
    return cells


def cells_text(cells):
    return "".join(c[0] for c in cells)


def wrap_cells(cells, n):
    """Split cells so the first part fits in n characters, breaking at a space when possible."""
    if len(cells) <= n:
        return cells, []
    cut = next((i for i in range(min(n, len(cells) - 1), 0, -1) if cells[i][0] == " "), None)
    line, rest = (cells[:n], cells[n:]) if cut is None else (cells[:cut], cells[cut + 1:])
    while line and line[-1][0] == " ":
        line = line[:-1]
    while rest and rest[0][0] == " ":
        rest = rest[1:]
    return line, rest


def _runs(cells):
    """Group consecutive cells with the same colour/blink."""
    runs = []
    for ch, c, b in cells:
        if runs and runs[-1][1] == c and runs[-1][2] == b:
            runs[-1][0].append(ch)
        else:
            runs.append(([ch], c, b))
    return [("".join(chars), c, b) for chars, c, b in runs]


def _attrs(state, cs=None, color=None, blink=None):
    """Emit attribute commands that differ from what this command string has set so far."""
    out = ""
    if cs is not None and state.get("cs") != cs:
        out += f"<CS {cs}>"
        state["cs"] = cs
    if color is not None and state.get("color") != color:
        out += f"<{color}>"
        state["color"] = color
    if blink is not None and state.get("blink") != blink:
        out += f"<BL {blink}>"
        state["blink"] = blink
    return out


def _t_blocks(text):
    """<T>text</T>, split where the text itself contains "</T" or "<ID" so the parser
    can't mistake it for a command."""
    parts = re.split(r"(?<=<)(?=/[Tt]>|[Ii][Dd])", text)
    return "".join(f"<T>{p}</T>" for p in parts if p)


def _literal(cells, cs, state):
    """Command text for cells drawn literally; returns (text, element count)."""
    out, elements = "", 0
    for text, c, b in _runs(cells):
        blocks = _t_blocks(text)
        out += _attrs(state, cs, c, b) + blocks
        elements += blocks.count("<T>")
    return out, elements


def count_elements(command):
    return command.count("<T>") + command.count("<STR ")


# ---------------------------------------------------------------------------
# Text frames
# ---------------------------------------------------------------------------

@dataclass
class RowRequest:
    text: str = ""
    color: str = "green"
    blink: str = "none"
    align: str = "left"
    marquee: str = "auto"      # auto | on | off
    font: object = "normal"


@dataclass
class RowPlan:
    window: tuple
    font: Font
    y_offset: int
    cells: list
    align: str                 # LJ / CJ / RJ
    scroll: bool

    @property
    def win_width(self):
        return self.window[2] - self.window[0] + 1


@dataclass
class Chunk:
    fragment: str              # the row's command text for this piece
    strvars: dict
    duration: float            # seconds this piece takes to scroll through


@dataclass
class Stream:
    index: int                 # which row (plan/unit) the pieces replace
    chunks: list


@dataclass
class Frame:
    commands: list = field(default_factory=list)
    strvars: dict = field(default_factory=dict)
    streams: list = field(default_factory=list)
    plans: list = field(default_factory=list)
    pixels: dict = None        # LED frames: {(x, y): (color, blink)}
    units: list = field(default_factory=list)    # one command fragment per row
    display_id: int = 1


def pack_fragments(display_id, fragments):
    """Clear the screen and draw the fragments, using as few command strings as fit.

    The whole screen is always redrawn: deleting a single row with <CLRWIN> while
    it is scrolling froze firmware 1.66 (see PROTOCOL_NOTES.md)."""
    commands = []
    current = f"<ID {display_id}><CLR>"
    base = len(current)
    for frag in fragments:
        if len(current) + len(frag) > MAX_COMMAND and len(current) > base:
            commands.append(current)
            current = f"<ID {display_id}>"
            base = len(current)
        current += frag
    commands.append(current)
    return commands


def _make_plan(window, font, cells, req, marquee):
    win_w = window[2] - window[0] + 1
    win_h = window[3] - window[1] + 1
    if marquee not in ("auto", "on", "off"):
        raise ViewMarqError("marquee must be auto, on or off")
    scroll = marquee == "on" or (marquee == "auto" and text_width(len(cells), font) > win_w)
    if not scroll:
        cells = cells[:capacity(win_w, font)]
    y_offset = 0 if font.height >= win_h - 1 else (win_h - font.height) // 2
    align = ALIGNS.get(req.align)
    if align is None:
        raise ViewMarqError("align must be left, center or right")
    return RowPlan(window, font, y_offset, cells, align, scroll)


def _row_header(plan, speed, direction, pause):
    x0, y0, x1, y1 = plan.window
    out = f"<WIN {x0} {y0} {x1} {y1}>"
    if plan.scroll:
        base = "SL" if direction == "left" else "SR"
        out += f"<{base} {plan.align} {pause}>" if pause is not None else f"<{base}>"
        out += f"<S {SPEEDS[speed]}>"
    else:
        out += f"<{plan.align}>"
    if plan.y_offset:
        out += f"<POS {x0} {y0 + plan.y_offset}>"
    return out


def encode_row(plan, cells, varnos, budget, header):
    """Encode one row as a single command fragment of at most `budget` characters.

    Text that doesn't fit literally moves, from the end backwards, into string
    variables (100 chars each). Returns (fragment, {var: text}, elements) or None.
    """
    cs = plan.font.cs
    state = {}
    lit, n = _literal(cells, cs, state)
    if len(header) + len(lit) <= budget:
        return header + lit, {}, n
    rest, chunks, free = list(cells), [], list(varnos)
    while free and rest:
        c, b = rest[-1][1], rest[-1][2]
        k = 0
        while k < min(STRVAR_LEN, len(rest)) and rest[-1 - k][1:] == (c, b):
            k += 1
        chunks.insert(0, (free.pop(0), rest[len(rest) - k:]))
        rest = rest[:len(rest) - k]
        state = {}
        lit, n = _literal(rest, cs, state)
        refs = "".join(_attrs(state, cs, ch[0][1], ch[0][2]) + f"<STR {vn} {len(ch)}>" for vn, ch in chunks)
        if len(header) + len(lit) + len(refs) <= budget:
            return header + lit + refs, {vn: cells_text(ch) for vn, ch in chunks}, n + len(chunks)
    return None


def _chunk_cells(plan, cells, varnos, budget, header):
    """Split cells into pieces that each encode with `varnos`, preferring word breaks."""
    pieces, rest = [], list(cells)
    while rest:
        lo, hi = 1, len(rest)
        while lo < hi:                           # longest prefix that still encodes
            mid = (lo + hi + 1) // 2
            if encode_row(plan, rest[:mid], varnos, budget, header):
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        if cut < len(rest):
            space = next((i for i in range(cut, int(cut * 0.8), -1) if rest[i - 1][0] == " "), None)
            cut = space or cut
        pieces.append(rest[:cut])
        rest = rest[cut:]
    return pieces


def build_text_frame(geom, display_id, requests, *, big=False, spill=False, speed="medium",
                     direction="left", pause=None, pps=None):
    """Build the commands for a text screen.

    requests: {row number: RowRequest}. With big=True only row 1 is used and it
    fills the whole display height. With spill=True row 1's overflow pours into
    the rows below (word wrapped).
    """
    if speed not in SPEEDS:
        raise ViewMarqError("speed must be slow, medium or fast")
    if direction not in ("left", "right"):
        raise ViewMarqError("direction must be left or right")
    W = geom.width
    plans = []
    if big:
        req = requests.get(1) or RowRequest()
        font = resolve_font(req.font)
        if font.height <= 8:
            font = FONT_BY_NAME["wide"]
        cells = parse_markup(to_display_ascii(req.text), COLORS[req.color], BLINKS[req.blink])
        plans.append(_make_plan((0, 0, W - 1, geom.height - 1), font, cells, req, req.marquee))
    else:
        for r in requests:
            if not 1 <= r <= geom.rows:
                raise ViewMarqError(f"row {r} doesn't exist; this display has rows 1-{geom.rows}")
        rows = {}
        for r, req in requests.items():
            font = resolve_font(req.font)
            if font.height > 8:
                raise ViewMarqError(f"font '{font.name}' is two rows tall; use --big for it")
            rows[r] = (req, parse_markup(to_display_ascii(req.text), COLORS[req.color], BLINKS[req.blink]))
        if spill and rows:
            if set(rows) != {1}:
                raise ViewMarqError("spill pours row 1 into the rows below; give only one text with it")
            req, remaining = rows[1]
            font = resolve_font(req.font)
            rows = {}
            for r in range(1, geom.rows + 1):
                if r < geom.rows:
                    line, remaining = wrap_cells(remaining, capacity(W, font))
                else:
                    line, remaining = remaining, []
                rows[r] = (req, line)
                if not remaining:
                    break
        for r in sorted(rows):
            req, cells = rows[r]
            plans.append(_make_plan(geom.row_window(r), resolve_font(req.font), cells, req, req.marquee))

    frame = Frame(plans=plans, display_id=display_id)
    # Every row must fit in one command string together with the "<ID n><CLR>" prefix.
    budget = MAX_COMMAND - len(f"<ID {display_id}><CLR>")
    headers = [_row_header(p, speed, direction, pause) for p in plans]
    units = {}
    need_vars = []
    for i, p in enumerate(plans):
        enc = encode_row(p, p.cells, [], budget, headers[i])
        if enc:
            units[i] = enc
        else:
            need_vars.append(i)
    if need_vars:
        varnos = list(range(1, STRVAR_COUNT + 1))
        share = len(varnos) // len(need_vars)
        for k, i in enumerate(need_vars):
            pool = varnos[k * share:(k + 1) * share]
            p = plans[i]
            enc = encode_row(p, p.cells, pool, budget, headers[i])
            if enc:
                units[i] = enc
                continue
            # Longer than the display can hold: stream it in pieces, alternating
            # between two halves of the variable pool so the next piece can be
            # loaded while the current one is still scrolling.
            halves = [pool[:len(pool) // 2], pool[len(pool) // 2:]] if len(pool) > 1 else [pool, pool]
            # size pieces for the half with the longer <STR n> tags so both halves fit
            sizing = max(halves, key=lambda h: (-len(h), sum(len(str(v)) for v in h)))
            pieces = _chunk_cells(p, p.cells, sizing, budget, headers[i])
            rate = pps or DEFAULT_PPS[speed]
            chunks = []
            for n, piece in enumerate(pieces):
                frag, svars, _ = encode_row(p, piece, halves[n % 2], budget, headers[i])
                chunks.append(Chunk(frag, svars, (text_width(len(piece), p.font) + p.win_width) / rate))
            frame.streams.append(Stream(i, chunks))
            units[i] = encode_row(p, pieces[0], halves[0], budget, headers[i])

    total = sum(u[2] for u in units.values())
    if total > MAX_ELEMENTS:
        raise ViewMarqError(f"this screen needs {total} text elements but the display holds "
                            f"{MAX_ELEMENTS}; use fewer colour changes")
    for _, svars, _ in units.values():
        frame.strvars.update(svars)
    frame.units = [units[i][0] for i in range(len(plans))]
    frame.commands = pack_fragments(display_id, frame.units)
    return frame


_BLINK_NAMES = {"N": "steady", "S": "slow", "M": "medium", "F": "fast"}
_ALIGN_NAMES = {"LJ": "left", "CJ": "center", "RJ": "right"}


def cells_to_markup(cells):
    """Inverse of parse_markup, with explicit colour/blink tags."""
    out, cur = [], None
    for ch, c, b in cells:
        if (c, b) != cur:
            out.append("{%s}{%s}" % (COLOR_NAMES[c], _BLINK_NAMES[b]))
            cur = (c, b)
        out.append("{{" if ch == "{" else "}}" if ch == "}" else ch)
    return "".join(out)


def describe_screen(frame, big):
    """What a text frame shows, in a form that can be rebuilt later (used by --update)."""
    rows = {}
    for p in frame.plans:
        rows[str(p.window[1] // 8 + 1)] = {"text": cells_to_markup(p.cells), "align": _ALIGN_NAMES[p.align],
                                          "marquee": "on" if p.scroll else "off", "font": p.font.name}
    return {"big": big, "rows": rows}


# ---------------------------------------------------------------------------
# LED frames
# ---------------------------------------------------------------------------

SELECTOR_HELP = """\
LED selections (coordinates are LEDs: x 0-71 left to right, y 0-15 top to bottom):
  all                     whole board            top / bottom        the two text rows
  row N                   text row N (8 LEDs)    left / right        board halves
  quadrant N              1=top-left 2=top-right 3=bottom-left 4=bottom-right
  pixel X Y [X Y ...]     one or more single LEDs (also: led, p)
  rect X Y W H            rectangle by corner and size
  box X0 Y0 X1 Y1         rectangle by two corners
  col X [Y0 Y1]           vertical line          line Y [X0 X1]      horizontal line
  cell R C                character cell (row 1-2, column 1-12), 6x8 LEDs
  border                  outline of the board   random N            N random LEDs
Then a colour (red, green, amber/orange, or off) and optionally a hardware blink
rate (slow, medium, fast; `blink` alone = medium).
Examples:  "all red"   "pixel 0 0 71 15 green fast"   "rect 10 2 20 12 amber"
"""


def select_leds(selector, nums, geom):
    """Return the list of (x, y) LEDs a selector refers to."""
    W, H = geom.width, geom.height

    def need(n, usage):
        if len(nums) != n:
            raise ViewMarqError(f"'{selector}' needs {usage}")

    def rect(x, y, w, h):
        pts = [(xx, yy) for yy in range(max(0, y), min(H, y + h)) for xx in range(max(0, x), min(W, x + w))]
        if not pts:
            raise ViewMarqError(f"'{selector} {' '.join(map(str, nums))}' is outside the {W}x{H} board")
        return pts

    if selector in ("all", "board", "everything", "full"):
        return rect(0, 0, W, H)
    if selector == "top":
        return rect(0, 0, W, 8)
    if selector == "bottom":
        return rect(0, H - 8, W, 8)
    if selector in ("row", "tier"):
        need(1, f"a row number 1-{geom.rows}")
        if not 1 <= nums[0] <= geom.rows:
            raise ViewMarqError(f"row must be 1-{geom.rows}")
        return rect(0, (nums[0] - 1) * 8, W, 8)
    if selector in ("left", "right"):
        return rect(0 if selector == "left" else W // 2, 0, W // 2, H)
    if selector in ("quadrant", "quarter"):
        need(1, "a number 1-4")
        q = nums[0] - 1
        if not 0 <= q <= 3:
            raise ViewMarqError("quadrant must be 1-4")
        return rect((q % 2) * (W // 2), (q // 2) * (H // 2), W // 2, H // 2)
    if selector in ("pixel", "pixels", "led", "leds", "p", "dot", "dots"):
        if not nums or len(nums) % 2:
            raise ViewMarqError(f"'{selector}' needs X Y pairs")
        pts = list(zip(nums[0::2], nums[1::2]))
        bad = [p for p in pts if not (0 <= p[0] < W and 0 <= p[1] < H)]
        if bad:
            raise ViewMarqError(f"LED {bad[0]} is off the board (x 0-{W - 1}, y 0-{H - 1})")
        return pts
    if selector in ("rect", "rectangle"):
        need(4, "X Y W H")
        return rect(*nums)
    if selector == "box":
        need(4, "X0 Y0 X1 Y1")
        x0, x1 = sorted(nums[0::2])
        y0, y1 = sorted(nums[1::2])
        return rect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
    if selector in ("col", "column", "vline"):
        if len(nums) not in (1, 3):
            raise ViewMarqError(f"'{selector}' needs X [Y0 Y1]")
        y0, y1 = (nums[1], nums[2]) if len(nums) == 3 else (0, H - 1)
        return rect(nums[0], min(y0, y1), 1, abs(y1 - y0) + 1)
    if selector in ("line", "hline"):
        if len(nums) not in (1, 3):
            raise ViewMarqError(f"'{selector}' needs Y [X0 X1]")
        x0, x1 = (nums[1], nums[2]) if len(nums) == 3 else (0, W - 1)
        return rect(min(x0, x1), nums[0], abs(x1 - x0) + 1, 1)
    if selector == "cell":
        need(2, "ROW COLUMN")
        r, c = nums
        if not (1 <= r <= geom.rows and 1 <= c <= geom.chars):
            raise ViewMarqError(f"cell must be row 1-{geom.rows}, column 1-{geom.chars}")
        return rect((c - 1) * 6, (r - 1) * 8, 6, 8)
    if selector in ("border", "frame", "outline"):
        return sorted(set(rect(0, 0, W, 1) + rect(0, H - 1, W, 1) + rect(0, 0, 1, H) + rect(W - 1, 0, 1, H)))
    if selector == "random":
        need(1, "a count")
        return random.sample([(x, y) for y in range(H) for x in range(W)], min(nums[0], W * H))
    raise ViewMarqError(f"unknown selection '{selector}'.\n{SELECTOR_HELP}")


def parse_led_spec(spec, geom, color="green", blink="none"):
    """'rect 0 0 10 8 red fast' -> (leds, color tag or None for off, blink tag)."""
    toks = spec.replace(",", " ").lower().split()
    if not toks:
        raise ViewMarqError("empty LED selection")
    i = 1
    while i < len(toks) and re.fullmatch(r"-?\d+", toks[i]):
        i += 1
    nums = [int(t) for t in toks[1:i]]
    for w in toks[i:]:
        if w in COLORS or w in LED_OFF_WORDS:
            color = w
        elif w in BLINKS:
            blink = w
        elif w == "blink":
            blink = "medium"
        else:
            raise ViewMarqError(f"don't understand '{w}' in '{spec}' (expected a colour or blink rate)")
    leds = select_leds(toks[0], nums, geom)
    tag = None if color in LED_OFF_WORDS else COLORS.get(color)
    if color not in LED_OFF_WORDS and tag is None:
        raise ViewMarqError(f"unknown colour {color!r}")
    if blink not in BLINKS:
        raise ViewMarqError(f"unknown blink rate {blink!r}")
    return leds, tag, BLINKS[blink]


def paint(specs, geom, color="green", blink="none", pixels=None):
    """Apply LED specs in order to a pixel map {(x, y): (color, blink)}."""
    pixels = dict(pixels or {})
    for spec in specs:
        leds, c, b = parse_led_spec(spec, geom, color, blink)
        for p in leds:
            if c is None:
                pixels.pop(p, None)
            else:
                pixels[p] = (c, b)
    return pixels


def _rectangles(pixels, W, H):
    """Cover the pixel map with same-colour rectangles (greedy, row-major)."""
    done, rects = set(), []
    for y in range(H):
        for x in range(W):
            key = pixels.get((x, y))
            if key is None or (x, y) in done:
                continue
            w = 1
            while x + w < W and pixels.get((x + w, y)) == key and (x + w, y) not in done:
                w += 1
            h = 1
            while y + h < H and all(pixels.get((x + i, y + h)) == key and (x + i, y + h) not in done
                                    for i in range(w)):
                h += 1
            done.update((x + i, y + j) for i in range(w) for j in range(h))
            rects.append((key, x, y, w, h))
    return rects


def _best_stamp(w, h, display_height):
    """Stamp that tiles a w x h rectangle with the fewest elements."""
    def rank(st):
        cs, _, _, _, sw, sh = st
        return (math.ceil(w / sw) * math.ceil(h / sh), CS_HEIGHT[cs] > display_height, cs)
    best = min(STAMPS, key=rank)
    return best, rank(best)[0]


def build_led_frame(geom, display_id, pixels, use_mtn=True):
    """Commands that show exactly `pixels` ({(x, y): (color, blink)}), all else dark.

    The largest rectangle uses <MTN> (one solid block, any size, the display's
    LED test pattern); the rest are clipped glyph stamps.
    """
    rects = []
    for key, x, y, w, h in _rectangles(pixels, geom.width, geom.height):
        stamp, cost = _best_stamp(w, h, geom.height)
        rects.append(((key, x, y, w, h), stamp, cost))
    mtn = max(rects, key=lambda r: (r[2], r[0][3] * r[0][4])) if (use_mtn and rects) else None

    elements = []
    for rect, stamp, _ in rects:
        if mtn is not None and rect is mtn[0]:
            continue
        (color, blink), x, y, w, h = rect
        cs, ch, gx, gy, sw, sh = stamp
        for tx in range(x, x + w, sw):
            for ty in range(y, y + h, sh):
                x1, y1 = min(tx + sw, x + w) - 1, min(ty + sh, y + h) - 1
                elements.append((f"<WIN {tx} {ty} {x1} {y1}><POS {tx - gx} {ty - gy}>", cs, color, blink, ch))
    if len(elements) > MAX_ELEMENTS:
        raise ViewMarqError(
            f"that pattern needs {len(elements)} display elements but the display can only hold "
            f"{MAX_ELEMENTS} (+1 solid block) at once. Use fewer separate shapes/colours, or show it "
            f"in steps with `flash`/`chase`.")

    frame = Frame(pixels=dict(pixels))
    state = {}
    current = f"<ID {display_id}><CLR>"
    if mtn is not None:
        # <MTN> must be alone in its command string: it clears the screen and marks
        # itself as the end of the display list, so anything after it in the same
        # string is stored but never drawn. Later strings append normally.
        (color, blink), x, y, w, h = mtn[0]
        frame.commands.append(f"<ID {display_id}><BL {blink}><MTN {MTN_MODE[color]} {x} {y} {w} {h}>")
        if not elements:
            return frame
        current = f"<ID {display_id}>"
    for win, cs, color, blink, ch in elements:
        piece = win + _attrs(dict(state), cs, color, blink) + f"<T>{ch}</T>"
        if len(current) + len(piece) > MAX_COMMAND:
            frame.commands.append(current)
            current, state = f"<ID {display_id}>", {}
        current += win + _attrs(state, cs, color, blink) + f"<T>{ch}</T>"
    frame.commands.append(current)
    return frame


# ---------------------------------------------------------------------------
# Network: discovery (UDP 9560) and Modbus TCP
# ---------------------------------------------------------------------------

@dataclass
class Found:
    mac: str
    ip: str
    mask: str
    gateway: str
    flags: int
    name: str
    display_id: int
    model: str
    tcp_port: int
    idle_timeout: int
    local_ip: str          # this computer's address that heard the reply


def _decode_model(raw):
    # The firmware stores "MD4-0212T" as "MD4-0212\0T4-0212" (overlapping copy).
    parts = raw.split(b"\0")
    model = parts[0].decode("ascii", "replace")
    if len(parts) > 1 and parts[1][:1].isalpha():
        model += parts[1][:1].decode()
    return model


def local_ipv4_addresses():
    addrs = set()
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(ai[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))      # no packet is sent; just asks for the default route
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def discover(timeout=1.5, local_ips=None):
    """Broadcast the ViewMarq discovery query from every local address."""
    query = DISCOVERY_MAGIC + bytes([1, 0, 0, 1, 1])   # version 1, txn 0, length 1, function 1
    socks = {}
    for ip in local_ips or local_ipv4_addresses():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((ip, 0))
            s.sendto(query, ("255.255.255.255", DISCOVERY_PORT))
            if ip.startswith("169.254."):
                s.sendto(query, ("169.254.255.255", DISCOVERY_PORT))
            socks[s] = ip
        except OSError:
            s.close()
    found = {}
    end = time.monotonic() + timeout
    try:
        while socks and time.monotonic() < end:
            ready, _, _ = select.select(list(socks), [], [], max(0.0, end - time.monotonic()))
            for s in ready:
                try:
                    data, _ = s.recvfrom(2048)
                except OSError:
                    continue
                if len(data) < 66 or data[:4] != DISCOVERY_MAGIC or data[8] != 0x81:
                    continue
                d = Found(
                    mac=":".join(f"{b:02X}" for b in data[9:15]),
                    ip=socket.inet_ntoa(data[15:19]), mask=socket.inet_ntoa(data[19:23]),
                    gateway=socket.inet_ntoa(data[23:27]), flags=struct.unpack(">H", data[27:29])[0],
                    name=data[29:45].split(b"\0")[0].decode("ascii", "replace"), display_id=data[45],
                    model=_decode_model(data[46:62]), tcp_port=struct.unpack(">H", data[62:64])[0],
                    idle_timeout=struct.unpack(">H", data[64:66])[0], local_ip=socks[s])
                found.setdefault(d.mac, d)
    finally:
        for s in socks:
            s.close()
    return list(found.values())


def udp_set_network(found, *, ip=None, mask=None, gateway=None, dhcp=None, name=None,
                    display_id=None, tcp_port=None, idle_timeout=None):
    """Send the "Set Configuration" packet (function 2) the software's Ethernet Browse
    Editor uses. It addresses the display by MAC, so it works across subnets."""
    flags = found.flags
    if dhcp is not None:
        flags = (flags | OPT_DHCP) if dhcp else (flags & ~OPT_DHCP)
    name_b = (found.name if name is None else name).encode("ascii", "replace")[:15]
    pkt = (DISCOVERY_MAGIC + bytes([1, 0, 0, 0x2A, 2]) + bytes.fromhex(found.mac.replace(":", ""))
           + socket.inet_aton(ip or found.ip) + socket.inet_aton(mask or found.mask)
           + socket.inet_aton(gateway or found.gateway) + struct.pack(">H", flags)
           + name_b.ljust(16, b"\0") + bytes([found.display_id if display_id is None else display_id])
           + struct.pack(">HH", found.tcp_port if tcp_port is None else tcp_port,
                         found.idle_timeout if idle_timeout is None else idle_timeout))
    assert len(pkt) == 50
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind((found.local_ip, 0))
        s.sendto(pkt, ("255.255.255.255", DISCOVERY_PORT))
        s.settimeout(2.0)
        try:
            data, _ = s.recvfrom(2048)
            return data
        except OSError:
            return None


class ModbusError(ViewMarqError):
    pass


class ViewMarq:
    """An open connection to one display."""

    def __init__(self, ip, local_ip=None, port=MODBUS_PORT, timeout=3.0, verbose=False, mac=None):
        self.ip, self.local_ip, self.port, self.timeout = ip, local_ip, port, timeout
        self.verbose = verbose
        self.mac = mac             # lets us find the display again if its address changes
        self.sock = None
        self.tid = 0
        self._last_command = 0.0
        self._connect()
        g = self.read_registers(REG_GLOBAL, 15)
        self.options = struct.unpack(">H", g[18:20])[0]
        self.display_id = struct.unpack(">H", g[24:26])[0]
        self.name = self._unpack_str(g[0:16])
        self.model = _decode_model(self._unpack_str_raw(self.read_registers(REG_MODEL, 10)))
        self.geometry = Geometry.from_model(self.model)

    # -- transport --------------------------------------------------------

    def _connect(self):
        src = (self.local_ip, 0) if self.local_ip else None
        try:
            self.sock = socket.create_connection((self.ip, self.port), self.timeout, source_address=src)
        except OSError as e:
            raise ViewMarqError(f"can't connect to {self.ip}:{self.port}"
                                + (f" from {self.local_ip}" if self.local_ip else "") + f": {e}") from e
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            part = self.sock.recv(n - len(buf))
            if not part:
                raise ConnectionError("display closed the connection")
            buf += part
        return buf

    def _transact(self, pdu):
        """One Modbus request. Reconnects if the display dropped us (it closes
        connections idle for 30 s), and if that fails, finds it again by MAC: in
        DHCP mode it periodically re-requests an address ("Acquiring IP") and is
        unreachable for about a minute, sometimes coming back on a new address."""
        for attempt in (1, 2, 3):
            try:
                if attempt == 3:
                    if not self.mac:
                        raise ConnectionError("lost the display")
                    found = wait_for_address(self.mac, self.local_ip)
                    self.ip, self.local_ip = found.ip, found.local_ip
                if self.sock is None:
                    self._connect()
                self.tid = (self.tid + 1) & 0xFFFF
                self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, MODBUS_UNIT) + pdu)
                while True:
                    tid, _, length, _ = struct.unpack(">HHHB", self._recv(7))
                    body = self._recv(length - 1)
                    if tid == self.tid:
                        break
                if body[0] & 0x80:
                    raise ModbusError(f"Modbus exception {body[1]} (function {body[0] & 0x7F})")
                return body
            except (OSError, ConnectionError, ViewMarqError) as e:
                self.close()
                if isinstance(e, ModbusError) or attempt == 3:
                    raise

    def read_registers(self, addr, count):
        body = self._transact(struct.pack(">BHH", 3, addr, count))
        return body[2:2 + body[1]]

    def write_registers(self, addr, data):
        if len(data) % 2:
            data += b"\0"
        for off in range(0, len(data), WRITE_CHUNK_REGS * 2):
            part = data[off:off + WRITE_CHUNK_REGS * 2]
            self._transact(struct.pack(">BHHB", 16, addr + off // 2, len(part) // 2, len(part)) + part)

    # -- strings ----------------------------------------------------------

    def _swap(self, data):
        """Strings travel low byte first in each register unless the display's
        'byte swap' option is on."""
        if len(data) % 2:
            data += b"\0"
        if getattr(self, "options", 0) & OPT_BYTE_SWAP:
            return data
        return bytes(b for i in range(0, len(data), 2) for b in (data[i + 1], data[i]))

    def _unpack_str_raw(self, data):
        return self._swap(data)

    def _unpack_str(self, data):
        return self._swap(data).split(b"\0")[0].decode("ascii", "replace")

    # -- commands ---------------------------------------------------------

    def send(self, command, check=True, ignore=()):
        """Send one ASCII command string, wait until the display has processed it,
        and return its reply ("OK" or an error such as "E21: Syntax error at pos 7")."""
        data = command.encode("ascii", "replace") + b"\r"
        if len(data) > 512:
            raise ViewMarqError(f"command is {len(data) - 1} characters; the display's limit is {MAX_COMMAND}")
        raw = self._swap(data)
        wait = self._last_command + MIN_COMMAND_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        # Write the tail first: the display only acts once the first register is
        # non-zero (it clears it after each command), so it never sees half a command.
        size = WRITE_CHUNK_REGS * 2
        for off in reversed(range(0, len(raw), size)):
            self.write_registers(REG_COMMAND + off // 2, raw[off:off + size])
        start = time.monotonic()
        while self.read_registers(REG_COMMAND, 1) != b"\0\0":
            if time.monotonic() - start > 3.0:
                raise ViewMarqError("display didn't process the command within 3 s")
            time.sleep(0.01)
        self._last_command = time.monotonic()
        status = self.status()
        if self.verbose:
            print(f"  -> {command}\n  <- {status}")
        if check and status.startswith("E") and not any(status.startswith(i) for i in ignore):
            raise ViewMarqError(f"display rejected the command: {status}\n  command: {command}")
        return status

    def status(self):
        return self._unpack_str(self.read_registers(REG_STATUS, 32))

    def set_string_var(self, number, text):
        if not 1 <= number <= STRVAR_COUNT:
            raise ViewMarqError("string variables are numbered 1-16")
        data = to_display_ascii(text).encode("ascii", "replace")[:STRVAR_LEN]
        self.write_registers(REG_STRVAR + (number - 1) * 50, self._swap(data.ljust(STRVAR_LEN, b"\0")))

    def clear(self):
        return self.send(f"<ID {self.display_id}><CLR>")

    # -- information / configuration ------------------------------------

    def info(self):
        g = self.read_registers(REG_GLOBAL, 15)
        net = self.read_registers(REG_NETWORK, 8)
        mac = self.read_registers(REG_MAC, 3)
        return {
            "model": self.model,
            "firmware": self._unpack_str(self.read_registers(REG_FW_VERSION, 10)),
            "ascii version": self._unpack_str(self.read_registers(REG_ASCII_VERSION, 10)),
            "name": self._unpack_str(g[0:16]),
            "mac": ":".join(f"{b:02X}" for b in mac),
            "connected ip": self.ip,
            "configured static ip": socket.inet_ntoa(net[0:4]),
            "configured mask": socket.inet_ntoa(net[4:8]),
            "configured gateway": socket.inet_ntoa(net[8:12]),
            "ascii id": struct.unpack(">H", g[24:26])[0],
            "modbus tcp port": struct.unpack(">H", g[20:22])[0],
            "tcp idle timeout s": struct.unpack(">H", g[22:24])[0],
            "heartbeat s": struct.unpack(">H", g[16:18])[0],
            "options": struct.unpack(">H", g[18:20])[0],
            "geometry": f"{self.geometry.width} x {self.geometry.height} LEDs, "
                        f"{self.geometry.rows} rows x {self.geometry.chars} characters",
        }

    def write_orientation(self, mode):
        """Fix or free the display orientation by replaying the software's
        'Write Configuration' sequence with only the orientation bits changed.
        The display saves the settings and restarts."""
        g = bytearray(self.read_registers(REG_GLOBAL, 15))
        net = self.read_registers(REG_NETWORK, 8)
        p1 = self.read_registers(REG_PORT1, 9)
        p2 = self.read_registers(REG_PORT2, 7)
        flags = struct.unpack(">H", g[18:20])[0] & ~(OPT_ORIENT_FIXED | OPT_UPSIDE_DOWN)
        if mode == "normal":
            flags |= OPT_ORIENT_FIXED
        elif mode == "inverted":
            flags |= OPT_ORIENT_FIXED | OPT_UPSIDE_DOWN
        elif mode != "auto":
            raise ViewMarqError("orientation must be auto, normal or inverted")
        g[18:20] = struct.pack(">H", flags)
        self.write_registers(REG_CFG_GLOBAL, bytes(g))
        self.write_registers(REG_CFG_NETWORK, net)
        self.write_registers(REG_CFG_PORT1, p1)
        self.write_registers(REG_CFG_PORT2, p2)
        for words in CFG_COMMIT_WORDS:
            try:
                self.write_registers(REG_CFG_COMMIT, struct.pack(f">{len(words)}H", *words))
            except (OSError, ConnectionError):
                break          # the display is restarting
        self.close()
        return flags


class DryRun:
    """Stands in for a display: prints what would be sent."""

    def __init__(self, geometry=None, display_id=1):
        self.geometry = geometry or Geometry()
        self.display_id = display_id
        self.model = "dry run"

    def send(self, command, check=True, ignore=()):
        print(f"  [send] {command}")
        return "OK"

    def set_string_var(self, number, text):
        print(f"  [string variable {number}] {text!r}")

    def clear(self):
        return self.send(f"<ID {self.display_id}><CLR>")

    def close(self):
        pass


def wait_for_address(mac, local_ip=None, timeout=150.0):
    """Discover the display with this MAC, waiting while it reports 0.0.0.0
    (the "Acquiring IP" phase of DHCP mode)."""
    end = time.monotonic() + timeout
    told = False
    while time.monotonic() < end:
        for f in discover(timeout=1.0):
            if f.mac.lower() == mac.lower():
                if f.ip != "0.0.0.0":
                    if told:
                        print(f"Display is back at {f.ip}.", file=sys.stderr)
                    return f
                if not told:
                    print("The display is re-acquiring its IP address (DHCP retry, shows 'Acquiring IP'); "
                          "waiting for it...", file=sys.stderr)
                    told = True
        time.sleep(2.0)
    raise ViewMarqError("the display didn't come back on the network. If it looks frozen, power-cycle it.")


def _source_candidates(ip):
    local = local_ipv4_addresses()
    target = ip.split(".")
    local.sort(key=lambda a: -sum(1 for x, y in zip(a.split("."), target) if x == y))
    return local + [None]


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(data):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def open_display(ip=None, local_ip=None, mac=None, use_cache=True, verbose=False, quiet=False):
    """Find and connect to a display.

    With no ip it tries the last display that worked, then broadcasts a discovery
    query on every network adapter. The connection is made from the local address
    that heard the display, which matters on PCs with several 169.254.x.x adapters
    (VPNs such as Tailscale can otherwise capture the traffic)."""
    cache = load_cache()
    attempts = []
    if ip:
        attempts = [(ip, local_ip)] if local_ip else [(ip, s) for s in _source_candidates(ip)]
    else:
        last = cache.get("display") or {}
        if use_cache and last.get("ip") and (not mac or last.get("mac", "").lower() == mac.lower()):
            attempts.append((last["ip"], local_ip or last.get("local_ip")))
    known_mac = mac or (cache.get("display") or {}).get("mac") or None
    for host, src in attempts:
        try:
            dev = ViewMarq(host, src, timeout=1.5, verbose=verbose, mac=known_mac)
            dev.timeout = 3.0
            dev.sock.settimeout(3.0)
            _remember(cache, dev, known_mac)
            return dev
        except (ViewMarqError, OSError):
            continue
    if ip:
        raise ViewMarqError(f"no ViewMarq answered at {ip} (tried every local adapter)")
    if not quiet:
        print("Looking for ViewMarq displays...", file=sys.stderr)
    found = discover(local_ips=[local_ip] if local_ip else None)
    if mac:
        found = [f for f in found if f.mac.lower() == mac.lower()]
    if not found:
        raise ViewMarqError(
            "no ViewMarq display found. Check the Ethernet cable and that the adapter it's on is "
            "enabled, then run `python viewmarq.py discover`. If the display shows 'Acquiring IP', "
            "wait for it to show an address and try again.")
    if len(found) > 1 and not quiet:
        print(f"Found {len(found)} displays; using {found[0].name} at {found[0].ip} "
              f"(choose with --mac).", file=sys.stderr)
    f = found[0]
    if f.ip == "0.0.0.0":
        f = wait_for_address(f.mac, local_ip)
    dev = ViewMarq(f.ip, f.local_ip, port=f.tcp_port, verbose=verbose, mac=f.mac)
    _remember(cache, dev, f.mac)
    return dev


def _remember(cache, dev, mac=None):
    cache["display"] = {"ip": dev.ip, "local_ip": dev.local_ip, "model": dev.model,
                        "mac": mac or (cache.get("display") or {}).get("mac", "")}
    save_cache(cache)


# ---------------------------------------------------------------------------
# Sending frames, effects
# ---------------------------------------------------------------------------

def send_frame(dev, frame):
    for n, text in frame.strvars.items():
        dev.set_string_var(n, text)
    for cmd in frame.commands:
        dev.send(cmd)


def run_streams(dev, frame, loops=None):
    """Keep feeding pieces of text that is too long for the display's memory.

    When the current piece has scrolled through, the next piece's variables are
    loaded (into the half of the variables not on screen) and the screen redrawn.
    Timing comes from the marquee speed in LEDs/s (see `calibrate`)."""
    if not frame.streams:
        return
    print(f"Text is longer than the display can hold at once, so it is streamed in "
          f"{max(len(s.chunks) for s in frame.streams)} pieces. Ctrl+C to stop.")
    pos = [0] * len(frame.streams)
    passes = 0
    due = time.monotonic() + max(s.chunks[0].duration for s in frame.streams)
    try:
        while True:
            time.sleep(max(0.0, due - time.monotonic()))
            pos = [(p + 1) % len(s.chunks) for p, s in zip(pos, frame.streams)]
            if not any(pos):
                passes += 1
                if loops and passes >= loops:
                    break
            fragments = list(frame.units)
            for p, s in zip(pos, frame.streams):
                chunk = s.chunks[p]
                fragments[s.index] = chunk.fragment
                for n, text in chunk.strvars.items():
                    dev.set_string_var(n, text)
            for cmd in pack_fragments(frame.display_id, fragments):
                dev.send(cmd)
            due = time.monotonic() + max(s.chunks[p].duration for p, s in zip(pos, frame.streams))
    except KeyboardInterrupt:
        print("\nStopped streaming (the current piece keeps scrolling on the display).")


def flash(dev, pixels, times=3, interval=0.5, colors=None):
    """Software flash: show the pattern, blank, repeat. `colors` cycles the colour."""
    g, did = dev.geometry, dev.display_id
    for i in range(times):
        shown = pixels
        if colors:
            c = COLORS[colors[i % len(colors)]]
            shown = {p: (c, b) for p, (_, b) in pixels.items()}
        t0 = time.monotonic()
        for cmd in build_led_frame(g, did, shown).commands:
            dev.send(cmd)
        time.sleep(max(0.0, interval - (time.monotonic() - t0)))
        t0 = time.monotonic()
        dev.clear()
        if i < times - 1:
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))


def chase(dev, leds, color="RED", blink="N", interval=0.1, fill=False, loops=1):
    """Light LEDs one at a time in reading order (individually), or cumulatively."""
    g, did = dev.geometry, dev.display_id
    order = sorted(set(leds), key=lambda p: (p[1], p[0]))
    for _ in range(loops):
        lit = {}
        for x, y in order:
            t0 = time.monotonic()
            if fill:
                lit[(x, y)] = (color, blink)
                for cmd in build_led_frame(g, did, lit).commands:
                    dev.send(cmd)
            else:
                dev.send(f"<ID {did}><BL {blink}><MTN {MTN_MODE[color]} {x} {y} 1 1>")
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))


# ---------------------------------------------------------------------------
# Terminal preview
# ---------------------------------------------------------------------------

_FONT_OFFSETS = {2: 0x1ED1E, 0: 0x1EEFE, 1: 0x1F19E, 3: 0x1F49E, 4: 0x1F9DE, 5: 0x2045E}
_font_cache = None


def _load_fonts():
    """Glyph bitmaps for the preview, read from the installed firmware image if present."""
    global _font_cache
    if _font_cache is not None:
        return _font_cache
    _font_cache = {}
    path = os.environ.get("VIEWMARQ_FIRMWARE")
    if not path and os.path.isdir(FIRMWARE_DIR):
        files = sorted(f for f in os.listdir(FIRMWARE_DIR) if f.lower().endswith(".vmf"))
        path = os.path.join(FIRMWARE_DIR, files[-1]) if files else None
    if not path:
        return _font_cache
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return _font_cache
    for cs, off in _FONT_OFFSETS.items():
        font = FONTS[cs]
        bpr = (font.width + 7) // 8
        size = font.height * bpr
        space = data[off:off + size]
        bar = data[off + (ord("|") - 32) * size: off + (ord("|") - 31) * size]
        if len(space) == size and not any(space) and any(bar):
            _font_cache[cs] = (data, off, bpr)
    return _font_cache


def _glyph(cs, ch):
    fonts = _load_fonts()
    font = FONTS[cs]
    if cs not in fonts or not 32 <= ord(ch) < 128:
        return [[1 if 0 < r < font.height - 1 and 0 < c < font.width - 1 else 0
                 for c in range(font.width)] for r in range(font.height)]
    data, off, bpr = fonts[cs]
    size = font.height * bpr
    g = data[off + (ord(ch) - 32) * size: off + (ord(ch) - 31) * size]
    return [[(int.from_bytes(g[r * bpr:(r + 1) * bpr], "big") >> (bpr * 8 - 1 - c)) & 1
             for c in range(font.width)] for r in range(font.height)]


def render_preview(geom, frame):
    """Approximate picture of a frame (a scrolling row is shown at its start)."""
    W, H = geom.width, geom.height
    canvas = {}
    if frame.pixels is not None:
        canvas = {p: c for p, (c, _) in frame.pixels.items()}
    for plan in frame.plans:
        x0, y0, x1, y1 = plan.window
        width = text_width(len(plan.cells), plan.font)
        x = x0
        if not plan.scroll and plan.align == "CJ":
            x = x0 + (plan.win_width - width) // 2
        elif not plan.scroll and plan.align == "RJ":
            x = x1 - width + 1
        for ch, color, _ in plan.cells:
            for r, row in enumerate(_glyph(plan.font.cs, ch)):
                for c, bit in enumerate(row):
                    px, py = x + c, y0 + plan.y_offset + r
                    if bit and x0 <= px <= x1 and y0 <= py <= y1:
                        canvas[(px, py)] = color
            x += plan.font.advance
            if x > x1:
                break
    if os.name == "nt":
        os.system("")          # turns on ANSI colours in the Windows console
    enc = (sys.stdout.encoding or "ascii").lower()
    on = "\u25cf" if "utf" in enc else "O"
    ansi = {"RED": "\x1b[91m", "GRN": "\x1b[92m", "AMB": "\x1b[93m"}
    lines = []
    for y in range(H):
        row = ""
        for x in range(W):
            c = canvas.get((x, y))
            row += f"{ansi[c]}{on}\x1b[0m" if c else "\x1b[90m.\x1b[0m"
        lines.append(row)
    notes = [f"row {i + 1}: scrolls" for i, p in enumerate(frame.plans) if p.scroll]
    return "\n".join(lines) + ("\n(" + ", ".join(notes) + ")" if notes else "")


# ---------------------------------------------------------------------------
# Lap timer
# ---------------------------------------------------------------------------

LAP_KEYS_HELP = """\
Keys (in this window; with --global, from any window):
  Ctrl+Shift+S   start the timer
  Ctrl+Space     lap (only counts once the timer is running)
  Ctrl+Left      current lap time big (press again: normal view)
  Ctrl+Right     previous lap + laps completed big (press again: normal view)
  Ctrl+5         finish: save the CSV, show the standby message, exit
  Ctrl+C         same as Ctrl+5"""

LAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "laps")
LAP_AUTOSAVE = "_in_progress.csv"   # rewritten after every lap; becomes a normal log if a run is cut off
VAR_CURRENT, VAR_BOTTOM = 1, 2      # string variables holding the live values


def fmt_hms(seconds):
    """Display format HH:MM:SS (whole seconds, as the time is counting)."""
    s = min(int(seconds), 99 * 3600 + 59 * 60 + 59)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def fmt_lap(seconds):
    """CSV format HH:MM:SS.mmm."""
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def next_log_path(folder, date):
    """folder/MM-DD-YY-N.csv with N one higher than any existing log for that date."""
    pattern = re.compile(rf"{re.escape(date)}-(\d+)\.csv", re.IGNORECASE)
    taken = [int(m.group(1)) for name in os.listdir(folder) for m in [pattern.fullmatch(name)] if m]
    return os.path.join(folder, f"{date}-{max(taken, default=0) + 1}.csv")


def write_lap_csv(path, laps):
    import csv
    with open(path, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["Lap", "Lap Time"])
        for n, t in enumerate(laps, 1):
            out.writerow([n, fmt_lap(t)])


def _key_action(vk, state, char=""):
    """Map a Windows key press (virtual key + control-key state) to a lap timer action."""
    ctrl = bool(state & 0x000C)          # LEFT_CTRL_PRESSED | RIGHT_CTRL_PRESSED
    alt = bool(state & 0x0003)           # LEFT_ALT / RIGHT_ALT (AltGr = Ctrl+Alt, so excluded)
    shift = bool(state & 0x0010)
    if not ctrl or alt:
        return None
    if shift:
        return "start" if vk == 0x53 else None                       # Ctrl+Shift+S
    return {0x20: "lap", 0x25: "current", 0x27: "previous",          # Ctrl+Space / Left / Right
            0x35: "stop", 0x65: "stop", 0x43: "abort"}.get(vk) or \
        ("lap" if char == "\0" and vk == 0x32 else None)             # Ctrl+Space sent as NUL


class ConsoleKeys:
    """Reads key presses from the console window this program runs in (Windows).
    The console is put in raw mode so Ctrl+S and Ctrl+C arrive as keys."""

    def __init__(self, events):
        import ctypes
        import threading
        from ctypes import wintypes

        class KEY_EVENT_RECORD(ctypes.Structure):
            _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                        ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                        ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

        class EVENT(ctypes.Union):
            _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_size", ctypes.c_byte * 16)]

        class INPUT_RECORD(ctypes.Structure):
            _fields_ = [("EventType", wintypes.WORD), ("Event", EVENT)]

        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.handle = self.k32.GetStdHandle(-10)                    # STD_INPUT_HANDLE
        self.mode = wintypes.DWORD()
        if not self.k32.GetConsoleMode(self.handle, ctypes.byref(self.mode)):
            raise OSError("standard input is not a console window")
        self.k32.SetConsoleMode(self.handle, self.mode.value & ~0x0007)   # no processed/line/echo input
        self.k32.FlushConsoleInputBuffer(self.handle)
        self.events, self.stop = events, threading.Event()

        def run():
            rec, n, down = INPUT_RECORD(), wintypes.DWORD(), set()
            while not self.stop.is_set():
                if self.k32.WaitForSingleObject(self.handle, 100) != 0:
                    continue
                if not self.k32.ReadConsoleInputW(self.handle, ctypes.byref(rec), 1, ctypes.byref(n)) or not n.value:
                    continue
                if rec.EventType != 1:                               # KEY_EVENT
                    continue
                ke = rec.Event.KeyEvent
                vk = ke.wVirtualKeyCode
                if not ke.bKeyDown:
                    down.discard(vk)
                    continue
                if vk in down:                                       # held down: ignore auto-repeat
                    continue
                down.add(vk)
                action = _key_action(vk, ke.dwControlKeyState, ke.uChar)
                if action:
                    self.events.put((action, time.perf_counter()))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(1.0)
        self.k32.SetConsoleMode(self.handle, self.mode.value)


class GlobalKeys:
    """System-wide hotkeys (Windows RegisterHotKey): work whichever window has focus.
    While the timer runs, these key combinations don't reach other programs."""

    BINDS = {1: (0x2, 0x20, "lap"), 2: (0x2, 0x25, "current"), 3: (0x2, 0x27, "previous"),
             4: (0x2 | 0x4, 0x53, "start"), 5: (0x2, 0x35, "stop"), 6: (0x2, 0x65, "stop")}
    NAMES = {1: "Ctrl+Space", 2: "Ctrl+Left", 3: "Ctrl+Right", 4: "Ctrl+Shift+S", 5: "Ctrl+5", 6: "Ctrl+Numpad5"}

    def __init__(self, events):
        import threading
        self.events, self.stop, self.ready = events, threading.Event(), threading.Event()
        self.failed = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(2.0)
        missing = [self.NAMES[i] for i in self.failed if i != 6]
        if missing:
            print(f"Warning: another program already uses {', '.join(missing)} as a hotkey, so "
                  f"that key won't work; run without --global to use this window's keys instead.",
                  file=sys.stderr)

    def _run(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        for hid, (mods, vk, _) in self.BINDS.items():
            if not user32.RegisterHotKey(None, hid, mods | 0x4000, vk):     # MOD_NOREPEAT
                self.failed.append(hid)
        self.ready.set()
        msg = wintypes.MSG()
        while not self.stop.is_set():
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):     # PM_REMOVE
                if msg.message == 0x0312 and msg.wParam in self.BINDS:     # WM_HOTKEY
                    self.events.put((self.BINDS[msg.wParam][2], time.perf_counter()))
            time.sleep(0.005)
        for hid in self.BINDS:
            user32.UnregisterHotKey(None, hid)

    def close(self):
        self.stop.set()
        self.thread.join(1.0)


class LineKeys:
    """Fallback when there is no console (piped input, other systems): one word per line:
    start, lap, current, previous, stop."""

    WORDS = {"start": "start", "s": "start", "lap": "lap", "l": "lap", "current": "current",
             "left": "current", "previous": "previous", "right": "previous", "stop": "stop",
             "5": "stop", "quit": "stop", "q": "stop"}

    def __init__(self, events):
        import threading
        self.events = events

        def run():
            for line in sys.stdin:
                action = self.WORDS.get(line.strip().lower())
                if action:
                    self.events.put((action, time.perf_counter()))

        threading.Thread(target=run, daemon=True).start()

    def close(self):
        pass


class LapTimer:
    """Lap timer and lap counter on the display.

    Normal view: current lap time (HH:MM:SS) centred on row 1; "previous lap ; N Laps
    complete" on row 2, scrolling because it is longer than a row. The two values live
    in string variables, so the clock ticks without redrawing the screen and the
    bottom row keeps scrolling smoothly."""

    def __init__(self, dev, folder=LAP_DIR, current_color="GRN", previous_color="AMB",
                 standby_text="Standing by...", standby_color="AMB", min_lap=1.0, speed="medium"):
        self.dev, self.folder = dev, folder
        self.current_color, self.previous_color = current_color, previous_color
        self.standby_text, self.standby_color = standby_text, standby_color
        self.min_lap, self.speed = min_lap, SPEEDS[speed]
        self.date = time.strftime("%m-%d-%y")
        self.running = False
        self.lap_start = None
        self.laps = []
        self.view = "split"           # split | current | previous
        self.need_redraw = True
        self.shown = None             # text currently in the current-lap variable
        self.last_io = 0.0
        self.display_ok = True

    # -- display ------------------------------------------------------------

    def _io(self, fn):
        """Run display I/O; a display problem never stops the timing."""
        try:
            fn()
            self.last_io = time.monotonic()
            if not self.display_ok:
                print("\n  display is responding again.")
            self.display_ok = True
            return True
        except (ViewMarqError, OSError, ConnectionError) as e:
            if self.display_ok:
                print(f"\n  display problem ({e}); timing continues, retrying...")
            self.display_ok = False
            self.need_redraw = True
            return False

    def current_text(self, now):
        return fmt_hms(now - self.lap_start) if self.running else "00:00:00"

    def bottom_text(self):
        previous = fmt_hms(self.laps[-1]) if self.laps else "00:00:00"
        return f"{previous} ; {len(self.laps)} Laps complete"

    def layout(self, bottom):
        """Command fragments for the current view."""
        g = self.dev.geometry
        W, H = g.width, g.height
        blink = "N" if self.running else "S"        # time blinks slowly until the timer starts

        def bottom_row(y0, y1, font):
            scroll = text_width(len(bottom), font) > W
            effect = f"<SL><S {self.speed}>" if scroll else "<CJ>"
            return (f"<WIN 0 {y0} {W - 1} {y1}>{effect}<CS {font.cs}><{self.previous_color}><BL N>"
                    f"<STR {VAR_BOTTOM} {len(bottom)}>")

        current = lambda y1, cs: (f"<WIN 0 0 {W - 1} {y1}><CJ><CS {cs}><{self.current_color}><BL {blink}>"
                                  f"<STR {VAR_CURRENT} 8>")
        if self.view == "current":
            return [current(H - 1, 3)]
        if self.view == "previous":
            return [bottom_row(0, H - 1, FONTS[3])]
        return [current(7, 0), bottom_row(8, 15, FONTS[0])]

    def redraw(self, now):
        current, bottom = self.current_text(now), self.bottom_text()

        def io():
            self.dev.set_string_var(VAR_CURRENT, current)
            self.dev.set_string_var(VAR_BOTTOM, bottom)
            for cmd in pack_fragments(self.dev.display_id, self.layout(bottom)):
                self.dev.send(cmd)
        if self._io(io):
            self.need_redraw = False
            self.shown = current

    def tick(self, now):
        if self.need_redraw:
            self.redraw(now)
        elif self.running:
            text = self.current_text(now)
            if text != self.shown and self._io(lambda: self.dev.set_string_var(VAR_CURRENT, text)):
                self.shown = text
        if time.monotonic() - self.last_io > 10:                   # keep the connection alive
            self._io(self.dev.status)

    def standby(self):
        frame = build_text_frame(self.dev.geometry, self.dev.display_id,
                                 {1: RowRequest(self.standby_text, COLOR_NAMES[self.standby_color], "none",
                                                "center", "auto", "normal")}, spill=True)
        return self._io(lambda: send_frame(self.dev, frame))

    # -- actions ------------------------------------------------------------

    def start(self, t):
        if self.running:
            print("\n  already running")
            return
        self.running, self.lap_start, self.need_redraw = True, t, True
        print("\n  timer started")

    def lap(self, t):
        if not self.running:
            print("\n  lap ignored: start the timer first (Ctrl+Shift+S)")
            return
        took = t - self.lap_start
        if took < self.min_lap:
            print(f"\n  lap ignored: {took:.2f} s since the last one (double press?)")
            return
        self.laps.append(took)
        self.lap_start = t
        self.need_redraw = True
        print(f"\n  lap {len(self.laps)}: {fmt_lap(took)}")
        try:
            write_lap_csv(os.path.join(self.folder, LAP_AUTOSAVE), self.laps)
        except OSError:
            pass

    def toggle_view(self, view):
        self.view = "split" if self.view == view else view
        self.need_redraw = True

    def finish(self):
        """Save the CSV, show the standby message. Returns the CSV path."""
        path = next_log_path(self.folder, self.date)
        write_lap_csv(path, self.laps)
        try:
            os.remove(os.path.join(self.folder, LAP_AUTOSAVE))
        except OSError:
            pass
        self.standby()
        return path

    # -- main loop ------------------------------------------------------------

    def run(self, events):
        import queue
        status = None
        aborted = False
        try:
            while True:
                try:
                    action, t = events.get(timeout=0.05)
                except queue.Empty:
                    action, t = None, None
                if action == "start":
                    self.start(t)
                elif action == "lap":
                    self.lap(t)
                elif action in ("current", "previous"):
                    self.toggle_view(action)
                elif action in ("stop", "abort"):
                    aborted = action == "abort"
                    break
                now = time.perf_counter()
                self.tick(now)
                line = (f"[{'running' if self.running else 'ready - Ctrl+Shift+S to start'}] "
                        f"lap {len(self.laps) + 1}  current {self.current_text(now)}  "
                        f"previous {fmt_hms(self.laps[-1]) if self.laps else '--:--:--'}  "
                        f"view {self.view}")
                if line != status:
                    print(f"\r{line:<79}", end="", flush=True)
                    status = line
        except KeyboardInterrupt:
            aborted = True
        print()
        path = self.finish()
        if aborted:
            print("Interrupted.")
        print(f"{len(self.laps)} lap(s) saved to {path}")
        if self.laps:
            best = min(range(len(self.laps)), key=self.laps.__getitem__)
            print(f"Best lap: {best + 1} ({fmt_lap(self.laps[best])}), total {fmt_lap(sum(self.laps))}")
        return path


def recover_unfinished_run(folder):
    """If a previous run was cut off (window closed, power loss), keep its laps as a normal log."""
    autosave = os.path.join(folder, LAP_AUTOSAVE)
    if not os.path.exists(autosave):
        return None
    date = time.strftime("%m-%d-%y", time.localtime(os.path.getmtime(autosave)))
    path = next_log_path(folder, date)
    os.replace(autosave, path)
    return path


# ---------------------------------------------------------------------------
# Command line / shell
# ---------------------------------------------------------------------------

DEFAULTS = {"color": "green", "blink": "none", "align": "left", "marquee": "auto", "spill": "off",
            "font": "normal", "bigfont": "wide", "speed": "medium", "direction": "left"}


class Session:
    def __init__(self, args):
        self.args = args
        self.device = None
        self.defaults = dict(DEFAULTS)
        self.cache = load_cache()

    def dev(self):
        if self.device is None:
            a = self.args
            if a.dry_run:
                self.device = DryRun(Geometry(*a.geometry) if a.geometry else None, a.id or 1)
            else:
                self.device = open_display(a.ip, a.local_ip, a.mac, use_cache=not a.no_cache,
                                           verbose=a.verbose)
                if a.id is not None:
                    self.device.display_id = a.id
                print(f"Connected to {self.device.model} '{self.device.name}' at {self.device.ip} "
                      f"(from {self.device.local_ip or 'default route'}).", file=sys.stderr)
        return self.device

    def pick(self, value, key):
        return value if value is not None else self.defaults[key]

    def pps(self, speed, override=None):
        if override:
            return override
        return (self.cache.get("pps") or {}).get(speed) or DEFAULT_PPS[speed]


def _row_option_args(p):
    for r in (1, 2, 3, 4):
        p.add_argument(f"--color{r}", help=argparse.SUPPRESS)
        p.add_argument(f"--blink{r}", help=argparse.SUPPRESS)
        p.add_argument(f"--align{r}", help=argparse.SUPPRESS)
        p.add_argument(f"--marquee{r}", help=argparse.SUPPRESS)
        p.add_argument(f"--font{r}", help=argparse.SUPPRESS)


def build_parser():
    p = argparse.ArgumentParser(
        prog="viewmarq.py",
        description="Control a ViewMarq LED message display over Ethernet. Run with no command for "
                    "the interactive shell.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               '  python viewmarq.py text "Hello" "World" --color2 red\n'
               '  python viewmarq.py text "A long sentence that will not fit" --spill\n'
               '  python viewmarq.py leds "top red" "bottom green fast"\n'
               '  python viewmarq.py flash "all amber" --times 5\n'
               "  python viewmarq.py demo")
    p.add_argument("--ip", help="display IP address (default: find it automatically)")
    p.add_argument("--local-ip", help="this PC's address on the display's network")
    p.add_argument("--mac", help="pick a display by MAC address when there are several")
    p.add_argument("--id", type=int, help="ASCII display id (default: read from the display)")
    p.add_argument("--no-cache", action="store_true", help="always run discovery")
    p.add_argument("--dry-run", action="store_true", help="print commands instead of sending them")
    p.add_argument("--geometry", type=int, nargs=2, metavar=("ROWS", "CHARS"),
                   help="display size for --dry-run (default 2 12)")
    p.add_argument("--preview", action="store_true", help="draw the result in the terminal")
    p.add_argument("-v", "--verbose", action="store_true", help="show every command and reply")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("discover", help="find displays on every network adapter")
    sub.add_parser("info", help="show model, firmware, network and option settings")
    sub.add_parser("clear", help="blank the display")

    t = sub.add_parser("text", help="show text", formatter_class=argparse.RawDescriptionHelpFormatter,
                       description="Show text. Each TEXT goes on its own row (row 1 = top). "
                                   "Inline colours: {red} {green} {amber} {fast} {slow} {steady} {reset}.",
                       epilog="Per-row options: --color2 red, --align1 center, --marquee2 off, --blink1 fast, "
                              "--font2 small (the number is the row).")
    t.add_argument("texts", nargs="+", metavar="TEXT")
    t.add_argument("--row", type=int, help="put the (single) TEXT on this row")
    t.add_argument("--color", choices=sorted(COLORS))
    t.add_argument("--blink", choices=sorted(BLINKS))
    t.add_argument("--align", choices=sorted(ALIGNS))
    t.add_argument("--marquee", choices=("auto", "on", "off"),
                   help="auto (default): scroll only text that doesn't fit")
    t.add_argument("--spill", action="store_const", const="on", default=None,
                   help="let row 1 text that doesn't fit continue on the next row")
    t.add_argument("--no-spill", action="store_const", const="off", dest="spill")
    t.add_argument("--font", help="normal, full, small (rows) or tall, wide, widefull (with --big)")
    t.add_argument("--big", action="store_true", help="one line using the full display height")
    t.add_argument("--speed", choices=sorted(SPEEDS), help="marquee speed (one speed for the whole display)")
    t.add_argument("--direction", choices=("left", "right"))
    t.add_argument("--pause", type=int, metavar="SECONDS", help="scroll in, pause, repeat (hardware)")
    t.add_argument("--update", action="store_true", help="change only the given rows; the others keep the "
                   "text this program last put there")
    t.add_argument("--loops", type=int, help="for streamed (very long) text: stop after N passes")
    t.add_argument("--pps", type=float, help="marquee speed in LEDs/s for streaming (see calibrate)")
    _row_option_args(t)

    l = sub.add_parser("leds", help="light LEDs / sections", description=SELECTOR_HELP,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    l.add_argument("specs", nargs="+", metavar="SELECTION")
    l.add_argument("--color", choices=sorted(COLORS), help="colour for selections that don't name one")
    l.add_argument("--blink", choices=sorted(BLINKS), help="hardware blink for selections that don't name one")
    l.add_argument("--no-mtn", action="store_true", help=argparse.SUPPRESS)

    f = sub.add_parser("flash", help="flash LEDs on and off a number of times",
                       description=SELECTOR_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    f.add_argument("specs", nargs="+", metavar="SELECTION")
    f.add_argument("--color", choices=sorted(COLORS))
    f.add_argument("--colors", help="cycle colours, e.g. red,green,amber")
    f.add_argument("--times", type=int, default=3)
    f.add_argument("--interval", type=float, default=0.5, help="seconds on (and off), min 0.1")

    c = sub.add_parser("chase", help="light LEDs one at a time (individually) across a selection",
                       description=SELECTOR_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_argument("specs", nargs="+", metavar="SELECTION")
    c.add_argument("--color", choices=sorted(COLORS))
    c.add_argument("--interval", type=float, default=0.12)
    c.add_argument("--fill", action="store_true", help="leave each LED on (fill up)")
    c.add_argument("--loops", type=int, default=1)

    r = sub.add_parser("raw", help="send a raw ViewMarq ASCII command, e.g. \"<CLR><RED><T>Hi</T>\"")
    r.add_argument("command", nargs="+")
    v = sub.add_parser("setvar", help="set a string variable (shown by <STR n len>)")
    v.add_argument("number", type=int)
    v.add_argument("text")
    d = sub.add_parser("demo", help="walk through everything the display can do")
    d.add_argument("--delay", type=float, default=2.5, help="seconds per step (0 = wait for Enter)")
    k = sub.add_parser("calibrate", help="measure the marquee speed (used for streaming long text)")
    k.add_argument("--speed", choices=sorted(SPEEDS), default="medium")
    k.add_argument("--clock", action="store_true", help="show a running clock on row 2 instead of timing Enter presses")
    k.add_argument("--seconds", type=float, default=120, help="how long --clock runs (default 120)")
    k.add_argument("--period", type=float, help="save a loop time you measured (seconds)")
    o = sub.add_parser("orientation", help="show or set auto / fixed-normal / fixed-inverted orientation",
                       description="normal/inverted are relative to the display's factory orientation, not "
                                   "the viewer: on a board mounted the other way up, 'inverted' reads right-side "
                                   "up. 'auto' (factory setting) follows the accelerometer.")
    o.add_argument("mode", nargs="?", choices=("auto", "normal", "inverted"))
    o.add_argument("--yes", action="store_true", help="really write it (the display restarts)")
    n = sub.add_parser("netconfig", help="set the display's IP address (static or DHCP) by MAC")
    g = n.add_mutually_exclusive_group(required=True)
    g.add_argument("--dhcp", action="store_true")
    g.add_argument("--static", metavar="IP")
    n.add_argument("--mask", default="255.255.255.0")
    n.add_argument("--gateway", default="0.0.0.0")
    n.add_argument("--yes", action="store_true", help="really write it (the display restarts)")
    lt = sub.add_parser("laptimer", help="lap timer + lap counter driven by hotkeys (laptimer start)",
                        description="Lap timer. 'start' runs it; 'standby' only shows the standby message.\n\n"
                                    + LAP_KEYS_HELP + "\n\nEach finished run is saved as laps\\MM-DD-YY-N.csv.",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    lt.add_argument("action", choices=("start", "standby"))
    lt.add_argument("--global", dest="global_keys", action="store_true",
                    help="hotkeys work even when this window isn't focused (Windows)")
    lt.add_argument("--out", help="folder for the CSV files (default: the laps folder next to viewmarq.py)")
    lt.add_argument("--min-lap", type=float, default=1.0,
                    help="ignore laps shorter than this many seconds (guards against double presses)")
    lt.add_argument("--color-current", choices=sorted(COLORS), default="green")
    lt.add_argument("--color-previous", choices=sorted(COLORS), default="amber")
    lt.add_argument("--standby-text", default="Standing by...")
    lt.add_argument("--speed", choices=sorted(SPEEDS), default="medium", help="scroll speed of the long line")
    sub.add_parser("shell", help="interactive mode")
    return p


def _rows_from_args(s, a):
    texts = a.texts
    if a.row is not None:
        if len(texts) != 1:
            raise ViewMarqError("--row takes exactly one TEXT")
        rows = {a.row: texts[0]}
    else:
        rows = {i + 1: t for i, t in enumerate(texts)}
    requests = {}
    for r, text in rows.items():
        def opt(name, default_key):
            v = getattr(a, f"{name}{r}", None) if r <= 4 else None
            if v is None:
                v = getattr(a, name)
            return s.pick(v, default_key)
        font = opt("font", "bigfont" if a.big else "font")
        requests[r] = RowRequest(text, opt("color", "color"), opt("blink", "blink"), opt("align", "align"),
                                 opt("marquee", "marquee"), font)
        for key, value in (("color", requests[r].color), ("blink", requests[r].blink)):
            table = COLORS if key == "color" else BLINKS
            if value not in table:
                raise ViewMarqError(f"unknown {key} {value!r}")
    if not a.big and any(resolve_font(q.font).height > 8 for q in requests.values()):
        if len(requests) == 1:
            a.big = True          # a two-row font implies one big line
            requests = {1: next(iter(requests.values()))}
    return requests


def cmd_text(s, a):
    dev = s.dev()
    requests = _rows_from_args(s, a)
    spill = s.pick(a.spill, "spill") == "on"
    if a.update:
        if a.big or spill:
            raise ViewMarqError("--update replaces individual rows; it can't be combined with --big or --spill")
        last = load_cache().get("last_text") or {}
        if not last.get("big"):
            merged = {int(r): RowRequest(**q) for r, q in (last.get("rows") or {}).items()}
            merged.update(requests)
            requests = merged
    speed = s.pick(a.speed, "speed")
    frame = build_text_frame(dev.geometry, dev.display_id, requests, big=a.big, spill=spill, speed=speed,
                             direction=s.pick(a.direction, "direction"), pause=a.pause,
                             pps=s.pps(speed, a.pps))
    if a.preview or s.args.preview:
        print(render_preview(dev.geometry, frame))
    send_frame(dev, frame)
    if not frame.streams:
        cache = load_cache()
        cache["last_text"] = describe_screen(frame, a.big)
        save_cache(cache)
    else:
        _forget_text()
    for i, p in enumerate(frame.plans):
        label = "big line" if a.big else f"row {p.window[1] // 8 + 1}"
        mode = "scrolling" if p.scroll else "static"
        print(f"{label}: {mode}, {len(p.cells)} characters")
    run_streams(dev, frame, a.loops)


def _forget_text():
    """The screen no longer shows a remembered text layout (used by --update)."""
    cache = load_cache()
    if cache.pop("last_text", None) is not None:
        save_cache(cache)


def cmd_leds(s, a):
    _forget_text()
    dev = s.dev()
    pixels = paint(a.specs, dev.geometry, s.pick(a.color, "color"), s.pick(a.blink, "blink"))
    frame = build_led_frame(dev.geometry, dev.display_id, pixels, use_mtn=not a.no_mtn)
    if a.preview or s.args.preview:
        print(render_preview(dev.geometry, frame))
    send_frame(dev, frame)
    print(f"{len(pixels)} LEDs lit using {len(frame.commands)} command(s).")


def cmd_flash(s, a):
    _forget_text()
    dev = s.dev()
    colors = [c.strip() for c in a.colors.split(",")] if a.colors else None
    for c in colors or ():
        if c not in COLORS:
            raise ViewMarqError(f"unknown colour {c!r}")
    pixels = paint(a.specs, dev.geometry, s.pick(a.color, "color"), "none")
    flash(dev, pixels, a.times, max(a.interval, MIN_COMMAND_GAP), colors)


def cmd_chase(s, a):
    _forget_text()
    dev = s.dev()
    leds = []
    color = COLORS[s.pick(a.color, "color")]
    for spec in a.specs:
        pts, c, _ = parse_led_spec(spec, dev.geometry, s.pick(a.color, "color"))
        leds += pts
        color = c or color
    chase(dev, leds, color, interval=max(a.interval, MIN_COMMAND_GAP), fill=a.fill, loops=a.loops)


def cmd_discover(s, a):
    found = discover()
    if not found:
        print("No displays answered. Is the cable in and the adapter enabled?")
        return
    for f in found:
        orient = ("auto" if not f.flags & OPT_ORIENT_FIXED
                  else "fixed upside down" if f.flags & OPT_UPSIDE_DOWN else "fixed normal")
        print(f"{f.name:16s} {f.model:10s} IP {f.ip:15s} MAC {f.mac}  id {f.display_id}  "
              f"{'DHCP' if f.flags & OPT_DHCP else 'static'}  orientation {orient}  "
              f"(seen from {f.local_ip})")


def cmd_info(s, a):
    dev = s.dev()
    info = dev.info()
    for k, v in info.items():
        if k == "options":
            names = [n for bit, n in OPTION_FLAGS.items() if v & bit]
            v = f"0x{v:04X} ({', '.join(names) or 'none'})"
        print(f"{k:22s} {v}")
    opts = info["options"]
    orient = ("automatic (accelerometer)" if not opts & OPT_ORIENT_FIXED
              else "fixed, upside down" if opts & OPT_UPSIDE_DOWN else "fixed, normal")
    print(f"{'orientation':22s} {orient}")


def cmd_raw(s, a):
    dev = s.dev()
    command = " ".join(a.command)
    if not command.upper().startswith("<ID"):
        command = f"<ID {dev.display_id}>" + command
    print(dev.send(command, check=False))


def cmd_setvar(s, a):
    s.dev().set_string_var(a.number, a.text)
    print(f"string variable {a.number} set")


def cmd_orientation(s, a):
    dev = s.dev()
    if not a.mode:
        cmd_info(s, a)
        return
    if not a.yes:
        print("This rewrites the display's saved configuration (only the orientation bits change) and "
              "restarts it. Run again with --yes to go ahead.")
        return
    if isinstance(dev, DryRun):
        print("(dry run) would write orientation", a.mode)
        return
    flags = dev.write_orientation(a.mode)
    s.device = None
    print(f"Wrote options 0x{flags:04X}; the display is restarting. Waiting for it...")
    for _ in range(30):
        time.sleep(2)
        found = [f for f in discover(timeout=1.0) if f.ip == dev.ip or f.name == dev.name]
        if found:
            ok = (found[0].flags & (OPT_ORIENT_FIXED | OPT_UPSIDE_DOWN)) == (flags & (OPT_ORIENT_FIXED | OPT_UPSIDE_DOWN))
            print("Display is back. Orientation setting " + ("confirmed." if ok else
                  "did NOT change - see PROTOCOL_NOTES.md."))
            return
    print("The display hasn't answered discovery yet; check it in a minute with `discover`.")


def cmd_netconfig(s, a):
    found = discover()
    if s.args.mac:
        found = [f for f in found if f.mac.lower() == s.args.mac.lower()]
    if not found:
        raise ViewMarqError("no display found")
    f = found[0]
    what = "DHCP" if a.dhcp else f"static {a.static} mask {a.mask} gateway {a.gateway}"
    if not a.yes:
        print(f"Would set {f.name} ({f.mac}) to {what}. This changes saved settings and restarts the "
              f"display; your PC must be on the same subnet afterwards. Run again with --yes.")
        return
    reply = udp_set_network(f, dhcp=a.dhcp, ip=a.static, mask=None if a.dhcp else a.mask,
                            gateway=None if a.dhcp else a.gateway)
    print("Sent. " + ("Display acknowledged." if reply else "No acknowledgement (it may be restarting).")
          + " Run `discover` in a few seconds to see the new address.")
    cache = load_cache()
    cache.pop("display", None)
    save_cache(cache)


CAL_TEXT = ">>START<< " + " ".join(f"{i:02d}" for i in range(1, 46))   # ~16 s per loop at medium
CAL_VAR = 16                                                              # string variable for the clock


def _save_pps(s, speed, period, width):
    pps = (text_width(len(CAL_TEXT), FONTS[0]) + width) / period
    s.cache = load_cache()
    s.cache.setdefault("pps", {})[speed] = pps
    save_cache(s.cache)
    print(f"{speed}: one loop takes {period:.2f} s = {pps:.1f} LEDs/s (saved; the built-in "
          f"estimate was {DEFAULT_PPS[speed]:.1f}).")


def cmd_calibrate(s, a):
    dev = s.dev()
    width = dev.geometry.width
    if a.period:
        _save_pps(s, a.speed, a.period, width)
        return
    _forget_text()
    req = RowRequest(CAL_TEXT, "amber", "none", "left", "on", 0)
    frame = build_text_frame(dev.geometry, dev.display_id, {1: req}, speed=a.speed)
    fragments = list(frame.units)
    if a.clock:
        # A running clock on row 2, updated through a string variable so the
        # marquee on row 1 is never restarted.
        dev.set_string_var(CAL_VAR, "   0.0")
        fragments.append(f"<WIN 0 8 {width - 1} 15><LJ><CS 0><GRN><BL N><T>T=</T><STR {CAL_VAR} 6>")
    for cmd in pack_fragments(dev.display_id, fragments):
        dev.send(cmd)
    start = time.monotonic()
    if a.clock:
        print(f"Row 1 scrolls '>>START<<...' at {a.speed} speed; row 2 shows seconds since it started.\n"
              f"Note the clock each time '>>START<<' appears at the RIGHT edge (two times is enough).")
        while time.monotonic() - start < a.seconds:
            dev.set_string_var(CAL_VAR, f"{time.monotonic() - start:6.1f}")
            time.sleep(0.1)
        hint = (f"Save the result with:  python viewmarq.py calibrate --speed {a.speed} --period P\n"
                f"where P is the time between two appearances (or the first appearance time).")
        try:
            # (on Windows, isatty() is also true for NUL, so EOF is caught instead)
            first = input("Clock value at the first return of >>START<< (Enter to skip): ").strip()
            second = input("...and at the second return (Enter to skip): ").strip() if first else ""
        except EOFError:
            first = second = ""
        if not first:
            print("\n" + hint)
            return
        _save_pps(s, a.speed, float(second) - float(first) if second else float(first), width)
        return
    print("Press Enter each time '>>START<<' enters at the RIGHT edge.\n"
          "Do it 3 or more times, then type q and Enter.")
    marks = []
    while True:
        line = input()
        if line.strip().lower().startswith("q"):
            break
        marks.append(time.monotonic())
        if len(marks) > 1:
            print(f"  {marks[-1] - marks[-2]:.2f} s")
    if len(marks) < 2:
        print("Need at least two presses.")
        return
    periods = sorted(b - a_ for a_, b in zip(marks, marks[1:]))
    _save_pps(s, a.speed, periods[len(periods) // 2], width)


def cmd_demo(s, a):
    _forget_text()
    dev = s.dev()
    g, did = dev.geometry, dev.display_id

    def step(caption, action):
        print(f"- {caption}")
        action()
        if a.delay > 0:
            time.sleep(a.delay)
        else:
            input("    (Enter for next)")

    def leds(*specs):
        return lambda: send_frame(dev, build_led_frame(g, did, paint(specs, g)))

    def text(rows, **kw):
        reqs = {r: RowRequest(**q) for r, q in rows.items()}
        return lambda: send_frame(dev, build_text_frame(g, did, reqs, **kw))

    step("whole board green", leds("all green"))
    step("whole board red", leds("all red"))
    step("whole board amber", leds("all amber"))
    step("top row red, bottom row green", leds("top red", "bottom green"))
    step("left half amber, right half red", leds("left amber", "right red"))
    step("four quadrants in three colours", leds("quadrant 1 red", "quadrant 2 green",
                                                 "quadrant 3 amber", "quadrant 4 red"))
    step("single LEDs: the four corners and the centre",
         leds("pixel 0 0 red", "pixel 71 0 green", "pixel 0 15 amber", "pixel 71 15 red", "pixel 36 8 green"))
    step("border in amber with a red cell", leds("border amber", "cell 1 6 red"))
    step("hardware blink: whole board red, fast", leds("all red fast"))
    step("hardware blink: top green slow, bottom amber fast", leds("top green slow", "bottom amber fast"))
    print("- chase: each LED of the first 12 columns of row 1, one at a time")
    chase(dev, select_leds("rect", [0, 0, 12, 8], g), "GRN", interval=0.1)
    print("- software flash: whole board, cycling red/green/amber")
    flash(dev, paint(["all"], g), times=6, interval=0.4, colors=["red", "green", "amber"])
    step("text on both rows", text({1: dict(text="HELLO WORLD!", color="green"),
                                    2: dict(text="VIEWMARQ", color="red", align="center")}))
    step("long text scrolls automatically, short text doesn't",
         text({1: dict(text="This sentence is too long for one row so it scrolls", color="amber"),
               2: dict(text="I stay put", color="green")}))
    step("spill: one text wraps onto the second row",
         text({1: dict(text="Text that spills onto row two", color="green")}, spill=True))
    step("mixed colours in one row", text({1: dict(text="{red}ALARM {amber}pump 3 {green}OK")}))
    step("big text using both rows", text({1: dict(text="BIG!", color="red", font="widefull")}, big=True))
    step("marquee forced on for short text", text({1: dict(text="Hi!", marquee="on")}))
    dev.clear()
    print("Done.")


def cmd_laptimer(s, a):
    import queue
    _forget_text()
    dev = s.dev()
    folder = os.path.abspath(a.out or LAP_DIR)
    os.makedirs(folder, exist_ok=True)
    timer = LapTimer(dev, folder, COLORS[a.color_current], COLORS[a.color_previous], a.standby_text,
                     min_lap=a.min_lap, speed=a.speed)
    if a.action == "standby":
        timer.standby()
        return
    recovered = recover_unfinished_run(folder)
    if recovered:
        print(f"A previous run was cut off before Ctrl+5; its laps were saved to {recovered}")
    events = queue.Queue()
    keys = None
    if a.global_keys:
        if os.name != "nt":
            raise ViewMarqError("--global hotkeys need Windows")
        keys = GlobalKeys(events)
    elif os.name == "nt":
        try:
            keys = ConsoleKeys(events)
        except OSError:
            keys = None
    if keys is None:
        keys = LineKeys(events)
        print("No console keyboard here: type start / lap / current / previous / stop and press Enter.")
    print(f"Lap timer on {dev.model} - logs go to {folder}\n{LAP_KEYS_HELP}\n")
    try:
        timer.run(events)
    finally:
        keys.close()


COMMANDS = {"text": cmd_text, "leds": cmd_leds, "flash": cmd_flash, "chase": cmd_chase,
            "discover": cmd_discover, "info": cmd_info, "raw": cmd_raw, "setvar": cmd_setvar,
            "orientation": cmd_orientation, "netconfig": cmd_netconfig, "calibrate": cmd_calibrate,
            "demo": cmd_demo, "laptimer": cmd_laptimer,
            "clear": lambda s, a: (_forget_text(), s.dev().clear())}

SHELL_HELP = """\
Commands (same options as the command line, e.g. `text "Hi" --color red`):
  text TEXT [TEXT2]        show text on row 1 (and row 2)   text --help   for options
  leds SELECTION...        light LEDs, e.g.  leds "top red" "pixel 5 3 green fast"
  flash SELECTION...       flash on/off (--times N --interval S --colors red,green)
  chase SELECTION          light LEDs one at a time (--fill to keep them on)
  clear | info | discover | demo | raw <CMD> | setvar N TEXT | calibrate
  laptimer start           lap timer (Ctrl+Shift+S start, Ctrl+Space lap, Ctrl+5 finish)
Shell settings (used when a command doesn't say otherwise):
  marquee auto|on|off      spill on|off           color red|green|amber
  blink none|slow|medium|fast   align left|center|right   font normal|full|small
  speed slow|medium|fast   direction left|right   settings  (show them all)
  help leds                list LED selections     quit
"""


def shell(s, parser):
    print("ViewMarq shell. Type `help` for commands, `quit` to leave.")
    while True:
        try:
            line = input("viewmarq> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            words = shlex.split(line)
        except ValueError as e:
            print(f"error: {e}")
            continue
        head = words[0].lower()
        if head in ("quit", "exit", "q"):
            break
        if head == "help":
            print(SELECTOR_HELP if len(words) > 1 and words[1] in ("leds", "flash", "chase") else SHELL_HELP)
            continue
        if head == "settings":
            for k, v in s.defaults.items():
                print(f"  {k:10s} {v}")
            continue
        if head in DEFAULTS and len(words) == 2:
            value = words[1].lower()
            valid = {"color": COLORS, "blink": BLINKS, "align": ALIGNS, "marquee": ("auto", "on", "off"),
                     "spill": ("on", "off"), "speed": SPEEDS, "direction": ("left", "right"),
                     "font": FONT_BY_NAME, "bigfont": FONT_BY_NAME}[head]
            if value not in valid:
                print(f"{head} must be one of: {', '.join(valid)}")
            else:
                s.defaults[head] = value
                print(f"{head} = {value}")
            continue
        try:
            a = parser.parse_args(words)
        except SystemExit:
            continue
        if a.cmd in (None, "shell"):
            continue
        run(s, a)


def run(s, a):
    try:
        COMMANDS[a.cmd](s, a)
    except ViewMarqError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (OSError, ConnectionError) as e:
        print(f"network error: {e}", file=sys.stderr)
        if s.device is not None:
            s.device.close()
            s.device = None
        return 1
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    s = Session(args)
    if args.cmd in (None, "shell"):
        sub = build_parser()
        shell(s, sub)
        return 0
    return run(s, args)


if __name__ == "__main__":
    sys.exit(main())
