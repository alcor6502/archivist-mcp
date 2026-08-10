"""
test_vault.py — engine test suite, no network and no FastMCP required.

    python3 test_vault.py

Builds a throwaway vault in a temporary directory, exercises every important
path and prints a verdict. The checks that must FAIL matter as much as the ones
that must pass: most of this service's safety lives in things that must not
happen.

The last section is a static consistency check between server.py and vault.py:
the tools are not exercised here (that would need FastMCP and a network), so
this at least proves that every engine method server.py calls exists, with a
compatible signature. It is the gap the runtime tests cannot cover.
"""
from __future__ import annotations
import ast
import inspect
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from vault import VaultRoot, Dataset, VaultError  # noqa: E402

OK = FAIL = 0
KEY = "k7m2xq4p"


def ok(cond, label, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {extra}")


def must_fail(label, fn):
    global OK, FAIL
    try:
        fn()
        FAIL += 1
        print(f"  FAIL  {label}: did NOT fail")
    except VaultError:
        OK += 1
        print(f"  PASS  {label} (refused)")


def cidr_checks() -> None:
    """The IP filter parser, in BOTH directions.

    The half that matters most is the one proving that legitimate forms are
    accepted: a filter that refuses to start on a real configuration is worse
    than the hole it closes. The other half proves that a malformed entry is
    REFUSED rather than quietly skipped — a range that disappears in silence is
    exactly the failure this check exists to prevent."""
    from preflight import parse_cidrs, cidrs_from_env, DEFAULT_CIDRS

    one = parse_cidrs("160.79.104.0/21")
    ok(one == [("160.79.104.0/21", "")], "a bare entry, the form used until now", one)

    two = parse_cidrs("  160.79.104.0/21 # egress  ;100.64.0.0/10#tailnet  ")
    ok(two == [("160.79.104.0/21", "egress"), ("100.64.0.0/10", "tailnet")],
       "two entries, descriptions, ragged spacing", two)

    comma = parse_cidrs("100.64.0.0/10 # tailnet, and the web UI on it")
    ok(comma == [("100.64.0.0/10", "tailnet, and the web UI on it")],
       "a comma inside a description does not split the entry", comma)

    ok(parse_cidrs("10.0.0.0/8 ; ") == [("10.0.0.0/8", "")],
       "a trailing separator is tolerated")

    for bad in ("not-a-cidr", "160.79.104.0/99", "160.79.104.5/21", "# only a description"):
        try:
            parse_cidrs(bad)
            global FAIL
            FAIL += 1
            print(f"  FAIL  malformed entry {bad!r} was ACCEPTED")
        except ValueError:
            global OK
            OK += 1
            print(f"  PASS  malformed entry {bad!r} refused")

    saved = {k: os.environ.get(k) for k in ("ALLOWED_CIDRS", "ANTHROPIC_CIDR")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        ok(cidrs_from_env() == parse_cidrs(DEFAULT_CIDRS),
           "neither variable defined: the usual default")

        os.environ["ANTHROPIC_CIDR"] = "10.0.0.0/8"
        ok(cidrs_from_env() == [("10.0.0.0/8", "")],
           "ALLOWED_CIDRS undefined: the deprecated name still works")

        os.environ["ALLOWED_CIDRS"] = ""
        ok(cidrs_from_env() == [], "ALLOWED_CIDRS defined EMPTY: filter off")

        os.environ["ALLOWED_CIDRS"] = "192.168.0.0/16 # lan"
        ok(cidrs_from_env() == [("192.168.0.0/16", "lan")],
           "ALLOWED_CIDRS defined: it wins over the deprecated name")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def static_api_check() -> None:
    """Parse server.py and verify that every ds.<method>() / vault.<method>()
    call resolves to a real method on Dataset / VaultRoot, with an arity the
    call satisfies. Catches a rename done on one side only."""
    src = (HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    targets = {"vault": VaultRoot, "ds": Dataset, "ds_s": Dataset, "ds_d": Dataset}
    seen = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name) or owner.id not in targets:
            continue
        cls, meth = targets[owner.id], node.func.attr
        seen += 1
        fn = getattr(cls, meth, None)
        if fn is None or not callable(fn):
            ok(False, f"server.py calls {owner.id}.{meth}()", "no such method on the engine")
            continue
        params = [p for p in inspect.signature(fn).parameters if p != "self"]
        required = [p for p, v in inspect.signature(fn).parameters.items()
                    if p != "self" and v.default is inspect.Parameter.empty]
        given = len(node.args) + len(node.keywords)
        if given < len(required) or given > len(params):
            ok(False, f"{owner.id}.{meth}() arity",
               f"{given} arguments passed, expected {len(required)}..{len(params)}")
        else:
            ok(True, f"{owner.id}.{meth}() exists with a compatible signature")
    ok(seen >= 15, "static check covered the engine calls in server.py", f"only {seen} found")


def single_door_check() -> None:
    """Every tool that takes a path must reach the engine THROUGH the vault.

    The refusal of a dataset-prefixed path lives in VaultRoot, not in Dataset:
    `Dataset.move_path("a.md", "X/y.md")` called directly would happily create
    a nested folder. That is fine — the engine is the primitive — but it means
    the guarantee rests on server.py routing every path through `vault.open`
    (and `vault.check_path` for a second path in the same call). Nothing at
    runtime would notice a tool that skipped it, so this reads the source and
    checks. It is the same trade as the arity check above: cover the gap the
    runtime tests cannot see."""
    tree = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))
    checked = 0
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        args = [a.arg for a in fn.args.args]
        pathish = [a for a in args if a in ("path", "src", "dst")]
        if not pathish:
            continue
        calls = {n.func.attr for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "vault"}
        checked += 1
        ok("open" in calls, f"{fn.name}() resolves its path through vault.open()", sorted(calls))
        # A second path in the same call cannot come out of vault.open: it has
        # to be validated on its own, or it slips past the whole check.
        if len(pathish) > 1:
            ok("check_path" in calls,
               f"{fn.name}() validates its second path through vault.check_path()",
               sorted(calls))
        ok("dataset" in args, f"{fn.name}() names its dataset explicitly", args)
    ok(checked >= 14, "the single-door check covered the path-taking tools", checked)


def lock_discipline_check() -> None:
    """Everything that COMMITS does it under the dataset lock.

    Until 2.4.0 this was a rule with two exceptions, `ensure_git` and
    `prune_history`, and the argument for both was the same and sounded good:
    they run at boot, so nothing else is going on. That is a fact about today's
    CALLERS, not about the functions — and it is the kind of fact that stops
    being true without anyone touching the function that relied on it. A rule
    with exceptions cannot be checked; this one now has none, so it can.

    Reads are deliberately NOT required to lock. `status`, `history`, `diff`
    and the rest would gain nothing and would lose the property that a reader
    never waits behind a writer.

    The check is positional, not lexical: it is not enough that the method
    mentions the lock somewhere. Every mutating call must be INSIDE the `with`,
    because taking the lock for one half of a method and committing outside it
    is precisely the shape of the bug — and it reads, at a glance, like the
    correct code."""
    src = (HERE / "vault.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ds = next((n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "Dataset"), None)
    ok(ds is not None, "vault.py still declares a Dataset class")
    if ds is None:
        return

    # git verbs that CHANGE the repository. Anything read-only — status, log,
    # rev-list, rev-parse, cat-file — is absent on purpose.
    WRITES = {"init", "add", "commit", "commit-tree", "rebase", "reset",
              "read-tree", "checkout", "reflog", "gc", "am", "cherry-pick"}
    # The primitives ARE the operation; they are called from inside the lock by
    # everything above them, and requiring them to take it too would deadlock —
    # flock is per open file description, and _lock() opens a new one each time.
    PRIMITIVES = {"_git", "_commit", "_commit_external_if_dirty", "_lock",
                  "_atomic_write"}

    def mutating_calls_outside_the_lock(fn) -> list[str]:
        covered = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.With) and any(
                    isinstance(i.context_expr, ast.Call)
                    and isinstance(i.context_expr.func, ast.Attribute)
                    and i.context_expr.func.attr == "_lock"
                    for i in node.items):
                for sub in ast.walk(node):
                    covered.add(id(sub))
        bad = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name) and f.value.id == "self"):
                continue
            what = None
            if f.attr in ("_commit", "_commit_external_if_dirty"):
                what = f.attr
            elif (f.attr == "_git" and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and node.args[0].value in WRITES):
                what = f"_git({node.args[0].value!r})"
            if what and id(node) not in covered:
                bad.append(what)
        return bad

    writers = 0
    for fn in ds.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in PRIMITIVES:
            continue
        bad = mutating_calls_outside_the_lock(fn)
        touches = bad or any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"
            and (n.func.attr in ("_commit", "_commit_external_if_dirty")
                 or (n.func.attr == "_git" and n.args
                     and isinstance(n.args[0], ast.Constant)
                     and n.args[0].value in WRITES))
            for n in ast.walk(fn))
        if not touches:
            continue
        writers += 1
        ok(not bad, f"Dataset.{fn.name} commits only under the lock", bad)
    # If a refactor ever moves the writers out of this class, every check above
    # would pass by finding nothing. Count them, so silence has to be earned.
    ok(writers >= 6, "the check found the writers it is meant to police", writers)


