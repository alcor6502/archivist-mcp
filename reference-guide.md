# Archivist MCP — manual

`reference_guide()` is this page: the model, and only what a signature cannot
tell you. `reference_guide("append")` is one command's card. Ask for the card.

## THE MODEL

The vault root holds **datasets**: top-level directories, each with its own git.
Every call names its dataset in `dataset` — except `dataset_create`, where it
does not exist yet and the parameter is `name`. `path` is relative to that
dataset; **an empty path means the whole dataset**; repeating the dataset at the
head of `path` is refused, never silently corrected.

A **locked** dataset needs its key in `key` on every call. `vault_status()` says
which is which, and it is the call you make first — if it does not answer, stop
and say so. **`key` is always the LAST parameter: pass it by name**, or it lands
in an optional slot you did not know was there and fails for the wrong reason.

Each dataset has its own git, so `HEAD~3` is three commits back *in that
dataset*. Writing a path never creates a dataset; a tool never locks one.

## FOUR THINGS THAT ARE NOT LIKE ELSEWHERE

1. **The sha256 is the unit of truth.** Every read gives it, every write demands
   it and hands back the new one — so a chain of writes needs no re-read in
   between. `"new"` for a file that does not exist yet. A different sha means
   someone wrote after you: re-read, reconcile, retry. Never retry a `CONFLICT`
   unchanged.
2. **Nothing is deleted.** There is no delete tool. Disposal is `move_path` into
   `Trash/`.
3. **Nothing makes a directory**, because git does not keep empty ones. They
   appear as a side effect of writing a file inside them, and **an empty folder
   appears nowhere** — not in `list_files`, `manifest` or `archive`. A folder
   that must stay visible needs a file in it.
4. **Every write is atomic, verified and committed**, which costs a few seconds.
   A failed tool leaves the vault exactly as it was; there is no partial write.
   Do not retry because a call felt slow — that is how a duplicate is made.

## THE CEILING THAT IS NOT THIS SERVER'S

Our limits are on the cards and fail loudly. The one you meet first is not ours:
**the cap your client puts on a tool result.** Above it a client does not
truncate — it writes the result to a file and hands you a path. That is
excellent if the file lands where your code runs, and useless otherwise.

**Test it once:** the first time a result comes back as a path, run
`ls <that path>` where your code runs. There → work freely. Not there → keep
results small, and give `archive` a `max_chars` so it refuses with the size
instead of losing the payload.

## FOUR WRONG TURNS

Do not split a file to get past a size limit — above 2 MB the vault is not the
transport, use SMB or scp. Do not push a big binary through `write_binary`:
the base64 is typed by the model, a token every two or three characters, so a
500 KB PDF is tens of minutes for a write the server does in a millisecond —
drop it over SMB and the next write to the dataset adopts it, sha included —
and a PDF made of data is not typed at all: `render_pdf` draws it here.
Do not read a directory file by file to audit it —
`archive` once, or `manifest` if you only need to know whether anything changed.
Do not read files looking for something — `search`, then `read_file` on the
match. Do not rewrite a file to add a line or change a number — `append`,
`edit_file`.

# COMMANDS

One card each. `reference_guide("<name>")` returns just one.

## vault_status()

Is the vault alive, and which datasets exist. No key needed.
The call you make first. If it does not answer, stop — nothing else will work.

## reference_guide(name='')

This manual. Empty gives the model above; a command name gives that command's
card; an unknown name gives the list of names. No key needed.

## dataset_create(name)

A new dataset: open, empty, with its own git.
Name: no `/`, no leading `.` or `_`. The vault is full at 200 datasets.
The parameter is `name`, not `dataset` — this is the one tool where it is.

## dataset_drop(dataset, expected_manifest)

Deletes a dataset and its git repository. Irreversible.
Demands the current `manifest_sha256`, so it cannot be aimed at the wrong
dataset by accident, and it **refuses outright on a locked dataset**: to destroy
a protected one, remove its line from the key registry on the server by hand.
That is the point, not an obstacle — the one irreversible tool cannot be reached
from a chat alone.

## dataset_status(dataset, key='')

One dataset in detail: file count, Trash, git status, commits, repository size,
last commit.

## list_files(dataset, path='', key='')

Files under `path` with size and sha256, recursive. Same sha, same file.
Empty path means the whole dataset. Stops at 3,000 files: go one level deeper
with `path`. An empty folder does not appear — it is not hidden, it is not there.
Binaries live here: `search` will not find them, this will.

## read_file(dataset, path, key='')

A UTF-8 text file: content plus sha256. That sha is what `write_file` and
`edit_file` want back.
A long document is exactly where you meet your client's result cap — see the
model page.

