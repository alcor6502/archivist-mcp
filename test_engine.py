#!/usr/bin/env python3
"""The engine's own suite. No network, no Docker, no FastMCP installed.

`mcp_common_engine` is the part of this server that knows nothing about
vaults: the identity gate, the conversion of designed refusals, the config
helpers, the preflight harness, the log discipline. It was a repository of its
own, shared with a twin server, until archivist-mcp 4.0.2 took it in — the
twin was retired, and a pin to a second repository was a number to keep in
step for nobody. It stays a package with its own suite so that lifting it out
again is a copy, not a rewrite.

Three kinds of checks, and the split is deliberate:

  behaviour   config and the preflight harness are imported and exercised.
              They must import WITHOUT fastmcp — that is the package's own
              contract, and this suite running proves it better than asserting
              it.
  stubbed     make_tool needs a ToolError to raise and an mcp to register
              with. fastmcp is stubbed in sys.modules BEFORE the import: what
              is measured is the factory's own logic — branch order, the log
              line, what gets registered — none of which belongs to fastmcp.
              What DOES belong to fastmcp (where a message is routed, which
              exception survives the handshake) was established by experiment
              against the pinned version and cannot be re-proven here.
  AST         gate.py hooks by NAMING a method, and the base class ships a
              pass-through default for every hook name that exists: a typo
              does not fail, it disables the gate in silence. Nothing that
              runs without a server can see that — so the source is read.

Every guarantee here has been proven by injection at least once: the defect
was written in, the check went red, and the red line NAMED the culprit. A
check never seen failing is not a check.

Run: python3 test_engine.py — prints its own count, exits 1 on any failure.
"""
from __future__ import annotations

import ast
import io
import logging
import os
import re
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE / "mcp_common_engine"

PASS = 0
FAIL = 0


def ok(cond, label: str, extra="") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label} — {extra!r}")


def must_raise(label: str, exc, fn) -> None:
    """A refusal that does not happen is a hole, and a crash of the WRONG type
    is not a refusal. Both are red lines with names, never tracebacks: a suite
    that dies on line sixty runs none of the checks below, including the ones
    written to notice exactly what killed it."""
    try:
        fn()
    except exc as e:
        ok(True, f"{label}: {type(e).__name__}: {str(e)[:60]}")
    except Exception as e:                                      # noqa: BLE001
        ok(False, label, f"wrong exception: {type(e).__name__}: {e}")
    else:
        ok(False, label, "no exception at all")


# =====================================================================
# 0 · the package root imports with NO fastmcp anywhere
# =====================================================================
# This is the contract the CI asserts on a bare runner: a preflight has to be
# able to run, and to report, on an image where fastmcp is missing or broken —
# which is one of the things a preflight exists to catch. Here it is asserted
# from the inside: import the root, then look at what arrived.

print("== the root imports clean ==")

ok("fastmcp" not in sys.modules, "fastmcp is not installed where this suite runs "
   "— that absence is the fixture, not a shortcoming")

# Guarded, because the failure this block exists to catch — somebody importing
# gate or refusals from the package ROOT — would otherwise kill the suite on
# this very line with a traceback and no name: none of the checks below would
# run, including this one. A named red line, then out.
try:
    import mcp_common_engine as eng                             # noqa: E402
    from mcp_common_engine import (DEFAULT_CIDRS, LOG_LEVELS,   # noqa: E402
                                   RESULTS, SKIP, check, cidrs_from_env,
                                   describe_cidrs, is_placeholder,
                                   log_level_from_env, parse_cidrs)
except ImportError as e:
    ok(False, "the package root imports with fastmcp absent — a preflight has "
              "to be able to run, and to report, on an image where fastmcp is "
              "missing or broken", f"{type(e).__name__}: {e}")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1)

ok("fastmcp" not in sys.modules,
   "and importing the root did not drag fastmcp in", sorted(sys.modules))

_INIT_TREE = ast.parse((PKG / "__init__.py").read_text(encoding="utf-8"))
_INIT_FROM = {n.module for n in ast.walk(_INIT_TREE)
              if isinstance(n, ast.ImportFrom) and n.module}
ok(_INIT_FROM <= {"config", "preflight"},
   "__init__.py imports only config and preflight — gate and refusals stay "
   "behind their own doors", sorted(_INIT_FROM))

# =====================================================================
# 1 · the version is one number in one place
# =====================================================================

print("\n== one version, one place ==")

ok(re.fullmatch(r"\d+\.\d+\.\d+", eng.VERSION or ""),
   f"VERSION is a three-part number: {eng.VERSION}", eng.VERSION)

