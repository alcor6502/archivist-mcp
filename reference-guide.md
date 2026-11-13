# Archivist MCP — manual

`reference_guide()` is this page: the model, and only what a signature cannot
tell you. `reference_guide("append")` is one command's card. Ask for the card.

## IN SHORT

Every call costs seconds of travel, and what it returns fills your context:
make few calls, and ask each one for only what you need.

- **Look around** → `list_files` with `depth=1`: this level, with the folders
  below summed up; then the folder you want. `hashes=False` when names and
  sizes are enough.
- **Find** → `search` (the argument is `pattern`) with `context=2`: the lines
  around each hit come with it, often with no read needed after.
- **Read** → a few lines of a long file: `read_file` with `from_line` and
  `lines`, at the line number `search` gave. Several files: ONE `read_files`.
- **Change** → `edit_file` (`old_text`/`new_text`; several changes in ONE call
  with `edits`). Add at the end → `append`. `write_file` is for a new file or
  a full rewrite, never to change a line.
- **Did anything change?** → `manifest`: one number, no content.
- **See** → `view`: a photo, a scan, the pages of a PDF, a folder come back as
  pictures. What a PDF says → `pdf_text`.
- **Made a PDF?** LOOK at it before saying it is done: `render_pdf` with
  `preview=True` returns the page as a picture; a report, `view` on it.
- Every read and every write returns the file's `sha256`, a partial read
  too: pass it to the next write of that file, with no re-read. `CONFLICT` →
  re-read, reconcile, retry. A write lost in transport → `list_files` on that
  file, compare the sha, then retry.
- Every call names its `dataset`; `path` is relative to it. A locked dataset
  needs `key` on every call: pass it BY NAME — it is always the last
  parameter. The key is in the instructions of the project it belongs to.
- No `vault_status()` before working: it is for learning the datasets' names.
  Read the card before `render_pdf` and `render_report`,
  `reference_guide("render_pdf")`; for the others, when the description is
  not enough.
- `archive` is a base64 tar.gz: for clients that run code, not for a chat.

## THE MODEL

The vault root holds **datasets**: top-level directories, each with its own git.
`dataset_create` is the one tool whose parameter is `name`, not `dataset`.
**An empty path means the whole dataset**; repeating the dataset at the head of
`path` is refused, never silently corrected.

The key registry on the server decides access, and nothing else: a dataset is
**locked** (key on every call), **open** (no key) or **closed** (no line in
the registry: every call refused). `vault_status()` says which. No tool opens,
closes or locks a dataset, and a new one is born closed.

Each dataset has its own git, so `HEAD~3` is three commits back *in that
dataset*. Writing a path never creates a dataset. Git's own control files —
`.gitignore`, `.gitattributes`, `.gitmodules` — can be read and never written,
edited or moved: they are changed on the server, by the owner.

## FOUR THINGS THAT ARE NOT LIKE ELSEWHERE

1. **The sha256 is the unit of truth.** A write demands the one you hold;
   `"new"` for a file that does not exist yet. Never retry a `CONFLICT`
   unchanged.
2. **Nothing is deleted.** There is no delete tool. Disposal is `move_path` into
   `Trash/`.
3. **Nothing makes a directory**, because git does not keep empty ones. They
   appear as a side effect of writing a file inside them, and **an empty folder
   appears nowhere** — not in `list_files`, `manifest` or `archive`.
4. **Every write is atomic, verified and committed.** A failed tool leaves the
   vault exactly as it was; there is no partial write. Do not retry because a
   call felt slow — that is how a duplicate is made.

## THE CEILING THAT IS NOT THIS SERVER'S

Our limits are on the cards and fail loudly. The one you meet first is not ours:
**the cap your client puts on a tool result.** Above it a client does not
truncate — it writes the result to a file and hands you a path, useless unless
that file lands where your code runs. So keep results small: a range of lines,
fewer files per `read_files`, a `max_chars` on `archive`.

