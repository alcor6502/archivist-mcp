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
  HTTP_MODE               shape of the HTTP transport: stateful (default, the
                          behaviour of every earlier version) or stateless, in
                          which every request is served on its own transport
                          with no MCP session behind it. Anything else falls
                          back to stateful and says so. See the startup line:
                          it prints which one is running
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
# Imported by name on purpose: `mcp` is the server object below, and that name
# would shadow the package it comes from.
from mcp.types import Icon

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
from mcp_common_engine.logs import arm_argument_redaction, arm_timestamps
from mcp_common_engine.refusals import make_tool
# Read from preflight and not restated here for the same reason the log level
# is read from one expression: the suite can import preflight (it drags no
# fastmcp in) and cannot import this file, so a knob resolved here would be a
# knob nothing exercises.
from preflight import HTTP_MODES, http_mode_from_env
from vault import VaultRoot, VaultError, VaultFault, guide_for

VERSION = "2.10.0"

# The shape of a line of ours. It is a NAME because it is used twice: here, and
# again below where fastmcp's own handlers are given the same shape. Written out
# twice it would be two strings that agree until somebody edits one — and the
# symptom would be two kinds of line in one log, which reads as two services.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# The ROOT logger stays at WARNING. It used to be INFO, which switched on INFO
# for every library loaded, not for ours: that is where the noise came from.
# Only our own logger follows LOG_LEVEL.
logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT)
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

# The shape of the HTTP transport, and why it is a knob at all.
#
# Calls sent in the SAME batch fall over: measured on 2026-08-13, eight at once
# lost two — the third and the fourth, adjacent — while four at once lost none,
# and the retry always passes. The failing calls were `reference_guide`, which
# reads a file out of the image: no vault, no git, no lock, no disk, no thread
# pool. So it is not the work, it is the NUMBER, and the layer under the tools
# is the transport. The client's own message names neither, and misleads:
# "This connector's server hostname doesn't resolve or isn't reachable from
# this network. The connector may be misconfigured."
#
# In stateful mode — every version until this one — concurrent requests share
# one MCP session. In stateless mode fastmcp serves each request on its own
# transport: no `initialize` handshake, no `Mcp-Session-Id`, and a POST that
# names a tool is answered on its own. Measured against fastmcp 3.4.5 outside
# the image: stateful answers a session-less tool call with 400 "Bad Request:
# Missing session ID" and hands out an `mcp-session-id` header; stateless
# answers the same call 200 and hands out no header. The GET stream, which
# only exists to carry server-initiated notifications, is 400 in stateful and
# 405 in stateless — the route simply has no GET.
#
# It is a HYPOTHESIS, not a diagnosis, and this variable is the instrument to
# test it: with the default unchanged the service behaves exactly as before,
# and the mode can be flipped on an installed container without a new image,
# which is what makes before/after a comparison of one variable instead of two
# builds. fastmcp reads its own FASTMCP_STATELESS_HTTP for the same setting;
# the value is passed EXPLICITLY at mcp.run() below so that ours wins and the
# startup line cannot describe a mode nobody is running.
HTTP_MODE, _MODE_REJECTED = http_mode_from_env()
if _MODE_REJECTED:
    log.warning("HTTP_MODE=%r is not %s — using %s",
                _MODE_REJECTED, " or ".join(HTTP_MODES), HTTP_MODE)

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

# The icon, and what it does and does not buy.
#
# WHERE IT IS SHOWN TODAY: the OAuth consent page, which is the page seen when
# the connector is added or reconnected. fastmcp reads it there —
# `oauth_proxy/consent.py` takes `icons[0].src` and hands it to the logo — so
# this replaces FastMCP's own logo with ours. The page's default CSP is
# `img-src https: data:`, which is why an https URL is enough and no data URI
# is needed.
#
# WHERE IT IS NOT SHOWN, and this is not our defect: the connector list in
# Claude, which ignores `serverInfo.icons` entirely. The spec has carried the
# field since revision 2025-11-25 (SEP-973); the client does not read it yet
# (anthropics/claude-ai-mcp#152, open). Serving /favicon.ico and putting a
# <link rel="icon"> on a root page were both tried by others and are ignored
# too, so there is nothing here left to try. The icon there appears to be
# derived from the DOMAIN, which under a Funnel is *.ts.net and therefore
# Tailscale's — not something this file can reach.
#
# It is set anyway because it costs one argument, it wins the consent page now,
# and the day the client starts reading the field the list follows with no
# change here.
#
# THE URL IS NOT A SECOND COPY: it is the same one the Unraid template uses for
# the container icon, and a static check compares the two rather than trusting
# them to stay equal.
ICON_URL = ("https://raw.githubusercontent.com/alcor6502/archivist-mcp"
            "/main/archivist-icon.png")

mcp = FastMCP("archivist-mcp", auth=auth,
              icons=[Icon(src=ICON_URL, mimeType="image/png",
                          sizes=["256x256"])])

# A malformed call must not print what it carried. fastmcp validates arguments
# BEFORE the tool runs, so such a call never reaches `tool` below and leaves no
# refusal line of ours; what it does leave is fastmcp's own warning, which
# includes pydantic's `input` — for a call-validation failure, the WHOLE
# argument dict. For this server the arguments ARE the stored data: `content`,
# `text`, both halves of an `edit_file`. It happened on 2026-08-10 and put two
# spans of a document in the container log.
#
# HERE, and not earlier, because the filter goes on fastmcp's HANDLERS and
# fastmcp installs them when it configures its logging — which the line above is
# what triggers. Called too soon it would find nothing to arm, and it RAISES in
# that case rather than reporting zero: arming nothing is not a harmless
# outcome, it means the payload is still being printed. Letting that stop the
# boot is the point.
arm_argument_redaction()

# And the same lines get a clock. fastmcp's logger obeys no format of ours —
# the sentence above, read the other way round — so its records came out as
# "WARNING: Invalid arguments for tool 'x'": no date, no time, no service name.
# A line without a time correlates with nothing, and that is not theoretical:
# the investigation into calls dropping under concurrency wanted to know whether
# a malformed call had landed in the same second as a transport error, and could
# not ask. Same grip and same call site as the redaction, so if one of the two
# ever finds nothing to arm they both fail, loudly, at boot.
arm_timestamps(LOG_FORMAT)


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
    """Is the vault alive, and which datasets exist. Start here."""
    return vault.status(VERSION)


@tool
def reference_guide(name: str = "") -> dict:
    """The manual. Empty: the model. A command name: that command's card.
    No key needed."""
    # The behaviour is vault.guide_for, not here: this file cannot be imported
    # without fastmcp, so anything written in it is out of reach of the suite.
    # What stays is what only this layer can do — read the file, and stamp the
    # version.
    try:
        text = _GUIDE.read_text(encoding="utf-8")
    except OSError as e:
        raise VaultError(f"guide not available in the image: {e}")
    return {"version": VERSION, **guide_for(text, name)}


@tool
def dataset_create(name: str) -> dict:
    """A new dataset: open, empty, with its own git."""
    return vault.create(name)


@tool
def dataset_drop(dataset: str, expected_manifest: str) -> dict:
    """Delete an open dataset and everything in it. Irreversible."""
    return vault.drop(dataset, expected_manifest)


# =========================== dataset level ===========================

@tool
def dataset_status(dataset: str, key: str = "") -> dict:
    """One dataset in detail: files, Trash, git, size, last commit."""
    return vault.open_by_name(dataset, key).status()


@tool
def list_files(dataset: str, path: str = "", key: str = "") -> dict:
    """Files under `path` with size and sha256, recursive."""
    ds, rel = vault.open(dataset, path, key)
    return ds.list_files(rel)


@tool
def read_file(dataset: str, path: str, key: str = "") -> dict:
    """Read a text file: content plus sha256."""
    ds, rel = vault.open(dataset, path, key)
    return ds.read_file(rel)


@tool
def append(dataset: str, path: str, text: str, expected_sha256: str = "",
           key: str = "") -> dict:
    """Add a block to the end of an existing file. No sha needed; pass
    expected_sha256 to make a retry after a lost response safe (CAS)."""
    ds, rel = vault.open(dataset, path, key)
    return ds.append(rel, text, expected_sha256)


@tool
def write_file(dataset: str, path: str, content: str,
               expected_sha256: str, key: str = "") -> dict:
    """Write the WHOLE file. CAS on expected_sha256, "new" if new."""
    ds, rel = vault.open(dataset, path, key)
    return ds.write_file(rel, content, expected_sha256)


@tool
def edit_file(dataset: str, path: str, old_text: str, new_text: str,
              expected_sha256: str, key: str = "") -> dict:
    """Replace old_text — exactly one occurrence — with new_text. CAS."""
    ds, rel = vault.open(dataset, path, key)
    return ds.edit_file(rel, old_text, new_text, expected_sha256)