def gate_hook_check() -> None:
    """The Gate is wired by NAMING a hook, and that is the whole danger: the
    Middleware base class ships a pass-through default for every hook it knows,
    so `on_requst` — one letter short — is not an error. It is a method nobody
    ever calls, and the gate is off. Nothing fails, nothing logs, and the server
    happily answers a stranger. There is no runtime test that would notice,
    because the tests never build a FastMCP server: this reads the source.

    Two things are pinned, and they are different things. `HOOK` pins the
    DECISION — `on_request`, chosen in v2.1 over the narrower `on_call_tool`
    (which let a stranger enumerate the tools) and over the wider `on_message`
    (which also covers notifications, where raising has no channel to answer
    on). The method set pins the WIRING: exactly one hook, and its name equal
    to HOOK. Change the decision and this test has to be changed too, on
    purpose — which is the point."""
    tree = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))
    gate = next((n for n in tree.body
                 if isinstance(n, ast.ClassDef) and n.name == "Gate"), None)
    if gate is None:
        ok(False, "server.py defines the Gate middleware")
        return

    declared = next((s.value.value for s in gate.body
                     if isinstance(s, ast.Assign)
                     and any(getattr(t, "id", "") == "HOOK" for t in s.targets)
                     and isinstance(s.value, ast.Constant)), None)
    ok(declared == "on_request", "Gate.HOOK pins the decision: on_request", declared)

    hooks = {n.name for n in gate.body
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
             and n.name.startswith("on_")}
    ok(hooks == {declared}, "the Gate hooks exactly what HOOK names", sorted(hooks))

    # Read from the AST, never from the text. `"mcp.add_middleware(Gate())" in
    # src` stays green on `#mcp.add_middleware(Gate())` — which is the single
    # most likely way that line ever disappears: somebody comments it out while
    # chasing something else. A comment does not exist in the tree.
    registered = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == "mcp.add_middleware"
                  and n.args and ast.unparse(n.args[0]) == "Gate()"]
    ok(len(registered) == 1, "the Gate is registered, exactly once", len(registered))

    hook = next((n for n in gate.body
                 if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                 and n.name == declared), None)
    ok(hook is not None, f"the {declared} hook is there to be read")
    if hook is not None:
        # Wiring is not working. Every name above can be right and the gate
        # still be off: `return await call_next(ctx)` moved to the TOP of the
        # hook lets everything through with the body sitting there, unreachable
        # and reassuring. So the go-ahead is pinned by POSITION: once, and last.
        passes = [n for n in ast.walk(hook) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == "call_next"]
        ok(len(passes) == 1, "the hook lets a request through in exactly one place",
           len(passes))
        last = hook.body[-1]
        ok(isinstance(last, ast.Return) and "call_next" in ast.unparse(last),
           "and it is the LAST thing it does, after both filters",
           ast.unparse(last).split("\n")[0])

        # A refused request and a broken deployment look identical at the
        # client. The log line is the only thing that tells them apart, so it
        # is part of the contract, not of the comfort. Counted on the tree, for
        # the same reason as above: a commented-out call would satisfy text.
        warns = [n for n in ast.walk(hook) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == "log.warning"]
        ok(len(warns) >= 2,
           "both refusals are logged, identity and origin", len(warns))


