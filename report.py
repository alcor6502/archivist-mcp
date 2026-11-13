"""report.py — a PDF from Markdown that is ALREADY in the vault.

Why this exists
---------------
`render_pdf` draws blocks the model composes: the caller invents the page. This
one does the opposite. The documents already exist — thirty schede written over
months — and what is missing is a way to bind them into one printable object
without the bytes, or the text, crossing the model twice. The model sends about
a kilobyte: which files, in which order, and how the cover reads. The server
opens them, lays them out and writes the PDF next to them.

The two are twins, and share the guarantees that matter: the bytes are born on
this side of the wire, the write goes through the same CAS, `forbid` and
`text_check` look at what is actually on the page, and a refusal leaves nothing
behind. They differ in one place only, and it is deliberate: this one has NO
base64 door. A report is thirty schede; handing it back as base64 would be
exactly the cost the pair exists to avoid, so an empty path is a refusal, not a
"live" mode.

What it knows, and what it does not
-----------------------------------
Markdown — CommonMark shapes plus GFM tables and fenced code — and the
pagination rules of §4, which are fixed and not parameters: one section per
file always on a fresh page, a breadcrumb in the head, tables that shrink by
column count and never leave the margin, bold in dark red because in the schede
bold means "outside the range", monospace for the ASCII pedigrees. It knows
nothing about what any of it MEANS: a wrong value in the .md is a wrong value
in the PDF. The truth stays in the schede.

Two things it refuses to do, and both are about not reading what it was not
asked to read: the file selection never recurses (a pattern on the root never
reaches `Parametri Vitali/` or `Trash/`), and every external resource — an
`<img src="…">` typed into a scheda, a stylesheet, a font by URL — is refused
by the fetcher. A document that asks for one is a refusal naming the URL: a PDF
renderer that quietly fetches whatever a document names is a reader of files
nobody asked it to read.
"""
from __future__ import annotations

import fnmatch
import html as _html
import os
import re
from typing import Any

import rxlimit

MAX_SOURCE_BYTES = 5_000_000    # the Markdown that goes in, summed
MAX_PAGES = 200                 # the PDF that comes out; high, because no base64
# One section per file, always on a fresh page: more files than pages cannot
# print. Derived, so there is no second ceiling to keep in step with the first.
MAX_FILES = MAX_PAGES

# The faces the PDF embeds. DejaVu, not Liberation, and for one reason: the
# schede are full of µ ×  ⁹ → ▸, and Liberation has holes there. A face chosen
# by fontconfig instead of by name would embed something else on a differently
# built machine, and the page would silently change: missing files are a fault.
FONT_DIR = os.environ.get("REPORT_FONT_DIR", "/usr/share/fonts/truetype/dejavu")
FONT_FILES = ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSansMono.ttf")
PAGE_SIZES = {"a4": "A4", "letter": "letter"}

# §3: everything optional, and this is what "not given" means. The types are
# the validation: a key is checked against the type of its default.
LAYOUT_DEFAULTS: dict[str, Any] = {
    "title": "Report", "subtitle": "", "cover_note": "", "footer": "",
    "page": "a4", "toc": True, "font_pt": 9.5, "forbid": [], "text_check": [],
}
SOURCES_KEYS = {"groups", "base"}
GROUP_KEYS = {"title", "files", "pattern", "exclude", "landscape"}

# §4.4, the three table sizes and the two thresholds. Absolute points, not a
# scale of `font_pt`: the refusal below names 6.2 pt, so 6.2 pt has to be a
# number this file states once.
TABLE_PT, TABLE_PT_12, TABLE_PT_16 = 8.0, 7.0, 6.2
WIDE_12, WIDE_16 = 11, 15

# Tags that would take the layout out of §4's hands. Markdown passes raw HTML
# through, so a single <style> block in one scheda restyles the whole report —
# quietly, and only in the copy that scheda is in. Looked for in the CONVERTED
# HTML, never in the source: inside a fenced code block the same characters are
# already escaped, and a scheda documenting a stylesheet is not styling one.
LAYOUT_TAGS = re.compile(r"<(style|script|link|base|iframe|object|embed)\b", re.I)


