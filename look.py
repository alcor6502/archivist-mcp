"""
look.py — what a file LOOKS like, and what a PDF says.

Everything else in this vault hands a caller bytes or text. A photo, a scan
and a PDF the server has just drawn are none of those to a model: as base64
they are megabytes it cannot read. This module turns them into what it can —
a reduced JPEG of a picture, of the pages of a PDF, of a folder as a contact
sheet — and reads the text layer of a PDF.

Three rules, all of them about not handing over more than was asked:
- NEVER the full resolution. A picture leaves at `VIEW_SIZES` on its long
  side, re-encoded; the original is not touched and the reduction is not kept.
- A PDF is shown `MAX_VIEW_PAGES` pages per call at most, and says which ones
  it left out.
- NO OCR. A page without a text layer has no text here; `pdf_text` says so
  and the page is looked at instead.

It knows nothing about datasets, paths or git: bytes in, bytes out. vault.py
decides which file, and whether the caller may have it.

PDFium is not thread-safe and FastMCP runs each call on a worker thread, so
every use of it sits behind ONE lock: two pages rendered at once is a crash of
the whole service, not a slow call.
"""
from __future__ import annotations

import io
import threading

# Pages of a PDF shown in one call. A page is an image of a few hundred KB in
# the caller's context: ten is already a lot to look at.
MAX_VIEW_PAGES = 10
# Long side, in pixels, of what leaves. `medium` reads a page of body text;
# `thumb` tells one picture from another.
VIEW_SIZES = {"medium": 1500, "thumb": 400}
# A picture larger than this is not decoded at all: the decoded bitmap is
# three bytes a pixel, in memory, whatever the file weighs on disk.
MAX_PIXELS = 150_000_000
JPEG_QUALITY = 80
# The contact sheet of a folder.
SHEET_COLUMNS = 5
SHEET_CELL = 300
SHEET_LABEL = 24
MAX_SHEET_ITEMS = 30
# Pages of a PDF whose TEXT is handed over in one call.
MAX_TEXT_PAGES = 50
# What `view` treats as a picture, by name, when it lists a folder. A single
# file is judged by its content, never by its name.
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
                  ".tif", ".tiff", ".bmp")

_PDFIUM_LOCK = threading.Lock()
_HEIF_READY = False


class LookError(Exception):
    """The caller's file or request cannot be shown: not a picture, a PDF
    that will not open, a page that is not there."""


class LookFault(Exception):
    """The machinery is missing or broke: a library the image should carry."""


def _pil():
    """Pillow, with the HEIC opener registered once. A missing library is a
    fault with a name: the image is built without it, not the caller wrong."""
    global _HEIF_READY
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError as e:
        raise LookFault(f"Pillow cannot be loaded: {e}") from None
    if not _HEIF_READY:
        try:
            import pillow_heif
        except ImportError as e:
            raise LookFault(f"pillow-heif cannot be loaded: {e}") from None
        pillow_heif.register_heif_opener()
        _HEIF_READY = True
    return Image, ImageDraw, ImageFont, ImageOps


def _pdfium():
    try:
        import pypdfium2
    except ImportError as e:
        raise LookFault(f"pypdfium2 cannot be loaded: {e}") from None
    return pypdfium2


def is_pdf(data: bytes) -> bool:
    """By content: a PDF starts with its marker, whatever the file is called.
    The marker may sit a little way in — scanners prepend junk."""
    return b"%PDF-" in data[:1024]


def parse_pages(spec: str, total: int, ceiling: int) -> tuple[list[int], list[int]]:
    """`spec` as a caller writes it → the pages to give, and the ones asked
    for and left out because of the ceiling. '' is page 1; '3'; '2-5'; 'all';
    and, since 4.2.1, a list of those: '1-3,11' — the cover, the index and
    one page far away are ONE call, which is what checking a report takes.
    Pages are numbered from 1, as a person counts them, and come back in
    page order, each once."""
    spec = (spec or "").strip().lower()
    if total < 1:
        raise LookError("the PDF has no pages")
    if not spec:
        want = [1]
    elif spec == "all":
        want = list(range(1, total + 1))
    else:
        chosen = set()
        for part in spec.split(","):
            first, sep, last = part.partition("-")
            if not first.strip().isdigit() or (sep and not last.strip().isdigit()):
                raise LookError(f"pages={spec!r}: write one page ('3'), a range ('2-5'), "
                                "a list of those ('1-3,11') or 'all'")
            a = int(first)
            b = int(last) if sep else a
            if not 1 <= a <= b <= total:
                raise LookError(f"pages={spec!r}: the PDF has {total} "
                                f"page{'s' if total != 1 else ''}, numbered from 1")
            chosen.update(range(a, b + 1))
        want = sorted(chosen)
    return want[:ceiling], want[ceiling:]


