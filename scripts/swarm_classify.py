#!/usr/bin/env python3
"""Classify one finished agent run.

Usage: swarm_classify.py <last-line-of-stream-log> <exit-code-of-run>
Prints one line: "<category>\t<action>\t<message>"   (action: ok | retry | resume | stop)

Categories
  ok            success with a real result
  stalled       success, but the final message only announces a next step  -> resume the session
  degenerate    final text contains raw chat-template tokens                -> retry
  api_transient timeouts, connection errors, 408/429/502/503/504/529        -> retry with backoff
  api_fatal     500 and other 5xx, 400, 401, 403, 404, 413, 422             -> stop and surface
                (Claude Code has already retried 5xx/429/timeouts internally before giving up,
                so a 500 that reaches us is not worth another run)
  run_timeout   our own wall-clock limit (exit 124)                          -> stop and surface
  sandbox       bwrap / bridge / model-server unavailable                    -> stop and surface
  config        bad CLI option or argument                                   -> stop and surface
  unknown       anything else                                                -> retry once, then stop
"""
import json, re, sys

path, rc = sys.argv[1], int(sys.argv[2])
raw = open(path, errors="replace").read().strip() if path else ""
try:
    r = json.loads(raw) if raw.startswith("{") else {}
except ValueError:
    r = {}
text = (r.get("result") or raw or "").strip()
status = r.get("api_error_status")
short = " ".join(text.split())[:300]

TRANSIENT_HTTP = {408, 429, 502, 503, 504, 529}
FATAL_HTTP = {400, 401, 403, 404, 413, 422}
TRANSIENT_TEXT = re.compile(r"timed? ?out|timeout|ETIMEDOUT|ECONNRESET|ECONNREFUSED|socket hang up|"
                            r"connection (error|reset|closed)|overloaded|temporarily unavailable|"
                            r"API Error: (408|429|502|503|504|529)\b", re.I)
FATAL_TEXT = re.compile(r"API Error: (4\d\d|5\d\d)\b", re.I)


def out(cat, action, msg=short):
    print(f"{cat}\t{action}\t{msg}")
    sys.exit(0)


if rc == 124:
    out("run_timeout", "stop", "the agent hit the wall-clock limit (--timeout); split the task or raise the limit")
if re.search(r"error: (option|unknown option|missing required)", raw, re.I):
    out("config", "stop")
if re.search(r"^bwrap: |bridge failed|model server not answering|no model list", raw, re.I | re.M):
    out("sandbox", "stop")
if re.search(r"<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>|<\|eot_id\|>", text):
    out("degenerate", "retry", "final text contains raw chat-template tokens (model output collapsed)")
if r.get("is_error") or r.get("terminal_reason") == "api_error" or status:
    code = status if isinstance(status, int) else None
    if code is None:
        m = re.search(r"API Error: (\d{3})", text)
        code = int(m.group(1)) if m else None
    if code in TRANSIENT_HTTP or (code is None and TRANSIENT_TEXT.search(text)):
        out("api_transient", "retry")
    if code in FATAL_HTTP or (code is not None and 500 <= code < 600) or FATAL_TEXT.search(text):
        out("api_fatal", "stop")
    out("unknown", "retry")
if rc == 0 and r.get("subtype") == "success":
    intent = re.match(r"(let me|let.s|i.ll|i will|i am going to|next,? (i|let)|now (i|let)|first,? (i|let))\b", text, re.I)
    if intent and len(text) < 600:
        out("stalled", "resume")
    out("ok", "ok", "")
if TRANSIENT_TEXT.search(raw):
    out("api_transient", "retry")
out("unknown", "retry")