class ReportError(Exception):
    """The call is wrong: a file that is not there, the same file in two
    groups, a table that does not fit. The caller's to fix."""


class ReportFault(Exception):
    """The machinery is wrong: fonts missing from the image, weasyprint
    absent, a rendered page that will not say which sections are on it. Not
    the caller's to fix."""


# ---------------------------------------------------------------------------
# §2 — which files, in which order. Pure: the listing comes from the vault.
# ---------------------------------------------------------------------------

def select(sources: Any, names: list[str]) -> list[dict]:
    """Turn `sources` into groups of file names, in the order they will print.

    `names` is what lives directly in `base` — no subdirectories, which is what
    makes "the glob is not recursive" a property of the input rather than a
    rule to remember while writing patterns.

    Every refusal here names the file: a report that quietly leaves out a
    scheda is worse than one that does not print.
    """
    if not isinstance(sources, dict):
        raise ReportError("sources must be an object with a `groups` list")
    unknown = sorted(set(sources) - SOURCES_KEYS)
    if unknown:
        raise ReportError(f"sources: unknown key(s) {unknown}; known: {sorted(SOURCES_KEYS)}")
    groups = sources.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ReportError("sources.groups must be a non-empty list of groups")
    listing = sorted(names)
    out: list[dict] = []
    seen: dict[str, str] = {}     # file -> the group that already took it
    for i, g in enumerate(groups):
        where = f"group {i + 1}"
        if not isinstance(g, dict):
            raise ReportError(f"{where} is not an object")
        unknown = sorted(set(g) - GROUP_KEYS)
        if unknown:
            raise ReportError(f"{where}: unknown key(s) {unknown}; known: {sorted(GROUP_KEYS)}")
        title = g.get("title", "")
        if not isinstance(title, str) or not title.strip():
            raise ReportError(f"{where}: `title` is required and must be a non-empty string")
        where = f"group {title!r}"
        land = g.get("landscape", False)
        if not isinstance(land, bool):
            raise ReportError(f"{where}: `landscape` must be true or false")
        excl = g.get("exclude", [])
        if not isinstance(excl, list) or not all(isinstance(x, str) for x in excl):
            raise ReportError(f"{where}: `exclude` must be a list of globs")
        has_files, has_pattern = "files" in g, "pattern" in g
        if has_files == has_pattern:
            raise ReportError(f"{where}: give either `files` or `pattern`, not both and not neither")
        if has_files:
            files = g["files"]
            if not isinstance(files, list) or not all(isinstance(x, str) for x in files):
                raise ReportError(f"{where}: `files` must be a list of file names")
            chosen = []
            for f in files:
                if not f.lower().endswith(".md"):
                    raise ReportError(f"{where}: {f!r} is not a .md file; this prints Markdown only")
                if f not in listing:
                    raise ReportError(f"{where}: no such file: {f!r}")
                chosen.append(f)
        else:
            pat = g["pattern"]
            if not isinstance(pat, str) or not pat:
                raise ReportError(f"{where}: `pattern` must be a non-empty glob")
            # fnmatchcase, not fnmatch: fnmatch normalises case on some
            # platforms, and a pattern that behaves differently depending on
            # where the container runs is not a pattern.
            chosen = [n for n in listing
                      if n.lower().endswith(".md") and fnmatch.fnmatchcase(n, pat)]
        chosen = [n for n in chosen if not any(fnmatch.fnmatchcase(n, x) for x in excl)]
        for n in chosen:
            if n in seen:
                raise ReportError(
                    f"{n!r} is in two groups ({seen[n]!r} and {title!r}): "
                    "a file prints once — narrow one of the two with `exclude`")
            seen[n] = title
        out.append({"title": title, "landscape": land, "files": chosen})
    total = sum(len(g["files"]) for g in out)
    if total == 0:
        raise ReportError("no file selected: every group is empty")
    if total > MAX_FILES:
        raise ReportError(f"{total} files selected, max {MAX_FILES}: narrow the patterns")
    return out


# ---------------------------------------------------------------------------
# §4 — Markdown to the HTML the layout is written against
# ---------------------------------------------------------------------------

