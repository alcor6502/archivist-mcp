"""
render.py — a PDF from a document of blocks, so the bytes never cross the model.

Why this exists
---------------
A PDF written through `write_binary` travels as base64 INSIDE the tool call,
and the tool call is typed by the model, roughly a token every two or three
characters: a 500 KB PDF is a quarter of a million output tokens for a write
the server does in a millisecond. Every constraint the old dashboard script
carried — under 15 KB, base-14 fonts only, cp1252 only — existed to squeeze a
PDF through that hole. Here the model sends a few KB of JSON describing the
page, and the server draws it: real fonts, full Unicode, no ceiling worth
naming.

What it knows, and what it does not
-----------------------------------
Twelve shapes — a title line, a band of stat pills, cards side by side, a
donut, a stat grid, a gauge, a table in sections, a checklist, headings,
paragraphs, rules, notes — and a footer repeated on every page. It knows
nothing about what the numbers mean: a portfolio, an order, a tax bracket are
the caller's business, and so are the checks that the numbers add up. The
caller composes and checks; this draws.

Two guards that are about the OUTPUT, not the meaning: `forbid` — regexes
that must not match any string drawn, checked on the draw log before a byte
is written — and `text_check` — strings that must appear, checked the same
way. The log is the renderer's own record of what it drew, so both look at
what is actually on the page.

Pagination is by flow, and a table section or a checklist group is never
split across pages; a block taller than a page is a refusal, not a cut. Two
passes: the first counts pages, the second prints "pagina N di M".
"""
from __future__ import annotations

import io
import os
import re
from typing import Any

import rxlimit

MAX_DOCUMENT_BYTES = 200_000    # the JSON, serialised — a page is a few KB
MAX_PAGES = 20
MAX_BLOCKS = 400

FONT_DIR = os.environ.get("RENDER_FONT_DIR", "/usr/share/fonts/truetype/liberation")
PAGE_SIZES = {"a4": (595.2756, 841.8898), "letter": (612.0, 792.0)}

TONES = {"navy": "#1F3A5F", "accent": "#2E6E8E", "muted": "#6b7280", "green": "#1a7f52",
         "red": "#b3402f", "black": "#000000", "desc": "#555555", "dark": "#222222"}
TONE_BG = {"navy": "#eef1f6", "accent": "#eef4f8", "green": "#eaf3ee", "red": "#f6ebe9",
           "muted": "#eef0f2", "black": "#eef1f6", "desc": "#eef1f6", "dark": "#eef1f6"}
TAG_STYLES = {"t-ord": ("#f3e6df", "#9a4a1f"), "t-qual": ("#e0efe6", "#1a7f52"),
              "t-muni": ("#e4eef6", "#2E6E8E"), "t-roc": ("#efe9f5", "#6b4b9a"),
              "t-mix": ("#f2eede", "#8a6d1f"), "t-none": ("#eef0f2", "#9aa7b4")}
LINE, BG, SEP = "#e3e8ee", "#f6f8fa", "#cdd8e2"
TITLE_AIR = 6   # least clear space between the title line's text and its value
GAP = 12    # the one unit of breathing room: between cards, around the stats band, under the title rule

# How far the ink of a line rises above its baseline, in ems: the ascent of
# Liberation Sans, the same in all four faces. Until 3.4.0 text was placed by
# its BASELINE with offsets tuned for one size, so a bigger title rose through
# the top of its card and a bigger tick rose into the bar above it: the room
# a field promised was eaten by the type. Placing text by its ink needs this
# number in measure(), which runs without a canvas — so it is written here,
# and the suite reads it back out of the four font files.
ASCENT = 0.72802734375


class RenderError(Exception):
    """The document is wrong: unknown block, missing field, a section taller
    than a page, a forbidden string drawn. The caller's to fix."""


class RenderFault(Exception):
    """The machinery is wrong: fonts missing from the image, reportlab
    absent. Not the caller's to fix."""


# ---------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------

_FONTS: dict[str, str] = {}


# The four faces, in ONE place: _fonts() registers these files and _face()
# refuses anything else by name. Written twice, a caller could pass a face the
# guide lists and the font dict has not got — a KeyError from inside reportlab.
FACE_FILES = {"R": "LiberationSans-Regular.ttf", "B": "LiberationSans-Bold.ttf",
              "I": "LiberationSans-Italic.ttf", "BI": "LiberationSans-BoldItalic.ttf"}
FACES = tuple(FACE_FILES)


def _fonts() -> dict[str, str]:
    """Register Liberation Sans once and hand back the four face names. The
    family is metric-compatible with Helvetica, so a layout designed on the
    base-14 face keeps its measures. Missing files are a fault: the image is
    built with fonts-liberation, and a PDF drawn with a fallback face would
    silently look different from every other."""
    if _FONTS:
        return _FONTS
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError as e:  # pragma: no cover — the suite installs reportlab
        raise RenderFault(f"reportlab is not installed: {e}")
    names = {}
    for key, fn in FACE_FILES.items():
        p = os.path.join(FONT_DIR, fn)
        if not os.path.isfile(p):
            # a build without the italics leans on the upright face rather
            # than on Helvetica, so the page stays one family.
            if key in ("I", "BI"):
                names[key] = names["R" if key == "I" else "B"]
                continue
            raise RenderFault(f"font missing in the image: {p}")
        name = "LS-" + key
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, p))
        names[key] = name
    _FONTS.update(names)
    return _FONTS


# ---------------------------------------------------------------------------
# the canvas wrapper: every string drawn goes through here, and is logged
# ---------------------------------------------------------------------------