def _jpeg(im) -> bytes:
    """Any Pillow image → JPEG bytes on white. JPEG has no transparency: an
    alpha channel left alone comes out black, which reads as a broken file."""
    Image, _, _, _ = _pil()
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=im.getchannel("A"))
        im = flat
    elif im.mode != "RGB":
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def _open_picture(data: bytes, long_side: int):
    """A picture, upright and no larger than `long_side` on its long side.
    Returns (image, original size). Never enlarged."""
    Image, _, _, ImageOps = _pil()
    try:
        # Opening reads the header only: the size is known, and refused,
        # before a single pixel is decoded. Pillow's own ceiling is left where
        # it is — it is process-wide, and the renderers live in this process.
        im = Image.open(io.BytesIO(data))
        original = im.size
        if original[0] * original[1] > MAX_PIXELS:
            raise LookError(f"the picture is {original[0]}×{original[1]} pixels, over "
                            f"{MAX_PIXELS // 1_000_000} megapixels: too large to open here")
        # A JPEG can be decoded straight at a fraction of its size: a 48 MP
        # photo never becomes 144 MB of bitmap on the way to a 1,500 px view.
        im.draft("RGB", (long_side, long_side))
        # The camera's rotation is a tag, not pixels: without this a portrait
        # photo arrives lying on its side.
        im = ImageOps.exif_transpose(im)
        im.thumbnail((long_side, long_side), Image.Resampling.LANCZOS)
        im.load()
    except LookError:
        raise
    except Image.DecompressionBombError:
        raise LookError(f"the picture is over {MAX_PIXELS // 1_000_000} megapixels: "
                        "too large to open here") from None
    except Exception as e:                                       # noqa: BLE001
        # Pillow raises a different class per format for a file it cannot
        # read; whose fault it is does not depend on which.
        raise LookError("not a picture this server can open (JPEG, PNG, HEIC, WebP, "
                        f"GIF, TIFF, BMP) nor a PDF: {type(e).__name__}") from None
    return im, original


def picture(data: bytes, size: str = "medium") -> tuple[bytes, dict]:
    """One picture, reduced. Returns (JPEG bytes, {pixels, shown})."""
    im, original = _open_picture(data, VIEW_SIZES[size])
    return _jpeg(im), {"pixels": list(original), "shown": list(im.size)}


def _open_pdf(data: bytes):
    """Called with the lock HELD."""
    pdfium = _pdfium()
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise LookError(f"the PDF cannot be opened (damaged, or protected by a "
                        f"password): {e}") from None


def _render_page(pdf, n: int, long_side: int):
    """Page `n` (from 1) as a Pillow image `long_side` on its long side.
    Called with the lock HELD."""
    page = pdf[n - 1]
    try:
        w, h = page.get_size()
        bitmap = page.render(scale=long_side / max(w, h, 1.0))
        try:
            return bitmap.to_pil().convert("RGB")
        finally:
            bitmap.close()
    finally:
        page.close()


def pdf_pages(data: bytes, pages: str = "", size: str = "medium") -> tuple[list[bytes], dict]:
    """The pages of a PDF as JPEGs. Returns (images, {pages, shown, more?})."""
    _pil()
    with _PDFIUM_LOCK:
        pdf = _open_pdf(data)
        try:
            total = len(pdf)
            want, rest = parse_pages(pages, total, MAX_VIEW_PAGES)
            images = [_jpeg(_render_page(pdf, n, VIEW_SIZES[size])) for n in want]
        finally:
            pdf.close()
    info = {"pages": total, "shown": want}
    if rest:
        info["more"] = (f"{MAX_VIEW_PAGES} pages per call: {len(rest)} were left out, "
                        f"from page {rest[0]} to {rest[-1]} — ask again starting at "
                        f"pages='{rest[0]}-{min(rest[-1], rest[0] + MAX_VIEW_PAGES - 1)}'")
    return images, info


