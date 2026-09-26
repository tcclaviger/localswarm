#!/usr/bin/env python3
"""Runs INSIDE the sandbox: exposes the bridge's unix sockets as local TCP ports.

Usage: swarm_fwd.py <llm-port> <proxy-port>
  127.0.0.1:<llm-port>   -> /sock/llm.sock
  127.0.0.1:<proxy-port> -> /sock/net.sock
"""
import asyncio, sys

MAP = {int(sys.argv[1]): "/sock/llm.sock", int(sys.argv[2]): "/sock/net.sock"}


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


def make_handler(sock):
    async def handler(reader, writer):
        try:
            ur, uw = await asyncio.open_unix_connection(sock)
        except OSError:
            writer.close()
            return
        await asyncio.gather(pipe(reader, uw), pipe(ur, writer))
    return handler


async def main():
    servers = [await asyncio.start_server(make_handler(s), "127.0.0.1", p) for p, s in MAP.items()]
    await asyncio.gather(*(s.serve_forever() for s in servers))


asyncio.run(main())