@tool
def move_path(dataset: str, src: str, dst: str, key: str = "") -> dict:
    """Move, rename or trash. There is no delete tool: Trash/ is it."""
    # src and dst are relative to the SAME dataset, so a move across datasets is
    # no longer expressible: the runtime check that used to catch it is gone,
    # replaced by the shape of the call itself.
    ds, rel_s = vault.open(dataset, src, key)
    rel_d = vault.check_path(ds.name, dst)
    return ds.move_path(rel_s, rel_d)


@tool
def search(dataset: str, pattern: str, path: str = "",
           regex: bool = False, key: str = "") -> dict:
    """Grep the dataset server-side. Literal unless regex=True."""
    ds, rel = vault.open(dataset, path, key)
    return ds.search(pattern, rel, regex)


@tool
def manifest(dataset: str, path: str = "", key: str = "") -> dict:
    """Fingerprint of a whole tree in one number."""
    ds, rel = vault.open(dataset, path, key)
    return ds.manifest(rel)


@tool
def archive(dataset: str, path: str = "", pattern: str = "*.md",
            max_chars: int = 0, key: str = "") -> dict:
    """Every file matching `pattern`, in one call, as a base64 tar.gz.
    READ reference_guide('archive') FIRST — its defaults and its
    ceilings hand you less than you asked for, silently."""
    ds, rel = vault.open(dataset, path, key)
    return ds.archive(rel, pattern, max_chars)


@tool
def read_binary(dataset: str, path: str, key: str = "") -> dict:
    """Read any file (PDF, binary) as base64 plus sha256."""
    ds, rel = vault.open(dataset, path, key)
    return ds.read_binary(rel)


@tool
def write_binary(dataset: str, path: str, content_base64: str,
                 expected_sha256: str, key: str = "") -> dict:
    """Write a binary file from base64. Same CAS as write_file."""
    ds, rel = vault.open(dataset, path, key)
    return ds.write_binary(rel, content_base64, expected_sha256)


@tool
def read_at(dataset: str, path: str, rev: str, key: str = "") -> dict:
    """Read a text file as it was at a past revision."""
    ds, rel = vault.open(dataset, path, key)
    return ds.read_at(rel, rev)


@tool
def history(dataset: str, path: str = "", n: int = 10, key: str = "") -> dict:
    """The last n commits of the dataset, or of one file."""
    ds, rel = vault.open(dataset, path, key)
    return ds.history(rel, n)


@tool
def diff(dataset: str, rev_a: str, path: str = "", rev_b: str = "HEAD",
         key: str = "") -> dict:
    """Differences between two revisions."""
    ds, rel = vault.open(dataset, path, key)
    return ds.diff(rev_a, rev_b, rel)


@tool
def dataset_restore(dataset: str, rev: str, expected_manifest: str, key: str = "") -> dict:
    """Rewrite EVERY file back to `rev`. Commits forward, so undoable."""
    ds = vault.open_by_name(dataset, key)
    return ds.restore(rev, expected_manifest)


@tool
def trash_purge(dataset: str, before: str, key: str = "") -> dict:
    """Empty Trash/ of what was trashed before an ISO date."""
    ds = vault.open_by_name(dataset, key)
    return ds.trash_purge(before)


if __name__ == "__main__":
    # The engine's version rides next to our own, and it is a cure, not a
    # decoration: two repositories pinning a third can pin different tags, and
    # "identical to the twin" quietly becomes "identical if both updated".
    # This line is where somebody already looks after every Apply.
    log.info("archivist-mcp %s (engine %s) — starting on %s:%s (http %s) — "
             "base_url %s — allowed user: %s — IP filter: %s — token store: %s "
             "— retention: %s",
             VERSION, ENGINE_VERSION, BIND_ADDR, PORT, HTTP_MODE,
             BASE_URL, ALLOWED_LOGIN,
             describe_cidrs(ALLOWED_CIDRS),
             os.environ.get("FASTMCP_HOME", "(default — NOT persistent!)"),
             f"{RETENTION} months" if RETENTION else "disabled")
    # The mode travels as a boolean because that is what fastmcp's signature
    # asks for (`stateless_http: bool | None` on run_http_async, forwarded by
    # run() through **transport_kwargs — read off fastmcp 3.4.5 itself, not a
    # blog). It is derived from HTTP_MODE and never written as a literal: a
    # literal here would freeze the mode while the log went on quoting the
    # variable, which is the exact shape of the BIND_HOST defect.
    mcp.run(transport="http", host=BIND_ADDR, port=PORT,
            stateless_http=HTTP_MODE == "stateless")
