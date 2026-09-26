#!/usr/bin/env python3
"""swarm_board.py — the ticket board for a localswarm run (library + CLI).

State lives in <ws>/tickets/ — durable: it survives delivery and `pack`, so the user can always
find what was planned, gated, fixed and escalated. Only the orchestrator (this CLI) and the
dispatcher write it; agents see it read-only (implementers) or not at all (reviewers).
  board.json    the board: goal, settings, tickets (single source of truth; file-locked)
  events.jsonl  append-only history of every change
  BOARD.md      human-readable rendering, rewritten on every change
  plan.json     the planner's plan (input to `import`)

Ticket fields
  id, title, type, effort, deps[], task, done_when[], deliverables[], spec_refs[], gates[],
  status, attempts, max_attempts, parent, hints[], reports[], result, commit_before,
  commit_after, notes[]
Types (see TYPES): the role and default template each type runs with.
Statuses: todo -> running -> gating -> (review) -> done
          changes (a fix ticket is open) | awaiting_human | blocked (needs the orchestrator) |
          failed_run (agent run failed; retried by the dispatcher) | dropped
Gates (checked by the dispatcher after a successful run, in order):
  {"kind": "command", "cmd": "npm test", "cwd": "<path relative to ws>", "timeout": 900}
  {"kind": "review", "effort": "medium", "focus": "what the reviewer must look at hardest"}
  {"kind": "human", "question": "what the human must confirm"}

CLI (all commands take --ws DIR):
  init      --goal TEXT [--repo REL] [--spec REL] [--impl-slots N] [--review-slots N] [--until ISO]
  import    FILE            add tickets from a plan JSON ({"tickets": [...]}) after validation
  add       --json JSON     add one ticket
  list      [--status S]    one line per ticket
  show      ID
  set       ID --status S [--note TEXT]
  hint      ID (--text TEXT | --file PATH)   attach orchestrator guidance (plain words, no code)
  approve   ID [--note TEXT]   pass a human gate
  reject    ID --note TEXT     fail a human gate (opens a fix ticket with the note)
  unblock   ID [--attempts N]  return a blocked ticket to todo (optionally raise max_attempts)
  render                     rewrite BOARD.md
  validate                   check the whole board
  pack      (--archive | --delete)   final clean-up of workingtemp after delivery (host side)
"""
import argparse, contextlib, datetime, fcntl, json, os, shutil, subprocess, sys, tarfile

TYPES = {
    # type        role      template            default effort
    "spec":      ("impl",   "work.md",          "high"),
    "implement": ("impl",   "implementer.md",   "medium"),
    "fix":       ("impl",   "implementer.md",   "medium"),
    "research":  ("impl",   "work.md",          "medium"),
    "write":     ("impl",   "work.md",          "medium"),
    "test":      ("impl",   "test.md",          "off"),
    "deliver":   ("impl",   "deliver.md",       "medium"),
    "review":    ("review", "reviewer.md",      "medium"),
    "audit":     ("review", "auditor.md",       "high"),
    "qa":        ("review", "blackbox.md",      "medium"),
    "factcheck": ("review", "factcheck.md",     "off"),
}
EFFORTS = ("off", "low", "medium", "high")
STATUSES = ("todo", "running", "gating", "review", "changes", "awaiting_human", "blocked",
            "failed_run", "done", "dropped")
GATE_KINDS = ("command", "review", "human")


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def wt(ws):
    return os.path.join(os.path.abspath(ws), "workingtemp")


def bdir(ws):
    """The durable ticket registry: <ws>/tickets (not inside workingtemp)."""
    return os.path.join(os.path.abspath(ws), "tickets")