def tool_conversion_check() -> None:
    """Every tool goes through the @tool decorator, and the decorator really
    converts.

    This is the same trade as the single-door check: the guarantee rests on how
    server.py is WRITTEN, and nothing at runtime would notice a tool that
    skipped it. One tool left on a bare @mcp.tool is one tool whose designed
    refusals still print twenty-five lines of traceback at ERROR — and being
    the one nobody thought about, it is the one that never gets tested. The
    log of 2026-Aug-09 is what this exists to keep from coming back: three
    refusals, three tracebacks, two of them CONFLICTs on edit_file, which is
    the CAS doing its job and being logged as a fault.

    The decorator's own shape is pinned too, because each piece of it is load
    bearing and each one is silent when removed:
      - `functools.wraps` is what keeps the MCP schema intact. FastMCP builds
        name, docstring and signature from the function and follows
        __wrapped__ to find them; without it the whole surface changes and
        every client has to reconnect.
      - `from None` is the point of the exercise: a chained traceback is
        exactly what we are taking out of the log.
      - `log_level` below ERROR is the other half. Converting to ToolError and
        leaving the level at ERROR would change the number of lines and
        nothing else.
      - our OWN log line, at INFO, because FastMCP's does not reach the
        container's log at all: the Dockerfile sets FASTMCP_LOG_LEVEL=WARNING,
        so an INFO record from fastmcp.server.server is dropped before it is
        printed. Without this line the change trades twenty-five lines for
        none. INFO and not WARNING is a decision, not a detail: WARNING is
        where the Gate logs a stranger turned away, and mixing a CONFLICT in
        there costs the one signal that tells a refused stranger from a broken
        deployment.

    A tool that lost its decorator ALTOGETHER is caught elsewhere and on
    purpose: it stops being a tool, so guide_signature_check() sees its
    signature still promised by the manual and names it. The two checks cover
    the two ways of falling out."""
    src = (HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # ONE `def tool`, and nowhere else in the file. A second one further down
    # wins for every function defined after it — and they are ALL defined after
    # it, so three lines left behind while chasing something else would empty
    # the whole MCP surface with the suite green.
    convs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == "tool"]
    ok(len(convs) == 1, "server.py defines the @tool decorator exactly once",
       len(convs))
    if not convs:
        return
    conv = convs[0]

    # WHAT it registers, and WHICH function that name really points at.
    #
    # Reading the decorator with ast.walk only asks that the pieces be WRITTEN
    # somewhere inside it. Move the try/except into a nested function nobody
    # calls, reduce the wrapper to `return fn(...)`, and every check below
    # passes on a conversion that does not exist. So the order is: find the
    # registration, take the NAME it hands over, and analyse THAT function.
    registers = [n for n in ast.walk(conv) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == "mcp.tool"]
    ok(len(registers) == 1, "and registers with mcp.tool exactly once", len(registers))
    registered = (ast.unparse(registers[0].args[0])
                  if registers and registers[0].args else "")
    ok(registered.isidentifier(),
       "handing it a function by name, not an expression", registered or "no argument")

    # And the name must not be re-pointed on the way. `guarded = fn` one line
    # above the return is this very check's own defect, wearing the name the
    # check looks for.
    rebound = [ast.unparse(t) for s in ast.walk(conv) if isinstance(s, ast.Assign)
               for t in s.targets if isinstance(t, ast.Name) and t.id == registered]
    ok(not rebound, f"and `{registered or '?'}` is never reassigned", rebound)

    inner = next((n for n in ast.walk(conv)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == registered), None)
    ok(inner is not None, f"and `{registered or '?'}` is a function defined inside it")
    if inner is None:
        return

    # From here on, everything is read out of the function that is REGISTERED.
    # One try, in its body — not somewhere in the subtree, which is how a dead
    # branch gets to answer for a live one.
    tries = [s for s in inner.body if isinstance(s, ast.Try)]
    ok(len(tries) == 1, "which wraps the call in exactly one try/except", len(tries))
    if not tries:
        return
    ok(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "fn"
           for n in ast.walk(tries[0].body[0] if tries[0].body else tries[0])),
       "and the thing inside the try is the original function")

    handlers = tries[0].handlers
    caught = [ast.unparse(h.type) if h.type is not None else "*" for h in handlers]
    ok("VaultError" in caught, "the decorator catches VaultError", caught)

    # The ORDER, which is the whole distinction and is invisible once written:
    # VaultFault SUBCLASSES VaultError, so `except VaultError` placed first
    # would swallow every fault into the quiet path and nothing would look
    # wrong. Python has no warning for this; the test is the warning.
    # The ORDER, which is the whole distinction and is invisible once written:
    # VaultFault SUBCLASSES VaultError, so `except VaultError` placed first
    # would swallow every fault into the quiet path and nothing would look
    # wrong. Python has no warning for this; the test is the warning. Taken
    # from the handlers of the ONE try above, not from the first match in the
    # subtree — a decoy handler written higher up would answer for it.
    ok(caught[:1] == ["VaultFault"],
       "and it catches VaultFault FIRST, or the subclass never gets its turn",
       caught)
    fault_h = [h for h in handlers if ast.unparse(h.type or ast.Constant(0)) == "VaultFault"]
    ok(fault_h and all(isinstance(s, ast.Raise) and s.exc is None
                       for h in fault_h for s in h.body),
       "and it lets a fault rise untouched: traceback at ERROR, as before 2.3.0")

    raised = [n for h in handlers for n in ast.walk(h)
              if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ToolError"]
    ok(bool(raised), "and re-raises it as ToolError")

    # Pinned to INFO, and on EVERY occurrence. `"ERROR" not in level` was a
    # substring search satisfied by CRITICAL, which is higher; `levels[0]` was
    # a first-match satisfied by a dead handler holding the right value while
    # the live one holds the wrong one. Both were real, both were found by the
    # twin reading this file.
    levels = [ast.unparse(k.value) for r in raised for k in r.keywords
              if k.arg == "log_level"]
    ok(levels and all(v == "logging.INFO" for v in levels),
       "at logging.INFO exactly, everywhere it is raised",
       levels or "log_level not set")

    # `raise X from None` parses as cause=Constant(None) — not as no cause at
    # all, which is what a bare `raise X` gives you. Read from the handler that
    # actually converts.
    conv_h = [h for h in handlers if ast.unparse(h.type or ast.Constant(0)) == "VaultError"]
    ok(conv_h and all(any(isinstance(n, ast.Raise) and isinstance(n.cause, ast.Constant)
                          and n.cause.value is None for n in ast.walk(h))
                      for h in conv_h),
       "with `from None`: the chained traceback is what we are removing")

    logged = [n for h in handlers for n in ast.walk(h)
              if isinstance(n, ast.Call) and ast.unparse(n.func).startswith("log.")]
    ok(logged, "the refusal leaves a line of OUR own — FastMCP's never reaches "
               "the container's log, FASTMCP_LOG_LEVEL sees to that")
    ok(all(ast.unparse(n.func) == "log.info" for n in logged),
       "at INFO: WARNING is the Gate's height, and a CONFLICT is not a warning",
       sorted({ast.unparse(n.func) for n in logged}))

    ok(any(ast.unparse(d) == "functools.wraps(fn)" for d in inner.decorator_list),
       "and functools.wraps ON THE REGISTERED FUNCTION, or it loses its schema",
       [ast.unparse(d) for d in inner.decorator_list])

    # ---- the census, over the WHOLE tree ----
    def names_tool(d) -> bool:
        node = d.func if isinstance(d, ast.Call) else d
        return isinstance(node, ast.Name) and node.id == "tool"

    def is_func(n) -> bool:
        return isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))

    # A bare @mcp.tool is a tool that never learned to be quiet, and the
    # failure NAMES it. Walked, not read off tree.body: nesting is not an
    # excuse.
    bare = [n.name for n in ast.walk(tree) if is_func(n)
            and any(ast.unparse(d).startswith("mcp.tool") for d in n.decorator_list)]
    ok(not bare, "no tool is registered with a bare @mcp.tool", bare)
    ok(len(registers) == 1 and not bare,
       "`mcp.tool` is reached from exactly one place in the file")

    # The decorator is NEVER called as a function. This one expression closes
    # two doors at once: `@tool()` with brackets, which dies at import with a
    # TypeError the suite would never see, and `tool(fn)` written out in the
    # body, which puts a tool on the surface that no census can see.
    as_call = [ast.unparse(n)[:60] for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "tool"]
    ok(not as_call, "the decorator is only ever used as `@tool`, bare", as_call)

    everywhere = [n for n in ast.walk(tree) if is_func(n)
                  and any(names_tool(d) for d in n.decorator_list)]
    at_module = [n for n in tree.body if is_func(n)
                 and any(names_tool(d) for d in n.decorator_list)]
    nested = [n.name for n in everywhere if not any(n is m for m in at_module)]
    ok(not nested, "no tool is declared inside a function, a class or an `if`",
       nested)

    # An EQUALITY against what the file says, never a threshold: `>= 20`
    # against 21 tools tolerates exactly one escapee, and one is the realistic
    # mistake.
    lines = len(re.findall(r"(?m)^@tool[ \t]*$", src))
    ok(len(at_module) == lines, "and they all go through it, in the bare form",
       f"{len(at_module)} decorated vs {lines} `@tool` lines")

    # add_tool() is the other door into the surface and carries no decorator at
    # all. Matched on the tree and not as `"add_tool" not in src`: a textual
    # ban in the negative also forbids EXPLAINING itself in a comment, and in a
    # file that documents its own reasons that is a check destined to be
    # deleted rather than respected.
    added = [ast.unparse(n)[:60] for n in ast.walk(tree) if isinstance(n, ast.Call)
             and ast.unparse(n.func).endswith(".add_tool")]
    ok(not added, "tools enter through the decorator and nowhere else", added)

    # The registered wrapper is synchronous and RETURNS what fn returns. Handed
    # a coroutine function it would return the coroutine unawaited, FastMCP
    # would await it further out, and the VaultError would surface with the
    # try/except never entered — the tool works, the conversion silently does
    # not. Today all tools are sync. The day one is not, this says so instead
    # of blessing it.
    async_tools = [n.name for n in everywhere if isinstance(n, ast.AsyncFunctionDef)]
    ok(not async_tools,
       "no tool is async: the wrapper would return the coroutine without seeing "
       "its refusals", async_tools)

    # And the WITNESS for a tool that loses its decorator cannot be the prose of
    # a manual: rewrite one sentence and the witness is dismissed. The engine is
    # the witness. A module-level function that reaches `vault.` IS a tool, by
    # definition of what this server is for — so if it stops being one it has
    # fallen off the MCP surface while still looking, to every reader, exactly
    # like a tool. guide_signature_check() would also notice, but only for as
    # long as the guide keeps its line.
    reaches = [n for n in tree.body if is_func(n)
               and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                       and isinstance(c.func.value, ast.Name)
                       and c.func.value.id in ("vault", "ds")
                       for c in ast.walk(n))]
    orphan = [n.name for n in reaches
              if not any(names_tool(d) for d in n.decorator_list)]
    ok(reaches and not orphan,
       f"every one of the {len(reaches)} functions that reach the engine is a tool",
       orphan)

    # The engine must NOT import FastMCP: that is what lets this suite run with
    # no network, no Docker and no OAuth in under a minute, and it is the
    # reason the conversion lives in server.py rather than in vault.py.
    engine = ast.parse((HERE / "vault.py").read_text(encoding="utf-8"))
    imports = set()
    for n in ast.walk(engine):
        if isinstance(n, ast.Import):
            imports |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imports.add(n.module.split(".")[0])
    ok("fastmcp" not in imports and "mcp" not in imports,
       "vault.py imports no server framework", sorted(imports))

    # The fault type is pinned IN THE ENGINE, not only in the decorator. One
    # line — `VaultFault = VaultError`, perfectly plausible as a tidy-up —
    # makes the fault branch the first one for every REFUSAL and cancels the
    # whole delivery without a single red line. Four things, because three were
    # not enough: the class exists, it subclasses the ordinary one, the engine
    # really raises it, and every name server.py imports from the engine is
    # actually there.
    fault = next((n for n in engine.body if isinstance(n, ast.ClassDef)
                  and n.name == "VaultFault"), None)
    ok(fault is not None, "vault.py defines VaultFault as a class of its own")
    ok(fault is not None and [ast.unparse(b) for b in fault.bases] == ["VaultError"],
       "and it subclasses VaultError, so everything that already catches "
       "VaultError still does", [ast.unparse(b) for b in fault.bases] if fault else None)

    import vault as _engine
    ok(issubclass(_engine.VaultFault, VaultError)
       and _engine.VaultFault is not VaultError,
       "and at runtime it is a DISTINCT type: an alias would make every refusal "
       "take the fault branch")

    raises_fault = [n for n in ast.walk(engine) if isinstance(n, ast.Raise)
                    and isinstance(n.exc, ast.Call)
                    and getattr(n.exc.func, "id", "") == "VaultFault"]
    ok(len(raises_fault) >= 5, "and the engine really raises it, not just declares it",
       len(raises_fault))

    # Removing it from the engine would kill the container at import — and the
    # suite, which never imports server.py, would stay green while doing it.
    imported = [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and n.module == "vault" for a in n.names]
    missing = [name for name in imported if not hasattr(_engine, name)]
    ok(imported and not missing,
       f"and every name server.py imports from the engine exists there: {imported}",
       missing)
    # The three read-back checks are the engine's own integrity detectors:
    # nothing a caller sends can make them fire, so they are faults by
    # construction. This is a criterion, not a count — it names the shape, and
    # a fourth one added tomorrow is covered the day it is written.
    esrc = (HERE / "vault.py").read_text(encoding="utf-8")
    misfiled = [ln.strip() for ln in esrc.splitlines()
                if "verification failed" in ln and "raise VaultFault" not in ln]
    ok(not misfiled, "every post-write read-back failure is a VaultFault", misfiled)


