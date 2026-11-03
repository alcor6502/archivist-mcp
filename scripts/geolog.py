#!/usr/bin/env python3
"""geolog.py — what the renderer actually DREW, as text, so a change can be
proved to have moved nothing.

Why this exists
---------------
Two PDFs of the same document are never byte-identical: reportlab stamps a
creation date inside, so their sha256 differ. Comparing the files tells you
nothing. What can be compared is the DRAWING: every string placed, every
rectangle, every line, with its coordinates, plus the height every block
declared through measure(). This writes all of it as one canonical line each.

    scripts/geolog.py before.log scripts/fixtures/*.json     # before a change
    …edit render.py…
    scripts/geolog.py after.log  scripts/fixtures/*.json     # after it
    diff before.log after.log

An empty diff is the proof that a document naming none of the new fields comes
out exactly as it did. A non-empty one has to be read: in 3.2.0 every single
difference was `2.55` (a box that grew), `-2.55` (what sits under it) or
`-1.28` (half of that, where a stretched card centres its content), and
nothing else — which is what made the release shippable.

It needs reportlab and the fonts, so run it with the bench interpreter that
scripts/test.sh builds:

    ARCHIVIST_WORK=/tmp/bench sh scripts/test.sh          # builds the venv
    /tmp/bench/venv/bin/python scripts/geolog.py out.log scripts/fixtures/quadro.json

Coordinates are rounded to a hundredth of a point: fine enough to catch a real
move, coarse enough that float noise is not read as one.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import render        # noqa: E402 — the path has to be set first


def n(v):
    return f"{float(v):.2f}" if isinstance(v, (int, float)) else repr(v)


def arm(log):
    """Wrap the three drawing primitives and every measure(), so nothing a
    block does can escape the record."""
    P = render._Page
    _text, _rect, _line = P.text, P.rect, P.line

    def text(self, x, y, s, face="R", size=9, tone="black", align="left"):
        log.append(f"text  x={n(x)} y={n(y)} face={face} size={n(size)} tone={tone} align={align} s={str(s)!r}")
        return _text(self, x, y, s, face, size, tone, align)

    def rect(self, x, y, w, h, fill=None, stroke=None, radius=0, lw=0.7):
        log.append(f"rect  x={n(x)} y={n(y)} w={n(w)} h={n(h)} fill={fill} stroke={stroke} r={n(radius)} lw={n(lw)}")
        return _rect(self, x, y, w, h, fill, stroke, radius, lw)

    def line(self, x1, y1, x2, y2, tone=render.LINE, lw=0.5, dash=None):
        log.append(f"line  {n(x1)},{n(y1)} -> {n(x2)},{n(y2)} tone={tone} lw={n(lw)} dash={dash}")
        return _line(self, x1, y1, x2, y2, tone, lw, dash)

    P.text, P.rect, P.line = text, rect, line

    for name, cls in render._TYPES.items():
        if "measure" not in cls.__dict__:
            continue
        orig = cls.__dict__["measure"]

        def wrap(orig=orig, name=name):
            def measure(self, p, width):
                h = orig(self, p, width)
                log.append(f"meas  {name:9} {self.what:24} width={n(width)} -> h={n(h)}")
                return h
            return measure
        cls.measure = wrap()


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: geolog.py <out.log> <document.json>...", file=sys.stderr)
        raise SystemExit(2)
    out, docs = sys.argv[1], sys.argv[2:]
    log = []
    arm(log)
    lines = []
    for path in docs:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        log.clear()
        _data, verdict = render.build(doc)
        lines.append(f"===== {os.path.basename(path)} =====")
        lines.append(f"verdict pages={verdict['pages']} strings={verdict['strings_drawn']} missing={verdict['missing']}")
        lines += log
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"{out}: {len(lines)} lines over {len(docs)} document(s)")


main()
