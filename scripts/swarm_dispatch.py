#!/usr/bin/env python3
"""swarm_dispatch.py — work the ticket board until the orchestrator is needed.

  swarm_dispatch.py --ws DIR [--until ISO] [--poll 5]

Loop:
  * start every READY ticket (all deps done) while role slots are free
    (board.slots.impl writers, board.slots.review reviewers; fix tickets jump the queue);
  * each ticket runs as one sandboxed agent via swarm_run.sh with the template for its type;
  * after a successful run its GATES are checked in order:
      command  -> run inside the sandbox (swarm_run.sh --exec); failure opens a FIX ticket
                  carrying the output, the ticket goes to "changes"
      review   -> a REVIEW ticket is opened on a snapshot of the work (git worktree of the new
                  commits, or a copy of the workspace without workingtemp); the reviewer never
                  sees the implementer's notes. PASS -> next gate; CHANGES_REQUIRED -> FIX ticket
      human    -> "awaiting_human" + an attention entry; `swarm_board.py approve|reject`
    When a fix ticket finishes, its parent's gates run again. After max_attempts the parent is
    "blocked" and needs the orchestrator. Audit / QA / fact-check reports are flagged for triage.
  * exits when nothing is running and nothing can start:
      0  every ticket done or dropped
      10 the orchestrator is needed (board.attention lists why: blocked, human gate, reports)
      11 --until / board.until deadline reached (running tickets were allowed to finish)
      2  setup error
The orchestrator (an Opus/Fable Claude Code session) runs this in the background, gets woken by
its exit, acts on board.attention (hints, new tickets, approvals), and starts it again.
"""
import argparse, datetime, json, os, re, shutil, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import swarm_board as sb  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--ws", required=True)
ap.add_argument("--until")
ap.add_argument("--poll", type=float, default=5)
A = ap.parse_args()
WS = os.path.abspath(A.ws)
WT = sb.wt(WS)
for d in ("prompts", "reviews", "snap", "notes", "reports", "logs"):
    os.makedirs(os.path.join(WT, d), exist_ok=True)
LOG = os.path.join(WT, "logs", "dispatch.log")
running = {}            # ticket id -> thread
rlock = threading.Lock()


def log(msg):
    line = f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def attention(board, tid, msg):
    entry = f"{tid}: {msg}"
    if entry not in board.setdefault("attention", []):
        board["attention"].append(entry)


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def repo_path(board):
    if not board.get("repo"):
        return None
    p = os.path.join(WS, board["repo"])
    return p if os.path.isdir(os.path.join(p, ".git")) else None


def abs_ws(rel):
    return os.path.join(WS, rel) if rel and not os.path.isabs(rel) else rel


def task_text(t):
    parts = [t["task"].strip()]
    if t["done_when"]:
        parts.append("Done when:\n" + "\n".join(f"- {x}" for x in t["done_when"]))
    if t["deliverables"]:
        parts.append("Deliverables (the only files you should leave outside your scratch folder):\n"
                     + "\n".join(f"- {x}" for x in t["deliverables"]))
    if t["spec_refs"]:
        parts.append("Relevant spec sections: " + ", ".join(t["spec_refs"]))
    return "\n\n".join(parts)


def hints_text(board, t):
    h = list(t["hints"])
    if t.get("parent") and t["type"] == "fix":
        p = board["tickets"].get(t["parent"], {})
        h.insert(0, f"This is a fix for ticket {p.get('id')} ({p.get('title')}). "
                    f"Its original task was:\n{p.get('task', '')}")
    return ("Guidance from the orchestrator:\n" + "\n".join(f"- {x}" for x in h)) if h else ""


def fill(template, out, values):
    args = ["python3", os.path.join(HERE, "fill_template.py"), os.path.join(SKILL, "templates", template), out]
    args += [f"{k}={v}" for k, v in values.items()]
    subprocess.run(args, check=True, capture_output=True)