class _Page:
    def __init__(self, canvas, fonts, drawn: list[str]):
        from reportlab.lib import colors
        self.c, self.f, self.drawn, self._colors = canvas, fonts, drawn, colors

    def col(self, spec: str):
        return self._colors.HexColor(TONES.get(spec, spec))

    def width(self, s: str, face: str, size: float) -> float:
        return self.c.stringWidth(s, self.f[face], size)

    def clip(self, s: str, face: str, size: float, width: float) -> str:
        """`s` as it fits in `width`: whole, or cut with an ellipsis. For the
        small muted labels only, where a cut word still says what the box is;
        never for a value, which is refused instead."""
        if self.width(s, face, size) <= width:
            return s
        while s and self.width(s + "…", face, size) > width:
            s = s[:-1]
        return s.rstrip() + "…"

    def text(self, x, y, s, face="R", size=9, tone="black", align="left"):
        s = str(s)
        self.drawn.append(s)
        self.c.setFillColor(self.col(tone)); self.c.setFont(self.f[face], size)
        if align == "right":
            self.c.drawRightString(x, y, s)
        elif align == "center":
            self.c.drawCentredString(x, y, s)
        else:
            self.c.drawString(x, y, s)

    def wrap(self, s: str, face: str, size: float, width: float) -> list[str]:
        lines, cur = [], ""
        for word in str(s).split():
            probe = (cur + " " + word).strip()
            if self.width(probe, face, size) <= width or not cur:
                cur = probe
            else:
                lines.append(cur); cur = word
        if cur:
            lines.append(cur)
        return lines or [""]

    def rect(self, x, y, w, h, fill=None, stroke=None, radius=0, lw=0.7):
        c = self.c
        if fill:
            c.setFillColor(self.col(fill))
        if stroke:
            c.setStrokeColor(self.col(stroke)); c.setLineWidth(lw)
        if radius:
            c.roundRect(x, y, w, h, radius, stroke=1 if stroke else 0, fill=1 if fill else 0)
        else:
            c.rect(x, y, w, h, stroke=1 if stroke else 0, fill=1 if fill else 0)

    def line(self, x1, y1, x2, y2, tone=LINE, lw=0.5, dash=None):
        c = self.c
        c.setStrokeColor(self.col(tone)); c.setLineWidth(lw)
        if dash:
            c.setDash(*dash)
        c.line(x1, y1, x2, y2)
        if dash:
            c.setDash()


# ---------------------------------------------------------------------------
# blocks: measure(width) -> height, draw(page, x, top, width)
# ---------------------------------------------------------------------------

def _req(d: dict, key: str, what: str):
    if key not in d:
        raise RenderError(f"{what}: missing field {key!r}")
    return d[key]


def _str(v) -> str:
    return "" if v is None else str(v)


def _fits(p, what: str, kind: str, s: str, face: str, size: float, room: float) -> None:
    """A title-like line that does not fit its room is the caller's to fix:
    refused by name with the overrun, never drawn into its neighbour. Until
    3.4.0 none of these was measured, and they all ran out of their box."""
    w = p.width(s, face, size)
    if w > room:
        raise RenderError(f"{what}: {kind} {s!r} is {w - room:.0f}pt wider than its room "
                          f"({room:.0f}pt): shorten it")


class _Block:
    keeps_together = True     # a block never splits across pages unless it says so

    def __init__(self, spec: dict, what: str):
        self.spec, self.what = spec, what

    def _face(self, v) -> str:
        """A face named by the document. `"bold"` used to come back as a
        KeyError from inside the font dict, with no block named and no clue
        that the four names are these."""
        f = _str(v) or "R"
        if f not in FACES:
            raise RenderError(f"{self.what}: unknown face {f!r} — one of {', '.join(FACES)}")
        return f

    def measure(self, p: _Page, width: float) -> float:
        raise NotImplementedError

    def draw(self, p: _Page, x: float, top: float, width: float) -> None:
        raise NotImplementedError


class Heading(_Block):
    SIZE, H = 13, 22

    def measure(self, p, width):
        return self.H

    def draw(self, p, x, top, width):
        text = _str(_req(self.spec, "text", self.what))
        _fits(p, self.what, "heading", text, "B", self.SIZE, width)
        p.text(x, top - 14, text, "B", self.SIZE, "navy")


class Paragraph(_Block):
    def _lines(self, p, width):
        size = float(self.spec.get("size", 9.5))
        return p.wrap(_req(self.spec, "text", self.what), self._face(self.spec.get("face", "R")), size, width), size

    def measure(self, p, width):
        lines, size = self._lines(p, width)
        return len(lines) * size * 1.35 + 4

    def draw(self, p, x, top, width):
        lines, size = self._lines(p, width)
        y = top - size
        for ln in lines:
            p.text(x, y, ln, self._face(self.spec.get("face", "R")), size, self.spec.get("tone", "black"))
            y -= size * 1.35


class Note(_Block):
    """A footnote. Until 3.4.0 it was one line whatever its length, and a long
    one ran off the page; it now wraps like a paragraph, and a note that fits
    on one line draws exactly as before."""
    LEAD = 1.35

    def _lines(self, p, width):
        size = float(self.spec.get("size", 6.5))
        face = "I" if self.spec.get("italic", True) else "R"
        return p.wrap(_req(self.spec, "text", self.what), face, size, width), face, size

    def measure(self, p, width):
        lines, _, size = self._lines(p, width)
        return size + 8 + (len(lines) - 1) * size * self.LEAD

    def draw(self, p, x, top, width):
        lines, face, size = self._lines(p, width)
        align = self.spec.get("align", "left")
        xx = {"right": x + width, "center": x + width / 2}.get(align, x)
        y = top - size - 2
        for ln in lines:
            p.text(xx, y, ln, face, size, self.spec.get("tone", "red"), align)
            y -= size * self.LEAD


class Rule(_Block):
    def measure(self, p, width):
        return float(self.spec.get("gap", 8))

    def draw(self, p, x, top, width):
        g = float(self.spec.get("gap", 8))
        p.line(x, top - g / 2, x + width, top - g / 2, self.spec.get("tone", LINE),
               float(self.spec.get("lw", 0.5)))


class Spacer(_Block):
    def measure(self, p, width):
        return float(self.spec.get("height", 8))

    def draw(self, p, x, top, width):
        pass


