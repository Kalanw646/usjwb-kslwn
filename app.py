# NEXUZ — پنل V2Ray (نسخه ۱)
# Persian RTL panel, Lion & Sun (شیر و خورشید) theme, purple UI.
# Stack: Flask + SQLite (single file, Railway-ready).
# Pages: home (/) , create user (/create) , settings (/settings)
# VLESS configs + subscription link /sub/<token>
#   - plain text for V2Ray clients (v2rayNG, Streisand, FoXray, ...)
#   - beautiful Lion & Sun HTML page when opened in a browser (?view=1)
# Subscription content, in order:
#   1. 👤 نام کاربری
#   2. 📦 حجم کاربر
#   3. ⏳ زمان باقی‌مانده
#   4. کانفیگ VLESS واقعی کاربر
# The first three are info entries (dummy VLESS URIs pointing at
# 127.0.0.1:1) so they show up at the top of the client list, in order,
# and never interfere with the real config below them.
#
# Deploy on Railway: connect this repo, add a Volume mounted at /data
# (so users survive redeploys), set PORT automatically by Railway.
# Architecture: Dockerfile installs Xray core + nginx. nginx on $PORT
# proxies WS /nexuz to Xray (127.0.0.1:10086) and the rest to Flask
# (127.0.0.1:5000). The panel rewrites /data/xray-config.json from the DB
# on every user create/delete and restarts Xray, so configs really work.
# Login: password-only page, default password "admin" (change in Settings).
# Optional env: ADMIN_PASSWORD — if set, it overrides the stored password.
#              DATA_DIR — default /data when it exists, else local folder.

import hashlib
import math
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import (
    Flask, g, redirect, render_template_string, request,
    Response, session, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Xray core runs in the same Railway container (see Dockerfile/start.sh):
# nginx on $PORT proxies /ws/<uuid> to the Python WS relay ws_relay.py
# (127.0.0.1:10089) — the WhitePanel-style VLESS-over-WebSocket transport
# that actually works in Iran — XHTTP /xhttp to Xray (127.0.0.1:10087),
# the legacy WS /nexuz to Xray (127.0.0.1:10086), and everything else to
# this Flask panel (127.0.0.1:5000). Railway terminates TLS, so the relay
# and Xray use security=none internally and clients use tls on port 443.
# Reality runs on raw TCP 8443, exposed via Railway TCP Proxy.
WS_RELAY_PORT = 10089  # WhitePanel-style VLESS-over-WS relay (primary transport)
XRAY_PORT = 10086  # VLESS+WS inbound (behind nginx, plain HTTP after the edge)
XHTTP_PORT = 10087  # VLESS+XHTTP inbound (behind nginx, plain HTTP after the edge)
REALITY_PORT = 8443  # VLESS+Reality inbound (raw TCP, via Railway TCP Proxy)
XRAY_CONFIG_PATH = os.path.join("/data", "xray-config.json") if os.path.isdir("/data") else os.path.join(".", "xray-config.json")
WS_PATH = "nexuz"  # fixed path: nginx proxies /nexuz to Xray
XHTTP_PATH = "xhttp"  # fixed path: nginx proxies /xhttp to Xray

DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "nexuz.db")
# Optional env: ADMIN_PASSWORD — if set, it overrides the stored password.
# Otherwise the panel password lives in the DB (settings: admin_password_hash),
# default "admin", changeable from the Settings page.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# KataBump mode (set by main.py, the KataBump entry point): no Xray binary,
# no nginx, no TCP Proxy on KataBump — only the WS relay runs. The panel
# keeps its pages and DB exactly the same; it just skips the Xray config
# rewrite/restart and only hands out WS configs (XHTTP/Reality need Xray).
KATABUMP = os.environ.get("KATABUMP") == "1"

def _sha(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def check_admin_password(pw):
    if ADMIN_PASSWORD:
        return pw == ADMIN_PASSWORD
    return _sha(pw or "") == get_setting("admin_password_hash")

DEFAULT_SETTINGS = {
    "panel_name": "NEXUZ",
    "server_address": "",
    "port": "443",
    "sni": "",
    "ws_path": "nexuz",
    "network": "ws",
    "security": "tls",
    "sub_base_url": "",
    "sub_volume_unit": "GB",
    # Reality (VLESS over raw TCP via Railway TCP Proxy): cover domain whose
    # TLS the handshake mimics, the proxy endpoint Railway assigns, and the
    # panel's own x25519 keypair + shortId (generated once, stored in DB).
    "reality_dest": "snapp.ir",
    "reality_host": "",
    "reality_port": "",
    "reality_private_key": "",
    "reality_public_key": "",
    "reality_short_id": "",
    "admin_password_hash": _sha("admin"),
}

# ---------------------------------------------------------------- DB
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        uuid TEXT NOT NULL,
        token TEXT UNIQUE NOT NULL,
        volume_gb REAL NOT NULL,
        used_gb REAL NOT NULL DEFAULT 0,
        days INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    for k, v in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    # migration: stop/start flag for users (added after v1)
    cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if "stopped" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN stopped INTEGER NOT NULL DEFAULT 0")
    db.commit()
    db.close()

def get_setting(key):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

def get_all_settings():
    return {k: get_setting(k) for k in DEFAULT_SETTINGS}

def set_setting(key, value):
    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()

# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc)

def parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return now()

def remaining_days(user):
    delta = parse_dt(user["expires_at"]) - now()
    return max(0, math.ceil(delta.total_seconds() / 86400)) if delta.total_seconds() > 0 else 0

def is_active(user):
    try:
        if user["stopped"]:
            return False
    except Exception:
        pass
    return bool(user["active"]) and remaining_days(user) > 0

def format_volume_text(user, settings):
    """📦 line for the sub link, in GB or MB per the Settings choice."""
    vol = float(user["volume_gb"])
    left = max(0.0, vol - float(user["used_gb"]))
    if (settings.get("sub_volume_unit") or "GB").upper() == "MB":
        return f"📦 حجم: {vol * 1024:g} مگابایت | باقی‌مانده: {left * 1024:g} مگابایت"
    return f"📦 حجم: {vol:g} گیگابایت | باقی‌مانده: {left:g} گیگابایت"

def fmt_date(s):
    return parse_dt(s).strftime("%Y-%m-%d")

def _is_ipv4(host):
    parts = (host or "").split(":")[0].split(".")
    return (len(parts) == 4
            and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts))

def tunnel_url():
    """Current Cloudflare Quick Tunnel URL, written by main.py when it
    starts cloudflared (KataBump only). Empty when no tunnel runs."""
    if not KATABUMP:
        return ""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "tunnel_url.txt")
        with open(p) as f:
            return f.read().strip()
    except Exception:
        return ""

