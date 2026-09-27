# SETUP — instructions for an AI agent

You are an AI coding agent (for example Claude Code) running on a user's machine. The user gave
you this folder. Your job: install `localswarm` on this machine, wire Claude Code to the user's
model endpoint (a local server or a remote gateway, possibly behind a bearer token), prove the
sandbox works, and tailor the skill to this deployment.
Read README.md and SKILL.md in this folder first.

Ground rules for you:
- Ask the user before you install system packages, change `~/.claude/settings.json` or hooks,
  overwrite any existing file, or change a server's configuration. Show what you will change.
- Never put machine-specific values into the scripts. They go in the profile file.
- Never put a token in the profile, a script, a prompt, a commit or your replies. Tokens live only
  in the `.env` file (mode 600). Do not print it back when you confirm it works.
- Keep probe traffic small when the endpoint is shared or metered.
- Report every check with its real output. If something fails, say so; do not paper over it.

## Part A — what the "Claude Code wrapper" is and why each piece exists

Claude Code normally talks to Anthropic's API. It can instead talk to any server that speaks the
Anthropic Messages API (`POST <base>/messages`), such as vLLM's Anthropic-compatible endpoint.
For servers that only speak the OpenAI chat-completions API (Open WebUI, many gateways), the
localswarm **bridge** sits in between and translates. The wrapper itself is only environment
variables around `claude -p`:

| Variable | Why |
|---|---|
| `ANTHROPIC_BASE_URL=http://<server>` | Send requests to your endpoint (or to the bridge) instead of the cloud. Claude Code appends `/v1/messages` itself, so a direct base URL must be the part before `/v1`. |
| `ANTHROPIC_AUTH_TOKEN=<token or "local">` | Sent as `Authorization: Bearer …`. Direct mode: the endpoint's token (read from `.env` by `claude-local`). Via the bridge or a token-less server: any placeholder — the bridge drops it and adds the real token itself. |
| `ANTHROPIC_MODEL=<id>` | The served model id (read it from `GET /v1/models`). |
| `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL=<id>` | Claude Code uses other model tiers internally (sub-agents, summaries, WebFetch processing); point them all at the local model or those calls go to names the server does not know. |
| `CLAUDE_CODE_EFFORT_LEVEL=<string>` | Reasoning effort, passed through verbatim. Servers accept different names (one real server accepted `low|medium|xhigh` and returned HTTP 400 for `high` and `max`). The CLI `--effort` flag only accepts the CLI's own names, and with no effort set Claude Code sends its own default — which can be one the server rejects. So always set this variable to a value the server accepts. |
| `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1`, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` | Avoid request features a local server may not implement. |
| `ANTHROPIC_CUSTOM_HEADERS="X-Swarm-Thinking: off"` | Used only for effort `off`: the localswarm bridge sees this header and merges the model's "no thinking" chat-template switch (e.g. `{"enable_thinking": false}` for Qwen-style templates) into the request body. Claude Code cannot send that field itself, and Anthropic-style `thinking: {type: "disabled"}` is often ignored by local servers. |
| `CLAUDE_CONFIG_DIR=<dir>` | Inside the sandbox each agent gets its own config dir, so the user's own hooks, skills, memory and CLAUDE.md do not leak in. |
| `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_AUTOUPDATER` | No calls home from a sandbox that has no route home anyway. |

Facts to know:
- Temperature/top-p cannot be set per agent: Claude Code does not send them. They come from the
  server's defaults (vLLM: `--override-generation-config`).
- `WebSearch` is a server-side Anthropic tool; it does not exist on local servers. `WebFetch`
  works (Claude Code fetches the page itself, then asks the model — hence the HAIKU mapping).
- `scripts/claude-local` is the unsandboxed version of this wrapper for quick one-offs; it uses the
  user's normal `~/.claude`. `scripts/swarm_run.sh` is the sandboxed version for real work.
- The bridge (`scripts/swarm_bridge.py`) owns everything endpoint-specific: base URL and path
  prefix, API format (`anthropic` pass-through or `openai` translation incl. streaming, tool calls,
  reasoning, and folding Claude Code's mid-conversation system messages, which OpenAI chat
  templates often reject), the bearer token, the route filter, retries on connection failures,
  and the think-off kwargs. Agents only ever talk to the bridge.

## Part A2 — endpoint and token facts

- `SWARM_LLM_URL` is the API base **with** its prefix: `http://127.0.0.1:8000/v1`,
  `https://llm.example.com/v1`, `https://webui.example.com/api`. Find the right prefix by asking
  the user, then confirm with a GET on `<base>/models` (send the token if one is needed). An HTML
  page or 404 means the prefix is wrong.
- `SWARM_API_FORMAT=auto` makes the bridge probe `<base>/messages` once at start-up with a real
  model id; 404/405/HTML there means OpenAI format. Set it explicitly when you know.
- Token: ask the user for it, write it yourself only into
  `${XDG_CONFIG_HOME:-~/.config}/localswarm/.env` as `SWARM_API_TOKEN=...` with mode 600 (or the
  file/variable named by `SWARM_ENV_FILE` / `SWARM_TOKEN_VAR`). Only the bridge (and
  `claude-local` in direct mode) reads it. After changing it, restart the bridge
  (`kill "$(cat <sock-dir>/bridge.pid)"`).