def _fonts() -> None:
    """The faces are named in the CSS and resolved by fontconfig; missing
    files mean fontconfig would answer with something else and the page would
    silently change family. Same stance as render.py, same kind of fault."""
    for fn in FONT_FILES:
        if not os.path.isfile(os.path.join(FONT_DIR, fn)):
            raise ReportFault(f"font missing in the image: {os.path.join(FONT_DIR, fn)}")


def _section_html(name: str, text: str, columns: dict[str, int], sid: str) -> str:
    """One scheda's Markdown, as the body of one section.

    `columns` collects the widest table found, by section id: the overflow
    refusal in §4.4 has to name a column count, and this is where the count is
    known — the rendered box tree knows the width but not what produced it.
    """
    try:
        import markdown
    except ImportError as e:  # pragma: no cover — the suite installs it
        raise ReportFault(f"markdown is not installed: {e}")
    # HTML comments are service notes — "<!-- fonte: referto del 12 -->" — and
    # they are stripped BEFORE the conversion, where they are still comments;
    # afterwards they are raw HTML that markdown has already passed through.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out = markdown.markdown(text, extensions=["tables", "fenced_code"])
    # An internal link points at an anchor of the source vault, not of this
    # PDF: it would print as a live link to nothing. It becomes its own text.
    # An external one — http, mailto — is left alone and stays clickable.
    out = re.sub(r'<a href="#[^"]*">(.*?)</a>', r"\1", out, flags=re.S)

    def wide(m: re.Match) -> str:
        # `<th[ >]`, not a count of "<th": "<thead>" starts with it, and
        # counting it made every table one column wider than it is — which
        # moved both thresholds by one without anything saying so.
        n = len(re.findall(r"<th[ >]", m.group(0)))
        columns[sid] = max(columns.get(sid, 0), n)
        cls = "w16" if n > WIDE_16 else ("w12" if n > WIDE_12 else "")
        return m.group(0).replace("<table>", f'<table class="{cls}">', 1)

    out = re.sub(r"<table>.*?</thead>", wide, out, flags=re.S)
    tag = LAYOUT_TAGS.search(out)
    if tag:
        raise ReportError(
            f"{name!r} carries a <{tag.group(1).lower()}> tag: the layout of a report is "
            "fixed, and a scheda does not get to change it. Put it in a fenced code block "
            "if it is meant to be read as text")
    return out


def _css(layout: dict) -> str:
    size = PAGE_SIZES[layout["page"]]
    # The footer is a caller's string going into a CSS string literal: the two
    # characters that can end it early are escaped, and nothing else is.
    foot = layout["footer"].replace("\\", "\\\\").replace('"', '\\"')
    tail = ' " · pag. " counter(page) " di " counter(pages)'
    content = (f'"{foot}"' + tail) if foot else ('"pag. " counter(page) " di " counter(pages)')
    return f"""
@page {{ size: {size}; margin: 16mm 14mm;
  @bottom-center {{ content: {content}; font: 8pt "DejaVu Sans", sans-serif; color: #666; }} }}
@page land {{ size: {size} landscape; }}
section.land {{ page: land; }}
section {{ break-before: page; }}
body {{ font-family: "DejaVu Sans", sans-serif; font-size: {layout['font_pt']}pt;
  color: #222; line-height: 1.32; }}
.cover {{ text-align: left; padding-top: 60mm; }}
.cover h1 {{ font-size: 30pt; margin: 0; }} .cover p {{ color: #555; }}
.crumb {{ font-size: 8pt; color: #888; border-bottom: 1px solid #ddd; margin-bottom: 4mm; }}
h1 {{ font-size: 16pt; margin: 0 0 3mm; }}
h2 {{ font-size: 12pt; margin: 5mm 0 2mm; border-bottom: 1px solid #ccc; }}
h3 {{ font-size: 10.5pt; margin: 4mm 0 1.5mm; }}
/* §4.7: a heading is never the last line of a page, and no line of a
   paragraph is left behind on its own. */
h1, h2, h3, h4 {{ break-after: avoid; }}
p, li, blockquote {{ orphans: 2; widows: 2; }}
table {{ border-collapse: collapse; width: 100%; font-size: {TABLE_PT}pt;
  margin: 2mm 0 3mm; break-inside: auto; }}
th, td {{ border: 1px solid #ccc; padding: 1.2mm 1.6mm; vertical-align: top; }}
th {{ background: #eef1f5; text-align: left; }}
tr {{ break-inside: avoid; }}
td:first-child {{ white-space: nowrap; }}
table.w12 {{ font-size: {TABLE_PT_12}pt; }} table.w16 {{ font-size: {TABLE_PT_16}pt; }}
table.w16 td:first-child, table.w12 td:first-child {{ white-space: normal; }}
/* §4.5: in the schede bold means "outside the range", so bold is red. */
strong {{ color: #b23; }}
pre {{ font-family: "DejaVu Sans Mono", monospace; font-size: 7.5pt; white-space: pre; }}
code {{ font-family: "DejaVu Sans Mono", monospace; }}
blockquote {{ border-left: 3px solid #ccc; margin: 2mm 0; padding-left: 3mm; color: #555; }}
.toc ol {{ list-style: none; padding: 0; margin: 0 0 3mm; }} .toc h3 {{ margin-top: 5mm; }}
.toc li {{ margin: 0.6mm 0; font-size: {layout['font_pt']}pt; }}
.toc li a {{ display: block; width: 100%; text-decoration: none; color: #224; }}
.toc li a::after {{ content: leader(". ") target-counter(attr(href), page); color: #666; }}
"""


