# Economy Bot

A Discord roleplay economy (banking, businesses, departments, payroll, citations, shops, items) with a web panel that uses Discord login, plus optional SnailyCAD integration.

## Install (one line)

```bash
curl -fsSL https://raw.githubusercontent.com/EWANZO101/csrp-bot/main/install.sh | sudo bash
```

This installs Docker if needed, downloads the bot to `/opt/csrp-bot`, starts it with its own database and prints the panel address. Open that address and paste your bot token. **Running the same line again updates the bot** and keeps all your data.

### Moving to another VPS (keeping everything)
1. On the old server: `bash backup.sh`. This creates `csrp-backup.tar.gz` (database and panel settings).
2. Copy that file to the new server and run the one-line install **from the same folder**. The backup is restored automatically. Then in the Discord Developer Portal → your app → OAuth2 → Redirects, add the new panel address followed by `/callback` (for example `https://bot.snailyapp.com/callback`). Discord doesn't let bots add this themselves.

Useful:
* Logs: `cd /opt/csrp-bot && docker compose logs -f bot`
* Restart: `docker compose restart bot`
* Start setup over (connect a different bot): delete `data/settings.json`, then `docker compose restart bot`

## Setup is done in the browser
1. Open the panel and paste the **bot token** (Developer Portal → New Application → Bot → Reset Token). The panel tries to turn on the Members intent for you.
2. Click **Add to server**. The server is detected automatically.
3. Copy the one URL the panel shows into OAuth2 → Redirects (Discord requires this once for each panel address).
4. Log in with Discord → **Server setup**. Toggle features, pick channels and roles from dropdowns, set your branding, then save. The bot posts the documents, tickets and applications panels itself.

## Discord application checklist
1. Bot → Reset Token → paste it into the panel.
2. No client secret is needed. Login uses Discord's token flow.
3. Invite the bot with the link the panel gives you after setup. Its role must sit **above** any roles that items grant.

## Commands
`/bank` (button panel) `/balance` `/pay` `/deposit` `/withdraw` `/history` `/leaderboard`
`/shop browse|buy|all` `/inventory` `/give` `/use`
`/business …` and `/department …`: create, delete, list, info, inventory, edit, deposit, withdraw, send, transfer_ownership, `item add|edit|remove|give`, `role add|remove`
`/payroll add|remove|list|run` (paid automatically every `payroll_hours`)
`/citation issue|pay|mine|list|info|edit|void` (late fee added automatically once a citation is overdue)
`/admin print|remove|wipe|freeze|undo|audit|stats|settings|config|set_log|staff`
`/cad profile|sync` (SnailyCAD) · `/help`

Amounts accept `500`, `2.5k`, `1m` and `all`.

## SnailyCAD
Panel → Settings → SnailyCAD. Enter the CAD's MySQL connection (a dedicated user with `SELECT` on the CAD DB plus `UPDATE` on `Record` is enough).
* CAD tickets for citizens whose CAD account is linked to Discord become citations. The fine is the sum of fine × counts.
* Paying in Discord or on the web marks the CAD record **PAID**.
* Tickets that are paid or deleted in the CAD are voided here.
* By default only tickets written after you enable the link are imported. There's a checkbox to import older unpaid ones too.

## Documents, tickets, applications
* **Documents:** `/docs` on the web panel is a branded documents site. Pages are built in the panel's document builder (headings, tables, callouts, images), or imported from a Google Doc or Sheet in one click. Each page can be public, members-only or staff-only. The Discord panel links to every document and has an optional “I have read the documents” button that grants a role.
* **Tickets:** a panel with your ticket types. Each ticket gets a private channel with Claim and Close buttons. Transcripts go to a log channel and appear in the web panel.
* **Applications:** forms are built in the panel (6 question types) with requirements: required or blocked roles, account age, time in the server, read-the-docs, no unpaid fines, custom notes, and a cooldown after denial. Members apply on the web. Reviewers accept or deny from Discord buttons or the panel, and roles are added or removed automatically.

## Staff access
Only **owners** and members of **staff ranks** can open the staff area, both on the web panel and for the bot's staff commands. Having Manage Server in Discord does *not* give access.
* **Owners** have full access and can't be locked out. They're set during setup and can be changed under **Staff access**.
* **Ranks** (for example Admin, Moderator, Support) each choose their areas: Citizens & money, Businesses & departments, Citations, Tickets, Applications, Documents, Server setup, Staff management. A rank is linked to Discord roles (anyone with the role gets it) and/or specific user IDs. It can optionally give its Discord role to the users listed on it.
