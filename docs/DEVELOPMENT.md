# Developer guide

Everything a developer needs to understand, change, test, release and run the CSRP economy bot.
For the user-facing install and command list, see the [README](../README.md).

---

## 1. What it is

One Python process (`main.py`) that runs **two things side by side**:

1. **A Discord bot** (discord.py 2.7, slash commands, buttons, live "cards" in channels).
2. **A web panel** (aiohttp) with Discord login, a JSON API and a single-page UI in `static/`.

Data lives in **MariaDB 11.4**. In production both run in Docker (`docker-compose.yml`: services `db` and `bot`).
The bot talks to the **OpsLab Bot Hub** (`HUB_URL`) for heartbeats, update notifications and off-site backups.

```
Discord ⇄ bot (cogs/*)  ┐
                        ├─ core.py (DB pool, config, money, permissions) ─ MariaDB
Browser ⇄ web panel ────┘        (web.py + web_ext.py + backups.py)
                        └─ hub_client.py ⇄ OpsLab Bot Hub
Host cron: updater.sh (updates) · install.sh (install / update / move)
```

---

## 2. Repository map

| Path | What it does |
|---|---|
| `main.py` | Entry point. Connects to the DB (retries 30×), loads `data/settings.json` into `AppState`, starts the hub client and web panel, then starts the bot if it's set up. Handles intent fallbacks and token errors. |
| `bot.py` | `EconomyBot`: intents, the list of cogs to load (`EXTENSIONS` always, `OPTIONAL` per feature), removes commands of disabled features, syncs slash commands **to the configured guild only**, leaves any other guild, central slash-command error handler. |
| `core.py` | Shared plumbing. DB helpers (`q`, `q1`, `ex`, `tx`), per-guild config (`cfg`, `set_cfg`, `DEFAULTS`), features, money movement (`transfer`, `MINT`, `BURN`), characters, staff permissions, org permission levels, embeds/formatting, logging, DMs, confirm dialogs. **Read this first.** |
| `schema.sql` | Core tables. Run on every start (idempotent). |
| `cogs/*.py` | One feature per file (see §6). |
| `web.py` | Web panel core: signed-cookie sessions, Discord OAuth (token flow, no client secret), setup wizard, player/staff API, settings, CAD settings. Calls `web_ext.register()` and `backups.register()`. |
| `web_ext.py` | Extra panel routes: documents site, tickets, applications, server setup, branding, staff ranks, updates, feature requests. |
| `backups.py` | Daily/manual DB dumps, download/upload/restore, Bot Hub backup sync and hub restore jobs. |
| `hub_client.py` | Bot Hub registration, 15-min heartbeat, update requests (writes `data/update-request.json`). |
| `docs_lib.py` | HTML sanitiser for the document builder + Google Docs/Sheets importer. |
| `apps_lib.py` | Application forms: requirement checks, review embeds, accept/deny (shared by cog and panel). |
| `player_info.py` | "Everything about one player" embeds (used by tickets → 📋 Player info). |
| `static/` | `index.html` (panel SPA), `guide.html`, `app.css`, `brand/logo.png`. |
| `tools/import_manual.py` | Import a hand-typed/OCR'd card through the importer's snapshot logic. |
| `install.sh` | Installer/updater/mover (Docker, nginx, certbot, cron jobs, backup restore). |
| `updater.sh` | Host cron job (every minute) that performs updates requested by the panel/hub. |
| `backup.sh` | Makes `csrp-backup.tar.gz` (DB dump + `data/`) for moving servers. |
| `VERSION` | Current version string. Shown in the panel and compared against hub releases. |

Not in git (see `.gitignore`): `.env`, `data/`, `db/`, backups, `__pycache__`.

---

## 3. Running it locally

### With Docker (recommended, same as production)
```bash
git clone https://github.com/EWANZO101/csrp-bot && cd csrp-bot
printf "DB_PASS=devpass\nWEB_PORT=8095\n" > .env
docker compose up -d --build
docker compose logs -f bot
```
Open `http://localhost:8095`. The setup code is printed in the logs and stored in `data/setup-code.txt`.
**Use a separate test bot and a test Discord server.** Never use the production bot token on two machines
at once (both would answer every command, and both would run timers like payroll and billing).