@contextlib.contextmanager
def locked(ws):
    """Exclusive lock on the board for a read-modify-write cycle."""
    os.makedirs(bdir(ws), exist_ok=True)
    with open(os.path.join(bdir(ws), ".lock"), "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load(ws):
    p = os.path.join(bdir(ws), "board.json")
    if not os.path.exists(p):
        raise SystemExit(f"no board at {p} (run: swarm_board.py init --ws {ws} --goal ...)")
    with open(p) as f:
        return json.load(f)


def save(ws, board, event=None):
    p = os.path.join(bdir(ws), "board.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(board, f, indent=2)
    os.replace(tmp, p)
    if event:
        event = dict(event, at=now())
        with open(os.path.join(bdir(ws), "events.jsonl"), "a") as f:
            f.write(json.dumps(event) + "\n")
    render(ws, board)


def new_id(board, prefix="T"):
    board["seq"] = board.get("seq", 0) + 1
    return f"{prefix}-{board['seq']:03d}"


def normalize(t, board):
    """Fill defaults and validate one ticket dict. Returns (ticket, errors)."""
    errs = []
    t = dict(t)
    if not t.get("id"):
        t["id"] = new_id(board)
    typ = t.get("type", "implement")
    if typ not in TYPES:
        errs.append(f"{t['id']}: unknown type {typ!r} (one of {', '.join(TYPES)})")
        typ = "implement"
    t["type"] = typ
    t.setdefault("title", t.get("task", "")[:80] or t["id"])
    t.setdefault("effort", TYPES[typ][2])
    if t["effort"] not in EFFORTS:
        errs.append(f"{t['id']}: effort {t['effort']!r} not in {EFFORTS}")
    for k in ("deps", "done_when", "deliverables", "spec_refs", "gates", "hints", "reports", "notes"):
        v = t.get(k) or []
        t[k] = [v] if isinstance(v, str) else list(v)
    if not t.get("task"):
        errs.append(f"{t['id']}: missing 'task' (what to do, in plain words)")
    if t["gates"] and (TYPES[typ][0] == "review" or typ == "test") and not t.get("parent"):
        errs.append(f"{t['id']}: a {typ} ticket is itself a check — it cannot have gates (its report's "
                    f"Verdict is the check; a failing verdict reopens the ticket it depends on)")
    for g in t["gates"]:
        if not isinstance(g, dict) or g.get("kind") not in GATE_KINDS:
            errs.append(f"{t['id']}: bad gate {g!r} (kind must be one of {GATE_KINDS})")
        elif g["kind"] == "command" and not g.get("cmd"):
            errs.append(f"{t['id']}: command gate without 'cmd'")
        elif g["kind"] == "human" and not g.get("question"):
            errs.append(f"{t['id']}: human gate without 'question'")
    t.setdefault("status", "todo")
    if t["status"] not in STATUSES:
        errs.append(f"{t['id']}: bad status {t['status']!r}")
    t.setdefault("attempts", 0)
    t.setdefault("max_attempts", board.get("max_attempts", 3))
    t.setdefault("parent", None)
    t.setdefault("result", None)
    return t, errs


def check_graph(tickets):
    errs = []
    ids = set(tickets)
    for t in tickets.values():
        for d in t["deps"]:
            if d not in ids:
                errs.append(f"{t['id']}: depends on unknown ticket {d}")
    # cycle check (DFS)
    state = {}

    def visit(i, path):
        if state.get(i) == 1:
            errs.append("dependency cycle: " + " -> ".join(path + [i]))
            return
        if state.get(i) == 2:
            return
        state[i] = 1
        for d in tickets[i]["deps"]:
            if d in tickets:
                visit(d, path + [i])
        state[i] = 2

    for i in tickets:
        visit(i, [])
    return errs


def ready(board):
    """Tickets whose deps are all done and that are waiting to run."""
    tk = board["tickets"]
    out = []
    for t in tk.values():
        if t["status"] in ("todo", "failed_run") and all(tk[d]["status"] in ("done", "dropped") for d in t["deps"] if d in tk):
            out.append(t)
    order = list(tk)
    return sorted(out, key=lambda t: (t["type"] != "fix", order.index(t["id"])))   # fixes jump the queue


def render(ws, board):
    tk = board["tickets"]
    counts = {s: sum(1 for t in tk.values() if t["status"] == s) for s in STATUSES}
    lines = [f"# Board — {board.get('goal', '')}", "",
             f"Updated {now()} · " + " · ".join(f"{s}: {n}" for s, n in counts.items() if n), ""]
    if board.get("attention"):
        lines += ["## Needs the orchestrator", ""] + [f"- {a}" for a in board["attention"]] + [""]
    lines += ["| ID | Type | Status | Effort | Deps | Title | Tries |", "|---|---|---|---|---|---|---|"]
    for t in tk.values():
        lines.append(f"| {t['id']} | {t['type']} | {t['status']} | {t['effort']} | {', '.join(t['deps'])} | "
                     f"{t['title'].replace('|', '/')} | {t['attempts']}/{t['max_attempts']} |")
    with open(os.path.join(bdir(ws), "BOARD.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------
def cmd_init(a):
    os.makedirs(bdir(a.ws), exist_ok=True)
    p = os.path.join(bdir(a.ws), "board.json")
    if os.path.exists(p):
        raise SystemExit(f"board already exists: {p}")
    board = {"goal": a.goal, "ws": os.path.abspath(a.ws), "repo": a.repo, "spec": a.spec,
             "slots": {"impl": a.impl_slots, "review": a.review_slots}, "until": a.until,
             "max_attempts": 3, "created": now(), "seq": 0, "tickets": {}, "attention": []}
    with locked(a.ws):
        save(a.ws, board, {"op": "init", "goal": a.goal})
    print(f"board created: {p}")


def _add_many(ws, raw_tickets, source):
    with locked(ws):
        board = load(ws)
        new, errs = {}, []
        for rt in raw_tickets:
            t, e = normalize(rt, board)
            errs += e
            if t["id"] in board["tickets"] or t["id"] in new:
                errs.append(f"{t['id']}: duplicate id")
            new[t["id"]] = t
            n = int(t["id"].split("-")[-1]) if t["id"].split("-")[-1].isdigit() else 0
            board["seq"] = max(board.get("seq", 0), n)
        errs += check_graph({**board["tickets"], **new})
        if errs:
            raise SystemExit("plan rejected:\n  " + "\n  ".join(errs))
        board["tickets"].update(new)
        save(ws, board, {"op": "add", "source": source, "ids": list(new)})
    print(f"added {len(new)} ticket(s): {', '.join(new)}")


def cmd_import(a):
    if not os.path.isfile(a.file):
        raise SystemExit(f"import: no plan file at {a.file} (run swarm_plan.sh first)")
    try:
        with open(a.file) as f:
            plan = json.load(f)
    except ValueError as e:
        raise SystemExit(f"import: {a.file} is not valid JSON: {e}")
    _add_many(a.ws, plan["tickets"] if isinstance(plan, dict) else plan, a.file)


def cmd_add(a):
    _add_many(a.ws, [json.loads(a.json)], "cli")


def cmd_list(a):
    b = load(a.ws)
    for t in b["tickets"].values():
        if not a.status or t["status"] == a.status:
            print(f"{t['id']:8} {t['type']:9} {t['status']:14} {t['effort']:6} {t['title']}")
    if b.get("attention"):
        print("\nattention:\n  " + "\n  ".join(b["attention"]))


def cmd_show(a):
    print(json.dumps(load(a.ws)["tickets"][a.id], indent=2))


def _mutate(ws, tid, fn, event):
    with locked(ws):
        b = load(ws)
        if tid not in b["tickets"]:
            raise SystemExit(f"no ticket {tid}")
        fn(b, b["tickets"][tid])
        b["attention"] = [x for x in b.get("attention", []) if not x.startswith(tid + ":")]
        save(ws, b, dict(event, id=tid))


def cmd_set(a):
    if a.status not in STATUSES:
        raise SystemExit(f"status must be one of {STATUSES}")

    def fn(b, t):
        t["status"] = a.status
        if a.note:
            t["notes"].append(f"{now()} orchestrator: {a.note}")
    _mutate(a.ws, a.id, fn, {"op": "set", "status": a.status, "note": a.note})


def cmd_hint(a):
    text = a.text if a.text else open(a.file).read()
    _mutate(a.ws, a.id, lambda b, t: t["hints"].append(text), {"op": "hint"})


def cmd_approve(a):
    def fn(b, t):
        if t["status"] != "awaiting_human":
            raise SystemExit(f"{t['id']} is {t['status']}, not awaiting_human")
        t["status"] = "done"
        t["notes"].append(f"{now()} human approved: {a.note or ''}")
    _mutate(a.ws, a.id, fn, {"op": "approve", "note": a.note})


def cmd_reject(a):
    def fn(b, t):
        if t["status"] != "awaiting_human":
            raise SystemExit(f"{t['id']} is {t['status']}, not awaiting_human")
        fx, _ = normalize({"id": new_id(b), "type": "fix", "parent": t["id"], "effort": t["effort"],
                           "title": f"Fix {t['id']}: human review", "deps": [],
                           "task": f"A human reviewed the result of {t['id']} ({t['title']}) and rejected it. "
                                   f"Their note: {a.note}\nMake the changes they ask for.",
                           "done_when": t["done_when"], "gates": []}, b)
        b["tickets"][fx["id"]] = fx
        t["status"] = "changes"
        t["notes"].append(f"{now()} human rejected: {a.note} -> {fx['id']}")
    _mutate(a.ws, a.id, fn, {"op": "reject", "note": a.note})


def cmd_unblock(a):
    def fn(b, t):
        t["status"] = "todo"
        if a.attempts:
            t["max_attempts"] = max(t["max_attempts"], a.attempts)
    _mutate(a.ws, a.id, fn, {"op": "unblock"})


def cmd_render(a):
    render(a.ws, load(a.ws))
    print(os.path.join(bdir(a.ws), "BOARD.md"))


def cmd_validate(a):
    b = load(a.ws)
    errs = []
    for t in b["tickets"].values():
        errs += normalize(t, b)[1]
    errs += check_graph(b["tickets"])
    print("board ok" if not errs else "problems:\n  " + "\n  ".join(errs))
    sys.exit(1 if errs else 0)


def cmd_pack(a):
    """Host-side final clean-up. Removes review worktrees, then archives or deletes workingtemp.
    The tickets/ registry is never touched."""
    w = wt(a.ws)
    b = load(a.ws)
    open_t = [t["id"] for t in b["tickets"].values() if t["status"] not in ("done", "dropped")]
    if not b["tickets"] and not a.force:
        raise SystemExit("refusing: the board has no tickets — nothing was delivered (use --force to pack anyway)")
    if open_t and not a.force:
        raise SystemExit(f"refusing: tickets not done: {', '.join(open_t)} (use --force to pack anyway)")
    repo = os.path.join(os.path.abspath(a.ws), b["repo"]) if b.get("repo") else None
    snap = os.path.join(w, "snap")
    if repo and os.path.isdir(os.path.join(repo, ".git")) and os.path.isdir(snap):
        for d in sorted(os.listdir(snap)):
            subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", os.path.join(snap, d)], check=False)
        subprocess.run(["git", "-C", repo, "worktree", "prune"], check=False)
    if a.archive:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = os.path.join(os.path.abspath(a.ws), f"workingtemp-{stamp}.tar.gz")
        with tarfile.open(out, "w:gz") as tf:
            tf.add(w, arcname="workingtemp")
        print(f"archived to {out}")
    shutil.rmtree(w)
    print(f"removed {w}; ticket registry kept at {bdir(a.ws)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def p(name, fn):
        sp = sub.add_parser(name)
        sp.add_argument("--ws", required=True)
        sp.set_defaults(fn=fn)
        return sp

    sp = p("init", cmd_init)
    sp.add_argument("--goal", required=True)
    sp.add_argument("--repo")
    sp.add_argument("--spec")
    sp.add_argument("--impl-slots", type=int, default=1)
    sp.add_argument("--review-slots", type=int, default=1)
    sp.add_argument("--until")
    p("import", cmd_import).add_argument("file")
    p("add", cmd_add).add_argument("--json", required=True)
    p("list", cmd_list).add_argument("--status")
    p("show", cmd_show).add_argument("id")
    sp = p("set", cmd_set)
    sp.add_argument("id")
    sp.add_argument("--status", required=True)
    sp.add_argument("--note")
    sp = p("hint", cmd_hint)
    sp.add_argument("id")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--file")
    sp = p("approve", cmd_approve)
    sp.add_argument("id")
    sp.add_argument("--note")
    sp = p("reject", cmd_reject)
    sp.add_argument("id")
    sp.add_argument("--note", required=True)
    sp = p("unblock", cmd_unblock)
    sp.add_argument("id")
    sp.add_argument("--attempts", type=int)
    p("render", cmd_render)
    p("validate", cmd_validate)
    sp = p("pack", cmd_pack)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--archive", action="store_true")
    g.add_argument("--delete", action="store_true")
    sp.add_argument("--force", action="store_true")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
