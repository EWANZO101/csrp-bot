"""#🚓・patrol — a live patrol timer for police.

• Visible to police (LSPD / BCSO / SAST / Dispatch tag roles) and staff; read-only.
• One card: patrol status, a live timer, who started it, and attendance — everyone who joins the Briefing
  stage channel while the patrol runs, with their time in it.
• ▶️ Start / ⏹️ Stop (staff only). Stopping logs the patrol (summary posted in the channel + 📒 bot-logs) and the
  timer resets; the next Start begins again from 0:00. 📜 Patrol log shows past patrols.
• Survives restarts: the running patrol and open Briefing sessions are kept in the database.
"""
import datetime as dt
import logging

import discord
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, emb, log, reply, is_admin, GREEN, RED, BLUE, GOLD

logger = logging.getLogger("economy.patrol")
DEFAULTS = {"stage_id": 1155069065774256139,
            "police_roles": [1155355856725082162, 1155355945027768360, 1155356012484763719, 1155356260871438428]}
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS patrols (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      started_at DATETIME NOT NULL,
      started_by BIGINT UNSIGNED NOT NULL,
      ended_at DATETIME NULL,
      ended_by BIGINT UNSIGNED NULL,
      INDEX ix_pat (guild_id, ended_at)
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS patrol_attendance (
      patrol_id INT NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      first_join DATETIME NOT NULL,
      joined_at DATETIME NULL,
      seconds INT NOT NULL DEFAULT 0,
      joins INT NOT NULL DEFAULT 0,
      PRIMARY KEY (patrol_id, user_id)
    ) ENGINE=InnoDB""",
]


def now():
    return dt.datetime.utcnow().replace(microsecond=0)


def unix(d):
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def hms(secs):
    secs = max(0, int(secs))
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


async def active(gid):
    return await q1("SELECT * FROM patrols WHERE guild_id=%s AND ended_at IS NULL ORDER BY id DESC LIMIT 1", gid)


async def attendance(pid):
    """Rows with live totals (open sessions counted up to now)."""
    rows = await q("SELECT * FROM patrol_attendance WHERE patrol_id=%s ORDER BY first_join", pid)
    t = now()
    for r in rows:
        r["total"] = r["seconds"] + (int((t - r["joined_at"]).total_seconds()) if r["joined_at"] else 0)
        r["in_now"] = r["joined_at"] is not None
    return rows


class PatrolCard(discord.ui.LayoutView):
    def __init__(self, cog, body=None, running=False):
        super().__init__(timeout=None)
        self.cog = cog
        start = discord.ui.Button(label="Start patrol", emoji="▶️", style=discord.ButtonStyle.success,
                                  custom_id="patrol:start", disabled=running)
        stop = discord.ui.Button(label="Stop patrol", emoji="⏹️", style=discord.ButtonStyle.danger,
                                 custom_id="patrol:stop", disabled=not running)
        logb = discord.ui.Button(label="Patrol log", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="patrol:log")
        for b in (start, stop, logb):
            b.callback = self.on_button
        self.add_item(discord.ui.Container(*(body or [discord.ui.TextDisplay("## 🚓 Patrol")]), discord.ui.Separator(),
                                           discord.ui.ActionRow(start, stop, logb),
                                           discord.ui.TextDisplay("-# ▶️ Start / ⏹️ Stop: staff only · every patrol is logged and the timer resets after it stops"),
                                           accent_colour=discord.Colour.green() if running else discord.Colour.dark_grey()))

    async def on_button(self, inter: discord.Interaction):
        k = inter.data["custom_id"].split(":")[1]
        try:
            if k == "log":
                return await reply(inter, embed=await self.cog.log_embed(inter.guild_id), ephemeral=True)
            if not is_admin(inter.user, await cfg(inter.guild_id)):
                raise EconError("Only staff can start or stop a patrol.")
            await inter.response.defer(ephemeral=True)
            msg = await (self.cog.start(inter.guild, inter.user) if k == "start" else self.cog.stop(inter.guild, inter.user))
            await inter.followup.send(embed=emb(msg, None, GREEN), ephemeral=True)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class Patrol(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(PatrolCard(self))
        self.refresh.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)

    def cog_unload(self):
        self.refresh.cancel()

    @property
    def conf(self):
        c = self.bot.state.settings.setdefault("patrol", {})
        for k, v in DEFAULTS.items():
            c.setdefault(k, v)
        return c

    # ───────── start / stop ─────────
    async def start(self, guild, who):
        if await active(guild.id):
            raise EconError("A patrol is already running.")
        t = now()
        _, pid = await ex("INSERT INTO patrols (guild_id,started_at,started_by) VALUES (%s,%s,%s)", guild.id, t, who.id)
        stage = guild.get_channel(int(self.conf["stage_id"]))
        for m in getattr(stage, "members", []) or []:   # people already in Briefing count from the start
            if not m.bot:
                await ex("INSERT IGNORE INTO patrol_attendance (patrol_id,user_id,first_join,joined_at,joins) VALUES (%s,%s,%s,%s,1)",
                         pid, m.id, t, t)
        await log(self.bot, guild.id, emb("🚓 Patrol started", f"#{pid} by {who.mention}", GREEN))
        await self.sync()
        return f"▶️ Patrol #{pid} started — the timer is running."

    async def stop(self, guild, who):
        p = await active(guild.id)
        if not p:
            raise EconError("No patrol is running.")
        t = now()
        await ex("UPDATE patrol_attendance SET seconds=seconds+TIMESTAMPDIFF(SECOND, joined_at, %s), joined_at=NULL "
                 "WHERE patrol_id=%s AND joined_at IS NOT NULL", t, p["id"])
        await ex("UPDATE patrols SET ended_at=%s, ended_by=%s WHERE id=%s", t, who.id, p["id"])
        rows = await attendance(p["id"])
        dur = int((t - p["started_at"]).total_seconds())
        e = emb(f"📋 Patrol #{p['id']} log", None, BLUE)
        e.add_field(name="Duration", value=hms(dur))
        e.add_field(name="Started", value=f"<t:{unix(p['started_at'])}:t> by <@{p['started_by']}>")
        e.add_field(name="Stopped", value=f"<t:{unix(t)}:t> by {who.mention}")
        e.add_field(name=f"In Briefing ({len(rows)})", value="\n".join(
            f"<@{r['user_id']}> — {hms(r['total'])}" + (f" · joined {r['joins']}×" if r["joins"] > 1 else "")
            for r in sorted(rows, key=lambda r: -r["total"]))[:1024] or "Nobody joined Briefing.", inline=False)
        await log(self.bot, guild.id, e)
        ch = guild.get_channel(int(self.conf.get("channel_id") or 0))
        if ch:
            await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        await self.sync()
        return f"⏹️ Patrol #{p['id']} stopped after {hms(dur)} and logged. The timer has reset."

    # ───────── Briefing tracking ─────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or member.guild.id not in self.bot.guild_ids:
            return
        stage = int(self.conf["stage_id"])
        was, is_ = getattr(before.channel, "id", None) == stage, getattr(after.channel, "id", None) == stage
        if was == is_:
            return
        p = await active(member.guild.id)
        if not p:
            return
        t = now()
        if is_:
            await ex("INSERT INTO patrol_attendance (patrol_id,user_id,first_join,joined_at,joins) VALUES (%s,%s,%s,%s,1) "
                     "ON DUPLICATE KEY UPDATE joined_at=COALESCE(joined_at, VALUES(joined_at)), joins=joins+1", p["id"], member.id, t, t)
        else:
            await ex("UPDATE patrol_attendance SET seconds=seconds+TIMESTAMPDIFF(SECOND, joined_at, %s), joined_at=NULL "
                     "WHERE patrol_id=%s AND user_id=%s AND joined_at IS NOT NULL", t, p["id"], member.id)
        await self.sync()

    async def reconcile(self):
        """After a restart: close sessions for people who left Briefing while the bot was offline, open for those in it."""
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            p = await active(gid)
            if not (g and p):
                continue
            stage = g.get_channel(int(self.conf["stage_id"]))
            inside = {m.id for m in (getattr(stage, "members", []) or []) if not m.bot}
            t = now()
            for r in await q("SELECT * FROM patrol_attendance WHERE patrol_id=%s", p["id"]):
                if r["joined_at"] and r["user_id"] not in inside:
                    await ex("UPDATE patrol_attendance SET seconds=seconds+TIMESTAMPDIFF(SECOND, joined_at, %s), joined_at=NULL "
                             "WHERE patrol_id=%s AND user_id=%s", t, p["id"], r["user_id"])
            for uid in inside:
                await ex("INSERT INTO patrol_attendance (patrol_id,user_id,first_join,joined_at,joins) VALUES (%s,%s,%s,%s,1) "
                         "ON DUPLICATE KEY UPDATE joined_at=COALESCE(joined_at, VALUES(joined_at))", p["id"], uid, t, t)

    # ───────── card ─────────
    async def log_embed(self, gid):
        rows = await q("SELECT p.*, (SELECT COUNT(*) FROM patrol_attendance a WHERE a.patrol_id=p.id) n FROM patrols p "
                       "WHERE guild_id=%s AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 15", gid)
        lines = [f"`#{r['id']}` <t:{unix(r['started_at'])}:d> <t:{unix(r['started_at'])}:t> — **{hms((r['ended_at'] - r['started_at']).total_seconds())}**"
                 f" · {r['n']} in Briefing · by <@{r['started_by']}>" for r in rows]
        tot = await q1("SELECT COUNT(*) n, COALESCE(SUM(TIMESTAMPDIFF(SECOND, started_at, ended_at)),0) s FROM patrols "
                       "WHERE guild_id=%s AND ended_at IS NOT NULL", gid)
        e = emb("📜 Patrol log", "\n".join(lines) or "No patrols logged yet.", BLUE)
        e.set_footer(text=f"{tot['n']} patrols · {hms(int(tot['s']))} total")
        return e

    async def render(self, guild):
        p = await active(guild.id)
        stage_id = int(self.conf["stage_id"])
        stamp = f"<t:{int(discord.utils.utcnow().timestamp())}:R>"
        if p:
            rows = await attendance(p["id"])
            elapsed = int((now() - p["started_at"]).total_seconds())
            inside = [r for r in rows if r["in_now"]]
            att = "\n".join(f"{'🟢' if r['in_now'] else '⚪'} <@{r['user_id']}> — {hms(r['total'])}" for r in rows[:30]) \
                or f"*Nobody has joined <#{stage_id}> yet.*"
            body = [discord.ui.TextDisplay(
                        f"## 🚓 Patrol #{p['id']} — 🟢 ON PATROL\n"
                        f"# ⏱️ {hms(elapsed)}\n"
                        f"Started <t:{unix(p['started_at'])}:R> (<t:{unix(p['started_at'])}:t>) by <@{p['started_by']}>"),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(f"### 👮 In <#{stage_id}> — {len(inside)} now · {len(rows)} total\n{att}\n"
                                           f"-# Timer refreshes every 30 seconds · updated {stamp}")]
        else:
            last = await q1("SELECT * FROM patrols WHERE guild_id=%s AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1", guild.id)
            body = [discord.ui.TextDisplay(
                f"## 🚓 Patrol — ⚪ No patrol running\n# ⏱️ 0m 00s\n"
                + (f"Last patrol #{last['id']} lasted **{hms((last['ended_at'] - last['started_at']).total_seconds())}**, "
                   f"ended <t:{unix(last['ended_at'])}:R>." if last else "No patrols yet.")
                + f"\nAttendance is taken from <#{stage_id}> while a patrol runs.\n-# updated {stamp}")]
        return PatrolCard(self, body, running=bool(p))

    async def overwrites(self, guild):
        panel = self.bot.get_cog("StaffPanel")
        ow = await panel.overwrites(guild) if panel else {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        for t_, perm in ow.items():
            if t_ != guild.me:
                perm.send_messages = False
        for rid in self.conf["police_roles"]:
            role = guild.get_role(int(rid))
            if role:
                ow[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False, add_reactions=False)
        return ow

    async def sync(self, perms=False):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            ch = g.get_channel(int(self.conf.get("channel_id") or 0))
            if not ch:
                hub = g.get_channel(1571328922388271264)
                ch = await g.create_text_channel("🚓・patrol", overwrites=await self.overwrites(g),
                                                 category=hub if isinstance(hub, discord.CategoryChannel) else None,
                                                 topic="Live patrol timer and Briefing attendance — police & staff")
                self.conf.update(channel_id=ch.id, message_id=None)
                logger.info("created #%s (%s)", ch.name, ch.id)
            elif perms:
                await ch.edit(overwrites=await self.overwrites(g))
            view = await self.render(g)
            ok = False
            if self.conf.get("message_id"):
                try:
                    await ch.get_partial_message(int(self.conf["message_id"])).edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                    ok = True
                except discord.NotFound:
                    pass
            if not ok:
                msg = await ch.send(view=view, allowed_mentions=discord.AllowedMentions.none())
                self.conf["message_id"] = msg.id
            self.bot.state.save()

    @tasks.loop(seconds=30)
    async def refresh(self):
        try:
            if self.refresh.current_loop == 0:
                await self.reconcile()
            await self.sync()
        except Exception:
            logger.exception("patrol card refresh failed")

    @refresh.before_loop
    async def _w(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Patrol(bot))
