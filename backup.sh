#!/usr/bin/env bash
# Makes csrp-backup.tar.gz (database + panel settings) to move the bot to another server.
set -euo pipefail
cd "$(dirname "$0")"
tmp=$(mktemp -d)
docker compose exec -T db sh -c 'mariadb-dump -ueconomybot -p"$MARIADB_PASSWORD" --single-transaction economybot' > "$tmp/backup.sql"
cp -a data "$tmp/data"; rm -f "$tmp/data/.restored"
tar -czf csrp-backup.tar.gz -C "$tmp" backup.sql data
rm -rf "$tmp"; chmod 600 csrp-backup.tar.gz
echo "Created $(pwd)/csrp-backup.tar.gz — copy it to the new server and run the installer from the same folder."
