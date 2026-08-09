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

    src = (HERE / "server.py").read_text(encoding="utf-8")
    ok("mcp.add_middleware(Gate())" in src, "the Gate is actually registered")
    # A refused request and a broken deployment look identical at the client.
    # The log line is the only thing that tells them apart, so it is part of
    # the contract, not of the comfort.
    body = ast.get_source_segment(src, gate) or ""
    ok(body.count("log.warning") >= 2,
       "both refusals are logged, identity and origin", body.count("log.warning"))


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

    def rendered(fn: ast.FunctionDef) -> str:
        pad = [None] * (len(fn.args.args) - len(fn.args.defaults))
        parts = [a.arg if d is None else f"{a.arg}={ast.unparse(d)}"
                 for a, d in zip(fn.args.args, pad + list(fn.args.defaults))]
        return f"{fn.name}({', '.join(parts)})"

    real = [rendered(n) for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and any(isinstance(d, ast.Attribute) and d.attr == "tool"
                    and isinstance(d.value, ast.Name) and d.value.id == "mcp"
                    for d in n.decorator_list)]
    ok(bool(real), "the AST finds the tools at all", len(real))

    guide = (HERE / "reference-guide.md").read_text(encoding="utf-8")
    names = {r.split("(", 1)[0] for r in real}
    written = [ln.strip() for ln in guide.splitlines()
               if ln.startswith("    ") and ln.strip().split("(", 1)[0] in names
               and ln.strip().endswith(")")]

    missing = [r for r in real if r not in written]
    ok(not missing, "every tool is in the guide with its exact signature", missing)

    # The other direction, which is the one that rots silently: a signature that
    # stays in the manual after the tool changed under it.
    stale = [w for w in written if w not in real]
    ok(not stale, "the guide promises no signature the code does not have", stale)

    # Named in prose but never declared: the reader is sent to a door that is
    # not there. `status()` is the one that actually happened — it reads like a
    # tool, it is not one, and the two real ones are vault_status() and
    # dataset_status(). The lookbehind is what keeps those two from matching.
    ghosts = re.findall(r"(?<![\w.])(status|drop|create)\s*\(", guide)
    ok(not ghosts, "the guide names no bare verb that is not a tool", sorted(set(ghosts)))

    # The two READMEs document the same surface at greater length, and they are
    # what a stranger reads before installing anything. They spell each tool in
    # bold WITHOUT `key`, which is explained once for all of them, so that one
    # parameter is dropped before comparing. Quotes are normalised because Python
    # renders defaults with single quotes and the prose uses double ones: that is
    # a difference in typography, and a test that fails on typography gets
    # switched off.
    bare = [re.sub(r", key='[^']*'", "", r).replace("'", '"') for r in real]
    for name in ("README.md", "README.it.md"):
        text = (HERE / name).read_text(encoding="utf-8").replace("'", '"')
        absent = [s for s in bare if f"**`{s}`**" not in text]
        ok(not absent, f"{name} spells every tool with its exact signature", absent)

    # The limits are the other kind of number the documents copy by hand, and
    # the one a caller plans around: told 2 MB when the real ceiling is 1, they
    # build a call that fails. The prose keeps its human form — nobody wants to
    # read 2000000 — so the test renders the constant the way a person writes it
    # and looks for that string. Both files, both languages.
    import vault as _v
    limits = [("MAX_READ_BYTES", _v.MAX_READ_BYTES, 2_000_000, "2 MB"),
              ("MAX_WRITE_BYTES", _v.MAX_WRITE_BYTES, 2_000_000, "2 MB"),
              ("MAX_BINARY_BYTES", _v.MAX_BINARY_BYTES, 2_000_000, "2 MB"),
              ("MAX_APPEND_BYTES", _v.MAX_APPEND_BYTES, 64_000, "64 KB"),
              ("MAX_LIST_FILES", _v.MAX_LIST_FILES, 3_000, "3,000"),
              ("MAX_SEARCH_HITS", _v.MAX_SEARCH_HITS, 200, "200 lines"),
              ("MAX_DIFF_BYTES", _v.MAX_DIFF_BYTES, 60_000, "60 KB"),
              ("MAX_ARCHIVE_BYTES", _v.MAX_ARCHIVE_BYTES, 30_000_000, "30 MB"),
              ("MAX_ARCHIVE_OUT_BYTES", _v.MAX_ARCHIVE_OUT_BYTES, 5_000_000, "5 MB"),
              ("MAX_DATASETS", _v.MAX_DATASETS, 200, "datasets in the vault | 200")]
    for const, value, pinned, shown in limits:
        ok(value == pinned and shown in guide,
           f"{const} and the line the guide prints for it still agree", value)


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

        print("\n[13] static server.py <-> vault.py consistency")
        static_api_check()
        single_door_check()

        print("\n[14] the Dockerfile still quiets FastMCP down")
        dockerfile_env_check()

        print("\n[14b] the Gate is wired to the hook it claims")
        gate_hook_check()

        print("\n[14c] the manual says what the code actually offers")
        guide_signature_check()

        print("\n[15] the IP filter list, in both directions")
        cidr_checks()

        print(f"\n{'=' * 46}\n  {OK} passed, {FAIL} failed\n{'=' * 46}")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
