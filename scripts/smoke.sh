#!/bin/sh
# The image, STARTED, before it is published. The suite proves the code on the
# runner; nothing proved the image — it was built, pushed as :latest, and run
# for the first time on the server that depends on it. A font package missing
# from the Dockerfile, a file missing from COPY, an entrypoint that no longer
# drops privileges: each of those is a green suite and a service that does not
# start, found at the Apply.
#
#   scripts/smoke.sh <image>
#
# It runs the image through its REAL entrypoint on an empty vault with one
# dataset, waits for the startup line carrying the version server.py declares,
# and then, inside the container and as the service user, runs
# scripts/smoke_inside.py: an unauthenticated call must be turned away, and
# both renderers must draw a PDF with the fonts the image carries.
#
# The three preflight checks that need a Tailscale node are skipped, by name:
# there is no node on a CI runner. Everything else in the preflight runs.
set -eu

IMAGE=${1:?usage: scripts/smoke.sh <image>}
HERE=$(cd "$(dirname "$0")/.." && pwd)
# One number, one place: the version is READ from server.py, never written here.
VER=$(sed -n 's/^VERSION = "\([^"]*\)"/\1/p' "$HERE/server.py")
[ -n "$VER" ] || { echo "SMOKE FAIL: no VERSION in server.py" >&2; exit 2; }

WORK=$(mktemp -d)
NAME="archivist-smoke-$$"
mkdir -p "$WORK/vault/Smoke" "$WORK/data"
printf '# Scheda\n\nRiga di prova, inventata.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n' >"$WORK/vault/Smoke/scheda.md"
printf 'Smoke |\n' >"$WORK/vault/keys.txt"

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

fail() {
  echo "SMOKE FAIL: $1" >&2
  echo "---- container log ----" >&2
  docker logs "$NAME" 2>&1 | tail -60 >&2 || true
  exit 1
}

# Every credential is made up on the spot: nothing here is a secret, and a
# literal key in a public repository is a finding for every scanner.
docker run -d --name "$NAME" \
  -v "$WORK/vault:/vault" -v "$WORK/data:/data" -v "$HERE/scripts:/smoke:ro" \
  -e BASE_URL=https://smoke.invalid \
  -e GITHUB_CLIENT_ID=smoke-client -e GITHUB_CLIENT_SECRET=smoke-secret \
  -e ALLOWED_GITHUB_LOGIN=smoke-user \
  -e JWT_SIGNING_KEY="$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')" \
  -e ALLOWED_CIDRS= -e HTTP_MODE=stateless \
  -e PREFLIGHT_SKIP=funnel,node_key,public_dns \
  "$IMAGE" >/dev/null

echo "== smoke: waiting for 'archivist-mcp $VER' in the log (60 s at most) =="
n=0
until docker logs "$NAME" 2>&1 | grep -q "archivist-mcp $VER (engine .*starting on"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ] \
    || fail "the container stopped before the startup line"
  n=$((n + 1)); [ $n -le 60 ] || fail "no startup line for version $VER within 60 s"
  sleep 1
done
echo "SMOKE ok: the image starts and says it is $VER"

docker logs "$NAME" 2>&1 | grep -q 'PREFLIGHT: .*starting' \
  || fail "the preflight did not pass"
SKIPPED=$(docker logs "$NAME" 2>&1 | grep -c 'SKIPPED (PREFLIGHT_SKIP)' || true)
[ "$SKIPPED" = "3" ] || fail "expected exactly 3 skipped preflight checks, found $SKIPPED"
echo "SMOKE ok: preflight passed, with the 3 Tailscale checks skipped and no other"

docker logs "$NAME" 2>&1 | grep -q 'dropping privileges' \
  || fail "the entrypoint did not drop privileges"
WHO=$(docker exec "$NAME" sh -c 'for p in /proc/[0-9]*; do if grep -q "server.py" "$p/cmdline" 2>/dev/null; then stat -c %u "$p"; break; fi; done')
[ "$WHO" = "99" ] || fail "server.py runs as uid '$WHO', not 99"
echo "SMOKE ok: the service runs as uid 99"

# A second or two for uvicorn to bind after the startup line.
sleep 3
docker exec -u 99:100 -w /app "$NAME" python3 /smoke/smoke_inside.py \
  || fail "the checks inside the container failed (their own lines are above)"
echo "SMOKE: all passed for $IMAGE"
