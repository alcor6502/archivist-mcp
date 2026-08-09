# Archivist MCP — manual

## THE MODEL

The vault root holds **datasets**: top-level directories, each with its own git
repository. **Every call names its dataset explicitly, in `dataset`** — with one
exception, `dataset_create`, where the dataset does not exist yet and the
parameter is called `name`. `path` is relative to that dataset, and an **empty
path means the whole dataset**.

    dataset="Example Project", path="01 Notes/a.md"   →  that one file
    dataset="Example Project", path=""                →  the whole dataset

**Do not repeat the dataset inside the path.** `dataset="Example Project"` with
`path="Example Project/01 Notes/a.md"` is refused, and deliberately so: it is
never silently corrected, on reads any more than on writes.

The paths that come back are relative too, so a path you read out of a result is
the same string the documents in the vault use — copy it either way and it still
means what it says.

A **locked** dataset needs its key in `key` on every call; an **open** one does
not. `vault_status()` says which is which, and it is the call you make first.
If it does not answer, stop and say so — do not try the other tools.

**`key` is always the LAST parameter**, after the optional ones. Several tools
have optionals you may not know about — `search` has `regex`, `history` has `n`,
`archive` has `pattern`, `diff` has `rev_b` — so a key passed by position lands
in the wrong slot and the call fails for a reason that has nothing to do with
the key. Pass it by name and the question never arises.

Each dataset carries **its own git repository**, so every revision is local to
it: in `read_at` and `diff`, `HEAD~3` means three commits back *in that dataset*.
A dataset is born only from `dataset_create` (or by hand on the server) — writing
a path never creates one — and it becomes locked only when a line is added to the
key registry on the server, never from a tool.

## EVERY CALL, IN FULL

Names, order and defaults, exactly as the server declares them. A static check
compares this block against the code before any image is published, so it
cannot drift: if the two ever disagree, the release fails rather than the guide
lying.

    vault_status()
    reference_guide()
    dataset_create(name)
    dataset_drop(dataset, expected_manifest)
    dataset_status(dataset, key='')
    list_files(dataset, path='', key='')
    read_file(dataset, path, key='')
    append(dataset, path, text, key='')
    write_file(dataset, path, content, expected_sha256, key='')
    edit_file(dataset, path, old_text, new_text, expected_sha256, key='')
    move_path(dataset, src, dst, key='')
    search(dataset, pattern, path='', regex=False, key='')
    manifest(dataset, path='', key='')
    archive(dataset, path='', pattern='*.md', key='')
    read_binary(dataset, path, key='')
    write_binary(dataset, path, content_base64, expected_sha256, key='')
    read_at(dataset, path, rev, key='')
    history(dataset, path='', n=10, key='')
    diff(dataset, rev_a, path='', rev_b='HEAD', key='')
    dataset_restore(dataset, rev, expected_manifest, key='')
    trash_purge(dataset, before, key='')

Four defaults are worth reading twice, because each of them makes a call do
something narrower than it looks:

- **`archive` ships `pattern='*.md'`.** An archive taken without thinking about
  it contains the Markdown and nothing else — no PDFs, no images, no binaries.
  Pass `pattern='*'` when you mean everything.
- **`search` is literal unless `regex=True`.** A pattern full of `.` and `*`
  will be looked for as those characters.
- **`history` stops at `n=10`.** The eleventh commit back is not missing, it was
  not asked for.
- **`diff` puts `path` BETWEEN the two revisions.** `diff(X, a, b)` passes `b`
  as a path, silently, and diffs the wrong thing against `HEAD`.

## THE RULES

1. **Every tool returns a verdict, not a dump.** To *know*: `search`,
   `manifest`, `list_files`. To *read*: `read_file`.
2. **The sha256 is the unit of truth.** Every read gives it, every write
   demands it — and **every write hands back the new one**, ready to go
   straight into the next call. A chain needs no re-read in between:

       write_file(ds, p, text, "new")       → sha256 b946…
       edit_file(ds, p, old, new, "b946…")  → accepted, sha256 8ded…

   `append` returns it too. `move_path` does not, and does not need to: the
   content did not change, so the sha you already held still holds.
   A different sha means someone wrote after you: re-read, reconcile, retry.
   `expected_sha256="new"` for a file that does not exist yet.
3. **Nothing is deleted.** Disposal is `move_path` into `Trash/`. `trash_purge`
   removes clutter; the contents stay in git history. Its date is the date the
   file was **trashed**, not the date it was last modified — a document from
   2024 thrown away yesterday survives `before="2026-01-01"`.
4. **Every write is atomic, verified and committed.** A tool that fails leaves
   the vault exactly as it was. There is no partial write.
5. **`external_commit_first` in a result is not an error.** It means changes
   made outside the tools — over SMB, by hand — were found and committed
   separately before yours, so tool commits stay clean.

## WHICH TOOL

| You want to | Use | Sha? |
|---|---|---|
| add lines to a register or log | `append` | no |
| change a phrase or a number | `edit_file` | yes |
| rewrite the file, or create it | `write_file` | yes (`"new"` if new) |
| write a PDF or a binary | `write_binary` | yes |
| move, rename, trash | `move_path` | no |
| know whether something exists, and where | `search` | — |
| know which files exist | `list_files` | — |
| know whether a tree changed at all | `manifest` | — |
| know how big a dataset is, how dirty, how many commits | `dataset_status` | — |
| know when something changed, and get its hash | `history` | — |
| read text | `read_file` | — |
| read a PDF or a binary | `read_binary` (needs a sandbox) | — |
| read a whole tree at once | `archive` (needs a sandbox, mind `pattern`) | — |
| read how it was before | `read_at` | — |
| see what changed between two moments | `diff` | — |
| make a new, empty dataset | `dataset_create` | — |
| roll a whole dataset back to a revision | `dataset_restore` | the manifest |
| empty `Trash/` of what was thrown away before a date | `trash_purge` | — |
| destroy a dataset | `dataset_drop` | the manifest |
| which datasets exist, and which are locked | `vault_status` | — |
| this page | `reference_guide` | — |

