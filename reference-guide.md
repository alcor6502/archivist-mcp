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
and a PDF made of data is not typed at all: `render_pdf` draws it here, and a
PDF of documents this vault already holds is `render_report`.
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

**Faces are `R` `B` `I` `BI`** and nothing else — Liberation Sans regular,
bold, italic, bold italic. `"bold"` is a refusal that lists the four.
**Tones**: `navy accent muted green red black desc dark`, or any `#rrggbb`.
Tag styles: `t-ord t-qual t-muni t-roc t-mix t-none`, or `{bg, fg}`.

### The blocks

Each is `{"type": …}`. **Every default below is what the renderer draws when
the field is absent**, and a row marked *fixed* is written in the renderer:
no field reaches it, so a page that needs it different needs a release.

**`heading`** `{text}` — text required.
| field | default | notes |
|---|---|---|
| *face/size/tone* | *fixed* `B` / 13 / `navy` | ⚠ **identical to a card title and to a table title.** A `heading` reached for as "bold text inside a card" gives two lines of the same weight and no hierarchy — use `paragraph` with `face: "B"` |
| *height* | *fixed* 22 | |

**`paragraph`** `{text, size, face, tone}` — text required, wraps to the width.
| field | default | values |
|---|---|---|
| `size` | 9.5 | any |
| `face` | `R` | `R` `B` `I` `BI` |
| `tone` | `black` | a tone or `#rrggbb` |
| *line spacing* | *fixed* 1.35 × size, +4 at the foot | |

**`note`** `{text, size, tone, align, italic}` — a footnote line.
| field | default | values |
|---|---|---|
| `size` | 6.5 | any |
| `tone` | `red` | a tone or `#rrggbb` |
| `align` | `left` | `left` `center` `right` |
| `italic` | `true` | `false` draws `R` instead of `I` |

**`rule`** `{gap, tone, lw}` — a line across the width. `gap` 8 (the block's
whole height, the line sits in the middle), `tone` `#e3e8ee`, `lw` 0.5.

**`spacer`** `{height}` — height 8, draws nothing.

**`stats`** `{items:[{label, value, tone, bg}]}` — one row of pills, the
leftover width shared equally. **A band that does not fit on one row is a
refusal**, not a squeeze.
| field | default | notes |
|---|---|---|
| `tone` | `navy` | colours the value |
| `bg` | the tone's own pale shade | any `#rrggbb` |
| *label / value* | *fixed* `R` 7.8 `desc` / `B` 9.5 tone | |
| *pill* | *fixed* height 17, padding 10, radius 8, gap 12 | the block measures 29 |

**`grid`** `{cols, items:[{label, value, tone}]}` — small boxes, label above,
value below. `cols` 2, at least 1 or a refusal.
| field | default | notes |
|---|---|---|
| `tone` | `navy` | colours the value |
| *label* | *fixed* `R` 6.6 `muted`, **upper-cased** | |
| *value* | *fixed* `B` 11.5 | |
| *box* | *fixed* height 28, gaps 8 × 6, radius 5, fill `#f6f8fa` | |

**`gauge`** `{label, position, bands, ticks, marker, thickness, label_*}` —
coloured bands with a marker. `position` 0-100 required, outside that a refusal.
| field | default | values |
|---|---|---|
| `label` | none | the line above the bar; **upper-cased unless `label_upper` is false** |
| `label_upper` | `true` | `false` keeps the case you sent |
| `label_face` | `R` | `R` `B` `I` `BI` |
| `label_size` | 6.6 | above 0, or a refusal |
| `label_tone` | `muted` | a tone or `#rrggbb` |
| `thickness` | 18 | the bar's height, above 0 or a refusal |
| `bands` | one band to 100, `#f6f8fa` | `[{to, color}]`, `to` a percent, cumulative left to right |
| `marker` | `accent` | a tone or `#rrggbb` — the line at `position` |
| `ticks` | none | `[{at, text, align, face, size, tone}]`, `at` a percent |
| tick `align` | `center` | `left` `center` `right` |
| tick `face` / `size` / `tone` | `R` / 6.2 / `muted` | per tick, not per gauge |
| *top gap* | *fixed* 8 | above the label |
| *tick baseline* | *fixed* 9 under the bar | |
| *marker* | *fixed* 3 wide, 2 proud of the bar top and foot | |

