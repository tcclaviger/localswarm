# localswarm — sandboxed agent swarms for Claude Code on your own models

A Claude Code skill plus a few scripts that let one Claude Code session (the **orchestrator**,
e.g. Opus or Fable) drive many Claude Code agents running on **your** model endpoint — a local
server (vLLM, SGLang) or a remote gateway (your own API, Open WebUI, ...), with or without a
bearer token — on any task. Work runs as a **ticket board**: a planner agent designs a workflow
that fits the task, each ticket carries its own gates (commands, an independent review, human
sign-off), a dispatcher keeps the slots full and opens fix tickets when a gate fails, and a final
delivery ticket cleans up and hands over. Every agent runs in a `bwrap` sandbox locked to one
folder.

In the project folder you will find:
- `tickets/` — the durable ticket registry (`BOARD.md` is the readable view). It stays after delivery.
- `workingtemp/` — every temporary process file (prompts, logs, reviews, scratch). Archived or
  deleted at the end.
- `DELIVERY.md` — what was delivered, how to use it, final check results, known gaps.

Status: a working starting point extracted from a real run. Expect to tailor it (see SETUP.md).

## What you get

| Path | What it is |
|---|---|
| `SKILL.md` | The `/localswarm` skill: intake questions, fence, effort, pipeline, hints, monitoring |
| `SETUP.md` | A prompt for an LLM agent to install and tailor all of this on a new machine |
| `profile.env.example` | Every machine-specific setting (endpoint, format, token file, effort names, ...) |
| `scripts/swarm_plan.sh` | Planner agent → validated `tickets/plan.json` → independent plan review |
| `scripts/swarm_board.py` | Ticket registry CLI: init, import, add, list, show, set, hint, approve/reject, unblock, pack |
| `scripts/swarm_dispatch.py` | Works the board: starts ready tickets, runs gates, opens review/fix tickets, escalates |
| `scripts/swarm_run.sh` | Run one sandboxed agent (roles `impl` and `review`) or one gate command (`--exec`) |
| `scripts/swarm_bridge.py` | Host bridge: model route (auth, path prefix, OpenAI↔Anthropic translation, route filter) + public-HTTPS-only proxy |
| `scripts/swarm_fwd.py` | In-sandbox forwarder from local TCP ports to the bridge sockets |
| `scripts/swarm_worktree.sh` | Snapshot a commit into a git worktree for an isolated reviewer |
| `scripts/fill_template.py` | Fill `{{KEY}}` placeholders in prompt templates |
| `scripts/swarm_check.sh`, `swarm_probe.py` | Preflight: tools, namespaces, and a probe of the endpoint through a temporary bridge (format, token, effort names, thinking switch, tool calls, streaming) |
| `scripts/claude-local` | Unsandboxed one-off wrapper: Claude Code → your endpoint (direct or via the bridge) |
| `templates/` | Prompts per ticket type (planner, plan-review, implementer, work, test, reviewer, auditor, blackbox, factcheck, deliver, smoke), sandbox settings, `second-opinion` sub-agent |
| `hooks/block-cloud-agents.sh` | Optional parent hook: refuse cloud-model subagents, point to the swarm |

## Requirements

- Linux with unprivileged user namespaces and `bwrap` (bubblewrap).
- Claude Code CLI (`claude`) — tested with 2.1.280. It must honour `ANTHROPIC_BASE_URL`,
  `CLAUDE_CODE_EFFORT_LEVEL`, `ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CONFIG_DIR`.
- Python 3.9+, git, curl. Node/npm for JavaScript projects.
- A model endpoint with tool calling that speaks either
  - the **Anthropic Messages API** (`<base>/messages`, `<base>/models`) — passed straight through, or
  - the **OpenAI chat-completions API** (`<base>/chat/completions`, `<base>/models`) — the bridge
    translates requests and responses, including streaming, tool calls and reasoning.

## Quick start

```bash
cp -r localswarm-portable ~/.claude/skills/localswarm
mkdir -p ~/.config/localswarm && cp ~/.claude/skills/localswarm/profile.env.example ~/.config/localswarm/profile.env
$EDITOR ~/.config/localswarm/profile.env              # at least SWARM_LLM_URL (with its /v1 or /api prefix)
# only if the endpoint needs a token:
( umask 077; echo 'SWARM_API_TOKEN=<your token>' > ~/.config/localswarm/.env )
~/.claude/skills/localswarm/scripts/swarm_check.sh    # paste its suggested lines into the profile
```
Then in Claude Code: `/localswarm <folder> <what to build>`.

Or hand this folder to an agent with: "Read SETUP.md and follow it."

## Endpoints and tokens

| Endpoint | `SWARM_LLM_URL` | `SWARM_API_FORMAT` | Token |
|---|---|---|---|
| Local vLLM / SGLang | `http://127.0.0.1:8000/v1` | `auto` (→ anthropic) | usually none |
| Remote Anthropic-compatible gateway | `https://llm.example.com/v1` | `auto` / `anthropic` | `.env` |
| OpenAI-compatible gateway | `https://llm.example.com/v1` | `openai` | `.env` |
| Open WebUI | `https://webui.example.com/api` | `openai` | `.env` (an Open WebUI API key) |

The token is read from `SWARM_ENV_FILE` (default `~/.config/localswarm/.env`), variable
`SWARM_TOKEN_VAR` (default `SWARM_API_TOKEN`), **only by the host bridge**, which sends it as
`Authorization: Bearer …`. It never enters an agent sandbox, never appears in prompts or agent
logs, and is never written to `bridge.log`. `auto` probes once when the bridge starts; set the
format explicitly if you know it. After changing the endpoint or token, restart the bridge:
`kill "$(cat "$SWARM_SOCK_DIR/bridge.pid")"` — the next run starts a fresh one.

## Safety model, briefly

Agents see only the workspace folder, cannot see or signal host processes, have no host network
(only the model route and public HTTPS through the bridge, with private addresses refused), cannot
install globally, push, sudo or use docker, and never hold the endpoint token. This is defence in
depth for a cooperative model, not a hardened jail for hostile code: the kernel attack surface of
user namespaces applies, and public HTTPS means an agent can send data to the internet. Tighten
`swarm_bridge.py` to an allow-list if that matters to you. Anyone who can reach the bridge's
optional TCP port on the host can use your endpoint with your token — leave
`SWARM_BRIDGE_TCP_PORT` unset on shared machines.
