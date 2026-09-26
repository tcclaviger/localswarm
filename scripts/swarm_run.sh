#!/usr/bin/env bash
# swarm_run.sh — run ONE local-model Claude Code agent (or one gate command) inside a bwrap sandbox.
#
#   swarm_run.sh --ws DIR --name NAME --effort off|low|medium|high --prompt FILE
#                [--role impl|review] [--cwd DIR] [--timeout SECS] [--retries N]
#                [--ro PATH]... [--rw PATH]... [--git-dir REPO/.git] [--ro-ws] [--model ID]
#   swarm_run.sh --ws DIR --name NAME --exec "COMMAND" [--cwd DIR] [--timeout SECS] [--ro-ws]
#
# Layout: every process file lives under DIR/workingtemp/ (never elsewhere in the project):
#   workingtemp/logs/NAME.*        stream logs, .result (final text), .failures, .exec.log
#   workingtemp/homes/NAME/        the agent's private HOME + Claude config (fresh per agent name)
#   workingtemp/scratch/NAME/      the agent's scratch folder for notes, drafts, test scripts, output
# The runner prepends a "Workspace rules" block to EVERY prompt that tells the agent to keep all
# non-deliverable files in its scratch folder. If DIR is a git repository, workingtemp/ is added
# to .git/info/exclude.
#
# Fence:
#   impl   : DIR is read-write.
#   review : DIR is NOT mounted unless --ro-ws (read-only, with DIR/workingtemp hidden). Otherwise
#            only --rw/--ro paths (e.g. a snapshot, a reviews dir, a spec) + --git-dir read-only.
#   Both   : read-only /usr + a few /etc files + auto-detected toolchains; own PID/IPC/UTS/net
#            namespaces; no /run, /home, docker socket or systemd bus. Network = the bridge only
#            (filtered model route + public HTTPS). Settings deny global installs, push, sudo.
#   --exec : same fence as impl (or --ro-ws view); runs COMMAND with bash, no model involved.
#            Used for command gates. Exit code of COMMAND is returned; no retries.
# Effort: off = thinking disabled (bridge merges SWARM_THINK_OFF_KWARGS into chat_template_kwargs)
#         low | medium | high = value from SWARM_EFFORT_MAP, sent via CLAUDE_CODE_EFFORT_LEVEL.
# Config: ${LOCALSWARM_PROFILE:-${XDG_CONFIG_HOME:-~/.config}/localswarm/profile.env}
# Failures are classified by category (scripts/swarm_classify.py) and written to NAME.status:
#   api_transient (timeouts, connection errors, 408/429/502/503/504/529) and degenerate output
#   are retried with backoff; a stalled end resumes the same session; api_fatal (500/5xx,
#   400/401/403/404/413/422), run_timeout, sandbox and config errors stop at once and surface.
# Exit: 0 ok, 1 gave up after retries (or --exec command failed: its own code), 2 bad args or
#       config error, 3 bridge/sandbox/server not available, 4 api_fatal, 5 run_timeout.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SKILL=$(dirname "$HERE")
PROFILE=${LOCALSWARM_PROFILE:-${XDG_CONFIG_HOME:-$HOME/.config}/localswarm/profile.env}
# Exported so an auto-started bridge sees them; the sandbox itself starts from --clearenv.
# shellcheck disable=SC1090
if [ -f "$PROFILE" ]; then set -a; . "$PROFILE"; set +a; fi

role=impl; ws=""; name=""; effort=""; pfile=""; cwd=""; tmo=3600; retries=3; gitdir=""; model=${SWARM_MODEL:-}
execcmd=""; rows=0
ro=(); rw=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ws) ws=$2; shift 2 ;;            --name) name=$2; shift 2 ;;
    --effort) effort=$2; shift 2 ;;    --prompt) pfile=$2; shift 2 ;;
    --role) role=$2; shift 2 ;;        --cwd) cwd=$2; shift 2 ;;
    --timeout) tmo=$2; shift 2 ;;      --retries) retries=$2; shift 2 ;;
    --ro) ro+=("$2"); shift 2 ;;       --rw) rw+=("$2"); shift 2 ;;
    --git-dir) gitdir=$2; shift 2 ;;   --model) model=$2; shift 2 ;;
    --exec) execcmd=$2; shift 2 ;;     --ro-ws) rows=1; shift ;;
    *) echo "swarm_run: unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$ws" ] && [ -n "$name" ] || { echo "swarm_run: need --ws and --name" >&2; exit 2; }
