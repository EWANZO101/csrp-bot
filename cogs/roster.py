"""Chain of command: one live card in #📌│chain-of-command listing every staff position and who holds it.

Staff with the Server setup permission get ➕ Add, ✏️ Edit and ➖ Remove buttons (forms with live role/member search).
Positions are Discord roles shown as role mentions; holders are member mentions (nobody is pinged). Empty = *Vacant*.
"""
import logging

import discord
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, emb, log, reply, is_admin, GREEN, RED, GOLD

logger = logging.getLogger("economy.roster")
CHANNEL_ID = 1380563969026293913
SECTIONS = ["👑 Ownership", "🌐 Network", "🛠️ Server Administration", "🏙️ Civilian Operations", "🚔 Public Safety (DPS)", "📋 Recruiting"]
FOOTER = "If anyone is interested in staff, reach out to a member above to learn how to apply."
SEED = [  # (section, role_id, user_id or None)
    (0, 1032587295284531358, 769044253187112990), (0, 1032587295284531357, 1171684400321277973),
    (1, 1032587295259361327, 1000478723423867030), (1, 1032587295259361327, 601922439387021313),
    (2, 1032587295259361326, None), (2, 1283817486080278640, 504856558161887233), (2, 1032587295259361323, None),
    (3, 1032587295259361324, 377660105337864203), (3, 1032587295259361322, 1102246455890149546),
    (3, 1283682380284166146, 363807151799402499), (3, 1283682380284166146, 792143270326173706),
    (3, 1283682380284166146, 1518418191670313044),
    (4, 1032587295259361325, 905916803891494923), (4, 1134137328433102908, None),
    (4, 1032587295259361329, 1133781728641679451), (4, 1032587295259361329, 693300230103498792),
    (4, 1032587295259361329, 1317518276573663292),
    (5, 1283818296801497161, None),
]
SCHEMA = """CREATE TABLE IF NOT EXISTS roster (
  id INT AUTO_INCREMENT PRIMARY KEY,
  guild_id BIGINT UNSIGNED NOT NULL,
  section VARCHAR(80) NOT NULL,
  role_id BIGINT UNSIGNED NULL,
  title VARCHAR(80) NULL,
  user_id BIGINT UNSIGNED NULL,
  sort INT NOT NULL DEFAULT 0
) ENGINE=InnoDB"""


async def entries(gid):
    return await q("SELECT * FROM roster WHERE guild_id=%s ORDER BY sort, id", gid)


def label(guild, r):
    role = guild.get_role(r["role_id"]) if r["role_id"] else None
    pos = (role.name if role else r["title"] or "Position").replace("『✦』", "").strip()
    m = guild.get_member(r["user_id"]) if r["user_id"] else None
    return pos, (m.display_name if m else ("Vacant" if not r["user_id"] else f"User {r['user_id']}"))


async def section_options(gid, current=None):
    used = [r["section"] for r in await q("SELECT DISTINCT section FROM roster WHERE guild_id=%s", gid)]
    names = list(dict.fromkeys(SECTIONS + used))[:25]
    return [discord.SelectOption(label=n[:100], value=n[:100], default=(n == current)) for n in names]