`dataset_drop` deletes a dataset and its git repository. It demands the current
`manifest_sha256`, so it cannot be aimed at the wrong dataset by accident, and
it **refuses outright on a locked dataset**: to destroy a protected dataset you
first remove its line from the key registry on the server, by hand. That is not
an obstacle to work around, it is the point — the one irreversible tool cannot
be reached from a chat alone.

`append` needs no sha because it never touches existing bytes: no conflict is
possible, so there is nothing to protect. `edit_file` sends only the two
fragments — on an 80 KB file that is the difference between a light call and a
heavy one.

## DO NOT IMPROVISE

These are the wrong turns actually taken by people who never saw this page.
Each has a right answer that already exists.

- **Do not split a file into parts to get past a size limit.** Above 2 MB this
  vault is not the transport: the file moves over SMB or scp, and the tools
  are told about it afterwards. A file reassembled from chunks is a file whose
  sha nobody can vouch for.
- **Do not read a directory file by file to audit it.** `archive` once — with
  `pattern='*'` if the audit has to see everything, since the default is
  Markdown only — or `manifest` if all you need to know is whether anything
  changed.
- **Do not read files looking for something.** `search` first, then `read_file`
  on the one file that matched. `search` reads text only: a PDF that does not
  come up is not missing — find binaries by name with `list_files`.
- **Do not rewrite a file to add a line.** `append`.
- **Do not rewrite a file to change a number.** `edit_file`.
- **Do not try to delete.** `move_path` into `Trash/`.
- **Do not try to copy across datasets in one call.** `move_path` takes one
  `dataset` and moves inside it; there is no second dataset to move to.
  `read_file` in one, `write_file` in the other.
- **Do not put the dataset name at the head of `path`.** It is refused, and the
  message names the prefix to drop. The dataset travels in `dataset`, once.
- **Do not retry a `CONFLICT` unchanged.** The refusal is information: someone
  wrote after you read. Re-read, reconcile, then retry.
- **Do not paste a whole file into `edit_file` as `old_text`.** If the fragment
  is not unique, widen it by a line or two — not by a page.

## LIMITS, AND THE WAY PAST EACH

| Limit | | Way past |
|---|---|---|
| text file | 2 MB | over SMB/scp; the vault is not a transport |
| binary | 2 MB | same |
| `append` block | 64 KB | it is not for rewrites: `write_file` |
| `list_files` | 3,000 files | go one level deeper with `path` |
| `search` | 200 lines | narrow `path`, or make the pattern precise |
| `diff` | 60 KB | truncates rather than failing |
| `archive` | 30 MB in, 5 MB tgz out | narrow `path` or `pattern` |
| datasets in the vault | 200 | there is no way past: it is a ceiling, not a page size |

## WHAT TO EXPECT

**A few seconds per operation**: every write takes a lock, writes atomically,
verifies and commits. That is the design working, not a fault.
Do not retry because a call felt slow — a retry after a write that did succeed
is how a duplicate gets created.

If the vault stops answering altogether, say so and stop. It is one service on
one host: no tool will succeed while another fails.

## RECIPES

Written with `X` for the dataset. `key` goes on every call to a locked one, by
name and last.

    change a number:      read_file → sha → edit_file(X, path, old, new, sha)
    add to a log:         append(X, path, block)
    create a document:    write_file(X, path, content, "new")
    archive something:    move_path(X, "doc.md", "Trash/doc.md")
    find something:       search(X, "term") → read_file on the match only
    find by expression:   search(X, "^## ", regex=True)
    full audit:           manifest → list_files → archive(X, pattern='*') → manifest
    what changed lately:  history(X, n=30) → diff(X, hash) → read_at
    compare two moments:  diff(X, "HEAD~5", path="a.md", rev_b="HEAD")
    recover content:      history → read_at(X, path, hash) → write_file
    roll a dataset back:  history → manifest → dataset_restore(X, rev, manifest)
    did anything move:    manifest before, manifest after — equal means no
    copy across datasets: read_file("A", "x.md") → write_file("B", "x.md", …, "new")
    destroy a dataset:    manifest → dataset_drop(X, manifest_sha256)

## ERRORS AND WHAT TO DO

| Message | Cure |
|---|---|
| `dataset ... is protected: a key is required` | the key is in the project's instructions |
| `no such dataset` | `vault_status` lists them; the dataset goes in `dataset`, not in `path` |
| `path must be relative to the dataset` | the dataset is repeated at the head of `path`: drop it |
| `path is relative to the dataset, not absolute` | drop the leading `/` |
| `CONFLICT: expected sha ...` | re-read, reconcile, retry |
| `the file already exists` | `"new"` on an existing file: pass the real sha |
| `the file does not exist` | the opposite: pass `"new"` |
| `old_text NOT found` | re-read and copy the fragment exactly, spaces and newlines included |
| `old_text found N times` | widen the context until it is unique |
| `path not allowed` | a segment is `..`, `.git`, or a lockfile: those names never appear in a path |
| `destination already exists` | `move_path` never overwrites: choose another name |
| `more than 3000 files` | go one level deeper with `path` |
| `too many datasets` | the vault is full at 200: nothing to widen |
| `has a key and cannot be dropped` | take its line out of the key registry on the server first |
| `CONFLICT: expected manifest ...` | someone wrote after you looked: re-read the manifest, then retry |
| `block too large` | `append` is not for rewrites: `write_file` |
| `file too large` | above 2 MB, move it over SMB/scp |
