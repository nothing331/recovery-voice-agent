#!/bin/bash
# Run the app on this laptop with a free public link (Cloudflare quick tunnel: no account, no billing).
#   scripts/laptop.sh start   -> starts the app + tunnel, prints the public link
#   scripts/laptop.sh stop    -> stops both
#   scripts/laptop.sh status
# The link changes on every start. Login: any username + DASHBOARD_PASSWORD from backend/.env.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN=~/.recovery-voice-agent
PORT=7860
mkdir -p "$RUN"

stop() {
  pkill -f "cloudflared tunnel --no-autoupdate" 2>/dev/null
  pkill -f "uvicorn app.main:app --port $PORT" 2>/dev/null
  echo "stopped"
}

status() {
  pgrep -f "uvicorn app.main:app --port $PORT" >/dev/null && echo "app: running" || echo "app: stopped"
  pgrep -f "cloudflared tunnel --no-autoupdate" >/dev/null && echo "link: $(cat "$RUN/url.txt" 2>/dev/null)" || echo "link: stopped"
}

start() {
  stop >/dev/null
  grep -q '^DASHBOARD_PASSWORD=.' "$ROOT/backend/.env" || { echo "Set DASHBOARD_PASSWORD in backend/.env first"; exit 1; }
  (cd "$ROOT/backend" && nohup uv run uvicorn app.main:app --port $PORT --proxy-headers --forwarded-allow-ips='*' \
      > "$RUN/app.log" 2>&1 &)
  for _ in $(seq 1 60); do curl -sf localhost:$PORT/health >/dev/null && break; sleep 0.5; done
  : > "$RUN/tunnel.log"
  nohup cloudflared tunnel --no-autoupdate --protocol http2 --url http://localhost:$PORT > "$RUN/tunnel.log" 2>&1 &
  for _ in $(seq 1 90); do grep -q 'trycloudflare.com' "$RUN/tunnel.log" && break; sleep 1; done
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$RUN/tunnel.log" | head -1)
  echo "$URL" > "$RUN/url.txt"
  echo "Waiting for the link to go live..."
  for _ in $(seq 1 40); do [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL/health")" = 200 ] && break; sleep 3; done
  echo "Public link: $URL"
  echo "Login: any username, password = DASHBOARD_PASSWORD in backend/.env"
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  *) status ;;
esac
