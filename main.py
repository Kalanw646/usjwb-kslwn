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
import re
import subprocess
import threading
import time

# Must be set before app.py is imported (it reads this at module level).
os.environ.setdefault("KATABUMP", "1")

PORT = int(os.environ.get("PORT", "8080"))

# --- Cloudflare tunnel switches (edit these lines, re-upload, restart) ---
# TUNNEL_PROTOCOL: "http2" (current) or "quic". QUIC is cloudflared's
#   default and can be faster / less throttled on some paths — a cheap
#   speed test: flip this one word, restart, retest on the phone.
#   If QUIC never connects (UDP 7844 blocked), flip it back to "http2".
# NAMED_TUNNEL_TOKEN: paste the token from the Cloudflare dashboard
#   (Zero Trust > Networks > Tunnels > your tunnel) to run a NAMED
#   tunnel instead of the random quick tunnel; leave "" for quick.
#   A named tunnel needs a domain on Cloudflare DNS — the raw
#   <uuid>.cfargotunnel.com address is internal only and is NOT a
#   browsable hostname, and a CNAME from another DNS provider will
#   not work (Cloudflare only proxies records in the same account).
# NAMED_TUNNEL_HOSTNAME: the public hostname of the named tunnel,
#   e.g. "nexuz.example.com" — written to tunnel_url.txt so the panel
#   home page shows it (a token tunnel prints no trycloudflare URL).
TUNNEL_PROTOCOL = "http2"
NAMED_TUNNEL_TOKEN = ""
NAMED_TUNNEL_HOSTNAME = ""
# Pterodactyl/Wings sets SERVER_PORT to the public allocation port
# (20334 here). The allocation is DNAT'd to the SAME port inside the
# container, so listening only on $PORT (8080) leaves the public port
# closed — the panel refused every connection on 20334. Listen on
# both, so the panel answers whichever port the mapping uses.
SERVER_PORT = int(os.environ.get("SERVER_PORT", "0") or 0)
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [nexuz] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nexuz")


CLOUDFLARED_URL = ("https://github.com/cloudflare/cloudflared/releases/"
                   "latest/download/cloudflared-linux-amd64")


def _cloudflared_ok(exe):
    """True when the binary exists and actually runs (a half-downloaded
    file must not be trusted)."""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True,
                           timeout=15)
        return r.returncode == 0 and b"cloudflared" in r.stdout
    except Exception:
        return False


def _ensure_cloudflared(here):
    """Return the path to a working cloudflared binary, downloading the
    official release if it is missing. The KataBump console is NOT a
    shell — typed commands never run there — so the panel fetches the
    binary itself on startup instead of asking the user to type curl.
    Runs in a background thread, so a slow download never delays the
    panel start."""
    exe = os.path.join(here, "cloudflared")
    if os.path.isfile(exe):
        try:
            os.chmod(exe, 0o755)
        except OSError:
            pass
        if _cloudflared_ok(exe):
            return exe
        try:
            os.remove(exe)  # corrupt / half-downloaded — fetch fresh
        except OSError:
            pass
    try:
        import urllib.request
        log.info("downloading cloudflared ...")
        urllib.request.urlretrieve(CLOUDFLARED_URL, exe)
        os.chmod(exe, 0o755)
        if _cloudflared_ok(exe):
            log.info("cloudflared downloaded")
            return exe
        log.warning("downloaded cloudflared does not run — tunnel off")
        try:
            os.remove(exe)
        except OSError:
            pass
    except Exception as e:
        log.warning("could not download cloudflared: %s", e)
    return None