## WRONG TURNS

Do not split a file to get past a size limit — above 2 MB the vault is not the
transport, use SMB or scp. Do not push a big binary through `write_binary`:
the base64 is typed by the model, a token every two or three characters —
drop it over SMB and the next write to the dataset adopts it, sha included.
A PDF made of data is not typed at all: `render_pdf` draws it here, and a PDF
of documents this vault already holds is `render_report`.

# COMMANDS

One card each. `reference_guide("<name>")` returns just one.

## vault_status()

Which datasets exist, each `locked`, `open` or `closed`. No key needed.
Not a ritual before every task: call it when you do not know a dataset's name,
or when calls start failing — if it does not answer, stop and say so. It also
lists `orphan_registry_lines`: names in the key registry that match no dataset,
which is what a typo looks like (the dataset next to it shows up `closed`).

## reference_guide(name='')

This manual. Empty gives the model above; a command name gives that command's
card; an unknown name gives the list of names. No key needed.

## dataset_create(name)

A new, empty dataset with its own git. **It is born `closed`**: every call on it
is refused until the vault's owner adds `name |` (open) or `name | key` (locked)
to the key registry on the server. No tool writes the registry — say so to the
person, and wait.
Name: no `/`, no leading `.` or `_`. The vault is full at 200 datasets.
The parameter is `name`, not `dataset` — this is the one tool where it is.

## dataset_drop(dataset, expected_manifest)

Deletes a dataset and its git repository. Irreversible.
Demands the current `manifest_sha256`, so it cannot be aimed at the wrong
dataset by accident, and it **works on open datasets only**: to destroy a
locked or closed one, its line in the key registry must first become `name |`,
by hand, on the server.
That is the point, not an obstacle — the one irreversible tool cannot be reached
from a chat alone.

## dataset_status(dataset, key='')

One dataset in detail: file count, Trash, git status, commits, repository size,
last commit. Numbers only: to see the files, `list_files` with `depth=1`.

## list_files(dataset, path='', depth=0, pattern='', hashes=True, key='')

Files under `path` with size and sha256, recursive. Same sha, same file.
**On a dataset you do not know, start with `depth=1`**: the whole tree of a
large one is thousands of lines you will not use.
Empty path means the whole dataset. An empty folder does not appear — it is not
hidden, it is not there. Binaries live here: `search` will not find them, this
will.
Three ways to ask for less, and they combine:
- `depth=1` lists the files directly in `path`; everything deeper is summed up
  in `folders`, one line per folder: `path`, `files`, `bytes`. That is how a big
  tree is walked: one level, then the folder you want. `depth=2` goes two
  levels down, and so on; 0 is the whole tree.
- `pattern="*.pdf"` keeps the files whose path matches, at any depth
  (`"Invoices/*.pdf"` asks for the folder too). The same glob as `archive`.
- `hashes=False` gives `path` and `size` only: nothing is read, so it is the
  cheap way to look at a folder of photos.
Stops at 3,000 files listed: narrow it with one of the three, or with `path`.
The Finder's `.DS_Store` is never listed nor counted, by any tool.
A file above 2 MB is listed with its size and `unhashed` in place of the sha;
call `list_files` on that one file and the sha is computed, whatever the size.

## read_file(dataset, path, from_line=0, lines=0, key='')

A UTF-8 text file: content plus sha256. That sha is what `write_file` and
`edit_file` want back.
**Do not read a long file whole for a few lines of it**: `search` with
`context` first, then a range here.
**Part of a file:** `from_line` (1-based) and `lines` (how many; 0 means to the
end). `lines=40` alone is the first forty; `from_line=812, lines=30` is what
you read after `search` said `:812:` — the two number lines the same way. The
answer adds `total_lines`, `from_line` and `lines` (how many came back), and
`content` is exactly those lines. `sha256` and `size` are still the WHOLE
file's, so a partial read is enough to `edit_file` or `append` — never
`write_file`: that would replace the file with the part you read.
A long document is exactly where you meet your client's result cap — see the
model page. More than one file → `read_files`.

