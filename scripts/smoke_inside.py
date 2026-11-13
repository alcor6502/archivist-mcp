"""The half of scripts/smoke.sh that runs INSIDE the container, as the service
user, from /app. Each check prints one line and the first failure exits 1
naming itself: the image is not published.

It draws through the same code the tools call, with the fonts and the text
engine the IMAGE carries — which is the whole point: the suite draws the same
pages on the CI runner, where the packages were installed by another line of
another file.
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/app")
PORT = os.environ.get("PORT", "3000")


def fail(what: str) -> None:
    print(f"SMOKE FAIL (inside): {what}", file=sys.stderr)
    sys.exit(1)


# 1 — the door is shut to a caller with no token.
req = urllib.request.Request(
    f"http://127.0.0.1:{PORT}/mcp", method="POST",
    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
    headers={"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        fail(f"an unauthenticated tools/list was answered {r.status}, not refused")
except urllib.error.HTTPError as e:
    if e.code != 401:
        fail(f"an unauthenticated tools/list was answered {e.code}, expected 401")
except OSError as e:
    fail(f"nothing answers on 127.0.0.1:{PORT}: {e}")
print("SMOKE ok: an unauthenticated call is answered 401")

# 2 — render_pdf, through the vault's own door: fonts, CAS, git commit.
from vault import VaultRoot  # noqa: E402

ds, _ = VaultRoot("/vault").open("Smoke")
doc = json.load(open("/smoke/fixtures/quadro.json", encoding="utf-8"))
out = ds.render_pdf("smoke.pdf", doc)
if out.get("pages") != 1 or out.get("missing") or not out.get("strings_drawn"):
    fail(f"render_pdf verdict: {({k: out.get(k) for k in ('pages', 'missing', 'strings_drawn')})}")
if not open("/vault/Smoke/smoke.pdf", "rb").read(5) == b"%PDF-":
    fail("render_pdf did not write a PDF")
if out.get("commit") in (None, "(nothing to commit)"):
    fail(f"render_pdf did not commit: {out.get('commit')!r}")
print(f"SMOKE ok: render_pdf drew {out['strings_drawn']} strings on 1 page and committed {out['commit']}")

# 3 — render_report: WeasyPrint, Pango, the DejaVu faces.
out = ds.render_report("report.pdf", {"groups": [{"title": "Smoke", "files": ["scheda.md"]}]},
                       {"title": "Smoke", "text_check": ["Riga di prova"]})
if out.get("files") != 1 or out.get("missing") or out.get("pages", 0) < 2:
    fail(f"render_report verdict: {({k: out.get(k) for k in ('pages', 'files', 'missing')})}")
if not open("/vault/Smoke/report.pdf", "rb").read(5) == b"%PDF-":
    fail("render_report did not write a PDF")
print(f"SMOKE ok: render_report bound 1 file into {out['pages']} pages")

# 3b — look.py: the three libraries that show a file, each through the vault's
# own door. PDFium renders the page just drawn and reads its text; libheif
# opens the format a phone camera writes. All three come from wheels, and a
# wheel missing for this platform is exactly what only the image can show.
seen = ds.view("smoke.pdf")
if len(seen.images) != 1 or seen.images[0][:3] != b"\xff\xd8\xff" or seen.get("pages") != 1:
    fail(f"view of the PDF: {len(seen.images)} pictures, pages={seen.get('pages')}")
said = ds.pdf_text("smoke.pdf", "1")
if "PAPERINO" not in said["text"][0]["text"]:
    fail("pdf_text did not read the text of the page render_pdf drew")
import shutil  # noqa: E402

shutil.copy("/smoke/fixtures/finta.heic", "/vault/Smoke/finta.heic")
heic = ds.view("finta.heic", "", "thumb")
if heic.get("pixels") != [640, 480] or len(heic.images) != 1:
    fail(f"view of a HEIC: {dict(heic)}")
pre = ds.render_pdf("smoke2.pdf", doc, "new", preview=True)
if len(getattr(pre, "images", [])) != 1 or "attached" not in pre.get("look", ""):
    fail("render_pdf with preview=True returned no picture of the page")
print(f"SMOKE ok: view showed a PDF page ({len(seen.images[0])} bytes of JPEG) and a HEIC, "
      "pdf_text read the page, render_pdf previewed it")

# 4 — the manual is in the image, and it is the one the tools serve.
from vault import guide_for, server_instructions  # noqa: E402

text = open("/app/reference-guide.md", encoding="utf-8").read()
if len(guide_for(text)["cards"]) < 20 or not server_instructions(text):
    fail("the reference guide in the image has no cards or no instructions")
print(f"SMOKE ok: the guide is in the image, {len(guide_for(text)['cards'])} cards")