class EntryModal(discord.ui.Modal):
    def __init__(self, cog, guild, opts, row=None):
        super().__init__(title="✏️ Edit position" if row else "➕ Add a position", timeout=600)
        self.cog, self.row = cog, row
        self.section = discord.ui.Label(text="Section", component=discord.ui.Select(options=opts))
        self.role = discord.ui.Label(text="Position (role)", description="Start typing the role name",
                                     component=discord.ui.RoleSelect(required=False, min_values=0,
                                         default_values=[discord.Object(row["role_id"])] if row and row["role_id"] else []))
        self.title_ = discord.ui.Label(text="…or a custom title", description="Only if there's no role for it",
                                       component=discord.ui.TextInput(required=False, max_length=80,
                                                                      default=(row["title"] if row else None) or None))
        self.user = discord.ui.Label(text="Who holds it", description="Start typing a name · leave empty for Vacant",
                                     component=discord.ui.UserSelect(required=False, min_values=0,
                                         default_values=[discord.Object(row["user_id"])] if row and row["user_id"] else []))
        for x in (self.section, self.role, self.title_, self.user):
            self.add_item(x)

    async def on_submit(self, inter):
        try:
            if not is_admin(inter.user, await cfg(inter.guild_id), "setup"):
                raise EconError("Only staff with the Server setup permission can change this.")
            sec = (self.section.component.values or [SECTIONS[0]])[0]
            role = self.role.component.values[0] if self.role.component.values else None
            title = (self.title_.component.value or "").strip() or None
            user = self.user.component.values[0] if self.user.component.values else None
            if not role and not title:
                raise EconError("Pick a role or type a custom title.")
            gid = inter.guild_id
            if self.row:
                await ex("UPDATE roster SET section=%s, role_id=%s, title=%s, user_id=%s WHERE id=%s AND guild_id=%s",
                         sec, role.id if role else None, title, user.id if user else None, self.row["id"], gid)
                what = "updated"
            else:
                last = await q1("SELECT COALESCE(MAX(sort),0) s FROM roster WHERE guild_id=%s AND section=%s", gid, sec)
                base = SECTIONS.index(sec) * 100 if sec in SECTIONS else 900
                await ex("INSERT INTO roster (guild_id,section,role_id,title,user_id,sort) VALUES (%s,%s,%s,%s,%s,%s)",
                         gid, sec, role.id if role else None, title, user.id if user else None, max(base, int(last["s"])) + 1)
                what = "added"
            pos = role.mention if role else f"**{title}**"
            e = emb(f"📌 Chain of command {what}", f"{pos} — {user.mention if user else '*Vacant*'} · by {inter.user.mention}", GREEN)
            await log(self.cog.bot, gid, e)
            await reply(inter, embed=e, ephemeral=True)
            await self.cog.sync()
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class PickEntry(discord.ui.View):
    def __init__(self, cog, guild, rows, action):
        super().__init__(timeout=300)
        self.cog, self.action, self.rows = cog, action, {r["id"]: r for r in rows}
        for start in range(0, min(len(rows), 50), 25):
            chunk = rows[start:start + 25]
            s = discord.ui.Select(placeholder=("Which position to edit?" if action == "edit" else "Which position to remove?")
                                  + (f" ({start + 1}–{start + len(chunk)})" if len(rows) > 25 else ""),
                                  options=[discord.SelectOption(label=f"{label(guild, r)[0]} — {label(guild, r)[1]}"[:100],
                                                                value=str(r["id"]), description=r["section"][:100]) for r in chunk])
            s.callback = self.picked
            self.add_item(s)

    async def picked(self, inter):
        r = self.rows.get(int(inter.data["values"][0]))
        if not r:
            return await inter.response.send_message("That position no longer exists.", ephemeral=True)
        if self.action == "edit":
            return await inter.response.send_modal(EntryModal(self.cog, inter.guild, await section_options(inter.guild_id, r["section"]), r))
        pos, who = label(inter.guild, r)
        v = discord.ui.View(timeout=120)
        b = discord.ui.Button(label=f"Remove {pos}", emoji="➖", style=discord.ButtonStyle.danger)

        async def confirm(i2):
            if not is_admin(i2.user, await cfg(i2.guild_id), "setup"):
                return await i2.response.send_message("Only staff with the Server setup permission can change this.", ephemeral=True)
            await ex("DELETE FROM roster WHERE id=%s AND guild_id=%s", r["id"], i2.guild_id)
            await log(self.cog.bot, i2.guild_id, emb("📌 Chain of command: position removed", f"{pos} — {who} · by {i2.user.mention}", GOLD))
            await i2.response.edit_message(content=f"➖ Removed **{pos}** ({who}).", view=None)
            await self.cog.sync()
        b.callback = confirm
        v.add_item(b)
        await inter.response.send_message(f"Remove **{pos}** — {who}? This takes the line off the card.", view=v, ephemeral=True)


