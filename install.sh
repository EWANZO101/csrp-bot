#!/usr/bin/env bash
# CSRP Bot installer / updater.
#   Fresh VPS:  curl -fsSL https://raw.githubusercontent.com/EWANZO101/csrp-bot/main/install.sh | sudo bash
#   Update:     run the same command again (pulls the latest code, keeps your data).
#   Moving:     put csrp-backup.tar.gz (made by backup.sh on the old server) in the folder you run this from.
set -euo pipefail
REPO="${REPO:-https://github.com/EWANZO101/csrp-bot.git}"
DIR="${INSTALL_DIR:-/opt/csrp-bot}"
START_DIR="$(pwd)"
say(){ printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Please run as root (sudo)."; exit 1; }

if ! command -v git >/dev/null || ! command -v curl >/dev/null; then
  say "Installing git and curl"; apt-get update -qq && apt-get install -y -qq git curl ca-certificates >/dev/null
fi
if ! command -v docker >/dev/null; then
  say "Installing Docker"; curl -fsSL https://get.docker.com | sh >/dev/null
fi
docker compose version >/dev/null 2>&1 || { apt-get install -y -qq docker-compose-plugin >/dev/null; }

# running from inside a checkout? use it; otherwise clone/update into $DIR
if [ -f "$START_DIR/main.py" ] && [ -f "$START_DIR/docker-compose.yml" ]; then
  DIR="$START_DIR"
elif [ -d "$DIR/.git" ]; then
  say "Updating $DIR"; git -C "$DIR" pull --ff-only
else
  say "Downloading to $DIR"; git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

if [ ! -f .env ]; then
  printf "DB_PASS=%s\nWEB_PORT=8095\n" "$(openssl rand -hex 18 2>/dev/null || head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')" > .env
  chmod 600 .env
fi
mkdir -p data db

# restore a backup from the old server, if one was provided
BK=""
for f in "$START_DIR/csrp-backup.tar.gz" "$DIR/csrp-backup.tar.gz"; do [ -f "$f" ] && BK="$f" && break; done
if [ -n "$BK" ] && [ ! -f data/.restored ]; then
  say "Restoring backup $BK"
  tmp=$(mktemp -d); tar -xzf "$BK" -C "$tmp"
  cp -a "$tmp/data/." data/
  docker compose up -d db
  for i in $(seq 1 60); do docker compose exec -T db healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1 && break; sleep 2; done
  docker compose exec -T db sh -c 'mariadb -ueconomybot -p"$MARIADB_PASSWORD" economybot' < "$tmp/backup.sql"
  touch data/.restored; rm -rf "$tmp"
fi

say "Starting (first build takes a minute)"
docker compose up -d --build

PORT=$(grep ^WEB_PORT= .env | cut -d= -f2)
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then ufw allow "$PORT"/tcp >/dev/null || true; fi
IP=$(curl -s -m5 https://api.ipify.org || hostname -I | awk '{print $1}')
say "Done!"
echo "  Panel:   http://$IP:$PORT"
if [ -f data/settings.json ] && grep -q '"token"' data/settings.json; then
  echo "  Your bot settings were restored — just open the panel and log in."
else
  echo "  Open it in your browser and paste your bot token to finish setup."
fi
echo "  Logs:    cd $DIR && docker compose logs -f bot"