def _document(groups: list[dict], layout: dict, columns: dict[str, int]) -> tuple[str, list[dict]]:
    """The whole HTML, and the flat list of sections in printing order.

    Section ids are `s0, s1, …` and not a slug of the title on purpose: the id
    is what the table of contents links to and what the rendered page hands
    back as an anchor, so two schede whose names differ only in punctuation
    must not be able to collide into one.
    """
    esc = _html.escape
    sections, body, toc = [], [], []
    for g in groups:
        toc.append(f"<h3>{esc(g['title'])}</h3><ol>")
        for name in g["files"]:
            sid = f"s{len(sections)}"
            title = name[:-3] if name.lower().endswith(".md") else name
            sections.append({"id": sid, "file": name, "title": title})
            cls = "land" if g["landscape"] else "port"
            body.append(f'<section class="{cls}" id="{sid}">'
                        f'<div class="crumb">{esc(title)}</div>'
                        f'{_section_html(name, g["text"][name], columns, sid)}</section>')
            toc.append(f'<li><a href="#{sid}">{esc(title)}</a></li>')
        toc.append("</ol>")
    cover = [f'<div class="cover"><h1>{esc(layout["title"])}</h1>']
    if layout["subtitle"]:
        cover.append(f'<p>{esc(layout["subtitle"])}</p>')
    if layout["cover_note"]:
        cover.append(f'<p>{esc(layout["cover_note"])}</p>')
    cover.append("</div>")
    index = (f'<section class="port toc"><h1>Indice</h1>{"".join(toc)}</section>'
             if layout["toc"] else "")
    doc = (f'<html><head><meta charset="utf-8"><style>{_css(layout)}</style></head><body>'
           f'{"".join(cover)}{index}{"".join(body)}</body></html>')
    return doc, sections


# ---------------------------------------------------------------------------
# reading the rendered pages: what is on them, and what sticks out of them
# ---------------------------------------------------------------------------

def _page_box(page):
    """The layout tree of one page. It is weasyprint's private attribute, and
    a version that renames it must fail HERE, loudly: both the overflow guard
    and `forbid` read the page through this, and a guard that silently finds
    nothing to check is a guard that has stopped working."""
    box = getattr(page, "_page_box", None)
    if box is None:
        raise ReportFault("weasyprint does not expose the page box: the overflow "
                          "and forbid guards cannot read the page")
    return box


def _boxes(box):
    yield box
    for child in getattr(box, "children", ()):
        yield from _boxes(child)


