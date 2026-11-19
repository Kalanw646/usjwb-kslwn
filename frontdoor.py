#!/usr/bin/env python3
"""NEXUZ front door — one public port, two backends (KataBump mode).

KataBump gives a Python server exactly ONE public port (and one startup
command), but the panel needs two things on that port:

  * the Flask panel itself (plain HTTP: /, /login, /create, /settings,
    /sub/<token>, /health), and
  * the VLESS-over-WebSocket relay at /ws/<uuid> (ws_relay.py).

On Railway nginx did this split. On KataBump there is no nginx, so this
small aiohttp app is the front door:

  * /ws/<uuid> — WebSocket: it opens a WebSocket client connection to the
    local ws_relay.py and forwards complete messages both ways (a dumb
    frame pipe — the relay still does all the VLESS parsing, DB checks
    and usage accounting, untouched). Ping/pong keepalives run on both
    hops (aiohttp heartbeat=20 outside, websockets ping_interval=20
    inside).
  * everything else — plain HTTP reverse proxy to the Flask panel
    (waitress on 127.0.0.1:5000): method, path+query, headers and body
    are forwarded, and the panel's status, headers (incl. Set-Cookie)
    and body come back.

Run by main.py (KataBump entry point). Not used on Railway at all.
"""
import asyncio
import logging
import os
import re

import websockets
from websockets.asyncio.client import connect as ws_connect
from aiohttp import web, ClientSession

log = logging.getLogger("frontdoor")

FLASK_BASE = os.environ.get("FLASK_BASE", "http://127.0.0.1:5000")
RELAY_WS = os.environ.get("RELAY_WS", "ws://127.0.0.1:10089")

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

HOP_BY_HOP = {
    "connection", "keep-alive", "te", "trailer", "transfer-encoding",
    "upgrade", "proxy-authorization", "proxy-connection",
}


async def handle_ws(request):
    uuid_str = request.match_info.get("uuid", "")
    if not UUID_RE.match(uuid_str):
        return web.Response(status=400, text="bad path")

    # Connect to the real relay first: if it is down we answer 502 instead
    # of completing a WebSocket handshake we cannot serve.
    try:
        inner = await ws_connect(
            f"{RELAY_WS}/ws/{uuid_str}",
            ping_interval=20, ping_timeout=20, max_size=2 ** 20)
    except Exception as e:
        log.warning("relay connect failed for %s: %s", uuid_str, e)
        return web.Response(status=502, text="relay unavailable")

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=2 ** 20)
    await ws.prepare(request)
    log.info("ws %s connected", uuid_str)

    async def outer_to_inner():
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    await inner.send(msg.data)
                elif msg.type in (web.WSMsgType.CLOSE,
                                  web.WSMsgType.CLOSED,
                                  web.WSMsgType.ERROR):
                    break
        except Exception:
            pass
        finally:
            try:
                await inner.close()
            except Exception:
                pass

    async def inner_to_outer():
        try:
            async for data in inner:
                if isinstance(data, (bytes, bytearray)):
                    await ws.send_bytes(bytes(data))
        except websockets.ConnectionClosed:
            pass
        except Exception:
            pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    await asyncio.gather(outer_to_inner(), inner_to_outer(),
                         return_exceptions=True)
    log.info("ws %s closed", uuid_str)
    return ws


async def handle_http(request):
    """Reverse-proxy one HTTP request to the Flask panel."""
    session = request.app["client"]
    body = await request.read()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP}
    headers["Host"] = "127.0.0.1:5000"
    headers["X-Forwarded-For"] = request.remote or ""
    headers["X-Forwarded-Proto"] = request.scheme
    url = FLASK_BASE + request.path_qs
    try:
        async with session.request(
                request.method, url, headers=headers,
                data=body if body else None,
                allow_redirects=False) as resp:
            rheaders = {k: v for k, v in resp.headers.items()
                        if k.lower() not in HOP_BY_HOP}
            rbody = await resp.read()
            return web.Response(status=resp.status, headers=rheaders,
                                body=rbody)
    except Exception as e:
        log.warning("proxy %s %s failed: %s", request.method, url, e)
        return web.Response(status=502, text="panel unavailable")


async def _on_startup(app):
    app["client"] = ClientSession()


async def _on_cleanup(app):
    client = app.pop("client", None)
    if client is not None:
        await client.close()


def create_app():
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/ws/{uuid}", handle_ws)
    app.router.add_route("*", "/", handle_http)
    app.router.add_route("*", "/{path:.*}", handle_http)
    return app