_VER_ASSIGNS = [n for n in _INIT_TREE.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "VERSION" for t in n.targets)]
ok(len(_VER_ASSIGNS) == 1, "VERSION is assigned exactly once in __init__.py",
   len(_VER_ASSIGNS))

# Since archivist-mcp 4.0.2 this package lives INSIDE the server's repository
# (the twin it was shared with is retired), so it has no pyproject and no README
# of its own: the three checks that tied those to the constant went with them.
# What stays is the pin, which moved to the file the image installs from.
_SERVER_REQ = (HERE / "requirements-server.txt").read_text(encoding="utf-8")
ok(re.findall(r"^fastmcp\S*", _SERVER_REQ, re.MULTILINE) == ["fastmcp==3.4.5"],
   "fastmcp is pinned in requirements-server.txt, ==, one number — where a message "
   "is routed and which exception survives the handshake are properties of that version",
   re.findall(r"^fastmcp\S*", _SERVER_REQ, re.MULTILINE))

# =====================================================================
# 2 · is_placeholder — the guard that must not fire on real values
# =====================================================================

print("\n== is_placeholder ==")

for v in ("CHANGEME", "changeme", "CAMBIAMI", "CHANGE_ME", "CHANGE-ME",
          "CHANGE.ME", "change me", "https://CHANGEME.your-tailnet.ts.net",
          "Iv1.CHANGEME"):
    ok(is_placeholder(v), f"caught: {v!r}")

# The word has to START there. "exchange mechanism" squeezes to
# "exchangemechanism", which CONTAINS "changeme" — and so does a perfectly
# legitimate hostname. A guard that refuses to start the service on a real
# value is worse than the hole it closes.
for v in ("exchange mechanism", "https://exchange.me.ts.net",
          "a-real-secret-value", "ghp_x8FmQ2", ""):
    ok(not is_placeholder(v), f"let through: {v!r}")

# =====================================================================
# 3 · parse_cidrs — a malformed entry RAISES, never skips
# =====================================================================

print("\n== parse_cidrs ==")

ok(parse_cidrs("") == [], "empty string means NO filter, as [] and not as None")
ok(parse_cidrs("160.79.104.0/21") == [("160.79.104.0/21", "")],
   "one bare entry parses")
ok(parse_cidrs("160.79.104.0/21 # Anthropic egress ; 100.64.0.0/10 # tailnet")
   == [("160.79.104.0/21", "Anthropic egress"), ("100.64.0.0/10", "tailnet")],
   "two entries with descriptions — ';' separates, '#' describes")
ok(parse_cidrs("10.0.0.0/8 # a, b and c") == [("10.0.0.0/8", "a, b and c")],
   "a description may contain a comma: that is WHY the separator is ';'")
ok(parse_cidrs("10.0.0.0/8;") == [("10.0.0.0/8", "")],
   "a trailing ';' cannot change what the filter means")
ok(parse_cidrs(" ; ;10.0.0.0/8; ; ") == [("10.0.0.0/8", "")],
   "empty entries between separators are tolerated")

# The failure nobody notices is a filter wider or narrower than you believe:
# these must REFUSE, loudly, before any service starts behind them.
must_raise("a non-CIDR entry raises", ValueError, lambda: parse_cidrs("not-a-net"))
must_raise("host bits set raises — 10.0.0.1/8 is a typo for one of two "
           "different filters and the parser cannot know which", ValueError,
           lambda: parse_cidrs("10.0.0.1/8"))
must_raise("a description with no network raises", ValueError,
           lambda: parse_cidrs("# just words"))
must_raise("a comma used as separator raises on the joined blob — the mistake "
           "is caught instead of half the list disappearing", ValueError,
           lambda: parse_cidrs("10.0.0.0/8, 100.64.0.0/10"))

# =====================================================================
# 4 · cidrs_from_env — "defined and empty" is not "not defined"
# =====================================================================

print("\n== cidrs_from_env ==")


