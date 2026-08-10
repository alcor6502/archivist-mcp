"""
server.py — self-hosted MCP server for a dataset vault.

Architecture:
- the server listens on 127.0.0.1:PORT — loopback only, not configurable — and
  does NOT know how traffic reaches it (today Tailscale Funnel, tomorrow a
  reverse proxy: zero lines to change);
- auth: OAuth 2.1 towards the client (DCR/PKCE handled by FastMCP's
  OAuthProxy), login delegated to GitHub, access ALLOWED ONLY to
  ALLOWED_GITHUB_LOGIN;
- defence in depth: requests are refused unless their source IP falls in one
  of the ALLOWED_CIDRS ranges (an empty string disables the filter).

Datasets and keys:
- the vault root holds DATASETS (top-level directories), each with its own git
  repository;
- every call names its dataset explicitly in `dataset`, and `path` is relative
  to it; an empty path means "the whole dataset";
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
  ALLOWED_CIDRS           list of accepted ranges, ';' between entries and
                          '#' opening a description:
                            160.79.104.0/21 # egress ; 100.64.0.0/10 # tailnet
                          empty string disables the filter; if the variable is
                          not defined at all the deprecated ANTHROPIC_CIDR is
                          read, and failing that the documented egress range
  ANTHROPIC_CIDR          DEPRECATED, still honoured: see ALLOWED_CIDRS
  LOG_LEVEL               level of THIS logger only, INFO or WARNING (default
                          INFO). Nothing below INFO: there are no debug lines.
                          WARN is honoured as WARNING, being Python's own alias.
                          Anything else falls back to INFO and says so
  FASTMCP_HOME            token store; set in the Dockerfile, MUST persist
  VAULT_UID / VAULT_GID   service user, dropped to by the entrypoint (99/100)
  PREFLIGHT_SKIP          checks to skip, by name. Testing only
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider

# The engine: gate, refusal conversion, config helpers — the parts the twins
# had written twice, pinned by tag in requirements.txt. Config comes from the
# engine's ROOT (same expression the preflight reads, so the two can never
# disagree); Gate and make_tool from their own modules, which is where the
# reasoning lives — where the gate hooks and why not one level narrower, why
# the conversion cannot live in a middleware, why converting without writing
# your own log line produces NO line rather than one.
from mcp_common_engine import (VERSION as ENGINE_VERSION, cidrs_from_env,
                               describe_cidrs, log_level_from_env)
from mcp_common_engine.gate import Gate
from mcp_common_engine.refusals import make_tool
from vault import VaultRoot, VaultError, VaultFault

VERSION = "2.5.1"

# The ROOT logger stays at WARNING. It used to be INFO, which switched on INFO
# for every library loaded, not for ours: that is where the noise came from.
# Only our own logger follows LOG_LEVEL.
logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("archivist-mcp")
# Resolved in the engine's log_level_from_env for the same reason as the IP
# filter: one expression, read by this file and by the preflight, not two that
# agree today. The closed list lives THERE and not only in the Unraid template,
# because a container built by hand has no template — and setLevel() raises on
# an unknown value, at import, after a clean preflight, which is the worst
# place in the startup for a typo to land.
_LEVEL, _REJECTED = log_level_from_env()
log.setLevel(_LEVEL)
if _REJECTED:
    log.warning("LOG_LEVEL=%r is not INFO or WARNING — using INFO", _REJECTED)

# uvicorn's access log is one line per request; a request carries a path, and a
# path carries dataset and document names. Left on, the log slowly becomes a
# record of what was read and when. Same reasoning that keeps vault_status()
# minimal: commit messages contain paths.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# FastMCP's own banner, rich formatting and boot-time update check are turned
# off in the Dockerfile (ENV), NOT here: those settings are read when fastmcp is
# imported, so anything set after the import above would arrive too late. The
# test suite checks the Dockerfile still carries them, so the cure cannot go
# missing without something saying so.


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
# NOT configurable, and that is the design. Legitimate traffic arrives from the
# Funnel, which runs inside this container; nothing else has a reason to reach
# the port. It used to be the BIND_HOST variable, which bought one thing only:
# the ability to open the service to the LAN, bypassing the Funnel's filtering
# — a setting whose best outcome was that nobody used it.
#
# It is a constant rather than two literals because that is how it broke: the
# log printed 127.0.0.1 as text while the bind followed the variable, so
# BIND_HOST=0.0.0.0 produced a log naming an address nobody listened on. One
# name, used by both, cannot drift.
BIND_ADDR = "127.0.0.1"
# Resolved in the engine's cidrs_from_env so that the service and the preflight
# can never disagree about what the filter is. A malformed entry raises here,
# which is deliberate: it has already blocked the preflight by this point.
ALLOWED_CIDRS = cidrs_from_env()

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


# The decorator that turns a designed refusal into ToolError plus ONE log
# line of our own lives in the engine (mcp_common_engine/refusals.py), with
# the reasoning: where the trap is (a middleware sees the exception after
# logger.exception has run), why the fault branch is caught FIRST, and why
# converting without your own line trades twenty-five lines for none. What
# stays here is the BINDING, because it is the one thing the engine cannot
# know: which class is a refusal and which is a fault. Two decisions of ours
# ride on it and are worth restating at the seam:
#   - the line goes out at INFO, not WARNING: WARNING is the Gate's height,
#     and a CONFLICT — the CAS doing its job — must not sit at the same
#     height as a stranger turned away;
#   - the line carries the message, which carries paths. uvicorn's access log
#     was switched off above for exactly that reason; this records only what
#     was REFUSED, which is rare and is the thing being diagnosed. The knob
#     is documented: LOG_LEVEL=WARNING takes the line away.
tool = make_tool(mcp, log, refusal=VaultError, fault=VaultFault)


# The Gate — GitHub identity plus source-IP filter, hooked on `on_request` —
# lives in the engine (mcp_common_engine/gate.py), with the reasoning about
# the hook level and the two things it deliberately does not cover. Config is
# INJECTED: the engine reads no module globals, so what this line hands over
# is exactly what the startup line prints and the preflight reports. The XFF
# header is filled in by the Funnel, which is the trusted proxy.
mcp.add_middleware(Gate(log=log, allowed_login=ALLOWED_LOGIN,
                        allowed_cidrs=ALLOWED_CIDRS))

_GUIDE = Path(__file__).with_name("reference-guide.md")


# ============================ vault level ============================

@tool
def vault_status() -> dict:
    """Start here: is the vault alive, and which datasets exist (open / locked).
    No key needed. If it does not answer, stop and say so — no other tool will
    work either."""
    return vault.status(VERSION)


@tool
def reference_guide() -> dict:
    """The manual for this vault: the model, the rules, which tool for which job,
    the limits and what to do when you hit them, the recipes, the errors.
    Read it before your first write. No key needed."""
    try:
        return {"version": VERSION, "guide": _GUIDE.read_text(encoding="utf-8")}
    except OSError as e:
        raise VaultError(f"guide not available in the image: {e}")


@tool
def dataset_create(name: str) -> dict:
    """Create a new dataset: open, empty, with its own git.
    Name: no '/', no leading '.' or '_'."""
    return vault.create(name)


@tool
def dataset_drop(dataset: str, expected_manifest: str) -> dict:
    """Delete an OPEN dataset and everything in it. Needs the current
    manifest_sha256. A dataset with a key cannot be dropped."""
    return vault.drop(dataset, expected_manifest)


# =========================== dataset level ===========================

@tool
def dataset_status(dataset: str, key: str = "") -> dict:
    """One dataset in detail: file count, Trash, git status, commits, repository
    size, last commit."""
    return vault.open_by_name(dataset, key).status()


@tool
def list_files(dataset: str, path: str = "", key: str = "") -> dict:
    """Files under `path` with size and sha256, recursive. Same sha means same
    file. Empty path: the whole dataset. An empty folder does not appear."""
    ds, rel = vault.open(dataset, path, key)
    return ds.list_files(rel)


@tool
def read_file(dataset: str, path: str, key: str = "") -> dict:
    """Read a UTF-8 text file: content plus sha256. That sha is what write_file
    and edit_file want back."""
    ds, rel = vault.open(dataset, path, key)
    return ds.read_file(rel)


@tool
def append(dataset: str, path: str, text: str, key: str = "") -> dict:
    """Append a block to an existing file. It never touches existing bytes, so it
    needs no sha. Max 64 KB."""
    ds, rel = vault.open(dataset, path, key)
    return ds.append(rel, text)


@tool
def write_file(dataset: str, path: str, content: str,
               expected_sha256: str, key: str = "") -> dict:
    """Write the WHOLE file. CAS: expected_sha256 must match the file's current
    sha, or "new" for a new file. UTF-8, max 2 MB."""
    ds, rel = vault.open(dataset, path, key)
    return ds.write_file(rel, content, expected_sha256)


@tool
def edit_file(dataset: str, path: str, old_text: str, new_text: str,
              expected_sha256: str, key: str = "") -> dict:
    """Replace old_text — which must occur EXACTLY once — with new_text. Same CAS
    as write_file. Only the fragments travel, not the file."""
    ds, rel = vault.open(dataset, path, key)
    return ds.edit_file(rel, old_text, new_text, expected_sha256)


@tool
def move_path(dataset: str, src: str, dst: str, key: str = "") -> dict:
    """Move, rename or trash inside the dataset. Never overwrites. There is no
    delete tool: moving into Trash/ is the disposal route."""
    # src and dst are relative to the SAME dataset, so a move across datasets is
    # no longer expressible: the runtime check that used to catch it is gone,
    # replaced by the shape of the call itself.
    ds, rel_s = vault.open(dataset, src, key)
    rel_d = vault.check_path(ds.name, dst)
    return ds.move_path(rel_s, rel_d)


@tool
def search(dataset: str, pattern: str, path: str = "",
           regex: bool = False, key: str = "") -> dict:
    """Grep the dataset server-side: file:line:text, nothing downloaded.
    regex=True for expressions."""
    ds, rel = vault.open(dataset, path, key)
    return ds.search(pattern, rel, regex)


@tool
def manifest(dataset: str, path: str = "", key: str = "") -> dict:
    """Fingerprint of a tree in one number. Two equal manifests mean identical
    trees. Required by dataset_drop and dataset_restore."""
    ds, rel = vault.open(dataset, path, key)
    return ds.manifest(rel)


@tool
def archive(dataset: str, path: str = "", pattern: str = "*.md",
            max_chars: int = 0, key: str = "") -> dict:
    """Every file matching `pattern` under `path` in ONE call, as a base64
    tar.gz. Needs a sandbox to extract it in. A big one may not reach you at
    all — that ceiling is your client's, not this server's: read the guide
    before archiving a whole dataset. `max_chars` refuses instead of producing
    what will not travel."""
    ds, rel = vault.open(dataset, path, key)
    return ds.archive(rel, pattern, max_chars)


@tool
def read_binary(dataset: str, path: str, key: str = "") -> dict:
    """Read any file (PDF, binary) as base64 plus sha256. Max 2 MB. Useless
    without a sandbox to decode it in."""
    ds, rel = vault.open(dataset, path, key)
    return ds.read_binary(rel)


@tool
def write_binary(dataset: str, path: str, content_base64: str,
                 expected_sha256: str, key: str = "") -> dict:
    """Write a binary file from base64. Same CAS as write_file. Max 2 MB decoded.
    Compare the returned sha with the one computed at the source."""
    ds, rel = vault.open(dataset, path, key)
    return ds.write_binary(rel, content_base64, expected_sha256)


@tool
def read_at(dataset: str, path: str, rev: str, key: str = "") -> dict:
    """Read a text file as it was at a past revision (a hash from history, or
    "HEAD~3"). Read-only."""
    ds, rel = vault.open(dataset, path, key)
    return ds.read_at(rel, rev)


@tool
def history(dataset: str, path: str = "", n: int = 10, key: str = "") -> dict:
    """The last n commits of the dataset (empty path) or of one file: hash, ISO
    date, message. The short hash goes verbatim into read_at and diff."""
    ds, rel = vault.open(dataset, path, key)
    return ds.history(rel, n)


@tool
def diff(dataset: str, rev_a: str, path: str = "", rev_b: str = "HEAD",
         key: str = "") -> dict:
    """Differences between two revisions. An empty path gives the per-file
    summary; a file gives its full diff."""
    ds, rel = vault.open(dataset, path, key)
    return ds.diff(rev_a, rev_b, rel)


@tool
def dataset_restore(dataset: str, rev: str, expected_manifest: str, key: str = "") -> dict:
    """Rewrite EVERY file in the dataset back to `rev`. Needs the current
    manifest_sha256. Not destructive: it commits forward, so it can itself be
    undone."""
    ds = vault.open_by_name(dataset, key)
    return ds.restore(rev, expected_manifest)


@tool
def trash_purge(dataset: str, before: str, key: str = "") -> dict:
    """Empty Trash/ of what was trashed before an ISO date. Contents remain in
    git history."""
    ds = vault.open_by_name(dataset, key)
    return ds.trash_purge(before)


if __name__ == "__main__":
    # The engine's version rides next to our own, and it is a cure, not a
    # decoration: two repositories pinning a third can pin different tags, and
    # "identical to the twin" quietly becomes "identical if both updated".
    # This line is where somebody already looks after every Apply.
    log.info("archivist-mcp %s (engine %s) — starting on %s:%s — base_url %s — "
             "allowed user: %s — IP filter: %s — token store: %s — retention: %s",
             VERSION, ENGINE_VERSION, BIND_ADDR, PORT, BASE_URL, ALLOWED_LOGIN,
             describe_cidrs(ALLOWED_CIDRS),
             os.environ.get("FASTMCP_HOME", "(default — NOT persistent!)"),
             f"{RETENTION} months" if RETENTION else "disabled")
    mcp.run(transport="http", host=BIND_ADDR, port=PORT)
