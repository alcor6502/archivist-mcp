"""A malformed call must not write the payload into the log.

THE DEFECT, MEASURED. In fastmcp 3.4.5, a call whose arguments fail validation
never reaches the tool. `FastMCP._call_tool` catches the pydantic error and
logs it (`fastmcp/server/server.py:1325`):

    detail = cause.errors(include_url=False) ...
    logger.warning("Invalid arguments for tool %r: %s", name, detail)

`errors()` includes an `input` key, and for a call-validation failure `input`
is THE WHOLE ARGUMENT DICT. One missing parameter and every other argument is
printed. Observed in production on 2026-08-10: a call to `edit_file` without
`expected_sha256` put `old_text` and `new_text` — two spans of a stored
document — into the container log, in full.

WHY THAT IS WORSE THAN IT LOOKS FOR THESE SERVERS. The arguments of a server
that stores things ARE the things it stores: `write_file(content=...)`,
`append(text=...)`, a rule body, a document. The same log had uvicorn's access
line deliberately silenced because a *path* carries dataset and document names
— this is not the path, it is the contents. And the log of a server holding a
protected dataset is not itself protected.

It also arrives where nothing of ours can see it. The record is made on
fastmcp's own logger, which sets `propagate = False` and installs its own
handler and formatter, so it obeys neither `LOG_LEVEL` nor any format of ours.
And validation runs BEFORE the tool, so the call never reaches `make_tool`:
there is no refusal line of ours because, as far as the server is concerned,
nothing happened.

WHY A FILTER AND NOT A SILENCE. Turning the logger down would take the payload
away and the fact with it, and a refused call that leaves no trace is the
bargain `refusals.py` exists to refuse. What is worth keeping is exactly what
diagnoses: which tool, which argument, what is wrong with it. What is worth
dropping is the one key that carries the caller's data. So:

    Invalid arguments for tool 'edit_file':
      [{'type': 'missing_argument', 'loc': ('expected_sha256',),
        'msg': 'Missing required argument', 'input': '<redacted>'}]

WHERE IT HAS TO BE ATTACHED, WHICH IS THE PART THAT LOOKS WRONG. A filter set
on a LOGGER runs only for records made on that logger — a child's records are
not filtered by an ancestor logger, only by the ancestor's HANDLERS as they
propagate. The message above is made on `fastmcp.server.server` and printed by
the handler on `fastmcp`. So a filter added to `logging.getLogger("fastmcp")`
would be perfectly installed and would never fire once.

It therefore goes on the handlers, and `arm_argument_redaction` REFUSES when it
finds none rather than reporting success: fastmcp attaches its handler in
`configure_logging`, and a filter installed before that call, or after a
reconfiguration that replaced the handlers, protects nothing at all. A guard
that silently protects nothing is the failure mode this whole package keeps
being paid to avoid, so it is spelled as an exception the caller has to see at
startup.

NOTE ON THE BAR THIS DOES NOT MEET. `README.md` asks that code arrive here only
once **more than one server has already written it**. Neither has written this:
it is a cure, not a consolidation. It is proposed here because the defect is
not either server's — it belongs to the way both of them call the same pinned
version of fastmcp, and fixing it twice, weeks apart, is the habit this
repository exists to end. If the other side would rather keep it product-side,
that is a reasonable answer and this module comes back out.

AND THE SAME LINE MUST CARRY A TIME — the second cure in this module, and it
rests on the same sentence as the first. fastmcp's logger sets
`propagate = False` and installs its own handler and formatter, so its records
obey no format of ours:

    WARNING: Invalid arguments for tool 'vault_status': [{'type': ...}]

while every line of ours reads `2026-08-13 18:40:06,745 INFO archivist-mcp: …`.
No date, no time, no service name. That was known — it is written four
paragraphs up — and was left alone for a long time because it looked cosmetic.

WHAT MADE IT NOT COSMETIC IS A PRICE, PAID. A line without a time correlates
with nothing. On 2026-08-13 an investigation into calls dropping under
concurrency needed to know whether a malformed call had landed in the same
second as a transport error, and could not ask the question at all: one of the
two lines had no clock on it. The defect was old; what was new was the cost,
and that is the only thing that moves a cure from "would be nice" to "is done".

WHY THE FORMATTER AND NOT `propagate = True`. Handing fastmcp's records to our
handlers by switching propagation on and removing its own would read cleaner in
the result, and it changes WHERE the records go — which is a bigger promise
than changing how they look. Two ways it goes wrong quietly: if fastmcp
reconfigures its logging afterwards it reinstalls a handler and every line
comes out twice; and if the root logger is where our format lives, everything
depends on a level that `server.py` deliberately keeps at WARNING. The
formatter is the smaller claim — same grip as the filter above, same call site,
same failure mode already understood — and the line comes out once.

THE FORMAT IS NOT DECIDED HERE. `arm_timestamps` has no default format: the
caller passes the one it already gave `logging.basicConfig`, so there is one
string in one place per server instead of two that agree until somebody edits
one. It also leaves the twin free to have its own shape without this module
having an opinion about it.
"""