def with_env(pairs, fn):
    saved = {k: os.environ.get(k) for k in ("ALLOWED_CIDRS", "ANTHROPIC_CIDR")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        for k, v in pairs.items():
            os.environ[k] = v
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


ok(with_env({}, cidrs_from_env) == parse_cidrs(DEFAULT_CIDRS),
   "neither variable defined: the documented default")
ok(with_env({"ALLOWED_CIDRS": ""}, cidrs_from_env) == [],
   "ALLOWED_CIDRS defined and EMPTY: the filter is OFF — defined-and-empty is "
   "a gesture, not an absence")
ok(with_env({"ALLOWED_CIDRS": "10.0.0.0/8"}, cidrs_from_env)
   == [("10.0.0.0/8", "")], "ALLOWED_CIDRS defined: it is the filter")
ok(with_env({"ANTHROPIC_CIDR": "10.0.0.0/8"}, cidrs_from_env)
   == [("10.0.0.0/8", "")],
   "ANTHROPIC_CIDR alone still works: a container updated without touching its "
   "template keeps working — a new variable is always born optional")
ok(with_env({"ALLOWED_CIDRS": "10.0.0.0/8", "ANTHROPIC_CIDR": "100.64.0.0/10"},
            cidrs_from_env) == [("10.0.0.0/8", "")],
   "and when both are defined, ALLOWED_CIDRS wins")

# =====================================================================
# 5 · describe_cidrs — what was UNDERSTOOD, not what was given
# =====================================================================

print("\n== describe_cidrs ==")

ok(describe_cidrs([]) == "OFF (no IP filter)",
   "no filter says OFF, in words — silence here is how a mute breakage hides")
ok(describe_cidrs([("10.0.0.0/8", "")]) == "1 range — 10.0.0.0/8",
   "one range, singular")
_TWO = describe_cidrs([("10.0.0.0/8", "lan"), ("100.64.0.0/10", "")])
ok(_TWO == "2 ranges — 10.0.0.0/8 (lan), 100.64.0.0/10",
   "two ranges, plural, descriptions only where they exist", _TWO)

# =====================================================================
# 6 · log_level_from_env — the closed list, and the alias resolved FIRST
# =====================================================================

print("\n== log_level_from_env ==")


def with_level(value, fn=log_level_from_env):
    saved = os.environ.get("LOG_LEVEL")
    try:
        if value is None:
            os.environ.pop("LOG_LEVEL", None)
        else:
            os.environ["LOG_LEVEL"] = value
        return fn()
    finally:
        if saved is None:
            os.environ.pop("LOG_LEVEL", None)
        else:
            os.environ["LOG_LEVEL"] = saved


ok(LOG_LEVELS == ("INFO", "WARNING"),
   "the list is closed at INFO and WARNING — below INFO there is nothing to "
   "switch on, above WARNING the gate's refusals disappear", LOG_LEVELS)

# Not choosing is not the same gesture as choosing wrong: absent and empty are
# not corrections and are not reported.
ok(with_level(None) == ("INFO", None), "absent: INFO, nothing to report")
ok(with_level("") == ("INFO", None), "empty: INFO, nothing to report")
ok(with_level("   ") == ("INFO", None), "spaces: INFO, nothing to report")

ok(with_level("INFO") == ("INFO", None), "INFO is honoured")
ok(with_level("WARNING") == ("WARNING", None), "WARNING is honoured")
ok(with_level("info") == ("INFO", None), "case does not matter")
ok(with_level(" warning ") == ("WARNING", None), "nor does whitespace")

# WARN is not a typo: Python defines it as an alias of WARNING and
# setLevel("WARN") does not raise. Whoever writes it wants LESS noise, so
# falling back to INFO would give them MORE — honoured, silently.
ok(with_level("WARN") == ("WARNING", None), "WARN resolves to WARNING, silently")
ok(with_level("warn") == ("WARNING", None), "and in lower case")

# A knob that ignores you in silence is how you get accused of having broken
# it: the rejected value comes BACK, so the caller can say so out loud.
ok(with_level("DEBUG") == ("INFO", "DEBUG"),
   "DEBUG falls back to INFO AND is reported — there is not one .debug() call "
   "in either consumer, so the knob would read as broken")
ok(with_level("ERROR") == ("INFO", "ERROR"), "ERROR falls back, reported")
ok(with_level("banana") == ("INFO", "BANANA"), "garbage falls back, reported")

# THE SHAPE, and it is the one thing the two twins wrote differently. Codifier
# resolves the alias FIRST and then checks the closed list; archivist checked
# the list, then the table, and RETURNED WHATEVER THE TABLE SAID. Identical
# with {"WARN": "WARNING"}; the day an alias points outside the list,
# archivist's shape lets it through and this one does not. Codifier's shape is
# the one kept — confirmed from the archivist side, which is the side whose
# function changed. This check pins the difference by exercising it.
from mcp_common_engine import config as _cfg                    # noqa: E402

_saved_aliases = _cfg._ALIASES
try:
    _cfg._ALIASES = {"WARN": "NOPE"}
    ok(with_level("WARN") == ("INFO", "WARN"),
       "an alias pointing OUTSIDE the closed list cannot slip through: the "
       "list wins over the table, and the given value is what gets reported",
       with_level("WARN"))
finally:
    _cfg._ALIASES = _saved_aliases

# =====================================================================
# 7 · the preflight harness — SKIP, RESULTS, and a crash counts as FAILED
# =====================================================================

print("\n== preflight harness ==")

RESULTS.clear()


@check("good")
def c_good():
    return "all fine"


@check("bare")
def c_bare():
    return None                     # a check with nothing to add says "ok"


@check("bad")
def c_bad():
    raise RuntimeError("the disk is gone")


for fn in (c_good, c_bare, c_bad):
    fn()

ok(RESULTS == [("good", True, "all fine"),
               ("bare", True, "ok"),
               ("bad", False, "RuntimeError: the disk is gone")],
   "three checks, three verdicts, in order — and the crash is a FAILURE with "
   "the exception's name and text, never a pass and never a traceback",
   RESULTS)

RESULTS.clear()
SKIP.add("skipped-one")


@check("skipped-one")
def c_skipped():
    raise AssertionError("this body must never run")


c_skipped()
SKIP.discard("skipped-one")
ok(RESULTS == [("skipped-one", True, "SKIPPED (PREFLIGHT_SKIP)")],
   "a skipped check is recorded AS skipped — the sheet says so instead of "
   "quietly having one line fewer", RESULTS)
RESULTS.clear()

# =====================================================================
# 8 · make_tool — the factory, exercised against a stubbed fastmcp
# =====================================================================
# The stub is injected only if fastmcp is genuinely absent, so on a machine
# that has the real one, the real one is what gets measured.

print("\n== make_tool, exercised ==")

if "fastmcp" not in sys.modules:
    _fm = types.ModuleType("fastmcp")
    _fme = types.ModuleType("fastmcp.exceptions")

    class ToolError(Exception):
        def __init__(self, message, log_level=None):
            super().__init__(message)
            self.log_level = log_level

    _fme.ToolError = ToolError
    _fm.exceptions = _fme
    sys.modules["fastmcp"] = _fm
    sys.modules["fastmcp.exceptions"] = _fme
else:                                                           # pragma: no cover
    from fastmcp.exceptions import ToolError

from mcp_common_engine.refusals import make_tool                # noqa: E402


class Refusal(Exception):
    pass


class Fault(Refusal):
    pass


class FakeMCP:
    def __init__(self):
        self.registered = []

    def tool(self, fn):
        self.registered.append(fn)
        return fn


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, msg, *args):
        self.lines.append(msg % args)