class RosterCard(discord.ui.LayoutView):
    def __init__(self, cog, body=None):
        super().__init__(timeout=None)
        self.cog = cog
        btns = [discord.ui.Button(label="Add", emoji="➕", style=discord.ButtonStyle.success, custom_id="roster:add"),
                discord.ui.Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.primary, custom_id="roster:edit"),
                discord.ui.Button(label="Remove", emoji="➖", style=discord.ButtonStyle.danger, custom_id="roster:remove")]
        for b in btns:
            b.callback = self.on_button
        self.add_item(discord.ui.Container(*(body or [discord.ui.TextDisplay("## 📌 Chain of Command")]), discord.ui.Separator(),
                                           discord.ui.ActionRow(*btns),
                                           discord.ui.TextDisplay("-# ➕ ✏️ ➖ staff with the Server setup permission only"),
                                           accent_colour=discord.Colour.gold()))

    async def on_button(self, inter):
        k = inter.data["custom_id"].split(":")[1]
        try:
            if not is_admin(inter.user, await cfg(inter.guild_id), "setup"):
                raise EconError("Only staff with the Server setup permission can change the chain of command.")
            if k == "add":
                return await inter.response.send_modal(EntryModal(self.cog, inter.guild, await section_options(inter.guild_id)))
            rows = await entries(inter.guild_id)
            if not rows:
                raise EconError("There are no positions yet — press ➕ Add.")
            await inter.response.send_message(view=PickEntry(self.cog, inter.guild, rows, k), ephemeral=True)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class Roster(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(RosterCard(self))
        self.refresh.start()

    async def cog_load(self):
        await ex(SCHEMA)

    def cog_unload(self):
        self.refresh.cancel()

    @property
    def conf(self):
        return self.bot.state.settings.setdefault("roster", {})

    async def seed(self, gid):
        if self.conf.get("seeded") or await q1("SELECT 1 FROM roster WHERE guild_id=%s LIMIT 1", gid):
            self.conf["seeded"] = True
            return
        for i, (sec, role, user) in enumerate(SEED):
            await ex("INSERT INTO roster (guild_id,section,role_id,user_id,sort) VALUES (%s,%s,%s,%s,%s)",
                     gid, SECTIONS[sec], role, user, sec * 100 + i)
        self.conf["seeded"] = True
        self.bot.state.save()
        logger.info("seeded chain of command with %d positions", len(SEED))

    async def render(self, guild):
        rows = await entries(guild.id)
        order = list(dict.fromkeys(SECTIONS + [r["section"] for r in rows]))
        body = [discord.ui.TextDisplay("## 📌 Chain of Command")]
        for sec in order:
            items = [r for r in rows if r["section"] == sec]
            if not items:
                continue
            lines = []
            for r in items:
                pos = f"<@&{r['role_id']}>" if r["role_id"] and guild.get_role(r["role_id"]) else f"**{r['title'] or 'Position'}**"
                lines.append(f"{pos} — " + (f"<@{r['user_id']}>" if r["user_id"] else "*Vacant*"))
            body += [discord.ui.Separator(), discord.ui.TextDisplay(f"### {sec}\n" + "\n".join(lines))]
        body += [discord.ui.Separator(), discord.ui.TextDisplay(f"💬 {FOOTER}")]
        return RosterCard(self, body)

    async def sync(self):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            ch = g and g.get_channel(int(self.conf.get("channel_id") or CHANNEL_ID))
            if not ch:
                continue
            await self.seed(gid)
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
                self.conf.update(channel_id=ch.id, message_id=msg.id)
            self.bot.state.save()

    @tasks.loop(minutes=10)
    async def refresh(self):
        try:
            await self.sync()
        except Exception:
            logger.exception("chain of command refresh failed")

    @refresh.before_loop
    async def _w(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Roster(bot))
