# Archivist MCP — manual

## THE MODEL

The vault root holds **datasets**: top-level directories, each with its own git
repository. **Every path starts with a dataset name.** A path that is *only* a
dataset name means the whole dataset.

    Example Project/01 Notes/a.md   →  dataset "Example Project", file "01 Notes/a.md"
    Example Project                 →  the whole dataset

A **locked** dataset needs its key in `key` on every call; an **open** one does
not. `vault_status()` says which is which, and it is the call you make first.
If it does not answer, stop and say so — do not try the other tools.

Each dataset carries **its own git repository**, so every revision is local to
it: in `read_at` and `diff`, `HEAD~3` means three commits back *in that dataset*.
A dataset is born only from `dataset_create` (or by hand on the server) — writing
a path never creates one — and it becomes locked only when a line is added to the
key registry on the server, never from a tool.

## THE FIVE RULES

1. **Every tool returns a verdict, not a dump.** To *know*: `search`,
   `manifest`, `list_files`. To *read*: `read_file`.
2. **The sha256 is the unit of truth.** Every read gives it, every write
   demands it: `read_file` → `sha256` → back into `write_file`/`edit_file`.
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
| move, rename, trash | `move_path` | no |
| know whether something exists, and where | `search` | — |
| know which files exist | `list_files` | — |
| know whether a tree changed at all | `manifest` | — |
| read text | `read_file` | — |
| read a PDF or a binary | `read_binary` (needs a sandbox) | — |
| read a whole tree at once | `archive` (needs a sandbox) | — |
| read how it was before | `read_at` | — |
| see what changed between two moments | `diff` | — |

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
- **Do not read a directory file by file to audit it.** `archive` once, or
  `manifest` if all you need to know is whether anything changed.
- **Do not read files looking for something.** `search` first, then `read_file`
  on the one file that matched. `search` reads text only: a PDF that does not
  come up is not missing — find binaries by name with `list_files`.
- **Do not rewrite a file to add a line.** `append`.
- **Do not rewrite a file to change a number.** `edit_file`.
- **Do not try to delete.** `move_path` into `Trash/`.
- **Do not copy across datasets with `move_path`** — it refuses by design.
  `read_file` in one, `write_file` in the other.
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

## WHAT TO EXPECT

Roughly **3 to 4 seconds per operation**: every write takes a lock, writes
atomically, verifies and commits. That is the design working, not a fault.
Do not retry because a call felt slow — a retry after a write that did succeed
is how a duplicate gets created.

If the vault stops answering altogether, say so and stop. It is one service on
one host: no tool will succeed while another fails.

## RECIPES

    change a number:      read_file → sha → edit_file(path, old, new, sha)
    add to a log:         append(path, block)
    create a document:    write_file(path, content, "new")
    archive something:    move_path("X/doc.md", "X/Trash/doc.md")
    find something:       search("term", "X") → read_file on the match only
    full audit:           manifest → list_files → archive → verify → manifest
    recover content:      history → read_at(path, hash) → write_file
    roll a dataset back:  history → manifest → dataset_restore(ds, rev, manifest)
    did anything move:    manifest before, manifest after — equal means no
    copy across datasets: read_file("A/x.md") → write_file("B/x.md", …, "new")

## ERRORS AND WHAT TO DO

| Message | Cure |
|---|---|
| `dataset ... is protected: a key is required` | the key is in the project's instructions |
| `no such dataset` | `vault_status` lists them; the first path segment is the dataset |
| `CONFLICT: expected sha ...` | re-read, reconcile, retry |
| `the file already exists` | `"new"` on an existing file: pass the real sha |
| `the file does not exist` | the opposite: pass `"new"` |
| `old_text NOT found` | re-read and copy the fragment exactly, spaces and newlines included |
| `old_text found N times` | widen the context until it is unique |
| `path not allowed` | there is a `..` or a `.git` in the path |
| `destination already exists` | `move_path` never overwrites: choose another name |
| `more than 3000 files` | go one level deeper with `path` |
| `block too large` | `append` is not for rewrites: `write_file` |
| `file too large` | above 2 MB, move it over SMB/scp |