fake_mcp, fake_log = FakeMCP(), FakeLog()
tool = make_tool(fake_mcp, fake_log, refusal=Refusal, fault=Fault)


@tool
def sample(a, b=2):
    """the docstring is the schema"""
    return a + b


ok(len(fake_mcp.registered) == 1, "the factory registers exactly one function")
_REG = fake_mcp.registered[0]

ok(_REG.__name__ == "sample" and _REG.__doc__ == "the docstring is the schema",
   "functools.wraps holds: name and docstring survive, which is what the MCP "
   "schema is built from", (_REG.__name__, _REG.__doc__))
ok(getattr(_REG, "__wrapped__", None) is not None,
   "and __wrapped__ points home, which is how FastMCP finds the signature")
ok(_REG(3) == 5, "a clean call passes through and returns")

fake_log.lines.clear()


@tool
def refuses():
    raise Refusal("CONFLICT: expected sha 000")


try:
    fake_mcp.registered[-1]()
except ToolError as e:
    ok(str(e) == "CONFLICT: expected sha 000",
       "a refusal becomes ToolError, message intact word for word", str(e))
    ok(e.log_level == logging.INFO,
       "at logging.INFO exactly — WARNING is the Gate's height", e.log_level)
    ok(e.__cause__ is None and e.__suppress_context__,
       "raised `from None`: the chained traceback is what we are removing")
except Exception as e:                                          # noqa: BLE001
    # Without this branch, a conversion that stopped converting — the exact
    # mutation the checks below exist for — killed this suite with a traceback
    # on this line, and nothing after it ran. Found by injection: the factory
    # registering `fn` instead of `guarded` produced one incidental red line
    # and a crash, instead of three red lines with names.
    ok(False, "a refusal becomes ToolError, message intact word for word",
       f"escaped unconverted: {type(e).__name__}: {e}")
else:
    ok(False, "a refusal becomes ToolError", "nothing was raised")

