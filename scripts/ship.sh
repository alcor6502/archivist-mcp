#!/bin/sh
# One delivery, end to end, from a machine that has nothing but the clone:
# suite, commit of the NAMED files, push straight to main, proof of the push,
# and — when VERSION moved — the link that publishes the release.
#
#   scripts/ship.sh <message-file> <file>...
#
# The message file is the commit message: one lowercase line saying what
# changes and why, a blank line, then the body. Files are NAMED, never `-A`:
# a working tree can hold another hand's half-written change, and `-A` has
# already shipped one of those with the suite green.
#
# Why there is no tag push: from a sandbox `git push origin v2.9.0` answers 403
# — branches are pushed, tags are not — and a tag typed on a tablet comes out
# as `V2.9.0`, which the workflow's case-sensitive glob ignores in silence. So
# the tag is never typed: the script prints a release URL with the tag, the
# target and the notes already filled in, and one tap on Publish creates the
# tag that starts the same CI. The tag comes out lightweight; the workflow
# reads GITHUB_REF_NAME and does not care.
set -eu

HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"

if [ $# -lt 2 ]; then
  echo "usage: scripts/ship.sh <message-file> <file>..." >&2
  exit 2
fi
MSG=$1; shift
[ -s "$MSG" ] || { echo "message file missing or empty: $MSG" >&2; exit 2; }

# The identity is the anonymous one, passed on the command, never written to
# any config: the personal address reached a public commit once, and taking it
# back meant rewriting history.
GIT="git -c user.name=alcor6502 -c user.email=14092600+alcor6502@users.noreply.github.com"
REMOTE=origin
BRANCH=main

echo "== suite =="
scripts/test.sh

echo "== tree =="
git status --short
for f in "$@"; do [ -e "$f" ] || { echo "no such file: $f" >&2; exit 2; }; done
git add -- "$@"
if git diff --cached --quiet; then
  echo "nothing staged from: $*" >&2
  exit 2
fi

echo "== commit =="
$GIT commit -q -F "$MSG"
git log --oneline -1

echo "== push -> $REMOTE/$BRANCH =="
# Network failures retry with a backoff; a refusal (403, non-fast-forward)
# does not, because the second attempt would answer the same.
n=0; delay=2
until git push "$REMOTE" "HEAD:refs/heads/$BRANCH" 2>"$HERE/.push.err"; do
  if grep -qiE 'rejected|403|denied|non-fast-forward' "$HERE/.push.err"; then
    cat "$HERE/.push.err" >&2; rm -f "$HERE/.push.err"; exit 1
  fi
  n=$((n + 1)); [ $n -le 4 ] || { cat "$HERE/.push.err" >&2; rm -f "$HERE/.push.err"; exit 1; }
  echo "   push failed, retry $n in ${delay}s"; sleep $delay; delay=$((delay * 2))
done
rm -f "$HERE/.push.err"

# The push is not believed on its word: the hash on the remote is the proof.
git fetch -q "$REMOTE" "$BRANCH"
LOCAL=$(git rev-parse HEAD); REMOTEHEAD=$(git rev-parse "$REMOTE/$BRANCH")
if [ "$LOCAL" != "$REMOTEHEAD" ]; then
  echo "push NOT proven: HEAD $LOCAL but $REMOTE/$BRANCH is $REMOTEHEAD" >&2
  exit 1
fi
echo "   $REMOTE/$BRANCH = $(git log --oneline -1 "$REMOTE/$BRANCH")"

# A release only when the version constant moved in THIS commit.
if git show --format= --name-only HEAD | grep -qx server.py \
   && git show HEAD -- server.py | grep -q '^+VERSION = '; then
  VER=$(sed -n 's/^VERSION = "\([^"]*\)"/\1/p' server.py)
  OWNER_REPO=$(git remote get-url "$REMOTE" | sed -E 's#.*github.com[:/]##; s#\.git$##')
  URL=$(python3 - "$MSG" "$VER" "$OWNER_REPO" <<'PY'
import re, sys, urllib.parse
msg, ver, repo = sys.argv[1:]
lines = open(msg, encoding="utf-8").read().strip().splitlines()
# Trailers (Co-Authored-By:, Signed-off-by:, ...) are for git, not for the
# release page: strip the trailing block of `Token: value` lines.
while lines and re.match(r"^[A-Za-z][A-Za-z-]*: ", lines[-1]):
    lines.pop()
title = f"v{ver} — {lines[0]}"
body = "\n".join(lines[2:]).strip()
q = urllib.parse.urlencode({"tag": f"v{ver}", "target": "main", "title": title, "body": body})
print(f"https://github.com/{repo}/releases/new?{q}")
PY
)
  # A markdown link, not the URL in full: pasted into a chat it renders as
  # the tag name and opens the prefilled release page on a click. The URL
  # itself runs to a couple of thousand characters and reads as noise.
  echo "== release v$VER: open, check, tap Publish =="
  echo "[v$VER]($URL)"
fi