The block's height **follows what it draws**: `8 + label line + thickness +
tick tail`, where the label line is 1.82 × `label_size` and the tail is
`9 + 0.25 ×` the biggest tick's size. With every default and both a label and
ticks that is 48.55 — it was a constant 46 until 3.2.0, which is why a block
placed under a gauge used to be drawn on by the ticks.

**`donut`** `{slices:[{label, value, color}], center, center_label}` — sectors
drawn, with a legend and the percentages. The values must sum above zero; **a
slice at 0 keeps its legend line and draws no sector**; a negative one is a
refusal by name.
| field | default | notes |
|---|---|---|
| slice `color` | `accent` | a tone or `#rrggbb` |
| *ring* | *fixed* radius 42, hole 24, legend row 18 | |
| *legend / percent* | *fixed* `R` 8 `desc` / `B` 9.5 `navy`, right | |
| *centre* | *fixed* `B` 10.5 `navy`, its label `R` 6.2 `muted` | |

**`card`** `{title, blocks, padding, pad_top, pad_bottom, title_size}` — a
white box with a border. **Each gap is its own field**: until 3.3.0 one number
was the left inset, the right inset, the top gap and the bottom gap at once,
which is why a card holding a gauge read bottom-heavy — under the last line
sits the gauge's own tick tail AND the padding.
| field | default | which gap it governs |
|---|---|---|
| `title` | none | the line at the top; the band it adds is `title_size` × 12/13 |
| `title_size` | 13 | the title's type size, and with it the band and the baseline. Above 0 or a refusal |
| `padding` | 12 | the **left and right** inset, and the default of the two below |
| `pad_top` | `padding` | the room above the content — **above the title when there is one**, so it moves the title too. Negative is a refusal |
| `pad_bottom` | `padding` | the room under the last block. Negative is a refusal |
| *title face and tone* | *fixed* `B` `navy` | ⚠ at the default size, the same as `heading` and a table title |
| *box* | *fixed* radius 7, border `#e3e8ee`, white fill | |

So the head above the content is `pad_top` with no title, and `pad_top +
title_size × 12/13` with one — 12 and 24 at the defaults, which are the two
numbers that used to be written in the renderer. Stretched to a taller
neighbour in a `row`, a card **centres** its content in the room left over.


**`row`** `{items:[blocks]}` — side by side, **equal widths**, gap 12; cards
stretched to the tallest. At least one item.

**`table`** `{title, columns, sections}` — see the geometry below.

**`checklist`** `{title, numbered, start, items:[{cols:[…]}]}` — numbered rows
with a **drawn** box, columns sized on this sheet's content. `numbered` true,
`start` 1. A one-column item wraps; a multi-column row that is wider than the
page is a refusal. Fixed: box 10.2, row 22.7, text `R` 11, title `B` 12.

### Table geometry, which is where the refusals come from

`columns: [{label, align, width}]`, `sections: [{title, subtitle, rows:
[{cells:[…]}]}]`. First cell `{text, sub}` — a name and a description; the
others `{text, sub, tone, bold}` or `{tag, style}` / `{tag, bg, fg}`.

- **`width` is a fraction of the row, strictly above 0 and below 0.9.** Not
  points. Their sum must stay under 0.9.
- **The FIRST column's declared width is ignored**: it takes whatever the
  others leave. Give it a `label` and no `width`.
- **A cell wider than its column is a refusal**, never a wrap and never an
  overflow into the neighbour. So is a column label wider than its column.
  Only the first column's `sub` is truncated, with an ellipsis.
- The budget is `width × usable − 14` points. On A4 portrait with margin 28
  the usable width is 539.3pt, and a digit of the cell face (`B` 8.6) is
  4.78pt:

  | `width` | points | budget | digits | fits |
  |---|---|---|---|---|
  | 0.086 | 46.4 | 32.4 | 6 | `$1,234` |
  | 0.114 | 61.5 | 47.5 | 9 | `$1,234,56` |
  | 0.126 | 67.9 | 53.9 | 11 | `+$121,400` |
  | 0.165 | 89.0 | 75.0 | 15 | `+$121,400 +19.6%` |
  | 0.20 | 107.9 | 93.9 | 19 | |

  A cell carrying a `sub` spends both on the same line, plus 4 between them.
  Landscape A4 gives 785.9pt to spend instead, so the same fraction buys ~45%
  more.
