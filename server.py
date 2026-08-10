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
                          Anything else falls back to INFO and says so
  FASTMCP_HOME            token store; set in the Dockerfile, MUST persist
  VAULT_UID / VAULT_GID   service user, dropped to by the entrypoint (99/100)
  PREFLIGHT_SKIP          checks to skip, by name. Testing only
"""
from __future__ import annotations

import functools
import ipaddress
import logging
import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

from preflight import cidrs_from_env, describe_cidrs, log_level_from_env
from vault import VaultRoot, VaultError, VaultFault

VERSION = "2.3.0"

# The ROOT logger stays at WARNING. It used to be INFO, which switched on INFO
# for every library loaded, not for ours: that is where the noise came from.
# Only our own logger follows LOG_LEVEL.
logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("archivist-mcp")
# Resolved in preflight.log_level_from_env for the same reason as the IP filter:
# one expression, not two that agree today. The closed list lives THERE and not
# only in the Unraid template, because a container built by hand has no template
# — and setLevel() raises on an unknown value, at import, after a clean
# preflight, which is the worst place in the startup for a typo to land.
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
# Resolved in preflight.cidrs_from_env so that the service and the preflight
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


def tool(fn):
    """Register a tool, and turn its refusals into something the log can tell
    apart from a fault.

    A VaultError is a DESIGNED refusal: a wrong key, a CONFLICT, a path
    outside the dataset, a file that is not there, a block over the ceiling.
    Not every error the engine raises is one — git failing on a commit, a
    short read, a write whose read-back does not match are FAULTS, and they
    carry VaultFault, which is caught first and left alone. That distinction
    is not decoration: without it this decorator would have taken a full disk
    and made it a line starting with the word "refused", which is the defect
    it exists to close, inverted.

    Left as a plain exception, a refusal is logged by FastMCP through
    logger.exception. The log of 2026-Aug-09 is what
    settled it: three refusals, three tracebacks of some twenty-five lines,
    all under `ERROR: Error calling tool` — and two of the three were CONFLICTs
    on edit_file, which is the CAS doing exactly its job, two writers on one
    document and the second turned away with a message telling it what to do.
    The system working, logged as a failure. The level is ERROR, so
    LOG_LEVEL=WARNING does not silence them either: they are always there, and
    after a week of them nobody reads tracebacks any more — the next real fault
    arrives disguised as routine.

    Raised as ToolError the traceback goes away: FastMCP logs FastMCPError
    with exc_info=False, at the level the exception carries. A bug still gets
    the full traceback at ERROR, which is what a bug deserves.

    But FastMCP's own line does not survive the container either, and this was
    measured rather than assumed: the Dockerfile sets FASTMCP_LOG_LEVEL=WARNING
    — for the noise, and rightly — so an INFO record from fastmcp.server.server
    is dropped before it is printed. Converting alone therefore does not turn
    twenty-five lines into one: it turns them into none, and a refusal that
    leaves no trace at all is a different bargain from the one being made here.
    So the line is OURS. It goes on the archivist-mcp logger, which follows
    LOG_LEVEL and defaults to INFO, and it says more than FastMCP's ever did —
    which tool, and why it refused:

        INFO archivist-mcp: refused edit_file: CONFLICT: expected sha …

    Deliberately INFO and not WARNING. WARNING is where the Gate logs a
    stranger turned away, and that line is contractual precisely because it is
    the only thing that tells a refused stranger from a broken deployment. A
    CONFLICT — the system working — must not sit at the same height.

    The line carries the message, which carries paths — and uvicorn's access
    log was switched off a few lines above for exactly that reason. The two are
    not the same bargain, and the difference is worth writing down: an access
    log records everything that was READ, and becomes a register of what was
    looked at and when. This records only what was REFUSED, which is rare, is
    the thing you are trying to diagnose, and is useless as a register because
    it is precisely the calls that did not happen. Anyone who disagrees has one
    knob and it is documented: LOG_LEVEL=WARNING takes the line away.

    THE TRAP, and it cost the codifier an hour: doing this in a Middleware does
    not work. call_tool applies the middleware chain OUTSIDE and logs INSIDE —
    the outer call delegates to itself with run_middleware=False, and that
    inner call is where the try/except lives. By the time a middleware sees the
    exception, logger.exception has already run. The conversion has to happen
    inside the tool function, which is here.

    A second reason, and it is a risk rather than a fact: a plain exception is
    subject to FastMCP's error masking. Today the messages reach the chat
    intact — the CONFLICT text reads word for word — but that rests on a
    default. The day it flips, every talking error in the table would arrive as
    "an error occurred". ToolError messages are passed through by contract.

    functools.wraps is what keeps the MCP contract intact — name, docstring and
    signature are what FastMCP builds the schema from, and it follows
    __wrapped__. Verified against fastmcp 3.4.5 by dumping all 21 schemas
    before and after: parameters, defaults and required lists came out
    identical, so no client has to reconnect.

    The Gate is NOT part of this and stays as it is: it raises ValueError, its
    refusals are about identity and origin, and they are logged at WARNING on
    purpose — a refused stranger and a broken deployment look the same at the
    client, and that line is the only thing that tells them apart.

    The conversion lives HERE and never in vault.py: the engine must stay
    importable without FastMCP, which is what lets the suite run with no
    network, no Docker and no OAuth. test_vault checks that every tool goes
    through this door, since nothing at runtime would notice one that did
    not."""
    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except VaultFault:
            # A fault is not a refusal, and the ORDER of these two branches is
            # the whole distinction: VaultFault subclasses VaultError, so
            # swapping them would swallow every fault into the quiet path.
            # Left to rise, it keeps the traceback at ERROR — which is what a
            # broken machine deserves, and it is how it behaved before 2.3.0.
            raise
        except VaultError as e:
            log.info("refused %s: %s", fn.__name__, e)
            raise ToolError(str(e), log_level=logging.INFO) from None
    return mcp.tool(guarded)


class Gate(Middleware):
    """Two filters, before anything else: the GitHub identity and the source IP.
    The XFF header is filled in by the Funnel, which is the trusted proxy.

    It hooks `on_request`, which covers EVERY message that expects an answer —
    `initialize` and `tools/list` as much as `tools/call`. Until v2.0 it hooked
    `on_call_tool`, and the hole that left was narrow but real: a stranger who
    completed the OAuth flow with their own GitHub account got a valid token,
    and with it could list every tool with its description. Every
    call was refused, so no data leaked — but the shape of the surface did, and
    a surface nobody can enumerate is one nobody can study.

    Not `on_message`, which is one level wider: it also covers NOTIFICATIONS,
    which are fire-and-forget. Raising there has no channel to answer on, so it
    buys undefined behaviour instead of security — a notification reads nothing.

    The refusals are LOGGED, and that is not decoration. Once the gate covers
    the handshake, a refused stranger and a broken deployment produce the same
    symptom at the client: "the connector will not connect". The log line is the
    only thing that tells the two apart."""

    HOOK = "on_request"   # pinned by a static check: a typo here disables the
                          # gate in silence, because the base class supplies a
                          # default for every hook name that does exist.

    def __init__(self) -> None:
        self.nets = [ipaddress.ip_network(c) for c, _ in ALLOWED_CIDRS]

    async def on_request(self, ctx: MiddlewareContext, call_next):
        tok = get_access_token()
        login = (tok.claims.get("login") if tok and tok.claims else None)
        if login != ALLOWED_LOGIN:
            log.warning("refused %s: GitHub login %r is not %r",
                        ctx.method, login, ALLOWED_LOGIN)
            raise ValueError("user not authorised")
        if self.nets:
            req = get_http_request()
            src = (req.headers.get("x-forwarded-for", "").split(",")[0].strip()
                   or (req.client.host if req.client else ""))
            try:
                ip = ipaddress.ip_address(src) if src else None
                if ip is None or not any(ip in n for n in self.nets):
                    raise ValueError("origin not allowed")
            except ValueError:
                log.warning("refused %s: source %r outside the allowed ranges",
                            ctx.method, src)
                raise ValueError("origin not allowed")
        return await call_next(ctx)


mcp.add_middleware(Gate())

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
    file. Empty path: the whole dataset."""
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
def archive(dataset: str, path: str = "", pattern: str = "*.md", key: str = "") -> dict:
    """Every file matching `pattern` under `path` in ONE call, as a base64
    tar.gz. Needs a sandbox to extract it in."""
    ds, rel = vault.open(dataset, path, key)
    return ds.archive(rel, pattern)


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
    log.info("archivist-mcp %s — starting on %s:%s — base_url %s — allowed user: %s "
             "— IP filter: %s — token store: %s — retention: %s",
             VERSION, BIND_ADDR, PORT, BASE_URL, ALLOWED_LOGIN, describe_cidrs(ALLOWED_CIDRS),
             os.environ.get("FASTMCP_HOME", "(default — NOT persistent!)"),
             f"{RETENTION} months" if RETENTION else "disabled")
    mcp.run(transport="http", host=BIND_ADDR, port=PORT)
