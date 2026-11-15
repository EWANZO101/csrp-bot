"""GTA V activity — logs when members are "Playing Grand Theft Auto V" (Xbox, PlayStation or PC) via Discord presence.

• Needs the Presence Intent (Developer Portal → Bot). Players need their Xbox/PSN linked with "display as status" on,
  activity sharing on, and not be Invisible — otherwise Discord doesn't tell the bot.
• #gta-activity (staff only): live card — who's playing now, today's and this week's hours — refreshed every 2 minutes.
  Every start/stop is logged in the card's "📜 Session log" thread. 🔎 Look up a player shows anyone's hours + sessions.
• Sessions are stored in the database and survive restarts (on startup the bot closes sessions for people who stopped
  while it was offline and opens sessions for people already playing).
Only shows that someone is playing GTA V — not which server they're on.
"""
import asyncio
import datetime as dt
import logging
import re

import discord
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, emb, reply, is_admin, GREEN, RED, BLUE, GOLD

logger = logging.getLogger("economy.gta")
GTA = re.compile(r"grand\s*theft\s*auto\s*(v|5|online)\b|\bgta\s*(v|5|online)\b", re.I)
MEMBER_CHANNEL = 1032587296345702514   # #📱│main-chat — only people who can see this channel are tracked
PLATFORMS = {"xbox": "🟩 Xbox", "ps4": "🟦 PlayStation", "ps5": "🟦 PlayStation", "playstation": "🟦 PlayStation",
             "desktop": "🖥️ PC", "samsung": "📺 TV", "ios": "📱 Mobile", "android": "📱 Mobile"}
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS gta_sessions (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      game VARCHAR(100) NULL,
      platform VARCHAR(20) NULL,
      started_at DATETIME NOT NULL,
      ended_at DATETIME NULL,
      INDEX ix_gta_open (guild_id, ended_at),
      INDEX ix_gta_user (guild_id, user_id, started_at)
    ) ENGINE=InnoDB""",
]


def now():
    return dt.datetime.utcnow().replace(microsecond=0)


def unix(d):
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def hm(seconds) -> str:
    """1h 02m 15s / 4m 09s / 37s"""
    seconds = max(int(seconds or 0), 0)
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s" if m else f"{sec}s"


def tracked(member, conf) -> bool:
    """Only members in the configured channel's member list (= can view it). If the channel is missing, track everyone."""
    ch = member.guild.get_channel(int(conf.get("member_channel") or MEMBER_CHANNEL)) if hasattr(member.guild, "get_channel") else None
    return ch is None or ch.permissions_for(member).view_channel


def gta_activity(member):
    """The member's GTA V / GTA Online activity, or None."""
    for a in getattr(member, "activities", None) or ():
        if a.type == discord.ActivityType.playing and GTA.search(a.name or ""):
            return a
    return None


def platform_of(a) -> str | None:
    p = (getattr(a, "platform", None) or "").lower()
    return p[:20] or None


def plabel(p) -> str:
    return PLATFORMS.get((p or "").lower(), "🎮 " + (p.title() if p else "Unknown"))


async def played_since(gid, since, uid=None):
    """Seconds played per user since `since` (open sessions count up to now)."""
    t = now()
    rows = await q("SELECT user_id, SUM(TIMESTAMPDIFF(SECOND, GREATEST(started_at, %s), COALESCE(ended_at, %s))) s "
                   "FROM gta_sessions WHERE guild_id=%s AND COALESCE(ended_at, %s) > %s" + (" AND user_id=%s" if uid else "") +
                   " GROUP BY user_id ORDER BY s DESC", since, t, gid, t, since, *((uid,) if uid else ()))
    return [(r["user_id"], int(r["s"] or 0)) for r in rows]