class Stats(_Block):
    """A band of pills on ONE row, the same gap a row of cards has, spanning
    the full width: each pill takes what its content needs and the leftover
    is shared equally — so the band lines up with whatever sits under it. A
    band whose contents do not fit the row is a refusal — the old script's
    assert, kept — not a squeeze."""
    LS, VS, PAD, H = 7.8, 9.5, 10, 17
    GAP = GAP

    def _widths(self, p, items, width):
        need = [p.width(_str(it.get("label")), "R", self.LS) + 5
                + p.width(_str(it.get("value")), "B", self.VS) + 2 * self.PAD for it in items]
        room = width - self.GAP * (len(items) - 1)
        if sum(need) > room:
            raise RenderError(f"{self.what}: the stats band does not fit on one row "
                              f"({sum(need):.0f}pt of {room:.0f}): shorten a label")
        extra = (room - sum(need)) / len(items)
        return [n + extra for n in need]

    def measure(self, p, width):
        items = _req(self.spec, "items", self.what)
        if not items:
            raise RenderError(f"{self.what}: stats needs at least one item")
        self._widths(p, items, width)
        return self.H + self.GAP          # the pills, then one unit before the next block

    def draw(self, p, x, top, width):
        items = self.spec["items"]
        y = top - self.H
        for it, w in zip(items, self._widths(p, items, width)):
            tone = it.get("tone", "navy")
            p.rect(x, y, w, self.H, fill=it.get("bg", TONE_BG.get(tone, "#eef1f6")), radius=8)
            p.text(x + self.PAD, y + 5, _str(it.get("label")), "R", self.LS, "desc")
            p.text(x + self.PAD + p.width(_str(it.get("label")), "R", self.LS) + 5, y + 4,
                   _str(it.get("value")), "B", self.VS, tone)
            x += w + self.GAP


class Grid(_Block):
    """Small stat boxes in columns: label above, value below.

    Until 3.4.0 neither was measured against its box: a long label ran under
    the next box, or out of the card when it was the last one. Now the label
    is cut with an ellipsis — it names the box, a cut word still does — and a
    value wider than its box is a refusal, because a number cut short is a
    different number."""
    BOX_H, GAP_X, GAP_Y = 28, 8, 6
    INSET = 8          # text from the box's left edge, and room kept on its right

    def _rows(self):
        items = _req(self.spec, "items", self.what)
        cols = int(self.spec.get("cols", 2))
        if cols < 1:
            raise RenderError(f"{self.what}: grid cols must be at least 1, got {cols}")
        return [items[i:i + cols] for i in range(0, len(items), cols)], cols

    def measure(self, p, width):
        rows, _ = self._rows()
        return len(rows) * (self.BOX_H + self.GAP_Y)

    def draw(self, p, x, top, width):
        rows, cols = self._rows()
        bw = (width - (cols - 1) * self.GAP_X) / cols
        y = top
        for row in rows:
            y -= self.BOX_H
            for i, it in enumerate(row):
                xx = x + i * (bw + self.GAP_X)
                room = bw - 2 * self.INSET
                value = _str(it.get("value"))
                if p.width(value, "B", 11.5) > room:
                    raise RenderError(
                        f"{self.what}: grid value {value!r} is {p.width(value, 'B', 11.5) - room:.0f}pt "
                        f"wider than its box ({room:.0f}pt): fewer columns, or a shorter value")
                p.rect(xx, y, bw, self.BOX_H, fill=BG, radius=5)
                p.text(xx + self.INSET, y + 18, p.clip(_str(it.get("label")).upper(), "R", 6.6, room),
                       "R", 6.6, "muted")
                p.text(xx + self.INSET, y + 5, value, "B", 11.5, it.get("tone", "navy"))
            y -= self.GAP_Y