def default_server():
    # Xray runs on the same domain as the panel (nginx proxies /nexuz to it),
    # so when no server address is set in Settings, the panel's own host is
    # the VLESS server — configs are never empty / never your-server.com.
    # Railway sets RAILWAY_PUBLIC_DOMAIN for services with a public domain
    # (e.g. web-production-xxx.up.railway.app). Prefer it: Railway's edge
    # rewrites the Host header to the container's internal address
    # (127.0.0.1:$PORT), so request.host / X-Forwarded-Host are useless
    # on Railway — without this, every generated config pointed at
    # 127.0.0.1 (unusable on a phone). Found 2026-11-20 on the live panel.
    pub = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if pub:
        return pub.split(":")[0]
    try:
        host = request.headers.get("X-Forwarded-Host") or request.host
        return host.split(":")[0]
    except Exception:
        return "your-server.com"

def build_vless(user, settings, server=None, remark=None, kind="ws"):
    """Real VLESS URI for a user.
    kind="ws": PRIMARY — WhitePanel-style VLESS+WS+TLS on 443 to the panel
    domain, path /ws/<uuid>, alpn http/1.1. The server side is ws_relay.py
    (the panel's own VLESS-over-WebSocket relay, a port of WhitePanel's
    Go relay), not Xray's WS transport — that is the transport Ali verified
    works in Iran on the same Railway infra. WS rides on HTTP/1.1 only.
    kind="xhttp": VLESS+XHTTP+TLS on 443 to the panel domain.
    kind="reality": VLESS+Reality over raw TCP to the Railway TCP Proxy
    endpoint; None when not configured yet."""
    if KATABUMP and kind in ("xhttp", "reality"):
        return None  # no Xray on KataBump — WS only
    if kind == "reality":
        host = (settings.get("reality_host") or "").strip()
        port = (settings.get("reality_port") or "").strip()
        pub = (settings.get("reality_public_key") or "").strip()
        sid = (settings.get("reality_short_id") or "").strip()
        dest = (settings.get("reality_dest") or "snapp.ir").strip()
        if not (host and port and pub and sid):
            return None
        params = (f"encryption=none&flow=xtls-rprx-vision&security=reality"
                  f"&sni={quote(dest)}&fp=chrome&pbk={quote(pub)}&sid={quote(sid)}&type=tcp")
        return (f"vless://{user['uuid']}@{host}:{port}?{params}"
                f"#{quote(remark if remark is not None else user['username'])}")
    server = (server or settings.get("server_address") or default_server()).strip()
    port = settings.get("port") or "443"
    sni = (settings.get("sni") or server).strip()
    if KATABUMP and kind == "ws":
        # When the panel is reached through a DNS hostname (a Cloudflare
        # Tunnel trycloudflare.com URL, or any other TLS-fronted domain),
        # the edge terminates TLS on 443 — emit the WhitePanel TLS/443
        # link, same as on Railway. The SNI must be the tunnel host
        # itself (the edge cert is for it), never a stale sni setting
        # from the Railway era. Only a raw IP (KataBump's own
        # allocation, no TLS) gets the plain-WS link below.
        if not _is_ipv4(server):
            params = (f"encryption=none&security=tls&sni={quote(server)}&fp=chrome"
                      f"&alpn=http%2F1.1&type=ws&host={quote(server)}"
                      f"&path=%2Fws%2F{user['uuid']}")
            return (f"vless://{user['uuid']}@{server}:443?{params}"
                    f"#{quote(remark if remark is not None else user['username'])}")
        # KataBump gives a raw IP:port with no TLS — plain WS on the
        # public allocation port (SERVER_PORT, e.g. 20334). A tls/443
        # link would point at a port nothing listens on.
        pub = (os.environ.get("SERVER_PORT") or "").strip()
        if not pub and (settings.get("port") or "443") != "443":
            pub = settings.get("port")
        if not pub:
            pub = os.environ.get("PORT") or "443"
        params = (f"encryption=none&security=none&type=ws"
                  f"&host={quote(server)}&path=%2Fws%2F{user['uuid']}")
        return (f"vless://{user['uuid']}@{server}:{pub}?{params}"
                f"#{quote(remark if remark is not None else user['username'])}")
    if kind == "xhttp":
        # Railway terminates TLS on 443; Xray behind nginx uses xhttp, no TLS.
        params = (f"encryption=none&security=tls&sni={quote(sni)}&fp=chrome"
                  f"&type=xhttp&mode=packet-up&path=%2F{XHTTP_PATH}&host={quote(server)}")
    else:
        # WhitePanel-style, byte-for-byte the format Ali verified live:
        # encryption=none&security=tls&sni=<domain>&fp=chrome&alpn=http/1.1
        # &type=ws&host=<domain>&path=/ws/<uuid> — served by ws_relay.py.
        params = (f"encryption=none&security=tls&sni={quote(sni)}&fp=chrome"
                  f"&alpn=http%2F1.1&type=ws&host={quote(server)}"
                  f"&path=%2Fws%2F{user['uuid']}")
    return (f"vless://{user['uuid']}@{server}:{port}?{params}"
            f"#{quote(remark if remark is not None else user['username'])}")

def build_sub_lines(user, settings):
    # Info entries use the SAME real server + UUID as the main config, only
    # the remark differs. Earlier dummy entries (127.0.0.1:1) made some
    # clients (v2rayNG) reject the whole subscription — valid entries import
    # everywhere (v2rayNG, Streisand, FoXray, Happ).
    vol = float(user["volume_gb"])
    left = max(0.0, vol - float(user["used_gb"]))
    days = remaining_days(user)
    lines = [
        build_vless(user, settings, remark=f"👤 نام کاربری: {user['username']}"),
        build_vless(user, settings, remark=format_volume_text(user, settings)),
        build_vless(user, settings, remark=f"⏳ زمان باقی‌مانده: {days} روز"),
        build_vless(user, settings),
    ]
    # WS first (WhitePanel-style relay — the transport that works in Iran),
    # then XHTTP, then Reality (both were reported cut in Iran).
    xhttp = build_vless(user, settings, kind="xhttp",
                        remark=f"⚡ XHTTP — {user['username']}")
    if xhttp:
        lines.append(xhttp)
    reality = build_vless(user, settings, kind="reality",
                          remark=f"🔒 Reality — {user['username']}")
    if reality:
        lines.append(reality)
    return lines

def _read_settings_direct():
    """Read settings straight from SQLite (works outside a request)."""
    out = dict(DEFAULT_SETTINGS)
    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        for r in db.execute("SELECT key, value FROM settings").fetchall():
            out[r["key"]] = r["value"]
        db.close()
    except Exception:
        pass
    return out