## read_files(dataset, paths, key='')

Several UTF-8 text files in ONE call: `files` holds, for each path in the order
asked, `path`, `size`, `sha256` and `content` — the same as `read_file`, so each
sha is what `edit_file` wants back. A file that cannot be read (missing, binary,
not a file) comes back as `{path, error}` and the others still arrive; `errors`
counts them. A path no read could accept (`..`, absolute, the dataset repeated)
refuses the whole call. At most 50 paths, and 2 MB in all — the ceiling of one
read: ask for fewer at a time, not around it.
Whole files only. The paths come from one `list_files` or one `search`: two
calls for a folder's worth of reading. For a part of a long file, `read_file`
with `from_line`.

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

Writes the WHOLE file. UTF-8, max 2 MB. For a new file or a full rewrite: the
whole content travels, typed by you. To change part of a file, `edit_file`
sends only the fragments; to add at the end, `append`.
CAS: `expected_sha256` must match the file's current sha, or `"new"` for a file
that does not exist yet. `"new"` on an existing file is refused, and so is a
real sha on a missing one.

## render_pdf(dataset, path, document, expected_sha256='new', preview=False, key='')

A PDF **drawn by the server** from a JSON document of blocks, written with the
same CAS as `write_binary` (`"new"` by default). The bytes are born on the
server: a dashboard is ~5 KB of JSON in the call and ~50 KB of PDF on disk.
Real fonts, full Unicode, max 20 pages.
Returns `size · sha256 · commit · pages · strings_drawn · missing · look`.

**A PDF is not done until it has been looked at.** `pages`, `strings_drawn`
and `missing` say it was drawn; a clipped cell, a marker in the wrong band and
an empty box are on the page and in none of those numbers. So:
1. call it with **`preview=True`**: the pages come back as pictures in the
   same answer (the first 10; the rest with `view`);
2. look: one page if one was meant, nothing cut or overlapping, tones and
   markers where they belong;
3. wrong → fix the document and render again **to the same path with the
   `sha256` just returned**, not to a new name;
4. to hand the file to the person: `read_binary` on the path, in a client
   that runs code.

An **empty `path`** renders and returns `pdf_base64` without writing — the
"live" case, for a sheet the chat consumes and the vault never keeps.

The document: `{"page", "title", "footer", "forbid", "text_check", "blocks"}`.
- `page`: `{"size": "a4"|"letter", "margin": 28, "landscape": false}`
- `title`: `{"text", "subtitle", "badge", "value"}` — one line, a red badge,
  a big figure on the right; repeated on every page after the first. The line
  is measured: text, subtitle and badge must end 6 points before `value`.
  Too long is a refusal that says by how many points: shorten the subtitle
- `footer`: list of lines, on every page; the last one gets `· pagina N di M`
- `forbid`: regexes that must match NO string drawn (an account number, a
  name) — a match is a refusal and nothing is written; so is a list that takes
  more than 5 seconds to run
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

**`heading`** `{text}` — text required, one line: **wider than its width is a refusal**.
| field | default | notes |
|---|---|---|
| *face/size/tone* | *fixed* `B` / 13 / `navy` | ⚠ **identical to a table title**, and bigger than a card title (11). A `heading` reached for as "bold text inside a card" outweighs the card's own title — use `paragraph` with `face: "B"` |
| *height* | *fixed* 22 | |

**`paragraph`** `{text, size, face, tone}` — text required, wraps to the width.
| field | default | values |
|---|---|---|
| `size` | 9.5 | any |
| `face` | `R` | `R` `B` `I` `BI` |
| `tone` | `black` | a tone or `#rrggbb` |
| *line spacing* | *fixed* 1.35 × size, +4 at the foot | |