def snapshot(board, t, name):
    """Give a reviewer an isolated copy of the work. Returns (path, extra swarm_run args, range)."""
    repo = repo_path(board)
    dest = os.path.join(WT, "snap", name)
    parent = board["tickets"].get(t.get("parent") or "", {})
    if repo:
        head = git(repo, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", dest], capture_output=True)
        subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", dest, head], capture_output=True, check=True)
        base = parent.get("commit_before") or ""
        rng = f"{base}..{head}" if base and base != head else f"{head} (review the files; no new commits were recorded)"
        return dest, ["--git-dir", os.path.join(repo, ".git")], rng
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(WS, dest, symlinks=True,
                    ignore=shutil.ignore_patterns("workingtemp", "tickets", ".git", "node_modules", ".venv", "__pycache__"))
    return dest, [], "(not a git project: review the files in the snapshot)"


def build_run(board, t):
    """Prepare prompt + swarm_run arguments for one ticket attempt."""
    role, template, _ = sb.TYPES[t["type"]]
    repo = repo_path(board)
    if t["type"] in ("implement", "fix") and not repo:
        template = "work.md"
    template = t.get("template") or template
    name = f"{t['id']}-a{t['attempts'] + 1}"
    spec = abs_ws(board.get("spec")) or "(no separate specification; the task text is the spec)"
    vals = {"WS": WS, "REPO": repo or "(none — this project has no git repository)", "SPEC": spec,
            "NOTES": os.path.join(WT, "notes", "NOTES.md"), "ID": t["id"], "TITLE": t["title"],
            "TASK": task_text(t), "HINTS": hints_text(board, t), "GOAL": board.get("goal", ""),
            "BOARD": os.path.join(sb.bdir(WS), "BOARD.md"),
            "REPORT_FILE": os.path.join(WT, "reports", f"{t['id']}.md"),
            "FOCUS": t.get("focus", ""), "INTERFACE": t.get("interface") or board.get("interface", "its public interface")}
    run = [os.path.join(HERE, "swarm_run.sh"), "--ws", WS, "--name", name, "--effort", t["effort"],
           "--role", role, "--timeout", str(t.get("timeout", 3600))]
    if role == "review":
        snap, extra, rng = snapshot(board, t, name)
        report = os.path.join(WT, "reviews", f"{t['id']}.md")
        vals.update({"WORKTREE": snap, "RANGE": rng, "REVIEW_FILE": report})
        run += ["--cwd", snap, "--rw", snap, "--rw", os.path.join(WT, "reviews")] + extra
        if board.get("spec") and os.path.exists(abs_ws(board["spec"])):
            run += ["--ro", abs_ws(board["spec"])]
        t["report"] = report
    else:
        run += ["--cwd", repo or WS]
        if t["type"] == "deliver":
            t["report"] = os.path.join(WS, "DELIVERY.md")
            vals["REPORT_FILE"] = t["report"]
        elif t["type"] == "test":
            t["report"] = vals["REPORT_FILE"]
    pfile = os.path.join(WT, "prompts", f"{name}.md")
    fill(template, pfile, vals)
    return name, run + ["--prompt", pfile]


def new_ticket(board, **kw):
    t, errs = sb.normalize(dict(kw, id=sb.new_id(board)), board)
    if errs:
        log("internal ticket errors: " + "; ".join(errs))
    board["tickets"][t["id"]] = t
    return t


def open_fix(board, parent, why, details_path=None):
    """Open a fix ticket for `parent`. Returns the fix id, or None if parent is now blocked."""
    parent["attempts"] += 1
    if parent["attempts"] >= parent["max_attempts"]:
        parent["status"] = "blocked"
        attention(board, parent["id"], f"blocked after {parent['attempts']} attempts; last problem: {why}"
                  + (f" ({details_path})" if details_path else ""))
        log(f"{parent['id']} blocked")
        return None
    task = (f"Ticket {parent['id']} ({parent['title']}) did not pass its checks: {why}.\n"
            + (f"Read the full details at {details_path} and address every blocking point.\n" if details_path else "")
            + "Make the smallest change that fixes it, keep the rest of the work intact, and re-run the "
              "checks yourself before you finish.")
    fx = new_ticket(board, type="fix", parent=parent["id"], effort=parent["effort"] if parent["effort"] != "off" else "low",
                    title=f"Fix {parent['id']}: {why[:60]}", task=task, done_when=parent["done_when"],
                    deliverables=parent["deliverables"], spec_refs=parent["spec_refs"], deps=[])
    parent["status"] = "changes"
    log(f"{parent['id']} -> changes, opened {fx['id']}")
    return fx["id"]


def gate_loop(tid, start=0):
    """Run ticket tid's gates from index `start`. Takes the board lock only for short updates,
    never while a gate command runs. Ends when the ticket is done, waiting, or has a fix open."""
    i = start
    while True:
        with sb.locked(WS):
            board = sb.load(WS)
            t = board["tickets"][tid]
            gates = t["gates"]
            if i >= len(gates):
                t["status"] = "done"
                t.pop("gate_index", None)
                log(f"{tid} done")
                follow = None
                if t["type"] == "fix" and t.get("parent") in board["tickets"]:
                    p = board["tickets"][t["parent"]]           # a finished fix re-opens its parent's gates
                    p["status"] = "gating"
                    if repo_path(board):
                        p["commit_after"] = git(repo_path(board), "rev-parse", "HEAD")
                    follow = p["id"]
                sb.save(WS, board, {"op": "done", "id": tid})
                break
            g = gates[i]
            t["gate_index"] = i
            if g["kind"] == "review":
                rv = new_ticket(board, type="review", parent=tid, effort=g.get("effort", "medium"),
                                title=f"Review {tid}: {t['title'][:60]}", focus=g.get("focus", ""),
                                task=f"Review the work done for ticket {tid} ({t['title']}).\n\nThe ticket asked for:\n"
                                     + task_text(t), deps=[])
                t["status"] = "review"
                sb.save(WS, board, {"op": "review_opened", "id": tid, "review": rv["id"]})
                log(f"{tid} -> review ({rv['id']})")
                return
            if g["kind"] == "human":
                t["status"] = "awaiting_human"
                attention(board, tid, f"human gate: {g['question']}")
                sb.save(WS, board, {"op": "awaiting_human", "id": tid})
                log(f"{tid} -> awaiting_human")
                return
            name = f"{tid}-g{i}-a{t['attempts'] + 1}"
            cwd = abs_ws(g.get("cwd")) or repo_path(board) or WS
            t["status"] = "gating"
            sb.save(WS, board, {"op": "gate", "id": tid, "gate": g})
        r = subprocess.run([os.path.join(HERE, "swarm_run.sh"), "--ws", WS, "--name", name, "--exec", g["cmd"],
                            "--cwd", cwd, "--timeout", str(g.get("timeout", 900))], capture_output=True, text=True)
        logp = os.path.join(WT, "logs", f"{name}.exec.log")
        log(f"{tid} gate `{g['cmd']}` rc={r.returncode}")
        if r.returncode != 0:
            with sb.locked(WS):
                board = sb.load(WS)
                open_fix(board, board["tickets"][tid], f"command gate `{g['cmd']}` failed (exit {r.returncode})", logp)
                sb.save(WS, board, {"op": "gate_failed", "id": tid})
            return
        i += 1
    if follow:
        gate_loop(follow, 0)


VERDICT = re.compile(r"^\W*verdict\W*:?\W*(pass|changes[_ ]required)", re.I | re.M)


def after_review(board, t):
    """Record a finished check ticket (review / audit / qa / factcheck / test). Returns a parent id
    whose next gates must run (the caller runs them after releasing the lock), else None.
      gate review (has parent)  : PASS -> parent's next gate; CHANGES -> fix for parent
      standalone test/factcheck : checks its `target` (default: its last dependency); CHANGES ->
                                  fix for the target, and this check re-queues behind the fix
      audit / qa                : report goes to the orchestrator for triage"""
    parent = board["tickets"].get(t.get("parent") or "")
    report = t.get("report")
    text = open(report).read() if report and os.path.exists(report) else ""
    m = VERDICT.search(text)
    if t["type"] in ("audit", "qa"):
        t["status"] = "done"
        attention(board, t["id"], f"report ready for triage: {report}")
        return None
    if not m:
        t["attempts"] += 1
        t["status"] = "failed_run" if t["attempts"] < t["max_attempts"] else "blocked"
        if t["status"] == "blocked":
            attention(board, t["id"], f"check produced no Verdict line ({report})")
        return None
    passed = m.group(1).lower() == "pass"
    if not parent:
        target = board["tickets"].get(t.get("target") or (t["deps"][-1] if t["deps"] else ""))
        if passed:
            t["status"] = "done"
            log(f"{t['id']} check PASS")
        elif not target:
            t["status"] = "done"
            attention(board, t["id"], f"check failed but names no target ticket: {report}")
        else:
            fx = open_fix(board, target, f"check {t['id']} ({t['type']}) failed", report)
            if fx:
                t["status"] = "todo"           # re-check after the fix
                t["deps"] = list(dict.fromkeys(t["deps"] + [fx]))
                t["attempts"] += 1
            else:
                t["status"] = "blocked"
                attention(board, t["id"], f"its target {target['id']} is blocked")
        return None
    t["status"] = "done"
    parent["reports"].append(report)
    if m.group(1).lower() == "pass":
        log(f"{parent['id']} review PASS")
        parent["status"] = "gating"
        return parent["id"]
    open_fix(board, parent, "the independent review asked for changes", report)
    return None


def work(tid):
    with sb.locked(WS):
        board = sb.load(WS)
        t = board["tickets"][tid]
        repo = repo_path(board)
        if repo and t["type"] in ("implement", "fix", "spec", "write", "research", "deliver"):
            t["commit_before"] = git(repo, "rev-parse", "HEAD")
        try:
            name, cmd = build_run(board, t)
        except Exception as e:  # noqa: BLE001 - any setup failure blocks this ticket only
            t["status"] = "blocked"
            attention(board, tid, f"could not prepare run: {e}")
            sb.save(WS, board, {"op": "prepare_failed", "id": tid, "error": str(e)})
            return
        t["status"] = "running"
        sb.save(WS, board, {"op": "start", "id": tid, "agent": name})
    log(f"{tid} start {name} ({t['type']}, effort {t['effort']})")
    r = subprocess.run(cmd, capture_output=True, text=True)
    log(f"{tid} {r.stdout.strip() or r.stderr.strip()[:200]}")
    with sb.locked(WS):
        board = sb.load(WS)
        t = board["tickets"][tid]
        t["result"] = os.path.join(WT, "logs", f"{name}.result")
        if r.returncode != 0:
            t["attempts"] += 1
            # swarm_run.sh wrote the failure category and exact error to NAME.status
            sp = os.path.join(WT, "logs", name + ".status")
            cat, _, msg = (open(sp).read().strip().split("\t") + ["", "", ""])[:3] if os.path.exists(sp) else ("", "", "")
            t["last_failure"] = f"{cat or 'exit ' + str(r.returncode)}: {msg}".strip()
            # 2 config, 3 sandbox/bridge, 4 api_fatal, 5 run_timeout: stop and surface, never retry
            if r.returncode in (2, 3, 4, 5) or t["attempts"] >= t["max_attempts"]:
                t["status"] = "blocked"
                hint = {"run_timeout": " — split the ticket or raise its timeout",
                        "api_fatal": " — check the model endpoint, token and model id",
                        "sandbox": " — check bwrap / the bridge (swarm_check.sh)"}.get(cat, "")
                attention(board, tid, f"agent run stopped [{t['last_failure']}]{hint}; "
                                      f"details: {os.path.join(WT, 'logs', name + '.failures')}")
            else:
                t["status"] = "failed_run"
            sb.save(WS, board, {"op": "run_failed", "id": tid, "rc": r.returncode})
            return
        next_gates = None
        if sb.TYPES[t["type"]][0] == "review" or t["type"] == "test":
            nxt = after_review(board, t)
            if nxt:
                next_gates = (nxt, board["tickets"][nxt].get("gate_index", 0) + 1)
        else:
            if repo_path(board):
                t["commit_after"] = git(repo_path(board), "rev-parse", "HEAD")
            t["status"] = "gating"
            next_gates = (tid, 0)
        sb.save(WS, board, {"op": "finish", "id": tid, "status": t["status"]})
    if next_gates:
        gate_loop(*next_gates)


def main():
    board = sb.load(WS)
    if not board["tickets"]:
        log("the board has no tickets — import a plan first (swarm_board.py import)")
        return 2
    until = A.until or board.get("until")
    deadline = datetime.datetime.fromisoformat(until) if until else None
    log(f"dispatcher up: {len(board['tickets'])} tickets, slots {board['slots']}, until {until or '-'}")
    while True:
        with rlock:
            for tid in [k for k, th in running.items() if not th.is_alive()]:
                running.pop(tid)
        board = sb.load(WS)
        past = deadline and datetime.datetime.now().astimezone() >= deadline
        if not past:
            busy = {"impl": 0, "review": 0}
            for tid in running:
                busy[sb.TYPES[board["tickets"][tid]["type"]][0]] += 1
            for t in sb.ready(board):
                role = sb.TYPES[t["type"]][0]
                if t["id"] in running or busy[role] >= board["slots"].get(role, 1):
                    continue
                th = threading.Thread(target=work, args=(t["id"],), daemon=True)
                with rlock:
                    running[t["id"]] = th
                busy[role] += 1
                th.start()
        if not running:
            board = sb.load(WS)
            if past:
                log("deadline reached")
                return 11
            if not sb.ready(board):
                open_t = [t for t in board["tickets"].values() if t["status"] not in ("done", "dropped")]
                if not open_t:
                    log("all tickets done")
                    return 0
                if not board.get("attention"):
                    attention(board, "board", "no ticket can start: " + ", ".join(f"{t['id']}={t['status']}" for t in open_t))
                    with sb.locked(WS):
                        b2 = sb.load(WS)
                        b2["attention"] = board["attention"]
                        sb.save(WS, b2, {"op": "stalled"})
                log("orchestrator needed: " + " | ".join(board["attention"]))
                return 10
        time.sleep(A.poll)


if __name__ == "__main__":
    sys.exit(main())