ok(fake_log.lines == ["refused refuses: CONFLICT: expected sha 000"],
   "and the refusal leaves ONE line of the server's own, naming the tool and "
   "the reason — FastMCP's own record never survives FASTMCP_LOG_LEVEL=WARNING",
   fake_log.lines)

fake_log.lines.clear()


@tool
def breaks():
    raise Fault("read-back verification failed")


must_raise("a FAULT rises untouched — the subclass is caught FIRST, or a full "
           "disk comes out wearing the word 'refused'", Fault,
           fake_mcp.registered[-1])
ok(fake_log.lines == [],
   "and a fault leaves NO 'refused' line: it is not a refusal", fake_log.lines)


@tool
def bug():
    raise ValueError("an actual bug")


must_raise("an exception that is neither class rises as itself — a bug gets "
           "its traceback at ERROR, which is what a bug deserves", ValueError,
           fake_mcp.registered[-1])

# =====================================================================
# 9 · refusals.py and gate.py, read as source
# =====================================================================
# The same guarantees the consumers pin from THEIR side, pinned here where the
# code lives. Both suites going red on the same mutation is the point, not a
# redundancy: the consumers' copy protects them from a stale pin, this copy
# protects the tag from ever carrying the defect at all.

print("\n== refusals.py, as written ==")

_RTREE = ast.parse((PKG / "refusals.py").read_text(encoding="utf-8"))
_MAKES = [n for n in _RTREE.body
          if isinstance(n, ast.FunctionDef) and n.name == "make_tool"]
ok(len(_MAKES) == 1, "one make_tool, at module level", len(_MAKES))

if _MAKES:
    _factory = _MAKES[0]
    _guarded = next((n for n in ast.walk(_factory)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == "guarded"), None)
    ok(_guarded is not None, "the wrapper is `guarded`, there to be read")
    _rebound = [ast.unparse(n) for n in ast.walk(_factory)
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "guarded" for t in n.targets)]
    ok(not _rebound, "`guarded` is only ever the def, never reassigned "
       "— `guarded = fn` is this check's own defect wearing the right name",
       _rebound)
    _registers = [n for n in ast.walk(_factory) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == "mcp.tool"]
    ok(len(_registers) == 1 and _registers[0].args
       and ast.unparse(_registers[0].args[0]) == "guarded",
       "and mcp.tool receives `guarded` — registering `fn` keeps every piece "
       "above written down while converting nothing",
       [ast.unparse(n)[:40] for n in _registers])
    if _guarded is not None:
        _handlers = [h for t in ast.walk(_guarded) if isinstance(t, ast.Try)
                     for h in t.handlers]
        _caught = [ast.unparse(h.type) for h in _handlers if h.type]
        ok(_caught[:1] == ["fault"],
           "the fault branch comes FIRST — Python does not warn when a "
           "subclass branch is placed second and therefore never taken",
           _caught)

print("\n== gate.py, as written ==")

_GTREE = ast.parse((PKG / "gate.py").read_text(encoding="utf-8"))
_GATES = [n for n in _GTREE.body
          if isinstance(n, ast.ClassDef) and n.name == "Gate"]
ok(len(_GATES) == 1, "one Gate, at module level", len(_GATES))