**`note`** `{text, size, tone, align, italic}` — a footnote; a long one wraps to
the width (until 3.4.0 it ran off the page), and each line keeps `align`.
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
| *label* | *fixed* `R` 6.6 `muted`, **upper-cased** | a label wider than its box is **cut with an ellipsis** |
| *value* | *fixed* `B` 11.5 | a value wider than its box is a **refusal** — a number cut short is another number |
| *box* | *fixed* height 28, gaps 8 × 6, radius 5, fill `#f6f8fa` | |

**`gauge`** `{label, position, bands, ticks, marker, thickness, label_*}` —
coloured bands with a marker. `position` 0-100 required, outside that a refusal.
| field | default | values |
|---|---|---|
| `label` | none | the line above the bar; **upper-cased unless `label_upper` is false**. Wider than the gauge is a refusal |
| `label_upper` | `true` | `false` keeps the case you sent |
| `label_face` | `R` | `R` `B` `I` `BI` |
| `label_size` | 6.6 | above 0, or a refusal |
| `label_tone` | `muted` | a tone or `#rrggbb` |
| `thickness` | 14 | the bar's height, above 0 or a refusal (18 until 3.4.0) |
| `bands` | one band to 100, `#f6f8fa` | `[{to, color}]`, `to` a percent, cumulative left to right |
| `marker` | `accent` | a tone or `#rrggbb` — the line at `position` |
| `ticks` | none | `[{at, text, align, face, size, tone}]`, `at` a percent |
| tick `align` | `center` | `left` `center` `right` |
| tick `face` / `size` / `tone` | `R` / 6.2 / `muted` | per tick, not per gauge |
| *top gap* | *fixed* 8 | above the label |
| *tick gap* | *fixed* 7 | clear space from the bar's foot to the top of the tick's ink, **at any tick size** |
| *tick at an end* | | slides inward so its ink stays within the bar's width, whatever its `align` |
| *marker* | *fixed* 3 wide, 2 proud of the bar top and foot | |

The block's height **follows what it draws**: `8 + label line + thickness +
tick tail`, where the label line is 1.82 × `label_size` and the tail is
`7 + 0.978 ×` the biggest tick's size (its ink, 0.728 em, and room under the
baseline, 0.25 em). With every default and both a label and ticks that is
47.06. It was a constant 46 until 3.2.0, which is why a block placed under a
gauge used to be drawn on by the ticks; and until 3.4.0 the ticks hung at a
fixed baseline, so a big one rose into the bar.

**`donut`** `{slices:[{label, value, color}], center, center_label}` — sectors
drawn, with a legend and the percentages. The values must sum above zero; **a
slice at 0 keeps its legend line and draws no sector**; a negative one is a
refusal by name.
| field | default | notes |
|---|---|---|
| slice `color` | `accent` | a tone or `#rrggbb` |
| *ring* | *fixed* radius 42, hole 24, legend row 18 | |
| *legend / percent* | *fixed* `R` 8 `desc` / `B` 9.5 `navy`, right | a label longer than the room its percentage leaves is cut with an ellipsis |
| *centre* | *fixed* `B` 10.5 `navy`, its label `R` 6.2 `muted` | |

**`card`** `{title, blocks, padding, pad_top, pad_bottom, title_size}` — a
white box with a border. **Each gap is its own field**: until 3.3.0 one number
was the left inset, the right inset, the top gap and the bottom gap at once,
which is why a card holding a gauge read bottom-heavy — under the last line
sits the gauge's own tick tail AND the padding.
| field | default | which gap it governs |
|---|---|---|
| `title` | none | the line at the top; the band it adds is `title_size` × 1.19 (its ink and the room under it). **Wider than the insets, it shrinks to fit, down to 75% of `title_size`; past that a refusal** |
| `title_size` | 11 | the title's type size (13 until 3.4.0), and with it the band. Above 0 or a refusal |
| `padding` | 12 | the **left and right** inset, and the default of the two below |
| `pad_top` | `padding` | the clear room above the content — **above the title's ink when there is one**, so it moves the title too. Negative is a refusal |
| `pad_bottom` | `padding` | the room under the last block. Negative is a refusal |
| *title face and tone* | *fixed* `B` `navy` | at 13 it is the same as `heading` and a table title |
| *box* | *fixed* radius 7, border `#e3e8ee`, white fill | |

