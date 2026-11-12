#!/usr/bin/env bash
# CSRP Bot installer / updater.
#   Fresh VPS:  curl -fsSL https://raw.githubusercontent.com/EWANZO101/csrp-bot/main/install.sh | sudo bash
#   Update:     run the same command again (pulls the latest code, keeps your data).
#   Moving:     put csrp-backup.tar.gz (made by backup.sh on the old server) in the folder you run this from.
set -euo pipefail
REPO="${REPO:-https://github.com/EWANZO101/csrp-bot.git}"
DOMAIN="${DOMAIN:-bot.snailyapp.com}"     # panel moves to https://$DOMAIN as soon as its DNS points here
DIR="${INSTALL_DIR:-/opt/csrp-bot}"
START_DIR="$(pwd)"
say(){ printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Please run as root (sudo)."; exit 1; }

if ! command -v git >/dev/null || ! command -v curl >/dev/null || ! command -v nginx >/dev/null || ! command -v certbot >/dev/null; then
  say "Installing git, curl, nginx and certbot"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl ca-certificates nginx certbot python3-certbot-nginx dnsutils >/dev/null
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

grep -q '^PUBLIC_URL=' .env || echo "PUBLIC_URL=" >> .env
chmod +x "$DIR/updater.sh"
echo "* * * * * root $DIR/updater.sh $DIR >> /var/log/csrp-bot-updater.log 2>&1" > /etc/cron.d/csrp-bot-updater
say "Starting (first build takes a minute)"
docker compose up -d --build

PORT=$(grep ^WEB_PORT= .env | cut -d= -f2)
IP=$(curl -s -m5 https://api.ipify.org || hostname -I | awk '{print $1}')

# ── nginx: serve the panel on the IP now, and on $DOMAIN once DNS is ready ──
say "Configuring nginx"
DEF="default_server"
if grep -rqs "listen[^;]*80[^;]*default_server" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null \
   && ! grep -qs "csrp-bot" /etc/nginx/sites-enabled/csrp-bot 2>/dev/null; then DEF=""; fi   # don't fight another site
rm -f /etc/nginx/sites-enabled/default
if [ ! -f /etc/nginx/sites-available/csrp-bot ] || ! grep -q "ssl_certificate" /etc/nginx/sites-available/csrp-bot; then
cat > /etc/nginx/sites-available/csrp-bot <<NGINX
# csrp-bot — managed by install.sh
server {
    listen 80 $DEF;
    listen [::]:80 $DEF;
    server_name $DOMAIN $IP _;
    client_max_body_size 12m;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 90s;
    }
}
NGINX
fi
ln -sf /etc/nginx/sites-available/csrp-bot /etc/nginx/sites-enabled/csrp-bot
nginx -t >/dev/null 2>&1 && systemctl reload nginx || { echo "nginx config test failed:"; nginx -t; }

# ── HTTPS: try now, and keep trying every 10 minutes until $DOMAIN points here ──
cat > /usr/local/bin/csrp-bot-ssl <<SSL
#!/usr/bin/env bash
# Gets a Let's Encrypt certificate for $DOMAIN once its DNS points at this server, then switches the panel to it.
DOMAIN="$DOMAIN"; DIR="$DIR"
[ -d /etc/letsencrypt/live/\$DOMAIN ] && grep -q "^PUBLIC_URL=https://\$DOMAIN" \$DIR/.env && { rm -f /etc/cron.d/csrp-bot-ssl; exit 0; }
MYIP=\$(curl -s -m5 https://api.ipify.org)
DNSIP=\$(dig +short A \$DOMAIN @1.1.1.1 | tail -n1)
# ready when DNS points straight here, or (Cloudflare proxy) the domain already serves this panel
if ! { [ -n "\$MYIP" ] && [ "\$MYIP" = "\$DNSIP" ]; } && ! curl -s -m8 "http://\$DOMAIN/api/brand" | grep -q '"rgb"'; then
  echo "\$(date '+%F %T') \$DOMAIN -> '\$DNSIP', this server is \$MYIP — not ready yet"; exit 0
fi
certbot --nginx -d \$DOMAIN --non-interactive --agree-tos --register-unsafely-without-email --redirect --keep-until-expiring || exit 1
sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=https://\$DOMAIN#" \$DIR/.env
cd \$DIR && docker compose up -d >/dev/null 2>&1
rm -f /etc/cron.d/csrp-bot-ssl
echo "HTTPS is live: https://\$DOMAIN"
SSL
chmod +x /usr/local/bin/csrp-bot-ssl
echo "*/10 * * * * root /usr/local/bin/csrp-bot-ssl >> /var/log/csrp-bot-ssl.log 2>&1" > /etc/cron.d/csrp-bot-ssl
/usr/local/bin/csrp-bot-ssl || true

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null || true; ufw allow 443/tcp >/dev/null || true; ufw allow "$PORT"/tcp >/dev/null || true
fi
say "Done!"
if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then echo "  Panel:   https://$DOMAIN"; else
echo "  Panel:   http://$IP   (also http://$IP:$PORT)"
echo "  Domain:  point an A record for $DOMAIN to $IP — HTTPS switches on automatically within 10 minutes."; fi
if [ -f data/settings.json ] && grep -q '"token"' data/settings.json; then
  echo "  Your bot settings were restored — just open the panel and log in."
else
  echo "  Open it in your browser and paste your bot token to finish setup."
fi
echo "  Logs:    cd $DIR && docker compose logs -f bot"
