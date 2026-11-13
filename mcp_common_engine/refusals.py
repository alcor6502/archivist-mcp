"""A designed refusal must not read like a fault.

Left as an ordinary exception, every refusal the server was BUILT to give — a
wrong code, a stale version, a ceiling reached — comes out of FastMCP through
`logger.exception`: thirty lines of traceback through anyio and pydantic,
shaped exactly like a real fault. After a week of those nobody reads tracebacks
any more, and the next genuine fault arrives disguised as routine.

WHERE the conversion happens is the trap. In fastmcp 3.4.5 `call_tool` applies
the middleware chain OUTSIDE and logs INSIDE, delegating to itself with
`run_middleware=False`: by the time a middleware sees the exception,
`logger.exception` has already run. So the conversion cannot live in a
middleware. It lives in a decorator, and the decorator uses `functools.wraps` —
which is also what keeps the MCP contract intact, because name, docstring and
signature are what FastMCP builds the tool schema from.

THE CONVERSION ALONE MAKES NO LINE — it makes none. `ToolError` is logged by
FastMCP's own logger, and a server that sets `FASTMCP_LOG_LEVEL=WARNING`
discards an INFO record before printing it. Converting and stopping there does
not turn thirty lines into one: it turns them into nothing, and a refusal that
leaves no trace is a different bargain from the one being made. So the line is
the server's own, on the server's own logger, and it says more than FastMCP's
ever did — which tool, and why it refused.

THE FAULT BRANCH COMES FIRST, and the order is load-bearing. "Every error my
engine raises is a designed refusal" is a premise, not a fact, and it has been
false in practice: a full disk, a missing library, a directory that is suddenly
unwritable are FAULTS, and without a separate branch this decorator would take
one and print a line beginning with the word "refused" — the defect it exists
to close, inverted. So the engine raises a fault subclass of its refusal class,
and the subclass is caught FIRST and left alone, to rise with its traceback.
Python does not warn you when a subclass branch is placed second and therefore
never taken.

The boundary between the two is not how the message reads, it is WHOSE FAULT it
is. An operation that fails on input the caller chose stays a refusal, even
when the failure comes out of a library.

CONSEQUENCE FOR WHOEVER ADOPTS THIS. A server that used to define its own
`def tool` at module level, and pinned that with a static check — one
definition, never rebound — now writes `tool = make_tool(...)`, which is an
ASSIGNMENT, and an assignment is the very shape those checks were written to
forbid (`tool = mcp.tool` registers every tool naked, and no counter notices).
The check has to move with the code: it stops asserting "one `def tool`, never
rebound" and starts asserting "the name `tool` is bound exactly once, at module
level, to a call of `make_tool` from this package". That check must be written
BEFORE the move, not after.

ONE LINE PER CALL, since 1.3.0, and what is NOT in it is the design. A call
that returns leaves `call <tool>: <ms> ms, <bytes> bytes` at INFO: which tool,
how long the work took on this side, how much went back. No argument and no
path, ever — uvicorn's access log is kept off in the consumers precisely
because a path names a document, and this line must not become the same
record by another road. It exists because nothing else says where the time of
a call goes: with it, eight lines in one second are eight calls that arrived
together, and a slow chat can be told from a slow server. A refusal keeps its
own line and gets no second one; a fault keeps its traceback.

ANNOTATIONS are the consumer's table, name → whatever its framework wants,
and a tool that is not in the table does not register: the import dies naming
it. A table with a hole would otherwise hand the client the protocol's
defaults for that tool — "not read-only, destructive, open world" — which is
the most alarming description there is, given to whichever tool was added
last.
"""

import functools
import json
import logging
import time

from fastmcp.exceptions import ToolError


def json_bytes(result) -> int:
    """How much a result weighs on the wire, near enough: its JSON, in bytes.
    A result that will not serialise still gets a line — zero, not a crash in
    the one place that must never be the reason a call fails."""
    try:
        return len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def make_tool(mcp, log, *, refusal, fault, annotations=None, finish=None):
    """Build the `@tool` decorator for one server.

    `refusal` is the engine's ordinary error class — every instance of it is a
    refusal the server meant to give. `fault` is its subclass reserved for
    genuine faults, caught first and left alone.

    `annotations` maps a tool's name to what the framework is handed as its
    annotations; given, EVERY tool must be in it. `finish` turns what the
    function returned into (what travels, its size in bytes): it is where a
    server that returns pictures builds its content blocks and counts them.
    Left out, the result travels as it is and its size is its JSON.
    """

    def tool(fn):
        extra = {}
        if annotations is not None:
            if fn.__name__ not in annotations:
                raise LookupError(f"tool {fn.__name__!r} has no annotations: add it "
                                  "to the table handed to make_tool")
            extra["annotations"] = annotations[fn.__name__]

        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            started = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except fault:
                raise
            except refusal as e:
                log.info("refused %s: %s", fn.__name__, e)
                raise ToolError(str(e), log_level=logging.INFO) from None
            wire, size = finish(result) if finish else (result, json_bytes(result))
            log.info("call %s: %d ms, %d bytes", fn.__name__,
                     round((time.monotonic() - started) * 1000), size)
            return wire
        return mcp.tool(guarded, **extra)

    return tool
