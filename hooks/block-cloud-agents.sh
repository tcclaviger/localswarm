#!/usr/bin/env bash
# OPTIONAL PreToolUse hook (matcher "Agent") for the PARENT Claude Code session.
# Refuses subagents that would run on a cloud model, and tells Claude to use the local swarm
# instead. Install by adding to ~/.claude/settings.json:
#   "hooks": { "PreToolUse": [ { "matcher": "Agent",
#       "hooks": [ { "type": "command", "command": "~/.claude/hooks/block-cloud-agents.sh" } ] } ] }
# Exit 2 = block the tool call and hand stderr back to Claude. Requires jq.
payload=$(cat)
tool=$(printf '%s' "$payload" | jq -r '.tool_name // "?"')
[ "$tool" = "Agent" ] || exit 0
model=$(printf '%s' "$payload" | jq -r '.tool_input.model // ""' | tr '[:upper:]' '[:lower:]')
agent=$(printf '%s' "$payload" | jq -r '.tool_input.subagent_type // "general-purpose"')
case "$model" in
  opus*|sonnet*|haiku*|fable*|claude-*)
    cat >&2 <<EOF
BLOCKED: Agent model '$model' is not allowed here. Agent work runs on the local model server.
One-off (unsandboxed):  claude-local "<task prompt>" --agent $agent --effort medium
Fenced / multi-agent:   use the /localswarm skill (scripts/swarm_run.sh).
EOF
    exit 2 ;;
esac
exit 0
