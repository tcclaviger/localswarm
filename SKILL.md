---
name: localswarm
description: Opus/Fable-driven swarm of Claude Code agents running on YOUR model endpoint (local vLLM/SGLang or a remote gateway / Open WebUI, Anthropic or OpenAI format, optional bearer token kept outside the sandbox). Any task, run as a ticket board - planner designs a workflow that fits the task, tickets carry their own gates (commands, independent review, human sign-off), a dispatcher keeps the slots full and opens fix tickets on failure, and a final packing/delivery ticket cleans up. Every agent is bwrap-sandboxed to one folder, and all process files live in <project>/workingtemp. Linux only. Invoke with /localswarm.
---

# /localswarm — you orchestrate, local agents work, tickets keep order

**Roles.** You (Opus/Fable, the parent session) are the **orchestrator**: intake, plan approval,
triage, plain-language hints, budget, final hand-off. Local agents do **all** the work, each one a
fresh sandboxed Claude Code session on the user's endpoint. Do not write deliverables yourself
unless the user says so.

`$SKILL` = this skill's folder. Scripts in `$SKILL/scripts/`, templates in `$SKILL/templates/`.
Machine settings: `${LOCALSWARM_PROFILE:-~/.config/localswarm/profile.env}` (see
`profile.env.example`). Never edit machine specifics into scripts.

## 0. Intake — MANDATORY, before anything else

Take what the invocation args already say, then ask for everything missing in ONE AskUserQuestion
round. Never guess the folder.

| # | Ask | Default if user says "default" |
|---|---|---|
| 1 | **Workspace folder** — the ONLY folder agents may see (created if missing) | none — must be given |
| 2 | **The task** and what "done" looks like | none — must be given |
| 3 | **Time budget** — fixed duration (keep improving until it ends) or stop when done | stop when done |
| 4 | **Repo** — existing git repo inside the workspace, new repo, or no repo (non-code job) | decided by the planner |
| 5 | **Gates** — review every ticket, milestones only, and which steps need the user's sign-off | review every ticket; user signs off the plan and the delivery |
| 6 | **Network** — web docs + project-local packages? extra domains to block? | on; profile's block list |
| 7 | **Hard rules** — never do/touch; standards/versions to follow | none extra |

Write the answers to `<ws>/workingtemp/intake.md`, confirm back in 3–5 lines, and suggest the user
runs `/add-dir <ws>` so your own reads there do not prompt.

## 0b. Preflight (first run on a machine or endpoint)

```bash
$SKILL/scripts/swarm_check.sh     # tools, namespaces, endpoint via a temp bridge (format, auth,
                                  # effort names, thinking switch, tool calls, streaming)
```
Show differing suggested profile lines to the user before writing them. Tokens go only in the
`.env` file (mode 600) — never in prompts, hints, tickets, profiles or replies. For a new
workspace, run the smoke test once (`templates/smoke.md`, see §8).

## 1. Project layout — tickets/ (durable) and workingtemp/ (temporary)

```
<ws>/tickets/                 the TICKET REGISTRY — durable, stays after delivery and pack
  board.json                  tickets + status (single source of truth; written only by swarm_board.py / the dispatcher)
  BOARD.md                    readable view, rewritten on every change — point the user here
  events.jsonl                full history of every change
  plan.json                   the planner's plan
<ws>/workingtemp/             every temporary process file — archived or deleted at the end
  intake.md  brief.md         owner's answers, orchestrator brief    reviews/   review / audit / qa / fact-check reports
  notes/NOTES.md              agents' running notes                  reports/   test reports
  prompts/                    every filled prompt                    snap/      reviewer snapshots
  logs/                       agent stream logs, results, dispatch.log
  homes/<agent>/              per-agent HOME + config                scratch/<agent>/  each agent's scratch folder
  hints/                      orchestrator hints
```
Agents see `tickets/` read-only (implementers) or not at all (reviewers — it holds your hints).
In a git workspace both folders are added to `.git/info/exclude`; commit `tickets/` deliberately
if the user wants the history in the repository.
`swarm_run.sh` prepends **Workspace rules** to EVERY agent prompt: all non-deliverable files go in
that agent's scratch folder, never loose in the project. If the workspace is a git repo,
`workingtemp/` is added to `.git/info/exclude`. Reviewers see the work through a snapshot with
`workingtemp` hidden, so they never read the implementer's notes. Your own orchestrator files
(briefs, hints) also go under `workingtemp/`.

