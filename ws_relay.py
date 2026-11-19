#!/usr/bin/env python3
"""NEXUZ WS relay — VLESS-over-WebSocket, WhitePanel-style.

The transport that actually works in Iran (proven by the WhitePanel project
Itskillmaster/WhitePanel-UI, whose config Ali verified live on Railway):

  * The panel (nginx on Railway's $PORT, TLS terminated by Railway's edge)
    proxies /ws/<uuid> to this relay on 127.0.0.1:10089.
  * The relay upgrades the WebSocket itself, reads the first binary message,
    parses the VLESS header (version 0x00, 16-byte UUID, addon length,
    command, port, address), checks the UUID against the panel DB (user
    exists, not stopped, not expired, volume left) and that the UUID in the
    header matches the UUID in the path.
  * Then it dials the target directly (plain TCP, no Xray in the data path)
    and relays bytes both ways. The first downstream chunk gets the 2-byte
    VLESS response header (0x00 0x00) prepended, exactly like WhitePanel.
  * Ping/pong keepalive (websockets sends a ping every 20s) keeps the
    tunnel alive through middleboxes.

Why not Xray's own WS transport: Xray WS behind nginx handshakes and pings
fine from Iran, but real data (YouTube/Telegram) starves — the same symptom
Reality/XHTTP showed. WhitePanel's Go relay (same Railway infra, same
*.up.railway.app domain, same client apps) moves data at full speed, so the
difference is the server-side WS implementation, not the domain. This file
is a faithful Python port of WhitePanel's handleWSRelay/parseVlessHeader.

Only TCP (command 0x01) is proxied, like WhitePanel. UDP/MUX are closed.

Usage accounting: up/down bytes per connection are flushed to the panel DB
(users.used_gb) by a background thread, so the panel's "حجم مصرفی" is real.
"""
import asyncio
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import queue
from datetime import datetime, timezone

import websockets
from websockets.asyncio.server import serve

WS_PORT = int(os.environ.get("WS_RELAY_PORT", "10089"))
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else ".")
DB_PATH = os.path.join(DATA_DIR, "nexuz.db")
GIB = 1024 ** 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ws-relay] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ws-relay")

UUID_RE = re.compile(r"^/ws/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                     r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


class IncompleteHeader(Exception):
    pass


class InvalidHeader(Exception):
    pass


def parse_vless_header(buf):
    """Parse a VLESS request header from the start of buf.

    Returns (uuid_str, command, address, port, payload_bytes).
    Raises IncompleteHeader when buf is too short to decide, InvalidHeader
    when the bytes are definitely not a valid VLESS header.
    Mirrors WhitePanel's parseVlessHeader (Go) exactly.
    """
    if len(buf) < 24:
        raise IncompleteHeader()
    if buf[0] != 0x00:
        raise InvalidHeader(f"bad version {buf[0]}")
    u = buf[1:17]
    uuid_str = "%s-%s-%s-%s-%s" % (u[0:4].hex(), u[4:6].hex(), u[6:8].hex(),
                                   u[8:10].hex(), u[10:16].hex())
    pos = 17
    opt_len = buf[pos]
    pos += 1 + opt_len
    if pos + 3 > len(buf):
        raise IncompleteHeader()
    command = buf[pos]
    pos += 1
    port = int.from_bytes(buf[pos:pos + 2], "big")
    pos += 2
    addr_type = buf[pos]
    pos += 1
    if addr_type == 1:  # IPv4
        if pos + 4 > len(buf):
            raise IncompleteHeader()
        addr = ".".join(str(b) for b in buf[pos:pos + 4])
        pos += 4
    elif addr_type == 2:  # domain
        if pos + 1 > len(buf):
            raise IncompleteHeader()
        dlen = buf[pos]
        pos += 1
        if pos + dlen > len(buf):
            raise IncompleteHeader()
        addr = buf[pos:pos + dlen].decode("utf-8", "replace")
        pos += dlen
    elif addr_type == 3:  # IPv6
        if pos + 16 > len(buf):
            raise IncompleteHeader()
        import ipaddress
        addr = str(ipaddress.IPv6Address(buf[pos:pos + 16]))
        pos += 16
    else:
        raise InvalidHeader(f"unknown addr type {addr_type}")
    return uuid_str, command, addr, port, bytes(buf[pos:])


# ---------------------------------------------------------------- users / DB
def lookup_user(uuid_str):
    """Return the user row dict, or None. Runs in a thread (blocking sqlite)."""
    try:
        db = sqlite3.connect(DB_PATH, timeout=10)
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT id, username, uuid, volume_gb, used_gb, expires_at, stopped"
            " FROM users WHERE uuid=?", (uuid_str,)).fetchone()
        db.close()
    except Exception as e:
        log.warning("db lookup failed: %s", e)
        return None
    if row is None:
        return None
    return dict(row)