case "$name" in *[!A-Za-z0-9._-]*) echo "swarm_run: --name may only use A-Z a-z 0-9 . _ -" >&2; exit 2 ;; esac
if [ -z "$execcmd" ]; then
  [ -n "$effort" ] && [ -f "$pfile" ] || { echo "swarm_run: need --effort and --prompt(existing file), or --exec" >&2; exit 2; }
fi
case "$role" in impl|review) ;; *) echo "swarm_run: role must be impl|review" >&2; exit 2 ;; esac

# Effort mapping
EFFORT_MAP=${SWARM_EFFORT_MAP:-"low=low medium=medium high=high"}
map_effort() { local k=$1 pair; for pair in $EFFORT_MAP; do [ "${pair%%=*}" = "$k" ] && { echo "${pair#*=}"; return 0; }; done; return 1; }
headers=""; envlevel=""
case "${effort:-none}" in
  none) ;;
  off) envlevel=$(map_effort low) || envlevel=low; headers="X-Swarm-Thinking: off" ;;
  low|medium|high) envlevel=$(map_effort "$effort") || { echo "swarm_run: SWARM_EFFORT_MAP has no '$effort'" >&2; exit 2; } ;;
  *) echo "swarm_run: effort must be off|low|medium|high" >&2; exit 2 ;;
esac

command -v bwrap > /dev/null || { echo "swarm_run: bwrap not installed" >&2; exit 3; }
mkdir -p "$ws"; ws=$(readlink -f "$ws"); cwd=${cwd:-$ws}

