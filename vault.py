"""
vault.py — storage operations, v2 (datasets as a field + keys).

The model
---------
- The vault root holds no working files: it holds DATASETS. A dataset is a
  top-level directory with its OWN git repository.
- The dataset is NAMED EXPLICITLY on every call, in its own `dataset`
  argument; `path` is relative to it, and an empty path means "the whole
  dataset". There is no root-level operation at all: the entire protection
  model follows from that single rule, which up to v1.8 was expressed by
  making the dataset the first segment of the path.
- A dataset listed in keys.txt is LOCKED and every call must carry its key.
  Without a line it is OPEN.
- keys.txt lives in the vault root and is unreachable BY CONSTRUCTION: it is
  not a dataset, so it cannot be named in `dataset`, and as a `path` it is
  only ever looked for INSIDE a dataset. It is not even expressible.

Invariants
----------
- every tool returns a VERDICT, not a DUMP;
- append is atomic and needs no sha: it never touches existing bytes;
- write/edit use compare-and-swap on the expected sha;
- listings carry per-file hashes;
- git history is queryable server-side;
- files are never deleted: the only disposal is a move into Trash/.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import os
import re as _re
import subprocess
import tarfile
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

MAX_READ_BYTES = 2_000_000
MAX_WRITE_BYTES = 2_000_000
MAX_LIST_FILES = 3_000
MAX_DIFF_BYTES = 60_000
MAX_BINARY_BYTES = 2_000_000       # read/write binary: above this, use SMB/scp
MAX_ARCHIVE_BYTES = 30_000_000     # sum of files packed by archive() (input)
MAX_ARCHIVE_OUT_BYTES = 5_000_000  # cap on the PRODUCED tgz: that is what crosses the context
MAX_SEARCH_HITS = 200
MAX_APPEND_BYTES = 64_000          # largest block for append()
MAX_DATASETS = 200

KEYS_BASENAME = "keys.txt"
TRASH = "Trash"
LOCKFILE = ".archivist.lock"
# Root-level lock, for create and drop only. Dataset writes keep their own
# per-dataset lock: datasets are independent repositories, so queueing a write
# on one behind a write on another would be a cost with nothing bought. Like
# keys.txt, this file is unreachable from the tools — its name is not a dataset
# name, so it cannot appear in `dataset`, and inside a dataset it does not exist.
ROOT_LOCKFILE = ".archivist-root.lock"

# Dataset names: letters, digits, space, dash, underscore and INNER dots.
# The first character cannot be '.' (invisible over SMB from macOS) nor '_'
# (reserved for future use).
_NAME_OK = _re.compile(r"^[A-Za-z0-9\u00C0-\u024F][A-Za-z0-9 ._\-\u00C0-\u024F]{0,62}$")


class VaultError(Exception):
    """A readable error, returned to the client as the tool error text."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s or "").strip()


def _fold(s: str) -> str:
    """Comparison form for collisions: NFC + casefold. 'Scratch' and 'scratch'
    are the same dataset and must not be able to coexist."""
    return _norm(s).casefold()


# =====================================================================
#  VaultRoot — the root: datasets, keys, path resolution
# =====================================================================

