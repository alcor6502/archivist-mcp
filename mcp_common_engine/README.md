# mcp_common_engine

The part of archivist-mcp that knows nothing about vaults: who may speak to
the server, how a refusal is told apart from a fault, how the startup checks
block, and what the log is allowed to say.

Nothing in this folder knows what the server is *for* — no datasets, no tool
names, no storage. That is what keeps it liftable: it was a repository of its
own until archivist-mcp 4.0.2, and it can become one again by copying the
folder and `test_engine.py`. Its history up to v1.2.0 is in the archived
repository, `github.com/alcor6502/mcp-common-engine`.

## What is in it

| Module | What it does |
|---|---|
| `gate.Gate` | The identity gate: a FastMCP middleware hooked on `on_request`. A request passes only if the token carries the allowed GitHub login and, when a filter is set, the source IP is inside the allowed ranges. Both refusals are the same generic `McpError` ("not authorised"), so a stranger never learns which door they hit; the log line, which only the operator sees, says which and why. |
| `refusals.make_tool` | Builds the `@tool` decorator. A *designed refusal* (wrong key, CONFLICT, a ceiling reached) becomes a `ToolError` plus ONE log line of ours; a *fault* (git failing, a short read) is left alone and rises with its traceback. A call that returns leaves one line too — tool, milliseconds, bytes, never a path. The server passes in the two exception classes and, optionally, a table of annotations (every tool must be in it) and a `finish` hook that shapes what travels. |
| `config` | Reads configuration in one place, so the service and its preflight cannot disagree: placeholder detection, the IP range list (`ALLOWED_CIDRS`), the log level. A malformed value raises instead of being skipped. |
| `preflight` | The harness of the startup checks: the `check` decorator, the results, the skip list. Every check **blocks**, and a check that crashes counts as failed. The checks themselves live in the server's `preflight.py`. |
| `logs.arm_argument_redaction` | Keeps a malformed call from printing what it carried. FastMCP validates arguments before the tool runs and logs the whole argument dict on failure — and here the arguments are the stored documents. |
| `logs.arm_timestamps` | Gives FastMCP's own log lines the same format as ours, so they carry a date, a time and the service name. |

Each module's docstring holds the reasoning: where the gate hooks and why not
one level narrower, why the refusal conversion cannot live in a middleware, why
converting without writing your own log line leaves *no* line. Read it before
changing anything.

## How the server uses it

```python
from mcp_common_engine import cidrs_from_env, log_level_from_env, check
from mcp_common_engine.gate import Gate
from mcp_common_engine.refusals import make_tool
from mcp_common_engine.logs import arm_argument_redaction, arm_timestamps

mcp = FastMCP(...)
arm_argument_redaction()          # after the server object exists
arm_timestamps(LOG_FORMAT)
tool = make_tool(mcp, log, refusal=VaultError, fault=VaultFault,
                 annotations=ANNOTATIONS, finish=_finish)   # the last two are optional
mcp.add_middleware(Gate(log=log, allowed_login=ALLOWED_LOGIN,
                        allowed_cidrs=ALLOWED_CIDRS))
```

**`Gate` and `make_tool` are not re-exported from the package root, on
purpose.** Importing `mcp_common_engine` must not drag FastMCP in: the
preflight has to be able to run — and to report — on an image where FastMCP is
missing or broken, which is one of the things it exists to catch. The suite
asserts it.

## Three things that look wrong and are not

- **The log filters go on FastMCP's *handlers*, not on its logger.** A filter
  on a logger never sees a child logger's records; installed the obvious way
  it would be present and fire zero times.
- **`arm_argument_redaction` and `arm_timestamps` raise when they find no
  handler**, instead of returning zero. Arming nothing means the payload is
  still being printed; letting that stop the boot is the point. What they
  cannot cover is a reconfiguration of logging *after* the arming.
- **In `Gate`, the `try` around the source IP covers the parse and nothing
  else.** The refusal is raised after its own log line. With the raise inside
  the `try`, an `McpError` would sail past the `except` and the refusal would
  happen correctly and unlogged.

## Measured against

`fastmcp==3.4.5` (pinned in `requirements-server.txt`), Python 3.12. The pin is
part of the contract: where a message is routed, where an exception is logged,
and which exception type survives the handshake are properties of that version,
established by experiment. Moving the pin means re-measuring `gate` and
`refusals`.

## The rules its checks encode

`test_engine.py` runs with no FastMCP installed: behaviour for `config` and the
preflight harness, a stubbed FastMCP for the factory, the syntax tree for the
gate. `test_vault.py` adds the checks on how the server wires it. Every rule
below was paid for at least once, with the suites green:

- **A check that reads source as text is satisfied by comments.** Where a
  guarantee depends on how the code is written, read the syntax tree.
- **Wiring is not working.** The right hook name with `return await
  call_next(ctx)` moved to the top switches the gate off. Pin *where* the
  go-ahead passes: once, and last.
- **Read the function that is actually registered**, not any function that
  contains the right pieces.
- **A name is what was bound last.** `tool` is bound exactly once, at module
  level, to a call of `make_tool`; a second `class Gate` or a `tool = mcp.tool`
  further down hands the running server the other one.
- **A threshold tolerates exactly what it should catch.** Equality, against
  something the file states about itself.
- **One number, one place.**
- **Every guarantee is proven by injection**: write the defect in, and the
  failure must *name* it. A check never seen failing is a decoration.
- **A check that selects on the property it guards is not a gate.**
- **A crash counts as a failure, never as a pass.**