## append(dataset, path, text, expected_sha256='', key='')

Adds a block to the end of an existing file. Max 64 KB per block.
This is the tool for a log or a register. Do not rewrite a file to add a line.
**No sha needed**: it never touches existing bytes, so no conflict is possible.
**But pass `expected_sha256` when you can** — the sha the last read or write
handed you. It costs nothing when the file is where you left it, and after a
transport error it is the only thing that tells a lost RESPONSE from a lost
REQUEST: if your first append did arrive, the retry is refused with CONFLICT
instead of writing the block twice. Without it, verify the tail with
`list_files` before retrying.

## write_file(dataset, path, content, expected_sha256, key='')

Writes the WHOLE file. UTF-8, max 2 MB.
CAS: `expected_sha256` must match the file's current sha, or `"new"` for a file
that does not exist yet. `"new"` on an existing file is refused, and so is a
real sha on a missing one.

## render_pdf(dataset, path, document, expected_sha256='new', key='')

A PDF **drawn by the server** from a JSON document of blocks, written with the
same CAS as `write_binary` (`"new"` by default). The bytes are born on the
server: a dashboard is ~5 KB of JSON in the call and ~50 KB of PDF on disk,
and the PDF never crosses the model. Real fonts, full Unicode, max 20 pages.
An **empty `path`** renders and returns `pdf_base64` without writing — the
"live" case, for a sheet the chat consumes and the vault never keeps.
Returns `size · sha256 · commit · pages · strings_drawn · missing`.

The document: `{"page", "title", "footer", "forbid", "text_check", "blocks"}`.
- `page`: `{"size": "a4"|"letter", "margin": 28, "landscape": false}`
- `title`: `{"text", "subtitle", "badge", "value"}` — one line, a red badge,
  a big figure on the right; repeated on every page after the first
- `footer`: list of lines, on every page; the last one gets `· pagina N di M`
- `forbid`: regexes that must match NO string drawn (an account number, a
  name) — a match is a refusal and nothing is written
- `text_check`: strings that must appear; the missing ones come back in
  `missing`, they do not stop the render
- `blocks`, in order, each `{"type": …}`:
  `stats` `{items:[{label,value,tone}]}` one row of pills, refused if it does
  not fit · `row` `{items:[blocks]}` side by side, cards stretched equal ·
  `card` `{title, blocks}` · `donut` `{slices:[{label,value,color}], center,
  center_label}` · `grid` `{cols, items:[{label,value,tone}]}` · `gauge`
  `{label, position 0-100, bands:[{to,color}], ticks:[{at,text,align}]}` ·
  `table` `{title, columns:[{label,align,width}], sections:[{title,subtitle,
  rows:[{cells:[…]}]}]}` — first cell `{text,sub}` name + description, others
  `{text,sub,tone,bold}` or `{tag,style}`; a section never splits across
  pages; a cell wider than its column is a refusal · `checklist` `{title,
  numbered, start, items:[{cols:[…]}]}` drawn boxes, columns sized on content
  · `heading` · `paragraph` `{text,size,face,tone}` · `note` `{text,tone,
  align}` · `rule` · `spacer`.
- tones: `navy accent muted green red black desc dark` or any `#rrggbb`;
  tag styles `t-ord t-qual t-muni t-roc t-mix t-none` or `{bg,fg}`.

The server draws; it does not know what the numbers mean. Whatever must add
up, add it up before calling.

## edit_file(dataset, path, old_text, new_text, expected_sha256, key='')

Replaces `old_text` — which must occur EXACTLY once — with `new_text`.
Same CAS as `write_file`. Only the two fragments travel, not the file: on an
80 KB document that is the difference between a light call and a heavy one.
`old_text NOT found` means copy it again, spaces and newlines included.
`found N times` means widen it by a line or two — not by a page.

## move_path(dataset, src, dst, key='')

Move, rename or trash, inside one dataset. Never overwrites.
This is the disposal route: there is no delete tool, and moving into `Trash/` is
how things are thrown away. Returns no sha, and needs none — the content did not
change, so the one you hold still holds.
There is no second dataset to move to: to copy across, `read_file` in one and
`write_file` in the other.

## search(dataset, pattern, path='', regex=False, key='')

Greps the dataset server-side: `file:line:text`, nothing downloaded.
**Literal unless `regex=True`** — a pattern full of `.` and `*` is looked for as
those characters. Stops at 200 lines: narrow `path`, or make the pattern precise.
Text only. A PDF that does not come up is not missing — find binaries by name
with `list_files`.
A truncated result proves presence, never absence.