### Without Docker
Needs Python 3.12, a MariaDB/MySQL server, and `tesseract-ocr` (only for screenshot OCR in the importer).
```bash
python -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cat > .env <<EOF
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=economybot
DB_PASS=devpass
DB_NAME=economybot
WEB_PORT=8095
EOF
python main.py
```

### Environment variables
| Var | Default | Meaning |
|---|---|---|
| `DB_HOST` `DB_PORT` `DB_USER` `DB_PASS` `DB_NAME` | – | Database connection (compose sets these). |
| `DATA_DIR` | `./data` (`/app/data` in Docker) | Where settings, backups, images and updater files live. |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8095` | Panel listen address. In compose, `WEB_PORT` in `.env` is the **host** port. |
| `PUBLIC_URL` | empty | Public panel URL (used for OAuth redirects and links). Set by the installer's SSL step. |
| `HUB_URL` | OpsLab Bot Hub | Developer hub. |
| `BOT_OWNER_IDS` | empty | Extra owner IDs (comma-separated); normally owners are set in the panel. |

---

## 4. Where settings and data live

There are **three** places, and it matters which one you use:

| Store | What goes there | How to read / write |
|---|---|---|
| `data/settings.json` | Bot connection and runtime state: `token`, `client_id`, `guild_id`, `owner_ids`, `web_secret`, `features`, `public_url`, `cad`, `hub`, `updates`, `import`, and each cog's channel/message IDs (`activity`, `directory`, `staff_panel`, `patrol`, `gta`, `schedule`, …). | `bot.state.settings` (a dict) then `bot.state.save()`. In cogs: `self.bot.state.settings.setdefault("mycog", {})`. |
| `guild_config` table (JSON per guild) | Server settings changed in the panel: currency, start balance, log channel, staff ranks, payroll hours, taxes, ticket/doc/app panel texts, branding… Defaults in `core.DEFAULTS`. | `c = await core.cfg(gid)` (cached) and `await core.set_cfg(gid, key=value)`. Never write the table directly, or the cache will be stale. |
| Other DB tables | All game data (users, orgs, items, ledger, citations, tickets, …). | `core.q/q1/ex/tx`. |

> ⚠️ **The bot rewrites `settings.json` from memory** whenever it saves and when it shuts down.
> To hand-edit it: `docker compose stop bot` → edit → `docker compose start bot`. If you edit it while the bot
> runs, your change is lost.

To add a new server setting: add it to `core.DEFAULTS` (and `CONFIG_TYPES` if it's a `str`/`bool`/`float` with a
`None` default), then expose it in the panel (`web.py` settings API + `static/index.html`) or `/admin config`.

---

## 5. Core concepts (read before changing money or permissions)

### Money: always use `core.transfer`
```python
await transfer(gid, [(src, dst, amount), ...], actor_id, kind, memo, extra=None, allow_negative=False)
```
- Accounts: `("wallet", uid)`, `("bank", uid)`, `("org", org_id)`, `("cwallet"|"cbank", character_id)`, `MINT` (money
  created), `BURN` (money destroyed).
- All legs run in **one DB transaction** with row locks (`FOR UPDATE`), check funds and frozen accounts,
  and write one `ledger` row per leg. `/admin undo`, history and audits depend on the ledger. **Never
  `UPDATE users SET bank=…` directly.**
- `extra(cur)` runs inside the same transaction. Use it for stock and inventory changes that must succeed or fail with the payment.
- `kind` is a short tag (`pay`, `shop`, `payroll`, `import`, `admin_print`, …) used for filtering and undo rules.
- User-facing failures: `raise EconError("message")`. The message is shown to the user as-is (see `bot.on_app_error`).

### Characters
A player can have several characters (`max_characters`). The **active** character's money and items live in
`users` / `user_items`, so all features work unchanged. Inactive characters are parked in `characters` /
`character_items`. Switching swaps them in one transaction, writes `character` ledger rows, and holds `core.IMPORT_LOCK`
so an import never lands mid-swap. Imports from the old bot always go to the **main** character
(`core.import_character`).

### Permissions
- **Owners** (`settings.json owner_ids`, plus `BOT_OWNER_IDS`) can do everything and can't be locked out.
- **Staff ranks** (`guild_config.staff_ranks`) grant perms from `core.STAFF_PERMS`: `economy`, `orgs`, `citations`,
  `tickets`, `applications`, `documents`, `discipline`, `setup`, `staff`. Discord's Manage Server does **not** count.
- In commands: `await core.require_admin(inter, "economy")`. In checks: `core.is_admin(member, c, "orgs")`.
- In the web API: `m, perms, c = await self.session(request, perm="economy")` (`perm="*"` = any staff).
- Business/department levels: `core.org_level()` → `NONE / MEMBER / HEAD / ADMIN` (`ADMIN` = has the `orgs` perm).
  Use `require_level(member, org, HEAD, "edit items")`.
- **Hiding a channel is not security.** Every button handler re-checks permission.

### Features
`core.FEATURES` lists features that can be switched on and off (economy, business, departments, payroll, citations, shop,
tickets, documents, applications, cad). `settings.json features` stores the switches. A disabled feature's slash commands
(`core.FEATURE_COMMANDS`) are removed at startup. `tickets`, `documents`, `applications` and `cad` cogs are not loaded at
all when off (`bot.OPTIONAL`). Changing features needs a bot restart (the panel does this).

### Logging to staff
`await core.log(bot, gid, emb("Title", "text", GOLD))` posts to the configured log channel.
`await core.dm(bot, uid, embed, gid)` respects the `dm_receipts` setting.

---

## 6. Feature map (cogs)

| Cog | Feature | Commands / channels | Background loops |
|---|---|---|---|
| `bank` | Wallet/bank | `/bank /balance /pay /deposit /withdraw /history /leaderboard` | – |
| `orgs` | Businesses & departments (one implementation) | `/business …` `/department …` | – |
| `items` | Shops & inventory | `/shop /inventory /give /use` | – |
| `payroll` | Payroll | `/payroll` | auto-pay every `payroll_hours` (run by the admin ticker) |
| `citations` | Fines | `/citation …` (needs character name + reason) | late fees (admin ticker) |
| `admin` | Staff tools + the 5-min ticker (auto-payroll, citation late fees, daily interest) | `/admin …` `/help` | 5 min |
| `staffpanel` | #staff-panel button forms | `/staff_panel` | – |
| `directory` | #player-accounts staff card | `/player_accounts` | 1 min |
| `activity` | #bot-activity "who's using the bot" | `/active` | 1 min |
| `characters` / `charpanel` | Multiple characters + #characters card | `/character …` | 2 min |
| `hospital` | Per-second hospital billing | `/hospital …` | 30 s |
| `utilities` | Utility companies, plans, bills, autopay | `/utilities`, #utilities | 1–2 min |
| `scheduler` | Scheduled announcements, money, events, payroll | `/schedule …`, #schedule | 15 s / 1 min |
| `requests` | Business & property requests (player + admin channels) | buttons | 5 min |
| `properties` | Property registry synced with a Google Sheet | #property-list | 5 min |
| `forms` | #user-setup and #dept-transfers forms with approve/deny | buttons | 5 min |
| `discipline` | Warnings, 3 strikes, timed suspensions | `/discipline …` | 1 min |
| `patrol` | Police patrol timer + Briefing attendance | buttons | 30 s |
| `gta` | "Playing GTA V" presence tracking | #gta-activity | 10 s / 2 min |
| `roster` | Chain-of-command card | buttons | 10 min |
| `guides` | #docs interactive how-to dropdown | – | 6 h |
| `importer` (+ `ocr_parse`) | Imports the old economy bot's cards and screenshots | import channel, `/importfix` | 3 s sweep, 60 s cleanup, 10 min review |
| `tickets` *(optional)* | Ticket panel → private channel → transcript | `/ticket …` | 10 min |
| `documents` *(optional)* | Documents panel + "I have read" role | `/docs /docs_panel` | – |
| `applications` *(optional)* | Web application forms | `/apply /apply_panel` | – |
| `cad` *(optional)* | SnailyCAD tickets ⇄ citations | `/cad …` | 2 min |

Each cog's docstring at the top of the file explains how it works. Read that before you change the cog.

### The importer (`cogs/importer.py`)
- Config: `settings.json → import: {channel_id, bot_id}`. Only embeds from `bot_id` in that channel are read.
- **Snapshot logic (prevents double-counting):** each value (wallet, bank, org balance, item qty per inventory page)
  is a snapshot keyed in `import_snapshots` with the Discord message ID. A message older than the stored snapshot is
  ignored. A newer one applies only the **difference**, so money earned in this bot in the meantime is kept.
  `import_log` stores a hash per message, so unchanged messages are skipped.
- Screenshots are saved to `data/import-images/`, OCR'd (`tesseract`) and parsed by `ocr_parse.py`. Anything unclear
  is parked for review (`import_images.status='review'`). `/importfix name @member` links an OCR name to a member.
- Fully imported messages in the import channel are deleted after 10 minutes (needs Manage Messages).
- `tools/import_manual.py card.json [--dry-run]` (run inside the container) imports a hand-typed card the same way.
- The 3 s whole-channel sweep is temporary (`POLL_UNTIL`).

---

## 7. How to change things (recipes)

### Add a slash command to an existing cog
```python
@app_commands.command(name="tip", description="Tip someone from your wallet")
@app_commands.describe(member="Who to tip", amount="e.g. 500, 2.5k")
async def tip(self, inter: discord.Interaction, member: discord.Member, amount: str):
    c = await cfg(inter.guild_id)
    u = await get_user(inter.guild_id, inter.user.id)
    n = parse_amount(amount, u["wallet"])
    await transfer(inter.guild_id, [(("wallet", inter.user.id), ("wallet", member.id), n)], inter.user.id, "tip", "Tip")
    await reply(inter, embed=emb("💸 Tipped", f"{member.mention} got **{money(c, n)}**", GREEN))
