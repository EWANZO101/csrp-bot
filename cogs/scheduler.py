"""Scheduler — admins schedule announcements, money actions, calendar events and payroll runs.

/schedule announce | money | event | payroll   create (once or repeating)
/schedule list | delete | pause | run | timezone
#schedule (public, read-only) shows a live calendar card of upcoming events and scheduled items.
Times: "in 30m", "in 2h", "20:00", "tomorrow 20:00", "fri 19:30", "2026-11-20 18:00" — in the server timezone
(/schedule timezone, default UTC). Repeat: none, 30m, 6h, daily, weekly, 2d …
"""
import datetime as dt
import logging
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, money, emb, transfer, ensure_user, get_user, log, reply, is_admin, \
    parse_amount, MINT, BURN, GREEN, RED, BLUE, GOLD

logger = logging.getLogger("economy.schedule")
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS schedule_config (
      guild_id BIGINT UNSIGNED PRIMARY KEY,
      tz VARCHAR(64) NOT NULL DEFAULT 'UTC'
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS schedules (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      kind VARCHAR(16) NOT NULL,
      title VARCHAR(200) NOT NULL,
      body TEXT NULL,
      channel_id BIGINT UNSIGNED NULL,
      ping_role_id BIGINT UNSIGNED NULL,
      next_run DATETIME NOT NULL,
      repeat_min INT NOT NULL DEFAULT 0,
      op VARCHAR(10) NULL,
      amount BIGINT NULL,
      target VARCHAR(40) NULL,
      remind_min INT NOT NULL DEFAULT 0,
      reminded TINYINT NOT NULL DEFAULT 0,
      enabled TINYINT NOT NULL DEFAULT 1,
      created_by BIGINT UNSIGNED NOT NULL,
      last_run DATETIME NULL,
      runs INT NOT NULL DEFAULT 0,
      INDEX ix_sched (enabled, next_run)
    ) ENGINE=InnoDB""",
]
KIND_ICON = {"announce": "📣", "money": "💸", "event": "📅", "payroll": "💼"}
REPEATS = [app_commands.Choice(name=n, value=v) for n, v in
           [("Once", "none"), ("Every 30 minutes", "30m"), ("Hourly", "1h"), ("Every 6 hours", "6h"), ("Every 12 hours", "12h"),
            ("Daily", "daily"), ("Every 2 days", "2d"), ("Weekly", "weekly"), ("Every 2 weeks", "2w")]]
DAYS = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}


def utcnow():
    return dt.datetime.utcnow().replace(second=0, microsecond=0)


def repeat_minutes(v: str) -> int:
    v = (v or "none").lower().strip()
    if v in ("none", "once", "0", ""):
        return 0
    if v == "daily":
        return 1440
    if v == "weekly":
        return 10080
    m = re.fullmatch(r"(\d+)\s*(m|min|h|d|w)", v)
    if not m:
        raise EconError("Repeat must be like none, 30m, 6h, daily, 2d, weekly.")
    n = int(m.group(1)) * {"m": 1, "min": 1, "h": 60, "d": 1440, "w": 10080}[m.group(2)]
    if n < 5:
        raise EconError("Shortest repeat is 5 minutes.")
    return n


def repeat_label(n):
    if not n:
        return "once"
    for unit, size in (("week", 10080), ("day", 1440), ("hour", 60)):
        if n % size == 0:
            k = n // size
            return {"day": "daily", "week": "weekly", "hour": "hourly"}[unit] if k == 1 else f"every {k} {unit}s"
    return f"every {n} min"


def parse_when(text: str, tz: ZoneInfo) -> dt.datetime:
    """Return a naive UTC datetime."""
    t = (text or "").strip().lower()
    local_now = dt.datetime.now(tz).replace(second=0, microsecond=0)
    m = re.fullmatch(r"in\s+(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)", t)
    if t == "now":
        target = local_now
    elif m:
        unit = m.group(2)[0]
        target = local_now + dt.timedelta(minutes=int(m.group(1)) * {"m": 1, "h": 60, "d": 1440, "w": 10080}[unit])
    else:
        dm_ = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})[ t](\d{1,2}):(\d{2})", t)
        hm = re.fullmatch(r"(?:(today|tomorrow|mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
        if dm_:
            y, mo, d, h, mi = map(int, dm_.groups())
            target = dt.datetime(y, mo, d, h, mi, tzinfo=tz)
        elif hm:
            day, h, mi, ap = hm.group(1), int(hm.group(2)), int(hm.group(3) or 0), hm.group(4)
            if ap == "pm" and h < 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0
            if h > 23 or mi > 59:
                raise EconError("That time doesn't exist.")
            target = local_now.replace(hour=h, minute=mi)
            if day == "tomorrow":
                target += dt.timedelta(days=1)
            elif day in DAYS:
                target += dt.timedelta(days=(DAYS[day] - target.weekday()) % 7)
                if target <= local_now:
                    target += dt.timedelta(days=7)
            elif day != "today" and target <= local_now:
                target += dt.timedelta(days=1)
        else:
            raise EconError("I couldn't read that time. Try `in 2h`, `20:00`, `tomorrow 8pm`, `fri 19:30` or `2026-11-20 18:00`.")
    return target.astimezone(dt.timezone.utc).replace(tzinfo=None)


async def any_org_ac(inter, current: str):
    rows = await q("SELECT name, kind FROM orgs WHERE guild_id=%s AND name LIKE %s ORDER BY name LIMIT 25", inter.guild_id, f"%{current}%")
    return [app_commands.Choice(name=f"{'🏢' if r['kind'] == 'business' else '🏛️'} {r['name']}", value=r["name"]) for r in rows]


def unix(d):
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


async def tzof(gid) -> ZoneInfo:
    r = await q1("SELECT tz FROM schedule_config WHERE guild_id=%s", gid)
    try:
        return ZoneInfo(r["tz"] if r else "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def describe_target(t):
    if t == "everyone":
        return "everyone"
    kind, _, v = (t or "").partition(":")
    return {"role": f"<@&{v}>", "user": f"<@{v}>", "org": f"org #{v}"}.get(kind, t)


class Scheduler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ticker.start()
        self.calendar.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)

    def cog_unload(self):
        self.ticker.cancel()
        self.calendar.cancel()

    @property
    def conf(self):
        return self.bot.state.settings.setdefault("schedule", {})

    schedule = app_commands.Group(name="schedule", description="Schedule announcements, payments, events and payroll", guild_only=True)

    async def create(self, inter, perm, **row):
        c = await core.require_admin(inter, perm)
        tz = await tzof(inter.guild_id)
        when = parse_when(row.pop("when"), tz)
        if when < utcnow() - dt.timedelta(minutes=1):
            raise EconError("That time is in the past.")
        rep = repeat_minutes(row.pop("repeat", "none"))
        cols = dict(guild_id=inter.guild_id, created_by=inter.user.id, next_run=when, repeat_min=rep, **row)
        _, sid = await ex(f"INSERT INTO schedules ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})", *cols.values())
        e = emb(f"{KIND_ICON[row['kind']]} Scheduled #{sid}: {row['title']}",
                f"**When:** <t:{unix(when)}:F> (<t:{unix(when)}:R>)\n**Repeats:** {repeat_label(rep)}", GREEN)
        await log(self.bot, inter.guild_id, emb(f"🗓️ Schedule #{sid} created", f"{row['title']} — by {inter.user.mention}", BLUE))
        await reply(inter, embed=e, ephemeral=True)
        await self.sync_calendar()

    @schedule.command(name="announce", description="(Admin) Post an announcement at a set time, once or repeating")
    @app_commands.describe(when="e.g. in 2h · 20:00 · tomorrow 8pm · fri 19:30 · 2026-11-20 18:00", ping="Role to ping (optional)")
    @app_commands.choices(repeat=REPEATS)
    async def announce(self, inter: discord.Interaction, channel: discord.TextChannel, title: app_commands.Range[str, 1, 200],
                       message: app_commands.Range[str, 1, 3500], when: str, repeat: str = "none", ping: discord.Role | None = None):
        await self.create(inter, "setup", kind="announce", title=title, body=message, channel_id=channel.id,
                          ping_role_id=ping.id if ping else None, when=when, repeat=repeat)

    @schedule.command(name="money", description="(Admin) Scheduled payment or charge — everyone, a role, a player or a business")
    @app_commands.describe(amount="Per person (or for the business)", when="e.g. in 1h · daily at 18:00 → when=18:00 repeat=Daily",
                           role="If who = role", user="If who = player", business="If who = business/department")
    @app_commands.choices(action=[app_commands.Choice(name="Give (create money)", value="give"),
                                  app_commands.Choice(name="Charge (take money)", value="charge")],
                          who=[app_commands.Choice(name="Everyone with an account", value="everyone"),
                               app_commands.Choice(name="Everyone with a role", value="role"),
                               app_commands.Choice(name="One player", value="user"),
                               app_commands.Choice(name="A business / department", value="org")],
                          repeat=REPEATS)
    @app_commands.autocomplete(business=any_org_ac)
    async def money_(self, inter: discord.Interaction, action: str, amount: str, who: str, when: str, repeat: str = "none",
                     role: discord.Role | None = None, user: discord.Member | None = None, business: str | None = None,
                     reason: app_commands.Range[str, 0, 150] | None = None):
        amt = parse_amount(amount)
        if who == "role":
            if not role:
                raise EconError("Pick the `role`.")
            target = f"role:{role.id}"
        elif who == "user":
            if not user:
                raise EconError("Pick the `user`.")
            target = f"user:{user.id}"
        elif who == "org":
            if not business:
                raise EconError("Pick the `business`.")
            target = f"org:{(await core.find_org(inter.guild_id, business))['id']}"
        else:
            target = "everyone"
        title = reason or f"{'Give' if action == 'give' else 'Charge'} {amt:,} → {who}"
        await self.create(inter, "economy", kind="money", title=title, op=action, amount=amt, target=target, when=when, repeat=repeat)

    @schedule.command(name="event", description="(Admin) Add an event to the #schedule calendar with reminders")
    @app_commands.describe(when="Start time", remind="Minutes before to post a reminder (0 = none)", ping="Role to ping")
    @app_commands.choices(repeat=REPEATS)
    async def event(self, inter: discord.Interaction, title: app_commands.Range[str, 1, 200], when: str,
                    description: app_commands.Range[str, 0, 1500] | None = None, remind: app_commands.Range[int, 0, 10080] = 30,
                    repeat: str = "none", ping: discord.Role | None = None):
        await self.create(inter, "setup", kind="event", title=title, body=description, remind_min=remind,
                          ping_role_id=ping.id if ping else None, when=when, repeat=repeat)

    @schedule.command(name="payroll", description="(Admin) Run payroll at set times (one business or all)")
    @app_commands.choices(repeat=REPEATS)
    @app_commands.autocomplete(business=any_org_ac)
    async def payroll(self, inter: discord.Interaction, when: str, repeat: str = "weekly", business: str | None = None):
        target = f"org:{(await core.find_org(inter.guild_id, business))['id']}" if business else "all"
        await self.create(inter, "setup", kind="payroll", title=f"Payroll — {business or 'all businesses & departments'}",
                          target=target, when=when, repeat=repeat)
        c = await cfg(inter.guild_id)
        if c["payroll_hours"]:
            await inter.followup.send(f"ℹ️ Automatic payroll is also running every {c['payroll_hours']}h. To pay **only** on this "
                                      "schedule, run `/admin config key:payroll_hours value:0`.", ephemeral=True)

    @schedule.command(name="list", description="(Staff) Everything that's scheduled")
    async def list_(self, inter: discord.Interaction):
        c = await cfg(inter.guild_id)
        if not is_admin(inter.user, c):
            raise EconError("Staff only.")
        rows = await q("SELECT * FROM schedules WHERE guild_id=%s ORDER BY enabled DESC, next_run", inter.guild_id)
        lines = []
        for r in rows:
            extra = ""
            if r["kind"] == "money":
                extra = f" · {r['op']} {money(c, r['amount'])} → {describe_target(r['target'])}"
            elif r["kind"] == "announce":
                extra = f" · <#{r['channel_id']}>"
            state = "⏸️ paused" if not r["enabled"] else f"next <t:{unix(r['next_run'])}:R>"
            lines.append(f"`#{r['id']}` {KIND_ICON.get(r['kind'], '•')} **{r['title'][:60]}**{extra}\n"
                         f"  {state} · {repeat_label(r['repeat_min'])} · ran {r['runs']}×")
        tz = await tzof(inter.guild_id)
        await reply(inter, embed=emb(f"🗓️ Schedules ({len(rows)}) · timezone {tz.key}", "\n".join(lines)[:4000] or "Nothing scheduled.", BLUE),
                    ephemeral=True)

    async def get(self, inter, sid, perm="setup"):
        await core.require_admin(inter, perm)
        r = await q1("SELECT * FROM schedules WHERE guild_id=%s AND id=%s", inter.guild_id, sid)
        if not r:
            raise EconError(f"No schedule #{sid}.")
        if r["kind"] == "money":
            await core.require_admin(inter, "economy")
        return r

    @schedule.command(name="delete", description="(Admin) Delete a schedule")
    async def delete(self, inter: discord.Interaction, id: int):
        r = await self.get(inter, id)
        await ex("DELETE FROM schedules WHERE id=%s", r["id"])
        await reply(inter, embed=emb(f"🗑️ Schedule #{id} deleted", r["title"], GOLD), ephemeral=True)
        await self.sync_calendar()

    @schedule.command(name="pause", description="(Admin) Pause or resume a schedule")
    async def pause(self, inter: discord.Interaction, id: int):
        r = await self.get(inter, id)
        nxt = r["next_run"]
        if not r["enabled"] and r["repeat_min"]:
            while nxt <= utcnow():
                nxt += dt.timedelta(minutes=r["repeat_min"])
        await ex("UPDATE schedules SET enabled=%s, next_run=%s WHERE id=%s", 0 if r["enabled"] else 1, nxt, r["id"])
        await reply(inter, embed=emb(f"{'⏸️ Paused' if r['enabled'] else '▶️ Resumed'} #{id}", r["title"], GOLD), ephemeral=True)
        await self.sync_calendar()

    @schedule.command(name="run", description="(Admin) Run a schedule right now (doesn't change its next time)")
    async def run_now(self, inter: discord.Interaction, id: int):
        r = await self.get(inter, id)
        await inter.response.defer(ephemeral=True)
        summary = await self.execute(r, manual=True)
        await inter.followup.send(embed=emb(f"▶️ Ran #{id}", summary or r["title"], GREEN), ephemeral=True)

    @schedule.command(name="timezone", description="(Admin) Server timezone for schedule times, e.g. America/New_York")
    async def timezone(self, inter: discord.Interaction, tz: str):
        await core.require_admin(inter, "setup")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise EconError("Unknown timezone. Examples: UTC, Europe/London, America/New_York, America/Los_Angeles, Australia/Sydney.")
        await ex("INSERT INTO schedule_config (guild_id,tz) VALUES (%s,%s) ON DUPLICATE KEY UPDATE tz=VALUES(tz)", inter.guild_id, tz)
        await reply(inter, embed=emb("🌍 Timezone set", f"Schedule times are now read as **{tz}**.", GREEN), ephemeral=True)

    @timezone.autocomplete("tz")
    async def tz_ac(self, inter, current: str):
        common = ["UTC", "Europe/London", "Europe/Berlin", "America/New_York", "America/Chicago", "America/Denver",
                  "America/Los_Angeles", "America/Phoenix", "America/Anchorage", "Pacific/Honolulu", "Australia/Sydney", "Asia/Tokyo"]
        return [app_commands.Choice(name=z, value=z) for z in common if current.lower() in z.lower()][:25]

    # ───────── execution ─────────
    async def targets(self, guild, target):
        gid = guild.id
        if target == "everyone":
            return [r["user_id"] for r in await q("SELECT user_id FROM users WHERE guild_id=%s", gid)]
        kind, _, v = target.partition(":")
        if kind == "role":
            role = guild.get_role(int(v))
            return [m.id for m in role.members if not m.bot] if role else []
        if kind == "user":
            return [int(v)]
        return []

    async def execute(self, r, manual=False) -> str:
        gid = r["guild_id"]
        g = self.bot.get_guild(gid)
        c = await cfg(gid)
        me = self.bot.user.id
        if r["kind"] == "announce":
            ch = g and g.get_channel(r["channel_id"])
            if not ch:
                return "Channel not found."
            v = discord.ui.LayoutView(timeout=None)
            v.add_item(discord.ui.Container(discord.ui.TextDisplay(f"## 📣 {r['title']}\n{r['body'] or ''}"),
                                            accent_colour=discord.Colour.gold()))
            if r["ping_role_id"]:
                await ch.send(f"<@&{r['ping_role_id']}>", allowed_mentions=discord.AllowedMentions(roles=True))
            await ch.send(view=v)
            return f"Posted in <#{ch.id}>"
        if r["kind"] == "money":
            amt, give = r["amount"], r["op"] == "give"
            if r["target"].startswith("org:"):
                oid = int(r["target"][4:])
                o = await q1("SELECT * FROM orgs WHERE id=%s", oid)
                if not o:
                    return "Business no longer exists."
                legs = [(MINT, ("org", oid), amt)] if give else [(("org", oid), BURN, min(amt, max(o["balance"], 0)))]
                await transfer(gid, legs, me, "scheduled", f"Schedule #{r['id']}: {r['title']}"[:255])
                summary = f"{'Gave' if give else 'Charged'} {money(c, amt)} {'to' if give else 'from'} **{o['name']}**"
            else:
                uids = await self.targets(g, r["target"]) if g else []
                total, n = 0, 0
                for uid in uids:
                    await ensure_user(gid, uid)
                    if give:
                        take = amt
                        legs = [(MINT, ("bank", uid), amt)]
                    else:
                        take = min(amt, max((await get_user(gid, uid))["bank"], 0))
                        dst = ("org", c["treasury_dept"]) if c["treasury_dept"] else BURN
                        legs = [(("bank", uid), dst, take)]
                    if take > 0:
                        await transfer(gid, legs, me, "scheduled", f"Schedule #{r['id']}: {r['title']}"[:255])
                        total += take
                        n += 1
                summary = f"{'Gave' if give else 'Charged'} {money(c, amt)} × {n} players ({describe_target(r['target'])}) = {money(c, total)}"
            await log(self.bot, gid, emb(f"💸 Scheduled payment #{r['id']}", f"{r['title']}\n{summary}", GOLD))
            return summary
        if r["kind"] == "payroll":
            from cogs.payroll import run_payroll
            if r["target"].startswith("org:"):
                orgs = await q("SELECT * FROM orgs WHERE id=%s", int(r["target"][4:]))
            else:
                orgs = await q("SELECT DISTINCT o.* FROM orgs o JOIN payroll p ON p.org_id=o.id WHERE o.guild_id=%s", gid)
            paid = total = 0
            for o in orgs:
                p, t, _ = await run_payroll(self.bot, o, me)
                paid, total = paid + p, total + t
            return f"Payroll: {paid} people paid · {money(c, total)} across {len(orgs)} orgs"
        if r["kind"] == "event":
            ch = g and g.get_channel(int(self.conf.get("channel_id") or 0))
            if ch:
                v = discord.ui.LayoutView(timeout=None)
                v.add_item(discord.ui.Container(discord.ui.TextDisplay(f"## 📅 Starting now: {r['title']}\n{r['body'] or ''}"),
                                                accent_colour=discord.Colour.green()))
                if r["ping_role_id"]:
                    await ch.send(f"<@&{r['ping_role_id']}>", allowed_mentions=discord.AllowedMentions(roles=True))
                await ch.send(view=v)
            return "Event started"
        return ""

    async def remind(self, r):
        g = self.bot.get_guild(r["guild_id"])
        ch = g and g.get_channel(int(self.conf.get("channel_id") or 0))
        if ch:
            v = discord.ui.LayoutView(timeout=None)
            v.add_item(discord.ui.Container(discord.ui.TextDisplay(
                f"## ⏰ Reminder: {r['title']}\nStarts <t:{unix(r['next_run'])}:R> (<t:{unix(r['next_run'])}:t>)\n{r['body'] or ''}"),
                accent_colour=discord.Colour.blurple()))
            if r["ping_role_id"]:
                await ch.send(f"<@&{r['ping_role_id']}>", allowed_mentions=discord.AllowedMentions(roles=True))
            await ch.send(view=v)
        await ex("UPDATE schedules SET reminded=1 WHERE id=%s", r["id"])

    @tasks.loop(seconds=15)
    async def ticker(self):
        try:
            now = utcnow()
            for r in await q("SELECT * FROM schedules WHERE enabled=1 AND kind='event' AND reminded=0 AND remind_min>0 "
                             "AND next_run > UTC_TIMESTAMP() AND next_run - INTERVAL remind_min MINUTE <= UTC_TIMESTAMP()"):
                await self.remind(r)
            due = await q("SELECT * FROM schedules WHERE enabled=1 AND next_run <= UTC_TIMESTAMP()")
            for r in due:
                try:
                    await self.execute(r)
                except Exception:
                    logger.exception("schedule #%s failed", r["id"])
                if r["repeat_min"]:
                    nxt = r["next_run"]
                    while nxt <= now:
                        nxt += dt.timedelta(minutes=r["repeat_min"])
                    await ex("UPDATE schedules SET next_run=%s, last_run=UTC_TIMESTAMP(), runs=runs+1, reminded=0 WHERE id=%s", nxt, r["id"])
                else:
                    await ex("UPDATE schedules SET enabled=0, last_run=UTC_TIMESTAMP(), runs=runs+1 WHERE id=%s", r["id"])
            if due:
                await self.sync_calendar()
        except Exception:
            logger.exception("scheduler tick failed")

    @ticker.before_loop
    async def _w1(self):
        await self.bot.wait_until_ready()

    # ───────── #schedule calendar ─────────
    async def render(self, guild):
        gid = guild.id
        c = await cfg(gid)
        tz = await tzof(gid)
        events = await q("SELECT * FROM schedules WHERE guild_id=%s AND enabled=1 AND kind='event' ORDER BY next_run LIMIT 15", gid)
        other = await q("SELECT * FROM schedules WHERE guild_id=%s AND enabled=1 AND kind IN ('announce','payroll','money') "
                        "ORDER BY next_run LIMIT 10", gid)
        ev = "\n".join(f"**{r['title']}** — <t:{unix(r['next_run'])}:F> · <t:{unix(r['next_run'])}:R>"
                       + (f" · 🔁 {repeat_label(r['repeat_min'])}" if r["repeat_min"] else "")
                       + (f"\n-# {r['body'][:150]}" if r["body"] else "") for r in events) or "*No events scheduled.*"
        ot = "\n".join(f"{KIND_ICON[r['kind']]} **{r['title'][:80]}** — <t:{unix(r['next_run'])}:R>"
                       + (f" · 🔁 {repeat_label(r['repeat_min'])}" if r["repeat_min"] else "") for r in other) or "*Nothing else scheduled.*"
        v = discord.ui.LayoutView(timeout=None)
        v.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## 🗓️ Server schedule\n-# Times show in your own timezone · server timezone {tz.key} · "
                                   f"updated <t:{int(discord.utils.utcnow().timestamp())}:R>"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"### 📅 Upcoming events\n{ev}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"### ⏱️ Also scheduled\n{ot}"),
            accent_colour=discord.Colour.blurple()))
        return v

    async def sync_calendar(self):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            ch = g.get_channel(int(self.conf.get("channel_id") or 0))
            if not ch:
                c = await cfg(gid)
                logch = g.get_channel(c.get("log_channel") or 0)
                ow = {g.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=True),
                      g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, mention_everyone=True)}
                ch = await g.create_text_channel("schedule", overwrites=ow, category=getattr(logch, "category", None),
                                                 topic="Upcoming events, payouts and announcements — live", reason="Schedule calendar")
                self.conf.update(channel_id=ch.id, message_id=None)
                logger.info("created #%s (%s)", ch.name, ch.id)
            view = await self.render(g)
            ok = False
            if self.conf.get("message_id"):
                try:
                    await ch.get_partial_message(int(self.conf["message_id"])).edit(view=view)
                    ok = True
                except discord.NotFound:
                    pass
            if not ok:
                msg = await ch.send(view=view)
                self.conf["message_id"] = msg.id
            self.bot.state.save()

    @tasks.loop(minutes=1)
    async def calendar(self):
        try:
            await self.sync_calendar()
        except Exception:
            logger.exception("calendar refresh failed")

    @calendar.before_loop
    async def _w2(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Scheduler(bot))
