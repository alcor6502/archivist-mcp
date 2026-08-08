# Archivist MCP — quick guide (v1.7)

## The model

The vault root holds **datasets**: top-level directories, each with its own git
repository. **Every path starts with a dataset name.** A path that is *only* the
dataset name means "the whole dataset".

```
Example Project/01 Notes/a.md   →  dataset "Example Project", file "01 Notes/a.md"
Example Project                 →  the whole dataset
```

A **locked** dataset needs its key in the `key` parameter on every call. An
**open** one does not. `vault_status()` tells you which is which.

## The five rules

1. **Every tool returns a verdict, not a dump.** To *know*, use `search`,
   `manifest`, `list_files`; to *read*, use `read_file`.
2. **The sha256 is the unit of truth.** Every read gives it, every write demands
   it: `read_file` → `sha256` → pass it back to `write_file`/`edit_file`.
   A different sha means someone wrote after you → re-read, reconcile, retry.
   New file: `expected_sha256="new"`.
3. **Nothing is deleted.** Disposal is `move_path` into `Trash/`.
   `trash_purge` removes clutter, but contents remain in git history.
4. **Every write is atomic, verified and committed.** If a tool fails, the vault
   is exactly as it was: never a partial write.
5. **`external_commit_first` in a result** means the repo was dirty and changes
   made outside the tools (SMB, by hand) were committed separately before
   yours. It is not an error.

## Which tool

| You want to | Use | Sha? |
|---|---|---|
| add lines to a register | `append` | no |
| change a phrase or a number | `edit_file` | yes |
| rewrite the file, or create it | `write_file` | yes (`"new"` if new) |
| move, rename, trash | `move_path` | no |
| know whether something exists, and where | `search` | — |
| know which files exist | `list_files` | — |
| compare two trees | `manifest` | — |
| read text | `read_file` | — |
| read a PDF or binary | `read_binary` (needs a sandbox) | — |
| read many at once | `archive` (needs a sandbox) | — |
| read how it was before | `read_at` | — |

`append` needs no sha because it never touches existing bytes: no conflict is
possible, so there is nothing to protect. It is the right operation for logs
and registers. `edit_file` sends only the two fragments, not the file — on an
80 KB file that is the difference between a light call and a heavy one.

## Syntax

Each tool's parameters are already in its description, which you have in front
of you: they are not repeated here. What is worth knowing beyond that is that
**`path` always starts with a dataset name**, and `key` is only needed for
locked datasets.

## Limits

text 2 MB · binaries 2 MB · `append` 64 KB · `list_files` 3,000 files ·
`search` 200 lines · `diff` 60 KB (truncates) · `archive` 30 MB in, 5 MB tgz out.

## Recipes

```
change a number:      read_file → sha → edit_file(path, old, new, sha)
add to a log:         append(path, line)
create a document:    write_file(path, content, "new")
archive something:    move_path("X/doc.md", "X/Trash/doc.md")
find something:       search("term", "X") → then read_file on the right file only
full audit:           manifest → list_files → archive → verify hashes → manifest
recover content:      history → read_at(path, hash) → write_file
compare two moments:  manifest before, manifest after — equal means nothing moved
```

## Errors and what to do

| Message | Cure |
|---|---|
| `dataset ... is protected: a key is required` | the key is in the project's instructions |
| `no such dataset` | `vault_status` lists them; the first path segment is the dataset |
| `CONFLICT: expected sha ...` | re-read, reconcile, retry |
| `the file already exists` | you used `"new"` on an existing file: pass the real sha |
| `the file does not exist: ... "new"` | the opposite |
| `old_text NOT found` | re-read and copy the exact fragment (spaces and newlines) |
| `old_text found N times` | widen the context until it is unique |
| `path not allowed` | there is a `..` or `.git` in the path |
| `destination already exists` | `move_path` never overwrites |
| `more than 3000 files` | go one level deeper with the path |
| `tgz is ... bytes (max ...)` | narrow `path` or `pattern` |
| `block too large (max 64000)` | `append` is not for rewrites: use `write_file` |
| `file too large (max 2000000)` | above 2 MB, move it over SMB/scp |

A failing tool never leaves a partial write.