```
If it belongs to a switchable feature, add its name to `core.FEATURE_COMMANDS[...]` so it disappears when that feature is off.
Commands sync to the guild on every bot start. Restart the bot to see the change.

### Add a new cog
1. Create `cogs/mything.py`:
   ```python
   import logging, discord
   from discord import app_commands
   from discord.ext import commands, tasks
   import core
   from core import q, q1, ex, cfg, emb

   logger = logging.getLogger("economy.mything")
   SCHEMA = ["""CREATE TABLE IF NOT EXISTS mything (
       guild_id BIGINT UNSIGNED NOT NULL, user_id BIGINT UNSIGNED NOT NULL, value INT NOT NULL DEFAULT 0,
       PRIMARY KEY (guild_id, user_id)) ENGINE=InnoDB"""]

   class MyThing(commands.Cog):
       def __init__(self, bot):
           self.bot = bot
           self.tick.start()

       async def cog_load(self):
           for s in SCHEMA:
               await ex(s)

       def cog_unload(self):
           self.tick.cancel()

       @property
       def conf(self):                       # channel/message IDs etc. → settings.json
           return self.bot.state.settings.setdefault("mything", {})

       @tasks.loop(minutes=1)
       async def tick(self):
           try:
               ...
           except Exception:
               logger.exception("mything tick failed")   # never let a loop die

       @tick.before_loop
       async def _wait(self):
           await self.bot.wait_until_ready()

   async def setup(bot):
       await bot.add_cog(MyThing(bot))
   ```
2. Add `"cogs.mything"` to `EXTENSIONS` in `bot.py` (or to `OPTIONAL` with a feature key + an entry in `core.FEATURES`).
3. Rebuild and restart: `docker compose up -d --build bot`.

### Persistent buttons (cards that keep working after a restart)
Give every button a fixed `custom_id`, use `discord.ui.View(timeout=None)` / `LayoutView(timeout=None)`, and register it
once on startup with `bot.add_view(MyView(bot))` (see `cogs/charpanel.py`, `cogs/forms.py`). Store the channel and message
IDs in `settings.json` (`self.conf`). On each refresh, edit that message and repost it if it's gone (see `cogs/activity.py → sync`).

### Database changes
- There is **no migration tool**. All schema changes must be **idempotent**, because they run on every start:
  - new table → `CREATE TABLE IF NOT EXISTS` (in `schema.sql` for core tables, or the cog's `SCHEMA` / `cog_load`);
  - new column → `ALTER TABLE x ADD COLUMN IF NOT EXISTS …` (MariaDB syntax; see the end of `schema.sql`);
  - new index → `ADD UNIQUE KEY IF NOT EXISTS …`.
- `schema.sql` is split on `;`, so **don't put `;` inside comments or strings** in that file.
- Never drop or rename columns that live servers use without a compatibility step. Old backups must still restore.
- The DB runs in UTC (`time_zone='+00:00'`). Use `UTC_TIMESTAMP()` / naive UTC datetimes; `core.ts()` formats them for Discord.
- Money is stored as whole numbers (`BIGINT`), with no decimals.

### Web panel (API + UI)
- Add a route in `Panel.__init__` (`web.py`) or `register()` in `web_ext.py` / `backups.py`.
- Protect it: `m, perms, c = await self.session(request, perm="setup")`. Return errors with `jerr("message")`.
  `EconError` raised inside handlers is turned into a JSON error by the middleware.
- The UI is plain HTML/JS in `static/index.html` (no build step). Styles go in `static/app.css`. Static files are baked into the
  Docker image, so **rebuild** after editing.
- Login uses Discord's token (implicit) flow. Every panel address needs `<url>/callback` added under OAuth2 → Redirects
  in the Discord Developer Portal. Discord does not let the bot add it for you.

### Text, branding, emojis
Most visible texts (panel titles, ticket types, docs text, brand name and colour) are server settings in `core.DEFAULTS`,
editable in the panel. Change the defaults there. Hard-coded card texts are in each cog's `render()` / embed code.

---

## 8. Testing checklist

There is no automated test suite. Before you release:
1. `python -m py_compile $(git ls-files '*.py')`. This catches syntax errors.
2. Start against a **test bot + test server** (fresh DB *and* a restored copy of a real backup).
3. Check the logs: `docker compose logs -f bot`. Look for tracebacks and "synced N commands".
4. Try the changed commands and buttons as a normal member **and** as staff. Check that money changes show up in `/history`.
5. Restart the bot and confirm cards/buttons still work (persistent views) and nothing is imported or charged twice.
6. If you touched the schema, start an **old** version's DB with the new code (upgrade path).

---

## 9. Releasing a new version

1. Bump `VERSION` (semver: `1.4.0` → `1.5.0` features, `1.4.1` fixes).
2. Update the README if commands or setup changed.
3. Commit and push to `main`:
   ```bash
   git commit -am "v1.5.0: short description"
   git tag v1.5.0 && git push origin main --tags
   ```
4. Publish the release in the **OpsLab Bot Hub** (admin). Bots see `update_available` on their next heartbeat (≤15 min).
   Owners update from the panel, or automatically if they turned on auto-update. The hub admin can also push an update.
5. Whatever is on `main` is what every new install and every "run the install line again" gets. **Keep `main`
   deployable.** Use branches for unfinished work.

### How updates reach a server
- **Panel / hub update:** the bot writes `data/update-request.json`. The host cron job
  (`/etc/cron.d/csrp-bot-updater` → `updater.sh`, every minute) runs `git fetch` and **`git reset --hard origin/main`**
  (or checks out a tag), then `docker compose up -d --build`. Progress goes to `data/update-status.json`.
- **Manual:** re-run the install one-liner, or `cd /opt/csrp-bot && git pull --ff-only && docker compose up -d --build`.

> ⚠️ `updater.sh` throws away uncommitted changes on the server. If someone has edited files directly on a
> server, run `git status` there first and commit or merge that work before you update. Otherwise it's lost.

---

## 10. Operating a server

All commands run in the install folder (default `/opt/csrp-bot`):

| Task | Command |
|---|---|
| Logs | `docker compose logs -f bot` (`--since 10m` to limit) |
| Restart (no code change) | `docker compose restart bot` |
| Deploy code you changed on the server | `docker compose up -d --build bot` (code is **baked into the image**; restart alone won't pick it up) |
| Status | `docker compose ps` |
| DB shell | `docker compose exec db sh -c 'mariadb -ueconomybot -p"$MARIADB_PASSWORD" economybot'` |
| Manual DB dump | `docker compose exec -T db sh -c 'mariadb-dump -ueconomybot -p"$MARIADB_PASSWORD" --single-transaction economybot' > dump.sql` |
| Restore a panel backup by hand | `gunzip -c data/backups/db/FILE.sql.gz \| docker compose exec -T db sh -c 'mariadb -ueconomybot -p"$MARIADB_PASSWORD" economybot'` |
| Move to a new server | `bash backup.sh` → copy `csrp-backup.tar.gz` → run the installer from the same folder on the new server |
| Reset setup (connect another bot) | stop bot, delete `data/settings.json`, start bot |

Backups: the panel makes a daily dump at 04:00 UTC into `data/backups/db/` (kept: 14 auto, 20 manual, 10 pre-restore/uploaded/hub)
and sends it to the Bot Hub if enabled. Restoring always takes a `pre-restore` backup first.

Files on the host that the installer creates: `/etc/cron.d/csrp-bot-updater`, `/etc/cron.d/csrp-bot-ssl` (removed once
HTTPS works), `/usr/local/bin/csrp-bot-ssl`, nginx site `/etc/nginx/sites-available/csrp-bot`, logs in
`/var/log/csrp-bot-*.log`.

---

## 11. Security rules

- Never commit `.env`, `data/` (bot token, web secret, hub key, CAD credentials) or DB dumps. They're in `.gitignore`. Keep it that way.
  **This repo is public.**
- Don't log tokens or passwords. Don't put them in embeds or API responses.
- Every staff action must check permissions on the server side (`require_admin`, `session(perm=…)`), even behind a hidden channel or button.
- Use parameterised SQL (`q("… WHERE id=%s", x)`). Never build SQL by string formatting with user input. The only f-strings in
  SQL are fixed column names from internal maps (see `transfer`).
- Use `allowed_mentions=discord.AllowedMentions.none()` on cards that list members, so nobody gets pinged.
- User HTML (documents) must go through `docs_lib` sanitising.
- One token = one running bot. Don't start a copy of a production bot (for example from a backup) while the original is online.

---

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Panel says "Discord rejected the bot token" | Token was reset. Paste the new one in the panel. |
| Warning about Members/Presence intent | Turn on the privileged intents in Developer Portal → Bot, then restart. The bot runs without them, but names, the importer and GTA tracking need them. |
| Slash commands missing | Bot not in the configured server (invite link in Settings), the feature is off, or commands didn't sync (check logs for "synced N commands"). |
| Login loops / "invalid redirect" | `<panel url>/callback` is not in the app's OAuth2 Redirects, or `PUBLIC_URL` is wrong in `.env`. |
| A setting you edited in `settings.json` disappeared | The bot rewrote the file. Stop the bot before editing (§4). |
| Code change has no effect | You restarted instead of rebuilding. Run `docker compose up -d --build bot`. |
| Update failed | Check `data/update-status.json` and `/var/log/csrp-bot-updater.log`. Is the cron file present? |
| "database not ready" on start | The DB container is still starting (healthcheck). The bot retries for about 60 s. |
| Card/button says "interaction failed" after a restart | The view isn't registered with `bot.add_view` or has no fixed `custom_id`. |
