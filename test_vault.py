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


def dockerfile_env_check() -> None:
    """FastMCP reads its settings when it is IMPORTED, so they cannot be set
    from inside server.py — they live in the Dockerfile as ENV. That makes them
    easy to delete by accident, and the only symptom would be the noise quietly
    coming back. This is the tripwire.

    Values verified against fastmcp 3.4.5."""
    df = (HERE / "Dockerfile").read_text(encoding="utf-8")
    for var, val in (("FASTMCP_SHOW_SERVER_BANNER", "false"),
                     ("FASTMCP_ENABLE_RICH_LOGGING", "false"),
                     ("FASTMCP_CHECK_FOR_UPDATES", "off"),
                     ("FASTMCP_LOG_LEVEL", "WARNING")):
        ok(f"ENV {var}={val}" in df, f"Dockerfile sets {var}={val}")


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
        # does not exist, and as a path it can only mean a file inside a dataset,
        # where there is none. The registry in the vault root stays untouched.
        must_fail("keys.txt as a path inside a dataset",
                  lambda: ds.read_file("keys.txt"))
        ok((Path(root) / "keys.txt").read_text().startswith("Example Project"),
           "the key registry is still where it was")

        print("\n[2b] the ambiguous path — the v1.8 shape MUST be refused")
        # A caller not yet rewritten sends the dataset twice: once in `dataset`,
        # once as the head of `path`. Refused loudly, on reads exactly as on
        # writes — a read that normalised would teach the wrong form and never
        # complain.
        for label, fn in (
            ("write", lambda: v.open("Example Project", "Example Project/log.md", KEY)),
            ("read", lambda: v.open("Example Project", "Example Project/01 Notes/a.md", KEY)),
            ("bare dataset as path", lambda: v.open("Example Project", "Example Project", KEY)),
            ("folded case", lambda: v.open("Example Project", "example project/log.md", KEY)),
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
        returns = {
            "list_files": ds.list_files(""),
            "list_files (one file)": ds.list_files("log.md"),
            "read_file": ds.read_file("log.md"),
            "manifest": ds.manifest(""),
            "search": ds.search("line", ""),
            "history": ds.history("", 5),
            "archive": ds.archive("", "*.md"),
            "status": ds.status(),
        }
        for label, res in returns.items():
            ok(res.get("dataset") == "Example Project", f"{label} echoes the dataset", res.get("dataset"))
        for label, field in (("list_files", "base"), ("list_files (one file)", "file"),
                             ("read_file", "path"), ("manifest", "base"), ("history", "path")):
            val = returns[label][field]
            ok(not val.startswith("Example Project"),
               f"{label}[{field}] carries no dataset prefix", val)
        ok(all(not ln.startswith("Example Project") for ln in returns["search"]["lines"]),
           "search lines are file:line:text, relative", returns["search"]["lines"][:1])
        ok(all(not f["path"].startswith("Example Project") for f in returns["list_files"]["files"]),
           "every entry in list_files is relative")

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

        print("\n[14] the Dockerfile still quiets FastMCP down")
        dockerfile_env_check()

        print("\n[15] the IP filter list, in both directions")
        cidr_checks()

        print(f"\n{'=' * 46}\n  {OK} passed, {FAIL} failed\n{'=' * 46}")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