class Gauge(_Block):
    """Coloured bands with a marker at `position` (0-100) and labels below.

    The label line, each tick and the bar's thickness are settable, and every
    one of them defaults to the value this block has always drawn: a document
    that names none of them draws exactly as before.

    The two label offsets are held in EMS of the label's own size, so a bigger
    label keeps its proportions instead of sitting in a box measured for 6.6pt.
    At the default size they are exactly the 5 and 12 that used to be written
    here as bare numbers — that is the arithmetic below, not a coincidence.

    3.4.0, judged on a printed page: the bar was 18 and the ticks hung at a
    fixed baseline 9 under it, which left 4.5pt between bar and digits at the
    default size and NONE at 14pt, where the digits rose into the bar. The bar
    is now 14, and TICK_GAP is the clear space between the bar's foot and the
    top of the tick's INK, whatever its size: 7, so the digits stand apart."""
    TOP_GAP = 8
    LABEL_SIZE, LABEL_FACE, LABEL_TONE = 6.6, "R", "muted"
    LABEL_DROP = 5.0 / LABEL_SIZE      # the label baseline under the top gap
    LABEL_LEAD = 12.0 / LABEL_SIZE     # the line the label takes up
    THICKNESS = 14
    TICK_SIZE, TICK_FACE, TICK_TONE = 6.2, "R", "muted"
    TICK_GAP = 7                       # clear space from the bar's foot to the tick's ink
    DESCENT = 0.25                     # room under a baseline for the descenders

    def _shape(self):
        """Everything measure() and draw() must agree on, read once. They used
        to agree by both being written by hand, which is how H stayed 46 while
        the drawing outgrew it."""
        spec = self.spec
        label = _str(spec.get("label"))
        label_size = float(spec.get("label_size", self.LABEL_SIZE))
        if label and label_size <= 0:
            raise RenderError(f"{self.what}: gauge label_size must be above 0, got {label_size}")
        thickness = float(spec.get("thickness", self.THICKNESS))
        if thickness <= 0:
            raise RenderError(f"{self.what}: gauge thickness must be above 0, got {thickness}")
        ticks = spec.get("ticks") or []
        # The tail is measured on the BIGGEST tick, not on the default: one
        # tick asking for 12pt would otherwise hang out of the measured box.
        tail = (self.TICK_GAP + max(float(t.get("size", self.TICK_SIZE)) for t in ticks) * (ASCENT + self.DESCENT)
                if ticks else 0.0)
        head = label_size * self.LABEL_LEAD if label else 0.0
        return label, label_size, thickness, ticks, head, tail

    def measure(self, p, width):
        pos = float(_req(self.spec, "position", self.what))
        if not 0 <= pos <= 100:
            raise RenderError(f"{self.what}: gauge position {pos} is not within 0-100")
        _, _, thickness, _, head, tail = self._shape()
        return self.TOP_GAP + head + thickness + tail

    def draw(self, p, x, top, width):
        spec = self.spec
        label, label_size, thickness, ticks, head, _ = self._shape()
        y = top - self.TOP_GAP
        if label:
            text = label.upper() if spec.get("label_upper", True) else label
            face = self._face(spec.get("label_face", self.LABEL_FACE))
            if p.width(text, face, label_size) > width:
                raise RenderError(
                    f"{self.what}: gauge label {text!r} is {p.width(text, face, label_size) - width:.0f}pt "
                    f"wider than the gauge ({width:.0f}pt): shorten it")
            p.text(x, y - label_size * self.LABEL_DROP, text, face, label_size,
                   spec.get("label_tone", self.LABEL_TONE))
            y -= head
        bar_y = y - thickness
        start = 0.0
        for band in spec.get("bands", [{"to": 100, "color": BG}]):
            to = float(band.get("to", 100)) / 100
            p.rect(x + width * start, bar_y, width * (to - start), thickness, fill=band.get("color", BG))
            start = to
        pos = float(spec["position"]) / 100
        p.rect(x + width * pos - 1.5, bar_y - 2, 3, thickness + 4, fill=spec.get("marker", "accent"))
        for t in ticks:
            at = float(t.get("at", 0)) / 100
            size, text = float(t.get("size", self.TICK_SIZE)), _str(t.get("text"))
            face, align = self._face(t.get("face", self.TICK_FACE)), t.get("align", "center")
            # A tick near an end slides inward instead of hanging past the bar:
            # its ink is kept between x and x + width, whatever its alignment.
            w = p.width(text, face, size)
            lead = {"right": w, "center": w / 2}.get(align, 0.0)
            tx = min(max(x + width * at, x + lead), x + width - (w - lead))
            p.text(tx, bar_y - self.TICK_GAP - ASCENT * size, text, face, size,
                   t.get("tone", self.TICK_TONE), align)


class Donut(_Block):
    """Sectors drawn, not a legend pretending: slices with value, label and
    colour; the centre may carry a figure and a caption."""
    R, RI, ROW = 42, 24, 18

    def measure(self, p, width):
        slices = _req(self.spec, "slices", self.what)
        if not slices or sum(float(s.get("value", 0)) for s in slices) <= 0:
            raise RenderError(f"{self.what}: donut needs slices whose values sum above zero")
        for s in slices:
            if float(s.get("value", 0)) < 0:
                raise RenderError(f"{self.what}: slice {_str(s.get('label'))!r} has a negative value")
        return max(2 * self.R + 8, len(slices) * self.ROW + 4)

    def draw(self, p, x, top, width):
        slices = self.spec["slices"]
        total = sum(float(s.get("value", 0)) for s in slices)
        h = self.measure(p, width)
        cx, cy = x + self.R + 6, top - h / 2
        a0 = 90.0
        c = p.c
        for s in slices:
            ext = -(float(s.get("value", 0)) / total) * 360
            if ext == 0:
                # An empty class is a normal allocation, not an error: it keeps
                # its line in the legend (0.0%) and draws no sector — an arc of
                # extent zero is a division by zero inside reportlab.
                continue
            c.setFillColor(p.col(s.get("color", "accent"))); c.setStrokeColor(p.col("#ffffff")); c.setLineWidth(1.1)
            path = c.beginPath(); path.moveTo(cx, cy)
            path.arcTo(cx - self.R, cy - self.R, cx + self.R, cy + self.R, a0, ext); path.close()
            c.drawPath(path, stroke=1, fill=1); a0 += ext
        c.setFillColor(p.col("#ffffff")); c.circle(cx, cy, self.RI, stroke=0, fill=1)
        if self.spec.get("center"):
            p.text(cx, cy + 1, _str(self.spec["center"]), "B", 10.5, "navy", "center")
            if self.spec.get("center_label"):
                p.text(cx, cy - 9, _str(self.spec["center_label"]), "R", 6.2, "muted", "center")
        lx, ly = cx + self.R + 16, top - 12
        for s in slices:
            pct = "%.1f%%" % (float(s.get("value", 0)) / total * 100)
            # The label ran under its own percentage until 3.4.0: it gets the
            # room the percentage leaves, and an ellipsis past that.
            room = x + width - (lx + 14) - p.width(pct, "B", 9.5) - 6
            p.rect(lx, ly, 8, 8, fill=s.get("color", "accent"), radius=2)
            p.text(lx + 14, ly + 1, p.clip(_str(s.get("label")), "R", 8, room), "R", 8, "desc")
            p.text(x + width, ly + 1, pct, "B", 9.5, "navy", "right")
            p.line(lx, ly - 4, x + width, ly - 4, LINE, 0.5, dash=(1, 2)); ly -= self.ROW


