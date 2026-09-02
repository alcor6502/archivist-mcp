#!/bin/sh
# The suite, run the only way its verdict can be trusted: every line into a log,
# then the EXIT CODE. A suite piped into `tail` shows the last lines of stdout,
# which are PASS lines, while the traceback that killed it went to stderr — a
# suite dead halfway reads green. Measured on 2026-08-13; not again.
#
#   scripts/test.sh              build the bench, run test_vault.py, print the verdict
#   PYTHON=python3.12            the interpreter (default: python3.12 — the image's)
#   ARCHIVIST_WORK=<dir>         reuse a bench instead of building a fresh one
#
# The bench is a venv with the common engine installed FROM THE PIN in
# requirements.txt — the tarball when the network allows it, a git clone of the
# same tag when it does not (a sandbox whose egress policy answers 403 to the
# tarball still serves git). Either way the suite's own check compares the pin
# with what got installed, so the two cannot drift.
#
# Every path is the script's own: never a fixed name under /tmp, which is shared
# and not yours, and where a `cat >` that fails does not stop the line after it.
set -eu

HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3.12}
WORK=${ARCHIVIST_WORK:-$(mktemp -d)}
VENV="$WORK/venv"
REQ="$HERE/requirements.txt"

# One pin, one place: the tag is READ from requirements.txt, never written here.
TAG=$(sed -n 's#.*mcp-common-engine/archive/refs/tags/\(v[0-9][0-9.]*\)\.tar\.gz.*#\1#p' "$REQ")
if [ -z "$TAG" ]; then
  echo "requirements.txt does not pin mcp-common-engine to a tag" >&2
  exit 2
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "== bench: $VENV ($("$PY" --version 2>&1)) =="
  "$PY" -m venv "$VENV"
fi
PIP="$VENV/bin/pip"
if ! "$VENV/bin/python" -c 'import mcp_common_engine, reportlab' 2>/dev/null; then
  echo "== engine $TAG: from the pin =="
  if ! "$PIP" install -q --no-deps -r "$REQ" 2>"$WORK/pip.err"; then
    echo "   tarball refused ($(grep -o 'HTTP error [0-9]*' "$WORK/pip.err" | head -1 || echo see "$WORK/pip.err")): cloning the tag instead"
    rm -rf "$WORK/engine"
    git -c advice.detachedHead=false clone -q --depth 1 --branch "$TAG" https://github.com/alcor6502/mcp-common-engine.git "$WORK/engine"
    "$PIP" install -q --no-deps "$WORK/engine"
  fi
fi

echo "== suite: $HERE/test_vault.py -> $WORK/suite.log =="
set +e
"$VENV/bin/python" "$HERE/test_vault.py" >"$WORK/suite.log" 2>&1
RC=$?
set -e
tail -3 "$WORK/suite.log"
echo "exit=$RC  log=$WORK/suite.log"
exit $RC