LLM_URL=${SWARM_LLM_URL:-http://127.0.0.1:8000/v1}
PROXY_PORT=${SWARM_PROXY_PORT:-3128}
INNER_LLM_PORT=${SWARM_INNER_LLM_PORT:-18080}
SOCK=${SWARM_SOCK_DIR:-${XDG_RUNTIME_DIR:-/tmp}/localswarm-$(id -u)}
WT=$ws/workingtemp; logdir=$WT/logs; home=$WT/homes/$name; scratch=$WT/scratch/$name
mkdir -p "$logdir" "$home/.claude" "$home/tmp" "$scratch"
cp "$SKILL/templates/settings-$role.json" "$home/.claude/settings.json"
rm -rf "$home/.claude/agents"; cp -r "$SKILL/templates/agents" "$home/.claude/agents"
# Keep workingtemp/ and the tickets/ registry out of git when the workspace itself is a repository
# (the orchestrator may commit tickets/ deliberately).
if [ -d "$ws/.git" ]; then
  mkdir -p "$ws/.git/info"; touch "$ws/.git/info/exclude"
  for d in workingtemp/ tickets/; do grep -qx "$d" "$ws/.git/info/exclude" || echo "$d" >> "$ws/.git/info/exclude"; done
fi
TICKETS=$ws/tickets

# Bridge: start it if it is not up (detached; survives this script). It reads the remaining
# settings (SWARM_API_FORMAT, SWARM_ENV_FILE, SWARM_TOKEN_VAR, SWARM_THINK_OFF_KWARGS,
# SWARM_UPSTREAM_TIMEOUT) from the exported profile; only the bridge ever reads the token.
# A socket file alone is not proof: a killed bridge leaves it behind. Trust the pid file.
bridge_alive() { [ -S "$SOCK/llm.sock" ] && [ -f "$SOCK/bridge.pid" ] && kill -0 "$(cat "$SOCK/bridge.pid")" 2> "$SOCK/.alive.err"; }
if ! bridge_alive; then
  rm -f "$SOCK/llm.sock" "$SOCK/net.sock" "$SOCK/bridge.pid"
  bargs=(--sock-dir "$SOCK" --llm-url "$LLM_URL")
  for s in ${SWARM_BLOCK_SUFFIXES:-}; do bargs+=(--block-suffix "$s"); done
  [ -n "${SWARM_BRIDGE_TCP_PORT:-}" ] && bargs+=(--tcp-port "$SWARM_BRIDGE_TCP_PORT")
  mkdir -p "$SOCK"; chmod 700 "$SOCK"
  setsid nohup python3 "$HERE/swarm_bridge.py" "${bargs[@]}" >> "$SOCK/bridge.out" 2>&1 < /dev/null &
  for _ in $(seq 1 40); do [ -S "$SOCK/llm.sock" ] && break; sleep 0.3; done
  [ -S "$SOCK/llm.sock" ] || { echo "swarm_run: bridge failed to start, see $SOCK/bridge.out" >&2; exit 3; }
fi

# Model id: explicit/profile, else first model the server lists (asked through the bridge, so
# remote endpoints get their token without this script ever seeing it).
if [ -z "$execcmd" ] && [ -z "$model" ]; then
  model=$(curl -s -m 15 --unix-socket "$SOCK/llm.sock" http://bridge/v1/models | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])') \
    || { echo "swarm_run: no model list from $LLM_URL via the bridge (see $SOCK/bridge.log; check URL, format and token)" >&2; exit 3; }
fi

args=(--unshare-all --die-with-parent --new-session
  --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/sbin /sbin
  --ro-bind-try /usr/lib64 /lib64
  --ro-bind-try /etc/ssl /etc/ssl --ro-bind-try /etc/ca-certificates /etc/ca-certificates
  --ro-bind-try /etc/pki /etc/pki --ro-bind-try /etc/alternatives /etc/alternatives
  --ro-bind-try /etc/ld.so.cache /etc/ld.so.cache --ro-bind-try /etc/passwd /etc/passwd
  --ro-bind-try /etc/group /etc/group --ro-bind-try /etc/nsswitch.conf /etc/nsswitch.conf
  --ro-bind-try /etc/localtime /etc/localtime --ro-bind-try /etc/hosts /etc/hosts
  --proc /proc --dev /dev --tmpfs /tmp
  --ro-bind "$HERE/swarm_fwd.py" /opt/swarm/swarm_fwd.py --ro-bind "$SOCK" /sock)

# Toolchains outside /usr (nvm, ~/.local, /opt, ...): mount their install prefix read-only at a
# neutral path /opt/swarm/tc/N (so no host path or user name shows inside) and expose the binary
# via /opt/swarm/bin. The prefix is two levels above the real binary
# (e.g. <nvm>/versions/node/vX for <nvm>/versions/node/vX/bin/node); never $HOME or / itself.
declare -A seen=(); tcn=0; ldpath=""
for tool in claude node npm npx git python3 ${SWARM_EXTRA_TOOLS:-}; do
  p=$(command -v "$tool") || { [ "$tool" = claude ] && [ -z "$execcmd" ] && { echo "swarm_run: claude CLI not found" >&2; exit 3; }; continue; }
  real=$(readlink -f "$p")
  # Version-manager shims (pyenv, asdf, ...) are scripts that need their manager at run time:
  # resolve python to the real interpreter instead of the shim.
  if [ "$tool" = python3 ]; then
    real=$("$p" -c 'import os,sys; print(os.path.realpath(sys.executable))') || continue
  fi
  case "$real" in /usr/*) continue ;; esac
  prefix=$(dirname "$(dirname "$real")")
  case "$prefix" in /|/home|"$HOME"|/opt|/root) prefix=$(dirname "$real") ;; esac
  if [ -z "${seen[$prefix]:-}" ]; then
    tcn=$((tcn + 1)); seen[$prefix]=/opt/swarm/tc/$tcn
    args+=(--ro-bind "$prefix" "${seen[$prefix]}")
    # Relocated builds that link their own shared libs by absolute path (pyenv python, ...)
    # find them again through LD_LIBRARY_PATH.
    [ -d "$prefix/lib" ] && ldpath="${ldpath:+$ldpath:}${seen[$prefix]}/lib"
  fi
  args+=(--symlink "${seen[$prefix]}${real#"$prefix"}" "/opt/swarm/bin/$tool")
done
IFS=: read -r -a extra_ro <<< "${SWARM_EXTRA_RO:-}"
for p in "${extra_ro[@]}"; do [ -n "$p" ] && args+=(--ro-bind "$p" "$p"); done

# Workspace view. The tickets/ registry is written only by the orchestrator and the dispatcher:
# implementers see it read-only; read-only (reviewer) views hide it, because it holds the
# orchestrator's hints to implementers.
if [ "$rows" = 1 ]; then
  args+=(--ro-bind "$ws" "$ws" --tmpfs "$WT")          # read-only, process files hidden
  [ -d "$TICKETS" ] && args+=(--tmpfs "$TICKETS")
elif [ "$role" = impl ]; then
  args+=(--bind "$ws" "$ws")
  [ -d "$TICKETS" ] && args+=(--ro-bind "$TICKETS" "$TICKETS")
fi
if [ "$role" = review ] && [ -n "$gitdir" ]; then
  args+=(--ro-bind "$gitdir" "$gitdir")
  if [ -f "$cwd/.git" ]; then   # worktree: its metadata dir must be writable for git status/index
    wtmeta=$(sed -n 's/^gitdir: //p' "$cwd/.git")
    [ -n "$wtmeta" ] && args+=(--bind "$wtmeta" "$wtmeta")
  fi
fi
for p in "${ro[@]}"; do args+=(--ro-bind "$p" "$p"); done
for p in "${rw[@]}"; do args+=(--bind "$p" "$p"); done
# The agent's own home and scratch are always writable (mounted last so a read-only view of
# the workspace cannot hide them).
args+=(--bind "$home" "$home" --bind "$scratch" "$scratch")

gname=${SWARM_GIT_NAME:-Local Swarm Agent}; gmail=${SWARM_GIT_EMAIL:-swarm-agent@localhost}
args+=(--chdir "$cwd" --clearenv
  --setenv HOME "$home" --setenv CLAUDE_CONFIG_DIR "$home/.claude" --setenv TMPDIR "$home/tmp"
  --setenv PATH "/opt/swarm/bin:/usr/local/bin:/usr/bin" --setenv LANG C.UTF-8 --setenv TERM dumb
  --setenv LD_LIBRARY_PATH "$ldpath" --setenv SWARM_SCRATCH "$scratch"
  --setenv ANTHROPIC_BASE_URL "http://127.0.0.1:$INNER_LLM_PORT" --setenv ANTHROPIC_AUTH_TOKEN local
  --setenv ANTHROPIC_MODEL "$model" --setenv ANTHROPIC_DEFAULT_OPUS_MODEL "$model"
  --setenv ANTHROPIC_DEFAULT_SONNET_MODEL "$model" --setenv ANTHROPIC_DEFAULT_HAIKU_MODEL "$model"
  --setenv ANTHROPIC_CUSTOM_HEADERS "$headers" --setenv CLAUDE_CODE_EFFORT_LEVEL "$envlevel"
  --setenv CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING 1 --setenv CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS 1
  --setenv CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC 1 --setenv DISABLE_TELEMETRY 1
  --setenv DISABLE_ERROR_REPORTING 1 --setenv DISABLE_AUTOUPDATER 1
  --setenv HTTPS_PROXY "http://127.0.0.1:$PROXY_PORT" --setenv https_proxy "http://127.0.0.1:$PROXY_PORT"
  --setenv NO_PROXY 127.0.0.1,localhost --setenv no_proxy 127.0.0.1,localhost
  --setenv npm_config_https_proxy "http://127.0.0.1:$PROXY_PORT" --setenv npm_config_proxy "http://127.0.0.1:$PROXY_PORT"
  --setenv npm_config_update_notifier false --setenv npm_config_fund false --setenv npm_config_audit false
  --setenv PIP_REQUIRE_VIRTUALENV 1
  --setenv GIT_AUTHOR_NAME "$gname" --setenv GIT_AUTHOR_EMAIL "$gmail"
  --setenv GIT_COMMITTER_NAME "$gname" --setenv GIT_COMMITTER_EMAIL "$gmail")

# ---- gate command mode --------------------------------------------------------------------
if [ -n "$execcmd" ]; then
  elog=$logdir/$name.exec.log
  { echo "\$ $execcmd"; echo "(cwd $cwd, $(date -Is))"; } > "$elog"
  timeout "$tmo" bwrap "${args[@]}" /bin/bash -c '
    py=python3; [ -x /usr/bin/python3 ] && py=/usr/bin/python3
    "$py" /opt/swarm/swarm_fwd.py "$2" "$3" & fwd=$!
    sleep 0.5
    bash -c "$1"; rc=$?; kill $fwd; exit $rc' _ "$execcmd" "$INNER_LLM_PORT" "$PROXY_PORT" >> "$elog" 2>&1
  rc=$?
  echo "(exit $rc)" >> "$elog"
  echo "EXEC $name rc=$rc log=$elog"; exit $rc
fi

# ---- agent mode ---------------------------------------------------------------------------
# Workspace rules are prepended to EVERY prompt so no template can forget them.
preamble="## Workspace rules (apply to every action in this session)
- Workspace: $ws. Put deliverables only where your task says.
- EVERY other file you create — notes, drafts, plans, scratch or test scripts, command output,
  downloaded docs, temporary data, logs — goes under your scratch folder: $scratch
  Never leave stray files anywhere else in the workspace or inside a repository.
- Do not create or change anything else under $WT unless your task names that exact path.
- Never write to $TICKETS (the ticket registry; read-only for you).
- Before you finish: delete temporary files you created outside your scratch folder, and make
  sure build outputs you created are ignored or removed as the project expects.

"
prompt="$preamble$(cat "$pfile")"
RESUME_MSG="Continue with your task. Your last message only announced a next step and the session ended before you did it. Use your tools to do the remaining work, then finish with the report your task asks for."

# One sandboxed Claude Code call. $1 = prompt or continuation message, $2 = session id to resume
# (or ""). The forwarder prefers the system python so the sandbox network does not depend on a
# user-installed toolchain.
run_claude() {
  timeout "$tmo" bwrap "${args[@]}" /bin/bash -c '
    py=python3; [ -x /usr/bin/python3 ] && py=/usr/bin/python3
    "$py" /opt/swarm/swarm_fwd.py "$2" "$3" & fwd=$!
    sleep 1
    if [ -n "$4" ]; then
      claude -p --resume "$4" "$1" --permission-mode acceptEdits --output-format stream-json --verbose
    else
      claude -p "$1" --permission-mode acceptEdits --output-format stream-json --verbose
    fi
    rc=$?; kill $fwd; exit $rc' _ "$1" "$INNER_LLM_PORT" "$PROXY_PORT" "$2"
}

# A session that "succeeds" with a short final message that only announces a next step
# ("Let me read X…") stopped early: the model wrote its intent as text instead of a tool call.
stalled() {
  python3 -c '
import json, re, sys
try:
    r = json.loads(open(sys.argv[1]).read())
except ValueError:
    sys.exit(1)
t = (r.get("result") or "").strip()
intent = re.match(r"(let me|let.s|i.ll|i will|i am going to|next,? (i|let)|now (i|let)|first,? (i|let))\b", t, re.I)
sys.exit(0 if (r.get("subtype") == "success" and len(t) < 600 and intent) else 1)' "$1"
}

rm -f "$logdir/$name.result"
for attempt in $(seq 1 "$retries"); do
  log=$logdir/$name.attempt$attempt.jsonl
  start=$(date +%s)
  run_claude "$prompt" "" > "$log" 2>&1
  rc=$?
  tail -n 1 "$log" > "$logdir/$name.last"
  for resume in 1 2; do
    { [ $rc -eq 0 ] && stalled "$logdir/$name.last"; } || break
    sid=$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).read()).get("session_id",""))' "$logdir/$name.last")
    [ -n "$sid" ] || break
    echo "STALLED $name attempt=$attempt: resuming session (#$resume)" >> "$logdir/$name.failures"
    run_claude "$RESUME_MSG" "$sid" > "$log.resume$resume" 2>&1
    rc=$?
    tail -n 1 "$log.resume$resume" > "$logdir/$name.last"
  done
  # Classify the outcome by category (see swarm_classify.py) and act on it. The category and the
  # exact error go to NAME.status so the dispatcher / orchestrator can surface it.
  IFS=$'\t' read -r cat action msg < <(python3 "$HERE/swarm_classify.py" "$logdir/$name.last" "$rc")
  secs=$(( $(date +%s) - start ))
  printf '%s\t%s\t%s\n' "$cat" "$action" "$msg" > "$logdir/$name.status"
  [ "$action" = ok ] || echo "[$cat] $name attempt=$attempt rc=$rc secs=$secs: $msg" >> "$logdir/$name.failures"
  case "$action" in
    ok)
      python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).read())["result"])' "$logdir/$name.last" > "$logdir/$name.result"
      echo "OK $name attempt=$attempt secs=$secs"; exit 0 ;;
    stop)
      echo "STOPPED $name [$cat]: $msg"
      case "$cat" in api_fatal) exit 4 ;; run_timeout) exit 5 ;; sandbox) exit 3 ;; config) exit 2 ;; *) exit 1 ;; esac ;;
  esac
  # retry (api_transient, degenerate, unknown) or resume that did not help
  if [ "$cat" = unknown ] && [ "${seen_unknown:-0}" = 1 ]; then
    echo "STOPPED $name [unknown, twice]: $msg"; exit 1
  fi
  [ "$cat" = unknown ] && seen_unknown=1
  if [ "$attempt" -lt "$retries" ]; then
    case "$cat" in api_transient) sleep $((attempt * 20)) ;; *) sleep 5 ;; esac
  fi
done
echo "GAVE UP $name after $retries attempts [$cat]: $msg (see $logdir/$name.failures)"; exit 1