def _start_cloudflared():
    """Start a Cloudflare Tunnel for the front door. No account,
    no card — it dials out to Cloudflare and publishes the panel on a
    random https://<...>.trycloudflare.com URL with TLS on 443. That
    is what makes the panel reachable from Iran (plain WS on the raw
    IP:port is blocked there). The URL is random on every start; a
    watcher logs it and writes it to tunnel_url.txt so the panel home
    page can show it. If NAMED_TUNNEL_TOKEN is set (see top of file),
    a named tunnel runs instead and NAMED_TUNNEL_HOSTNAME is written
    to tunnel_url.txt. If the binary cannot be fetched, the panel just
    runs without a tunnel (direct IP:port only)."""
    here = os.path.dirname(os.path.abspath(__file__))
    url_path = os.path.join(here, "tunnel_url.txt")
    try:
        os.remove(url_path)  # a stale URL from a previous run is wrong
    except OSError:
        pass
    threading.Thread(target=_cloudflared_thread, args=(here,),
                     daemon=True).start()


def _cloudflared_thread(here):
    url_path = os.path.join(here, "tunnel_url.txt")
    exe = _ensure_cloudflared(here)
    if not exe:
        log.info("no cloudflared binary — tunnel off")
        return
    try:
        lf = open(os.path.join(here, "cloudflared.log"), "wb")
        if NAMED_TUNNEL_TOKEN.strip():
            # Remotely-managed named tunnel: config lives in the
            # Cloudflare dashboard, the token is the only secret the
            # server needs. No trycloudflare URL is printed, so write
            # the stable hostname for the panel home page instead.
            subprocess.Popen(
                [exe, "tunnel", "--no-autoupdate", "run",
                 "--token", NAMED_TUNNEL_TOKEN.strip()],
                stdout=lf, stderr=subprocess.STDOUT,
                start_new_session=True)
            log.info("cloudflared started (named tunnel, log: "
                     "cloudflared.log)")
            if NAMED_TUNNEL_HOSTNAME.strip():
                with open(url_path, "w") as out:
                    out.write("https://" +
                              NAMED_TUNNEL_HOSTNAME.strip())
                log.info("tunnel URL: https://%s",
                         NAMED_TUNNEL_HOSTNAME.strip())
            return
        subprocess.Popen(
            [exe, "tunnel", "--protocol", TUNNEL_PROTOCOL, "--url",
             f"http://127.0.0.1:{PORT}"],
            stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        log.info("cloudflared started (%s quick tunnel, log: "
                 "cloudflared.log)", TUNNEL_PROTOCOL)
    except Exception as e:
        log.warning("could not start cloudflared: %s", e)
        return
    _watch_tunnel_log(here)


def _watch_tunnel_log(here):
    # The log also mentions https://api.trycloudflare.com (the API the
    # binary calls to create the tunnel) — that is NOT the tunnel URL,
    # so skip it. The real line is "Your quick Tunnel has been created
    # https://<words>.trycloudflare.com".
    url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    seen = None
    log_path = os.path.join(here, "cloudflared.log")
    url_path = os.path.join(here, "tunnel_url.txt")
    while True:
        try:
            with open(log_path, "r", errors="ignore") as f:
                for line in f:
                    m = url_re.search(line)
                    if not m:
                        continue
                    url = m.group(0)
                    if url == "https://api.trycloudflare.com":
                        continue
                    if url != seen:
                        seen = url
                        with open(url_path, "w") as out:
                            out.write(url)
                        log.info("tunnel URL: %s", url)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        time.sleep(5)


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
        # Shiprr's TCP health check probes a port it never tells us,
        # and a wrong port fails the whole rollout with no error. So
        # listen on every plausible port: $PORT (or 8080), plus the
        # common defaults 8000 and 3000. Each bind is best-effort — a
        # busy port must not kill the app.
        listen_ports = sorted({PORT, SERVER_PORT, 8080, 8000, 3000} - {0})
        bound_any = False
        for p in listen_ports:
            try:
                site = web.TCPSite(runner, "0.0.0.0", p)
                await site.start()
                bound_any = True
                log.info("front door on 0.0.0.0:%d (panel 127.0.0.1:%d)",
                         p, FLASK_PORT)
            except Exception as e:
                log.warning("could not listen on 0.0.0.0:%d: %s", p, e)
        if not bound_any:
            raise RuntimeError("front door could not bind any port")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    _start_cloudflared()
    threading.Thread(target=_run_flask, daemon=True).start()
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass
