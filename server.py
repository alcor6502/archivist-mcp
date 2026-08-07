"""
server.py — self-hosted MCP server for a dataset vault. v1.6

Architecture:
- the server listens on 127.0.0.1:PORT and does NOT know how traffic reaches
  it (today Tailscale Funnel, tomorrow a reverse proxy: zero lines to change);
- auth: OAuth 2.1 towards the client (DCR/PKCE handled by FastMCP's
  OAuthProxy), login delegated to GitHub, access ALLOWED ONLY to
  ALLOWED_GITHUB_LOGIN;
- defence in depth: requests are refused unless they come from Anthropic's
  documented egress range (disable with ANTHROPIC_CIDR="").

Datasets and keys:
- the vault root holds DATASETS (top-level directories), each with its own git
  repository;
- every path starts with the dataset name; a path that is only the dataset
  name means "the whole dataset";
- a dataset listed in keys.txt is LOCKED: every call must carry its key in the
  `key` parameter. Without a line it is OPEN.

Environment:
  VAULT_ROOT              vault root inside the container (default /vault)
  KEYS_FILE               key registry (default <VAULT_ROOT>/keys.txt)
  GIT_RETENTION_MONTHS    prune history at boot; 0 = disabled (default)
  BASE_URL                public URL (e.g. https://host.tailnet.ts.net)
  GITHUB_CLIENT_ID        GitHub OAuth App
  GITHUB_CLIENT_SECRET    GitHub OAuth App
  ALLOWED_GITHUB_LOGIN    the only user allowed in
  JWT_SIGNING_KEY         stable key for issued tokens (openssl rand -hex 32)
  PORT                    default 3000
  ANTHROPIC_CIDR          default 160.79.104.0/21; empty string disables it
"""
from __future__ import annotations

import ipaddress
import logging
import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

from vault import VaultRoot, VaultError

VERSION = "1.6.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("archivist-mcp")


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        log.error("missing environment variable: %s", name)
        sys.exit(2)
    return v


VAULT_ROOT = env("VAULT_ROOT", "/vault")
KEYS_FILE = env("KEYS_FILE", str(Path(VAULT_ROOT) / "keys.txt"))
RETENTION = int(env("GIT_RETENTION_MONTHS", "0") or 0)
BASE_URL = env("BASE_URL")
ALLOWED_LOGIN = env("ALLOWED_GITHUB_LOGIN")
PORT = int(env("PORT", "3000"))
CIDR = os.environ.get("ANTHROPIC_CIDR", "160.79.104.0/21").strip()

vault = VaultRoot(VAULT_ROOT, KEYS_FILE)
for line in vault.boot(RETENTION):
    log.info("boot — %s", line)
log.info("datasets: %s — key registry: %s",
         ", ".join(f"{d['name']}({d['state']})" for d in vault.status(VERSION)["datasets"]) or "(none)",
         KEYS_FILE)

auth = GitHubProvider(
    client_id=env("GITHUB_CLIENT_ID"),
    client_secret=env("GITHUB_CLIENT_SECRET"),
    base_url=BASE_URL,
    jwt_signing_key=env("JWT_SIGNING_KEY"),
    require_authorization_consent=True,
)

mcp = FastMCP("archivist-mcp", auth=auth)


class Gate(Middleware):
    """Two filters, before anything else: the GitHub identity and the source IP."""

    def __init__(self) -> None:
        self.net = ipaddress.ip_network(CIDR) if CIDR else None

    async def on_call_tool(self, ctx: MiddlewareContext, call_next):
        tok = get_access_token()
        login = (tok.claims.get("login") if tok and tok.claims else None)
        if login != ALLOWED_LOGIN:
            raise ValueError("user not authorised")
        if self.net is not None:
            req = get_http_request()
            src = (req.headers.get("x-forwarded-for", "").split(",")[0].strip()
                   or (req.client.host if req.client else ""))
            try:
                if not src or ipaddress.ip_address(src) not in self.net:
                    raise ValueError("origin not allowed")
            except ValueError:
                raise ValueError("origin not allowed")
        return await call_next(ctx)


mcp.add_middleware(Gate())

_GUIDE = Path(__file__).with_name("reference-guide.md")


# ============================ vault level ============================

@mcp.tool
def vault_status() -> dict:
    """Quick check: the vault answers, plus the list of datasets and their
    state (open / locked). Nothing else — no file counts, no git status.
    Call this first: if it does not answer, do not try the others.
    For the details of one dataset use dataset_status."""
    return vault.status(VERSION)


@mcp.tool
def reference_guide() -> dict:
    """The usage guide for this vault: the model, the rules, the recipes.
    Call it when you are unsure which tool to use or how a path is composed.
    No key required."""
    try:
        return {"version": VERSION, "guide": _GUIDE.read_text(encoding="utf-8")}
    except OSError as e:
        raise VaultError(f"guide not available in the image: {e}")


@mcp.tool
def dataset_create(name: str) -> dict:
    """Create a new dataset, OPEN and empty, with its own git repository.
    Use it to park the material of a small project so it does not vanish when
    the conversation ends. To protect it afterwards, a line is added to the key
    registry on the server. Names: no '/', no leading '.' or '_'."""
    return vault.create(name)


@mcp.tool
def dataset_drop(dataset: str, expected_manifest: str) -> dict:
    """Delete an OPEN dataset and everything in it, git included.
    expected_manifest is the current manifest_sha256 (from manifest): you cannot
    throw away what you have not looked at, and if someone wrote in the meantime
    the drop is refused.
    A dataset with a key is NOT droppable: it must first be removed from the key
    registry on the server, or deleted by hand. There is no way around it."""
    return vault.drop(dataset, expected_manifest)


# =========================== dataset level ===========================

@mcp.tool
def dataset_status(dataset: str, key: str = "") -> dict:
    """State of one dataset: file count, how many are in Trash, git status,
    number of commits, repository size, last commit."""
    return vault.open_by_name(dataset, key).status()


@mcp.tool
def list_files(path: str, key: str = "") -> dict:
    """List the files under `path` with the size and sha256 of each. The hash is
    the fact: two views with the same sha are the same file. Recursive.
    `path` always starts with a dataset name; the bare dataset name means the
    whole dataset."""
    ds, rel = vault.open(path, key)
    return ds.list_files(rel)


@mcp.tool
def read_file(path: str, key: str = "") -> dict:
    """Read a UTF-8 text file and return its content plus sha256.
    That sha256 is the one to pass back to write_file or edit_file."""
    ds, rel = vault.open(path, key)
    return ds.read_file(rel)


@mcp.tool
def append(path: str, text: str, key: str = "") -> dict:
    """Append a BLOCK of text to an existing file (1..N lines, max 64 KB),
    atomically, and commit. It NEVER touches existing bytes and needs no sha:
    this is the right operation for logs, registers and multi-line entries.
    To change existing content use edit_file; to rewrite, write_file."""
    ds, rel = vault.open(path, key)
    return ds.append(rel, text)


@mcp.tool
def write_file(path: str, content: str, expected_sha256: str, key: str = "") -> dict:
    """Write the WHOLE file, but only if expected_sha256 matches the file's
    current sha (compare-and-swap). For a new file pass "new".
    A wrong sha means someone wrote after you read: re-read and retry.
    UTF-8 text only, max 2 MB. It cannot create directories in the vault root:
    the first path segment must be a dataset that already exists."""
    ds, rel = vault.open(path, key)
    return ds.write_file(rel, content, expected_sha256)


@mcp.tool
def edit_file(path: str, old_text: str, new_text: str,
              expected_sha256: str, key: str = "") -> dict:
    """Surgical edit of a text file: replaces old_text (which must occur EXACTLY
    once) with new_text. Only the fragments travel, not the file. Same CAS as
    write_file: a wrong sha is refused. If the fragment occurs several times,
    widen the context until it is unique. Max 2 MB."""
    ds, rel = vault.open(path, key)
    return ds.edit_file(rel, old_text, new_text, expected_sha256)


@mcp.tool
def move_path(src: str, dst: str, key: str = "") -> dict:
    """Move or rename WITHIN one dataset (including into Trash/ to archive).
    Never overwrites: if the destination exists, it refuses. There is no tool to
    delete files — moving into Trash/ is the disposal route.
    src and dst must live in the same dataset."""
    ds_s, rel_s = vault.open(src, key)
    ds_d, rel_d = vault.open(dst, key)
    if ds_s.name != ds_d.name:
        raise VaultError(
            f"src is in {ds_s.name!r} and dst in {ds_d.name!r}: move_path works "
            "inside a single dataset. Moves across datasets are done on the server.")
    return ds_s.move_path(rel_s, rel_d)


@mcp.tool
def search(pattern: str, path: str, regex: bool = False, key: str = "") -> dict:
    """Search text files server-side: returns file:line:text without downloading
    anything. Default is a literal string; regex=True for expressions.
    Binaries are left out (find those by name with list_files). Max 200 lines:
    if it truncates, narrow with path or a more precise pattern.
    `path` may be the bare dataset name to search all of it."""
    ds, rel = vault.open(path, key)
    return ds.search(pattern, rel, regex)


@mcp.tool
def manifest(path: str, key: str = "") -> dict:
    """The fingerprint of a tree in ONE number: file count, total bytes and the
    sha256 of the ordered (sha, path) list. Two equal manifests mean identical
    trees. Used for integrity checks, before/after comparisons, and as the
    mandatory confirmation for dataset_drop and dataset_restore."""
    ds, rel = vault.open(path, key)
    return ds.manifest(rel)


@mcp.tool
def archive(path: str, pattern: str = "*.md", key: str = "") -> dict:
    """Download in ONE call every file matching pattern under path: tar.gz as
    base64. Extract it in a sandbox and verify the hashes against list_files.
    Replaces hundreds of read_file calls in an audit. Max 30 MB uncompressed in
    and 5 MB of tgz out: above that, narrow the scope."""
    ds, rel = vault.open(path, key)
    return ds.archive(rel, pattern)


@mcp.tool
def read_binary(path: str, key: str = "") -> dict:
    """Read ANY file (PDF, binary) as base64 plus sha256. Max 2 MB: a larger
    binary is not consumable in a conversation anyway — move it over SMB/scp.
    The base64 is only useful if you have a sandbox to decode it in."""
    ds, rel = vault.open(path, key)
    return ds.read_binary(rel)


@mcp.tool
def write_binary(path: str, content_base64: str, expected_sha256: str, key: str = "") -> dict:
    """Write a binary file (PDF, raw CSV...) from base64, with the same CAS as
    write_file ("new" for new files). Atomic, verified, committed.
    Max 2 MB decoded. ALWAYS compare the returned sha256 with the one computed
    at the source: base64 travels as generated text and the copy can go wrong —
    the sha catches it every time."""
    ds, rel = vault.open(path, key)
    return ds.write_binary(rel, content_base64, expected_sha256)


@mcp.tool
def read_at(path: str, rev: str, key: str = "") -> dict:
    """Read a text file AS IT WAS at a past git revision (a hash from history,
    or "HEAD~3"). Read-only: for recovering lost content without touching
    anything. Revisions are those of the single dataset."""
    ds, rel = vault.open(path, key)
    return ds.read_at(rel, rev)


@mcp.tool
def history(path: str, n: int = 10, key: str = "") -> dict:
    """The last n git history entries. If `path` is the bare dataset name, the
    history is that of the whole dataset; if it is a file, that of the file
    (following renames too). Format: hash · ISO date · message.
    The short hash is passed verbatim to read_at and diff."""
    ds, rel = vault.open(path, key)
    return ds.history(rel, n)


@mcp.tool
def diff(rev_a: str, path: str, rev_b: str = "HEAD", key: str = "") -> dict:
    """Differences between two revisions of a dataset, e.g.
    diff("HEAD~1", "Example Project"). If `path` is the bare dataset name it
    shows the per-file summary; if it is a file, the full diff of that file.
    Truncates at 60 KB rather than failing."""
    ds, rel = vault.open(path, key)
    return ds.diff(rev_a, rev_b, rel)


@mcp.tool
def dataset_restore(dataset: str, rev: str, expected_manifest: str, key: str = "") -> dict:
    """USE WITH CARE: rewrites EVERY file in the dataset, bringing it back to
    how it was at `rev`. Check the revision with history before calling, and
    pass the current manifest_sha256 as confirmation.
    It is not destructive: the restore is a FORWARD commit, so history is not
    lost and this too can be undone."""
    ds = vault.open_by_name(dataset, key)
    return ds.restore(rev, expected_manifest)


@mcp.tool
def trash_purge(dataset: str, before: str, key: str = "") -> dict:
    """Empty Trash/ of everything trashed BEFORE the given date (ISO, e.g.
    "2026-06-01"). The date considered is when the file was trashed, not when it
    was last modified.
    Contents remain in git history: they stay recoverable with history +
    read_at. This removes clutter, it does not destroy information."""
    ds = vault.open_by_name(dataset, key)
    return ds.trash_purge(before)


if __name__ == "__main__":
    log.info("archivist-mcp %s — starting on 127.0.0.1:%s — base_url %s — allowed user: %s "
             "— IP filter: %s — token store: %s — retention: %s",
             VERSION, PORT, BASE_URL, ALLOWED_LOGIN, CIDR or "OFF",
             os.environ.get("FASTMCP_HOME", "(default — NOT persistent!)"),
             f"{RETENTION} months" if RETENTION else "disabled")
    mcp.run(transport="http", host=os.environ.get("BIND_HOST", "127.0.0.1"), port=PORT)
