#!/usr/bin/env python3
"""Host-side bridge for sandboxed local agents. Runs OUTSIDE the sandbox.

Creates two unix sockets in --sock-dir (bind-mounted into every sandbox at /sock):

  llm.sock  Model route. Accepts only POST /v1/messages* and GET /v1/models from agents
            (Anthropic Messages API, which is what Claude Code speaks) and forwards them to
            --llm-url, a base URL that may carry a path prefix:
                http://127.0.0.1:8000/v1      local vLLM / SGLang
                https://example.com/v1        remote gateway
                https://example.com/api       Open WebUI
            --api-format anthropic : upstream speaks Anthropic Messages -> passed through
                                     (<base>/messages, <base>/models).
            --api-format openai    : upstream speaks OpenAI chat completions -> requests and
                                     responses (incl. streaming and tool calls) are translated
                                     (<base>/chat/completions, <base>/models).
            --api-format auto      : probe once at start-up and pick one.
            Auth: if a token is found (--env-file, variable --token-var) the bridge sends
            `Authorization: Bearer <token>` upstream. The token never enters the sandbox and is
            never logged. The agent's own placeholder credentials are dropped.
            A request with header `X-Swarm-Thinking: off` gets --think-off-kwargs merged into
            chat_template_kwargs (the switch chat templates such as Qwen's honor).

  net.sock  HTTP CONNECT proxy for public hosts on port 443 only (package registries, docs).
            Loopback, private, link-local and reserved addresses are refused after DNS
            resolution, as are hosts matching --block-suffix.

Usage: swarm_bridge.py --sock-dir DIR [--llm-url URL] [--api-format auto|anthropic|openai]
                       [--env-file FILE] [--token-var NAME] [--block-suffix example.org]...
                       [--think-off-kwargs JSON] [--tcp-port N]
Log: DIR/bridge.log (every allow/deny/upstream error; never request bodies or tokens).
PID: DIR/bridge.pid  (stop with: kill "$(cat DIR/bridge.pid)")
"""
import argparse, asyncio, http.client, http.server, ipaddress, json, os, socket, socketserver
import ssl, threading, time, uuid
from urllib.parse import urlparse

ap = argparse.ArgumentParser()
ap.add_argument("--sock-dir", required=True)
ap.add_argument("--llm-url", default=os.environ.get("SWARM_LLM_URL", "http://127.0.0.1:8000/v1"))
ap.add_argument("--api-format", default=os.environ.get("SWARM_API_FORMAT", "auto"), choices=("auto", "anthropic", "openai"))
ap.add_argument("--env-file", default=os.environ.get("SWARM_ENV_FILE", os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "localswarm", ".env")))
ap.add_argument("--token-var", default=os.environ.get("SWARM_TOKEN_VAR", "SWARM_API_TOKEN"))
ap.add_argument("--block-suffix", action="append", default=[])
ap.add_argument("--think-off-kwargs", default=os.environ.get("SWARM_THINK_OFF_KWARGS") or '{"enable_thinking": false}')
ap.add_argument("--upstream-timeout", type=float, default=float(os.environ.get("SWARM_UPSTREAM_TIMEOUT", "3600")))
ap.add_argument("--tcp-port", type=int, default=0,
                help="also expose the filtered LLM route on 127.0.0.1:PORT for host-side clients")
A = ap.parse_args()

LOG = os.path.join(A.sock_dir, "bridge.log")
_log_lock = threading.Lock()


