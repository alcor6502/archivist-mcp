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
from vault import VaultRoot, Dataset, VaultError, guide_for  # noqa: E402

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


def _engine_pkg_dir():
    """Where the INSTALLED engine lives, or None. The checks that used to read
    the local Gate and the local decorator now read the engine's files: the pin
    in requirements.txt decides what those files say, so a stale or doctored
    engine goes red here instead of misbehaving in a chat."""
    import importlib.util
    spec = importlib.util.find_spec("mcp_common_engine")
    return Path(spec.origin).parent if spec and spec.origin else None


def _sole_import(tree, name: str, module: str) -> None:
    """The name is bound exactly once, by ONE module-level `from <module>
    import <name>`, and never bound to anything else afterwards.

    Python gives the name to whatever was bound last, in silence. A second
    `class Gate` further down, `Gate = _NoGate` on the line above the
    registration, `tool = mcp.tool` before the first `@tool` — each leaves the
    true binding in place for a reader to find and hands the running server
    the other one. All of them were executed on one twin or the other, and
    every suite stayed green until this shape of check existed."""
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)
               and n.module == module
               and any((a.asname or a.name) == name for a in n.names)]
    ok(len(imports) == 1,
       f"server.py imports `{name}` from {module}, exactly once, at module level",
       len(imports))
    other = []
    for n in ast.walk(tree):
        hit = False
        if isinstance(n, (ast.Import, ast.ImportFrom)) and n not in imports:
            hit = any((a.asname or a.name.split(".")[0]) == name for a in n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            hit = n.name == name
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign,
                            ast.NamedExpr, ast.For)):
            hit = any(isinstance(x, ast.Name) and x.id == name
                      and isinstance(x.ctx, ast.Store) for x in ast.walk(n))
        elif isinstance(n, ast.Global):
            hit = name in n.names
        if hit:
            other.append(ast.unparse(n)[:50])
    ok(not other, f"and the name `{name}` is never bound to anything else", other)


def gate_hook_check() -> None:
    """The Gate lives in the engine since 2.5.0, and it is wired by NAMING a
    hook: the Middleware base class ships a pass-through default for every hook
    it knows, so `on_requst` — one letter short — is not an error. It is a
    method nobody ever calls, and the gate is off. Nothing fails, nothing logs,
    and the server happily answers a stranger. No runtime test would notice,
    because the tests never build a FastMCP server: this reads the source —
    server.py for the seam, the INSTALLED engine for the class. The checks that
    guarded the local class did not retire with it; they changed address.

    What stays ours is the seam: WHICH Gate arrives, that it is registered
    exactly once, and that it is built with OUR config injected — the engine
    reads no module globals of anybody's, so forgetting a keyword here is a
    gate with somebody else's filter."""
    tree = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))

    _sole_import(tree, "Gate", "mcp_common_engine.gate")

    # Read from the AST, never from the text. `"mcp.add_middleware(" in src`
    # stays green on a commented-out registration — the single most likely way
    # that line ever disappears: somebody comments it out while chasing
    # something else. A comment does not exist in the tree.
    registered = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == "mcp.add_middleware"]
    ok(len(registered) == 1, "the Gate is registered, exactly once", len(registered))
    if registered:
        arg = registered[0].args[0] if registered[0].args else None
        ok(isinstance(arg, ast.Call) and ast.unparse(arg.func) == "Gate",
           "and what is registered is a Gate(...) call, not a stand-in",
           ast.unparse(arg)[:60] if arg is not None else "no argument")
        kw = ({k.arg: ast.unparse(k.value) for k in arg.keywords}
              if isinstance(arg, ast.Call) else {})
        ok(kw == {"log": "log", "allowed_login": "ALLOWED_LOGIN",
                  "allowed_cidrs": "ALLOWED_CIDRS"},
           "built with OUR logger, OUR login and OUR filter, injected — the "
           "engine reads no globals, so a missing keyword is not a default, "
           "it is a TypeError at boot or a gate around the wrong door", kw)

    pkg = _engine_pkg_dir()
    ok(pkg is not None, "mcp_common_engine is installed where the suite runs")
    if pkg is None:
        return
    gtree = ast.parse((pkg / "gate.py").read_text(encoding="utf-8"))
    gates = [n for n in gtree.body
             if isinstance(n, ast.ClassDef) and n.name == "Gate"]
    ok(len(gates) == 1, "the engine defines Gate exactly once", len(gates))
    if len(gates) != 1:
        return
    gate = gates[0]
    ok(any(ast.unparse(b) == "Middleware" for b in gate.bases),
       "the engine's Gate subclasses Middleware, which is what makes a hook a hook",
       [ast.unparse(b) for b in gate.bases])

    # ALL the assignments, not the first: a second `HOOK = ...` underneath is
    # what wins at runtime.
    assigned = [s.value.value for s in gate.body
                if isinstance(s, ast.Assign)
                and any(getattr(t, "id", "") == "HOOK" for t in s.targets)
                and isinstance(s.value, ast.Constant)]
    declared = assigned[-1] if assigned else None
    ok(assigned == ["on_request"],
       "Gate.HOOK pins the decision: on_request, assigned exactly once", assigned)

    hooks = {n.name for n in gate.body
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
             and n.name.startswith("on_")}
    ok(hooks == {declared}, "the Gate hooks exactly what HOOK names", sorted(hooks))

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
        # is part of the contract, not of the comfort.
        warns = [n for n in ast.walk(hook) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == "self.log.warning"]
        ok(len(warns) >= 2,
           "both refusals are logged, identity and origin", len(warns))
        ok(all(any(ast.unparse(a) == "ctx.method" for a in w.args) for w in warns),
           "and each refusal names the method it turned away")


