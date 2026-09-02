#!/bin/sh
set -e
cd /app
U="${VAULT_UID:-99}"; G="${VAULT_GID:-100}"
V="${VAULT_ROOT:-/vault}"

# One git repository per dataset, so safe.directory has to cover every child of
# the root. Needed both as root (here) and as the service user (below).
# --replace-all, not --add: the service user's HOME is on the persistent volume,
# and --add appended one more identical line to its .gitconfig at EVERY
# restart — parsed by every git call, forever. Found on 2026-09-02 by reading.
echo "== git init (as root) =="
git config --global --replace-all safe.directory '*'
python3 - <<'PY'
import os
from vault import VaultRoot, VaultError
try:
    v = VaultRoot(os.environ.get("VAULT_ROOT", "/vault"),
                  os.environ.get("KEYS_FILE"))
    for line in v.boot(int(os.environ.get("GIT_RETENTION_MONTHS", "0") or 0)):
        print("  " + line)
except VaultError as e:
    print(f"dataset init skipped: {e}")
PY

echo "== permissions: chown ${U}:${G} + 666/777 on vault and data =="
chown -R "$U:$G" "$V" /data
find "$V" -path '*/.git' -prune -o -type d -exec chmod 777 {} +
find "$V" -path '*/.git/*' -prune -o -type f -exec chmod 666 {} +
# The key registry stays 640: the service reads it, the world does not.
if [ -f "${KEYS_FILE:-$V/keys.txt}" ]; then
  chmod 640 "${KEYS_FILE:-$V/keys.txt}"
  echo "   key registry: $(basename "${KEYS_FILE:-$V/keys.txt}") 640 ${U}:${G}"
fi

echo "== dropping privileges -> uid ${U} gid ${G}, umask 000 =="
export HOME=/data/home; mkdir -p "$HOME"; chown "$U:$G" "$HOME"
exec setpriv --reuid "$U" --regid "$G" --clear-groups /bin/sh -c '
  umask 000
  git config --global --replace-all safe.directory "*" 2>/dev/null || true
  echo "== preflight (as the service user) =="
  python3 preflight.py || exit $?
  echo "== server =="
  exec python3 server.py
'
