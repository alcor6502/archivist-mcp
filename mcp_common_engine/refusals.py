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
"""

import functools
import logging

from fastmcp.exceptions import ToolError


def make_tool(mcp, log, *, refusal, fault):
    """Build the `@tool` decorator for one server.

    `refusal` is the engine's ordinary error class — every instance of it is a
    refusal the server meant to give. `fault` is its subclass reserved for
    genuine faults, caught first and left alone.
    """

    def tool(fn):
        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except fault:
                raise
            except refusal as e:
                log.info("refused %s: %s", fn.__name__, e)
                raise ToolError(str(e), log_level=logging.INFO) from None
        return mcp.tool(guarded)

    return tool
