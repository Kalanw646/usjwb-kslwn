#!/bin/bash
set -e
# nginx listens on Railway's $PORT; Xray on 10086, Flask panel on 5000.
sed "s/__PORT__/${PORT:-8080}/g" /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf
nginx -t -c /etc/nginx/nginx.conf
# Build the first Xray config from the panel DB (empty clients list is fine).
python3 -c "import app; app.sync_xray_config()" || true
CFG=/data/xray-config.json
[ -f "$CFG" ] || CFG=./xray-config.json
# Xray loop: restarts automatically when the panel kills it after a
# user create/delete (the panel rewrites the config first).
( while true; do
    /usr/local/bin/xray run -c "$CFG" || true
    echo "xray exited, restarting in 2s"
    sleep 2
  done ) &
# Flask panel (single worker so Xray restarts are not duplicated)
gunicorn app:app --workers 1 --bind 127.0.0.1:5000 &
# nginx in foreground = container PID 1
exec nginx -g "daemon off;" -c /etc/nginx/nginx.conf