def ensure_reality_keys():
    """Generate the panel's Reality x25519 keypair once (via the xray CLI)
    and a shortId; stored in the settings DB, never in git."""
    import secrets, shutil, subprocess
    try:
        s = _read_settings_direct()
        if s.get("reality_private_key") and s.get("reality_short_id"):
            return
        db = sqlite3.connect(DB_PATH)
        if not s.get("reality_private_key"):
            exe = shutil.which("xray") or "/usr/local/bin/xray"
            try:
                r = subprocess.run([exe, "x25519"], capture_output=True,
                                   text=True, timeout=15)
                priv = pub = ""
                for line in (r.stdout or "").splitlines():
                    # Xray 26 prints "PrivateKey: ..." / "Password (PublicKey): ...";
                    # older versions print "Private key: ..." / "Public key: ...".
                    low = line.lower().replace(" ", "")
                    if low.startswith("privatekey:"):
                        priv = line.split(":", 1)[1].strip()
                    if low.startswith("password(publickey):") or low.startswith("publickey:"):
                        pub = line.split(":", 1)[1].strip()
                if priv:
                    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reality_private_key', ?)", (priv,))
                    if pub:
                        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reality_public_key', ?)", (pub,))
            except Exception:
                pass
        if not s.get("reality_short_id"):
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reality_short_id', ?)",
                       (secrets.token_hex(8),))
        db.commit()
        db.close()
    except Exception:
        pass

def sync_xray_config():
    """Rewrite Xray's config from the panel DB (all active users as clients).
    Safe to call outside a request: opens its own SQLite connection.
    Three inbounds share the same users:
    - WS on 10086 (behind nginx, TLS terminated at Railway's edge) — fallback
    - XHTTP on 10087 (behind nginx, same edge) — second option on port 443
    - Reality on 8443 (raw TCP, exposed via Railway TCP Proxy) — the strong
      one for Iran: the handshake looks exactly like TLS to the cover domain,
      so DPI can't starve it the way it starves the WS tunnel."""
    if KATABUMP:
        return  # no Xray on KataBump; the WS relay reads the DB per connection
    import json
    ensure_reality_keys()
    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        users = db.execute("SELECT * FROM users").fetchall()
        db.close()
    except Exception:
        users = []
    rs = _read_settings_direct()
    ws_clients = []
    reality_clients = []
    for u in users:
        try:
            if not is_active(u):
                continue
        except Exception:
            pass
        ws_clients.append({"id": u["uuid"], "email": u["username"], "level": 0})
        reality_clients.append({"id": u["uuid"], "email": u["username"],
                                "level": 0, "flow": "xtls-rprx-vision"})
    inbounds = [{
        "port": XRAY_PORT,
        "protocol": "vless",
        "settings": {"clients": ws_clients, "decryption": "none"},
        "streamSettings": {
            "network": "ws",
            "wsSettings": {"path": f"/{WS_PATH}"},
        },
        # Sniffing OFF: it misroutes non-TLS traffic (Telegram MTProto)
        # and stalls connections; the original destination is correct.
        "sniffing": {"enabled": False},
    }, {
        "port": XHTTP_PORT,
        "protocol": "vless",
        "settings": {"clients": ws_clients, "decryption": "none"},
        "streamSettings": {
            "network": "xhttp",
            "xhttpSettings": {"path": f"/{XHTTP_PATH}", "mode": "packet-up"},
        },
        "sniffing": {"enabled": False},
    }]
    priv = (rs.get("reality_private_key") or "").strip()
    sid = (rs.get("reality_short_id") or "").strip()
    dest = (rs.get("reality_dest") or "snapp.ir").strip()
    if priv:
        inbounds.append({
            "port": REALITY_PORT,
            "protocol": "vless",
            "settings": {"clients": reality_clients, "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "dest": f"{dest}:443",
                    "serverNames": [dest],
                    "privateKey": priv,
                    "shortIds": [sid] if sid else [],
                },
            },
            "sniffing": {"enabled": False},
        })
    cfg = {
        "log": {"loglevel": "warning"},
        # Force IPv4 DNS: some networks answer AAAA records first but have no
        # IPv6 egress, so connections hang forever (ping works, data never
        # flows — YouTube/Telegram never load). UseIPv4 fixes that.
        "dns": {"servers": ["8.8.8.8", "1.1.1.1"], "queryStrategy": "UseIPv4"},
        "inbounds": inbounds,
        "outbounds": [{"protocol": "freedom", "tag": "direct",
                       "domainStrategy": "UseIPv4"}],
    }
    try:
        os.makedirs(os.path.dirname(XRAY_CONFIG_PATH) or ".", exist_ok=True)
        with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return len(ws_clients)

def restart_xray():
    """Ask the start.sh loop to restart Xray with the new config."""
    import subprocess
    try:
        subprocess.run(["pkill", "-f", "xray run"], capture_output=True, timeout=5)
    except Exception:
        pass

def sync_and_restart_xray():
    if KATABUMP:
        return  # nothing to restart: the relay reads the DB live
    sync_xray_config()
    restart_xray()

def sub_url_for(user, settings):
    base = (settings.get("sub_base_url") or "").strip().rstrip("/")
    if not base:
        # Same Railway Host-header problem as default_server(): Flask sees
        # 127.0.0.1, so fall back to the public-domain env var first.
        pub = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
        if pub:
            base = "https://" + pub
    if not base:
        base = request.host_url.rstrip("/")
    # Railway serves the panel over https (TLS terminated at the edge), but
    # Flask behind the proxy sees http — force https so clients accept the sub.
    if base.startswith("http://") and ("railway.app" in base or request.headers.get("X-Forwarded-Proto") == "https"):
        base = "https://" + base[len("http://"):]
    return f"{base}/sub/{user['token']}"

def require_login():
    if session.get("admin") is True:
        return None
    return redirect(url_for("login", next=request.path))