def tool_conversion_check() -> None:
    """Every tool goes through the @tool decorator, and the decorator really
    converts. Since 2.5.0 the decorator is built by the ENGINE's make_tool;
    what stays here is the BINDING — which class is a refusal and which is a
    fault is the one thing the engine cannot know.

    The check changed SUBJECT with the move, exactly as the engine's docstring
    demands, and it was rewritten BEFORE the copies were stripped. The old law
    was "one `def tool`, never rebound" — and `tool = make_tool(...)` is an
    ASSIGNMENT, the very shape that law forbade, because `tool = mcp.tool`
    registers every tool naked while every counter keeps agreeing. The new
    law: the name `tool` is bound exactly once, at module level, to a call of
    make_tool from the engine, with OUR classes, and to nothing else, ever.

    The wrapper's shape — functools.wraps, `from None`, the INFO line, the
    fault branch FIRST — is still pinned piece by piece, read from the
    INSTALLED engine's refusals.py: the pin in requirements.txt decides what
    those files say, so a stale or doctored engine goes red here instead of
    misbehaving in a chat. The log of 2026-Aug-09 is what all of it exists to
    keep from coming back: three refusals, three tracebacks of twenty-five
    lines, two of them CONFLICTs on edit_file — the CAS doing its job, logged
    as a fault.

    A tool that lost its decorator ALTOGETHER is caught elsewhere and on
    purpose: it stops being a tool, so guide_signature_check() sees its
    signature still promised by the manual and names it. The two checks cover
    the two ways of falling out."""
    src = (HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    _sole_import(tree, "make_tool", "mcp_common_engine.refusals")

    # The name `tool` is bound EXACTLY once, and every kind of binding counts:
    # def, class, import, assignment in any form, global, walrus, for-target.
    # Python gives the name to whatever was bound last, without saying so.
    binds = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.name == "tool":
            binds.append(n)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name.split(".")[0]) == "tool" for a in n.names):
                binds.append(n)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign,
                            ast.NamedExpr, ast.For)):
            if any(isinstance(x, ast.Name) and x.id == "tool"
                   and isinstance(x.ctx, ast.Store) for x in ast.walk(n)):
                binds.append(n)
        elif isinstance(n, ast.Global) and "tool" in n.names:
            binds.append(n)
    ok(len(binds) == 1, "`tool` is bound exactly once in server.py",
       [ast.unparse(n)[:60] for n in binds])
    bind = binds[0] if len(binds) == 1 else None
    ok(isinstance(bind, ast.Assign) and bind in tree.body,
       "and that binding is the one assignment, at module level",
       ast.unparse(bind)[:60] if bind is not None else "absent")
    if isinstance(bind, ast.Assign):
        val = bind.value
        ok(isinstance(val, ast.Call) and isinstance(val.func, ast.Name)
           and val.func.id == "make_tool",
           "and it is a call of make_tool — `tool = mcp.tool` is the naked door",
           ast.unparse(val)[:60])
        if isinstance(val, ast.Call):
            pos = [ast.unparse(a) for a in val.args]
            kw = {k.arg: ast.unparse(k.value) for k in val.keywords}
            ok(pos == ["mcp", "log"]
               and kw == {"refusal": "VaultError", "fault": "VaultFault"},
               "with our server, our logger, VaultError as the refusal and "
               "VaultFault as the fault — swapped, every fault takes the "
               "quiet path", f"{pos} {kw}")

    # `mcp.tool` is never called in server.py at all now: the one legitimate
    # call lives inside the engine's make_tool. Any occurrence here is a tool
    # that converts nothing.
    naked = [ast.unparse(n)[:60] for n in ast.walk(tree) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "mcp.tool"]
    ok(not naked, "`mcp.tool` is never called in server.py — the one call "
                  "lives inside the engine's make_tool", naked)

    # THE WRAPPER, read from the installed engine — not from a local copy that
    # no longer exists, and not trusted to the tag's name alone.
    pkg = _engine_pkg_dir()
    ok(pkg is not None, "mcp_common_engine is installed where the suite runs")
    guarded = None
    if pkg is not None:
        rtree = ast.parse((pkg / "refusals.py").read_text(encoding="utf-8"))
        makes = [n for n in rtree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "make_tool"]
        ok(len(makes) == 1, "the engine defines make_tool exactly once", len(makes))
        factory = makes[0] if makes else None
        if factory is not None:
            guarded = next((n for n in ast.walk(factory)
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and n.name == "guarded"), None)
            ok(guarded is not None, "the engine's factory defines its wrapper, `guarded`")
            rebound = [ast.unparse(n) for n in ast.walk(factory)
                       if isinstance(n, ast.Assign)
                       and any(getattr(t, "id", "") == "guarded" for t in n.targets)]
            ok(not rebound, "`guarded` is only ever the def, never reassigned —"
               " `guarded = fn` is this very check's own defect, wearing the "
               "name the check looks for", rebound)
            registers = [n for n in ast.walk(factory) if isinstance(n, ast.Call)
                         and ast.unparse(n.func) == "mcp.tool"]
            ok(len(registers) == 1 and registers[0].args
               and ast.unparse(registers[0].args[0]) == "guarded",
               "and it registers the WRAPPED function, not the bare one",
               [ast.unparse(n)[:40] for n in registers])

    # From here on, everything is read out of the function that is REGISTERED.
    # One try, in its body — not somewhere in the subtree, which is how a dead
    # branch gets to answer for a live one. In the engine the two classes are
    # the factory's PARAMETERS, `fault` and `refusal`; the binding above is
    # what makes them VaultFault and VaultError here.
    if guarded is None:
        return
    tries = [s for s in guarded.body if isinstance(s, ast.Try)]
    ok(len(tries) == 1, "which wraps the call in exactly one try/except", len(tries))
    if not tries:
        return
    ok(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "fn"
           for n in ast.walk(tries[0].body[0] if tries[0].body else tries[0])),
       "and the thing inside the try is the original function")

    handlers = tries[0].handlers
    caught = [ast.unparse(h.type) if h.type is not None else "*" for h in handlers]
    ok("refusal" in caught, "the wrapper catches the refusal class", caught)

    # The ORDER, which is the whole distinction and is invisible once written:
    # the fault SUBCLASSES the refusal, so the refusal caught first would
    # swallow every fault into the quiet path and nothing would look wrong.
    # Python has no warning for this; the test is the warning. Taken from the
    # handlers of the ONE try above, not from the first match in the subtree —
    # a decoy handler written higher up would answer for it.
    ok(caught[:1] == ["fault"],
       "and it catches the fault FIRST, or the subclass never gets its turn",
       caught)
    fault_h = [h for h in handlers if ast.unparse(h.type or ast.Constant(0)) == "fault"]
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
    conv_h = [h for h in handlers if ast.unparse(h.type or ast.Constant(0)) == "refusal"]
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

    ok(any(ast.unparse(d) == "functools.wraps(fn)" for d in guarded.decorator_list),
       "and functools.wraps ON THE REGISTERED FUNCTION, or it loses its schema",
       [ast.unparse(d) for d in guarded.decorator_list])

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


