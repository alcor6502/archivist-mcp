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
# The bench is a venv with the pins of requirements.txt installed --no-deps,
# exactly as the CI job does. No FastMCP: neither suite needs it. The engine is
# the mcp_common_engine/ folder of this repository and is not installed at all.
#
# Two suites, each with its own log and its own count: test_engine.py (the
# gate, the refusals, the config helpers) and test_vault.py (everything else).
# The exit code is the first one that is not zero.
#
# Every path is the script's own: never a fixed name under /tmp, which is shared
# and not yours, and where a `cat >` that fails does not stop the line after it.
set -eu

HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3.12}
WORK=${ARCHIVIST_WORK:-$(mktemp -d)}
VENV="$WORK/venv"
REQ="$HERE/requirements.txt"

if [ ! -x "$VENV/bin/python" ]; then
  echo "== bench: $VENV ($("$PY" --version 2>&1)) =="
  "$PY" -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c 'import reportlab, weasyprint, markdown, regex, pypdfium2, pillow_heif' 2>/dev/null; then
  echo "== bench: installing the pins of requirements.txt, --no-deps =="
  "$VENV/bin/pip" install -q --no-deps -r "$REQ"
fi

echo "== engine suite: $HERE/test_engine.py -> $WORK/engine.log =="
set +e
"$VENV/bin/python" "$HERE/test_engine.py" >"$WORK/engine.log" 2>&1
RC_ENGINE=$?
set -e
tail -1 "$WORK/engine.log"
echo "exit=$RC_ENGINE  log=$WORK/engine.log"

echo "== suite: $HERE/test_vault.py -> $WORK/suite.log =="
set +e
"$VENV/bin/python" "$HERE/test_vault.py" >"$WORK/suite.log" 2>&1
RC=$?
set -e
tail -3 "$WORK/suite.log"
echo "exit=$RC  log=$WORK/suite.log"
[ "$RC_ENGINE" -eq 0 ] || exit "$RC_ENGINE"
exit $RC