def dockerfile_env_check() -> None:
    """FastMCP reads its settings when it is IMPORTED, so they cannot be set
    from inside server.py — they live in the Dockerfile as ENV. That makes them
    easy to delete by accident, and the only symptom would be the noise quietly
    coming back. This is the tripwire.

    PYTHONUNBUFFERED is not a FastMCP setting and is checked here for the same
    reason: it has to be in the environment before the interpreter starts, so
    it cannot live in the code either, and losing it does not fail — it just
    reorders the log and drops the last block when the container is killed.

    FastMCP values verified against fastmcp 3.4.5."""
    df = (HERE / "Dockerfile").read_text(encoding="utf-8")
    for var, val in (("FASTMCP_SHOW_SERVER_BANNER", "false"),
                     ("FASTMCP_ENABLE_RICH_LOGGING", "false"),
                     ("FASTMCP_CHECK_FOR_UPDATES", "off"),
                     ("FASTMCP_LOG_LEVEL", "WARNING"),
                     ("PYTHONUNBUFFERED", "1")):
        ok(f"ENV {var}={val}" in df, f"Dockerfile sets {var}={val}")


def log_level_checks() -> None:
    """logging.setLevel() raises on an unknown level, and it runs at import —
    after the preflight has printed a clean sheet. So the one way to get a
    container that dies in a loop with no useful message was to leave the
    optional LOG_LEVEL field empty, which the Unraid dropdown does not prevent
    and a hand-built container has no dropdown for at all.

    Both directions: the two real levels survive untouched, and everything else
    falls back to INFO while REPORTING what it rejected. The reporting is the
    part worth testing — a knob that silently ignores you is how you get told
    the feature is broken."""
    from preflight import log_level_from_env
    for value, expect_level, expect_rejected in (
            (None, "INFO", None),          # not defined at all
            ("", "INFO", None),            # defined and empty: the crash case
            ("   ", "INFO", None),         # whitespace only
            ("INFO", "INFO", None),
            ("WARNING", "WARNING", None),
            ("warning", "WARNING", None),  # case is typography, not intent
            (" info ", "INFO", None),
            ("DEBUG", "INFO", "DEBUG"),    # inert here: there are no debug lines
            ("ERROR", "INFO", "ERROR"),    # would silence the gate's refusals
            # WARN is Python's own alias, not a typo, and the intent behind it is
            # unambiguous: less noise. Correcting it to INFO would hand back MORE,
            # and say so in a line that to its author reads as false. Honoured.
            ("WARN", "WARNING", None),
            ("warn", "WARNING", None),
            ("INF0", "INFO", "INF0")):
        old = os.environ.pop("LOG_LEVEL", None)
        try:
            if value is not None:
                os.environ["LOG_LEVEL"] = value
            got = log_level_from_env()
        finally:
            os.environ.pop("LOG_LEVEL", None)
            if old is not None:
                os.environ["LOG_LEVEL"] = old
        ok(got == (expect_level, expect_rejected),
           f"LOG_LEVEL={value!r} resolves to {expect_level} "
           f"{'and says what it rejected' if expect_rejected else 'silently'}", got)