## 2. The flow

1. **Brief (optional).** For large or fuzzy jobs, write a plain-language brief to
   `workingtemp/brief.md`, or plan a `spec` ticket first and let an agent write the spec.
   Check current standards/versions yourself (WebSearch/WebFetch) and tell the planner in words
   to verify and follow them — local models write from stale memory.
2. **Plan.** `$SKILL/scripts/swarm_plan.sh --ws <ws> --intake <ws>/workingtemp/intake.md [--repo REL] [--spec REL]`
   The planner designs a workflow that fits the task (ticket types, dependencies, per-ticket gates,
   efforts, a final `deliver` ticket), the plan is validated, and a fresh agent reviews it cold.
   Re-plan with `--round 2 --hints FILE` until the review passes and the plan fits.
3. **Plan gate.** Show the user the workflow summary and ticket list (from `tickets/plan.json`);
   get approval (unless intake said no). Then:
   `swarm_board.py init --ws <ws> --goal "…" [--repo REL] [--spec REL] [--impl-slots 1] [--review-slots 1] [--until ISO]`
   `swarm_board.py import --ws <ws> <ws>/tickets/plan.json`
   Tell the user they can follow progress in `<ws>/tickets/BOARD.md`.
4. **Dispatch.** `$SKILL/scripts/swarm_dispatch.py --ws <ws>` in the background. It starts ready
   tickets while slots are free, snapshots work for reviewers, runs gates, opens review and fix
   tickets, and exits when it needs you. You are woken by its exit:

   | Exit | Meaning | You do |
   |---|---|---|
   | 0 | all tickets done | go to 6 |
   | 10 | `board.attention` lists what needs you | triage (5), then restart the dispatcher |
   | 11 | deadline reached | report state to the user; plan follow-ups |

5. **Triage** (read `tickets/BOARD.md` and the files named in `attention`):
   - *blocked* after max attempts → read the review/gate log, add a hint
     (`swarm_board.py hint ID --file …`), then `unblock ID [--attempts N]`; or split it into new
     tickets (`add --json …`) and `set ID --status dropped`.
   - *report ready for triage* (audit / qa / fact-check) → turn real findings into tickets
     (`add --json` with type `fix`/`implement` and gates); ignore noise; note decisions.
   - *human gate* → ask the user; `approve ID` or `reject ID --note "…"` (reject opens a fix).
   - *stalled* → a dependency is blocked or dropped; fix the graph.
   - Budget left and board done early? Add improvement tickets (audit, qa, coverage, docs,
     hardening) — never leave slots idle while budget remains.
6. **Deliver.** The planned `deliver` ticket cleans cruft outside `workingtemp`, runs final checks,
   and writes `<ws>/DELIVERY.md`; its human gate asks the user to accept. After acceptance, ask the
   user: archive or delete `workingtemp`? Then `swarm_board.py pack --ws <ws> --archive|--delete`.
   `tickets/` always stays.

## 3. Ticket types and gates

| Type | Role | Template | Default effort | Typical gates |
|---|---|---|---|---|
| spec | impl | work.md | high | review |
| implement / fix | impl | implementer.md (git) / work.md | medium | command (build, tests) + review |
| research / write | impl | work.md | medium | review or factcheck |
| test | impl | test.md | off | — (it *is* a check; report has a Verdict) |
| deliver | impl | deliver.md | medium | command (final checks) + human |
| review | review | reviewer.md | medium | created by review gates |
| audit / qa | review | auditor.md / blackbox.md | high / medium | report → triage |
| factcheck | review | factcheck.md | off | Verdict |

