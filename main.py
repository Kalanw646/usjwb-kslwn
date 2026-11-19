#!/usr/bin/env python3
"""NEXUZ on KataBump — single-process entry point.

KataBump runs ONE startup command and gives the server ONE public port
($PORT). The whole panel runs inside this one process:

  * Flask panel (app.py) — served by waitress in a thread on
    127.0.0.1:5000 (same as Railway's gunicorn backend).
  * ws_relay.py — the WhitePanel-style VLESS-over-WebSocket relay on
    127.0.0.1:10089, in the same asyncio loop as the front door.
  * frontdoor.py — aiohttp on 0.0.0.0:$PORT: /ws/<uuid> goes to the
    relay, everything else is reverse-proxied to the panel.

KATABUMP=1 is set here before app.py is imported, so the panel runs in
its no-Xray mode: no Xray binary on KataBump, no config rewrite, no
XHTTP/Reality links (only the WS transport — the one that works in Iran).
The panel's pages, DB schema, sub format and WS link format are exactly
the same as on Railway; only the deployment plumbing differs.

KataBump server settings: main file = main.py, requirements.txt is
installed automatically, $PORT is set by the platform. Data (users DB)
lives in ./nexuz.db next to the code — KataBump's disk persists across
restarts. Free servers must be renewed every 4 days or they stop.
"""
import asyncio
import logging
import os
import threading

# Must be set before app.py is imported (it reads this at module level).
os.environ.setdefault("KATABUMP", "1")

PORT = int(os.environ.get("PORT", "8080"))
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [nexuz] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nexuz")


def _run_flask():
    """Flask panel on waitress (pure Python WSGI server)."""
    from waitress import serve
    import app as panel
    log.info("panel on 127.0.0.1:%d", FLASK_PORT)
    serve(panel.app, host="127.0.0.1", port=FLASK_PORT, threads=4)


async def _async_main():
    import ws_relay
    from websockets.asyncio.server import serve as ws_serve
    from frontdoor import create_app
    from aiohttp import web

    async with ws_serve(
            ws_relay.handle_connection, "127.0.0.1", ws_relay.WS_PORT,
            ping_interval=20, ping_timeout=20, max_size=2 ** 20):
        log.info("ws relay on 127.0.0.1:%d (DB %s)",
                 ws_relay.WS_PORT, ws_relay.DB_PATH)
        fapp = create_app()
        runner = web.AppRunner(fapp)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info("front door on 0.0.0.0:%d (panel 127.0.0.1:%d)",
                 PORT, FLASK_PORT)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    threading.Thread(target=_run_flask, daemon=True).start()
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass
