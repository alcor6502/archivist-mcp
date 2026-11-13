"""The identity gate: who may speak to this server at all.

Two filters, before anything else — the GitHub identity carried by the token,
and the source IP as the trusted proxy reports it.

WHERE IT HOOKS is the whole design, and it is deliberately not the narrowest
hook available. `on_call_tool` leaves the handshake outside, and the hole that
leaves is narrow but real: OAuth stops whoever is not authenticated, not
whoever authenticates with their OWN account. Such a stranger gets a valid
token and with it `tools/list` — every call is refused and no data leaves, but
the SHAPE of the surface does, and a surface that can be enumerated is one that
can be studied. `on_message` is one level wider, and that level is
notifications: fire-and-forget, no id, no channel to deliver a refusal on, so
raising there buys undefined behaviour in exchange for no surface at all.
`on_request` is the narrowest hook with DEFINED behaviour, which is not the
narrowest hook there is.

TWO THINGS IT DOES NOT COVER, established by experiment on fastmcp 3.4.5 rather
than assumed, and worth knowing before anyone concludes from the log that the
door is wider than it is. `ping` and `logging/setLevel` are answered by the
SDK's own default handlers and never build a middleware context at all: a
stranger refused at `initialize` still receives a session id and can keep those
two alive, silently. They read nothing, which is why this is a note and not a
hole. And a `tools/call` on a cold session is refused while FastMCP is still
resolving the tool, so the line that comes out says `tools/list`: the method
names the message the GATE saw, not always the one the caller sent.

THE REFUSALS ARE LOGGED, and that is contract and not comfort. Once the gate
covers the handshake, a refused stranger and a broken deployment produce the
same symptom at the client — "the connector will not connect" — and the log
line is the only thing that tells them apart. It goes out at WARNING, so a
service that has been told to be quiet about itself is not also quiet about
this.

THE REFUSAL IS AN McpError, and it was a plain ValueError until v1.1.0. fastmcp
3.4.5 turns only McpError into a protocol answer inside `on_initialize`: a
ValueError escaped, the SDK dressed it as `-32602 Invalid request parameters`
and logged it on the ROOT logger without the service name, so the client saw a
protocol error where a refusal belonged — the same defect the refusal decorator
exists to fix, one layer down. Three things about the cure were decided before
this repository existed and are kept:

- the message stays GENERIC. A refusal of identity must not tell a stranger
  which of the two filters turned them away, so both raise the same sentence.
  The log line, which only the operator sees, keeps saying which and why. What
  changed is the dress, not the content;
- the conversion happens on EVERY request, not only on `initialize`. One shape
  to explain and one shape to test, instead of a rule with an exception inside
  it — and `on_request` is the only hook here anyway, so "every request" is
  the whole of it;
- the code is `INVALID_REQUEST` (-32600), not the `-32602` the SDK used to
  improvise. Nothing is wrong with the parameters: the request itself is one
  this caller may not make.

THE IP BRANCH WAS RESTRUCTURED, and not for tidiness. It used to raise
`ValueError("origin not allowed")` INSIDE a `try` whose `except ValueError`
caught it, logged, and re-raised — so the log line for a refused origin was
produced by the except branch. Swapping the raise for an McpError there would
have sailed straight past that except and taken the log line with it, silently:
the refusal would still have happened, correctly dressed, with nothing written
down. The `try` now covers only what can genuinely raise — parsing the address —
and the refusal is raised once, after its own log line, like the identity one.
No AST check could have caught that: both shapes have two warnings, both name
`ctx.method`. It was found by reading, and it is written here because the next
person to touch this branch deserves to know what the old shape was for.
"""

import ipaddress

from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import INVALID_REQUEST, ErrorData
from mcp.shared.exceptions import McpError


class Gate(Middleware):
    """Identity and origin, checked before anything else runs.

    `allowed_cidrs` is the parsed form produced by `config.cidrs_from_env` —
    pairs of (network, description). An empty list means NO filter, which is
    the existing meaning of an empty variable and does not change here.
    """

    HOOK = "on_request"   # pinned by a static check: a typo here does not fail,
                          # it disables the gate in SILENCE, because the base
                          # class ships a pass-through default for every hook
                          # name that does exist.

    def __init__(self, *, log, allowed_login: str, allowed_cidrs) -> None:
        self.log = log
        self.allowed_login = allowed_login
        self.nets = [ipaddress.ip_network(c) for c, _ in allowed_cidrs]

    # One sentence for both filters, on purpose: see the module docstring. A
    # stranger learns that they were refused, never which door they hit.
    REFUSAL = "not authorised"

    def _refusal(self) -> McpError:
        return McpError(ErrorData(code=INVALID_REQUEST, message=self.REFUSAL))

    async def on_request(self, ctx: MiddlewareContext, call_next):
        tok = get_access_token()
        login = tok.claims.get("login") if tok and tok.claims else None
        if login != self.allowed_login:
            self.log.warning("refused %s: GitHub login %r is not %r",
                             ctx.method, login, self.allowed_login)
            raise self._refusal()
        if self.nets:
            req = get_http_request()
            src = (req.headers.get("x-forwarded-for", "").split(",")[0].strip()
                   or (req.client.host if req.client else ""))
            # The try covers the PARSE and nothing else. It used to wrap the
            # decision too, and the refusal was raised from the except branch —
            # see the module docstring for why that shape could not survive the
            # change to McpError.
            try:
                ip = ipaddress.ip_address(src) if src else None
            except ValueError:
                ip = None
            if ip is None or not any(ip in n for n in self.nets):
                self.log.warning("refused %s: source %r outside the allowed ranges",
                                 ctx.method, src)
                raise self._refusal()
        return await call_next(ctx)