if _GATES:
    _GATE = _GATES[0]
    ok([ast.unparse(b) for b in _GATE.bases] == ["Middleware"],
       "it subclasses Middleware, which is what makes a hook a hook",
       [ast.unparse(b) for b in _GATE.bases])
    _hook_assigns = [s.value.value for s in _GATE.body if isinstance(s, ast.Assign)
                     and any(getattr(t, "id", "") == "HOOK" for t in s.targets)
                     and isinstance(s.value, ast.Constant)]
    ok(_hook_assigns == ["on_request"],
       "HOOK pins the decision: on_request, assigned exactly once — the base "
       "class ships a pass-through default for every hook name that exists, "
       "so `on_requst` is not an error, it is a gate off in silence",
       _hook_assigns)
    _hooks = {n.name for n in _GATE.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name.startswith("on_")}
    ok(_hooks == set(_hook_assigns), "and the Gate hooks exactly what HOOK names",
       sorted(_hooks))
    _hook_fn = next((n for n in _GATE.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == "on_request"), None)
    if _hook_fn is not None:
        _passes = [n for n in ast.walk(_hook_fn) if isinstance(n, ast.Call)
                   and ast.unparse(n.func) == "call_next"]
        ok(len(_passes) == 1,
           "the go-ahead appears in ONE place", len(_passes))
        _last = _hook_fn.body[-1]
        ok(isinstance(_last, ast.Return) and "call_next" in ast.unparse(_last),
           "and it is the LAST statement — moved to the top, everything walks "
           "through with the body sitting there, unreachable and reassuring",
           ast.unparse(_last)[:50])
        _warns = [n for n in ast.walk(_hook_fn) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == "self.log.warning"]
        ok(len(_warns) >= 2, "both refusals are logged, identity and origin — "
           "that line is the only thing telling a refused stranger from a "
           "broken deployment", len(_warns))
        ok(all(any(ast.unparse(a) == "ctx.method" for a in w.args)
               for w in _warns),
           "and each names the method it turned away")
    _init = next((n for n in _GATE.body if isinstance(n, ast.FunctionDef)
                  and n.name == "__init__"), None)
    ok(_init is not None and [a.arg for a in _init.args.kwonlyargs]
       == ["log", "allowed_login", "allowed_cidrs"],
       "config is INJECTED, keyword-only: log, allowed_login, allowed_cidrs — "
       "the engine reads no module globals of anybody's",
       [a.arg for a in _init.args.kwonlyargs] if _init else "no __init__")

    # ---- the v1.1.0 cure: the refusal is a protocol answer, not a crash ----
    # Read rather than run, for the same reason as the hook: what fastmcp does
    # with each exception type was established by experiment against the pinned
    # version and cannot be re-proven without a server. What CAN be pinned here
    # is that the code still raises the type that experiment blessed.
    _raises = [n for n in ast.walk(_GATE) if isinstance(n, ast.Raise) and n.exc]
    ok(_raises, "the gate still refuses somebody")
    ok(not any("ValueError" in ast.unparse(r.exc) for r in _raises),
       "no refusal is a bare ValueError any more — fastmcp 3.4.5 turns only "
       "McpError into a protocol answer, so a ValueError escapes and is dressed "
       "as -32602 on the ROOT logger, without the service name",
       [ast.unparse(r.exc) for r in _raises])
    ok(all(ast.unparse(r.exc) == "self._refusal()" for r in _raises),
       "and every refusal goes through the SAME builder — two call sites with "
       "two messages is how a stranger learns which door they hit",
       sorted({ast.unparse(r.exc) for r in _raises}))

    _builder = next((n for n in _GATE.body if isinstance(n, ast.FunctionDef)
                     and n.name == "_refusal"), None)
    ok(_builder is not None, "the builder exists")
    _built = ast.unparse(_builder) if _builder else ""
    ok("McpError" in _built and "ErrorData" in _built,
       "it builds an McpError with ErrorData — the shape fastmcp converts")
    ok("INVALID_REQUEST" in _built,
       "with INVALID_REQUEST, not the -32602 the SDK used to improvise: nothing "
       "is wrong with the PARAMETERS, the request is one this caller may not make",
       _built)
    ok("self.REFUSAL" in _built,
       "and the sentence comes from one constant, so the two filters cannot "
       "drift apart into two different messages")

    # THE TRAP THIS CURE WALKED INTO. The IP branch used to raise inside a `try`
    # whose `except ValueError` caught it, logged, and re-raised — so the log
    # line came out of the except. An McpError sails straight past that except:
    # the refusal stays correct and the LINE disappears. Nothing above can see
    # it (both shapes have two warnings, both name ctx.method), so it is pinned
    # here, in the only form that distinguishes them: no refusal is raised from
    # inside a try, and the try guards a parse, not a decision.
    if _hook_fn is not None:
        _in_try = [r for t in ast.walk(_hook_fn) if isinstance(t, ast.Try)
                   for r in ast.walk(t) if isinstance(r, ast.Raise)]
        ok(not _in_try,
           "no refusal is raised from inside a `try` — the old shape logged "
           "from the `except`, and an McpError does not stop there",
           [ast.unparse(r) for r in _in_try])
        _tries = [t for t in ast.walk(_hook_fn) if isinstance(t, ast.Try)]
        ok(all(len(t.body) == 1 for t in _tries),
           "and each `try` wraps ONE statement: the parse that can genuinely "
           "raise, never the decision that follows it",
           [len(t.body) for t in _tries])

# gate.py and refusals.py may import fastmcp — they are the two modules that
# exist to talk to it — but config and preflight must not, or the root import
# contract above is one refactor away from false.
for _mod in ("config", "preflight", "logs"):
    _t = ast.parse((PKG / f"{_mod}.py").read_text(encoding="utf-8"))
    _imp = set()
    for n in ast.walk(_t):
        if isinstance(n, ast.Import):
            _imp |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            _imp.add(n.module.split(".")[0])
    ok("fastmcp" not in _imp and "mcp" not in _imp,
       f"{_mod}.py imports no server framework", sorted(_imp))

