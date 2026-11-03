#!/usr/bin/env python3
"""inject.py — a guard never seen failing is not a guard.

The README of the vault says it: every new guarantee is proved by PUTTING THE
DEFECT BACK, and the red must NAME the culprit, not just be red somewhere.
This does that mechanically: for each entry below it edits render.py, runs the
suite, records which checks went red, and puts the file back.

    ARCHIVIST_WORK=/tmp/bench sh scripts/test.sh      # build the bench once
    scripts/inject.py                                  # then this

⚠ IT EDITS render.py IN PLACE. So it refuses to start unless that file is
clean in git, and it restores it from the copy it took — in a `finally`, and
checked again before it exits. If it ever reports that the restore failed,
`git checkout -- render.py` is the cure.

The list is the injections of 3.2.0, 3.3.0 and 3.4.0, kept as worked examples of the
shape rather than as a suite of their own: each is (what the defect is, the
exact text to replace, what to replace it with). Editing the list is the
normal way to use this — write the injection for the guard you just added, run
it, and read whether the red names your defect. If an entry stops matching
because the code moved, it says NOT PROVED instead of quietly passing.
"""
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "render.py")
# The bench, by test.sh's own convention: ARCHIVIST_WORK to reuse one, else a
# fresh mktemp. NEVER a path inside the repo — a default run would leave a venv
# among the sources, and .gitignore does not cover it.
WORK = os.environ.get("ARCHIVIST_WORK") or tempfile.mkdtemp(prefix="archivist-inject-")

CARD_DRAW = '''        inner = width - 2 * pad
        for b in self.children:
            b.draw(p, x + pad, y, inner); y -= b.measure(p, inner)'''
CARD_DRAW_OLD = '''        inner = width - 2 * self.PAD
        for b in self.children:
            b.draw(p, x + self.PAD, y, inner); y -= b.measure(p, inner)'''

INJECTIONS = [
    # 3.2.0 — the gauge
    ("the gauge measure stands still again (a constant 46, the original defect)",
     ("        _, _, thickness, _, head, tail = self._shape()\n        return self.TOP_GAP + head + thickness + tail",
      "        self._shape()\n        return 46")),
    ("the gauge label is always upper-cased (label_upper ignored)",
     ('            text = label.upper() if spec.get("label_upper", True) else label',
      "            text = label.upper()")),
    ("the gauge bar ignores thickness",
     ('            p.rect(x + width * start, bar_y, width * (to - start), thickness, fill=band.get("color", BG))',
      '            p.rect(x + width * start, bar_y, width * (to - start), self.THICKNESS, fill=band.get("color", BG))')),
    ("the tick tail is measured on the DEFAULT size, not the biggest tick",
     ('        tail = (self.TICK_GAP + max(float(t.get("size", self.TICK_SIZE)) for t in ticks) * (ASCENT + self.DESCENT)\n                if ticks else 0.0)',
      "        tail = (self.TICK_GAP + self.TICK_SIZE * (ASCENT + self.DESCENT) if ticks else 0.0)")),
    ("a face named by the document is not validated (back to KeyError: 'bold')",
     ('        f = _str(v) or "R"\n        if f not in FACES:\n            raise RenderError(f"{self.what}: unknown face {f!r} — one of {\', \'.join(FACES)}")\n        return f',
      '        return _str(v) or "R"')),
    # 3.3.0 — the card
    ("card pad_bottom is PARSED AND IGNORED (measure spends `padding` instead)",
     ("        return head + sum(b.measure(p, inner) for b in self.children) + bottom",
      "        return head + sum(b.measure(p, inner) for b in self.children) + pad")),
    ("card draw ignores `padding` and keeps using the old constant",
     (CARD_DRAW, CARD_DRAW_OLD)),
    ("the card title is drawn at the default size, whatever title_size says",
     ("                   self._title_size(p, width, pad, size), \"navy\")",
      "                   self.TITLE_SIZE, \"navy\")")),
    ("the card title band is a constant again, not derived from the size",
     ('        head = top + (size * (ASCENT + self.TITLE_BELOW) if spec.get("title") else 0.0)',
      '        head = top + (14.0 if spec.get("title") else 0.0)')),
    # 3.4.0 — the ink stays in its box
    ("the card title is placed by its baseline again (its capitals eat pad_top)",
     ("            p.text(x + pad, top - pad_top - ASCENT * size, _str(self.spec[\"title\"]), \"B\",",
      "            p.text(x + pad, top - pad_top - size * 6 / 13, _str(self.spec[\"title\"]), \"B\",")),
    ("a card title too wide is drawn at full size anyway (no fit)",
     ("        if w <= room:\n            return size\n",
      "        if True:\n            return size\n")),
    ("a stretched card centres its content again",
     ("        # and the spare room goes to the foot (until 3.4.0 it was centred).\n",
      "        # and the spare room goes to the foot (until 3.4.0 it was centred).\n        y -= (h - natural) / 2\n")),
    ("the ticks hang at a fixed baseline again (a big tick rises into the bar)",
     ("            p.text(tx, bar_y - self.TICK_GAP - ASCENT * size, text, face, size,",
      "            p.text(tx, bar_y - 9, text, face, size,")),
    ("a tick at an end hangs past the bar again (no slide inward)",
     ("            tx = min(max(x + width * at, x + lead), x + width - (w - lead))",
      "            tx = x + width * at")),
    ("a grid label is not cut to its box",
     ('p.clip(_str(it.get("label")).upper(), "R", 6.6, room)', '_str(it.get("label")).upper()')),
    ("a grid value wider than its box is drawn anyway",
     ("                if p.width(value, \"B\", 11.5) > room:", "                if False:")),
    ("a gauge label wider than the gauge is drawn anyway",
     ("            if p.width(text, face, label_size) > width:", "            if False:")),
    ("a heading is not measured against its width",
     ('        _fits(p, self.what, "heading", text, "B", self.SIZE, width)\n', "")),
    ("a table title is not measured against its width",
     ('            _fits(p, self.what, "table title", _str(self.spec["title"]), "B", 13, width)\n', "")),
    ("a section title is not measured against the first column",
     ('        _fits(p, self.what, "section title", title, "BI", 10.5, room)\n', "")),
    ("a section subtitle is not cut to the first column",
     ('p.clip("— " + _str(s["subtitle"]), "R", 8, x + room - sx)', '"— " + _str(s["subtitle"])')),
    ("a checklist title is not measured against its width",
     ('            _fits(p, self.what, "checklist title", _str(self.spec["title"]), "B", 12, width)\n', "")),
    ("a donut legend label runs under its percentage again",
     ('p.clip(_str(s.get("label")), "R", 8, room)', '_str(s.get("label"))')),
    ("a note is one line again, whatever its length",
     ('        return p.wrap(_req(self.spec, "text", self.what), face, size, width), face, size',
      '        return [_str(_req(self.spec, "text", self.what))], face, size')),
    ("ASCENT drifts from the font files",
     ("ASCENT = 0.72802734375", "ASCENT = 0.70")),
]