def user_allowed(user):
    if not user:
        return False, "unknown uuid"
    if user.get("stopped"):
        return False, "stopped"
    try:
        exp = datetime.fromisoformat(user["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= datetime.now(timezone.utc):
            return False, "expired"
    except Exception:
        pass
    try:
        if float(user.get("used_gb") or 0) >= float(user.get("volume_gb") or 0):
            return False, "volume exhausted"
    except Exception:
        pass
    return True, ""


# ------------------------------------------------------- usage accounting
_usage_q = queue.Queue()


def report_usage(uuid_str, up_bytes, down_bytes):
    if up_bytes or down_bytes:
        _usage_q.put((uuid_str, up_bytes, down_bytes))


def _usage_flusher():
    """Background thread: batch usage updates into the panel DB every 5s."""
    pending = {}
    while True:
        try:
            deadline = time.time() + 5
            while True:
                timeout = deadline - time.time()
                if timeout <= 0:
                    break
                try:
                    uuid_str, up_b, down_b = _usage_q.get(timeout=timeout)
                except queue.Empty:
                    break
                a = pending.setdefault(uuid_str, [0, 0])
                a[0] += up_b
                a[1] += down_b
            if pending:
                items = pending
                pending = {}
                for attempt in range(3):
                    try:
                        db = sqlite3.connect(DB_PATH, timeout=30)
                        for uuid_str, (up_b, down_b) in items.items():
                            gb = (up_b + down_b) / GIB
                            db.execute(
                                "UPDATE users SET used_gb = used_gb + ?"
                                " WHERE uuid=?", (gb, uuid_str))
                        db.commit()
                        db.close()
                        break
                    except Exception as e:
                        log.warning("usage flush failed (try %d): %s",
                                    attempt + 1, e)
                        time.sleep(0.2)
        except Exception as e:
            log.warning("usage flusher error: %s", e)
            time.sleep(1)


threading.Thread(target=_usage_flusher, daemon=True).start()


# ------------------------------------------------------------------- relay
async def handle_connection(ws):
    try:
        req = getattr(ws, "request", None)
        path = req.path if req is not None else getattr(ws, "path", "")
    except Exception:
        path = ""
    m = UUID_RE.match(path or "")
    if not m:
        log.info("reject non-ws path: %r", path)
        await ws.close(code=1008, reason="bad path")
        return
    path_uuid = m.group(1).lower()

    # Read messages until the VLESS header is complete.
    buf = b""
    try:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            if not isinstance(msg, (bytes, bytearray)):
                continue
            buf += bytes(msg)
            try:
                uuid_str, command, addr, port, payload = parse_vless_header(buf)
                break
            except IncompleteHeader:
                if len(buf) > 65536:
                    log.info("header too long, closing")
                    await ws.close(code=1008)
                    return
                continue
            except InvalidHeader as e:
                log.info("bad VLESS header from %s: %s", path_uuid, e)
                await ws.close(code=1008)
                return
    except asyncio.TimeoutError:
        log.info("no first message from %s", path_uuid)
        await ws.close(code=1008)
        return
    except websockets.ConnectionClosed:
        return

    if uuid_str.lower() != path_uuid:
        log.info("uuid mismatch: header=%s path=%s", uuid_str, path_uuid)
        await ws.close(code=1008)
        return
    if command != 0x01:  # TCP only, like WhitePanel
        log.info("non-TCP command %d from %s, closing", command, path_uuid)
        await ws.close(code=1008)
        return

    loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, lookup_user, uuid_str)
    ok, why = user_allowed(user)
    if not ok:
        log.info("user %s (%s) denied: %s", uuid_str,
                 user.get("username") if user else "?", why)
        await ws.close(code=1008, reason=why)
        return
    username = user["username"]

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(addr, port), timeout=10)
    except Exception as e:
        log.info("dial %s:%d for %s failed: %s", addr, port, username, e)
        await ws.close(code=1011)
        return
    log.info("relay %s -> %s:%d", username, addr, port)

    if payload:
        writer.write(payload)
        try:
            await writer.drain()
        except Exception:
            pass

    stats = {"up": len(payload), "down": 0}
    done = asyncio.Event()

    async def ws_to_tcp():
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)) and msg:
                    stats["up"] += len(msg)
                    writer.write(bytes(msg))
                    await writer.drain()
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as e:
            log.debug("ws->tcp error for %s: %s", username, e)
        finally:
            done.set()
            try:
                writer.close()
            except Exception:
                pass

    async def tcp_to_ws():
        first = True
        try:
            while True:
                chunk = await reader.read(32 * 1024)
                if not chunk:
                    break
                stats["down"] += len(chunk)
                out = b"\x00\x00" + chunk if first else chunk
                first = False
                await ws.send(out)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as e:
            log.debug("tcp->ws error for %s: %s", username, e)
        finally:
            done.set()
            try:
                await ws.close()
            except Exception:
                pass

    t1 = asyncio.create_task(ws_to_tcp())
    t2 = asyncio.create_task(tcp_to_ws())
    await done.wait()
    for t in (t1, t2):
        t.cancel()
    try:
        writer.close()
    except Exception:
        pass
    report_usage(uuid_str, stats["up"], stats["down"])
    log.info("closed %s (%s:%d) up=%d down=%d",
             username, addr, port, stats["up"], stats["down"])


async def main():
    async with serve(
            handle_connection, "127.0.0.1", WS_PORT,
            ping_interval=20, ping_timeout=20, max_size=2 ** 20):
        log.info("listening on 127.0.0.1:%d (DB %s)", WS_PORT, DB_PATH)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
