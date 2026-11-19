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
# Optional env: ADMIN_PASSWORD — if set, the panel asks for it on login.
#              DATA_DIR — default /data when it exists, else local folder.

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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "nexuz.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

DEFAULT_SETTINGS = {
    "panel_name": "NEXUZ",
    "server_address": "",
    "port": "443",
    "sni": "",
    "ws_path": "nexuz",
    "network": "ws",
    "security": "tls",
    "sub_base_url": "",
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
    return bool(user["active"]) and remaining_days(user) > 0

def fmt_date(s):
    return parse_dt(s).strftime("%Y-%m-%d")

def build_vless(user, settings, server=None):
    """Real VLESS URI for a user."""
    server = (server or settings.get("server_address") or "your-server.com").strip()
    port = settings.get("port") or "443"
    sni = (settings.get("sni") or server).strip()
    path = (settings.get("ws_path") or "nexuz").strip().lstrip("/")
    network = settings.get("network") or "ws"
    security = settings.get("security") or "tls"
    params = f"encryption=none&security={security}&sni={quote(sni)}&fp=chrome&type={network}"
    if network == "ws":
        params += f"&path=%2F{quote(path)}&host={quote(server)}"
    return (f"vless://{user['uuid']}@{server}:{port}?{params}"
            f"#{quote(user['username'])}")

def dummy_vless(remark):
    """Info entry: shows in the client list, connects to nothing."""
    return ("vless://00000000-0000-0000-0000-000000000000"
            f"@127.0.0.1:1?encryption=none&security=none&type=tcp#{quote(remark)}")

def build_sub_lines(user, settings):
    vol = float(user["volume_gb"])
    left = max(0.0, vol - float(user["used_gb"]))
    days = remaining_days(user)
    lines = [
        dummy_vless(f"👤 نام کاربری: {user['username']}"),
        dummy_vless(f"📦 حجم: {vol:g} گیگابایت | باقی‌مانده: {left:g} گیگابایت"),
        dummy_vless(f"⏳ زمان باقی‌مانده: {days} روز"),
        build_vless(user, settings),
    ]
    return lines

def sub_url_for(user, settings):
    base = (settings.get("sub_base_url") or "").strip().rstrip("/")
    if not base:
        base = request.host_url.rstrip("/")
    return f"{base}/sub/{user['token']}"

def require_login():
    if not ADMIN_PASSWORD:
        return None
    if session.get("admin") is True:
        return None
    return redirect(url_for("login", next=request.path))

# ---------------------------------------------------------------- theme / layout
CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:'Vazirmatn',Tahoma,sans-serif;direction:rtl;
background:radial-gradient(1000px 600px at 85% -10%,#a855f7 0%,transparent 55%),
radial-gradient(900px 500px at 0% 110%,#6d28d9 0%,transparent 50%),
linear-gradient(160deg,#1a0533 0%,#2e1065 45%,#4c1d95 80%,#5b21b6 100%);
color:#f5f3ff;min-height:100vh}
a{color:#fbbf24}
.wrap{max-width:980px;margin:0 auto;padding:22px 16px 60px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.emblem{width:72px;height:72px;border-radius:50%;flex:0 0 72px;
background:radial-gradient(circle at 35% 30%,#fef08a,#f59e0b 55%,#b45309);
display:flex;align-items:center;justify-content:center;font-size:38px;
box-shadow:0 0 0 3px #fde68a,0 0 34px rgba(245,158,11,.65);position:relative}
.emblem span{position:absolute;top:-9px;left:-4px;font-size:22px}
h1{margin:0;font-size:34px;letter-spacing:3px;
background:linear-gradient(90deg,#fef08a,#fbbf24,#fde68a);
-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:#ddd6fe;font-size:14px}
.flag{height:14px;border-radius:8px;overflow:hidden;display:flex;margin:14px 0 4px;
box-shadow:0 2px 12px rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.25)}
.flag div{flex:1}.flag .g{background:#239f40}.flag .w{background:#fff}.flag .r{background:#da0001}
nav{display:flex;gap:10px;margin:18px 0;flex-wrap:wrap}
nav a{text-decoration:none;color:#ede9fe;background:rgba(255,255,255,.09);
border:1px solid rgba(255,255,255,.18);padding:9px 16px;border-radius:999px;font-size:15px}
nav a.on{background:linear-gradient(90deg,#f59e0b,#fbbf24);color:#451a03;border-color:#fde68a;font-weight:700}
.card{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.16);
border-radius:18px;padding:18px;margin:14px 0;backdrop-filter:blur(8px)}
.card h2{margin:0 0 12px;font-size:20px;color:#fde68a}
label{display:block;margin:12px 0 5px;color:#ddd6fe;font-size:14px}
input,select{width:100%;padding:11px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.25);
background:rgba(0,0,0,.3);color:#fff;font-family:inherit;font-size:16px}
.btn{display:inline-block;background:linear-gradient(90deg,#f59e0b,#fbbf24);color:#451a03;
border:none;padding:12px 26px;border-radius:999px;font-weight:700;font-size:16px;cursor:pointer;font-family:inherit}
.btn.ghost{background:rgba(255,255,255,.1);color:#fff;border:1px solid rgba(255,255,255,.25)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.16);border-radius:16px;padding:14px;text-align:center}
.stat b{font-size:26px;display:block;color:#fde68a}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:10px 6px;border-bottom:1px solid rgba(255,255,255,.12);text-align:right;vertical-align:top}
th{color:#fde68a}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px}
.badge.ok{background:#14532d;color:#bbf7d0}.badge.off{background:#7f1d1d;color:#fecaca}
.mono{direction:ltr;text-align:left;word-break:break-all;background:rgba(0,0,0,.35);
border:1px solid rgba(255,255,255,.18);border-radius:12px;padding:12px;font-size:13px;color:#e9d5ff}
.small{font-size:13px;color:#c4b5fd}
.err{background:#7f1d1d;border:1px solid #fca5a5;border-radius:12px;padding:10px 14px;margin:10px 0}
.okmsg{background:#14532d;border:1px solid #86efac;border-radius:12px;padding:10px 14px;margin:10px 0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.qr{background:#fff;padding:10px;border-radius:14px;display:inline-block}
.lionband{text-align:center;padding:8px 0 2px;font-size:15px;color:#fde68a}
@media(max-width:640px){h1{font-size:26px}.emblem{width:56px;height:56px;flex-basis:56px;font-size:30px}table{font-size:12px}}
"""

BASE = """<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} | NEXUZ 🦁☀️</title>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;700;900&display=swap" rel="stylesheet">
<style>{{ css }}</style></head><body><div class="wrap">
<header><div class="emblem">🦁<span>☀️</span></div>
<div><h1>{{ panel_name }}</h1><div class="sub">شیر و خورشید 🦁☀️ &nbsp;|&nbsp; پنل V2Ray</div></div></header>
<div class="flag"><div class="g"></div><div class="w"></div><div class="r"></div></div>
<div class="lionband">پرچم شیر و خورشید — افتخار ایران ☀️🦁</div>
<nav>
<a href="/" class="{{ 'on' if active=='home' else '' }}">🏠 صفحه اصلی</a>
<a href="/create" class="{{ 'on' if active=='create' else '' }}">➕ ایجاد کاربر</a>
<a href="/settings" class="{{ 'on' if active=='settings' else '' }}">⚙️ تنظیمات</a>
</nav>
{{ content|safe }}
<p class="small" style="text-align:center;margin-top:30px">NEXUZ 🦁☀️ — شیر و خورشید</p>
</div></body></html>"""

def page(title, active, content):
    return render_template_string(
        BASE, title=title, active=active, content=content,
        css=CSS, panel_name=get_setting("panel_name") or "NEXUZ")

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
    if request.method == "POST" and request.form.get("password") == ADMIN_PASSWORD:
        session["admin"] = True
        return redirect(request.args.get("next") or "/")
    return page("ورود", "home", """
        <div class="card"><h2>🔐 ورود به پنل</h2>
        <form method="post"><label>رمز مدیریت</label>
        <input type="password" name="password" required>
        <div style="height:12px"></div><button class="btn">ورود</button></form></div>""")

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
    rows = ""
    for u in users:
        days = remaining_days(u)
        st = "<span class='badge ok'>فعال ✅</span>" if is_active(u) else "<span class='badge off'>منقضی ⛔</span>"
        vless = build_vless(u, settings)
        sub = sub_url_for(u, settings)
        rows += f"""<tr><td><b>{u['username']}</b><br><span class="small">{st}</span></td>
        <td>{float(u['volume_gb']):g} GB</td><td>{days} روز<br><span class="small">تا {fmt_date(u['expires_at'])}</span></td>
        <td><div class="row">{copy_btn(vless, '📋 کانفیگ')}{copy_btn(sub, '🔗 ساب')}</div>
        <div style="height:6px"></div>
        <form method="post" action="/user/{u['id']}/delete" onsubmit="return confirm('کاربر {u['username']} حذف شود؟')">
        <button class="btn ghost" style="padding:6px 14px;font-size:13px">🗑️ حذف</button></form></td></tr>"""
    content = f"""
    <div class="stats">
      <div class="stat"><b>{total}</b>کل کاربران 👥</div>
      <div class="stat"><b>{active_n}</b>کاربر فعال ✅</div>
      <div class="stat"><b>{total_vol:g} GB</b>حجم کل 📦</div>
    </div>
    <div class="card"><h2>👥 کاربران</h2>
    <input id="q" placeholder="🔍 جستجوی کاربر..." oninput="document.querySelectorAll('tbody tr').forEach(r=>r.style.display=r.textContent.includes(this.value)?'':'none')">
    <div style="overflow-x:auto"><table><thead><tr><th>کاربر</th><th>حجم</th><th>زمان باقی‌مانده</th><th>اقدام</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="4">هنوز کاربری ساخته نشده — از صفحه «ایجاد کاربر» شروع کن ➕</td></tr>'}</tbody></table></div>
    <p class="small">💡 برای اینکه کانفیگ ساخته شود، اول در «تنظیمات» آدرس سرور را وارد کن.</p></div>
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
                u = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                vless = build_vless(u, settings)
                sub = sub_url_for(u, settings)
                result = f"""
                <div class="okmsg">✅ کاربر <b>{username}</b> ساخته شد!</div>
                <div class="card"><h2>🦁☀️ کانفیگ کاربر — شیر و خورشید</h2>
                <p>👤 نام کاربری: <b>{username}</b><br>📦 حجم: <b>{volume:g} گیگابایت</b><br>
                ⏳ مدت: <b>{days} روز</b> (تا {fmt_date(u['expires_at'])})</p>
                <p class="small">کانفیگ VLESS:</p><div class="mono">{vless}</div><br>
                <div class="row">{copy_btn(vless, '📋 کپی کانفیگ')}{copy_btn(sub, '🔗 کپی ساب‌لینک')}</div>
                <p class="small" style="margin-top:12px">ساب‌لینک:</p><div class="mono">{sub}</div>
                <p class="small">داخل ساب‌لینک، به ترتیب: 👤 نام کاربری، 📦 حجم، ⏳ زمان باقی‌مانده، و بعد خود کانفیگ.</p>
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
      <label>📦 حجم (گیگابایت)</label><input name="volume_gb" type="number" step="0.5" value="10" min="0.1" required>
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
        msg = "<div class='okmsg'>✅ تنظیمات ذخیره شد.</div>"
    s = get_all_settings()
    base = (s.get("sub_base_url") or request.host_url.rstrip("/"))
    content = f"""
    <div class="card"><h2>⚙️ تنظیمات</h2>{msg}
    <form method="post">
      <label>نام پنل</label><input name="panel_name" value="{s['panel_name']}">
      <label>🌐 آدرس سرور V2Ray (دامنه یا IP — همان که در کانفیگ VLESS می‌آید)</label>
      <input name="server_address" value="{s['server_address']}" placeholder="مثلاً vpn.example.com" dir="ltr" style="text-align:left">
      <label>🔌 پورت</label><input name="port" type="number" value="{s['port']}">
      <label>🔒 SNI (خالی = مثل آدرس سرور)</label><input name="sni" value="{s['sni']}" dir="ltr" style="text-align:left" placeholder="مثلاً vpn.example.com">
      <label>🛤️ مسیر WebSocket</label><input name="ws_path" value="{s['ws_path']}" dir="ltr" style="text-align:left">
      <label>📡 شبکه (Network)</label>
      <select name="network"><option value="ws" {'selected' if s['network']=='ws' else ''}>ws (WebSocket)</option>
      <option value="tcp" {'selected' if s['network']=='tcp' else ''}>tcp</option></select>
      <label>🔐 امنیت (Security)</label>
      <select name="security"><option value="tls" {'selected' if s['security']=='tls' else ''}>tls</option>
      <option value="none" {'selected' if s['security']=='none' else ''}>none</option></select>
      <label>🔗 آدرس پایه ساب‌لینک (خالی = همین دامنه پنل)</label>
      <input name="sub_base_url" value="{s['sub_base_url']}" dir="ltr" style="text-align:left" placeholder="https://your-panel.up.railway.app">
      <div style="height:14px"></div><button class="btn">💾 ذخیره تنظیمات</button>
    </form>
    <p class="small">نمونه ساب‌لینک: <span style="direction:ltr;display:inline-block">{base}/sub/&lt;token&gt;</span><br>
    💡 آدرس سرور را حتماً وارد کن؛ بدون آن کانفیگ‌ها با your-server.com ساخته می‌شوند.</p></div>"""
    return page("تنظیمات", "settings", content)

@app.route("/user/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    chk = require_login()
    if chk: return chk
    db = get_db()
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
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
    sub_url = sub_url_for(user, settings)
    html = f"""<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🦁☀️ ساب‌لینک {user['username']} | NEXUZ</title>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;700;900&display=swap" rel="stylesheet">
<style>{CSS}
.flagbig{{height:90px;border-radius:16px;overflow:hidden;position:relative;display:flex;
box-shadow:0 6px 30px rgba(0,0,0,.5);border:2px solid #fbbf24}}
.flagbig div{{flex:1}}.flagbig .g{{background:#239f40}}.flagbig .w{{background:#fff}}.flagbig .r{{background:#da0001}}
.flagbig .emblem{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%)}}
.info{{font-size:19px;line-height:2.1}}
.bar{{height:14px;background:rgba(0,0,0,.35);border-radius:999px;overflow:hidden}}
.bar i{{display:block;height:100%;background:linear-gradient(90deg,#f59e0b,#fbbf24);width:{pct}%}}
</style></head><body><div class="wrap" style="max-width:640px">
<header><div class="emblem">🦁<span>☀️</span></div>
<div><h1>NEXUZ</h1><div class="sub">شیر و خورشید 🦁☀️</div></div></header>
<div class="flagbig"><div class="g"></div><div class="w"></div><div class="r"></div>
<div class="emblem">🦁<span>☀️</span></div></div>
<p style="text-align:center;color:#fde68a">🇮🇷 پرچم شیر و خورشید 🇮🇷</p>
<div class="card"><h2>🦁☀️ اشتراک شما</h2><div class="info">
👤 <b>نام کاربری:</b> {user['username']}<br>
📦 <b>حجم:</b> {vol:g} گیگابایت &nbsp;|&nbsp; باقی‌مانده: {left:g} گیگابایت<br>
⏳ <b>زمان باقی‌مانده:</b> {days} روز (تا {fmt_date(user['expires_at'])})<br>
📊 <b>حجم باقی‌مانده:</b> {pct}٪</div>
<div class="bar"><i></i></div>
<p class="small" style="margin-top:14px">کانفیگ VLESS:</p><div class="mono">{vless}</div><br>
<div class="row">{copy_btn(vless, '📋 کپی کانفیگ')}{copy_btn(sub_url, '🔗 کپی ساب‌لینک')}</div>
<p class="small">این ساب‌لینک را در v2rayNG / Streisand / FoXray وارد کن — اول سه خط اطلاعات (👤 📦 ⏳) و بعد خود کانفیگ نمایش داده می‌شود.</p>
<div id="qr" class="qr"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>new QRCode(document.getElementById("qr"),{{text:"{vless}",width:180,height:180}});</script>
</div></div></body></html>"""
    return Response(html, mimetype="text/html; charset=utf-8")

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
else:
    init_db()