- If the endpoint lists many models, pin `SWARM_MODEL`.
- `claude-local` direct mode needs an Anthropic-format endpoint whose base ends in `/v1`. For
  anything else (OpenAI format, `/api` prefixes, `--effort off`) set `SWARM_BRIDGE_TCP_PORT` so
  it goes through the bridge. Do not set that port on shared machines: anyone on the host could
  use the endpoint with the user's token.

## Part B — install

1. **Inspect the machine** and report: OS and kernel; `bwrap --version`; whether unprivileged user
   namespaces work (`scripts/swarm_check.sh` tests this; on Ubuntu 24.04+ an AppArmor rule may
   block them — explain the options to the user, do not change kernel settings yourself);
   `claude --version`; python3, git, curl, node/npm locations (note any outside `/usr`).
2. **Find the model endpoint.** Ask the user for its URL (local or remote) and whether it needs a
   token. Store the token as described in Part A2. Confirm `GET <base>/models` answers with a
   model list and note the model id(s). The endpoint must support tool calling and streaming,
   in either Anthropic or OpenAI format.
3. **Install files** (after the user agrees):
   - copy this folder to `~/.claude/skills/localswarm/` (keep the layout; make scripts executable);
   - copy `scripts/claude-local` to a directory on the user's PATH (e.g. `~/.local/bin/`);
   - create `${XDG_CONFIG_HOME:-~/.config}/localswarm/profile.env` from `profile.env.example`.
4. **Probe and fill the profile.** Set `SWARM_LLM_URL` (and `SWARM_API_FORMAT`, `SWARM_MODEL` if
   known) in the profile, then run `scripts/swarm_check.sh`. It starts a temporary bridge and
   reports the detected format, whether auth works, which effort strings the endpoint accepts,
   whether the think-off switch removes thinking, and whether tool calls and streaming work
   through the bridge. Write the suggested `SWARM_EFFORT_MAP` into the profile. If think-off did not work, find the
   model family's switch in its chat template (`tokenizer_config.json` / `chat_template.jinja`,
   look for a variable that gates the thinking block) and set `SWARM_THINK_OFF_KWARGS`; re-run.
   Ask the user which of their own domains to put in `SWARM_BLOCK_SUFFIXES`.
5. **Optional parent hook.** Offer `hooks/block-cloud-agents.sh` (needs `jq`): it stops the parent
   session from spawning cloud-model sub-agents and points it at the local swarm. Install only if
   the user wants it; merge into their existing `settings.json` hooks, never replace them.

## Part C — prove it

1. Create a scratch workspace (ask where; e.g. `~/localswarm-test`).
2. Run the smoke test exactly as in SKILL.md section 0b. Every line marked "expect" must hold:
   no `/home` or `/run` visible; admin route to the model server denied by the bridge; loopback
   port refused; npm registry reachable; global npm and pip denied; `ps` shows only sandbox
   processes; WebFetch works; `second-opinion` answers; `general-purpose` is denied.
3. **Python venv check** (the smoke test does not cover it):
   `scripts/swarm_run.sh --ws <ws> --name pycheck --exec 'python3 -m venv <ws>/venvtest && <ws>/venvtest/bin/pip install -q six && <ws>/venvtest/bin/python -c "import six"'`
   It must exit 0. If it fails with `No module named 'encodings'`, python3 is a relocated build
   outside `/usr` (pyenv, asdf, uv-managed, ...): the sandbox mounts it at `/opt/swarm/tc/N`, but a
   venv made from it looks for the stdlib at the build's compiled-in prefix, which is not mounted.
   Fix it in the profile: add the host's `python3 -c 'import sys; print(sys.base_prefix)'` to
   `SWARM_EXTRA_RO` (mounted read-only). Re-run the check, then delete `<ws>/venvtest`.
4. Run one effort `high` and one effort `off` agent with a trivial prompt; confirm both succeed
   and that the `off` run's stream log has no thinking blocks.
5. Show the user `bridge.log` from the smoke run (its "bridge up" line shows upstream, format and
   `auth=yes/no`, never the token).
6. If a token is in use: search the scratch workspace and the bridge's socket folder for the
   token string (do not print the token itself; report only the count). The count must be 0.

## Part D — tailor

Discuss with the user and adjust, in the profile or templates, never in the fence logic unless
they ask:
- **Languages/stacks.** Settings allow/deny lists in `templates/settings-*.json` (e.g. allow
  `Bash(.venv/bin/pip *)` for Python projects that use an in-workspace venv; add build tools).
- **Toolchains outside /usr** (SDKs, JDKs, Android SDK): `SWARM_EXTRA_RO` and `SWARM_EXTRA_TOOLS`.
  A pyenv/asdf python also needs its `sys.base_prefix` in `SWARM_EXTRA_RO`, or venvs break (Part C3).
- **Network policy.** Keep public-HTTPS-only, or tighten `swarm_bridge.py` to an allow-list
  (package registry + chosen docs sites) if data leaving the machine is a concern.
- **Effort defaults** in the SKILL.md table if their model behaves differently.
- **Commit identity** (`SWARM_GIT_NAME`, `SWARM_GIT_EMAIL`).
- **Model id**: leave `SWARM_MODEL` empty to auto-detect, or pin it if the server lists several.

Finish with a short report: what was installed where, the profile values, the smoke-test table,
and anything that did not work with the exact error.