def _page_limit(page) -> float:
    """The right edge of the printable area: the root block's own right edge,
    which is the page box minus the margins the stylesheet asked for."""
    for b in _boxes(_page_box(page)):
        if getattr(b, "element_tag", None) == "html":
            return b.position_x + b.margin_width()
    raise ReportFault("a rendered page has no root box")


def _page_text(page) -> tuple[str, list[str]]:
    """Every string on the page, in order: the fragments as they were laid
    out, and them joined. `forbid` is checked against both — a phrase broken
    across two inline boxes lives only in the join, and a string that gained a
    space from the join lives only in the fragments."""
    frags = [b.text for b in _boxes(_page_box(page)) if type(b).__name__ == "TextBox"]
    return " ".join(frags), frags


def _wide_tables(page) -> list[float]:
    """How far each table on this page sticks out past the margin, for the
    ones that do. Zero-length means the page is clean."""
    limit = _page_limit(page)
    out = []
    for b in _boxes(_page_box(page)):
        if type(b).__name__ == "TableBox":
            over = (b.position_x + b.margin_width()) - limit
            if over > 0.5:      # half a point: rounding is not an overflow
                out.append(over)
    return out


def _anchors(pages) -> dict[str, int]:
    """Section id -> page number, from what the renderer says it anchored
    where. This is the only honest source for §5's `sections`: a count of
    flowed pages would be a second implementation of pagination."""
    found = {}
    for n, page in enumerate(pages, start=1):
        for name in getattr(page, "anchors", {}) or {}:
            found.setdefault(name, n)
    return found


# ---------------------------------------------------------------------------
# the whole job
# ---------------------------------------------------------------------------

def check_layout(layout: Any) -> dict:
    """`layout` merged onto the defaults, with every value type-checked. The
    types come from the defaults themselves: there is no second table of what
    a key may hold."""
    if layout is None:
        layout = {}
    if not isinstance(layout, dict):
        raise ReportError("layout must be an object")
    unknown = sorted(set(layout) - set(LAYOUT_DEFAULTS))
    if unknown:
        raise ReportError(f"layout: unknown key(s) {unknown}; known: {sorted(LAYOUT_DEFAULTS)}")
    out = dict(LAYOUT_DEFAULTS)
    for k, default in LAYOUT_DEFAULTS.items():
        if k not in layout:
            continue
        v = layout[k]
        if isinstance(default, bool):
            if not isinstance(v, bool):
                raise ReportError(f"layout.{k} must be true or false")
        elif isinstance(default, str):
            if not isinstance(v, str):
                raise ReportError(f"layout.{k} must be a string")
        elif isinstance(default, float):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ReportError(f"layout.{k} must be a number")
            v = float(v)
        elif isinstance(default, list):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ReportError(f"layout.{k} must be a list of strings")
        out[k] = v
    if out["page"] not in PAGE_SIZES:
        raise ReportError(f"layout.page must be one of {sorted(PAGE_SIZES)}")
    if not 5.0 <= out["font_pt"] <= 16.0:
        raise ReportError("layout.font_pt must be between 5 and 16")
    if not out["title"].strip():
        raise ReportError("layout.title cannot be empty: it is the cover")
    # The footer becomes a CSS string literal, and a CSS string literal cannot
    # hold a newline: one there would not print on two lines, it would break the
    # rule that draws the footer and take the page numbers with it, silently.
    if "\n" in out["footer"] or "\r" in out["footer"]:
        raise ReportError("layout.footer is one line: it prints on every page")
    for pat in out["forbid"]:
        try:
            rxlimit.compile(pat)
        except rxlimit.error as e:
            raise ReportError(f"forbid: {pat!r} is not a valid regex: {e}")
    return out


