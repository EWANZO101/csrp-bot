"""#bot-activity: a public, self-updating card showing who is using the bot.

Every slash command, button press or menu choice a member makes counts as activity. The card (refreshed every
minute) shows who is active right now (last 15 min), who was active today (24 h), and the most active users this week.
/active shows the same thing on demand.
"""
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import q, ex, cfg, reply, emb, BLUE

logger = logging.getLogger("economy.activity")
CHANNEL_NAME = "bot-activity"
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS user_activity (
      guild_id BIGINT UNSIGNED NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      day DATE NOT NULL,
      uses INT NOT NULL DEFAULT 0,
      last_seen DATETIME NOT NULL,
      last_action VARCHAR(64) NULL,
      PRIMARY KEY (guild_id, user_id, day)
    ) ENGINE=InnoDB""",
]


def mention_list(rows, fmt, limit=30):
    out = [fmt(r) for r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"… and {len(rows) - limit} more")
    return "\n".join(out)


class Activity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.refresh.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)

    def cog_unload(self):
        self.refresh.cancel()

    @property
    def conf(self):
        return self.bot.state.settings.setdefault("activity", {})

    @commands.Cog.listener()
    async def on_interaction(self, inter: discord.Interaction):
        if not inter.guild_id or inter.guild_id not in self.bot.guild_ids or inter.user.bot:
            return
        if inter.type == discord.InteractionType.autocomplete:
            return
        if inter.type == discord.InteractionType.application_command and inter.command:
            action = "/" + inter.command.qualified_name
        else:
            action = "button/menu"
        try:
            await ex("INSERT INTO user_activity (guild_id,user_id,day,uses,last_seen,last_action) "
                     "VALUES (%s,%s,UTC_DATE(),1,UTC_TIMESTAMP(),%s) ON DUPLICATE KEY UPDATE uses=uses+1, "
                     "last_seen=UTC_TIMESTAMP(), last_action=VALUES(last_action)", inter.guild_id, inter.user.id, action[:64])
        except Exception:
            logger.exception("activity tracking failed")

    async def data(self, gid):
        now_rows = await q("SELECT user_id, MAX(last_seen) seen, SUBSTRING_INDEX(GROUP_CONCAT(last_action ORDER BY last_seen DESC),',',1) act "
                           "FROM user_activity WHERE guild_id=%s AND last_seen > UTC_TIMESTAMP() - INTERVAL 15 MINUTE "
                           "GROUP BY user_id ORDER BY seen DESC", gid)
        day_rows = await q("SELECT user_id, MAX(last_seen) seen, SUM(uses) uses FROM user_activity WHERE guild_id=%s "
                           "AND last_seen > UTC_TIMESTAMP() - INTERVAL 1 DAY GROUP BY user_id ORDER BY seen DESC", gid)
        week_rows = await q("SELECT user_id, SUM(uses) uses FROM user_activity WHERE guild_id=%s "
                            "AND day >= UTC_DATE() - INTERVAL 6 DAY GROUP BY user_id ORDER BY uses DESC LIMIT 10", gid)
        return now_rows, day_rows, week_rows

    def unix(self, d):
        return int(d.replace(tzinfo=dt.timezone.utc).timestamp())

    async def render(self, guild):
        now_rows, day_rows, week_rows = await self.data(guild.id)
        medals = ["🥇", "🥈", "🥉"] + [f"`{i}.`" for i in range(4, 11)]
        v = discord.ui.LayoutView(timeout=None)
        v.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## 📈 Who's using the bot\n🟢 **{len(now_rows)}** active now · 🕒 **{len(day_rows)}** today\n"
                                   f"-# Counts slash commands, buttons and menus · updated <t:{int(discord.utils.utcnow().timestamp())}:R>"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### 🟢 Active now (last 15 min)\n" + (mention_list(
                now_rows, lambda r: f"<@{r['user_id']}> — <t:{self.unix(r['seen'])}:R>" + (f" · `{r['act']}`" if r["act"] else ""))
                or "*Nobody right now.*")),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### 🕒 Active today (last 24 h)\n" + (mention_list(
                day_rows, lambda r: f"<@{r['user_id']}> — last seen <t:{self.unix(r['seen'])}:R> · {r['uses']} actions", 25)
                or "*Nobody yet today.*")),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### 🏆 Most active this week\n" + ("\n".join(
                f"{medals[i]} <@{r['user_id']}> — {r['uses']} actions" for i, r in enumerate(week_rows)) or "*No activity yet.*")),
            accent_colour=discord.Colour.green()))
        return v

    async def sync(self):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            ch = g.get_channel(int(self.conf.get("channel_id") or 0))
            if not ch:
                c = await cfg(gid)
                logch = g.get_channel(c.get("log_channel") or 0)
                ow = {g.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False),
                      g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
                ch = await g.create_text_channel(CHANNEL_NAME, overwrites=ow, category=getattr(logch, "category", None),
                                                 topic="Who's using the economy bot — live", reason="Bot activity board")
                self.conf.update(channel_id=ch.id, message_id=None)
                logger.info("created #%s (%s)", ch.name, ch.id)
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

    @tasks.loop(minutes=1)
    async def refresh(self):
        try:
            await self.sync()
        except discord.Forbidden:
            logger.warning("activity board: missing permission")
        except Exception:
            logger.exception("activity board refresh failed")

    @refresh.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="active", description="See who's been using the bot")
    @app_commands.guild_only()
    async def active(self, inter: discord.Interaction):
        await inter.response.send_message(view=await self.render(inter.guild), ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Activity(bot))