def engine_adoption_check() -> None:
    """The engine is pinned to a tag, and the pin is what is installed.

    Two repositories pinning a third can pin different tags, and then
    "identical" quietly becomes "identical if both of them updated". The cure
    is to compare the number where somebody already looks: here, against the
    package that actually installed, and on the startup line, next to our own
    version. Both halves are checked, because each one alone is a number
    nobody reads twice."""
    req = (HERE / "requirements.txt").read_text(encoding="utf-8")

    # The tarball form, not git+: the image carries no git dependency for pip,
    # and a public tarball needs none. One pin, one tag.
    pins = re.findall(r"^mcp-common-engine @ https://github\.com/alcor6502/"
                      r"mcp-common-engine/archive/refs/tags/v(\d+\.\d+\.\d+)"
                      r"\.tar\.gz\s*$", req, re.MULTILINE)
    ok(len(pins) == 1,
       "requirements.txt pins the engine to ONE tag, in the tarball form", pins)

    # fastmcp's version is pinned in the ENGINE's pyproject, where the code
    # that depends on its routing lives. A second pin here would be the same
    # number in two places, with the expiry date that comes with it.
    ok(not re.search(r"^fastmcp", req, re.MULTILINE),
       "fastmcp is not pinned a second time in requirements.txt")

    import importlib.util
    ok(importlib.util.find_spec("mcp_common_engine") is not None,
       "mcp_common_engine imports where the suite runs")
    import mcp_common_engine as eng
    ok(pins == [eng.VERSION],
       f"and the engine installed here IS that tag: {eng.VERSION}",
       f"pin {pins} vs installed {eng.VERSION}")

    tree = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))

    # The startup line carries the engine's version next to our own — the cure
    # the engine's README names for the drift its pin makes possible.
    main_block = next((n for n in tree.body if isinstance(n, ast.If)
                       and ast.unparse(n.test) == "__name__ == '__main__'"), None)
    ok(main_block is not None, "server.py has the __main__ block")
    if main_block is not None:
        infos = [c for c in ast.walk(main_block) if isinstance(c, ast.Call)
                 and ast.unparse(c.func) == "log.info"]
        ok(any(any(isinstance(a, ast.Name) and a.id == "ENGINE_VERSION"
                   for a in c.args) for c in infos),
           "and the startup line carries ENGINE_VERSION next to VERSION")

    # preflight.py reaches the engine through its ROOT only: the root import
    # drags no fastmcp in, by the engine's own contract, and that is what lets
    # a preflight run — and report — on an image where fastmcp is broken.
    ptree = ast.parse((HERE / "preflight.py").read_text(encoding="utf-8"))
    pmods = {n.module for n in ast.walk(ptree)
             if isinstance(n, ast.ImportFrom) and n.module
             and n.module.startswith("mcp_common_engine")}
    ok(pmods == {"mcp_common_engine"},
       "preflight.py imports from the engine's root only — gate and refusals "
       "would drag fastmcp into the one file that must run without it", pmods)

    # And the moved names are not quietly redefined here: a local def wins
    # over the import for everything below it, and the twins would drift again
    # behind an engine nobody actually runs.
    moved = {"is_placeholder", "parse_cidrs", "cidrs_from_env",
             "describe_cidrs", "log_level_from_env", "check"}
    redefined = [n.name for n in ast.walk(ptree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name in moved]
    ok(not redefined, "none of the moved helpers is redefined in preflight.py",
       redefined)

    # The CI test job installs what the suite now imports, from the SAME pin:
    # --no-deps, because the suite needs no FastMCP — that property is what
    # lets it run in under a minute, and it is not one to give up.
    wf = (HERE / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    ok("pip install --no-deps -r requirements.txt" in wf,
       "the CI test job installs the engine from the same pin, --no-deps")


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


def redaction_armed_check() -> None:
    """A malformed call must not print the arguments it carried, and the guard
    that stops it is one call whose ORDER is the whole of it.

    The filter has to sit on fastmcp's HANDLERS, and fastmcp installs those when
    it configures its logging — which building the server is what triggers.
    Called before that line it finds nothing to arm. That case raises rather
    than returning zero, so it cannot pass unnoticed at runtime; what CANNOT be
    seen at runtime is the call having been deleted, which is silent and leaves
    the payload printing again. So the call is read here, and so is its
    position."""
    src = (HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    _sole_import(tree, "arm_argument_redaction", "mcp_common_engine.logs")

    calls = [n for n in tree.body if isinstance(n, ast.Expr)
             and isinstance(n.value, ast.Call)
             and ast.unparse(n.value.func) == "arm_argument_redaction"]
    ok(len(calls) == 1,
       "arm_argument_redaction() is called exactly once, at module level — "
       "without it fastmcp prints the whole argument dict of any malformed "
       "call, and for this server the arguments ARE the documents", len(calls))

    server = next((n for n in tree.body if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", "") == "mcp" for t in n.targets)
                   and isinstance(n.value, ast.Call)
                   and ast.unparse(n.value.func) == "FastMCP"), None)
    ok(server is not None, "the server object is built at module level")
    ok(server is not None and calls and calls[0].lineno > server.lineno,
       "and the arming comes AFTER it: fastmcp installs the handlers when it "
       "configures its logging, so arming any earlier finds nothing to arm",
       f"arm at {calls[0].lineno if calls else '-'}, "
       f"server at {server.lineno if server else '-'}")

    # The engine has to be new enough to have the module at all. The pin is
    # compared with the installed package in engine_adoption_check(); this is
    # the other half — that what installed actually carries the function.
    from mcp_common_engine.logs import arm_argument_redaction as _arm
    ok(callable(_arm), "and the installed engine really provides it")


def timestamps_armed_check() -> None:
    """fastmcp's own lines have to carry our clock, and our format has to be
    ONE string.

    The sibling of the check above, and it shares its grip: same logger, same
    handlers, same call site. What is specific here is the format. It is
    written once and passed twice — to `basicConfig` and to `arm_timestamps` —
    because two copies of a format string agree until somebody edits one, and
    the symptom then is two shapes of line in one log, which reads as two
    services rather than as a defect.

    A literal in either place would pass a string search and would be exactly
    the failure this reads for."""
    src = (HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    _sole_import(tree, "arm_timestamps", "mcp_common_engine.logs")

    assigns = [n for n in tree.body if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "LOG_FORMAT" for t in n.targets)]
    ok(len(assigns) == 1, "LOG_FORMAT is assigned exactly once", len(assigns))
    fmt = ast.literal_eval(assigns[0].value) if assigns else None
    ok(fmt is not None and "%(asctime)s" in fmt,
       "and the format really carries a timestamp — the one field the whole "
       "cure exists for", fmt)

    basic = next((n for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == "logging.basicConfig"), None)
    ok(basic is not None, "logging.basicConfig is called")
    given = next((k.value for k in (basic.keywords if basic else [])
                  if k.arg == "format"), None)
    ok(given is not None and ast.unparse(given) == "LOG_FORMAT",
       "and it is handed the NAME, not a second copy of the string",
       ast.unparse(given) if given is not None else None)

    calls = [n for n in tree.body if isinstance(n, ast.Expr)
             and isinstance(n.value, ast.Call)
             and ast.unparse(n.value.func) == "arm_timestamps"]
    ok(len(calls) == 1,
       "arm_timestamps() is called exactly once, at module level — without it "
       "fastmcp's lines keep coming out with no date, and a line with no time "
       "correlates with nothing", len(calls))
    arg = calls[0].value.args[0] if calls and calls[0].value.args else None
    ok(arg is not None and ast.unparse(arg) == "LOG_FORMAT",
       "and it is given the same NAME basicConfig got — one format, one place",
       ast.unparse(arg) if arg is not None else None)

    server = next((n for n in tree.body if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", "") == "mcp" for t in n.targets)
                   and isinstance(n.value, ast.Call)
                   and ast.unparse(n.value.func) == "FastMCP"), None)
    ok(server is not None and calls and calls[0].lineno > server.lineno,
       "and the arming comes AFTER the server object, for the same reason its "
       "sibling does: fastmcp installs the handlers when it configures its "
       "logging",
       f"arm at {calls[0].lineno if calls else '-'}, "
       f"server at {server.lineno if server else '-'}")

    from mcp_common_engine.logs import arm_timestamps as _arm
    ok(callable(_arm), "and the installed engine really provides it")


def icon_check() -> None:
    """The icon URL is written in two files that nothing links together: the
    Unraid template, which puts it on the container, and server.py, which hands
    it to FastMCP for the consent page. Two hand copies of one string do not
    stay equal — they have an expiry date — so this is the thing that compares
    them instead of hoping.

    It reads the ASSIGNMENT and then the CALL, because the two failures are
    different: a constant left behind after the argument was dropped would keep
    a string-search happy while the server passed no icon at all."""
    src = (HERE / "server.py").read_text(encoding="utf-8")
    xml = (HERE / "archivist-mcp.xml").read_text(encoding="utf-8")
    tree = ast.parse(src)

    assigns = [n for n in tree.body if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "ICON_URL" for t in n.targets)]
    ok(len(assigns) == 1, "ICON_URL is assigned exactly once", len(assigns))
    url = ast.literal_eval(assigns[0].value) if assigns else None

    in_xml = re.search(r"<Icon>\s*(\S+?)\s*</Icon>", xml)
    ok(in_xml is not None, "the Unraid template still declares an <Icon>")
    ok(in_xml is not None and url == in_xml.group(1),
       "the icon of the consent page and the icon of the container are the "
       "SAME url — one image, or the two drift and nobody notices",
       f"{url} vs {in_xml.group(1) if in_xml else None}")

    # The constant has to REACH FastMCP. A name that nothing passes is a
    # comment with a colon in it.
    call = next((n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == "FastMCP"), None)
    ok(call is not None, "FastMCP is constructed in server.py")
    icons = next((k.value for k in (call.keywords if call else [])
                  if k.arg == "icons"), None)
    ok(icons is not None, "and it is given an `icons` argument — without it "
       "the constant above is decoration")
    ok(icons is not None and "ICON_URL" in ast.unparse(icons),
       "and that argument carries ICON_URL, not a second copy of the string",
       ast.unparse(icons)[:80] if icons is not None else None)
    ok(icons is not None and "image/png" in ast.unparse(icons)
       and url is not None and url.endswith(".png"),
       "the declared mimeType matches the file actually pointed at")


# The variables the ENGINE reads on our behalf, so the template declares them
# and no file of ours mentions them. Named ONE BY ONE and then verified against
# the installed engine: an exemption phrased as "anything the engine might
# read" would be the hole this check exists to close, and an exemption nobody
# rechecks outlives the reader it was granted for.
ENGINE_READS = ("ALLOWED_CIDRS", "LOG_LEVEL")

# Names that must NOT be offered on the container, each for its own reason.
# The check above would already refuse a name nothing reads; this one keeps
# refusing it the day somebody adds a reader back, which is not the same event.
#   BIND_HOST       retired in 2.2.0: its only power was to bypass the Funnel
#   PREFLIGHT_SKIP  read by the engine, for local testing — a field on the
#                   container would be a documented way to disable the checks
#                   that stop an authless Funnel
#   ANTHROPIC_CIDR  deprecated, still honoured for containers that carry it;
#                   offering it to a NEW install would spread the old name
NEVER_DECLARED = ("BIND_HOST", "PREFLIGHT_SKIP", "ANTHROPIC_CIDR")


def _env_readers(*modules: str) -> set[str]:
    """Every environment variable name these modules actually READ.

    From the AST, and that is the whole point: a string search is satisfied by
    a comment, and a comment is precisely where a variable that has stopped
    being read goes to be talked about in the past tense.
    """
    names: set[str] = set()
    for mod in modules:
        tree = ast.parse((HERE / mod).read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call)
                    and ast.unparse(n.func) in ("os.environ.get", "os.getenv", "env")
                    and n.args and isinstance(n.args[0], ast.Constant)):
                names.add(n.args[0].value)
            elif (isinstance(n, ast.Subscript)
                    and ast.unparse(n.value) == "os.environ"
                    and isinstance(n.slice, ast.Constant)):
                names.add(n.slice.value)
    return names


def template_variable_check() -> None:
    """Every variable the Unraid template declares must have a reader.

    THE DIRECTION IS THE WHOLE THING. A check shaped "the template must declare
    PORT, BASE_URL, ..." catches an OMISSION and can never catch a SURVIVOR: the
    day a variable stays in the template and its last reader is deleted, that
    list has nothing to say. And a missing variable is the cheap failure — the
    preflight refuses and names it at first boot. A variable the template offers
    and nobody reads never fails at all: it is a field somebody fills in with
    care whose value goes nowhere, and the symptom arrives much later wearing
    another face — "I set it to 60 and it counts 90".

    Written after codifier-mcp found four of them in its own template on
    2026-08-14, three releases old, with the code's comments already speaking of
    them in the past tense. We had none; this is what keeps it that way.

    `Target` is filtered to SHOUTING_CASE because path and port mappings use the
    same attribute (`Target="/vault"`, `Target="9443"`), and the template is
    parsed as XML rather than grepped: attribute order is not a promise.

    entrypoint.sh is read too — it is where VAULT_UID and VAULT_GID are used,
    and a check that only looked at the Python would call them dead.
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(HERE / "archivist-mcp.xml").getroot()
    declared = {c.get("Target") for c in tree.iter("Config")
                if c.get("Type") == "Variable"
                and re.fullmatch(r"[A-Z][A-Z0-9_]*", c.get("Target") or "")}
    ok(len(declared) > 5, "the template declares variables at all", len(declared))

    readers = _env_readers("server.py", "vault.py", "preflight.py")
    # The shell reads by expansion, and once through an inline python -c.
    sh = (HERE / "entrypoint.sh").read_text(encoding="utf-8")
    readers |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)[:\-}]", sh))
    readers |= set(re.findall(r"""os\.environ\.get\(["']([A-Z][A-Z0-9_]*)["']""", sh))

    # The exemptions are only worth what they still describe.
    engine_dir = _engine_pkg_dir()
    ok(engine_dir is not None, "the engine is installed, so its reads can be "
       "verified rather than assumed")
    engine_names: set[str] = set()
    for mod in sorted(engine_dir.glob("*.py")) if engine_dir else []:
        t = ast.parse(mod.read_text(encoding="utf-8"))
        for n in ast.walk(t):
            if (isinstance(n, ast.Call)
                    and ast.unparse(n.func) in ("os.environ.get", "os.getenv")
                    and n.args and isinstance(n.args[0], ast.Constant)):
                engine_names.add(n.args[0].value)
    for name in ENGINE_READS:
        ok(name in engine_names,
           f"{name} is exempted because the INSTALLED engine reads it — "
           f"and it still does", sorted(engine_names))

    orphans = sorted(declared - readers - set(ENGINE_READS))
    ok(not orphans,
       f"every variable the template declares has a reader "
       f"({len(declared)} declared)", orphans)

    resurrected = sorted(set(NEVER_DECLARED) & declared)
    ok(not resurrected,
       "and none of the names that must never be offered on the container is "
       "back in the template", resurrected)


def entrypoint_growth_check() -> None:
    """The entrypoint runs at every restart, and the service user's HOME sits
    on the persistent volume: a `git config --global --add` there appends one
    more identical safe.directory line per restart, read by every git call
    the service makes, for as long as the volume lives. `--replace-all` sets
    the same value and leaves one line. The check reads every safe.directory
    line and names the ones that would grow."""
    sh = (HERE / "entrypoint.sh").read_text(encoding="utf-8")
    lines = [l.strip() for l in sh.splitlines() if "safe.directory" in l and not l.strip().startswith("#")]
    ok(len(lines) == 2, "entrypoint.sh sets safe.directory twice: as root and as the service user", lines)
    growing = [l for l in lines if "--replace-all" not in l]
    ok(not growing, "and both use --replace-all, so a restart adds no line to .gitconfig", growing)


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


def http_mode_checks() -> None:
    """The transport mode, in both directions.

    The default is the whole point: Unraid does not propagate a new variable to
    containers already installed, so an installation that has never heard of
    HTTP_MODE must keep the behaviour it had. Every case below where the
    variable is absent, empty or unreadable has to come back `stateful`, and
    the unreadable ones have to SAY what they rejected — a knob that silently
    ignores you is how you get told the feature is broken.

    And the other direction, which an "it must default to stateful" test cannot
    see: `stateless` has to actually arrive. A knob wired to a value nobody can
    reach defaults perfectly and does nothing."""
    from preflight import http_mode_from_env
    for value, expect_mode, expect_rejected in (
            (None, "stateful", None),          # not defined at all: the case
            ("", "stateful", None),            # of every container already
            ("   ", "stateful", None),         # installed
            ("stateful", "stateful", None),
            ("stateless", "stateless", None),
            ("STATELESS", "stateless", None),  # case is typography, not intent
            (" stateless ", "stateless", None),
            # Not a synonym here, however obvious it looks elsewhere: the value
            # is read into a log line and into an Unraid dropdown, and half a
            # vocabulary is worse than one.
            ("true", "stateful", "true"),
            ("1", "stateful", "1"),
            ("stateles", "stateful", "stateles"),
            ("session", "stateful", "session")):
        old = os.environ.pop("HTTP_MODE", None)
        try:
            if value is not None:
                os.environ["HTTP_MODE"] = value
            got = http_mode_from_env()
        finally:
            os.environ.pop("HTTP_MODE", None)
            if old is not None:
                os.environ["HTTP_MODE"] = old
        ok(got == (expect_mode, expect_rejected),
           f"HTTP_MODE={value!r} resolves to {expect_mode} "
           f"{'and says what it rejected' if expect_rejected else 'silently'}", got)


def http_mode_wiring_check() -> None:
    """The mode has to REACH fastmcp, and the log has to name the one running.

    server.py cannot be imported without fastmcp, so this is read from the
    source — and from the AST, because a string search is satisfied by the
    comment that explains the thing rather than by the thing.

    Three ways this goes wrong, and only the first is loud:
      - the argument is not passed at all: the knob moves nothing, and the only
        symptom is that the experiment says stateless changed nothing;
      - the argument is passed as a LITERAL: the mode freezes while the log
        goes on quoting the variable. That is not hypothetical, it is exactly
        how BIND_HOST broke — the line printed 127.0.0.1 as text while the bind
        followed the variable;
      - the startup line stops naming the mode: after an Apply the log is the
        only thing that says what is really running, and a mode written in a
        document is not a state."""
    src = (HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    _sole_import(tree, "http_mode_from_env", "preflight")

    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any("HTTP_MODE" in ast.unparse(t) for t in n.targets)]
    ok(len(assigns) == 1, "HTTP_MODE is resolved exactly once", len(assigns))
    ok(assigns and "http_mode_from_env()" in ast.unparse(assigns[0].value),
       "and it comes from the shared helper, not from a second os.environ read "
       "that agrees with the preflight only today",
       ast.unparse(assigns[0].value)[:60] if assigns else None)

    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == "mcp.run"]
    ok(len(runs) == 1, "mcp.run() is called exactly once", len(runs))
    arg = next((k.value for k in (runs[0].keywords if runs else [])
                if k.arg == "stateless_http"), None)
    ok(arg is not None,
       "and it is given `stateless_http` — the name fastmcp 3.4.5 takes on "
       "run_http_async and forwards from run(); without it HTTP_MODE is a "
       "label on a knob connected to nothing")
    ok(arg is not None and "HTTP_MODE" in ast.unparse(arg),
       "and its value is derived from HTTP_MODE, not written as a literal: a "
       "literal freezes the mode while the log keeps quoting the variable",
       ast.unparse(arg)[:60] if arg is not None else None)

    startup = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and ast.unparse(n.func) == "log.info"
               and n.args and isinstance(n.args[0], ast.Constant)
               and "starting on" in n.args[0].value]
    ok(len(startup) == 1, "the startup line is emitted exactly once", len(startup))
    ok(startup and any(ast.unparse(a) == "HTTP_MODE" for a in startup[0].args),
       "and it carries HTTP_MODE itself — not a second expression that says "
       "the same thing until one of the two is edited",
       ast.unparse(startup[0])[:90] if startup else None)

    # The template's dropdown and the code's closed list are two hand copies of
    # one set. The template is also the only half a hand-built container never
    # sees, which is why the list is closed in the code as well.
    xml = (HERE / "archivist-mcp.xml").read_text(encoding="utf-8")
    from preflight import HTTP_MODES
    field = re.search(r'<Config[^>]*Target="HTTP_MODE"[^>]*>', xml)
    ok(field is not None, "the Unraid template declares HTTP_MODE")
    default = re.search(r'Default="([^"]*)"', field.group(0)) if field else None
    ok(default is not None and tuple(default.group(1).split("|")) == HTTP_MODES,
       "and its dropdown offers exactly the values the code accepts, in the "
       "same order — the first one being the default the code falls back to",
       f"{default.group(1) if default else None} vs {'|'.join(HTTP_MODES)}")


def ship_scripts_check() -> None:
    """The delivery scripts, which are how a machine holding nothing but the
    clone runs the suite and ships: they must read the pin, never carry it.

    `scripts/test.sh` builds the bench from requirements.txt — the tarball, or
    a git clone of the SAME tag when the network refuses the tarball — so the
    number lives in one place and the adoption check above keeps it honest.
    `scripts/ship.sh` must run that suite before it commits, add NAMED files
    (never `-A`: a tree can hold another hand's half-written change), commit
    with the anonymous identity, and push to main — the branch the workflow's
    release link targets. What it must never do is push a tag: from a sandbox
    that answers 403, and a typed tag is where the case error comes from."""
    test_sh = HERE / "scripts" / "test.sh"
    ship_sh = HERE / "scripts" / "ship.sh"
    for f in (test_sh, ship_sh):
        ok(f.is_file() and os.access(f, os.X_OK), f"{f.relative_to(HERE)} exists and is executable")
    t = test_sh.read_text(encoding="utf-8") if test_sh.is_file() else ""
    s = ship_sh.read_text(encoding="utf-8") if ship_sh.is_file() else ""

    literal = re.findall(r"\bv\d+\.\d+\.\d+\b", t)
    ok(not literal and "requirements.txt" in t and "refs/tags" in t,
       "test.sh reads the engine tag out of requirements.txt and carries no tag of its own",
       literal)
    ok("mcp-common-engine.git" in t and "--no-deps" in t,
       "test.sh falls back to a git clone of the tag, installed --no-deps like CI")
    ok("suite.log" in t and "exit=" in t and "set -eu" in t,
       "test.sh logs the suite to a file and prints the exit code — the form that cannot lie")

    i_test, i_commit = s.find("scripts/test.sh"), s.find("commit -q")
    ok(0 <= i_test < i_commit, "ship.sh runs the suite BEFORE it commits", (i_test, i_commit))
    ok("git add -- \"$@\"" in s and "add -A" not in s,
       "ship.sh adds the files it was NAMED, never -A")
    ok("user.email=14092600+alcor6502@users.noreply.github.com" in s,
       "ship.sh commits with the anonymous identity, on the command")
    ok("HEAD:refs/heads/$BRANCH" in s and "BRANCH=main" in s,
       "ship.sh pushes straight to main")
    ok(not re.search(r"git push[^\n]*\bv\$", s) and "releases/new?" in s,
       "ship.sh never pushes a tag: it prints the release link instead")


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

    # The manual is cut into cards by vault.split_guide — the REAL function the
    # server calls, not a second parser written here to check the first. If the
    # cut moves, this moves with it or fails, which is the point.
    import vault as _v

    # Asked FIRST, by name. split_guide raises VaultFault without the separator,
    # and a check that dies in a traceback instead of failing by name is a check
    # in less: the suite goes red either way, but only one of the two tells you
    # that the heading is what went missing.
    ok("\n# COMMANDS\n" in guide,
       "the manual still carries the '# COMMANDS' heading the cut depends on")
    if "\n# COMMANDS\n" not in guide:
        return
    model, cards = _v.split_guide(guide)
    written = [c.splitlines()[0] for c in cards.values()]

    missing = [r for r in real if r not in written]
    ok(not missing, "every tool has a card, headed by its exact signature", missing)

    # The other direction, which is the one that rots silently: a card left
    # behind for a tool that was renamed or deleted. Comparing the whole set
    # means such a card has nowhere to hide — filtering by the names the code
    # still has would have made this check unable to see it.
    stale = [w for w in written if w not in real]
    ok(not stale, "no card promises a signature the code does not have", stale)

    # One card per tool, and the count is nobody's constant: it is the length of
    # the list on both sides. A tool added without a card, or a card added
    # twice, changes one of these two numbers and not the other.
    ok(len(cards) == len(real),
       "there are exactly as many cards as tools", f"{len(cards)} cards, {len(real)} tools")

    # The model page is what every caller pays for on the first read, so it is
    # kept to what a signature cannot say. This is not a style rule: the whole
    # point of the split is that the general page stays small, and a page that
    # grows back to holding every signature has quietly undone it.
    ok(len(model) < 4000,
       "the model page is still the short one, not the manual again", len(model))

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
    # form a person writes, and then looked for INSIDE THE CARD of the tool it
    # governs — not in the file at large, where "2 MB" is satisfied several
    # times over by a single occurrence somewhere else and a wrong number would
    # sail through. There is no second copy of any number here: change vault.py
    # and this expects the new value.
    mb = lambda n: f"{n // 1_000_000} MB"
    kb = lambda n: f"{n // 1_000} KB"
    ok(_v.MAX_READ_BYTES == _v.MAX_WRITE_BYTES,
       "reads and writes share one ceiling, as one card claims for both")
    limits = [("write_file", mb(_v.MAX_WRITE_BYTES)),
              ("read_binary", mb(_v.MAX_BINARY_BYTES)),
              ("write_binary", mb(_v.MAX_BINARY_BYTES)),
              ("append", kb(_v.MAX_APPEND_BYTES)),
              ("list_files", f"{_v.MAX_LIST_FILES:,} files"),
              ("search", f"{_v.MAX_SEARCH_HITS} lines"),
              ("diff", kb(_v.MAX_DIFF_BYTES)),
              ("archive", mb(_v.MAX_ARCHIVE_OUT_BYTES)),
              ("dataset_create", f"{_v.MAX_DATASETS} datasets")]
    for name, shown in limits:
        ok(name in cards and shown in cards[name],
           f"the card for {name} still states the limit the code enforces", shown)


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

        print("\n[2d] a link planted inside the dataset is not a file of the dataset")
        # _resolve refused READING a symlink that points outside — and the
        # walkers never went through _resolve: `search` returned the content
        # of a file outside the dataset through a link inside it. The target
        # here is the key registry, the file the whole model exists to keep
        # out of reach. Every walker must leave the link out, by name.
        _link = Path(root) / "Example Project" / "escape.md"
        _link.symlink_to(Path(root) / "keys.txt")
        try:
            must_fail("reading the link", lambda: ds.read_file("escape.md"))
            _paths = [f["path"] for f in ds.list_files("")["files"]]
            ok("escape.md" not in _paths, "list_files leaves the link out", _paths)
            _hits = ds.search(KEY, "")
            ok(_hits["matches"] == 0 and _hits["files_scanned"] == len(_paths),
               "search does not read through the link — the key is not findable", _hits)
            ok(ds.manifest("")["file_count"] == len(_paths),
               "manifest counts the same files list_files does", ds.manifest("")["file_count"])
            import tarfile as _tf, io as _io, base64 as _b64
            _tgz = ds.archive("", "*")
            _names = _tf.open(fileobj=_io.BytesIO(_b64.b64decode(_tgz["tgz_base64"]))).getnames()
            ok("escape.md" not in _names and _tgz["skipped_by_pattern"] == 0,
               "archive packs neither the link nor its target, and does not count it as skipped",
               (_names, _tgz["skipped_by_pattern"]))
        finally:
            _link.unlink()

        print("\n[2e] a NUL byte in a path is a refusal, not a traceback")
        # The kernel refuses it and Python turns that into a bare ValueError:
        # a fault in the log, with a traceback, for the caller's malformed
        # argument. Both doors — resolution and the per-dataset resolver.
        # must_fail would let a ValueError escape and take the suite down: the
        # verdict here is WHICH exception, so it is caught by hand and named.
        for _label, _fn in (
            ("NUL through the root door", lambda: v.open("Example Project", "a\x00.md", KEY)),
            ("NUL through the dataset resolver", lambda: ds.read_file("a\x00.md")),
            ("NUL in a write", lambda: ds.write_file("a\x00.md", "x", "new")),
        ):
            try:
                _fn()
                ok(False, _label, "did NOT fail")
            except VaultError:
                ok(True, f"{_label} (refused)")
            except Exception as _e:  # noqa: BLE001 — the point is the type
                ok(False, _label, f"escaped as {type(_e).__name__}: {_e}")

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

        print("\n[3c] the walker never enters .git, and lists what the old rule listed")
        # Two things, and the second is what makes the first checkable. The
        # listing must equal the rule every walker used until 2.9.0 —
        # sorted(rglob("*")) minus _skip — on a tree with the shapes that rule
        # met: nested folders, Trash, .gitignore, the lockfile, a link. And
        # .git must never be READ: os.scandir is wrapped to record every
        # directory opened, and the objects folder is proved non-empty first,
        # or the check would be green on a repository with nothing to skip.
        import unittest.mock as _mock
        _ep = Path(root) / "Example Project"
        (_ep / ".archivist.lock").touch()
        _link = _ep / "01 Notes" / "link.md"; _link.symlink_to(_ep / "log.md")
        try:
            _old_rule = sorted(q for q in _ep.rglob("*")
                               if not (q.is_symlink() or not q.is_file() or ".git" in q.parts
                                       or q.name == ".archivist.lock"))
            ok(len(list((_ep / ".git" / "objects").rglob("*"))) > 5,
               "the repository has loose objects for a walker to trip over")
            _opened = []
            _real = os.scandir
            def _spy(path=".", *a, **k):
                _opened.append(str(path)); return _real(path, *a, **k)
            with _mock.patch("os.scandir", _spy):
                _walked = ds._walk(_ep)
            ok(_walked == _old_rule, "the walk lists exactly what sorted(rglob) minus _skip listed",
               [str(x) for x in set(_walked) ^ set(_old_rule)])
            _git_dirs = [o for o in _opened if "/.git" in o]
            ok(_opened and not _git_dirs, "and never opened a directory under .git", _git_dirs[:3])
        finally:
            _link.unlink()
            (_ep / ".archivist.lock").unlink()

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

        print("\n[4b] a refused append leaves NOTHING on disk")
        # A file a few bytes under the read ceiling, plus a block that crosses
        # it. The refusal alone proves nothing — the read-back after the write
        # refused this too, with the block already on disk and the tree dirty,
        # so the next tool call committed it as "external". What has to hold is
        # the state: same bytes, clean tree. Remove the guard and both go red.
        import vault as _v
        _big = Path(root) / "Example Project" / "big.md"
        _big.write_bytes(b"x" * (_v.MAX_READ_BYTES - 10) + b"\n")
        ds._commit("setup big")
        _before = _big.stat().st_size
        must_fail("append that would cross the read ceiling",
                  lambda: ds.append("big.md", "y" * 40))
        ok(_big.stat().st_size == _before,
           "the file on disk is exactly as it was", (_before, _big.stat().st_size))
        ok(ds.status()["git"] == "clean",
           "and the tree is clean: nothing for the next call to commit as external",
           ds.status()["git"])
        _big.unlink()
        ds._commit("cleanup big")

        print("\n[4d] append takes an OPTIONAL sha, and with it a retry cannot double the block")
        # The shape of the defect: a lost response on an append, and a blind
        # retry. Without a sha the block lands twice and nothing says so; with
        # the sha of the file as it was BEFORE the first append, the retry is
        # refused, because the first one moved the file. All three directions:
        # omitted (as before), right, wrong — and the state after each.
        _log = ds.read_file("log.md")
        _before = _log["sha256"]
        _r1 = ds.append("log.md", "once", _before)
        ok(_r1["sha256"] != _before and _r1["commit"] != "(nothing to commit)",
           "append with the CURRENT sha passes and moves the file")
        must_fail("the same append retried with the OLD sha is refused (the first one arrived)",
                  lambda: ds.append("log.md", "once", _before))
        ok(ds.read_file("log.md")["content"].count("once") == 1,
           "and the block is in the file exactly once", ds.read_file("log.md")["content"].count("once"))
        _r2 = ds.append("log.md", "twice")
        ok(_r2["sha256"] != _r1["sha256"], "append without a sha still works as it always did")
        ok(ds.append("log.md", "thrice", _r2["sha256"])["commit"] != "(nothing to commit)",
           "and the sha a previous append handed back is accepted by the next one")
        must_fail("a sha that never was is refused", lambda: ds.append("log.md", "x", "0" * 64))

        print("\n[4c] a write costs four git processes, a boot on a clean dataset one")
        # Counted, not timed: a process is the unit of cost here — a few ms
        # on an idle box, tens under load — and the count is what a refactor
        # moves without anyone noticing, while a timing hides it in the fsync.
        # Every git call goes through subprocess.run, wrapped to record the verb.
        import unittest.mock as _mock
        import vault as _v
        _verbs = []
        _real_run = _v.subprocess.run
        def _count(argv, *a, **k):
            if argv and argv[0] == "git":
                _verbs.append(argv[3])
            return _real_run(argv, *a, **k)
        with _mock.patch.object(_v.subprocess, "run", _count):
            ds.write_file("count.md", "n\n", "new")
        ok(_verbs == ["status", "add", "commit", "rev-parse"],
           "write_file on a clean tree: status, add, commit, rev-parse — and nothing else", _verbs)
        _verbs.clear()
        with _mock.patch.object(_v.subprocess, "run", _count):
            v.boot(0)
        ok(_verbs == ["status"] * len(v.dataset_names()),
           "boot over clean repositories: one `status` per dataset, no config rewrite", _verbs)
        ok(ds.history("count.md", 1)["entries"][0].endswith("write: count.md")
           and "archivist-mcp" in ds._git("log", "-1", "--format=%an <%ae>"),
           "and the commit still carries our identity, from the environment",
           ds._git("log", "-1", "--format=%an <%ae>"))

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

        # The default pattern leaves things out, and used to leave them out in
        # silence: an archive of a folder holding PDFs looked exactly like an
        # archive of a folder that really was all Markdown. The description no
        # longer explains this — it says to read the card — so the RESULT has to
        # carry it, which costs nothing to anyone who is not archiving.
        (ds.root / "note.pdf").write_bytes(b"%PDF-1.4 not markdown\n")
        (ds.root / "sheet.csv").write_text("a,b\n1,2\n")
        md = ds.archive("", "*.md")
        here = len(ds.list_files("")["files"])
        ok(md["pattern"] == "*.md", "archive says which pattern it applied", md["pattern"])
        # Counted against list_files rather than against a number written here:
        # a fixture gains a file and a hard-coded 2 goes red for no reason, which
        # is how a check gets switched off. Both sides go through _skip, so they
        # are counting the same thing.
        ok(md["skipped_by_pattern"] == here - md["file_count"],
           "and counts what the pattern left behind — the silent part, made loud",
           f"{md['skipped_by_pattern']} skipped of {here}")
        ok(md["skipped_by_pattern"] >= 2,
           "the two non-Markdown files really are among the skipped", md["skipped_by_pattern"])
        every = ds.archive("", "*")
        ok(every["skipped_by_pattern"] == 0 and every["file_count"] == md["file_count"] + md["skipped_by_pattern"],
           "with '*' nothing is skipped and everything is in",
           f"{every['file_count']} vs {md['file_count']}")
        # The count means the same "file" the archive itself means: both sides
        # go through _skip, so .git and the lockfile are outside both.
        ok(every["file_count"] == len(ds.list_files("")["files"]),
           "archiving everything packs exactly what list_files lists",
           f"{every['file_count']} vs {len(ds.list_files('')['files'])}")
        (ds.root / "note.pdf").unlink()
        (ds.root / "sheet.csv").unlink()
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

        # The guide, served the way the tool serves it. reference_guide() is a
        # shell over guide_for(), so this is the real behaviour and not a
        # rehearsal of it: server.py cannot be imported without fastmcp, and
        # what cannot be imported cannot be tested.
        print("\n[6c] the guide, whole and by the card")
        gtext = (Path(__file__).parent / "reference-guide.md").read_text(encoding="utf-8")
        whole = guide_for(gtext)
        ok("cards" in whole and "archive" in whole["cards"],
           "the model page comes with the list of card names",
           len(whole.get("cards", [])))
        ok("# COMMANDS" not in whole["guide"],
           "and the model page stops before the cards")
        card = guide_for(gtext, "archive")
        ok(card["command"] == "archive" and card["guide"].startswith("archive("),
           "a card comes back headed by its own signature")
        ok(len(card["guide"]) < len(whole["guide"]),
           "and a card is smaller than the model page — the whole point")
        # Written as the manual prints it, which is how a reader will paste it.
        for spelling in ("Archive", " archive ", "archive()"):
            ok(guide_for(gtext, spelling)["command"] == "archive",
               f"{spelling!r} finds the same card")
        # The refusal carries the names: one round trip, not two.
        try:
            guide_for(gtext, "teleport"); ok(False, "an unknown name is refused")
        except VaultError as e:
            ok("append" in str(e) and "archive" in str(e),
               "an unknown name is refused WITH the list of names", str(e)[:60])
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

        print("\n[11b] the registry has ONE reader, and it never hands back a line")
        # Found in the boot log, not here: the preflight had a second parser
        # that split on TAB only, so a legitimate space-separated line was read
        # as one long name and reported as an orphan that did not exist — and
        # the message carried the LINE, which is `name<sep>KEY`. The key of a
        # protected dataset went into the container log in clear.
        from vault import parse_key_registry
        SECRET = "SuperSecretKey123"
        for label, text, expect_entries, expect_bad in (
                ("a TAB, the documented form", f"Example Project\t{SECRET}\n",
                 [("Example Project", SECRET)], []),
                ("two spaces, tolerated because file managers eat tabs",
                 f"Example Project  {SECRET}\n", [("Example Project", SECRET)], []),
                ("four spaces — the shape that was in the real registry",
                 f"MCP Projects    {SECRET}\n", [("MCP Projects", SECRET)], []),
                ("comments and blank lines count as nothing",
                 f"# a note\n\n   \nExample Project\t{SECRET}\n",
                 [("Example Project", SECRET)], []),
                ("a line with no separator is malformed, by NUMBER",
                 f"Example Project\t{SECRET}\nnoseparator\n",
                 [("Example Project", SECRET)], [2]),
                ("a name with no key is malformed too",
                 f"Example Project\t\n", [], [1]),
                ("two malformed lines, both numbered",
                 "one\ntwo\n", [], [1, 2])):
            got = parse_key_registry(text)
            ok(got == (expect_entries, expect_bad), f"registry: {label}", got)

        # The trap, asserted rather than described: the tolerated form splits on
        # the FIRST run of spaces, so a name carrying two of them needs a TAB.
        ok(parse_key_registry(f"Two  Spaces  {SECRET}\n")[0] == [("Two", f"Spaces  {SECRET}")],
           "a dataset name with two consecutive spaces MUST use a real tab — the "
           "space form would eat half the name, and this is what that looks like",
           parse_key_registry(f"Two  Spaces  {SECRET}\n")[0])

        # And the half that was the leak: no return value may carry the key.
        # ⚠ The `extra` of these checks reports a COUNT and a TYPE, never the
        # value found. A suite's output is a log like any other: it is read in a
        # terminal and pasted into a chat, so a check that proves a secret did
        # not leak, and prints the secret when it fails, has moved the leak
        # rather than closed it. The refinement is codifier's, 2026-08-14.
        for text in (f"Example Project\t{SECRET}\n", "noseparator-line\n",
                     f"Example Project  {SECRET}\n"):
            _, bad = parse_key_registry(text)
            ok(all(isinstance(n, int) for n in bad),
               "malformed lines come back as NUMBERS, never as text — a caller "
               "cannot leak into a log what it was never handed",
               f"{len(bad)} item(s) of type "
               f"{sorted({type(x).__name__ for x in bad}) or 'none'}")

        # The preflight's own message, end to end: it must name the line number
        # and NOT the secret. This is the injection that proves the cure.
        old_keys = os.environ.get("KEYS_FILE")
        bad_registry = Path(root) / "bad-keys.txt"
        bad_registry.write_text(f"Example Project\t{SECRET}\nnoseparator\n")
        try:
            import importlib
            os.environ["KEYS_FILE"] = str(bad_registry)
            os.environ["VAULT_ROOT"] = str(root)
            import preflight as _pf
            importlib.reload(_pf)
            # The harness SWALLOWS the exception and records it: a check that
            # crashes counts as failed. So the message is read where it really
            # lands — RESULTS — which is also where the log line comes from,
            # and therefore the only place worth asserting about.
            _pf.c_keys()
            _name, _passed, verdict = _pf.RESULTS[-1]
            ok(_name == "keys" and not _passed,
               "a registry with a malformed line FAILS the preflight, and the "
               "service does not start", (_name, _passed))
        finally:
            os.environ.pop("VAULT_ROOT", None)
            os.environ.pop("KEYS_FILE", None)
            if old_keys is not None:
                os.environ["KEYS_FILE"] = old_keys
        ok(SECRET not in verdict,
           "the preflight's complaint about a malformed registry does NOT "
           "contain the key — which is exactly what it used to do",
           f"{verdict.count(SECRET)} secret(s) inside the message")
        ok("line(s) 2" in verdict,
           "and it says WHICH line, which is what you actually need to fix it",
           verdict.replace(SECRET, "<redacted>")[:120])

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
        # The commits come from ONE `git fast-import` stream, not from four
        # hundred git processes in a shell loop: that loop was ~18 of the
        # suite's ~25 seconds (2026-09-02), and a suite that takes a minute
        # stops being run. The repository it produces is the same shape —
        # 200 dated commits, each adding a line to f.md, then a recent one.
        os.makedirs(Path(root) / "Long")
        v.boot(0)
        long_ds = Dataset(Path(root) / "Long", "Long")
        _branch = long_ds._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        _old = "1705320000 +0000"          # 2024-01-15T12:00:00Z
        _now = f"{int(time.time())} +0000"
        _who = "archivist-mcp <archivist-mcp@localhost>"
        _stream, _lines = [], []
        for i in range(1, 201):
            _lines.append(f"line {i}\n")
            _blob = "".join(_lines)
            _stream.append(
                f"commit refs/heads/{_branch}\n"
                f"committer {_who} {_old}\n"
                f"data {len(f'old {i}')}\nold {i}\n"
                + (f"from refs/heads/{_branch}^0\n" if i == 1 else "")
                + f"M 100644 inline f.md\ndata {len(_blob.encode())}\n{_blob}\n")
        _stream.append(
            f"commit refs/heads/{_branch}\ncommitter {_who} {_now}\n"
            f"data 6\nrecent\nM 100644 inline recent.md\ndata 7\nrecent\n\n")
        _r = subprocess.run(["git", "-C", str(Path(root) / "Long"), "fast-import", "--quiet"],
                            input="".join(_stream).encode(), capture_output=True)
        ok(_r.returncode == 0, "two hundred commits were made", _r.stderr[:200])
        # fast-import moves the ref, not the index or the working tree.
        long_ds._git("reset", "-q", "--hard")
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

        print("\n[14a] the entrypoint does not grow a config line per restart")
        entrypoint_growth_check()

        print("\n[14b] the Gate is wired to the hook it claims")
        gate_hook_check()

        print("\n[14b2] every tool's refusals go through the converter")
        tool_conversion_check()

        print("\n[14bis] the engine is pinned, installed, and announced")
        engine_adoption_check()

        print("\n[14bis2] the delivery scripts read the pin they install")
        ship_scripts_check()

        print("\n[14c] the manual says what the code actually offers")
        guide_signature_check()

        print("\n[14d] the log level cannot kill the service at import")
        log_level_checks()

        print("\n[14d2] the transport mode defaults to yesterday, and arrives")
        http_mode_checks()
        http_mode_wiring_check()

        print("\n[14e] the icon is one url, in two files that agree")
        icon_check()

        print("\n[14e2] every variable the template offers is read by somebody")
        template_variable_check()

        print("\n[14f] the payload of a malformed call cannot reach the log")
        redaction_armed_check()

        print("\n[14f2] and the same lines carry our clock, in our one format")
        timestamps_armed_check()

        print("\n[15] the IP filter list, in both directions")
        cidr_checks()

        print(f"\n{'=' * 46}\n  {OK} passed, {FAIL} failed\n{'=' * 46}")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