Gates: `{"kind":"command","cmd":"npm test","cwd":"repo","timeout":900}` runs in the sandbox
(`swarm_run.sh --exec`); `{"kind":"review","effort":"medium","focus":"…"}` opens a review ticket on a
snapshot; `{"kind":"human","question":"…"}` waits for approve/reject. A failed gate opens a fix
ticket carrying the evidence (fixes jump the queue); a finished fix re-runs the parent's gates;
after `max_attempts` (3) the parent is blocked and escalated to you.

## 4. Effort

| `effort` | Use for | Mechanism |
|---|---|---|
| `off` | tests, fact checks, file moves, lookups | thinking disabled (bridge merges `SWARM_THINK_OFF_KWARGS`) |
| `low` | trivial edits | `SWARM_EFFORT_MAP[low]` via `CLAUDE_CODE_EFFORT_LEVEL` |
| `medium` | normal work, small reviews | `SWARM_EFFORT_MAP[medium]` |
| `high` | specs, plans, hard bugs, audits | `SWARM_EFFORT_MAP[high]` (e.g. `xhigh` on some servers) |
Temperature is a server default (Claude Code does not send it).

## 5. The fence (enforced by `swarm_run.sh` + the bridge)

| Rule | How |
|---|---|
| Sees only the workspace | bwrap; `/usr` + toolchains read-only at neutral paths; no `/home`, `/mnt`, host `/tmp` |
| Reviewers see only a snapshot | role `review`: snapshot + reviews dir + spec; `workingtemp` hidden |
| No host processes / services | own PID/IPC/UTS/net namespaces; no `/run` (docker, systemd) |
| No global installs, push, sudo | `/usr` read-only + settings deny rules |
| Network | bridge only: model route (`POST /v1/messages*`, `GET /v1/models`) and HTTPS to public hosts; private IPs and `SWARM_BLOCK_SUFFIXES` refused |
| Endpoint token | read and sent only by the host bridge |
| Sub-agents | only `second-opinion` (fresh context, Read-only) |

Stop a bridge only with `kill "$(cat "$SWARM_SOCK_DIR/bridge.pid")"` — never `pkill -f`/`pgrep -f`
(they match your own shell). The runner restarts a dead bridge by itself.

## 6. Hints — steer without doing the work

What is wrong, why it matters, where to look, what "done" means. No code, no diffs. Name the
*pattern* when the same bug class recurs ("third time: empty env values treated as set — fix it in
one place"). Attach with `swarm_board.py hint ID --file workingtemp/hints/ID.md`.

## 7. Monitoring

`tickets/BOARD.md` (status), `workingtemp/logs/dispatch.log` (what ran, gate results),
`$SWARM_SOCK_DIR/bridge.log` (network), `grep -o '"name":"[A-Za-z]*"' workingtemp/logs/<agent>.attempt1.jsonl | tail`
(last tool calls). Wait for the dispatcher's exit; do not poll in a tight loop.

## 8. Manual mode and smoke test

Single agents without a board: `swarm_run.sh --ws <ws> --name X --effort medium --prompt FILE`
(`--role review --ro-ws` for a read-only view). Smoke test for a new workspace:
`cp $SKILL/templates/smoke.md <ws>/workingtemp/smoke.md && swarm_run.sh --ws <ws> --name smoke --effort off --prompt <ws>/workingtemp/smoke.md --retries 1`
— every "expect" in its report must hold.

## 9. Gotchas

- Editing a bash script while it runs corrupts the running copy. Change scripts between runs.
- `WebSearch` does not exist on local endpoints; give agents known URLs / `llms.txt` indexes.
- Agents in `-p` mode cannot ask permission; widen `templates/settings-*.json` deliberately.
- Unit tests can all pass while nothing is wired together. Plan `qa` tickets that drive the built
  thing from the outside at milestones.
- Models repeat bug classes; when a reviewer finds the same kind of bug twice, the hint must say so.
- Toolchains outside `/usr` mount automatically; other SDKs via `SWARM_EXTRA_RO` / `SWARM_EXTRA_TOOLS`.
- Python projects: global pip is denied; agents use a venv inside the workspace.