- Fixed: section title `BI` 10.5 `navy`, its subtitle `R` 8 `muted`, column
  labels `B` 6.8 `muted` upper-cased, the name `B` 8.6, its description `R` 8
  `desc`, a bold cell `B` 8.6, a plain one `R` 8.4, a `sub` `R` 6.8 `muted`,
  tags `B` 6.6. Header 17, row 16, title 22.
- A **section never splits** across pages; a section taller than a page is a
  refusal. The table itself flows.

### What nests in what

Every block may sit inside a `card` or a `row`, including another `card` or
`row`. Two things to know before nesting:
- a `table` inside a `card` **cannot flow to the next page** — the card is
  measured whole, so a long table there is refused as a card taller than a
  page. A table that needs to flow belongs at the top level.
- a `row` inside a `row` divides the width again, and nothing warns you.

### Sizes at a glance, because they are the hierarchy

`title.text` `B` 19 · `title.value` `B` 21 `navy` · subtitle `R` 8.5 `muted` ·
badge `B` 8.5 `red` · **`heading` = card title = table title, all `B` 13
`navy`** (only the card's is settable, with `title_size`) · checklist title `B` 12 · donut centre `B` 10.5 · table section
title `BI` 10.5 · grid value `B` 11.5 · checklist text `R` 11 · `paragraph`
9.5 · stats value `B` 9.5 · table cell `B` 8.6 bold, `R` 8.4 plain, `sub` 6.8
· footer `R` 8 · stats label 7.8 · table column label 6.8 · gauge label 6.6 ·
grid label 6.6 · `note` `I` 6.5 · gauge tick 6.2.

The server draws; it does not know what the numbers mean. Whatever must add
up, add it up before calling.

## render_report(dataset, path, sources, layout=None, expected_sha256='new', key='')

Markdown **already in the dataset**, bound into one PDF laid out by the server
and written with the same CAS as `write_binary`. The twin of `render_pdf` seen
from the other side: there you compose a page the vault has not got, here the
vault already holds the schede and the call only says which ones and in what
order — about a kilobyte for a thirty-file report. Max 200 pages and
5 MB of Markdown in. `path` is **required**: a report never comes back as
base64.
Returns `size · sha256 · commit · pages · files · empty_groups · missing ·
sections`, where `sections` is `[{"file", "page"}]` — the index as printed, so
the chat can say "Emocromo on page 7" without reopening the PDF.

`sources`: `{"base": "", "groups": [ … ]}`. `base` is the folder to start from
(empty = the dataset root) and the listing is **not recursive**: a pattern on
the root never reaches a subfolder, `Trash/` included. Each group is
`{"title", "landscape", "files" | "pattern", "exclude"}`:
- `files`: an explicit list, printed in the order given; a name that is not
  there is a refusal, and so is one that is not `.md`
- `pattern`: one glob on the file name, alphabetical; matches nothing = an
  empty group, reported in `empty_groups`, not a refusal
- `exclude`: globs removed afterwards · `landscape`: that group's pages turn,
  so one PDF holds both orientations
- the same file in two groups is a refusal: a scheda prints once.

`layout`, all optional: `title` (the cover, defaults to "Report") · `subtitle`
· `cover_note` · `footer` — one line on every page, the server appends
`· pag. N di M` · `page` `"a4"|"letter"` · `toc` (default true: an index after
the cover, one section per group, real page numbers with leader dots and
internal links) · `font_pt` (default 9.5) · `forbid` and `text_check`, exactly
as in `render_pdf` — checked on the strings that are ON THE PAGE, footer and
index included.

Layout is fixed and not a parameter: one section per file always on a fresh
page, the file name as a breadcrumb, HTML comments dropped, internal links
flattened to text, bold in dark red, tables at 8 pt (7 pt over 11 columns,
6.2 pt over 15) with the header row shaded, a row never split across pages,
fenced code in DejaVu Sans Mono. A table that still does not fit the margin is
a **refusal** naming the file and the column count: a column cut off at the
edge is worse than a report that does not print. Nothing external is ever
fetched — an `<img src="…">` in a scheda is a refusal, not a download.

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