## manifest(dataset, path='', key='')

A whole tree's fingerprint in one number. Two equal manifests, identical trees.
The cheapest way to ask "did anything move": manifest before, manifest after.
Required by `dataset_drop` and `dataset_restore`.

## archive(dataset, path='', pattern='*.md', max_chars=0, key='')

**Read the whole card before using this one.** It is the tool with the most ways
to hand you less than you asked for, silently.

Every file matching `pattern` under `path`, in ONE call, as a base64 tar.gz.
Needs a sandbox to extract it in.

- **The default is `pattern='*.md'`.** An archive taken without thinking about it
  holds the Markdown and nothing else — no PDFs, no images, no binaries. Pass
  `pattern='*'` when you mean everything. The result says which pattern it
  applied and how many files it skipped: read that line.
- **The ceiling you meet first is your client's, not the 5 MB here.** Over it,
  the result becomes a file path instead of data — useful only if that file
  lands where your code runs. Until you have run the `ls` test on the model
  page, `max_chars=20000` works everywhere.
- **`max_chars` refuses instead of producing.** The refusal says how many
  characters it would have been, so one round trip tells you the size instead of
  losing you the payload. It is 0 by default, meaning "no ceiling of mine".
- A whole dataset at once is the case to think about first. Otherwise go folder
  by folder with `path`.

## read_binary(dataset, path, key='')

Any file — PDF, image, binary — as base64 plus sha256. Max 2 MB.
Useless without a sandbox to decode it in.

## write_binary(dataset, path, content_base64, expected_sha256, key='')

A binary file from base64. Same CAS as `write_file`. Max 2 MB decoded. Fine up
to a few tens of KB; above that the base64 you have to type is the cost, not
the server — see FOUR WRONG TURNS.
Compare the returned sha with the one computed at the source.

## read_at(dataset, path, rev, key='')

A text file as it was at a past revision — a hash from `history`, or `HEAD~3`.
Read-only. The revision is local to this dataset's own git.
To recover: `history` → `read_at` → `write_file`.

## history(dataset, path='', n=10, key='')

The last `n` commits of the dataset, or of one file: hash, ISO date, message.
**Stops at 10 by default** — the eleventh commit back is not missing, it was not
asked for. The short hash goes verbatim into `read_at` and `diff`.

## diff(dataset, rev_a, path='', rev_b='HEAD', key='')

Differences between two revisions. Empty path gives the per-file summary; a file
gives its full diff. Truncates at 60 KB rather than failing.
**`path` sits BETWEEN the two revisions.** `diff(X, a, b)` passes `b` as a path,
silently, and diffs the wrong thing against HEAD. Two moments of one file is
`diff(X, "HEAD~5", path="a.md", rev_b="HEAD")`.

## dataset_restore(dataset, rev, expected_manifest, key='')

Rewrites EVERY file in the dataset back to `rev`. Needs the current
`manifest_sha256`.
Not destructive: it commits forward, so it can itself be undone.

## trash_purge(dataset, before, key='')

Empties `Trash/` of what was thrown away before an ISO date. Contents remain in
git history.
**The date is the date the file was trashed**, not the date it was last
modified: a document from 2024 thrown away yesterday survives
`before="2026-01-01"`. It only looks at `Trash/` at the dataset root.

## ERRORS

| Message | Cure |
|---|---|
| `dataset ... is protected: a key is required` | the key is in the project's instructions |
| `no such dataset` | `vault_status` lists them; it goes in `dataset`, not in `path` |
| `path must be relative to the dataset` | the dataset is repeated at the head of `path`: drop it |
| `path is relative to the dataset, not absolute` | drop the leading `/` |
| `CONFLICT: expected sha ...` | re-read, reconcile, retry — never unchanged |
| `the file already exists` | `"new"` on an existing file: pass the real sha |
| `the file does not exist` | the opposite: pass `"new"` |
| `old_text NOT found` | copy the fragment exactly, spaces and newlines included |
| `old_text found N times` | widen the context until it is unique |
| `path not allowed` | a segment is `..`, `.git`, or a lockfile |
| `destination already exists` | `move_path` never overwrites: choose another name |
| `more than 3000 files` | go one level deeper with `path` |
| `has a key and cannot be dropped` | take its line out of the key registry first |
| `CONFLICT: expected manifest ...` | re-read the manifest, then retry |
| `block too large` | `append` is not for rewrites: `write_file` |
| `file too large` | above 2 MB, move it over SMB/scp |
| `the archive would be N characters of base64, over the ...` | your own `max_chars` stopped it before it was built |
| `dataset ... is no longer there` | it was dropped while your call was in flight |