def build(groups: list[dict], layout: dict) -> tuple[bytes, dict]:
    """Groups of Markdown in, one PDF out.

    Each group is `{"title", "landscape", "files": [name], "text": {name: md}}`
    — `select` produced the first three, the vault filled in the fourth. The
    order of the pages IS the order of the list.
    """
    # OSError too: WeasyPrint dlopens Pango through cffi when it is imported,
    # and an image without libpango raises `OSError: cannot load library
    # 'libpango-1.0-0'`, not ImportError. Seen in the smoke test with the
    # package taken out: a bare traceback where a named fault belongs.
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as e:
        raise ReportFault(f"weasyprint cannot be loaded: {e}")
    _fonts()
    total = sum(len(t.encode("utf-8")) for g in groups for t in g["text"].values())
    if total > MAX_SOURCE_BYTES:
        raise ReportError(f"{total} bytes of Markdown, max {MAX_SOURCE_BYTES}: fewer files")
    columns: dict[str, int] = {}
    doc_html, sections = _document(groups, layout, columns)

    # Nothing is fetched. Not an image, not a stylesheet, not a font by URL:
    # the only input is the Markdown the caller named, and a document that
    # names a second one is refused rather than quietly served.
    asked: list[str] = []

    def fetcher(url: str, *a, **kw):
        asked.append(url)
        raise ValueError(f"external resources are not fetched: {url}")

    # weasyprint 70 asks its fetcher whether a failure is fatal before it
    # decides what to do with the exception. False, deliberately: the refusal
    # below is ours, raised on `asked` after the render, so it says the same
    # thing on every weasyprint that ever reads this attribute or ignores it.
    fetcher._fail_on_errors = False

    rendered = HTML(string=doc_html, url_fetcher=fetcher).render()
    if asked:
        raise ReportError(
            f"a source asks for an external resource, which this never fetches: {asked[0]}")
    pages = rendered.pages
    if len(pages) > MAX_PAGES:
        raise ReportError(f"the report came out at {len(pages)} pages, over {MAX_PAGES}: "
                          "print fewer groups")

    where = _anchors(pages)
    missing_anchor = [s["file"] for s in sections if s["id"] not in where]
    if missing_anchor:
        raise ReportFault(f"the renderer lost {len(missing_anchor)} section(s): {missing_anchor[0]!r}")
    # Which file each page belongs to: a section always starts a page, so a
    # page carries at most one section start and otherwise continues the last.
    starts = {where[s["id"]]: s for s in sections}
    owner, running = {}, None
    for n in range(1, len(pages) + 1):
        running = starts.get(n, running)
        owner[n] = running

    # §4.4 — a column cut off at the margin is worse than a refusal, and the
    # refusal has to say which scheda and how wide, because the fix is in the
    # scheda.
    for n, page in enumerate(pages, start=1):
        over = _wide_tables(page)
        if over:
            # `owner` is None only on the cover and the index, which carry no
            # table — but a message that crashes instead of naming the page is
            # not a message, so it says where it is either way.
            s = owner[n]
            what = f"a table in {s['file']!r}" if s else f"a table on page {n}"
            cols = columns.get(s["id"], 0) if s else 0
            raise ReportError(
                f"{what} does not fit the page even at {TABLE_PT_16} pt "
                f"({cols} columns, {max(over):.0f} pt past the margin): "
                "split the table or print that group landscape")

    # The guards look at what is on the PAGE, footer and index included, not at
    # what was handed in: that is the difference between checking the output
    # and checking the input.
    drawn, seen = [], []
    for page in pages:
        joined, frags = _page_text(page)
        drawn.append(joined)
        seen.extend(frags)
    # The patterns are the caller's, so they run on a clock: one budget for the
    # whole list, and a guard that could not finish has not passed.
    budget = rxlimit.Budget()
    for pat in layout["forbid"]:
        rx = rxlimit.compile(pat)
        try:
            hit = any(budget.search(rx, t) for t in drawn) or any(budget.search(rx, f) for f in seen)
        except rxlimit.RegexTimeout:
            raise ReportError(f"forbid: the patterns took more than {budget.seconds:g} s to run "
                              f"(stopped in {pat!r}): simplify it — nothing written") from None
        if hit:
            raise ReportError(f"forbidden pattern {pat!r} appears in the report: nothing written")
    whole = "\n".join(drawn)
    missing = [s for s in layout["text_check"] if s not in whole]

    data = rendered.write_pdf()
    verdict = {"pages": len(pages),
               "files": len(sections),
               "empty_groups": [g["title"] for g in groups if not g["files"]],
               "missing": missing,
               "sections": [{"file": s["file"], "page": where[s["id"]]} for s in sections]}
    return data, verdict
