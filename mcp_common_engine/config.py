"""Configuration read in ONE place, so the service and its preflight can never
disagree about what the filter or the log level actually is.

Moved verbatim from codifier-mcp/preflight.py, where the two twins had written
it twice. `log_level_from_env` is codifier's shape rather than archivist's:
they behave identically today, but codifier resolves the alias FIRST and then
checks the list, so an alias pointing outside the list cannot slip through.
That is a difference in safety, not in style.
"""

import ipaddress
import os
import re

_SEPARATORS = re.compile(r"[\s._\-]")

# Not preceded by a letter: the word has to START here. Without that guard,
# "exchange mechanism" squeezes to "exchangemechanism", which contains
# "changeme" — and so does a perfectly legitimate https://exchange.me.ts.net.
# A check that refuses to start the service on a real value is worse than the
# hole it closes.
_PLACEHOLDER = re.compile(r"(?<![A-Za-z])(CHANGEME|CAMBIAMI)", re.IGNORECASE)

def is_placeholder(v: str) -> bool:
    """True if the value is still a template placeholder.

    Separators are stripped before matching, so CHANGE_ME, CHANGE-ME, CHANGE.ME
    and 'change me' are all recognised. The first version of this matched the
    literal string only, which made the guard depend on whoever wrote the
    template spelling it exactly right — a guard that holds until the day it is
    needed.

    Only separators are stripped, never / or :, so the word boundary at the
    start of the placeholder survives: that is what tells CHANGEME inside
    https://CHANGEME.your-tailnet.ts.net (caught, and it teaches the syntax
    while being caught) from the one hiding inside exchange (let through)."""
    return bool(_PLACEHOLDER.search(_SEPARATORS.sub("", v)))

DEFAULT_CIDRS = "160.79.104.0/21 # documented egress of the model provider"

def parse_cidrs(raw: str) -> list[tuple[str, str]]:
    """Parse an ALLOWED_CIDRS list into [(cidr, description), ...].

    Entries are separated by ';' and '#' opens a description that runs to the
    end of the entry:

        160.79.104.0/21 # Anthropic egress ; 100.64.0.0/10 # tailnet

    The separator is not a comma precisely so a description may contain one. An
    empty string yields [], which means NO filter.

    A malformed entry RAISES; it is never skipped. A filter wider or narrower
    than you believe is worse than a service that refuses to start, because it
    is the failure nobody notices. Empty entries between separators are
    tolerated: a trailing ';' cannot change what the filter means."""
    out: list[tuple[str, str]] = []
    for chunk in raw.split(";"):
        entry = chunk.strip()
        if not entry:
            continue
        net_s, _, desc = entry.partition("#")
        net_s, desc = net_s.strip(), desc.strip()
        if not net_s:
            raise ValueError(f"entry with a description but no network: {entry!r}")
        try:
            net = ipaddress.ip_network(net_s, strict=True)
        except ValueError as e:
            raise ValueError(f"{net_s!r} is not a valid CIDR ({e})")
        out.append((str(net), desc))
    return out

def cidrs_from_env() -> list[tuple[str, str]]:
    """The IP filter as configured, resolved in ONE place.

    ALLOWED_CIDRS wins when it is DEFINED, even if empty — "defined and empty"
    means the filter is off, and is not the same thing as "not defined". The
    deprecated ANTHROPIC_CIDR is still honoured, so a container updated without
    touching its template keeps working exactly as before: a new variable is
    always born optional.

    server.py and preflight must never answer this question differently, which
    is why they both come here."""
    raw = os.environ.get("ALLOWED_CIDRS")
    if raw is None:
        raw = os.environ.get("ANTHROPIC_CIDR")      # deprecated, still supported
    if raw is None:
        raw = DEFAULT_CIDRS
    return parse_cidrs(raw)

LOG_LEVELS = ("INFO", "WARNING")

# WARN is not a typo: Python's logging module defines it as an alias of WARNING
# and setLevel("WARN") does not raise. Whoever writes it wants LESS noise, so
# falling back to INFO would give them MORE — the one value outside the list
# where the intention is unambiguous and the fallback would go the wrong way.
_ALIASES = {"WARN": "WARNING"}

def log_level_from_env() -> tuple[str, str | None]:
    """The log level as configured, resolved in ONE place only, so the service
    and anything else that needs to know read the same expression rather than
    two that agree today. (Unlike cidrs_from_env, which is also reported by a
    preflight check, this one currently has a single caller: server.py. It lives
    here because it is the same KIND of thing, not because the preflight prints
    it — it does not.)

    Returns the level to use and, when the value had to be corrected, the value
    that was given, so the caller can say so out loud. A knob that ignores you
    in silence is how you get accused of having broken it. An ABSENT or empty
    value is not a correction and is not reported: not choosing is not the same
    gesture as choosing wrong.

    Why correct instead of raising: logging.setLevel() raises on an unknown
    level, and it runs at IMPORT — that is, AFTER the preflight has printed a
    clean sheet. It is the worst place in the whole startup for a typo to land:
    every check says fine and the container dies immediately afterwards. The
    template offers a closed dropdown, but a container built by hand has no
    template at all, and the field is optional — "defined and empty" is a
    gesture people actually make.

    The list is closed at INFO and WARNING because the two ends are closed for
    different reasons. Below INFO there is nothing to switch on: this code
    contains no debug-level call at all, so DEBUG behaves exactly like INFO,
    and a knob that does nothing gets read as a knob that is broken. Above
    WARNING the gate's refusals disappear, and since v1.2 the gate covers the
    handshake — that line is the only thing separating a stranger turned away
    from a broken deployment."""
    given = os.environ.get("LOG_LEVEL", "").strip().upper()
    if not given:
        return "INFO", None
    resolved = _ALIASES.get(given, given)
    if resolved in LOG_LEVELS:
        return resolved, None
    return "INFO", given

def describe_cidrs(parsed: list[tuple[str, str]]) -> str:
    """What was UNDERSTOOD, not what was given. The way this breaks is mute: a
    comma in place of a semicolon and a range disappears without a word."""
    if not parsed:
        return "OFF (no IP filter)"
    n = len(parsed)
    body = ", ".join(f"{c} ({d})" if d else c for c, d in parsed)
    return f"{n} range{'s' if n != 1 else ''} — {body}"