class Card(_Block):
    """A white box with a border, optionally titled.

    `PAD` used to be ONE number doing four jobs — the left and right inset,
    the top gap, the bottom gap — and `TITLE_H` a constant that had no idea
    what size the title was drawn at. On a card holding a gauge that reads
    wrong: under the last line there is the gauge's own tick tail AND the
    bottom padding, while above sits a title band of 24. Each gap is now its
    own field, and the title band is derived from the title's size.

    3.4.0, judged on a printed page. `pad_top` promised the room above the
    title, but the title was placed by its baseline (`pad_top` + 6/13 em), so
    its capitals rose 0.73 em above that and ate the gap: at pad_top 4 and
    size 20 they crossed the border. The title is now placed by its INK, so
    `pad_top` is exactly the clear room above it. Its width was measured by
    nobody either: in three cards side by side a 13pt title ran past the
    border at its default size. The default is now 11, a title wider than the
    inset shrinks to fit down to TITLE_FIT_MIN of its size, and narrower than
    that it is a refusal. Stretched to a taller neighbour, the content stays
    under the title and the spare room goes to the foot: centred, it opened a
    hole between the two."""
    PAD, TITLE_SIZE = 12, 11
    TITLE_BELOW = 6.0 / 13   # room under the title's baseline, in ems of its size
    TITLE_FIT_MIN = 0.75     # how far a title may shrink to fit before it is refused

    def __init__(self, spec, what):
        super().__init__(spec, what)
        self.children = [_make(b, f"{what}.blocks[{i}]") for i, b in enumerate(spec.get("blocks", []))]

    def _pads(self):
        """The four measures, read ONCE. measure() and draw() used to agree by
        both spelling out self.PAD; a field read by one and not the other is a
        box that does not hold its content."""
        spec = self.spec
        pad = float(spec.get("padding", self.PAD))
        top = float(spec.get("pad_top", pad))
        bottom = float(spec.get("pad_bottom", pad))
        for name, v in (("padding", pad), ("pad_top", top), ("pad_bottom", bottom)):
            if v < 0:
                raise RenderError(f"{self.what}: card {name} cannot be negative, got {v}")
        size = float(spec.get("title_size", self.TITLE_SIZE))
        if size <= 0:
            raise RenderError(f"{self.what}: card title_size must be above 0, got {size}")
        # pad_top is the clear room above the title's ink; the band the title
        # adds is its ink plus the room under its baseline. Measured on the
        # declared size: a title shrunk to fit only leaves a little more air.
        head = top + (size * (ASCENT + self.TITLE_BELOW) if spec.get("title") else 0.0)
        return pad, top, bottom, size, head

    def _title_size(self, p, width, pad, size):
        """The size the title is DRAWN at: the declared one, or smaller so it
        fits between the two insets — and not below TITLE_FIT_MIN of it."""
        t = _str(self.spec["title"])
        room = width - 2 * pad
        w = p.width(t, "B", size)
        if w <= room:
            return size
        if room / w < self.TITLE_FIT_MIN:
            raise RenderError(
                f"{self.what}: card title {t!r} needs {w:.0f}pt at size {size:g} and the card "
                f"has {room:.0f}pt between its insets — even at {self.TITLE_FIT_MIN:.0%} of its "
                f"size it does not fit: shorten it, or give the card more width")
        return size * room / w

    def measure(self, p, width):
        pad, _, bottom, _, head = self._pads()
        inner = width - 2 * pad
        return head + sum(b.measure(p, inner) for b in self.children) + bottom

    def draw(self, p, x, top, width, height=None):
        pad, pad_top, _, size, head = self._pads()
        natural = self.measure(p, width)
        h = height or natural
        p.rect(x, top - h, width, h, fill="#ffffff", stroke=LINE, radius=7)
        y = top - head
        if self.spec.get("title"):
            p.text(x + pad, top - pad_top - ASCENT * size, _str(self.spec["title"]), "B",
                   self._title_size(p, width, pad, size), "navy")
        # Stretched to a neighbour's height, the content stays under the title
        # and the spare room goes to the foot (until 3.4.0 it was centred).
        inner = width - 2 * pad
        for b in self.children:
            b.draw(p, x + pad, y, inner); y -= b.measure(p, inner)


class Row(_Block):
    """Blocks side by side, equal widths, cards stretched to the tallest."""
    GAP = GAP

    def __init__(self, spec, what):
        super().__init__(spec, what)
        items = _req(spec, "items", what)
        if not items:
            raise RenderError(f"{what}: row needs at least one item")
        self.children = [_make(b, f"{what}.items[{i}]") for i, b in enumerate(items)]

    def _w(self, width):
        return (width - self.GAP * (len(self.children) - 1)) / len(self.children)

    def measure(self, p, width):
        w = self._w(width)
        return max(b.measure(p, w) for b in self.children)

    def draw(self, p, x, top, width):
        w = self._w(width)
        h = self.measure(p, width)
        for b in self.children:
            if isinstance(b, Card):
                b.draw(p, x, top, w, height=h)
            else:
                b.draw(p, x, top, w)
            x += w + self.GAP