# =====================================================================
# logs — the payload must not survive a malformed call
# =====================================================================
# Behaviour, not AST: the whole point is what comes out of the formatter, and
# that can be read directly. The record is built the way fastmcp builds it —
# message with %-placeholders, the detail left in record.args — because that
# is WHY a filter can do anything at all: the payload is still a structure at
# this stage, and is only flattened into text when the handler formats it.
print("\n[logs] a malformed call must not print what it carried")

from mcp_common_engine.logs import (REDACTED, ArgumentRedactingFilter,
                                    _redacted, arm_argument_redaction)

# The real thing, verbatim from the incident of 2026-08-10: the payload here
# is a document, and `loc` is the argument that was missing.
_SECRET = "the body of a stored document, in full"
_DETAIL = [{"type": "missing_argument",
            "loc": ("expected_sha256",),
            "msg": "Missing required argument",
            "input": {"dataset": "MCP Projects", "path": "a/Stato.md",
                      "old_text": _SECRET, "new_text": _SECRET}}]


def _emitted(detail, *, armed: bool) -> str:
    """Push one record through a real handler and return what got printed."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    if armed:
        handler.addFilter(ArgumentRedactingFilter())
    logger = logging.getLogger(f"probe.{armed}.{id(detail)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.warning("Invalid arguments for tool %r: %s", "edit_file", detail)
    return stream.getvalue()


_before = _emitted(_DETAIL, armed=False)
ok(_SECRET in _before,
   "unfiltered, the document really is printed — the check has something to "
   "measure, and this line is what makes the next one mean anything")

_after = _emitted(_DETAIL, armed=True)
ok(_SECRET not in _after, "filtered, the payload is gone", _after[:120])
ok(REDACTED in _after, "and it left a mark rather than vanishing silently",
   _after[:120])
for _keep in ("edit_file", "missing_argument", "expected_sha256",
              "Missing required argument"):
    ok(_keep in _after, f"what diagnoses survives: {_keep}", _after[:120])

# The caller's data can be shaped like anything; the redaction must not care.
ok(_redacted({"input": [1, {"deep": "x"}]})["input"] == REDACTED,
   "`input` is replaced WHOLE, never walked into")
ok(_redacted({"loc": ("a", "b")})["loc"] == ("a", "b"),
   "a tuple stays a tuple — `record.args` IS one, and a record formats itself "
   "with `msg % args`: hand back a list and the line does not read differently, "
   "it does not come out at all")
ok(type(_redacted(("a", {"input": "x"}))) is tuple,
   "and that holds for the args tuple itself, which is the one that matters")
ok(_redacted([{"ctx": {"input": "x"}}]) == [{"ctx": {"input": REDACTED}}],
   "nested error entries are reached too")
_untouched = object()
ok(_redacted(_untouched) is _untouched,
   "anything that is not a dict, list or tuple is handed back unchanged")

# A record with no args must not blow up: plenty of fastmcp lines have none.
ok(_emitted(None, armed=True).strip().endswith("None"),
   "a record with no structure to redact passes through")

# Arming, and the reason it refuses instead of reporting zero.
#
# Guarded, and the guard was earned: an implementation that arms the LOGGER
# instead of the handlers makes the SECOND call raise, and an uncaught raise
# here killed the suite mid-file — every check below it silently not run,
# including the one written to catch exactly that mistake. A wrong answer has
# to be a named red line, never a traceback.
def _armed(name):
    try:
        return arm_argument_redaction(name)
    except Exception as e:                                      # noqa: BLE001
        return f"{type(e).__name__}: {e}"


_probe = logging.getLogger("probe.arming")
_probe.handlers = [logging.NullHandler()]
_probe.filters = []
ok(_armed("probe.arming") == 1, "arming reports what it armed",
   _armed("probe.arming"))
ok(_armed("probe.arming") == 0,
   "arming twice adds nothing — a second call is not a second filter, and it "
   "is not an error either", _armed("probe.arming"))
_bare = logging.getLogger("probe.no.handlers")
_bare.handlers = []
_bare.filters = []
_verdict = _armed("probe.no.handlers")
ok(isinstance(_verdict, str) and _verdict.startswith("RuntimeError")
   and "protect nothing" in _verdict,
   "arming REFUSES when there is no handler to arm, and says why", _verdict)

# The trap this module exists to avoid, asserted rather than described: a
# filter on the LOGGER does not see a child's records. If this ever passes
# with the filter on the logger instead of the handler, the guard is off.
_parent = logging.getLogger("probe.parent")
_sink = io.StringIO()
_h = logging.StreamHandler(_sink)
_h.setFormatter(logging.Formatter("%(message)s"))
_parent.handlers = [_h]
_parent.propagate = False
_parent.setLevel(logging.WARNING)
_parent.addFilter(ArgumentRedactingFilter())          # deliberately the wrong place
logging.getLogger("probe.parent.child").warning(
    "Invalid arguments for tool %r: %s", "edit_file", _DETAIL)
ok(_SECRET in _sink.getvalue(),
   "a filter on the LOGGER lets a child's payload straight through — which is "
   "why arm_argument_redaction goes for the handlers")

# =====================================================================
# logs — and the same line must carry a time
# =====================================================================
# Behaviour again, and for the same reason: what is being claimed is what comes
# out of the handler, and that is readable directly. The record is made on a
# CHILD logger, because that is where fastmcp makes its own — `fastmcp.server.
# server` printed by the handler on `fastmcp` — and a cure that only works when
# the record is made on the logger it was armed on would pass a lazier test.
print("\n[logs] and the same line must carry a time")

from mcp_common_engine.logs import _StampedFormatter, arm_timestamps

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _stamp_probe(name: str) -> tuple[logging.Logger, io.StringIO]:
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    return logger, sink


_logger, _sink = _stamp_probe("probe.stamp")
logging.getLogger("probe.stamp.server").warning("Invalid arguments for tool %r", "x")
_bare_line = _sink.getvalue().strip()
ok(_bare_line == "WARNING: Invalid arguments for tool 'x'",
   "before arming, the line is fastmcp's shape: no date, no time, no service — "
   "this is the thing being cured, and it is measured rather than described",
   _bare_line)

ok(arm_timestamps(_FMT, "probe.stamp") == 1, "arming reports what it stamped")
_sink.truncate(0), _sink.seek(0)
logging.getLogger("probe.stamp.server").warning("Invalid arguments for tool %r", "x")
_stamped = _sink.getvalue().strip()
ok(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} WARNING ", _stamped),
   "after arming, the line opens with a date and a time — which is the whole "
   "point: a line without a clock correlates with nothing", _stamped)
ok(_stamped.endswith("probe.stamp.server: Invalid arguments for tool 'x'"),
   "and it names the logger that made it, so a fastmcp line is recognisable as "
   "one instead of looking like ours", _stamped)

ok(arm_timestamps(_FMT, "probe.stamp") == 0,
   "arming twice changes nothing — a second call is not a second formatter, "
   "and it is not an error either")
ok(arm_timestamps(_FMT + " ", "probe.stamp") == 1,
   "but a DIFFERENT format is applied: the marker is the class plus the "
   "format, so a changed shape is not mistaken for an armed handler")

# The refusal, guarded the way its sibling is: a wrong answer must be a named
# red line, never a traceback that kills the file and takes every check below
# it with it.
def _stamped_or_error(name):
    try:
        return arm_timestamps(_FMT, name)
    except Exception as e:                                          # noqa: BLE001
        return f"{type(e).__name__}: {e}"


_bare_logger = logging.getLogger("probe.stamp.no.handlers")
_bare_logger.handlers = []
_verdict = _stamped_or_error("probe.stamp.no.handlers")
ok(isinstance(_verdict, str) and _verdict.startswith("RuntimeError")
   and "without a date" in _verdict,
   "with no handler it REFUSES and says why: no handler means the assumption "
   "both cures rest on is wrong, and the redaction next door is protecting "
   "nothing either", _verdict)

# The two cures share a grip, and must not dislodge each other: arming one and
# then the other has to leave both in place. The order below is the order
# server.py uses, and the reverse is checked too because a handler that keeps
# its filters through setFormatter is an assumption about logging, not a fact
# anybody wrote down.
for _first, _second, _label in ((arm_argument_redaction, None, "redaction then stamp"),
                                (None, arm_argument_redaction, "stamp then redaction")):
    _l, _s = _stamp_probe(f"probe.both.{_label.replace(' ', '.')}")
    _name = _l.name
    if _first:
        _first(_name)
    arm_timestamps(_FMT, _name)
    if _second:
        _second(_name)
    logging.getLogger(f"{_name}.server").warning(
        "Invalid arguments for tool %r: %s", "edit_file", _DETAIL)
    _line = _s.getvalue().strip()
    ok(_SECRET not in _line and REDACTED in _line
       and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ", _line),
       f"{_label}: the line carries a time AND no payload — neither cure "
       f"dislodges the other", _line[:110])

# =====================================================================
# verdict
# =====================================================================

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