class LookupModal(discord.ui.Modal, title="🔎 GTA hours — look up a player"):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.pick = discord.ui.Label(text="Pick a player", description="Start typing to search everyone in the server.",
                                     component=discord.ui.UserSelect(placeholder="Search members…", min_values=0, max_values=1,
                                                                     required=False))
        self.who = discord.ui.Label(text="…or type a name", description="Nickname, username or ID — part of it is fine.",
                                    component=discord.ui.TextInput(placeholder="e.g. Julian Vance", required=False, max_length=100))
        self.add_item(self.pick)
        self.add_item(self.who)

    async def on_submit(self, inter):
        try:
            picked = self.pick.component.values
            raw = str(picked[0].id) if picked else (self.who.component.value or "").strip()
            if not raw:
                raise EconError("Pick a player or type a name.")
            await self.cog.lookup(inter, raw)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class Card(discord.ui.LayoutView):
    def __init__(self, cog, body=None):
        super().__init__(timeout=None)
        self.cog = cog
        look = discord.ui.Button(label="Look up a player", emoji="🔎", style=discord.ButtonStyle.primary, custom_id="gta:lookup")
        look.callback = self.on_lookup
        self.add_item(discord.ui.Container(
            *(body or [discord.ui.TextDisplay("## 🎮 GTA V activity")]), discord.ui.Separator(),
            discord.ui.Section(discord.ui.TextDisplay("**🔎 Look up a player**\nTheir GTA hours today / this week / all time and "
                                                      "recent sessions."), accessory=look),
            accent_colour=discord.Colour.green()))

    async def on_lookup(self, inter):
        if not is_admin(inter.user, await cfg(inter.guild_id), "economy"):
            return await reply(inter, embed=emb("❌ Staff only.", None, RED), ephemeral=True)
        await inter.response.send_modal(LookupModal(self.cog))


