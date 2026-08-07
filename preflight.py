"""
preflight.py — 10 checks that do not warn: if one fails the process exits 2 and
the service does NOT start (a check that crashes counts as FAILED, not passed).

Selective skip (for local testing only, never in production):
  PREFLIGHT_SKIP="funnel,node_key"
"""
from __future__ import annotations
import os, subprocess, sys, secrets

SKIP = {s.strip() for s in os.environ.get("PREFLIGHT_SKIP", "").split(",") if s.strip()}
RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def deco(fn):
        def run():
            if name in SKIP:
                RESULTS.append((name, True, "SKIPPED (PREFLIGHT_SKIP)")); return
            try:
                msg = fn()
                RESULTS.append((name, True, msg or "ok"))
            except Exception as e:  # a crash counts as a failure
                RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        return run
    return deco


V = os.environ.get("VAULT_ROOT", "/vault")
KEYS = os.environ.get("KEYS_FILE", os.path.join(V, "keys.txt"))


def _datasets() -> list[str]:
    return sorted(d for d in os.listdir(V)
                  if os.path.isdir(os.path.join(V, d))
                  and not d.startswith(".") and not d.startswith("_"))


@check("datasets")
def c_datasets():
    # A vault with no datasets is not an error (it may be brand new), but a root
    # that does not exist or cannot be read is — usually a wrong mount.
    if not os.path.isdir(V):
        raise RuntimeError(f"{V} does not exist: wrong mount?")
    ds = _datasets()
    stray = [f for f in os.listdir(V)
             if os.path.isfile(os.path.join(V, f)) and not f.startswith(".")
             and f != os.path.basename(KEYS)]
    note = f" (note: {len(stray)} stray files in the root, unreachable from the tools)" if stray else ""
    return f"{len(ds)} datasets: {', '.join(ds) if ds else '(none)'}{note}"


@check("readable")
def c_readable():
    total = ok = 0
    for d in _datasets():
        for dirpath, dirnames, filenames in os.walk(os.path.join(V, d)):
            dirnames[:] = [x for x in dirnames if x != ".git"]
            for f in filenames:
                if not f.endswith(".md"):
                    continue
                total += 1
                p = os.path.join(dirpath, f)
                if len(open(p, "rb").read()) == os.path.getsize(p):
                    ok += 1
    if ok != total:
        raise RuntimeError(f"read {ok} of {total} .md files: some declare bytes they do not deliver")
    return f"{ok}/{total} .md files read in full"


@check("writable")
def c_writable():
    p = os.path.join(V, f".preflight-{secrets.token_hex(4)}")
    open(p, "w").write("x")
    os.unlink(p)  # on some mounts deletion fails where writing succeeds
    return "writes AND deletes"


@check("git")
def c_git():
    ds = _datasets()
    if not ds:
        return "no datasets to check"
    missing = []
    for d in ds:
        r = subprocess.run(["git", "-C", os.path.join(V, d), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            missing.append(d)
    if missing:
        raise RuntimeError(f"git repository missing in: {', '.join(missing)} "
                           "(the server creates them at boot: relaunch)")
    return f"{len(ds)} repositories present"


@check("keys")
def c_keys():
    # The registry may be absent (all datasets open): not an error. If it is
    # there it must be readable and syntactically sane, or a dataset you believe
    # is protected would silently be open.
    if not os.path.exists(KEYS):
        return f"no registry at {KEYS}: every dataset is open"
    if not os.access(KEYS, os.R_OK):
        raise RuntimeError(f"{KEYS} is not readable by the service user: check owner and mode (99:100, 640)")
    known = {d.casefold() for d in _datasets()}
    n = 0
    orphans = []
    for line in open(KEYS, encoding="utf-8", errors="replace").read().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" not in line and "  " not in line:
            raise RuntimeError(f"registry line without a separator: {line[:40]!r} — a TAB is required")
        name = line.split("\t", 1)[0].strip()
        n += 1
        if name.casefold() not in known:
            orphans.append(name)
    note = f"; {len(orphans)} orphan lines ({', '.join(orphans[:3])}): no such dataset" if orphans else ""
    return f"{n} protected datasets{note}"


@check("oauth")
def c_oauth():
    # The most important check: without credentials this would be an authless
    # Funnel, indexed within minutes via certificate transparency logs.
    for k in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "ALLOWED_GITHUB_LOGIN", "BASE_URL"):
        v = os.environ.get(k, "")
        if not v or "CHANGEME" in v.upper() or "CAMBIAMI" in v.upper():
            raise RuntimeError(f"{k} missing or still a placeholder")
    if len(os.environ.get("JWT_SIGNING_KEY", "")) < 32:
        raise RuntimeError("JWT_SIGNING_KEY missing or too short (openssl rand -hex 32)")
    if not os.environ["BASE_URL"].startswith("https://"):
        raise RuntimeError("BASE_URL must be https")
    return "credentials present"


@check("token_store")
def c_token_store():
    # The OAuth store must live on a PERSISTENT volume: inside the container
    # filesystem, every recreation would throw the tokens away and the client
    # would ask for re-authorisation at every maintenance.
    h = os.environ.get("FASTMCP_HOME", "")
    if not h.startswith("/data"):
        raise RuntimeError(f"FASTMCP_HOME={h!r}: it must live under /data (persistent volume)")
    os.makedirs(h, exist_ok=True)
    p = os.path.join(h, ".w"); open(p, "w").write("x"); os.unlink(p)
    return h


@check("funnel")
def c_funnel():
    r = subprocess.run(["tailscale", "funnel", "status"], capture_output=True, text=True, timeout=10)
    out = r.stdout + r.stderr
    if r.returncode != 0:
        raise RuntimeError(f"tailscale funnel status: {out.strip()[:200]}")
    if "Funnel on" not in out:
        raise RuntimeError(f"Funnel is NOT on: {out.strip()[:200]}")
    port = os.environ.get("PORT", "3000")
    if port not in out:
        raise RuntimeError(f"Funnel is on but not towards port {port}: {out.strip()[:200]}")
    return "Funnel on, correct port"


@check("node_key")
def c_node_key():
    # "expires in 179 days" is a scheduled silent outage.
    r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError("tailscale status does not answer")
    import json
    ke = (json.loads(r.stdout).get("Self") or {}).get("KeyExpiry")
    if ke:
        raise RuntimeError(f"the node key EXPIRES ({ke}): disable expiry in the Tailscale admin console")
    return "key expiry disabled"


@check("public_dns")
def c_dns():
    # The BASE_URL hostname must resolve, or the client never arrives.
    import socket
    host = os.environ["BASE_URL"].split("//", 1)[1].split("/")[0]
    socket.getaddrinfo(host, 443)
    return f"{host} resolves"


CHECKS = [c_datasets, c_readable, c_writable, c_git, c_keys,
          c_oauth, c_token_store, c_funnel, c_node_key, c_dns]

if __name__ == "__main__":
    for fn in CHECKS:
        fn()
    width = max(len(n) for n, _, _ in RESULTS)
    failed = sum(0 if p else 1 for _, p, _ in RESULTS)
    for name, passed, msg in RESULTS:
        print(f"  {'OK ' if passed else 'FAIL'}  {name:<{width}}  {msg}")
    if failed:
        print(f"PREFLIGHT: {failed} checks failed — the service will NOT start.")
        sys.exit(2)
    print(f"PREFLIGHT: {len(CHECKS)}/{len(CHECKS)} — starting.")