def log(msg):
    with _log_lock, open(LOG, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


# ---------------------------------------------------------------------------------------------
# Upstream configuration
# ---------------------------------------------------------------------------------------------
U = urlparse(A.llm_url)
UP_SCHEME, UP_HOST = U.scheme or "http", U.hostname or "127.0.0.1"
UP_PORT = U.port or (443 if UP_SCHEME == "https" else 80)
UP_BASE = U.path.rstrip("/") or "/v1"          # bare host:port means the usual /v1 prefix
THINK_OFF = json.loads(A.think_off_kwargs)
BLOCK = tuple(s.lstrip(".").lower() for s in ["localhost", "local", "lan", "internal", "home.arpa"] + A.block_suffix)


def read_token():
    """Token from the process env first, then the .env file. Never logged."""
    if os.environ.get(A.token_var):
        return os.environ[A.token_var]
    try:
        with open(A.env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[len("export "):].strip()
                if k == A.token_var:
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                        v = v[1:-1]
                    return v or None
    except FileNotFoundError:
        return None
    return None


TOKEN = read_token()


def upstream_conn():
    if UP_SCHEME == "https":
        return http.client.HTTPSConnection(UP_HOST, UP_PORT, timeout=A.upstream_timeout, context=ssl.create_default_context())
    return http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=A.upstream_timeout)


def upstream_headers(extra=None):
    h = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if TOKEN:
        h["authorization"] = f"Bearer {TOKEN}"
    if extra:
        h.update(extra)
    return h


def upstream_request(method, path, body=None, headers=None, retries=2):
    """Open an upstream request; retry connection failures (not HTTP errors) with backoff."""
    last = None
    for attempt in range(retries + 1):
        conn = upstream_conn()
        try:
            conn.request(method, path, body=body, headers=upstream_headers(headers))
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            last = e
            conn.close()
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise ConnectionError(f"upstream {UP_HOST}:{UP_PORT} unreachable: {last}")


def detect_format():
    if A.api_format != "auto":
        return A.api_format
    # Use a real model id: servers answer 404 for an unknown model, which would look like a
    # missing route.
    model = "probe"
    try:
        conn, r = upstream_request("GET", UP_BASE + "/models", retries=0)
        if r.status == 200:
            model = (json.loads(r.read()).get("data") or [{}])[0].get("id", model)
        else:
            r.read()
        conn.close()
    except (ConnectionError, ValueError) as e:
        log(f"format probe: model list failed ({e})")
    probe = json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]})
    try:
        conn, r = upstream_request("POST", UP_BASE + "/messages", probe, retries=0)
        ctype = r.getheader("content-type", "")
        r.read()
        conn.close()
        if r.status in (404, 405) or "text/html" in ctype:
            return "openai"
        return "anthropic"
    except ConnectionError as e:
        log(f"format probe failed ({e}); assuming openai")
        return "openai"


# ---------------------------------------------------------------------------------------------
# Anthropic Messages  <->  OpenAI chat completions
# ---------------------------------------------------------------------------------------------
STOP_MAP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
            "function_call": "tool_use", "content_filter": "refusal"}


def _text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if b.get("type") == "text":
            out.append(b.get("text", ""))
        elif b.get("type") == "tool_result":
            out.append(_text_of(b.get("content", "")))
    return "\n".join(out)