# ---------------------------------------------------------------- theme / layout
CSS = """
*{box-sizing:border-box}
:root{
--bg1:#f6f1ff;--bg2:#ece4fb;--bg3:#ddcdfa;
--text:#2e1065;--muted:#5b21b6;--sub:#6b7280;
--card:rgba(255,255,255,.8);--card-border:rgba(76,29,149,.16);
--gold:#b45309;--gold2:#f59e0b;--gold3:#fbbf24;
--input-bg:rgba(255,255,255,.92);--input-text:#1e1b4b;--input-border:rgba(76,29,149,.28);
--mono-bg:rgba(76,29,149,.07);--mono-text:#4c1d95;
--link:#7c3aed;--ok:#15803d;--ok-bg:#dcfce7;--off:#b91c1c;--off-bg:#fee2e2;
--bg-opacity:.55;--shadow:0 10px 30px rgba(76,29,149,.12);
}
html.dark{
--bg1:#150826;--bg2:#2a0a4a;--bg3:#4c1d95;
--text:#f5f3ff;--muted:#ddd6fe;--sub:#c4b5fd;
--card:rgba(255,255,255,.08);--card-border:rgba(255,255,255,.16);
--gold:#fde68a;--gold2:#f59e0b;--gold3:#fbbf24;
--input-bg:rgba(0,0,0,.3);--input-text:#fff;--input-border:rgba(255,255,255,.25);
--mono-bg:rgba(0,0,0,.35);--mono-text:#e9d5ff;
--link:#fbbf24;--ok:#bbf7d0;--ok-bg:#14532d;--off:#fecaca;--off-bg:#7f1d1d;
--bg-opacity:.13;--shadow:0 10px 30px rgba(0,0,0,.45);
}
body{margin:0;font-family:'Vazirmatn',Tahoma,sans-serif;direction:rtl;
background:linear-gradient(160deg,var(--bg1) 0%,var(--bg2) 55%,var(--bg3) 100%);
color:var(--text);min-height:100vh;transition:background .5s ease,color .5s ease}
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
background:url(/static/bg.jpg) center 16%/min(920px,94vw) no-repeat;
opacity:var(--bg-opacity);transition:opacity .5s ease;
-webkit-mask-image:radial-gradient(ellipse 72% 62% at 50% 42%,#000 25%,transparent 78%);
mask-image:radial-gradient(ellipse 72% 62% at 50% 42%,#000 25%,transparent 78%)}
html.dark body::before{mix-blend-mode:screen;opacity:.2}
a{color:var(--link)}
.wrap{max-width:980px;margin:0 auto;padding:22px 16px 60px;position:relative;z-index:1}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;animation:fadeUp .5s ease both}
.emblem{width:72px;height:72px;border-radius:50%;flex:0 0 72px;overflow:hidden;background:#0f172a;
box-shadow:0 0 0 3px #fde68a,0 0 34px rgba(245,158,11,.65);
animation:floaty 5s ease-in-out infinite,glowPulse 4s ease-in-out infinite}
.emblem img{width:100%;height:100%;object-fit:cover;display:block}
h1{margin:0;font-size:34px;letter-spacing:3px;
background:linear-gradient(90deg,#b45309,#f59e0b,#b45309);
background-size:200% auto;-webkit-background-clip:text;background-clip:text;color:transparent;
animation:shimmer 6s linear infinite}
html.dark h1{background:linear-gradient(90deg,#fef08a,#fbbf24,#fde68a);background-size:200% auto;-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--muted);font-size:14px}
.flag{height:22px;border-radius:8px;display:flex;flex-direction:column;margin:16px 0 4px;position:relative;
box-shadow:0 2px 12px rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.25);animation:fadeUp .5s .08s ease both}
.flag div{flex:1}.flag .g{background:#239f40;border-radius:8px 8px 0 0}.flag .w{background:#fff}.flag .r{background:#da0001;border-radius:0 0 8px 8px}
.flag-emblem{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);height:38px;width:38px;border-radius:50%;box-shadow:0 0 0 2px rgba(255,255,255,.75),0 2px 10px rgba(0,0,0,.4)}
nav{display:flex;gap:10px;margin:18px 0;flex-wrap:wrap;animation:fadeUp .5s .14s ease both}
nav a{text-decoration:none;color:var(--text);background:var(--card);border:1px solid var(--card-border);padding:9px 16px;border-radius:999px;font-size:15px;transition:transform .25s ease,box-shadow .25s ease,background .25s ease}
nav a:hover{transform:translateY(-2px);box-shadow:var(--shadow)}
nav a.on{background:linear-gradient(90deg,#f59e0b,#fbbf24);color:#451a03;border-color:#fde68a;font-weight:700}
.card{background:var(--card);border:1px solid var(--card-border);border-radius:18px;padding:18px;margin:14px 0;backdrop-filter:blur(8px);box-shadow:var(--shadow);animation:fadeUp .55s .1s ease both;transition:transform .25s ease,box-shadow .25s ease,border-color .25s ease}
.card:hover{transform:translateY(-3px);border-color:rgba(245,158,11,.35)}
.card h2{margin:0 0 12px;font-size:20px;color:var(--gold)}
label{display:block;margin:12px 0 5px;color:var(--muted);font-size:14px}
input,select{width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--input-border);background:var(--input-bg);color:var(--input-text);font-family:inherit;font-size:16px;transition:border-color .25s ease,box-shadow .25s ease}
input:focus,select:focus{outline:none;border-color:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.3)}
.btn{display:inline-block;background:linear-gradient(90deg,#f59e0b,#fbbf24);color:#451a03;border:none;padding:12px 26px;border-radius:999px;font-weight:700;font-size:16px;cursor:pointer;font-family:inherit;transition:transform .2s ease,box-shadow .2s ease,filter .2s ease}
.btn:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(245,158,11,.45);filter:brightness(1.05)}
.btn:active{transform:scale(.97)}
.btn.ghost{background:var(--card);color:var(--text);border:1px solid var(--card-border)}
.btn.ghost:hover{box-shadow:var(--shadow)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:var(--card);border:1px solid var(--card-border);border-radius:16px;padding:14px;text-align:center;box-shadow:var(--shadow);animation:fadeUp .5s ease both;transition:transform .25s ease,box-shadow .25s ease}
.stat:nth-child(1){animation-delay:.05s}.stat:nth-child(2){animation-delay:.12s}.stat:nth-child(3){animation-delay:.19s}.stat:nth-child(4){animation-delay:.26s}
.stat:hover{transform:translateY(-3px)}
.stat b{font-size:26px;display:block;color:var(--gold)}
table{width:100%;border-collapse:collapse;font-size:14px}
tbody tr{animation:fadeIn .5s ease both}
th,td{padding:10px 6px;border-bottom:1px solid var(--card-border);text-align:right;vertical-align:top}
th{color:var(--gold)}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px}
.badge.ok{background:var(--ok-bg);color:var(--ok)}.badge.off{background:var(--off-bg);color:var(--off)}
.mono{direction:ltr;text-align:left;word-break:break-all;background:var(--mono-bg);border:1px solid var(--card-border);border-radius:12px;padding:12px;font-size:13px;color:var(--mono-text)}
.small{font-size:13px;color:var(--sub)}
.err{background:#7f1d1d;border:1px solid #fca5a5;border-radius:12px;padding:10px 14px;margin:10px 0;color:#fff}
.okmsg{background:#14532d;border:1px solid #86efac;border-radius:12px;padding:10px 14px;margin:10px 0;color:#fff}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.qr{background:#fff;padding:10px;border-radius:14px;display:inline-block}
.lionband{text-align:center;padding:8px 0 2px;font-size:15px;color:var(--gold);animation:fadeUp .5s .1s ease both}
.theme-btn{position:fixed;top:14px;left:14px;z-index:50;width:44px;height:44px;border-radius:50%;border:1px solid var(--card-border);background:var(--card);color:var(--text);font-size:20px;cursor:pointer;backdrop-filter:blur(8px);transition:transform .25s ease,box-shadow .25s ease}
.theme-btn:hover{transform:scale(1.1) rotate(-10deg);box-shadow:var(--shadow)}
.flagline{text-align:center;color:var(--gold);font-weight:700;font-size:16px}
.mini-flag{display:inline-flex;width:78px;height:24px;border-radius:5px;overflow:hidden;vertical-align:middle;box-shadow:0 2px 10px rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.5);margin:0 4px}
.mini-flag i{flex:1;display:block}.mini-flag .g{background:#239f40}.mini-flag .w{background:#fff;position:relative}.mini-flag .r{background:#da0001}
.mini-flag .w img{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);height:32px;width:32px;border-radius:50%}
.flagbig .emblem{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:64px;height:64px;animation:none}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes floaty{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
@keyframes shimmer{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
@keyframes glowPulse{0%,100%{box-shadow:0 0 0 3px #fde68a,0 0 24px rgba(245,158,11,.45)}50%{box-shadow:0 0 0 3px #fde68a,0 0 44px rgba(245,158,11,.75)}}
@keyframes barGrow{from{width:0}}
@media(max-width:640px){h1{font-size:26px}.emblem{width:56px;height:56px;flex-basis:56px}table{font-size:12px}.theme-btn{top:10px;left:10px;width:40px;height:40px;font-size:18px}}
"""
THEME_HEAD = """<script>try{var _t=localStorage.getItem('nexuz-theme');if(_t==='dark'){document.documentElement.classList.add('dark')}}catch(_e){}</script>"""