def guide_signature_check() -> None:
    """The manual travels inside the image and is served by reference_guide(),
    so it is read by the caller far more often than the code is. A manual that
    promises a parameter the tool has not got — or, worse, stays silent about a
    default that narrows what the call does — is a defect this project has
    already paid for: `archive` filters `*.md` by default, and the guide used to
    recommend it for audits without saying so, which quietly leaves every PDF
    out of the audit.

    Prose cannot be checked. A signature can. So the guide carries one block of
    signatures, verbatim, and this reads both sides: every tool declared with
    @mcp.tool must appear there with the same parameter names, the same ORDER
    and the same defaults, and nothing may appear there that is not a tool.

    It also fixes the count problem at its root: the number of tools is never
    written down anywhere, it is the length of this list."""
    src = (HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def rendered(fn) -> str:
        pad = [None] * (len(fn.args.args) - len(fn.args.defaults))
        parts = [a.arg if d is None else f"{a.arg}={ast.unparse(d)}"
                 for a, d in zip(fn.args.args, pad + list(fn.args.defaults))]
        return f"{fn.name}({', '.join(parts)})"

    def is_tool(fn) -> bool:
        # Every spelling that registers a tool, because every one of them works:
        # @tool, which is server.py's own converting decorator and what they all
        # use since 2.3.0; @mcp.tool, an Attribute; and @mcp.tool(name=…), a Call
        # wrapping it. Matching only some would skip a tool in silence, which is
        # the exact failure this test exists to prevent — it would go missing
        # from the manual and nothing would fail. That @tool is the ONLY one in
        # use is not asserted here but in tool_conversion_check().
        for d in fn.decorator_list:
            node = d.func if isinstance(d, ast.Call) else d
            if isinstance(node, ast.Name) and node.id == "tool":
                return True
            if (isinstance(node, ast.Attribute) and node.attr == "tool"
                    and isinstance(node.value, ast.Name) and node.value.id == "mcp"):
                return True
        return False

    real = [rendered(n) for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_tool(n)]

    # A decorator line appears once per tool in the source and nowhere else. If
    # the AST walk and a plain count of those lines disagree, the walk is missing
    # some and every check below is being run on a subset.
    lines = len(re.findall(r"(?m)^@(?:tool|mcp\.tool)\b", src))
    ok(len(real) == lines,
       "the AST sees every tool the file declares", f"{len(real)} vs {lines}")

    guide = (HERE / "reference-guide.md").read_text(encoding="utf-8")

    # Anchored to the block, not scattered over the file. Scanning the whole
    # manual would let a recipe line pose as a signature, and — worse — would
    # keep passing if the block itself were deleted.
    block = guide.split("## EVERY CALL, IN FULL", 1)
    ok(len(block) == 2, "the manual still has the block of signatures")
    body = block[1].split("\n## ", 1)[0] if len(block) == 2 else ""
    written = [ln.strip() for ln in body.splitlines()
               if ln.startswith("    ") and ln.strip().endswith(")")]

    missing = [r for r in real if r not in written]
    ok(not missing, "every tool is in the guide with its exact signature", missing)

    # The other direction, which is the one that rots silently: a line left in
    # the manual for a tool that was renamed or deleted. This compares the whole
    # block, so such a line has nowhere to hide — filtering it by the names the
    # code still has would have made this check unable to see it.
    stale = [w for w in written if w not in real]
    ok(not stale, "the guide promises no signature the code does not have", stale)

    # The manual states that `key` is always last, and a caller passing it
    # positionally depends on that being true. It is a promise, so it is checked.
    misplaced = [r for r in real if "key=" in r and not r.endswith("key='')")]
    ok(not misplaced, "`key` really is the last parameter everywhere", misplaced)

    # Named in prose but never declared: the reader is sent to a door that is
    # not there. `status()` is the one that actually happened — it reads like a
    # tool, it is not one, and the two real ones are vault_status() and
    # dataset_status(). The lookbehind is what keeps those two from matching.
    ghosts = re.findall(r"(?<![\w.])(status|drop|create)\s*\(", guide)
    ok(not ghosts, "the guide names no bare verb that is not a tool", sorted(set(ghosts)))

    # The README documents the same surface at greater length, and it is what a
    # stranger reads before installing anything. It spells each tool in bold
    # WITHOUT `key`, which is explained once for all of them, so that one
    # parameter is dropped before comparing. Quotes are normalised because Python
    # renders defaults with single quotes and the prose uses double ones: that is
    # a difference in typography, and a test that fails on typography gets
    # switched off.
    bare = [re.sub(r", key='[^']*'", "", r).replace("'", '"') for r in real]
    readme = (HERE / "README.md").read_text(encoding="utf-8").replace("'", '"')
    absent = [s for s in bare if f"**`{s}`**" not in readme]
    ok(not absent, "README.md spells every tool with its exact signature", absent)

    # The limits are the other kind of number the documents copy by hand, and
    # the one a caller plans around: told 2 MB when the real ceiling is 1, they
    # build a call that fails. The number is RENDERED from the constant into the
    # form a person writes, and then the whole table ROW is looked for — row and
    # all, because "2 MB" on its own is satisfied three times over by a single
    # occurrence, and a wrong row would sail through. There is no second copy of
    # any number here: change vault.py and this expects the new value.
    import vault as _v
    mb = lambda n: f"{n // 1_000_000} MB"
    kb = lambda n: f"{n // 1_000} KB"
    ok(_v.MAX_READ_BYTES == _v.MAX_WRITE_BYTES,
       "reads and writes share one ceiling, as the single table row claims")
    rows = [("text file", mb(_v.MAX_READ_BYTES)),
            ("binary", mb(_v.MAX_BINARY_BYTES)),
            ("`append` block", kb(_v.MAX_APPEND_BYTES)),
            ("`list_files`", f"{_v.MAX_LIST_FILES:,} files"),
            ("`search`", f"{_v.MAX_SEARCH_HITS} lines"),
            ("`diff`", kb(_v.MAX_DIFF_BYTES)),
            ("`archive`", f"{mb(_v.MAX_ARCHIVE_BYTES)} in, "
                          f"{mb(_v.MAX_ARCHIVE_OUT_BYTES)} tgz out"),
            ("datasets in the vault", f"{_v.MAX_DATASETS}")]
    for label, shown in rows:
        ok(f"| {label} | {shown} |" in guide,
           f"the guide's row for {label} still states what the code enforces", shown)


def main() -> int:
    root = tempfile.mkdtemp(prefix="archivist-test-")
    try:
        os.makedirs(Path(root) / "Example Project" / "01 Notes")
        (Path(root) / "Example Project" / "01 Notes" / "a.md").write_text("line one\nline two\n")
        (Path(root) / "Example Project" / "log.md").write_text("# Log\n")
        # A SECOND dataset, and inside the first a folder that carries its name.
        # That collision is not decoration: it is the case the amendment to the
        # ambiguous-path rule exists to protect, and it has to be present from
        # the start so nothing can quietly stop covering it.
        os.makedirs(Path(root) / "Example Project" / "Other Project")
        (Path(root) / "Example Project" / "Other Project" / "note.md").write_text("homonym\n")
        os.makedirs(Path(root) / "Other Project")
        (Path(root) / "Other Project" / "b.md").write_text("elsewhere\n")
        (Path(root) / "keys.txt").write_text(f"Example Project\t{KEY}\n")

        v = VaultRoot(root)
        v.boot(0)

        print("\n[1] vault status")
        st = v.status("test")
        ok(st["vault"] == "ok", "vault answers")
        ok(st["datasets"] == [{"name": "Example Project", "state": "locked"},
                              {"name": "Other Project", "state": "open"}],
           "dataset list and state", st["datasets"])

        print("\n[2] protection — everything here MUST fail")
        must_fail("no key", lambda: v.open("Example Project", "log.md", ""))
        must_fail("wrong key", lambda: v.open("Example Project", "log.md", "nope"))
        must_fail("keys.txt as a dataset", lambda: v.open("keys.txt", "", KEY))
        must_fail("traversal with ..", lambda: v.open("Example Project", "../keys.txt", KEY))
        must_fail("empty dataset name", lambda: v.open("", "log.md", KEY))
        must_fail("unknown dataset", lambda: v.open("Nowhere", "x.md", ""))
        must_fail(".git as a path", lambda: v.open("Example Project", ".git/config", KEY))
        must_fail("an absolute path", lambda: v.open("Example Project", "/log.md", KEY))

        ds, rel = v.open("Example Project", "log.md", KEY)
        ok(ds.name == "Example Project" and rel == "log.md", "resolution with the right key")
        # keys.txt is not merely refused, it is not EXPRESSIBLE: as a dataset it
        # does not exist, and as a path it can only ever mean a file inside a
        # dataset, where there is none. This one passes because the file is not
        # there, which is the point — but say so, or it looks like a guard.
        must_fail("keys.txt inside a dataset is simply absent",
                  lambda: ds.read_file("keys.txt"))
        ok((Path(root) / "keys.txt").read_text().startswith("Example Project"),
           "the key registry is still where it was")

        print("\n[2c] the lockfiles are refused as PATHS, not merely hidden")
        # Hiding them from listings, which _skip does, is not protection. A write
        # goes through os.replace and swaps the inode under whoever holds the
        # flock, and then two writers both believe they have it.
        from vault import LOCKFILE, ROOT_LOCKFILE as RLF
        with ds._lock():                                  # this is what creates it
            pass
        ok((Path(root) / "Example Project" / LOCKFILE).exists(), "the dataset lockfile exists")
        for label, fn in (
            ("read", lambda: ds.read_file(LOCKFILE)),
            ("write", lambda: ds.write_file(LOCKFILE, "stolen", "new")),
            ("move away", lambda: ds.move_path(LOCKFILE, "stolen.md")),
            ("move onto", lambda: ds.move_path("log.md", LOCKFILE)),
            ("at resolution", lambda: v.check_path("Example Project", LOCKFILE)),
            ("nested", lambda: v.check_path("Example Project", f"01 Notes/{LOCKFILE}")),
            ("root lockfile", lambda: v.check_path("Example Project", RLF)),
        ):
            must_fail(f"the lockfile as a path, {label}", fn)
        ok((Path(root) / "Example Project" / LOCKFILE).exists(),
           "and it is still there, untouched")
        # Case-folded, because on a case-insensitive volume an exact comparison
        # lets '.GIT' through to the real repository.
        for bad in (".GIT/config", ".Git", LOCKFILE.upper()):
            must_fail(f"case variant refused: {bad!r}",
                      lambda b=bad: v.check_path("Example Project", b))
        # And the other half, the one that matters: exact names only. '.gitignore'
        # is an ordinary file that lives in every dataset and must pass.
        ok(v.check_path("Example Project", ".gitignore") == ".gitignore",
           "'.gitignore' is not '.git': it passes")
        ok(ds.read_file(".gitignore")["path"] == ".gitignore", "and it really reads")

        print("\n[2b] the ambiguous path — the v1.8 shape MUST be refused")
        # A caller not yet rewritten sends the dataset twice: once in `dataset`,
        # once as the head of `path`. Refused loudly.
        #
        # These all go through v.open because that IS the single door — reads and
        # writes alike reach it, which is not asserted here but in
        # single_door_check(), by reading server.py. Listing a read and a write
        # separately would only have looked like two proofs.
        for label, fn in (
            ("a file", lambda: v.open("Example Project", "Example Project/log.md", KEY)),
            ("a nested file", lambda: v.open("Example Project", "Example Project/01 Notes/a.md", KEY)),
            ("bare dataset as path", lambda: v.open("Example Project", "Example Project", KEY)),
            ("folded case", lambda: v.open("Example Project", "example project/log.md", KEY)),
            ("with a trailing slash", lambda: v.open("Example Project", "Example Project/", KEY)),
        ):
            must_fail(f"prefixed path, {label}", fn)
        try:
            v.check_path("Example Project", "Example Project/log.md")
        except VaultError as e:
            ok("drop the leading" in str(e) and "Example Project/" in str(e),
               "the refusal says exactly what to drop", e)
        # The other half, which is the half that matters: the check must NOT
        # widen to "any existing dataset name". A folder inside a dataset may
        # legitimately be called like another dataset, and that day a wider
        # check would refuse a correct path.
        _, homonym = v.open("Example Project", "Other Project/note.md", KEY)
        ok(homonym == "Other Project/note.md",
           "a folder named like ANOTHER dataset passes", homonym)
        ok(ds.read_file("Other Project/note.md")["content"] == "homonym\n",
           "and it reads the file that is really there")

        print("\n[3] reading")
        r = ds.read_file(rel)
        ok(r["content"] == "# Log\n", "read_file content")
        ok(r["path"] == "log.md", "paths come back RELATIVE to the dataset", r["path"])
        ok(r["dataset"] == "Example Project", "and the dataset is echoed back", r.get("dataset"))
        ok(ds.list_files("")["count"] == 4, "list_files is recursive (counting .gitignore)")
        ok(len(ds.manifest("")["manifest_sha256"]) == 64, "manifest sha")
        # An empty path means the whole dataset, everywhere it is accepted.
        ok(ds.list_files("")["base"] == "", "empty path is the dataset root")
        ok(ds.list_files()["count"] == ds.list_files("")["count"],
           "the default path is the empty one")
        ok(ds.manifest()["file_count"] == 4, "manifest with no path covers the dataset")

        print("\n[3b] every returned path is relative, and carries its dataset")
        # The writes are exercised here too, not only the reads: a return that
        # kept the prefix on one side and dropped it on the other would be the
        # worst of both, and only a sweep catches the one that was forgotten.
        ds.write_file("sweep.md", "a\n", "new")
        sweep_sha = ds.read_file("sweep.md")["sha256"]
        returns = {
            "list_files": ds.list_files(""),
            "list_files (one file)": ds.list_files("log.md"),
            "read_file": ds.read_file("log.md"),
            "read_binary": ds.read_binary("log.md"),
            "read_at": ds.read_at("log.md", "HEAD"),
            "manifest": ds.manifest(""),
            "search": ds.search("line", ""),
            "history": ds.history("", 5),
            "history (one file)": ds.history("log.md", 5),
            "archive": ds.archive("", "*.md"),
            "diff": ds.diff("HEAD~1", "HEAD", ""),
            "status": ds.status(),
            "append": ds.append("sweep.md", "b"),
            "edit_file": ds.edit_file("sweep.md", "a\n", "c\n", ds.read_file("sweep.md")["sha256"]),
            "write_file": ds.write_file("sweep.md", "d\n", ds.read_file("sweep.md")["sha256"]),
            "write_binary": ds.write_binary("sweep.bin", "AAEC", "new"),
            "move_path": ds.move_path("sweep.md", "moved.md"),
            "trash_purge": ds.trash_purge("2020-01-01"),
        }
        for label, res in returns.items():
            ok(res.get("dataset") == "Example Project",
               f"{label} echoes the dataset", res.get("dataset"))
        for label, field in (("list_files", "base"), ("list_files (one file)", "file"),
                             ("read_file", "path"), ("read_binary", "path"),
                             ("read_at", "path"), ("manifest", "base"),
                             ("search", "base"), ("history", "path"),
                             ("history (one file)", "path"), ("archive", "base"),
                             ("diff", "path"), ("append", "path"),
                             ("edit_file", "path"), ("write_file", "path"),
                             ("write_binary", "path"), ("move_path", "from"),
                             ("move_path", "to")):
            val = returns[label][field]
            ok(not val.startswith("Example Project"),
               f"{label}[{field}] carries no dataset prefix", val)
        ok(all(not ln.startswith("Example Project") for ln in returns["search"]["lines"]),
           "search lines are file:line:text, relative", returns["search"]["lines"][:1])
        ok(all(not f["path"].startswith("Example Project") for f in returns["list_files"]["files"]),
           "every entry in list_files is relative")
        ok(sweep_sha != returns["write_file"]["sha256"], "the sweep really wrote")
        ds.move_path("moved.md", "Trash/moved.md")
        ds.move_path("sweep.bin", "Trash/sweep.bin")
        purged = ds.trash_purge("2035-01-01")
        ok(purged["dataset"] == "Example Project" and
           all(not f.startswith("Example Project") for f in purged["files"]),
           "trash_purge lists relative paths", purged["files"])

        print("\n[4] writing and CAS")
        ok(ds.append(rel, "| entry |")["commit"] != "(nothing to commit)", "append commits")
        sha = ds.read_file(rel)["sha256"]
        ok(ds.edit_file(rel, "# Log", "# Register", sha)["sha256"] != sha, "edit_file changes the sha")
        must_fail("write with a wrong sha", lambda: ds.write_file("log.md", "x", "0" * 64))
        must_fail("edit with absent text", lambda: ds.edit_file(
            "log.md", "NOT THERE", "x", ds.read_file("log.md")["sha256"]))
        (Path(root) / "Example Project" / "dup.md").write_text("aaa\naaa\n")
        ds._commit("setup dup")
        must_fail("edit with ambiguous text", lambda: ds.edit_file(
            "dup.md", "aaa", "bbb", ds.read_file("dup.md")["sha256"]))
        ok(ds.write_file("new.md", "hi\n", "new")["size"] == 3, "write_file new")
        must_fail('"new" on an existing file', lambda: ds.write_file("new.md", "x", "new"))
        must_fail("append over 64 KB", lambda: ds.append("log.md", "x" * 70_000))

        print("\n[5] binaries")
        import base64
        b = base64.b64encode(b"\xff\xfe\x00%PDF").decode()   # deliberately not valid UTF-8
        ok(ds.write_binary("bin.dat", b, "new")["size"] == 7, "write_binary")
        ok(ds.read_binary("bin.dat")["content_base64"] == b, "read_binary round trip")
        must_fail("invalid base64", lambda: ds.write_binary("x.dat", "not-base64!!", "new"))
        must_fail("read_file on a binary", lambda: ds.read_file("bin.dat"))

        print("\n[6] search and archive")
        s = ds.search("line", "")
        ok(s["matches"] >= 2, "search finds", s)
        ok(ds.search("^line", "", regex=True)["matches"] >= 2, "search with regex")
        must_fail("invalid regex", lambda: ds.search("[", "", regex=True))
        a = ds.archive("", "*.md")
        ok(a["file_count"] >= 3 and a["tgz_bytes"] > 0, "archive packs", a["file_count"])
        must_fail("archive with no match", lambda: ds.archive("", "*.xyz"))
        # The member names inside the tgz follow the same rule as every other
        # path that comes back: extracting reproduces the dataset's tree, not a
        # directory named after the dataset.
        import io as _io, tarfile as _tf
        names = _tf.open(fileobj=_io.BytesIO(base64.b64decode(a["tgz_base64"])),
                         mode="r:gz").getnames()
        ok(all(not n.startswith("Example Project") for n in names),
           "tar member names are dataset-relative", names[:2])

        print("\n[6b] max_chars — the caller's ceiling, which the server cannot know")
        # The number in the refusal has to be the REAL one. A caller told
        # "too big" without a size narrows blind and comes back twice; told a
        # rounded estimate, it narrows to something that still does not fit.
        # base64 is four characters per three bytes and b64encode adds no
        # newlines, so the figure is computable exactly — and is computed
        # BEFORE the encoding, which is the point of asking for the ceiling
        # instead of producing the payload and hoping.
        exact = len(a["tgz_base64"])
        # Caught rather than called bare: a ceiling that refuses what exactly
        # fits is an off-by-one, and an off-by-one has to arrive as a NAMED
        # failure. Left bare, the refusal would raise through this line and end
        # the suite on a traceback that names no line at all.
        try:
            ok(ds.archive("", "*.md", max_chars=exact)["tgz_bytes"] > 0,
               "max_chars exactly equal to the size lets it through", exact)
        except VaultError as e:
            ok(False, "max_chars exactly equal to the size lets it through",
               f"refused at exactly {exact}: {e}")
        must_fail("max_chars one character short",
                  lambda: ds.archive("", "*.md", max_chars=exact - 1))
        ok(ds.archive("", "*.md", max_chars=0)["file_count"] == a["file_count"],
           "0 means no ceiling of the caller's")
        # A negative is not a smaller ceiling and it is not "off": it is a
        # caller who has got the sense of the parameter backwards, and the
        # cheapest thing to do is say so. Falling back to "no ceiling" would
        # hand back MORE than was asked for, silently.
        must_fail("a negative max_chars", lambda: ds.archive("", "*.md", max_chars=-1))
        try:
            ds.archive("", "*.md", max_chars=10)
            ok(False, "the refusal states the size it would have been")
        except VaultError as e:
            ok(str(exact) in str(e),
               "the refusal states the size it would have been", e)

        print("\n[7] trash and purge")
        mv = ds.move_path("new.md", "Trash/new.md")
        ok(mv["trashed"] is True, "move into Trash is marked")
        must_fail("move onto an existing destination",
                  lambda: ds.move_path("log.md", "Trash/new.md"))
        ok(ds.trash_purge("2020-01-01")["removed"] == 0, "a purge in the past removes nothing")
        p = ds.trash_purge("2035-01-01")
        ok(p["removed"] == 1, "purge removes the trashed file", p)
        must_fail("purge with an invalid date", lambda: ds.trash_purge("not-a-date"))

        print("\n[8] history and recovery")
        h = ds.history("", 10)
        ok(len(h["entries"]) >= 5, "history", len(h["entries"]))
        rev = h["entries"][3].split(" · ")[0]
        ok("content" in ds.read_at("log.md", rev), "read_at")
        must_fail("invalid revision", lambda: ds.read_at("log.md", "; rm -rf /"))
        ok("diff" in ds.diff("HEAD~2", "HEAD", ""), "diff summary")

        print("\n[9] restore")
        before = ds.manifest("")
        must_fail("restore with a wrong manifest", lambda: ds.restore(rev, "0" * 64))
        res = ds.restore(rev, before["manifest_sha256"])
        ok(res["commit"] != "(nothing to commit)", "restore commits forward")
        ok(int(ds.status()["total_commits"]) > 0, "history survives the restore")

        print("\n[10] dataset administration")
        c = v.create("Scratch")
        ok(c["state"] == "open", "dataset created open")
        for bad in ("scratch", "_reserved", ".hidden", "a/b", "..", ""):
            must_fail(f"create {bad!r}", lambda n=bad: v.create(n))
        ds2 = v.open_by_name("Scratch", "")
        ok(ds2.status()["dataset"] == "Scratch", "open dataset needs no key")
        must_fail("drop a dataset that has a key",
                  lambda: v.drop("Example Project", ds.manifest("")["manifest_sha256"]))
        must_fail("drop with a wrong manifest", lambda: v.drop("Scratch", "0" * 64))
        d = v.drop("Scratch", ds2.manifest("")["manifest_sha256"])
        ok(d["dropped"] == "Scratch", "drop an open dataset")
        ok(not (Path(root) / "Scratch").exists(), "the directory is really gone")

        print("\n[10b] the root lock")
        from vault import ROOT_LOCKFILE
        lf = Path(root) / ROOT_LOCKFILE
        ok(lf.exists(), "create/drop left a root lockfile")
        ok(ROOT_LOCKFILE not in v.dataset_names(), "the root lockfile is not a dataset")
        must_fail("the root lockfile as a dataset", lambda: v.resolve_dataset(ROOT_LOCKFILE))
        must_fail("the dataset lockfile as a dataset", lambda: v.resolve_dataset(".archivist.lock"))
        # A directory that appeared from outside between the check and the
        # mkdir: the lock cannot prevent it, but the message must stay readable
        # instead of surfacing a raw FileExistsError.
        (Path(root) / "Ghost").mkdir()
        try:
            v.create("Ghost")
            ok(False, "create over an existing directory is refused")
        except VaultError as e:
            ok("already exists" in str(e), "create over an existing directory is refused", e)
        except FileExistsError as e:
            ok(False, "create over an existing directory is refused", f"raw FileExistsError: {e}")
        shutil.rmtree(Path(root) / "Ghost")

        print("\n[10d] the window `drop` leaves open is a refusal, not a traceback")
        # A caller that resolved a dataset a moment before it was dropped still
        # holds a handle to a directory that is no longer there. Closing that
        # race would take a global lock on every write — the very thing the
        # dataset design turned down. Making it SAY so costs nothing, and the
        # difference is whether the log records a refusal or a page of
        # traceback under the word FAULT.
        v.create("Doomed")
        doomed = v.open_by_name("Doomed", "")
        v.drop("Doomed", doomed.manifest("")["manifest_sha256"])
        try:
            doomed.write_file("x.md", "hi\n", "new")
            ok(False, "a write on a dataset dropped underneath is refused")
        except VaultError as e:
            ok("no longer there" in str(e),
               "a write on a dataset dropped underneath is refused", e)
        except Exception as e:
            ok(False, "a write on a dataset dropped underneath is refused",
               f"raw {type(e).__name__}: {e}")

        print("\n[10e] the lock, with two real processes")
        # Everything else about the lock is proved by construction or by
        # reading. This is the only check that puts two operating-system
        # processes on the same file at the same time — which is the situation
        # flock exists for, and the one a single-process test cannot stage.
        v.create("Concurrent")
        cds = v.open_by_name("Concurrent", "")
        cds.write_file("log.md", "start\n", "new")
        ROUNDS = 12
        pid = os.fork()
        if pid == 0:
            # The child MUST leave through os._exit: a normal exit would flush
            # the stdout buffer it inherited and print this suite's output a
            # second time, and would run the parent's teardown on the way out.
            rc = 0
            try:
                for i in range(ROUNDS):
                    cds.append("log.md", f"child {i}\n")
            except Exception:
                rc = 1
            os._exit(rc)
        parent_failed = 0
        try:
            for i in range(ROUNDS):
                cds.append("log.md", f"parent {i}\n")
        except Exception:
            parent_failed = 1
        _, status_word = os.waitpid(pid, 0)
        ok(os.WEXITSTATUS(status_word) == 0 and not parent_failed,
           "neither process was refused while the other held the lock",
           (os.WEXITSTATUS(status_word), parent_failed))
        body = cds.read_file("log.md")["content"]
        # The real question is not whether they both survived but whether
        # anything was LOST: an append that read the file before the other
        # process wrote, and wrote back over it, would show up here as a
        # missing block and nowhere else.
        ok(body.count("child ") == ROUNDS and body.count("parent ") == ROUNDS,
           "every block from both processes survived",
           (body.count("child "), body.count("parent ")))
        ok(cds.status()["git"] == "clean",
           "the repository is clean after two writers", cds.status()["git"])
        ok(cds.status()["total_commits"] >= 2 * ROUNDS,
           "every write got its own commit", cds.status()["total_commits"])
        v.drop("Concurrent", cds.manifest("")["manifest_sha256"])

        print("\n[10c] placeholders the preflight must catch")
        import preflight
        for bad in ("CHANGEME", "CHANGE_ME", "change-me", "change me", "CamBiaMi",
                    "https://CHANGEME.your-tailnet.ts.net", "CHANGEME-YOUR-GITHUB-USERNAME",
                    "0" * 28 + "CHANGE_ME" + "0" * 28):
            ok(preflight.is_placeholder(bad), f"placeholder recognised: {bad[:34]!r}")
        # The other half: a real value must NEVER be mistaken for a placeholder.
        # Refusing to start on a legitimate config is worse than the hole this
        # check closes — and "exchange" contains the letters of "changeme".
        for good in ("alcor6502", "https://svc-a1.example.ts.net", "a3f9" * 16,
                     "exchange mechanism", "https://exchange.me.ts.net", "exchangemeister"):
            ok(not preflight.is_placeholder(good), f"real value let through: {good[:34]!r}")

        print("\n[11] key registry hot reload")
        kf = Path(root) / "keys.txt"
        kf.write_text("# no keys\n"); time.sleep(0.02)
        ok(v.status("t")["datasets"][0]["state"] == "open", "removing the line opens the dataset")
        ok(v.open("Example Project", "log.md", "")[1] == "log.md", "no key needed now")
        kf.write_text(f"Example Project\t{KEY}\n"); time.sleep(0.02)
        ok(v.status("t")["datasets"][0]["state"] == "locked", "putting the line back locks it")
        must_fail("and it blocks again", lambda: v.open("Example Project", "log.md", ""))

        print("\n[12] history pruning")
        os.makedirs(Path(root) / "Old")
        v.boot(0)
        old = Dataset(Path(root) / "Old", "Old")
        for i in range(6, 0, -1):
            (Path(root) / "Old" / f"f{i}.md").write_text(f"content {i}\n")
            when = f"2024-0{i}-15T12:00:00"
            subprocess.run(["git", "-C", str(Path(root) / "Old"), "add", "-A"], capture_output=True)
            subprocess.run(["git", "-C", str(Path(root) / "Old"), "commit", "-q", "-m", f"c{i}"],
                           capture_output=True,
                           env=dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when))
        (Path(root) / "Old" / "recent.md").write_text("new\n")
        old._commit("recent")
        before_sha = old.manifest("")["manifest_sha256"]
        n_before = old.status()["total_commits"]
        old.prune_history(6)
        ok(old.status()["total_commits"] < n_before, "history gets shorter")
        ok(old.manifest("")["manifest_sha256"] == before_sha, "CONTENT NEVER CHANGES")
        ok("not needed" in old.prune_history(6), "a second prune is a no-op")

        print("\n[12c] pruning a history that is not small")
        # Six commits proved the mechanism. They did not prove it on anything
        # resembling a real repository, and pruning is the one operation here
        # that rewrites history rather than adding to it: if it is going to
        # behave differently at scale, it will be on the rebase.
        #
        # The commits are made in ONE shell rather than by four hundred
        # subprocess calls from Python: the loop is the cost, not the work, and
        # a suite that takes a minute stops being run.
        os.makedirs(Path(root) / "Long")
        v.boot(0)
        long_ds = Dataset(Path(root) / "Long", "Long")
        _old_date = "2024-01-15T12:00:00"
        _script = (
            'set -e; cd "$1"; '
            'for i in $(seq 1 200); do '
            '  echo "line $i" >> f.md; git add -A; '
            '  GIT_AUTHOR_DATE="$2" GIT_COMMITTER_DATE="$2" git commit -q -m "old $i"; '
            'done; '
            'echo recent > recent.md; git add -A; git commit -q -m recent'
        )
        _r = subprocess.run(["sh", "-c", _script, "sh", str(Path(root) / "Long"), _old_date],
                            capture_output=True, text=True)
        ok(_r.returncode == 0, "two hundred commits were made", _r.stderr[:200])
        n_before = long_ds.status()["total_commits"]
        ok(n_before > 200, "a history of some size to prune", n_before)
        sha_before = long_ds.manifest("")["manifest_sha256"]
        msg = long_ds.prune_history(6)
        n_after = long_ds.status()["total_commits"]
        ok(n_after < n_before // 10, "the prefix really is squashed", (n_before, n_after, msg))
        # The one guarantee that matters, and the one a rebase could break
        # without anything else noticing: history is what gets shorter, never
        # content.
        ok(long_ds.manifest("")["manifest_sha256"] == sha_before,
           "CONTENT NEVER CHANGES, at two hundred commits either")
        ok(long_ds.status()["git"] == "clean", "the working tree survives the rebase")
        ok(long_ds.read_file("f.md")["content"].count("line ") == 200,
           "every line of the pruned-away history is still in the file")

        print("\n[12b] a refusal and a fault are different things")
        # server.py treats them differently — one quiet line, or a full
        # traceback — so the engine has to get the label right. Both directions
        # are checked, and the second one is the one that matters: a fault
        # mislabelled as a refusal goes quiet, and a refusal mislabelled as a
        # fault turns a caller's typo into a page of traceback.
        # Fetched rather than imported, so that losing the type from the engine
        # produces a NAMED failure instead of an ImportError that takes the
        # whole suite down before it can say which line did it. A stand-in
        # nothing ever raises keeps the section readable: the FAULT cases go
        # red, the REFUSAL cases still prove what they prove.
        import vault as _v
        VaultFault = getattr(_v, "VaultFault", None)
        ok(VaultFault is not None and VaultFault is not VaultError,
           "the engine exports VaultFault as a type of its own", VaultFault)
        if VaultFault is None or VaultFault is VaultError:
            VaultFault = type("VaultFault", (Exception,), {})
        # Inside `root` so the teardown takes it away, and `root` itself is not
        # a repository — only the datasets under it are — so git finds nothing
        # to walk up to.
        _bare = Path(root) / "not-a-dataset-just-a-folder"
        _bare.mkdir()
        for label, fn in (
            ("git refusing a command of ours", lambda: ds._git("frobnicate")),
            # A directory that is not a repository, and has none above it
            # either — the shape a broken mount or a half-made dataset has.
            ("git in a directory that is not a repository",
             lambda: Dataset(_bare, "Bare")._git("rev-parse", "HEAD")),
        ):
            try:
                fn()
                ok(False, f"FAULT: {label}", "did not raise")
            except VaultFault:
                ok(True, f"FAULT: {label}")
            except VaultError as e:
                ok(False, f"FAULT: {label}", f"came back as a plain refusal: {e}")
            except Exception as e:
                # Anything else is wrong here too, and saying so beats letting
                # the traceback end the suite before it names the line.
                ok(False, f"FAULT: {label}", f"came back as {type(e).__name__}: {e}")

        for label, fn in (
            ("a revision that does not exist, in diff",
             lambda: ds.diff("deadbee")),
            ("a revision that does not exist, in restore",
             lambda: ds.restore("deadbee", ds.manifest("")["manifest_sha256"])),
            ("a CONFLICT, which is the system working",
             lambda: ds.write_file("log.md", "x", "0" * 64)),
            ("a file that is not there", lambda: ds.read_file("nope.md")),
        ):
            try:
                fn()
                ok(False, f"REFUSAL: {label}", "did not raise")
            except VaultFault as e:
                ok(False, f"REFUSAL: {label}", f"mislabelled as a fault: {e}")
            except VaultError:
                ok(True, f"REFUSAL: {label}")
            except Exception as e:
                ok(False, f"REFUSAL: {label}", f"came back as {type(e).__name__}: {e}")

        print("\n[13] static server.py <-> vault.py consistency")
        static_api_check()
        single_door_check()

        print("\n[13b] everything that commits does it under the lock")
        lock_discipline_check()

        print("\n[14] the Dockerfile still quiets FastMCP down")
        dockerfile_env_check()

        print("\n[14b] the Gate is wired to the hook it claims")
        gate_hook_check()

        print("\n[14b2] every tool's refusals go through the converter")
        tool_conversion_check()

        print("\n[14c] the manual says what the code actually offers")
        guide_signature_check()

        print("\n[14d] the log level cannot kill the service at import")
        log_level_checks()

        print("\n[15] the IP filter list, in both directions")
        cidr_checks()

        print(f"\n{'=' * 46}\n  {OK} passed, {FAIL} failed\n{'=' * 46}")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