def pdf_text(data: bytes, pages: str = "") -> dict:
    """What a PDF SAYS. Without `pages`: how many pages, the outline, and how
    many characters of text each page carries — the map, with no text. With
    `pages`: the text of those. A page that carries none is a scan: there is
    no OCR here, and the answer says to look at it instead."""
    with _PDFIUM_LOCK:
        pdf = _open_pdf(data)
        try:
            total = len(pdf)
            outline = []
            for item in pdf.get_toc(max_depth=4):
                dest = item.get_dest()
                index = dest.get_index() if dest is not None else None
                outline.append({"level": item.level + 1, "title": item.get_title(),
                                "page": None if index is None else index + 1})
                if len(outline) >= 200:
                    break

            def text_of(n: int) -> str:
                page = pdf[n - 1]
                try:
                    tp = page.get_textpage()
                    try:
                        return tp.get_text_bounded().replace("\r\n", "\n").strip()
                    finally:
                        tp.close()
                finally:
                    page.close()

            out: dict = {"pages": total, "outline": outline}
            if not (pages or "").strip():
                # The map: enough to choose which pages to ask for.
                counted = min(total, 500)
                out["chars_per_page"] = [len(text_of(n)) for n in range(1, counted + 1)]
                empty = [n for n, c in enumerate(out["chars_per_page"], 1) if not c]
                if counted < total:
                    out["note"] = f"characters counted on the first {counted} pages only"
            else:
                want, rest = parse_pages(pages, total, MAX_TEXT_PAGES)
                out["text"] = [{"page": n, "text": text_of(n)} for n in want]
                empty = [t["page"] for t in out["text"] if not t["text"]]
                if rest:
                    out["more"] = (f"{MAX_TEXT_PAGES} pages per call: {len(rest)} were "
                                   f"left out, from page {rest[0]} to {rest[-1]}")
        finally:
            pdf.close()
    if empty:
        out["no_text"] = empty
        out["no_text_note"] = ("these pages carry no text layer (a scan or a picture): "
                               "there is no OCR here — look at them with view")
    return out


def sheet(items: list[tuple[str, bytes | str]]) -> tuple[bytes, list[dict]]:
    """A folder at a glance: one cell per file, numbered, the number repeated
    in the list that travels beside the picture. A file that will not open
    keeps its cell, crossed out, and says why in the list — a sheet with a
    missing cell would number every picture after it wrong. An item whose
    data is a string was not read at all: the string is the reason."""
    Image, ImageDraw, ImageFont, _ = _pil()
    try:
        font = ImageFont.load_default(size=16)
    except (TypeError, OSError):                                 # pragma: no cover
        font = ImageFont.load_default()
    rows = max(1, -(-len(items) // SHEET_COLUMNS))
    pitch = SHEET_CELL + SHEET_LABEL
    canvas = Image.new("RGB", (SHEET_COLUMNS * SHEET_CELL, rows * pitch), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    listed = []
    for i, (name, data) in enumerate(items):
        x, y = (i % SHEET_COLUMNS) * SHEET_CELL, (i // SHEET_COLUMNS) * pitch
        entry = {"n": i + 1, "name": name}
        try:
            if isinstance(data, str):
                raise LookError(data)
            if is_pdf(data):
                with _PDFIUM_LOCK:
                    pdf = _open_pdf(data)
                    try:
                        entry["pages"] = len(pdf)
                        im = _render_page(pdf, 1, SHEET_CELL - 8)
                    finally:
                        pdf.close()
            else:
                im, _ = _open_picture(data, SHEET_CELL - 8)
                if im.mode != "RGB":
                    im = Image.open(io.BytesIO(_jpeg(im)))
            canvas.paste(im, (x + (SHEET_CELL - im.width) // 2,
                              y + SHEET_LABEL + (SHEET_CELL - im.height) // 2))
        except LookError as e:
            entry["error"] = str(e)
            box = (x + 8, y + SHEET_LABEL + 8, x + SHEET_CELL - 8, y + pitch - 8)
            draw.rectangle(box, outline=(170, 170, 170))
            draw.line((box[0], box[1], box[2], box[3]), fill=(170, 170, 170))
            draw.line((box[0], box[3], box[2], box[1]), fill=(170, 170, 170))
        draw.text((x + 6, y + 3), str(i + 1), fill=(0, 0, 0), font=font)
        listed.append(entry)
    return _jpeg(canvas), listed