import logging

REDACTED = "<redacted>"

# The one key pydantic fills with the caller's own data. Everything else in an
# error entry — type, loc, msg, ctx — describes the RULE that was broken, and
# describing a rule cannot leak what was sent.
_CARRIES_THE_PAYLOAD = "input"


def _redacted(value):
    """A copy of `value` with every `input` key replaced.

    Total on purpose: no branch here can raise. Filters run inside
    `Logger.handle`, so an exception raised in one does not produce a bad log
    line, it propagates into the caller — turning a warning about a malformed
    call into a failure of the call that reported it.

    `input` is replaced WHOLE and never descended into, so recursion is bounded
    by the shape of the error list — which is flat — and not by the shape of
    the arguments, which is the caller's to choose and could be anything.

    THE TUPLE BRANCH IS LOAD-BEARING, and it does not look it. `record.args` is
    itself a tuple, and a `LogRecord` formats itself with `msg % args`: handing
    back a list turns every `%s` into a `TypeError` inside the handler, so the
    line does not merely read differently — it does not come out at all. This
    was found by injection, having first been written down as cosmetic.
    """
    if type(value) is dict:
        return {k: (REDACTED if k == _CARRIES_THE_PAYLOAD else _redacted(v))
                for k, v in value.items()}
    if type(value) is list:
        return [_redacted(v) for v in value]
    if type(value) is tuple:
        return tuple(_redacted(v) for v in value)
    return value


class ArgumentRedactingFilter(logging.Filter):
    """Strips the payload out of a record before it is formatted.

    It works on `record.args`, not on the message text, and it looks for the
    KEY rather than for the wording: the sentence "Invalid arguments for tool"
    belongs to fastmcp and may be reworded in any release, while `input` is
    pydantic's name for the field and is part of a much older contract. A
    filter keyed to the sentence would go quiet on an upgrade without failing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = _redacted(record.args)
        return True


def arm_argument_redaction(logger_name: str = "fastmcp") -> int:
    """Install the filter on the handlers of `logger_name`. Returns how many.

    Raises `RuntimeError` when there are none, because that is not a harmless
    no-op: it means the payload is still being printed by somebody else's
    handler. Call it AFTER the server object exists, which is what makes
    fastmcp configure its logging, and let the exception stop the boot.
    """
    logger = logging.getLogger(logger_name)
    armed = 0
    for handler in logger.handlers:
        if any(isinstance(f, ArgumentRedactingFilter) for f in handler.filters):
            continue
        handler.addFilter(ArgumentRedactingFilter())
        armed += 1
    if not armed and not any(
            isinstance(f, ArgumentRedactingFilter)
            for h in logger.handlers for f in h.filters):
        raise RuntimeError(
            f"logger {logger_name!r} has no handler to filter: argument "
            f"redaction would protect nothing — call this after the server "
            f"object exists, and check nothing reconfigured logging after it")
    return armed


class _StampedFormatter(logging.Formatter):
    """A formatter of ours, recognisable as such.

    The marker is the CLASS and not a flag written onto the handler: a handler
    replaced by a reconfiguration takes its formatter with it, so the mark
    disappears exactly when the formatting does, and a second arming re-does
    the work instead of believing an attribute that outlived the thing it
    described.
    """


def arm_timestamps(fmt: str, logger_name: str = "fastmcp",
                   datefmt: str | None = None) -> int:
    """Give `logger_name`'s handlers our line shape. Returns how many.

    `fmt` is required and comes from the caller — the same string it handed
    `logging.basicConfig` — because a default here would be a second copy of a
    format that already exists in each server, and two copies of a string agree
    only until one of them is edited.

    Raises `RuntimeError` when there is no handler, for the same reason its
    sibling does and with one addition: no handler here means the assumption
    both cures rest on — that fastmcp prints through its own handler, attached
    when the server object is built — is wrong, and the redaction next door is
    then protecting nothing either. Call it after the server object exists,
    and let the exception stop the boot.

    Idempotent: a handler already carrying this formatter with this format is
    left alone and not counted.
    """
    logger = logging.getLogger(logger_name)
    if not logger.handlers:
        raise RuntimeError(
            f"logger {logger_name!r} has no handler to format: its lines would "
            f"still come out without a date — call this after the server object "
            f"exists, and check nothing reconfigured logging after it")
    stamped = 0
    for handler in logger.handlers:
        current = handler.formatter
        if (isinstance(current, _StampedFormatter)
                and current._fmt == fmt and current.datefmt == datefmt):
            continue
        handler.setFormatter(_StampedFormatter(fmt, datefmt))
        stamped += 1
    return stamped