def suite():
    r = subprocess.run(["sh", "scripts/test.sh"], cwd=REPO, capture_output=True, text=True,
                       env={**os.environ, "ARCHIVIST_WORK": WORK})
    log_path = os.path.join(WORK, "suite.log")
    try:
        with open(log_path, encoding="utf-8") as fh:
            log = fh.read()
    except OSError:
        return r.returncode, [f"(no suite.log at {log_path}: {r.stderr.strip()[:200]})"]
    return r.returncode, [ln.strip() for ln in log.splitlines() if ln.strip().startswith("FAIL")]


def main():
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "render.py"],
                           cwd=REPO, capture_output=True, text=True).stdout.strip()
    if dirty:
        raise SystemExit("render.py has uncommitted changes: commit or stash them first — "
                         "this script edits it in place and would restore over your work.")

    print(f"bench: {WORK}   (ARCHIVIST_WORK={WORK} to reuse it)\n")
    keep = os.path.join(WORK, "render.keep")
    os.makedirs(WORK, exist_ok=True)
    shutil.copy(SRC, keep)
    out = []
    try:
        for label, (old, new) in INJECTIONS:
            with open(SRC, encoding="utf-8") as fh:
                s = fh.read()
            if s.count(old) != 1:
                out.append(f"\n### {label}\n  !! injection point not unique ({s.count(old)}): NOT PROVED")
                continue
            with open(SRC, "w", encoding="utf-8") as fh:
                fh.write(s.replace(old, new))
            code, fails = suite()
            out.append(f"\n### {label}\n  exit={code}, {len(fails)} red")
            out += [f"    {f}" for f in fails[:4]]
            shutil.copy(keep, SRC)
    finally:
        shutil.copy(keep, SRC)

    still_dirty = subprocess.run(["git", "status", "--porcelain", "--", "render.py"],
                                 cwd=REPO, capture_output=True, text=True).stdout.strip()
    print("\n".join(out))
    if still_dirty:
        print("\n⚠ render.py was NOT restored cleanly: run `git checkout -- render.py`")
        raise SystemExit(1)
    print("\nrender.py restored clean.")


main()