class Table(_Block):
    """Columns declared once; rows grouped in sections that never split. The
    first column is name + description: the name bold, the rest muted and
    truncated with an ellipsis rather than overflowing into the numbers.
    Other cells: {text, tone, bold, sub} or {tag, style|bg,fg}."""
    HDR, ROW, GAP, TITLE_H = 17, 16, 8, 22
    keeps_together = False        # sections keep together; the table flows

    def __init__(self, spec, what):
        super().__init__(spec, what)
        self.columns = _req(spec, "columns", what)
        self.sections = _req(spec, "sections", what)
        if not self.columns or not self.sections:
            raise RenderError(f"{what}: table needs columns and sections")

    def _xs(self, x, width):
        """Column boundaries: the first column takes what the others leave."""
        rest = [float(c.get("width", 0.12)) for c in self.columns[1:]]
        for c, w in zip(self.columns[1:], rest):
            if not 0 < w < 0.9:
                raise RenderError(f"{self.what}: column {_str(c.get('label'))!r} has width {w}: a fraction of the row, above 0 and below 0.9")
        if sum(rest) >= 0.9:
            raise RenderError(f"{self.what}: the declared column widths leave no room for the first column")
        edges, cur = [], x + width * (1 - sum(rest))
        for w in rest:
            edges.append(cur); cur += width * w
        return edges

    def section_height(self, s):
        return self.HDR + self.ROW * len(s.get("rows", [])) + self.GAP

    def measure(self, p, width):
        return (self.TITLE_H if self.spec.get("title") else 0) + sum(self.section_height(s) for s in self.sections)

    def draw(self, p, x, top, width):
        """A table nested in a card or a row: title then every section, in the
        room measure() asked for. Without this, Card.draw called the _Block
        stub and the caller got `the renderer failed on a row block
        (NotImplementedError: )` — the wrong block named and nothing to act on.
        The page-breaking version lives in _Doc.render, and only there: inside
        a card there is no next page to break to, so a table taller than its
        page is refused as the card it sits in."""
        y = top - self.draw_title(p, x, top, width)
        for s in self.sections:
            self.draw_section(p, x, y, width, s)
            y -= self.section_height(s)

    def draw_title(self, p, x, top, width):
        if self.spec.get("title"):
            _fits(p, self.what, "table title", _str(self.spec["title"]), "B", 13, width)
            p.text(x, top - 16, _str(self.spec["title"]), "B", 13, "navy")
            return self.TITLE_H
        return 0

    def draw_section(self, p, x, top, width, s):
        edges = self._xs(x, width)
        y = top - 11
        gtop = y + 11
        # The section title shares its line with the column labels: its room
        # is the first column. The title is refused past it, the subtitle — a
        # description, like the first column's `sub` — is cut with an ellipsis.
        room = edges[0] - x - 6
        title = _str(s.get("title"))
        _fits(p, self.what, "section title", title, "BI", 10.5, room)
        p.text(x, y, title, "BI", 10.5, "navy")
        if s.get("subtitle"):
            sx = x + p.width(title, "BI", 10.5) + 7
            p.text(sx, y, p.clip("— " + _str(s["subtitle"]), "R", 8, x + room - sx), "R", 8, "muted")
        for col, e0 in zip(self.columns[1:], edges):
            w = (edges[edges.index(e0) + 1] if edges.index(e0) + 1 < len(edges) else x + width) - e0
            align = col.get("align", "right")
            label = _str(col.get("label")).upper()
            if p.width(label, "B", 6.8) + (4 if align == "center" else 15) > w:
                raise RenderError(f"{self.what}: the column label {label!r} is wider than its column: widen it or shorten the label")
            xx = e0 + w - 9 if align == "right" else (e0 + w / 2 if align == "center" else e0 + 6)
            p.text(xx, y, label, "B", 6.8, "muted", align)
        p.line(x, y - 5, x + width, y - 5, "navy", 1.1)
        y -= self.HDR
        for row in s.get("rows", []):
            cells = row.get("cells", [])
            if len(cells) != len(self.columns):
                raise RenderError(f"{self.what}: a row has {len(cells)} cells for {len(self.columns)} columns")
            first = cells[0]
            name = _str(first.get("text"))
            p.text(x, y, name, "B", 8.6, "black")
            xd = x + p.width(name, "B", 8.6) + 7
            desc = _str(first.get("sub"))
            if desc:
                room = (edges[0] if edges else x + width) - 6 - xd
                t = "— " + desc
                while p.width(t, "R", 8) > room and len(t) > 4:
                    t = t[:-2] + "…"
                p.text(xd, y, t, "R", 8, "desc")
            for i, (col, cell) in enumerate(zip(self.columns[1:], cells[1:])):
                e0 = edges[i]; e1 = edges[i + 1] if i + 1 < len(edges) else x + width
                align = col.get("align", "right")
                if "tag" in cell:
                    bg, fg = TAG_STYLES.get(cell.get("style", "t-none"), (cell.get("bg", "#eef0f2"), cell.get("fg", "#9aa7b4")))
                    tw = p.width(_str(cell["tag"]), "B", 6.6) + 12
                    cx = (e0 + e1) / 2
                    p.rect(cx - tw / 2, y - 3, tw, 12, fill=bg, radius=3)
                    p.text(cx, y, _str(cell["tag"]), "B", 6.6, fg, "center")
                    continue
                xx = e1 - 9 if align == "right" else (e0 + 6 if align == "left" else (e0 + e1) / 2)
                main = _str(cell.get("text"))
                tone = cell.get("tone", "navy" if cell.get("bold") else "dark")
                # ⚠ A cell wider than its column would be drawn over the neighbour
                # and the page would look fine at a glance. It is a refusal that
                # names the column and the text, like the old script's asserts.
                need = p.width(main, "B" if cell.get("bold", bool(cell.get("sub"))) else "R", 8.6)
                if cell.get("sub"):
                    need += p.width(_str(cell["sub"]), "R", 6.8) + 4
                if need > (e1 - e0) - 14:
                    raise RenderError(f"{self.what}: {main!r} does not fit the column "
                                      f"{col.get('label')!r} ({need:.0f}pt of {e1 - e0 - 14:.0f}): "
                                      "widen it or shorten the text")
                if cell.get("sub"):
                    sub = _str(cell["sub"])
                    p.text(xx, y, sub, "R", 6.8, "muted", align)
                    p.text(xx - p.width(sub, "R", 6.8) - 4, y, main, "B" if cell.get("bold", True) else "R", 8.6, tone, align)
                else:
                    p.text(xx, y, main, "B" if cell.get("bold") else "R", 8.6 if cell.get("bold") else 8.4, tone, align)
            p.line(x, y - 5, x + width, y - 5, LINE, 0.5)
            y -= self.ROW
        for e in edges:
            p.line(e, gtop, e, y + 11, SEP, 0.8)