def to_openai(req):
    msgs = []
    sysv = req.get("system")
    if sysv:
        msgs.append({"role": "system", "content": _text_of(sysv) if not isinstance(sysv, str) else sysv})
    for m in req.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if role == "system":
            # Claude Code sends system messages mid-conversation (hooks, reminders). Many OpenAI
            # chat templates only allow one system message, first. Fold leading ones into it;
            # later ones become a <system-reminder> on the user side.
            text = content if isinstance(content, str) else _text_of(content)
            if all(x["role"] == "system" for x in msgs):
                if msgs:
                    msgs[0]["content"] += "\n\n" + text
                else:
                    msgs.append({"role": "system", "content": text})
                continue
            note = f"<system-reminder>\n{text}\n</system-reminder>"
            if msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], str):
                msgs[-1]["content"] += "\n\n" + note
            else:
                msgs.append({"role": "user", "content": note})
            continue
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                     for b in content if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
            continue
        # user: tool results become role=tool messages, which must directly follow the assistant
        parts = []
        for b in content:
            t = b.get("type")
            if t == "tool_result":
                txt = _text_of(b.get("content", ""))
                if b.get("is_error"):
                    txt = "ERROR: " + txt
                msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"), "content": txt})
            elif t == "text":
                parts.append({"type": "text", "text": b.get("text", "")})
            elif t == "image" and b.get("source", {}).get("type") == "base64":
                s = b["source"]
                parts.append({"type": "image_url", "image_url": {"url": f"data:{s['media_type']};base64,{s['data']}"}})
        if parts:
            msgs.append({"role": "user", "content": parts if len(parts) > 1 or parts[0]["type"] != "text" else parts[0]["text"]})
    out = {"model": req.get("model"), "messages": msgs, "max_tokens": req.get("max_tokens", 4096),
           "stream": bool(req.get("stream"))}
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}
    for k in ("temperature", "top_p"):
        if k in req:
            out[k] = req[k]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]
    tools = [t for t in req.get("tools", []) if "input_schema" in t]   # skip server-side tools
    if tools:
        out["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                           "parameters": t["input_schema"]}} for t in tools]
        tc = req.get("tool_choice") or {}
        out["tool_choice"] = {"auto": "auto", "any": "required", "none": "none"}.get(tc.get("type") or "", "auto")
        if tc.get("type") == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    effort = (req.get("output_config") or {}).get("effort")
    if effort:
        out["reasoning_effort"] = effort
    if req.get("chat_template_kwargs"):
        out["chat_template_kwargs"] = req["chat_template_kwargs"]
    return out


def _reasoning(obj):
    return obj.get("reasoning_content") or obj.get("reasoning") or ""


def from_openai(resp, model):
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = []
    if _reasoning(msg):
        content.append({"type": "thinking", "thinking": _reasoning(msg), "signature": ""})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except ValueError:
            args = {"_raw_arguments": c["function"].get("arguments")}
        content.append({"type": "tool_use", "id": c.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                        "name": c["function"]["name"], "input": args})
    u = resp.get("usage") or {}
    return {"id": resp.get("id") or f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
            "model": resp.get("model") or model, "content": content,
            "stop_reason": STOP_MAP.get(ch.get("finish_reason") or "", "end_turn"), "stop_sequence": None,
            "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}}


class StreamTranslator:
    """Turns OpenAI chat-completion SSE chunks into Anthropic Messages SSE events."""

    def __init__(self, model):
        self.model, self.index, self.open = model, -1, None   # open: ("text"|"thinking"|"tool", key)
        self.tools = {}                                          # openai tool index -> our block index
        self.stop, self.usage, self.started = None, {}, False

    @staticmethod
    def ev(name, data):
        return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()

    def _start(self):
        self.started = True
        return self.ev("message_start", {"type": "message_start", "message": {
            "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def _close(self):
        if self.open is None:
            return b""
        out = b""
        if self.open[0] == "thinking":
            out += self.ev("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                    "delta": {"type": "signature_delta", "signature": ""}})
        self.open = None
        return out + self.ev("content_block_stop", {"type": "content_block_stop", "index": self.index})

    def _open(self, kind, block, key=None):
        out = self._close()
        self.index += 1
        self.open = (kind, key)
        return out + self.ev("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": block})

    def feed(self, chunk):
        out = b"" if self.started else self._start()
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            d = ch.get("delta") or {}
            r = _reasoning(d)
            if r:
                if not self.open or self.open[0] != "thinking":
                    out += self._open("thinking", {"type": "thinking", "thinking": ""})
                out += self.ev("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                        "delta": {"type": "thinking_delta", "thinking": r}})
            if d.get("content"):
                if not self.open or self.open[0] != "text":
                    out += self._open("text", {"type": "text", "text": ""})
                out += self.ev("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                        "delta": {"type": "text_delta", "text": d["content"]}})
            for tc in d.get("tool_calls") or []:
                ti = tc.get("index", 0)
                fn = tc.get("function") or {}
                if ti not in self.tools:
                    out += self._open("tool", {"type": "tool_use", "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                                               "name": fn.get("name", ""), "input": {}}, key=ti)
                    self.tools[ti] = self.index
                elif self.open != ("tool", ti):
                    continue   # interleaved deltas for an already-closed call are not representable; drop
                if fn.get("arguments"):
                    out += self.ev("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                            "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}})
            if ch.get("finish_reason"):
                self.stop = STOP_MAP.get(ch["finish_reason"] or "", "end_turn")
        return out

    def finish(self):
        out = (b"" if self.started else self._start()) + self._close()
        out += self.ev("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": self.stop or "end_turn", "stop_sequence": None},
                                         "usage": {"input_tokens": self.usage.get("prompt_tokens", 0),
                                                   "output_tokens": self.usage.get("completion_tokens", 0)}})
        return out + self.ev("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------------------------
# LLM route (threaded HTTP server on the unix socket, optionally on a TCP port)
# ---------------------------------------------------------------------------------------------
class LLMHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):   # silence default stderr logging
        pass

    def address_string(self):       # unix sockets have no peer address
        return "sandbox"

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _error(self, status, message, etype="api_error"):
        self._send_json(status, {"type": "error", "error": {"type": etype, "message": message}})

    def _chunk(self, data):
        if data:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

    def _start_stream(self, status, ctype):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("transfer-encoding", "chunked")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True

    def _end_stream(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self):
        if not self.path.startswith("/v1/models"):
            log(f"LLM DENY GET {self.path}")
            return self._error(403, f"bridge: GET {self.path} not allowed", "permission_error")
        try:
            conn, r = upstream_request("GET", UP_BASE + "/models" + self.path[len("/v1/models"):])
        except ConnectionError as e:
            log(f"LLM upstream error: {e}")
            return self._error(502, f"bridge: {e}")
        body = r.read()
        conn.close()
        self.send_response(r.status)
        self.send_header("content-type", r.getheader("content-type", "application/json"))
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if not path.startswith("/v1/messages"):
            log(f"LLM DENY POST {self.path}")
            return self._error(403, f"bridge: POST {self.path} not allowed", "permission_error")
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            return self._error(400, "bridge: request body is not JSON", "invalid_request_error")
        if self.headers.get("x-swarm-thinking", "").lower() == "off":
            req.setdefault("chat_template_kwargs", {}).update(THINK_OFF)
        if FORMAT == "anthropic":
            return self._anthropic(path, query, req)
        if path.startswith("/v1/messages/count_tokens"):
            # OpenAI endpoints have no token counter; a rough estimate keeps callers working.
            return self._send_json(200, {"input_tokens": max(1, len(raw) // 4)})
        return self._openai(req)

    def _anthropic(self, path, query, req):
        fwd = {k: v for k, v in self.headers.items() if k.lower().startswith("anthropic-")}
        up_path = UP_BASE + path[len("/v1"):] + (f"?{query}" if query else "")
        try:
            conn, r = upstream_request("POST", up_path, json.dumps(req).encode(), fwd)
        except ConnectionError as e:
            log(f"LLM upstream error: {e}")
            return self._error(502, f"bridge: {e}")
        if r.status >= 400:
            log(f"LLM upstream HTTP {r.status} on {path}")
        self._start_stream(r.status, r.getheader("content-type", "application/json"))
        try:
            while data := r.read1(65536):
                self._chunk(data)
            self._end_stream()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    def _openai(self, req):
        model = req.get("model", "")
        oreq = to_openai(req)
        try:
            conn, r = upstream_request("POST", UP_BASE + "/chat/completions", json.dumps(oreq).encode())
        except ConnectionError as e:
            log(f"LLM upstream error: {e}")
            return self._error(502, f"bridge: {e}")
        try:
            if r.status >= 400:
                detail = r.read().decode(errors="replace")[:500]
                log(f"LLM upstream HTTP {r.status} (openai)")
                return self._error(r.status, f"upstream: {detail}",
                                   "authentication_error" if r.status in (401, 403) else "api_error")
            if not oreq["stream"]:
                return self._send_json(200, from_openai(json.loads(r.read()), model))
            tr = StreamTranslator(model)
            self._start_stream(200, "text/event-stream")
            buf = b""
            while data := r.read1(65536):
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        continue
                    try:
                        self._chunk(tr.feed(json.loads(payload)))
                    except ValueError:
                        log("LLM openai stream: unparsable chunk skipped")
            self._chunk(tr.finish())
            self._end_stream()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class TCPHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------------------------
# Public-HTTPS CONNECT proxy (asyncio)
# ---------------------------------------------------------------------------------------------
def public_ip(host):
    """Resolve host; return one public IP, or None if any resolved address is not public."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return None
    ips = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not ips or any(not ip.is_global for ip in ips):
        return None
    return str(ips[0])


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def net_client(reader, writer):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except Exception:
        writer.close()
        return
    first = head.decode("latin-1").split("\r\n")[0].split(" ")
    ip, host = None, ""
    if len(first) >= 2 and first[0] == "CONNECT" and first[1].endswith(":443"):
        host = first[1].rsplit(":", 1)[0].strip("[]").lower()
        if not any(host == s or host.endswith("." + s) for s in BLOCK):
            ip = await asyncio.get_running_loop().run_in_executor(None, public_ip, host)
    if ip is None:
        log(f"NET DENY {' '.join(first[:2])}")
        body = b"bridge: only public hosts on port 443 are allowed"
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()
        return
    log(f"NET ALLOW {host} -> {ip}")
    try:
        ur, uw = await asyncio.open_connection(ip, 443)
    except OSError as e:
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        log(f"NET upstream error {host}: {e}")
        return
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(pipe(reader, uw), pipe(ur, writer))


async def main():
    net_path = os.path.join(A.sock_dir, "net.sock")
    if os.path.exists(net_path):
        os.unlink(net_path)
    server = await asyncio.start_unix_server(net_client, path=net_path)
    await server.serve_forever()


if __name__ == "__main__":
    os.makedirs(A.sock_dir, mode=0o700, exist_ok=True)
    with open(os.path.join(A.sock_dir, "bridge.pid"), "w") as f:
        f.write(f"{os.getpid()}\n")
    FORMAT = detect_format()
    llm_path = os.path.join(A.sock_dir, "llm.sock")
    if os.path.exists(llm_path):
        os.unlink(llm_path)
    servers: list[socketserver.BaseServer] = [UnixHTTPServer(llm_path, LLMHandler)]
    if A.tcp_port:
        servers.append(TCPHTTPServer(("127.0.0.1", A.tcp_port), LLMHandler))
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    log(f"bridge up upstream={UP_SCHEME}://{UP_HOST}:{UP_PORT}{UP_BASE} format={FORMAT} "
        f"auth={'yes' if TOKEN else 'no'} tcp={A.tcp_port or '-'} block={','.join(BLOCK)}")
    asyncio.run(main())