THEME_JS = """<script>
function toggleTheme(){var h=document.documentElement;var d=h.classList.toggle('dark');try{localStorage.setItem('nexuz-theme',d?'dark':'light')}catch(e){}var b=document.getElementById('themeBtn');if(b)b.textContent=d?'☀️':'🌙';}
(function(){var b=document.getElementById('themeBtn');if(b)b.textContent=document.documentElement.classList.contains('dark')?'☀️':'🌙';})();
</script>"""

BASE = """<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} | NEXUZ 🦁☀️</title>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;700;900&display=swap" rel="stylesheet">
<style>{{ css }}</style>{{ theme_head|safe }}</head><body><button id="themeBtn" class="theme-btn" onclick="toggleTheme()" title="حالت تیره / روشن">🌙</button><div class="wrap">
<header><div class="emblem"><img src="/static/logo.png" alt="NEXUZ"></div>
<div><h1>{{ panel_name }}</h1><div class="sub">شیر و خورشید 🦁☀️ &nbsp;|&nbsp; پنل V2Ray</div></div></header>
<div class="flag"><div class="g"></div><div class="w"></div><div class="r"></div><img class="flag-emblem" src="/static/logo.png" alt=""></div>
<div class="lionband">پرچم شیر و خورشید — افتخار ایران ☀️🦁</div>
<nav>
<a href="/" class="{{ 'on' if active=='home' else '' }}">🏠 صفحه اصلی</a>
<a href="/create" class="{{ 'on' if active=='create' else '' }}">➕ ایجاد کاربر</a>
<a href="/settings" class="{{ 'on' if active=='settings' else '' }}">⚙️ تنظیمات</a>
</nav>
{{ content|safe }}
<p class="small" style="text-align:center;margin-top:30px">NEXUZ 🦁☀️ — شیر و خورشید</p>
</div>{{ theme_js|safe }}</body></html>"""

def page(title, active, content):
    return render_template_string(
        BASE, title=title, active=active, content=content,
        css=CSS, panel_name=get_setting("panel_name") or "NEXUZ",
        theme_head=THEME_HEAD, theme_js=THEME_JS)

def copy_btn(text, label="📋 کپی"):
    esc = text.replace("'", "\\'").replace("\n", "\\n")
    return (f"<button class='btn ghost' onclick=\"navigator.clipboard.writeText('{esc}');"
            f"this.textContent='✅ کپی شد';setTimeout(()=>this.textContent='{label}',1500)\">{label}</button>")

# ---------------------------------------------------------------- routes
@app.before_request
def _init():
    init_db()

@app.route("/health")
def health():
    return "ok", 200

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if check_admin_password(request.form.get("password") or ""):
            session["admin"] = True
            return redirect(request.args.get("next") or "/")
        error = "رمز اشتباه است، دوباره امتحان کن."
    return render_template_string(f"""<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ورود | NEXUZ 🦁☀️</title>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;700;900&display=swap" rel="stylesheet">
<style>{CSS}
.loginbox{{max-width:420px;margin:9vh auto 0;text-align:center}}
.loginbox .emblem{{width:110px;height:110px;flex-basis:110px;font-size:58px;margin:0 auto 10px}}
.loginbox .emblem span{{font-size:32px;top:-12px;left:-6px}}
.loginbox h1{{font-size:44px}}
.loginbox .card{{padding:28px 24px}}
.loginbox input{{text-align:center;font-size:20px;letter-spacing:2px;padding:14px}}
.loginbox .btn{{width:100%;padding:14px;font-size:18px;margin-top:16px}}
</style>{THEME_HEAD}</head><body><button id="themeBtn" class="theme-btn" onclick="toggleTheme()" title="حالت تیره / روشن">🌙</button><div class="wrap"><div class="loginbox">
<div class="emblem"><img src="/static/logo.png" alt="NEXUZ"></div>
<h1>NEXUZ</h1><div class="sub">شیر و خورشید 🦁☀️ &nbsp;|&nbsp; پنل V2Ray</div>
<div class="flag"><div class="g"></div><div class="w"></div><div class="r"></div><img class="flag-emblem" src="/static/logo.png" alt=""></div>
<div class="card"><h2>🔐 ورود به پنل</h2>
{f'<div class="err">{error}</div>' if error else ''}
<form method="post"><label style="text-align:right">رمز ورود</label>
<input type="password" name="password" required autofocus placeholder="••••••••" autocomplete="current-password">
<button class="btn">🦁 ورود</button></form>
<p class="small" style="margin-top:14px">رمز اولیه: <b>admin</b> — بعد از ورود از تنظیمات عوضش کن.</p>
</div></div></div>{THEME_JS}</body></html>""")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/")
def home():
    chk = require_login()
    if chk: return chk
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    settings = get_all_settings()
    total = len(users)
    active_n = sum(1 for u in users if is_active(u))
    total_vol = sum(float(u["volume_gb"]) for u in users)
    used_vol = sum(float(u["used_gb"]) for u in users)
    if KATABUMP:
        # The WS relay runs inside the panel process itself (main.py), so
        # if this page renders, the core is up.
        xray_badge = "🟢 هسته WS فعال است"
    else:
        import subprocess as _sp
        try:
            _r = _sp.run(["pgrep", "-f", "xray run"], capture_output=True, timeout=3)
            _r2 = _sp.run(["pgrep", "-f", "ws_relay.py"], capture_output=True, timeout=3)
            xray_on = _r.returncode == 0 and _r2.returncode == 0
        except Exception:
            xray_on = False
        xray_badge = "🟢 هسته V2Ray فعال است" if xray_on else "🔴 هسته V2Ray خاموش است"
    rows = ""
    for u in users:
        days = remaining_days(u)
        try:
            stopped = bool(u["stopped"])
        except Exception:
            stopped = False
        if stopped:
            st = "<span class='badge off'>متوقف ⏸️</span>"
        elif is_active(u):
            st = "<span class='badge ok'>فعال ✅</span>"
        else:
            st = "<span class='badge off'>منقضی ⛔</span>"
        vless = build_vless(u, settings)
        reality = build_vless(u, settings, kind="reality")
        xhttp = build_vless(u, settings, kind="xhttp")
        sub = sub_url_for(u, settings)
        extra_btns = ""
        if reality:
            extra_btns += copy_btn(reality, '🔒 Reality')
        if xhttp:
            extra_btns += copy_btn(xhttp, '⚡ XHTTP')
        stop_label = "▶️ شروع" if stopped else "⏸️ توقف"
        rows += f"""<tr><td><b>{u['username']}</b><br><span class="small">{st}</span></td>
        <td>{float(u['volume_gb']):g} GB</td><td>{float(u['used_gb']):g} GB</td><td>{days} روز<br><span class="small">تا {fmt_date(u['expires_at'])}</span></td>
        <td><div class="row">{copy_btn(vless, '📋 کانفیگ')}{copy_btn(sub, '🔗 ساب')}{extra_btns}</div>
        <div style="height:6px"></div>
        <div class="row">
        <form method="post" action="/user/{u['id']}/toggle-stop">
        <button class="btn ghost" style="padding:6px 14px;font-size:13px">{stop_label}</button></form>
        <form method="post" action="/user/{u['id']}/delete" onsubmit="return confirm('کاربر {u['username']} حذف شود؟')">
        <button class="btn ghost" style="padding:6px 14px;font-size:13px">🗑️ حذف</button></form></div></td></tr>"""
    tun = tunnel_url()
    tunnel_line = ""
    if tun:
        from html import escape as _esc
        tunnel_line = (f"<p class=\"small\" style=\"text-align:center\">🌐 آدرس تونل کلادفلر: "
                       f"<a href=\"{_esc(tun)}\" dir=\"ltr\">{_esc(tun)}</a> — با همین آدرس وارد شو تا لینک‌ها TLS بشن</p>")
    content = f"""
    <div class="stats">
      <div class="stat"><b>{total}</b>کل کاربران 👥</div>
      <div class="stat"><b>{active_n}</b>کاربر فعال ✅</div>
      <div class="stat"><b>{total_vol:g} GB</b>حجم کل 📦</div>
      <div class="stat"><b>{used_vol:g} GB</b>حجم مصرفی 📊</div>
    </div>
    <p class="small" style="text-align:center">{xray_badge} &nbsp;|&nbsp; سرور کانفیگ: دامنه همین پنل {'(هسته WS روی همین سرور است)' if KATABUMP else '(هسته V2Ray روی همین سرور نصب است)'}</p>
    {tunnel_line}
    <div class="card"><h2>👥 کاربران</h2>
    <input id="q" placeholder="🔍 جستجوی کاربر..." oninput="document.querySelectorAll('tbody tr').forEach(r=>r.style.display=r.textContent.includes(this.value)?'':'none')">
    <div style="overflow-x:auto"><table><thead><tr><th>کاربر</th><th>حجم</th><th>مصرف‌شده</th><th>زمان باقی‌مانده</th><th>اقدام</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="5">هنوز کاربری ساخته نشده — از صفحه «ایجاد کاربر» شروع کن ➕</td></tr>'}</tbody></table></div>
    <p class="small">💡 کانفیگ اصلی (📋 کانفیگ) همان مدل WhitePanel است — VLESS روی WebSocket با مسیر /ws/شناسه کاربر{'؛ روی KataBump فقط همین کانفیگ کار می‌کند.' if KATABUMP else '؛ اگر Reality را می‌خواهی، در «تنظیمات» آدرس TCP Proxy ریل‌وی را وارد کن.'}</p></div>
    <script>/* search uses inline handler above */</script>"""
    return page("صفحه اصلی", "home", content)