class Checklist(_Block):
    """Numbered rows with a DRAWN box, columns sized on the content of this
    sheet. Groups never split. The box is a rectangle, never a glyph: the
    glyph that is missing prints a black square and nobody notices on screen."""
    BOX, ROW, TITLE_H, GAP = 10.2, 22.7, 24, 6

    SIZE, LEAD = 11, 15

    def _layout(self, p, width):
        """Column widths from the content, the text start, and per item the
        lines it takes: a one-column item wraps, a multi-column one must fit."""
        items = _req(self.spec, "items", self.what)
        if not items:
            raise RenderError(f"{self.what}: checklist needs at least one item")
        numbered = self.spec.get("numbered", True)
        x0 = self.BOX + 8.5 + (p.width("88. ", "R", self.SIZE) if numbered else 0)
        cols = max(len(it.get("cols", [])) for it in items)
        widths = [max((p.width(_str(it["cols"][i]), "R", self.SIZE) if i < len(it.get("cols", [])) else 0)
                      for it in items) + 11 for i in range(cols)]
        lines = []
        for it in items:
            c = it.get("cols", [])
            if len(c) <= 1:
                lines.append(p.wrap(_str(c[0]) if c else "", "R", self.SIZE, width - x0))
            else:
                if x0 + sum(widths[:len(c)]) > width:
                    raise RenderError(f"{self.what}: a checklist row is wider than the page "
                                      f"({x0 + sum(widths[:len(c)]):.0f}pt of {width:.0f}): shorten a column")
                lines.append([None])
        return items, numbered, x0, widths, lines

    def measure(self, p, width):
        items, _, _, _, lines = self._layout(p, width)
        h = (self.TITLE_H if self.spec.get("title") else 4) + self.GAP
        for ln in lines:
            h += self.ROW + (len(ln) - 1) * self.LEAD
        return h

    def draw(self, p, x, top, width):
        items, numbered, x0, widths, lines = self._layout(p, width)
        y = top
        if self.spec.get("title"):
            _fits(p, self.what, "checklist title", _str(self.spec["title"]), "B", 12, width)
            p.text(x, y - 16, _str(self.spec["title"]), "B", 12, "black"); y -= self.TITLE_H
        else:
            y -= 4
        start = int(self.spec.get("start", 1))
        for n, (it, ln) in enumerate(zip(items, lines), start):
            y -= self.ROW * 0.65
            p.rect(x, y - 1.7, self.BOX, self.BOX, stroke="black", lw=0.9)
            cx = x + self.BOX + 8.5
            if numbered:
                p.text(cx, y, f"{n}.", "R", self.SIZE, "black"); cx += p.width("88. ", "R", self.SIZE)
            if ln[0] is None:
                for i, val in enumerate(it.get("cols", [])):
                    p.text(cx, y, _str(val), "R", self.SIZE, "black"); cx += widths[i]
            else:
                for k, text in enumerate(ln):
                    p.text(cx, y - k * self.LEAD, text, "R", self.SIZE, "black")
                y -= (len(ln) - 1) * self.LEAD
            y -= self.ROW * 0.35


_TYPES = {"heading": Heading, "paragraph": Paragraph, "note": Note, "rule": Rule, "spacer": Spacer,
          "stats": Stats, "grid": Grid, "gauge": Gauge, "donut": Donut, "card": Card, "row": Row,
          "table": Table, "checklist": Checklist}


def _make(spec: Any, what: str) -> _Block:
    if not isinstance(spec, dict):
        raise RenderError(f"{what}: a block is an object, got {type(spec).__name__}")
    t = spec.get("type")
    if t not in _TYPES:
        raise RenderError(f"{what}: unknown block type {t!r} — one of {', '.join(sorted(_TYPES))}")
    return _TYPES[t](spec, what)


# ---------------------------------------------------------------------------
# the document
# ---------------------------------------------------------------------------