class GtaActivity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(Card(self))
        self.ready = False
        self._sync_lock = asyncio.Lock()
        self.refresh.start()
        self.live.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)

    def cog_unload(self):
        self.refresh.cancel()
        self.live.cancel()

    @property
    def conf(self) -> dict:
        return self.bot.state.settings.setdefault("gta", {})

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.bot, "has_presences", False))

    # ───── tracking ─────
    async def open_session(self, member, a, t=None):
        if await q1("SELECT 1 FROM gta_sessions WHERE guild_id=%s AND user_id=%s AND ended_at IS NULL", member.guild.id, member.id):
            return False
        await ex("INSERT INTO gta_sessions (guild_id,user_id,game,platform,started_at) VALUES (%s,%s,%s,%s,%s)",
                 member.guild.id, member.id, (a.name or "")[:100], platform_of(a), t or now())
        return True

    async def close_session(self, gid, uid, t=None):
        r = await q1("SELECT * FROM gta_sessions WHERE guild_id=%s AND user_id=%s AND ended_at IS NULL ORDER BY id DESC LIMIT 1", gid, uid)
        if not r:
            return None
        t = t or now()
        await ex("UPDATE gta_sessions SET ended_at=%s WHERE guild_id=%s AND user_id=%s AND ended_at IS NULL", t, gid, uid)
        return int((t - r["started_at"]).total_seconds()), r

    @commands.Cog.listener()
    async def on_presence_update(self, before, after):
        if after.bot or after.guild.id not in self.bot.guild_ids or not self.ready:
            return
        if not tracked(after, self.conf):
            return
        was, is_ = gta_activity(before), gta_activity(after)
        if bool(was) == bool(is_):
            return
        if is_:
            if await self.open_session(after, is_):
                await self.session_log(after.guild, f"🟢 {after.mention} started **{is_.name}** on {plabel(platform_of(is_))}")
        else:
            res = await self.close_session(after.guild.id, after.id)
            if res:
                secs, r = res
                await self.session_log(after.guild, f"🔴 {after.mention} stopped playing · session **{hm(secs)}** "
                                                    f"({plabel(r['platform'])})")

    async def reconcile(self):
        """Startup / periodic: match the database to what Discord says right now."""
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            playing = {m.id: (m, a) for m in g.members if not m.bot and (a := gta_activity(m)) and tracked(m, self.conf)}
            for r in await q("SELECT user_id FROM gta_sessions WHERE guild_id=%s AND ended_at IS NULL", gid):
                if r["user_id"] not in playing:
                    await self.close_session(gid, r["user_id"])
            for m, a in playing.values():
                await self.open_session(m, a)

    async def session_log(self, guild, text):
        th = guild.get_thread(int(self.conf.get("thread_id") or 0))
        if th is None and self.conf.get("thread_id"):
            try:
                th = await guild.fetch_channel(int(self.conf["thread_id"]))
            except discord.HTTPException:
                th = None
        if th:
            try:
                if th.archived:
                    await th.edit(archived=False)
                await th.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    # ───── card ─────
    async def body(self, gid):
        t = now()
        if not self.enabled:
            return [discord.ui.TextDisplay("## 🎮 GTA V activity\n⚠️ **Presence Intent is off**, so Discord isn't sending activity. "
                                           "Turn it on in Developer Portal → Bot → Privileged Gateway Intents, then restart the bot.")]
        open_ = await q("SELECT * FROM gta_sessions WHERE guild_id=%s AND ended_at IS NULL ORDER BY started_at", gid)
        now_lines = [f"{plabel(r['platform'])} <@{r['user_id']}> — ⏱️ **{hm((t - r['started_at']).total_seconds())}**\n"
                     f"  -# started <t:{unix(r['started_at'])}:t> · <t:{unix(r['started_at'])}:R>" for r in open_]
        day0 = t.replace(hour=0, minute=0, second=0)
        week0 = day0 - dt.timedelta(days=day0.weekday())
        today = await played_since(gid, day0)
        week = await played_since(gid, week0)
        board = lambda rows: "\n".join(f"`{i:>2}.` <@{u}> — **{hm(s)}**" for i, (u, s) in enumerate(rows[:15], 1)) or "*Nobody yet.*"
        stamp = f"<t:{int(discord.utils.utcnow().timestamp())}:R>"
        return [
            discord.ui.TextDisplay(f"## 🎮 GTA V activity\n**{len(open_)}** playing right now · "
                                   f"{len(today)} played today · {len(week)} this week"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### 🟢 Playing now\n" + ("\n".join(now_lines)[:1500] or "*Nobody is playing GTA V right now.*")),
            discord.ui.Separator(visible=False),
            discord.ui.TextDisplay(f"### 📊 Today (UTC)\n{board(today)}"[:1500]),
            discord.ui.Separator(visible=False),
            discord.ui.TextDisplay(f"### 📅 This week (since Monday, UTC)\n{board(week)}"[:1500]),
            discord.ui.TextDisplay(f"-# Tracks members of <#{int(self.conf.get('member_channel') or MEMBER_CHANNEL)}>. "
                                   "Based on Discord activity status — players need Xbox/PSN linked with status shown and "
                                   f"activity sharing on. Shows GTA is running, not which server. Live — updates every 10 s ({stamp})"),
        ]

    async def overwrites(self, guild):
        panel = self.bot.get_cog("StaffPanel")
        ow = await panel.overwrites(guild) if panel else {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                                                          guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        for tgt, p in ow.items():
            if tgt != guild.me:
                p.send_messages = False
                p.send_messages_in_threads = False
        ow[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
                                                  create_public_threads=True, send_messages_in_threads=True, manage_threads=True)
        return ow

    async def sync(self, perms=False):
        async with self._sync_lock:
            await self._sync(perms)

    async def _sync(self, perms=False):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            ch = g.get_channel(int(self.conf.get("channel_id") or 0))
            if not ch:
                c = await cfg(gid)
                logch = g.get_channel(c.get("log_channel") or 0)
                ch = await g.create_text_channel("gta-activity", overwrites=await self.overwrites(g), category=getattr(logch, "category", None),
                                                 topic="Who's playing GTA V (Discord activity) — staff only")
                self.conf.update(channel_id=ch.id, message_id=None, thread_id=None)
                logger.info("created #%s (%s)", ch.name, ch.id)
            elif perms:
                await ch.edit(overwrites=await self.overwrites(g))
            view = Card(self, await self.body(gid))
            msg = None
            if self.conf.get("message_id"):
                try:
                    msg = ch.get_partial_message(int(self.conf["message_id"]))
                    await msg.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                except discord.NotFound:
                    msg = None
            if msg is None:
                msg = await ch.send(view=view, allowed_mentions=discord.AllowedMentions.none())
                self.conf.update(message_id=msg.id, thread_id=None)
            if not self.conf.get("thread_id"):
                try:
                    th = await ch.create_thread(name="📜 Session log", type=discord.ChannelType.public_thread,
                                                auto_archive_duration=10080)
                    self.conf["thread_id"] = th.id
                    await th.send("Every time someone starts or stops GTA V it's logged here.")
                except discord.HTTPException:
                    logger.exception("couldn't create GTA session log thread")
            self.bot.state.save()

    @tasks.loop(minutes=2)
    async def refresh(self):
        try:
            if self.enabled:
                await self.reconcile()
                self.ready = True
            await self.sync(perms=self.refresh.current_loop == 0)
        except Exception:
            logger.exception("GTA activity refresh failed")

    @refresh.before_loop
    async def _w(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=10)
    async def live(self):
        """Re-edit the card every 10 s so the timers tick (only while someone is playing)."""
        if not (self.ready and self.conf.get("message_id")):
            return
        try:
            if any([await q1("SELECT 1 FROM gta_sessions WHERE guild_id=%s AND ended_at IS NULL LIMIT 1", gid) for gid in self.bot.guild_ids]):
                await self.sync()
                self._idle_synced = False
            elif not getattr(self, "_idle_synced", False):   # one last edit after the last player stops
                await self.sync()
                self._idle_synced = True
        except Exception:
            logger.exception("GTA live update failed")

    @live.before_loop
    async def _wl(self):
        await self.bot.wait_until_ready()

    # ───── staff lookup ─────
    async def lookup(self, inter, raw):
        g = inter.guild
        m = re.search(r"\d{15,21}", raw)
        member = g.get_member(int(m.group())) if m else None
        if not member:
            n = raw.lstrip("@").lower()
            names = lambda x: [x.name.lower(), (x.global_name or "").lower(), x.display_name.lower()]
            member = next((x for x in g.members if n in names(x)), None)
            if not member:
                part = [x for x in g.members if any(n in y for y in names(x))]
                if len(part) == 1:
                    member = part[0]
                elif part:
                    return await reply(inter, embed=emb(f"🔎 {len(part)} matches for “{raw}”", "\n".join(f"{x.mention} — `{x.name}`" for x in part[:25])
                                                        + "\n\nLook up again and pick one.", BLUE), ephemeral=True)
        if not member:
            raise EconError(f"Couldn't find a member **{raw}**.")
        gid, t = inter.guild_id, now()
        day0 = t.replace(hour=0, minute=0, second=0)
        week0 = day0 - dt.timedelta(days=day0.weekday())
        tot = lambda rows: rows[0][1] if rows else 0
        today, week = tot(await played_since(gid, day0, member.id)), tot(await played_since(gid, week0, member.id))
        allt = tot(await played_since(gid, dt.datetime(2000, 1, 1), member.id))
        rows = await q("SELECT * FROM gta_sessions WHERE guild_id=%s AND user_id=%s ORDER BY started_at DESC LIMIT 10", gid, member.id)
        lines = [f"{plabel(r['platform'])} <t:{unix(r['started_at'])}:f> — "
                 + (f"**{hm((r['ended_at'] - r['started_at']).total_seconds())}**" if r["ended_at"] else "🟢 **playing now**")
                 for r in rows]
        a = gta_activity(member)
        e = emb(f"🎮 {member.display_name} — GTA V", None, GREEN if a else BLUE)
        e.add_field(name="Today", value=hm(today))
        e.add_field(name="This week", value=hm(week))
        e.add_field(name="All time (tracked)", value=hm(allt))
        e.add_field(name="Recent sessions", value="\n".join(lines)[:1024] or "None tracked yet.", inline=False)
        if not rows:
            e.set_footer(text="No sessions — they may not have Xbox/PSN linked with status shown, or activity sharing is off.")
        await reply(inter, embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(GtaActivity(bot))