@app.route("/create", methods=["GET", "POST"])
def create():
    chk = require_login()
    if chk: return chk
    settings = get_all_settings()
    error = ""
    result = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        try:
            days = int(request.form.get("days") or 30)
            volume = float(request.form.get("volume_gb") or 10)
        except ValueError:
            days, volume = 30, 10.0
        days = max(1, min(days, 3650))
        volume = max(0.1, min(volume, 10000))
        if len(username) < 2:
            error = "نام کاربری باید حداقل ۲ کاراکتر باشد."
        else:
            db = get_db()
            try:
                uid = str(uuid.uuid4())
                token = secrets.token_urlsafe(16)
                created = now()
                expires = created + timedelta(days=days)
                db.execute("""INSERT INTO users (username, uuid, token, volume_gb, used_gb,
                    days, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (username, uid, token, volume, 0, days,
                     created.isoformat(), expires.isoformat()))
                db.commit()
                sync_and_restart_xray()
                u = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                vless = build_vless(u, settings)
                reality = build_vless(u, settings, kind="reality")
                xhttp = build_vless(u, settings, kind="xhttp")
                sub = sub_url_for(u, settings)
                def _cfg_block(link, title, note=""):
                    if not link:
                        return ""
                    return (f'<p class="small" style="margin-top:12px">{title}</p>'
                            f'<div class="mono">{link}</div><br>'
                            f'<div class="row">{copy_btn(link, "📋 کپی")}</div>'
                            f'<p class="small">{note}</p>' if note else
                            f'<p class="small" style="margin-top:12px">{title}</p>'
                            f'<div class="mono">{link}</div><br>'
                            f'<div class="row">{copy_btn(link, "📋 کپی")}</div>')
                result = f"""
                <div class="okmsg">✅ کاربر <b>{username}</b> ساخته شد!</div>
                <div class="card"><h2>🦁☀️ کانفیگ کاربر — شیر و خورشید</h2>
                <p>👤 نام کاربری: <b>{username}</b><br>📦 حجم: <b>{volume:g} گیگابایت</b><br>
                ⏳ مدت: <b>{days} روز</b> (تا {fmt_date(u['expires_at'])})</p>
                {_cfg_block(vless, '📋 <b>کانفیگ VLESS (WebSocket) — اول این را تست کن:</b>', 'همان مدل WhitePanel است: مسیر /ws/شناسه کاربر، روی دامنه همین پنل.')}
                {_cfg_block(xhttp, '⚡ <b>کانفیگ XHTTP:</b>', 'روی پورت 443 و از مسیر HTTPS پنل می‌آید؛ اگر WS وصل نشد این را تست کن.')}
                {_cfg_block(reality, '🔒 <b>کانفیگ Reality:</b>', 'دستگاه را طوری نشان می‌دهد که انگار به snapp.ir وصل است. اگر TCP Proxy در تنظیمات وارد نشده باشد، این کانفیگ نمایش داده نمی‌شود.')}
                <p class="small" style="margin-top:12px">ساب‌لینک (همه کانفیگ‌ها داخلش هست):</p><div class="mono">{sub}</div>
                <div class="row">{copy_btn(sub, '🔗 کپی ساب‌لینک')}</div>
                <p class="small">داخل ساب‌لینک، به ترتیب: 👤 نام کاربری، 📦 حجم، ⏳ زمان باقی‌مانده، و بعد خود کانفیگ‌ها{' (فقط WS)' if KATABUMP else ' (WS، بعد XHTTP، بعد Reality)'}.</p>
                <div id="qr" class="qr"></div>
                <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
                <script>new QRCode(document.getElementById("qr"),{{text:"{vless}",width:180,height:180}});</script>
                </div>"""
            except sqlite3.IntegrityError:
                error = f"کاربری با نام «{username}» قبلاً ساخته شده است."
    form = f"""
    <div class="card"><h2>➕ ایجاد کاربر جدید</h2>
    {f'<div class="err">{error}</div>' if error else ''}
    <form method="post">
      <label>👤 نام کاربری</label><input name="username" required minlength="2" placeholder="مثلاً ali123">
      <label>⏳ مدت زمان (روز)</label><input name="days" type="number" value="30" min="1" max="3650" required>
      <label>📦 حجم (گیگابایت)</label><input name="volume_gb" type="number" step="any" value="10" min="0.1" required>
      <div style="height:14px"></div><button class="btn">🦁 ساخت کاربر + کانفیگ VLESS</button>
    </form></div>
    {result}"""
    return page("ایجاد کاربر", "create", form)

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    chk = require_login()
    if chk: return chk
    msg = ""
    if request.method == "POST":
        new_pw = (request.form.get("new_password") or "").strip()
        new_pw2 = (request.form.get("new_password2") or "").strip()
        if new_pw or new_pw2:
            if len(new_pw) < 4:
                msg = "<div class='err'>رمز جدید باید حداقل ۴ کاراکتر باشد.</div>"
            elif new_pw != new_pw2:
                msg = "<div class='err'>تکرار رمز با رمز جدید یکی نیست.</div>"
            elif ADMIN_PASSWORD:
                msg = "<div class='err'>رمز از متغیر محیطی ADMIN_PASSWORD آمده و از اینجا عوض نمی‌شود.</div>"
            else:
                set_setting("admin_password_hash", _sha(new_pw))
                msg = "<div class='okmsg'>✅ رمز ورود عوض شد.</div>"
        if "panel_name" in request.form:
            set_setting("panel_name", (request.form.get("panel_name") or "NEXUZ").strip() or "NEXUZ")
            set_setting("server_address", (request.form.get("server_address") or "").strip())
            try:
                port = int(request.form.get("port") or 443)
                set_setting("port", str(max(1, min(port, 65535))))
            except ValueError:
                pass
            set_setting("sni", (request.form.get("sni") or "").strip())
            set_setting("ws_path", (request.form.get("ws_path") or "nexuz").strip().lstrip("/") or "nexuz")
            set_setting("network", request.form.get("network") or "ws")
            set_setting("security", request.form.get("security") or "tls")
            set_setting("sub_base_url", (request.form.get("sub_base_url") or "").strip())
            unit = (request.form.get("sub_volume_unit") or "GB").upper()
            set_setting("sub_volume_unit", "MB" if unit == "MB" else "GB")
            # Reality (raw TCP via Railway TCP Proxy) — the strong transport.
            set_setting("reality_dest", (request.form.get("reality_dest") or "snapp.ir").strip() or "snapp.ir")
            set_setting("reality_host", (request.form.get("reality_host") or "").strip())
            try:
                rport = int(request.form.get("reality_port") or 0)
                set_setting("reality_port", str(rport) if 1 <= rport <= 65535 else "")
            except ValueError:
                pass
            # After saving the proxy endpoint, rebuild Xray + Reality links.
            sync_and_restart_xray()
        if not msg:
            msg = "<div class='okmsg'>✅ تنظیمات ذخیره شد.</div>"
    s = get_all_settings()
    base = (s.get("sub_base_url") or request.host_url.rstrip("/"))
    content = f"""
    <div class="card"><h2>⚙️ تنظیمات</h2>{msg}
    <form method="post">
      <label>نام پنل</label><input name="panel_name" value="{s['panel_name']}">
      <label>🌐 آدرس سرور V2Ray (خالی = دامنه همین پنل؛ هسته V2Ray روی همین سرور نصب است و کانفیگ با دامنه پنل ساخته می‌شود)</label>
      <input name="server_address" value="{s['server_address']}" placeholder="خالی بگذار — دامنه پنل استفاده می‌شود" dir="ltr" style="text-align:left">
      <label>🔌 پورت</label><input name="port" type="number" value="{s['port']}">
      <label>🔒 SNI (خالی = مثل آدرس سرور)</label><input name="sni" value="{s['sni']}" dir="ltr" style="text-align:left" placeholder="مثلاً vpn.example.com">
      <p class="small">🛤️ مسیر WebSocket ثابت است: <b>/nexuz</b> (هسته V2Ray روی همین مسیر گوش می‌دهد).</p>
      <label>📡 شبکه (Network)</label>
      <select name="network"><option value="ws" {'selected' if s['network']=='ws' else ''}>ws (WebSocket)</option>
      <option value="tcp" {'selected' if s['network']=='tcp' else ''}>tcp</option></select>
      <label>🔐 امنیت (Security)</label>
      <select name="security"><option value="tls" {'selected' if s['security']=='tls' else ''}>tls</option>
      <option value="none" {'selected' if s['security']=='none' else ''}>none</option></select>
      <label>🔗 آدرس پایه ساب‌لینک (خالی = همین دامنه پنل)</label>
      <input name="sub_base_url" value="{s['sub_base_url']}" dir="ltr" style="text-align:left" placeholder="https://your-panel.up.railway.app">
      <label>📦 واحد حجم مصرفی در ساب‌لینک</label>
      <select name="sub_volume_unit"><option value="GB" {'selected' if (s.get('sub_volume_unit') or 'GB')=='GB' else ''}>گیگابایت (GB)</option>
      <option value="MB" {'selected' if (s.get('sub_volume_unit') or 'GB')=='MB' else ''}>مگابایت (MB)</option></select>
      <hr style="border-color:rgba(255,255,255,.15);margin:18px 0">
      <p class="small">🔒 <b>Reality (قدرتمندترین کانفیگ — برای ایران):</b> ترافیک Reality دقیقاً مثل اتصال TLS به سایت پوششی به نظر می‌رسد و DPI نمی‌تواند آن را خفه کند. برای فعال شدن، در Railway سرویس → Settings → Networking → <b>TCP Proxy</b> را روی پورت داخلی <b>8443</b> فعال کن و آدرس و پورتی که Railway می‌دهد را اینجا وارد کن.</p>
      <label>🌐 دامنه پوششی Reality (سایتی پرترافیک که کسی نمی‌تواند فیلترش کند)</label>
      <input name="reality_dest" value="{s.get('reality_dest') or 'snapp.ir'}" dir="ltr" style="text-align:left" placeholder="snapp.ir">
      <label>📡 آدرس TCP Proxy ریل‌وی (مثل xxx.proxy.rlwy.net)</label>
      <input name="reality_host" value="{s.get('reality_host') or ''}" dir="ltr" style="text-align:left" placeholder="xxx.proxy.rlwy.net">
      <label>🔌 پورت TCP Proxy ریل‌وی</label>
      <input name="reality_port" type="number" value="{s.get('reality_port') or ''}" dir="ltr" style="text-align:left" placeholder="مثلاً 15140">
      <p class="small">کلید عمومی Reality: <span style="direction:ltr;display:inline-block">{(s.get('reality_public_key') or '— هنوز ساخته نشده —')[:24]}…</span> &nbsp;|&nbsp; shortId: <span style="direction:ltr;display:inline-block">{s.get('reality_short_id') or '—'}</span></p>
      <div style="height:14px"></div><button class="btn">💾 ذخیره تنظیمات</button>
    </form>
    </div>
    <div class="card"><h2>🔐 تغییر رمز ورود پنل</h2>
    <p class="small">رمز فعلی فقط با رمز جدید عوض می‌شود؛ اگر هر دو فیلد خالی بماند، رمز عوض نمی‌شود.</p>
    <form method="post">
      <label>رمز جدید</label><input type="password" name="new_password" autocomplete="new-password">
      <label>تکرار رمز جدید</label><input type="password" name="new_password2" autocomplete="new-password">
      <div style="height:14px"></div><button class="btn">🔐 تغییر رمز</button>
    </form></div>
    <div class="card"><p class="small">نمونه ساب‌لینک: <span style="direction:ltr;display:inline-block">{base}/sub/&lt;token&gt;</span><br>
    💡 کانفیگ‌ها با دامنه همین پنل ساخته می‌شوند؛ برای Reality، آدرس TCP Proxy را در بخش Reality همین صفحه وارد کن.</p></div>"""
    return page("تنظیمات", "settings", content)

@app.route("/user/<int:user_id>/toggle-stop", methods=["POST"])
def toggle_stop(user_id):
    chk = require_login()
    if chk: return chk
    db = get_db()
    u = db.execute("SELECT stopped FROM users WHERE id=?", (user_id,)).fetchone()
    if u is not None:
        try:
            cur = int(u["stopped"])
        except Exception:
            cur = 0
        db.execute("UPDATE users SET stopped=? WHERE id=?", (0 if cur else 1, user_id))
        db.commit()
        # Stopped users leave Xray's client list (config stops working);
        # started users come back (config works again).
        sync_and_restart_xray()
    return redirect("/")

@app.route("/user/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    chk = require_login()
    if chk: return chk
    db = get_db()
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    sync_and_restart_xray()
    return redirect("/")

@app.route("/sub/<token>")
def sub(token):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if not user:
        return Response("User not found", status=404, mimetype="text/plain")
    settings = get_all_settings()
    lines = build_sub_lines(user, settings)
    ua = (request.headers.get("User-Agent") or "").lower()
    wants_html = "view=1" in request.query_string.decode() or (
        "mozilla" in ua and "text/html" in (request.headers.get("Accept") or ""))
    if request.args.get("format") == "base64":
        import base64
        body = base64.b64encode(("\n".join(lines)).encode()).decode()
        return Response(body, mimetype="text/plain")
    if not wants_html:
        return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")
    # Browser view — full Lion & Sun page
    vol = float(user["volume_gb"]); left = max(0.0, vol - float(user["used_gb"]))
    days = remaining_days(user)
    pct = int(left / vol * 100) if vol else 0
    vless = build_vless(user, settings)
    reality = build_vless(user, settings, kind="reality")
    xhttp = build_vless(user, settings, kind="xhttp")
    sub_url = sub_url_for(user, settings)
    html = f"""<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🦁☀️ ساب‌لینک {user['username']} | NEXUZ</title>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;700;900&display=swap" rel="stylesheet">
<style>{CSS}
.flagbig{{height:90px;border-radius:16px;overflow:hidden;position:relative;display:flex;flex-direction:column;
box-shadow:0 6px 30px rgba(0,0,0,.5);border:2px solid #fbbf24}}
.flagbig div{{flex:1}}.flagbig .g{{background:#239f40}}.flagbig .w{{background:#fff}}.flagbig .r{{background:#da0001}}
.flagbig .emblem{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:64px;height:64px;animation:none}}
.info{{font-size:19px;line-height:2.1}}
.bar{{height:14px;background:rgba(0,0,0,.35);border-radius:999px;overflow:hidden}}
.bar i{{display:block;height:100%;background:linear-gradient(90deg,#f59e0b,#fbbf24);width:{pct}%;animation:barGrow 1s ease both}}
</style>{THEME_HEAD}</head><body><button id="themeBtn" class="theme-btn" onclick="toggleTheme()" title="حالت تیره / روشن">🌙</button><div class="wrap" style="max-width:640px">
<header><div class="emblem"><img src="/static/logo.png" alt="NEXUZ"></div>
<div><h1>NEXUZ</h1><div class="sub">شیر و خورشید 🦁☀️</div></div></header>
<div class="flagbig"><div class="g"></div><div class="w"></div><div class="r"></div>
<div class="emblem"><img src="/static/logo.png" alt="NEXUZ"></div></div>
<p class="flagline"><span class="mini-flag"><i class="g"></i><i class="w"><img src="/static/logo.png" alt=""></i><i class="r"></i></span> پرچم شیر و خورشید</p>
<div class="card"><h2>🦁☀️ اشتراک شما</h2><div class="info">
👤 <b>نام کاربری:</b> {user['username']}<br>
{format_volume_text(user, settings)}<br>
⏳ <b>زمان باقی‌مانده:</b> {days} روز (تا {fmt_date(user['expires_at'])})<br>
📊 <b>حجم باقی‌مانده:</b> {pct}٪</div>
<div class="bar"><i></i></div>
<p class="small" style="margin-top:14px">کانفیگ VLESS:</p><div class="mono">{vless}</div><br>
<div class="row">{copy_btn(vless, '📋 کپی کانفیگ')}{copy_btn(sub_url, '🔗 کپی ساب‌لینک')}</div>
{f'<p class="small" style="margin-top:14px">🔒 کانفیگ Reality (اول این را تست کن):</p><div class="mono">{reality}</div><br><div class="row">{copy_btn(reality, "📋 کپی Reality")}</div>' if reality else ''}
{f'<p class="small" style="margin-top:14px">⚡ کانفیگ XHTTP:</p><div class="mono">{xhttp}</div><br><div class="row">{copy_btn(xhttp, "📋 کپی XHTTP")}</div>' if xhttp else ''}
<p class="small">این ساب‌لینک را در v2rayNG / Streisand / FoXray وارد کن — اول سه خط اطلاعات (👤 📦 ⏳) و بعد خود کانفیگ‌ها نمایش داده می‌شود.</p>
<div id="qr" class="qr"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>new QRCode(document.getElementById("qr"),{{text:"{vless}",width:180,height:180}});</script>
</div></div>{THEME_JS}</body></html>"""
    return Response(html, mimetype="text/html; charset=utf-8")

if __name__ == "__main__":
    init_db()
    try:
        sync_xray_config()
    except Exception:
        pass
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
else:
    init_db()
    try:
        sync_xray_config()
    except Exception:
        pass