class _Doc:
    def __init__(self, doc: dict):
        if not isinstance(doc, dict):
            raise RenderError("document must be a JSON object")
        page = doc.get("page", {})
        size = str(page.get("size", "a4")).lower()
        if size not in PAGE_SIZES:
            raise RenderError(f"page.size must be one of {', '.join(PAGE_SIZES)}")
        self.W, self.H = PAGE_SIZES[size]
        if page.get("landscape"):
            self.W, self.H = self.H, self.W
        self.M = float(page.get("margin", 28))
        self.title = doc.get("title") or {}
        self.footer = [str(x) for x in (doc.get("footer") or [])]
        self.forbid = [str(x) for x in (doc.get("forbid") or [])]
        self.text_check = [str(x) for x in (doc.get("text_check") or [])]
        blocks = doc.get("blocks") or []
        if len(blocks) > MAX_BLOCKS:
            raise RenderError(f"too many blocks ({len(blocks)}, max {MAX_BLOCKS})")
        self.blocks = [_make(b, f"blocks[{i}]") for i, b in enumerate(blocks)]
        if not self.blocks and not self.title:
            raise RenderError("an empty document: give it a title or blocks")
        for rx in self.forbid:
            try:
                rxlimit.compile(rx)
            except rxlimit.error as e:
                raise RenderError(f"forbid: invalid regex {rx!r}: {e}")

    def render(self, out: io.BytesIO, total_pages: int | None, drawn: list[str]) -> int:
        from reportlab.pdfgen import canvas as _canvas
        fonts = _fonts()
        c = _canvas.Canvas(out, pagesize=(self.W, self.H))
        c.setTitle(_str(self.title.get("text") or "document"))
        p = _Page(c, fonts, drawn)
        M, W, H = self.M, self.W, self.H
        AV = W - 2 * M
        state = {"page": 1}

        def footer_lines(page_label):
            lines = list(self.footer)
            if lines:
                lines[-1] = f"{lines[-1]} · {page_label}"
            else:
                lines = [page_label]
            return [ln for b in lines for ln in p.wrap(b, "R", 8, AV)]

        n_foot = len(footer_lines("pagina 99 di 99"))
        rule_y = M + 2 + (n_foot - 1) * 10 + 12
        bottom = rule_y + 10

        def footer():
            label = f"pagina {state['page']} di {total_pages}" if total_pages else f"pagina {state['page']}"
            lines = footer_lines(label)
            if len(lines) > n_foot:
                raise RenderError("the footer grew past the space measured for it")
            p.line(M, rule_y, W - M, rule_y, LINE, 0.5)
            y = M + 2 + (len(lines) - 1) * 10
            for ln in lines:
                p.text(W / 2, y, ln, "R", 8, "muted", "center"); y -= 10

        def title_parts(follows):
            """The title line as it will be drawn: [(text, face, size, gap
            after)] from the left, and the value that comes in from the right
            on the first page."""
            s = _str(self.title.get("subtitle")) + ("  ·  (segue)" if follows else "")
            badge = self.title.get("badge")
            left = [(_str(self.title.get("text")), "B", 19, 10)]
            if s:
                left.append((s, "R", 8.5, 6))
            if badge:
                left.append(("· " + _str(badge), "B", 8.5, 0))
            value = "" if follows else _str(self.title.get("value"))
            return left, value

        def title_fits(follows):
            """Until 4.2.1 nothing measured this line: text, subtitle and
            badge were laid from the left, the value from the right, and a
            long subtitle ran under the value and off the page with
            `missing: []` in the answer — seen only by looking at the page.
            Refused like every other title-like line, with the overrun. The
            line has two shapes and each is asked where it is drawn: the first
            page carries the value, a page after it carries "(segue)" instead
            — so a one-page document is never refused for a second page it
            has not got."""
            left, value = title_parts(follows)
            used = sum(p.width(t, f, sz) + gap for t, f, sz, gap in left)
            used -= left[-1][3]
            # TITLE_AIR, not GAP: a fixture of this repository leaves 11pt
            # there and reads well. The guard is against text UNDER the
            # value, not a rule of style, so it asks for the least air that
            # still keeps the two apart.
            room = W - 2 * M - ((p.width(value, "B", 21) + TITLE_AIR) if value else 0)
            if used > room:
                where = "on the pages after the first" if follows else (
                    "beside the value" if value else "on the page")
                raise RenderError(
                    f"title: text, subtitle and badge take {used:.0f}pt and the line has "
                    f"{room:.0f}pt {where}: {used - room:.0f}pt too wide — shorten the subtitle, or the title")


        def title_line(y, follows=False):
            if not _str(self.title.get("text")):
                return y
            title_fits(follows)
            left, value = title_parts(follows)
            x = M
            for i, (t, face, size, gap) in enumerate(left):
                # The title sits on the baseline; what follows it is small
                # type, raised a point to read as centred on it.
                p.text(x, y + (1 if i else 0), t, face, size,
                       "black" if not i else ("red" if face == "B" else "muted"))
                x += p.width(t, face, size) + gap
            if value:
                p.text(W - M, y - 1, value, "B", 21, "navy", "right")
            p.line(M, y - 11, W - M, y - 11, "navy", 1.6)
            return y - 11 - GAP

        def new_page():
            footer(); c.showPage(); state["page"] += 1
            if state["page"] > MAX_PAGES:
                raise RenderError(f"more than {MAX_PAGES} pages: the document is too long")
            return title_line(H - M - 14, follows=True)

        y = title_line(H - M - 14)
        for b in self.blocks:
            try:
                if isinstance(b, Table):
                    th = b.draw_title(p, M, y, AV)
                    if th and y - th - b.section_height(b.sections[0]) < bottom:
                        y = new_page(); th = b.draw_title(p, M, y, AV)
                    y -= th
                    for s in b.sections:
                        need = b.section_height(s)
                        if y - need < bottom:
                            if need > H - M - 14 - 40 - bottom:
                                raise RenderError(f"{b.what}: section {s.get('title')!r} is taller than a page ({need:.0f}pt)")
                            y = new_page()
                        b.draw_section(p, M, y, AV, s); y -= need
                    continue
                need = b.measure(p, AV)
                if y - need < bottom:
                    if need > H - M - 14 - 40 - bottom:
                        raise RenderError(f"{b.what}: a {b.spec.get('type')} block taller than a page ({need:.0f}pt)")
                    y = new_page()
                b.draw(p, M, y, AV); y -= need
            except (RenderError, RenderFault):
                raise
            except Exception as e:
                # A block that blows up inside the drawing library came out as a
                # bare `float division by zero` with no block named: whatever
                # escapes here is a fault of ours, and it says where.
                raise RenderFault(f"{b.what}: the renderer failed on a {b.spec.get('type')!s} block "
                                  f"({type(e).__name__}: {e})") from e
        footer(); c.showPage(); c.save()
        return state["page"]


def build(document: dict) -> tuple[bytes, dict]:
    """The PDF bytes and a verdict: pages, size, strings drawn, and the two
    checks — `forbidden` (which pattern matched what) and `missing` (which
    text_check items were never drawn). A forbidden match is a RenderError:
    nothing is handed back that must not exist."""
    doc = _Doc(document)
    drawn: list[str] = []
    n = doc.render(io.BytesIO(), None, drawn)        # first pass: count pages
    drawn.clear()
    out = io.BytesIO()
    doc.render(out, n, drawn)                         # second pass: "pagina N di M"
    # The patterns are the caller's, so they run on a clock: one budget for the
    # whole list, and a pattern that uses it up refuses the document — a guard
    # that could not finish has not passed.
    budget = rxlimit.Budget()
    for rx in doc.forbid:
        crx = rxlimit.compile(rx)
        try:
            hit = next((s for s in drawn if budget.search(crx, s)), None)
        except rxlimit.RegexTimeout:
            raise RenderError(f"forbid: the patterns took more than {budget.seconds:g} s to run "
                              f"(stopped in {rx!r}): simplify it — nothing written") from None
        if hit is not None:
            raise RenderError(f"forbidden pattern {rx!r} matches a string on the page: {hit[:60]!r}")
    missing = [t for t in doc.text_check if not any(t in s for s in drawn)]
    data = out.getvalue()
    return data, {"pages": n, "size": len(data), "strings_drawn": len(drawn), "missing": missing}
