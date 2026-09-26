#!/usr/bin/env bash
# swarm_check.sh — preflight for localswarm on this machine. Changes nothing permanent.
# Checks tools and bwrap user namespaces, then starts a TEMPORARY bridge with your profile and
# probes the model route through it (URL/prefix, API format, token, effort names, thinking
# switch, tool calls, streaming). Prints suggested profile.env lines.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PROFILE=${LOCALSWARM_PROFILE:-${XDG_CONFIG_HOME:-$HOME/.config}/localswarm/profile.env}
# shellcheck disable=SC1090
if [ -f "$PROFILE" ]; then set -a; . "$PROFILE"; set +a; echo "profile: $PROFILE"; else echo "profile: none (defaults) — expected at $PROFILE"; fi
fail=0
[ "$(uname -s)" = Linux ] || { echo "FAIL: Linux only (bwrap needs Linux user namespaces)"; exit 1; }
for t in bwrap claude python3 git curl; do
  if p=$(command -v "$t"); then echo "ok:   $t -> $(readlink -f "$p")"; else echo "FAIL: $t not found"; fail=1; fi
done
for t in node npm; do
  if p=$(command -v "$t"); then echo "ok:   $t -> $(readlink -f "$p")"; else echo "warn: $t not found (needed only for JS projects)"; fi
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "FAIL: python3 >= 3.9 needed"; fail=1; }
echo "claude version: $(claude --version)"
if bwrap --unshare-all --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib --ro-bind-try /usr/lib64 /lib64 --proc /proc --dev /dev /bin/true; then
  echo "ok:   bwrap can create a sandbox"
else
  echo "FAIL: bwrap cannot create user namespaces. On Ubuntu 24.04+ check"
  echo "      /proc/sys/kernel/apparmor_restrict_unprivileged_userns and the bwrap AppArmor profile."
  fail=1
fi
[ $fail -eq 0 ] || exit 1

url=${SWARM_LLM_URL:-http://127.0.0.1:8000/v1}
envf=${SWARM_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/localswarm/.env}
echo "upstream: $url  format: ${SWARM_API_FORMAT:-auto}  env file: $envf ($( [ -f "$envf" ] && echo present || echo absent ))"
tmp=$(mktemp -d)
python3 "$HERE/swarm_bridge.py" --sock-dir "$tmp" --llm-url "$url" >> "$tmp/bridge.out" 2>&1 &
for _ in $(seq 1 60); do [ -S "$tmp/llm.sock" ] && break; sleep 0.5; done
if [ -S "$tmp/llm.sock" ]; then
  grep "bridge up" "$tmp/bridge.log"
  python3 "$HERE/swarm_probe.py" "$tmp/llm.sock"; rc=$?
else
  echo "FAIL: temporary bridge did not start:"; cat "$tmp/bridge.out"; rc=1
fi
[ -f "$tmp/bridge.pid" ] && kill "$(cat "$tmp/bridge.pid")"
rm -rf "$tmp"
exit $rc