class VaultRoot:
    def __init__(self, root: str, keys_file: str | None = None) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise VaultError(f"vault root does not exist: {self.root}")
        self.keys_file = Path(keys_file).resolve() if keys_file else (self.root / KEYS_BASENAME)
        self._keys_cache: dict[str, str] = {}
        self._keys_mtime: float = -1.0
        self._lockfile = self.root / ROOT_LOCKFILE

    def _lock(self):
        """Exclusive lock on the vault root. Taken by create and drop, which
        are the only operations that add or remove a top-level directory and
        therefore the only ones no per-dataset lock can protect."""
        fh = open(self._lockfile, "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    # ---------- datasets ----------

    def dataset_names(self) -> list[str]:
        """Top-level directories, sorted. Not stray files (so never keys.txt),
        not directories starting with '.' or '_'."""
        out = []
        for p in sorted(self.root.iterdir()):
            if not p.is_dir():
                continue
            if p.name.startswith(".") or p.name.startswith("_"):
                continue
            out.append(p.name)
        return out

    def exists(self, name: str) -> bool:
        return _fold(name) in {_fold(n) for n in self.dataset_names()}

    def _actual_name(self, name: str) -> str | None:
        """The real on-disk name matching `name`, compared in folded form:
        a caller may write 'scratch' meaning 'Scratch'."""
        f = _fold(name)
        for n in self.dataset_names():
            if _fold(n) == f:
                return n
        return None

    # ---------- keys ----------

    def keys(self) -> dict[str, str]:
        """Read keys.txt, hot-reloading on mtime change, so a dataset can be
        protected or opened from a file manager without restarting anything.

        Format: `dataset name` TAB `key`, one line per dataset. Blank lines and
        lines starting with '#' are comments.
        """
        try:
            m = self.keys_file.stat().st_mtime
        except OSError:
            self._keys_cache, self._keys_mtime = {}, -1.0
            return {}
        if m == self._keys_mtime:
            return self._keys_cache
        out: dict[str, str] = {}
        try:
            text = self.keys_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise VaultError(f"key registry unreadable ({self.keys_file}): {e}")
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "\t" in line:
                name, key = line.split("\t", 1)
            else:  # tolerated: two or more spaces instead of a tab
                parts = _re.split(r"\s{2,}", line.strip(), maxsplit=1)
                if len(parts) != 2:
                    continue
                name, key = parts
            name, key = _norm(name), key.strip()
            if name and key:
                out[_fold(name)] = key
        self._keys_cache, self._keys_mtime = out, m
        return out

    def is_locked(self, name: str) -> bool:
        return _fold(name) in self.keys()

    def check_key(self, name: str, key: str) -> None:
        """Raise if the dataset is locked and the key does not match. The
        message reveals nothing about the correct key."""
        expected = self.keys().get(_fold(name))
        if expected is None:
            return  # open dataset
        if not key:
            raise VaultError(
                f"dataset {name!r} is protected: a key is required. "
                "You will find it in the instructions of the project it belongs to.")
        if key != expected:
            raise VaultError(f"wrong key for dataset {name!r}.")

    # ---------- resolution ----------

    def resolve_dataset(self, dataset: str) -> str:
        """The on-disk name of an existing dataset, or a readable refusal.

        This is the whole root-level surface: nothing that is not a dataset can
        be named here, keys.txt and the lockfiles included."""
        name = _norm(dataset)
        if not name:
            raise VaultError(
                "dataset is required: name it explicitly "
                "(vault_status lists the available ones)")
        real = self._actual_name(name)
        if real is None:
            if _fold(name) == _fold(self.keys_file.name):
                raise VaultError("the key registry is not accessible from the tools.")
            raise VaultError(
                f"no such dataset: {name!r}. Name it in `dataset` — see vault_status.")
        return real

    def check_path(self, dataset: str, path: str) -> str:
        """Validate a DATASET-RELATIVE path and hand it back normalised.
        An empty path means the whole dataset.

        `dataset` is the real name, as returned by resolve_dataset.

        Three refusals, and the third is the reason this version exists. A path
        whose first segment repeats the dataset name is the shape a caller
        written against v1.8 produces, and it is REFUSED rather than quietly
        stripped. Silence would be worse than the error: normalising here would
        teach the wrong form without ever complaining, and it would do so on
        reads as much as on writes.

        The refusal fires ONLY on the declared dataset, never on "any existing
        dataset name": a folder inside a dataset may legitimately be called like
        another dataset, and the day it is, a wider check would refuse a correct
        path. That is the same failure already paid for in v1.7, when a
        placeholder check taught that widening what a guard recognises is exactly
        where it starts refusing what must pass.
        """
        p = _norm(path)
        if not p or p == ".":
            return ""
        if p.startswith("/"):
            raise VaultError(
                f"path is relative to the dataset, not absolute: {path!r}")
        parts = Path(p).parts
        # The same check exists in Dataset._resolve, the choke point of every
        # operation. Here it is hoisted: the guarantee then holds already at
        # resolution time, and no future tool can bypass it by oversight.
        if any(x in ("..", ".git") for x in parts):
            raise VaultError(f"path not allowed: {path!r}")
        if _fold(parts[0]) == _fold(dataset):
            raise VaultError(
                f"path must be relative to the dataset: drop the leading {parts[0] + '/'!r}")
        return "/".join(parts)

    def open(self, dataset: str, path: str = "", key: str = "") -> tuple["Dataset", str]:
        """(unlocked dataset, validated relative path) — the one door every
        path-bearing tool goes through, reads included."""
        name = self.resolve_dataset(dataset)
        self.check_key(name, key)
        return Dataset(self.root / name, name), self.check_path(name, path)

    def open_by_name(self, dataset: str, key: str = "") -> "Dataset":
        """The same, for the tools that act on a whole dataset."""
        name = self.resolve_dataset(dataset)
        self.check_key(name, key)
        return Dataset(self.root / name, name)

    # ---------- status ----------

    def status(self, version: str) -> dict:
        """Minimal verdict: the vault answers, and who lives in it. Nothing
        else — this is the first call anyone makes and it must cost almost
        nothing."""
        keys = self.keys()
        return {
            "vault": "ok",
            "version": version,
            "guide": "call reference_guide() for the manual",
            "datasets": [
                {"name": n, "state": "locked" if _fold(n) in keys else "open"}
                for n in self.dataset_names()
            ],
        }

    # ---------- create and drop ----------

    def create(self, name: str) -> dict:
        """Create an OPEN, empty dataset with git init and a first commit.
        No password: an empty box is not a capability. If something worth
        protecting ends up in it, add a line to keys.txt."""
        n = _norm(name)
        if not _NAME_OK.match(n):
            raise VaultError(
                "invalid name. Allowed: letters, digits, space, '.', '_' and '-', "
                "1 to 63 characters; the first character cannot be '.' (invisible "
                "over SMB) nor '_' (reserved).")
        if _fold(n) == _fold(self.keys_file.name):
            raise VaultError("reserved name.")
        # Everything that reads or writes the set of datasets happens under the
        # root lock: without it, "check that it does not exist" and "create it"
        # are two steps with a gap in between, and the gap is the bug.
        with self._lock():
            if self.exists(n):
                real = self._actual_name(n)
                raise VaultError(f"dataset {real!r} already exists (names are case-insensitive).")
            if len(self.dataset_names()) >= MAX_DATASETS:
                raise VaultError(f"too many datasets (max {MAX_DATASETS}).")
            d = self.root / n
            try:
                d.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                # The lock rules out another create, but not a directory that
                # appeared over SMB a moment ago. Belt and braces, and above all
                # a readable message instead of a raw traceback.
                raise VaultError(
                    f"dataset {n!r} already exists (names are case-insensitive).") from None
            try:
                os.chmod(d, 0o777)
            except OSError:
                pass
            ds = Dataset(d, n)
            result = ds.ensure_git()
        return {"dataset": n, "state": "open", "git": result,
                "note": "open: add a line to the key registry to protect it"}

    def drop(self, name: str, expected_manifest: str) -> dict:
        """Delete an OPEN dataset. A dataset with a key is NOT droppable: to
        remove it, first take its line out of keys.txt (a manual act on the
        server) or delete it over SMB.

        expected_manifest is the current manifest_sha256: you cannot throw away
        what you have not looked at, and if someone wrote in the meantime the
        drop is refused."""
        import shutil
        with self._lock():
            n = self._actual_name(name)
            if n is None:
                raise VaultError(f"no such dataset: {name!r}")
            if self.is_locked(n):
                raise VaultError(
                    f"dataset {n!r} has a key and cannot be dropped. "
                    "To remove it: take its line out of the key registry (it then "
                    "becomes open), or delete it directly on the server.")
            ds = Dataset(self.root / n, n)
            current = ds.manifest("")["manifest_sha256"]
            if expected_manifest != current:
                raise VaultError(
                    f"CONFLICT: expected manifest {expected_manifest[:12]}... but the dataset "
                    f"is {current[:12]}... Re-read the manifest and retry — someone wrote "
                    "after you looked at it.")
            n_files = ds.manifest("")["file_count"]
            # The root lock alone would NOT be enough: a write in flight holds
            # the DATASET lock, not this one, so rmtree and the write would
            # still tread on each other. Both locks, always in this order —
            # root then dataset — so no two callers can take them in opposite
            # order and deadlock.
            #
            # What stays open, honestly: rmtree removes the lockfile too, so a
            # write ARRIVING afterwards finds no directory and fails. This
            # closes the window on a write already in progress; it does not
            # make "drop" and "start writing" atomic. That would take a global
            # lock on every write — the very thing the dataset design rejected.
            # drop only works on open datasets, which are ephemeral by design.
            with ds._lock():
                shutil.rmtree(self.root / n)
        return {"dropped": n, "files_removed": n_files,
                "note": "deleted. The ZFS snapshot of the vault is the only remaining net."}

    # ---------- boot-time maintenance ----------

    def boot(self, retention_months: int = 0) -> list[str]:
        """Adoption plus pruning, once at startup. Every top-level directory
        without a .git gets git init and a first commit: this is how a dataset
        created by hand (file manager, SMB) enters service on its own."""
        lines = []
        for n in self.dataset_names():
            ds = Dataset(self.root / n, n)
            try:
                lines.append(f"{n}: {ds.ensure_git()}")
            except VaultError as e:
                lines.append(f"{n}: GIT ERROR — {e}")
                continue
            if retention_months and retention_months > 0:
                try:
                    lines.append(f"{n}: {ds.prune_history(retention_months)}")
                except VaultError as e:
                    # pruning must NEVER prevent startup
                    lines.append(f"{n}: pruning skipped — {e}")
        return lines


# =====================================================================
#  Dataset — every operation, inside one dataset
# =====================================================================

class Dataset:
    def __init__(self, root: Path, name: str) -> None:
        self.root = Path(root).resolve()
        self.name = name
        self._lockfile = self.root / LOCKFILE

    # ---------- helpers ----------

    def _rel(self, p: Path | str) -> str:
        """A path as the client sees it: relative to the dataset, '/' separated,
        empty for the dataset root. It is the same form the documents in the
        vault use, which is the point of the whole thing: a path copied out of a
        result and pasted into a document now means what it says.

        Takes either an absolute path inside the dataset or an already relative
        one."""
        q = Path(p)
        if q.is_absolute():
            q = q.relative_to(self.root)
        r = str(q).replace(os.sep, "/")
        return "" if r == "." else r

    def _resolve(self, rel: str, *, must_exist: bool) -> Path:
        """Dataset-relative to absolute, ALWAYS inside the dataset: no
        traversal, no symlink escaping, no .git."""
        rel = _norm(rel).lstrip("/")
        if not rel or rel == ".":
            p = self.root
        else:
            if any(part in ("..", ".git") for part in Path(rel).parts):
                raise VaultError(f"path not allowed: {rel!r}")
            p = self.root / rel
        rp = p.resolve() if p.exists() else p.parent.resolve() / p.name
        if not str(rp).startswith(str(self.root) + os.sep) and rp != self.root:
            raise VaultError(f"path outside the dataset: {rel!r}")
        if must_exist and not rp.exists():
            raise VaultError(f"no such file: {self._rel(rel)!r} in dataset {self.name!r}")
        return rp

    def _skip(self, p: Path) -> bool:
        """The single exclusion rule. Every counting or listing operation goes
        through here, so they can never disagree on what counts as a file."""
        return (not p.is_file()) or ".git" in p.parts or p.name == LOCKFILE

    def _read_bytes(self, p: Path) -> bytes:
        size = p.stat().st_size
        if size > MAX_READ_BYTES:
            raise VaultError(f"file too large ({size} bytes, max {MAX_READ_BYTES})")
        data = p.read_bytes()
        if len(data) != size:
            raise VaultError(f"read {len(data)} bytes, {size} declared: incomplete read, stopping")
        return data

    def _lock(self):
        fh = open(self._lockfile, "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def _git(self, *args: str, check: bool = True, timeout: int = 60) -> str:
        r = subprocess.run(["git", "-C", str(self.root), *args],
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise VaultError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:400]}")
        return r.stdout

    def ensure_git(self) -> str:
        if not (self.root / ".git").is_dir():
            self._git("init", "-q")
            self._git("config", "user.name", "archivist-mcp")
            self._git("config", "user.email", "archivist-mcp@localhost")
            gi = self.root / ".gitignore"
            if not gi.exists():
                gi.write_text(f".DS_Store\n{LOCKFILE}\n", encoding="utf-8")
            self._git("add", "-A")
            self._git("commit", "-q", "--allow-empty", "-m", "archivist-mcp: initial commit")
            return "git repository created, initial commit done"
        self._git("config", "user.name", "archivist-mcp")
        self._git("config", "user.email", "archivist-mcp@localhost")
        if self._git("status", "--porcelain").strip():
            self._git("add", "-A")
            self._git("commit", "-q", "-m", "external: changes made while the server was down")
            return "repository present; external changes committed at boot"
        return "git repository present"

    def _commit_external_if_dirty(self) -> str | None:
        """If the repo is dirty BEFORE a tool operation, those changes came
        from outside (SMB, by hand). They get their OWN commit, with an honest
        message: tool commits stay pure and the history never lies."""
        if self._git("status", "--porcelain").strip():
            self._git("add", "-A")
            self._git("commit", "-q", "-m", "external: changes made outside the tools (SMB/manual)")
            return self._git("rev-parse", "--short", "HEAD").strip()
        return None

    def _commit(self, message: str) -> str:
        self._git("add", "-A")
        if not self._git("status", "--porcelain").strip():
            return "(nothing to commit)"
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "--short", "HEAD").strip()

    @staticmethod
    def _check_rev(rev: str) -> None:
        if not rev or not all(c.isalnum() or c in "~^-_." for c in rev) or len(rev) > 40:
            raise VaultError(f"invalid revision: {rev!r}")

    def _git_size(self) -> int:
        total = 0
        g = self.root / ".git"
        if g.is_dir():
            for p in g.rglob("*"):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except OSError:
                    pass
        return total

    def _atomic_write(self, p: Path, data: bytes) -> bytes:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".archivist-tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
            try:
                os.chmod(p, 0o666)   # mkstemp creates 0600, ignoring the umask
            except OSError:
                pass
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        back = p.read_bytes()
        if _sha(back) != _sha(data):
            raise VaultError("post-write verification failed: read-back differs from what was written")
        return back

    # ---------- status ----------

    def status(self) -> dict:
        md = sum(1 for p in self.root.rglob("*.md") if not self._skip(p))
        total = sum(1 for p in self.root.rglob("*") if not self._skip(p))
        dirty = self._git("status", "--porcelain").strip()
        last = self._git("log", "-1", "--format=%h %cI %s", check=False).strip() or "(no commits)"
        n_commits = self._git("rev-list", "--count", "HEAD", check=False).strip() or "0"
        trash = self.root / TRASH
        n_trash = sum(1 for p in trash.rglob("*") if not self._skip(p)) if trash.is_dir() else 0
        return {
            "dataset": self.name,
            "total_files": total,
            "md_files": md,
            "files_in_trash": n_trash,
            "git": "clean" if not dirty else f"UNCOMMITTED: {len(dirty.splitlines())} files",
            "total_commits": int(n_commits),
            "git_size_bytes": self._git_size(),
            "last_commit": last,
        }

    # ---------- reading ----------

    def list_files(self, rel: str = "") -> dict:
        base = self._resolve(rel, must_exist=True)
        if base.is_file():
            data = self._read_bytes(base)
            return {"dataset": self.name, "file": self._rel(base),
                    "size": len(data), "sha256": _sha(data)}
        out, n = [], 0
        for p in sorted(base.rglob("*")):
            if self._skip(p):
                continue
            n += 1
            if n > MAX_LIST_FILES:
                raise VaultError(f"more than {MAX_LIST_FILES} files: narrow the path")
            try:
                data = self._read_bytes(p)
                out.append({"path": self._rel(p), "size": len(data), "sha256": _sha(data)})
            except VaultError as e:
                out.append({"path": self._rel(p), "error": str(e)})
        return {"dataset": self.name, "base": self._rel(base),
                "count": len(out), "files": out}

    def read_file(self, rel: str) -> dict:
        p = self._resolve(rel, must_exist=True)
        if not p.is_file():
            raise VaultError(f"not a file: {self._rel(rel)!r}")
        data = self._read_bytes(p)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise VaultError("not UTF-8 (binary?): read_file is for text only — use read_binary")
        return {"dataset": self.name, "path": self._rel(p), "size": len(data),
                "sha256": _sha(data), "content": text}

    def read_binary(self, rel: str) -> dict:
        p = self._resolve(rel, must_exist=True)
        if not p.is_file():
            raise VaultError(f"not a file: {self._rel(rel)!r}")
        size = p.stat().st_size
        if size > MAX_BINARY_BYTES:
            raise VaultError(f"file too large ({size} bytes, max {MAX_BINARY_BYTES}): use SMB/scp")
        data = p.read_bytes()
        if len(data) != size:
            raise VaultError(f"read {len(data)} bytes, {size} declared: incomplete read, stopping")
        return {"dataset": self.name, "path": self._rel(p), "size": len(data),
                "sha256": _sha(data), "content_base64": base64.b64encode(data).decode("ascii")}

    def read_at(self, rel: str, rev: str) -> dict:
        self._check_rev(rev)
        p = self._resolve(rel, must_exist=False)
        r = str(p.relative_to(self.root))
        out = subprocess.run(["git", "-C", str(self.root), "show", f"{rev}:{r}"],
                             capture_output=True, timeout=30)
        if out.returncode != 0:
            raise VaultError(f"git show failed: {out.stderr.decode(errors='replace').strip()[:300]}")
        data = out.stdout
        if len(data) > MAX_READ_BYTES:
            raise VaultError(f"file too large at that revision ({len(data)} bytes)")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise VaultError("not UTF-8 at that revision: read_at is for text only")
        return {"dataset": self.name, "path": self._rel(r), "rev": rev, "size": len(data),
                "sha256": _sha(data), "content": text}

    # ---------- writing ----------

    def append(self, rel: str, text: str) -> dict:
        if not text.strip():
            raise VaultError("empty text: nothing to append")
        if len(text.encode("utf-8")) > MAX_APPEND_BYTES:
            raise VaultError(f"block too large (max {MAX_APPEND_BYTES} bytes): use write_file to rewrite")
        p = self._resolve(rel, must_exist=True)
        with self._lock():
            external = self._commit_external_if_dirty()
            data = self._read_bytes(p)
            add = ("" if (not data or data.endswith(b"\n")) else "\n") + text.rstrip("\n") + "\n"
            with open(p, "ab") as fh:
                fh.write(add.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            new = self._read_bytes(p)
            if not new.endswith(add.encode("utf-8")):
                raise VaultError("post-append verification failed: the file does not end with the block written")
            commit = self._commit(f"append: {p.relative_to(self.root)}")
        return {"dataset": self.name, "path": self._rel(p), "size": len(new),
                "sha256": _sha(new), "commit": commit,
                **({"external_commit_first": external} if external else {})}

    def write_file(self, rel: str, content: str, expected_sha256: str) -> dict:
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise VaultError(f"content too large ({len(data)} bytes, max {MAX_WRITE_BYTES})")
        return self._write_bytes(rel, data, expected_sha256, "write")

    def write_binary(self, rel: str, content_base64: str, expected_sha256: str) -> dict:
        try:
            data = base64.b64decode(content_base64, validate=True)
        except Exception:
            raise VaultError("content_base64 is not valid base64")
        if len(data) > MAX_BINARY_BYTES:
            raise VaultError(f"content too large ({len(data)} bytes, max {MAX_BINARY_BYTES})")
        return self._write_bytes(rel, data, expected_sha256, "write-binary")

    def _write_bytes(self, rel: str, data: bytes, expected_sha256: str, label: str) -> dict:
        p = self._resolve(rel, must_exist=False)
        with self._lock():
            external = self._commit_external_if_dirty()
            if p.exists():
                cur = _sha(p.read_bytes())
                if expected_sha256 == "new":
                    raise VaultError(f"the file already exists (sha {cur[:12]}...): re-read it and pass its sha")
                if cur != expected_sha256:
                    raise VaultError(
                        f"CONFLICT: expected sha {expected_sha256[:12]}... but the file is {cur[:12]}... "
                        "Someone wrote after you read it: re-read, reconcile, retry.")
            elif expected_sha256 != "new":
                raise VaultError('the file does not exist: to create it, expected_sha256 must be "new"')
            back = self._atomic_write(p, data)
            commit = self._commit(f"{label}: {p.relative_to(self.root)}")
        return {"dataset": self.name, "path": self._rel(p), "size": len(back),
                "sha256": _sha(back), "commit": commit,
                **({"external_commit_first": external} if external else {})}

    def edit_file(self, rel: str, old_text: str, new_text: str, expected_sha256: str) -> dict:
        if old_text == new_text:
            raise VaultError("old_text and new_text are identical: nothing to do")
        if not old_text:
            raise VaultError("old_text is empty: to create content use write_file or append")
        p = self._resolve(rel, must_exist=True)
        with self._lock():
            external = self._commit_external_if_dirty()
            data = self._read_bytes(p)
            cur = _sha(data)
            if cur != expected_sha256:
                raise VaultError(
                    f"CONFLICT: expected sha {expected_sha256[:12]}... but the file is {cur[:12]}... "
                    "Someone wrote after you read it: re-read, reconcile, retry.")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise VaultError("not UTF-8: edit_file is for text only")
            n = text.count(old_text)
            if n == 0:
                raise VaultError("old_text NOT found in the file: re-read it and copy the exact fragment")
            if n > 1:
                raise VaultError(f"old_text found {n} times: ambiguous — widen the context until it is unique")
            out = text.replace(old_text, new_text, 1).encode("utf-8")
            if len(out) > MAX_WRITE_BYTES:
                raise VaultError(f"result too large ({len(out)} bytes)")
            back = self._atomic_write(p, out)
            if new_text.encode("utf-8") not in back:
                raise VaultError("post-edit verification failed: the new text is not in the file")
            commit = self._commit(f"edit: {p.relative_to(self.root)}")
        return {"dataset": self.name, "path": self._rel(p), "size": len(back),
                "sha256": _sha(back), "commit": commit,
                **({"external_commit_first": external} if external else {})}

    def move_path(self, src: str, dst: str) -> dict:
        s = self._resolve(src, must_exist=True)
        d = self._resolve(dst, must_exist=False)
        with self._lock():
            external = self._commit_external_if_dirty()
            if d.exists():
                raise VaultError(f"destination already exists: {self._rel(dst)!r} (never overwrites)")
            d.parent.mkdir(parents=True, exist_ok=True)
            os.replace(s, d)
            # When the destination is inside Trash/, mtime becomes "trashed at":
            # it is the field trash_purge later works on. The mtime of a trashed
            # file serves no other purpose, so the slot is free to reuse.
            in_trash = TRASH in d.relative_to(self.root).parts
            if in_trash:
                now = datetime.now(timezone.utc).timestamp()
                try:
                    if d.is_dir():
                        for q in d.rglob("*"):
                            if q.is_file():
                                os.utime(q, (now, now))
                    else:
                        os.utime(d, (now, now))
                except OSError:
                    pass
            commit = self._commit(f"move: {s.relative_to(self.root)} -> {d.relative_to(self.root)}")
        return {"dataset": self.name, "from": self._rel(s), "to": self._rel(d),
                "trashed": in_trash, "commit": commit,
                **({"external_commit_first": external} if external else {})}

    # ---------- querying ----------

    def search(self, pattern: str, rel: str = "", regex: bool = False) -> dict:
        if not pattern or len(pattern) > 500:
            raise VaultError("pattern empty or too long")
        base = self._resolve(rel, must_exist=True)
        rx = None
        if regex:
            try:
                rx = _re.compile(pattern)
            except _re.error as e:
                raise VaultError(f"invalid regex: {e}")
        hits, scanned, truncated = [], 0, False
        targets = [base] if base.is_file() else sorted(base.rglob("*"))
        for p in targets:
            if self._skip(p):
                continue
            try:
                text = self._read_bytes(p).decode("utf-8")
            except (VaultError, UnicodeDecodeError):
                continue  # binaries and oversized files: findable by name only
            scanned += 1
            for i, line in enumerate(text.splitlines(), 1):
                ok = bool(rx.search(line)) if rx else (pattern in line)
                if ok:
                    if len(hits) >= MAX_SEARCH_HITS:
                        truncated = True
                        break
                    hits.append(f"{self._rel(p)}:{i}: {line.strip()[:200]}")
            if truncated:
                break
        return {"dataset": self.name, "pattern": pattern, "files_scanned": scanned,
                "matches": len(hits), "truncated": truncated, "lines": hits}

    def manifest(self, rel: str = "") -> dict:
        # The lines that go into the fingerprint carry dataset-relative paths, so
        # the same tree yields a DIFFERENT manifest_sha256 than it did in v1.8.
        # Nothing stores a manifest between calls — it is read and handed back
        # within the same exchange — so the change costs nothing; it is written
        # here because a fingerprint that silently changes meaning is exactly the
        # kind of thing that is noticed six months later.
        base = self._resolve(rel, must_exist=True)
        lines, total = [], 0
        for p in sorted(base.rglob("*") if base.is_dir() else [base], key=lambda x: str(x)):
            if self._skip(p):
                continue
            size = p.stat().st_size
            h = _sha(self._read_bytes(p)) if size <= MAX_READ_BYTES else "OVERSIZE"
            total += size
            lines.append(f"{h}  {self._rel(p)}")
        blob = "\n".join(lines).encode("utf-8")
        return {"dataset": self.name, "base": self._rel(base),
                "file_count": len(lines), "total_bytes": total,
                "manifest_sha256": _sha(blob)}

    def archive(self, rel: str = "", pattern: str = "*.md") -> dict:
        base = self._resolve(rel, must_exist=True)
        buf = io.BytesIO()
        n, total = 0, 0
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for p in sorted(base.rglob(pattern) if base.is_dir() else [base]):
                if self._skip(p):
                    continue
                data = self._read_bytes(p)
                total += len(data)
                if total > MAX_ARCHIVE_BYTES:
                    raise VaultError(f"archive over {MAX_ARCHIVE_BYTES} bytes: narrow path or pattern")
                info = tarfile.TarInfo(name=self._rel(p))
                info.size = len(data)
                info.mtime = int(p.stat().st_mtime)
                tar.addfile(info, io.BytesIO(data))
                n += 1
        if n == 0:
            raise VaultError(
                f"no files matching {pattern!r} under {self._rel(rel)!r} in dataset {self.name!r}")
        gz = buf.getvalue()
        if len(gz) > MAX_ARCHIVE_OUT_BYTES:
            raise VaultError(f"tgz is {len(gz)} bytes (max {MAX_ARCHIVE_OUT_BYTES}): narrow path or pattern")
        # Member names are dataset-relative, like every other path that comes
        # back: extracting the tgz reproduces the tree as the dataset holds it,
        # not a directory named after the dataset.
        return {"dataset": self.name, "base": self._rel(base), "file_count": n,
                "original_bytes": total, "tgz_bytes": len(gz),
                "tgz_base64": base64.b64encode(gz).decode("ascii")}

    def history(self, rel: str = "", n: int = 10) -> dict:
        n = max(1, min(int(n), 50))
        args = ["log", f"-{n}", "--format=%h · %cI · %s"]
        if rel:
            p = self._resolve(rel, must_exist=True)
            args += ["--follow", "--", str(p.relative_to(self.root))]
        out = self._git(*args, check=False).strip()
        return {"dataset": self.name, "path": self._rel(rel),
                "entries": out.splitlines() if out else ["(no history)"]}

    def diff(self, rev_a: str, rev_b: str = "HEAD", rel: str = "") -> dict:
        self._check_rev(rev_a)
        self._check_rev(rev_b)
        args = ["diff", "--stat=120", f"{rev_a}..{rev_b}"]
        if rel:
            p = self._resolve(rel, must_exist=False)
            args = ["diff", f"{rev_a}..{rev_b}", "--", str(p.relative_to(self.root))]
        out = self._git(*args)
        if len(out.encode()) > MAX_DIFF_BYTES:
            out = out.encode()[:MAX_DIFF_BYTES].decode(errors="replace") + "\n[... diff truncated ...]"
        return {"dataset": self.name, "path": self._rel(rel), "from": rev_a, "to": rev_b,
                "diff": out or "(no differences)"}

    # ---------- maintenance ----------

    def restore(self, rev: str, expected_manifest: str) -> dict:
        """Bring the WHOLE dataset back to how it was at `rev`, with a FORWARD
        commit: history is not lost, it grows. A restore can be undone the same
        way anything else can.

        Uses `read-tree -u --reset`, which aligns index and working tree to that
        revision's tree without moving HEAD; the following commit records the
        restore as an ordinary change."""
        self._check_rev(rev)
        with self._lock():
            self._commit_external_if_dirty()
            current = self.manifest("")["manifest_sha256"]
            if expected_manifest != current:
                raise VaultError(
                    f"CONFLICT: expected manifest {expected_manifest[:12]}... but the dataset "
                    f"is {current[:12]}... Re-read the manifest and retry.")
            full_sha = self._git("rev-parse", "--verify", f"{rev}^{{commit}}").strip()
            self._git("read-tree", "-u", "--reset", full_sha)
            commit = self._commit(f"restore: dataset returned to {full_sha[:7]}")
            after = self.manifest("")
        return {"dataset": self.name, "restored_from": full_sha[:7], "commit": commit,
                "file_count": after["file_count"], "manifest_sha256": after["manifest_sha256"]}

    def trash_purge(self, before: str) -> dict:
        """Empty Trash/ of everything trashed BEFORE `before` (ISO date, e.g.
        2026-06-01). The trashing date is the mtime, which move_path resets to
        "now" whenever the destination is inside Trash/.

        This is not real destruction: contents remain in git history and stay
        recoverable with history + read_at. The purge removes clutter from the
        working tree."""
        try:
            limit = datetime.fromisoformat(before.strip()).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            raise VaultError(f"invalid date: {before!r} — ISO expected, e.g. 2026-06-01")
        trash = self.root / TRASH
        if not trash.is_dir():
            return {"dataset": self.name, "removed": 0, "bytes_freed": 0,
                    "files": [], "note": "no Trash/ folder in this dataset"}
        with self._lock():
            external = self._commit_external_if_dirty()
            removed, freed = [], 0
            for p in sorted(trash.rglob("*")):
                if self._skip(p):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime < limit:
                    freed += st.st_size
                    removed.append(self._rel(p))
                    p.unlink()
            # directories left empty: remove those too, deepest first
            for d in sorted((q for q in trash.rglob("*") if q.is_dir()),
                            key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
            commit = self._commit(f"trash-purge: {len(removed)} files trashed before {before}") \
                if removed else "(nothing to commit)"
        return {"dataset": self.name, "before": before, "removed": len(removed),
                "bytes_freed": freed, "files": removed[:50],
                "note": "recoverable with history + read_at: contents remain in git history",
                **({"commit": commit} if removed else {}),
                **({"external_commit_first": external} if external else {})}

    def prune_history(self, months: int) -> str:
        """Prune history older than `months`. In git, old commits cannot simply
        be deleted — the chain is one — so the PREFIX is squashed: an orphan
        commit carrying the tree at the threshold is created, and the later
        history is grafted on top of it.

        This rewrites the hashes of commits after the threshold. It NEVER
        touches the working tree: in the worst case history is lost, never data.
        Called only at boot, and only when GIT_RETENTION_MONTHS > 0.
        """
        if self._git("status", "--porcelain").strip():
            raise VaultError("working tree not clean: pruning postponed")
        days = int(months) * 30
        cutoff = self._git("rev-list", "-1", f"--before={days} days ago", "HEAD",
                           check=False).strip()
        if not cutoff:
            return f"pruning not needed (no commits older than {months} months)"
        first = self._git("rev-list", "--max-parents=0", "HEAD", check=False).strip().splitlines()
        if first and cutoff.startswith(first[0][:len(cutoff)]):
            return "pruning not needed (the threshold falls on the initial commit)"
        before_n = int(self._git("rev-list", "--count", "HEAD", check=False).strip() or 0)
        tree = self._git("rev-parse", f"{cutoff}^{{tree}}").strip()
        date = self._git("log", "-1", "--format=%cs", cutoff).strip()
        base = subprocess.run(
            ["git", "-C", str(self.root), "commit-tree", tree, "-m",
             f"archivist-mcp: history truncated, content as of {date}"],
            capture_output=True, text=True, timeout=60)
        if base.returncode != 0:
            raise VaultError(f"commit-tree failed: {base.stderr.strip()[:200]}")
        new_base = base.stdout.strip()
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        r = subprocess.run(["git", "-C", str(self.root), "rebase", "--onto", new_base, cutoff, branch],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            subprocess.run(["git", "-C", str(self.root), "rebase", "--abort"],
                           capture_output=True, timeout=60)
            raise VaultError(f"rebase failed, history intact: {r.stderr.strip()[:200]}")
        self._git("reflog", "expire", "--expire=now", "--all", check=False)
        self._git("gc", "--prune=now", "-q", check=False, timeout=600)
        after_n = int(self._git("rev-list", "--count", "HEAD", check=False).strip() or 0)
        return (f"history pruned: {before_n} -> {after_n} commits "
                f"({months}-month threshold, content as of {date})")
