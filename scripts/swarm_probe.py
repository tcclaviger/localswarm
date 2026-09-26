#!/usr/bin/env python3
"""Probe the model route THROUGH a running localswarm bridge and suggest profile values.

Usage: swarm_probe.py <bridge llm.sock path>

Going through the bridge tests exactly what agents will use: the upstream URL and path prefix,
the API format (Anthropic pass-through or OpenAI translation), and the bearer token.
Checks: model list, which effort strings are accepted, whether the think-off switch removes
thinking, one tool call round-trip, and streaming. Prints suggested profile.env lines.
"""
import http.client, json, os, socket, sys

SOCK = sys.argv[1]


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("bridge", timeout=300)
        self.path_ = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(300)
        self.sock.connect(self.path_)


def call(method, path, body=None, headers=None):
    c = UnixConn(SOCK)
    h = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    h.update(headers or {})
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


code, data = call("GET", "/v1/models")
if code != 200:
    sys.exit(f"FAIL: model list via bridge -> HTTP {code}: {data[:300]!r}\n"
             "Check SWARM_LLM_URL (include the path prefix, e.g. /v1 or /api), SWARM_API_FORMAT and the token.")
ids = [m["id"] for m in json.loads(data)["data"]]
model = os.environ.get("SWARM_MODEL") or ids[0]
if model not in ids:
    sys.exit(f"FAIL: SWARM_MODEL={model} is not in the server's model list: {', '.join(ids[:20])}")
print(f"model:  {model}" + ("" if len(ids) == 1 else f"   (server lists {len(ids)}; pin with SWARM_MODEL)"))

base = {"model": model, "max_tokens": 600,
        "messages": [{"role": "user", "content": "What is 17*23? Answer with the number only."}]}


def post(body, headers=None):
    code, data = call("POST", "/v1/messages", body, headers)
    try:
        return code, json.loads(data)
    except ValueError:
        return code, data.decode(errors="replace")[:300]


accepted = []
for eff in ("low", "medium", "high", "xhigh", "max"):
    code, r = post(dict(base, thinking={"type": "adaptive"}, output_config={"effort": eff}))
    print(f"effort {eff:6} -> {'accepted' if code == 200 else f'REJECTED ({code}) {str(r)[:160]}'}")
    if code == 200:
        accepted.append(eff)


def thinking_chars(r):
    return sum(len(c.get("thinking", "")) for c in r.get("content", []) if c.get("type") == "thinking") if isinstance(r, dict) else -1


c_on, on = post(base)
c_off, off = post(base, {"x-swarm-thinking": "off"})
think_ok = c_on == 200 and c_off == 200 and thinking_chars(off) == 0
print(f"thinking: default {thinking_chars(on)} chars, with think-off {thinking_chars(off)} chars"
      + ("" if c_on == 200 and c_off == 200 else f"  (HTTP {c_on}/{c_off})"))

tool_req = dict(base, messages=[{"role": "user", "content": "What is the weather in Paris? Use the tool."}],
                tools=[{"name": "get_weather", "description": "Weather for a city",
                        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}])
code, r = post(tool_req, {"x-swarm-thinking": "off"})
tool_ok = code == 200 and any(b.get("type") == "tool_use" for b in r.get("content", [])) if isinstance(r, dict) else False
print(f"tool call: {'ok' if tool_ok else f'NOT seen (HTTP {code}) {str(r)[:200]}'}")

code, data = call("POST", "/v1/messages", dict(base, stream=True), {"x-swarm-thinking": "off"})
stream_ok = code == 200 and b"message_stop" in data and b"text_delta" in data
print(f"streaming: {'ok' if stream_ok else f'FAILED (HTTP {code}) {data[:200]!r}'}")

pick = lambda *c: next((x for x in c if x in accepted), "")
print("\n# suggested profile.env lines")
print(f'SWARM_EFFORT_MAP="low={pick("low", "medium")} medium={pick("medium", "low")} high={pick("high", "xhigh", "max", "medium")}"')
if not think_ok:
    print("# WARNING: think-off did not remove thinking. Find your model's chat-template switch and set")
    print("#   SWARM_THINK_OFF_KWARGS='{...}'  — until then do not use --effort off.")
if not accepted:
    print("# WARNING: no effort value accepted; set SWARM_EFFORT_MAP to values your server accepts.")
if not (tool_ok and stream_ok):
    print("# WARNING: tool calling or streaming failed; agents need both. Fix before running a swarm.")