So the head above the content is `pad_top` with no title, and `pad_top +
title_size × 1.19` with one — 12 and 25.08 at the defaults. Stretched to a
taller neighbour in a `row`, a card keeps its content **under its title** and
the spare room goes to the foot (until 3.4.0 the content was centred, which
opened a hole under the title).


**`row`** `{items:[blocks]}` — side by side, **equal widths**, gap 12; cards
stretched to the tallest. At least one item.

**`table`** `{title, columns, sections}` — see the geometry below.

**`checklist`** `{title, numbered, start, items:[{cols:[…]}]}` — numbered rows
with a **drawn** box, columns sized on this sheet's content. `numbered` true,
`start` 1. A one-column item wraps; a multi-column row that is wider than the
page is a refusal, and so is a title wider than the width. Fixed: box 10.2,
row 22.7, text `R` 11, title `B` 12.

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
- **The table title must fit the width, and a section title the first column**
  (it shares the line with the column labels): past that, a refusal. The
  section subtitle, after the title on the same line, is cut with an ellipsis
  at the first column's edge.
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
badge `B` 8.5 `red` · **`heading` = table title, `B` 13 `navy`** · checklist
title `B` 12 · **card title `B` 11 `navy`** (settable, with `title_size`) · donut centre `B` 10.5 · table section
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
sections · look`, where `sections` is `[{"file", "page"}]` — the index as
printed, so the chat can say "Emocromo on page 7" without reopening the PDF.

**Look at it before saying it is done**: ONE `view` on the same path with a
list of pages — the cover, the index and one page per kind of content (a wide
table, a landscape group) taken from `sections`: `pages="1-2,7,31"`. Not all
of them.

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

## edit_file(dataset, path, expected_sha256, old_text='', new_text='', edits=None, key='')

Replaces `old_text` — which must occur EXACTLY once — with `new_text`.
Same CAS as `write_file`. Only the fragments travel, not the file: on an
80 KB document that is the difference between a light call and a heavy one.
The names are `old_text` and `new_text` — not `old_string`, not `old_str`.

**Several changes to one file are ONE call**:
`edits=[{"old_text": "a", "new_text": "b"}, {"old_text": "c", "new_text": "d"}]`.
They apply in order, each on the text the previous one left, each matching
exactly once at its turn; one that fails and nothing is written, with a message
naming which (`edit 2 of 3: ...`). One commit, one new sha. Up to 50. Use either
`old_text`/`new_text` or `edits`, never both.
`old_text NOT found` means copy it again, spaces and newlines included.
`found N times` means widen it by a line or two — not by a page.

## move_path(dataset, src, dst, key='')

Move, rename or trash, inside one dataset. Never overwrites. A folder moves
with everything in it, in one call: do not move its files one by one.
This is the disposal route: there is no delete tool, and moving into `Trash/` is
how things are thrown away. Returns no sha, and needs none — the content did not
change, so the one you hold still holds.
There is no second dataset to move to: to copy across, `read_file` in one and
`write_file` in the other.

## search(dataset, pattern, path='', regex=False, context=0, ignore_case=False, key='')

Greps the dataset server-side: `file:line: text`, nothing downloaded. The text
to find goes in `pattern` — there is no `query`.
**Literal unless `regex=True`** — a pattern full of `.` and `*` is looked for as
those characters. `ignore_case=True` works with both.
`context=N` (0 to 5) prints N lines before and after each match, as grep does:
`file:12: text` is a match, `file-11- text` a line around it, `--` separates
two groups. With it a hit usually needs no `read_file` afterwards.
Stops at 200 matches: narrow `path`, or make the pattern precise. A regex gets
5 seconds for the whole call and is then refused, with nothing returned.
Text only. A PDF that does not come up is not missing — find binaries by name
with `list_files`.
A truncated result proves presence, never absence.

## manifest(dataset, path='', key='')

A whole tree's fingerprint in one number. Two equal manifests, identical trees.
The cheapest way to ask "did anything move": manifest before, manifest after —
a few bytes, where `list_files` would return every path to compare by eye.
`path` narrows it to a folder or one file.
Required by `dataset_drop` and `dataset_restore`.

## archive(dataset, path='', pattern='*.md', max_chars=0, key='')

**Read the whole card before using this one.** It is the tool with the most ways
to hand you less than you asked for, silently.

Every file matching `pattern` under `path`, in ONE call, as a base64 tar.gz.
Needs a sandbox to extract it in. In a chat without one, `read_files`.

- **The default is `pattern='*.md'`.** An archive taken without thinking about it
  holds the Markdown and nothing else — no PDFs, no images, no binaries. Pass
  `pattern='*'` when you mean everything. The result says which pattern it
  applied and how many files it skipped: read that line.
- **The ceiling you meet first is your client's, not the 5 MB here.** Over it,
  the result becomes a file path instead of data — useful only if that file
  lands where your code runs: the first time a result comes back as a path,
  `ls` it where your code runs. Until then, `max_chars=20000` works everywhere.
- **`max_chars` refuses instead of producing.** The refusal says how many
  characters it would have been, so one round trip tells you the size instead of
  losing you the payload. It is 0 by default, meaning "no ceiling of mine".
- A whole dataset at once is the case to think about first. Otherwise go folder
  by folder with `path`.

## read_binary(dataset, path, key='')

The **original bytes** of any file — PDF, image, binary — as base64 plus
sha256. Max 2 MB. It is for handing a file on: to the person, or to code.
In a client that runs code the answer is too large for the context, so the
client saves it to a file and gives you its path: decode `content_base64`
from there and compare the sha256.
**Not for looking.** To see a picture or a PDF: `view`. What a PDF says:
`pdf_text`. A sha alone: `list_files` on the file.

## view(dataset, path='', pages='', size='medium', key='')

**See a file.** The answer is JSON plus pictures (JPEG); nothing is written,
and the original never leaves at full resolution.
- **A picture** — JPEG, PNG, HEIC, WebP, GIF, TIFF, BMP: reduced to 1,500
  pixels on its long side, upright. Returns `pixels` (the original) and
  `shown`.
- **A PDF**: `pages=""` is page 1, `"3"`, `"2-5"`, `"all"`, or a list of
  those — `"1-2,11,40"` is the cover, the index and two pages far apart in
  one call. They come back in page order. Max 10 pages
  per call; `more` says which were left out and how to ask for them. Returns
  `pages` (how many it has) and `shown`.
- **A folder** (`path=""` is the dataset root): one numbered sheet of the
  pictures and PDFs directly in it, 30 per sheet, `pages="2"` for the second.
  `items` gives each number its file name. Not recursive.
- `size="thumb"`: 400 pixels, to tell things apart; `medium`, the default,
  reads a page of text.

A file is judged by its content, not its name. The source may be up to
100 MB. `pages` on a picture is a refusal.
**This is how a PDF you rendered is checked**, when `preview=True` was not
used or the PDF has more pages than a preview shows.

## pdf_text(dataset, path, pages='', key='')

What a PDF **says**, without the PDF crossing the wire.
- `pages=""`: the map — `pages`, `outline` (`level · title · page`) and
  `chars_per_page`. Read it first on a long PDF, then ask for the pages.
- `pages="3"`, `"2-5"`, `"1-2,11"`, `"all"`: `text`, one entry per page.
  Max 50 pages
  per call and 2 MB of text.

**No OCR.** A page with no text layer is a scan or a picture: it is listed in
`no_text`, and it is read by looking at it with `view`.

## write_binary(dataset, path, content_base64, expected_sha256, key='')

A binary file from base64. Same CAS as `write_file`. Max 2 MB decoded. Fine up
to a few tens of KB; above that the base64 you have to type is the cost, not
the server — see WRONG TURNS on the model page.
Compare the returned sha with the one computed at the source.

## read_at(dataset, path, rev, key='')

A text file as it was at a past revision — a hash from `history`, or `HEAD~3`.
Read-only. The revision is local to this dataset's own git.
To recover: `history` → `read_at` → `write_file`. To see what changed without
reading two whole versions, `diff`.

## history(dataset, path='', n=10, key='')

The last `n` commits of the dataset, or of one file: hash, ISO date, message.
Give `path` whenever the question is about one file: the answer is its commits
only, not the dataset's.
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
| `dataset ... is closed: it has no line in the key registry` | only the vault's owner can open it, on the server: tell the person |
| `no such dataset` | `vault_status` lists them; it goes in `dataset`, not in `path` |
| `path must be relative to the dataset` | the dataset is repeated at the head of `path`: drop it |
| `path is relative to the dataset, not absolute` | drop the leading `/` |
| `CONFLICT: expected sha ...` | re-read, reconcile, retry — never unchanged |
| `the file already exists` | `"new"` on an existing file: pass the real sha |
| `the file does not exist` | the opposite: pass `"new"` |
| `old_text NOT found` | copy the fragment exactly, spaces and newlines included |
| `old_text found N times` | widen the context until it is unique |
| `edit N of M: ...` | that change of the list failed, and none was written: fix it, resend the list |
| `nothing to edit` | the names are `old_text`/`new_text`, or a list in `edits` |
| `path not allowed` | a segment is `..`, `.git`, or a lockfile |
| `is one of git's own control files` | `.gitignore`, `.gitattributes`, `.gitmodules` are read-only from the tools: tell the person |
| `is a file, not a folder` | a segment in the middle of the path is a file: nothing can be written under it |
| `is a folder: a write needs a file path` / `not a file` | the path names a folder: add the file name |
| `cannot move ... into itself` | the destination is inside the source: move it elsewhere |
| `invalid revision` | a hash from `history`, or `HEAD~3`: never something starting with `-` |
| `destination already exists` | `move_path` never overwrites: choose another name |
| `more than 3000 files` | go one level deeper with `path`, or `depth=1`, a `pattern`, `hashes=False` |
| `from_line N is past the end` | the message says how many lines the file has |
| `the regex ran for more than 5 s` / `forbid: the patterns took more than 5 s` | a pattern with nested repetitions: simplify it, or narrow `path` |
| `is locked and cannot be dropped` / `is closed and cannot be dropped` | only an open dataset can be dropped: its line must become `name \|` first |
| `CONFLICT: expected manifest ...` | re-read the manifest, then retry |
| `block too large` | `append` is not for rewrites: `write_file` |
| `file too large` | above 2 MB, move it over SMB/scp |
| `cannot show ...: not a picture this server can open` | `view` shows pictures and PDFs: for text `read_file`, for anything else `read_binary` |
| `pages=...: the PDF has N pages` | pages count from 1; `pdf_text` with no `pages` says how many |
| `the PDF cannot be opened` | damaged, or protected by a password: tell the person |
| `title: text, subtitle and badge take N pt` | the document's title line does not fit beside its `value`: shorten the subtitle by the points it says |
| `the archive would be N characters of base64, over the ...` | your own `max_chars` stopped it before it was built |
| `dataset ... is no longer there` | it was dropped while your call was in flight |
