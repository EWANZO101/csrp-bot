"""#player-accounts: ONE staff-only, self-updating rich message listing every player's wallet, bank and inventory.

A single card with a compact table (richest first, paged with ◀ ▶), plus a player-search box that shows that
player's full account (all items, businesses) privately to whoever searched. Refreshed every minute.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import core
from core import q, q1, cfg, money, is_admin, ensure_user, EconError, emb, RED

logger = logging.getLogger("economy.directory")
CHANNEL_NAME = "player-accounts"
PER_PAGE = 20


async def staff_ok(inter) -> bool:
    c = await cfg(inter.guild_id)
    if is_admin(inter.user, c, "economy"):
        return True
    await inter.response.send_message(embed=emb("❌ Staff only (Citizens & money permission).", None, RED), ephemeral=True)
    return False


def controls(page: int, pages: int):
    """The interactive rows. custom_ids are fixed so the buttons keep working after restarts."""
    prev = discord.ui.Button(emoji="◀️", label="Prev", custom_id="dir:prev", style=discord.ButtonStyle.secondary, disabled=page <= 0)
    pos = discord.ui.Button(label=f"Page {page + 1} / {pages}", custom_id="dir:refresh", style=discord.ButtonStyle.secondary)
    nxt = discord.ui.Button(emoji="▶️", label="Next", custom_id="dir:next", style=discord.ButtonStyle.secondary, disabled=page >= pages - 1)
    pick = discord.ui.UserSelect(custom_id="dir:pick", placeholder="🔎 Type a name to see a player's full account…")
    return discord.ui.ActionRow(prev, pos, nxt), discord.ui.ActionRow(pick)


class DirView(discord.ui.LayoutView):
    """Used both for rendering and (empty-content) as the persistent handler of the controls."""

    def __init__(self, cog, body=None, page=0, pages=1):
        super().__init__(timeout=None)
        self.cog = cog
        row1, row2 = controls(page, pages)
        for b in row1.children:
            b.callback = self.on_button
        row2.children[0].callback = self.on_pick
        self.add_item(discord.ui.Container(*(body or [discord.ui.TextDisplay("Loading…")]), discord.ui.Separator(), row1, row2,
                                           accent_colour=discord.Colour.gold()))

    async def on_button(self, inter: discord.Interaction):
        if not await staff_ok(inter):
            return
        cid = inter.data.get("custom_id")
        conf = self.cog.conf
        conf["page"] = max(0, int(conf.get("page", 0)) + (1 if cid == "dir:next" else -1 if cid == "dir:prev" else 0))
        view = await self.cog.render(inter.guild)
        await inter.response.edit_message(view=view)

    async def on_pick(self, inter: discord.Interaction):
        if not await staff_ok(inter):
            return
        uid = int(inter.data["values"][0])
        member = inter.guild.get_member(uid)
        await inter.response.send_message(view=await self.cog.full_card(inter.guild, uid, member), ephemeral=True)


class Directory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(DirView(self))
        self.refresh.start()

    def cog_unload(self):
        self.refresh.cancel()

    @property
    def conf(self):
        return self.bot.state.settings.setdefault("directory", {})

    async def render(self, guild):
        gid = guild.id
        c = await cfg(gid)
        rows = await q("SELECT u.*, (SELECT COALESCE(SUM(qty),0) FROM user_items i WHERE i.guild_id=u.guild_id AND i.user_id=u.user_id "
                       "AND qty>0) items FROM users u WHERE guild_id=%s ORDER BY wallet+bank DESC, user_id", gid)
        pages = max(1, -(-len(rows) // PER_PAGE))
        page = min(int(self.conf.get("page", 0)), pages - 1)
        self.conf["page"] = page
        tot_w = sum(r["wallet"] for r in rows)
        tot_b = sum(r["bank"] for r in rows)
        lines = []
        for n, r in enumerate(rows[page * PER_PAGE:(page + 1) * PER_PAGE], page * PER_PAGE + 1):
            m = guild.get_member(r["user_id"])
            name = (m.display_name if m else f"left server · {r['user_id']}")[:24]
            lines.append(f"`{n:>2}` **{discord.utils.escape_markdown(name)}**{' 🧊' if r['frozen'] else ''} — "
                         f"💵 {money(c, r['wallet'])} · 🏦 {money(c, r['bank'])} · 🎒 {r['items']}")
        body = [
            discord.ui.TextDisplay(f"## 📒 Player accounts\n**{len(rows)}** players · 💵 {money(c, tot_w)} in wallets · "
                                   f"🏦 {money(c, tot_b)} in banks · Σ **{money(c, tot_w + tot_b)}**\n"
                                   f"-# Richest first · auto-updates every minute · updated <t:{int(discord.utils.utcnow().timestamp())}:R>"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines) or "*No accounts yet.*"),
        ]
        return DirView(self, body, page, pages)

    async def full_card(self, guild, uid, member):
        gid = guild.id
        c = await cfg(gid)
        await ensure_user(gid, uid)
        r = await q1("SELECT * FROM users WHERE guild_id=%s AND user_id=%s", gid, uid)
        items = await q("SELECT name, qty FROM user_items WHERE guild_id=%s AND user_id=%s AND qty>0 ORDER BY name", gid, uid)
        biz = await q("SELECT name, balance FROM orgs WHERE guild_id=%s AND owner_id=%s ORDER BY name", gid, uid)
        name = member.display_name if member else f"User {uid}"
        head = (f"## {discord.utils.escape_markdown(name)}{' 🧊 frozen' if r['frozen'] else ''}\n<@{uid}> · `{uid}`\n"
                f"💵 **{money(c, r['wallet'])}**  ·  🏦 **{money(c, r['bank'])}**  ·  Σ {money(c, r['wallet'] + r['bank'])}")
        top = discord.ui.Section(discord.ui.TextDisplay(head), accessory=discord.ui.Thumbnail(member.display_avatar.url)) \
            if member else discord.ui.TextDisplay(head)
        parts = [top]
        if biz:
            parts += [discord.ui.Separator(), discord.ui.TextDisplay(
                "**🏢 Businesses**\n" + "\n".join(f"• {b['name']} — {money(c, b['balance'])}" for b in biz))]
        inv = "\n".join(f"• {i['name']} ×{i['qty']}" for i in items) or "*empty*"
        if len(inv) > 3000:
            inv = inv[:3000].rsplit("\n", 1)[0] + f"\n… and more ({len(items)} items total)"
        parts += [discord.ui.Separator(), discord.ui.TextDisplay(f"**🎒 Inventory ({len(items)})**\n{inv}")]
        v = discord.ui.LayoutView(timeout=None)
        v.add_item(discord.ui.Container(*parts, accent_colour=discord.Colour.blurple()))
        return v

    async def channel(self, guild):
        ch = guild.get_channel(int(self.conf.get("channel_id") or 0))
        panel = self.bot.get_cog("StaffPanel")
        ow = await panel.overwrites(guild) if panel else {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        for target, perm in ow.items():
            if target != guild.me:
                perm.send_messages = False
        if not ch:
            c = await cfg(guild.id)
            logch = guild.get_channel(c.get("log_channel") or 0)
            ch = await guild.create_text_channel(CHANNEL_NAME, overwrites=ow, category=getattr(logch, "category", None),
                                                 topic="Every player's wallet, bank and inventory — staff only, auto-updating")
            self.conf.update(channel_id=ch.id, message_id=None)
            self.bot.state.save()
        return ch, ow

    async def sync(self, resync_perms=False):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            ch, ow = await self.channel(g)
            if resync_perms:
                await ch.edit(overwrites=ow)
            # one-time cleanup of the old multi-message layout
            for old in self.conf.pop("message_ids", None) or []:
                try:
                    await ch.get_partial_message(old).delete()
                except discord.HTTPException:
                    pass
            view = await self.render(g)
            mid = self.conf.get("message_id")
            ok = False
            if mid:
                try:
                    await ch.get_partial_message(int(mid)).edit(view=view)
                    ok = True
                except discord.NotFound:
                    pass
            if not ok:
                msg = await ch.send(view=view)
                self.conf["message_id"] = msg.id
            self.bot.state.save()

    @tasks.loop(minutes=1)
    async def refresh(self):
        try:
            await self.sync()
        except discord.Forbidden:
            logger.warning("player accounts: missing permission")
        except Exception:
            logger.exception("player accounts refresh failed")

    @refresh.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="player_accounts", description="Refresh #player-accounts now and re-sync who can see it")
    @app_commands.guild_only()
    async def player_accounts(self, inter: discord.Interaction):
        c = await cfg(inter.guild_id)
        if not is_admin(inter.user, c, "economy"):
            raise EconError("You need the **Citizens & money** staff permission for this.")
        await inter.response.defer(ephemeral=True)
        await self.sync(resync_perms=True)
        await inter.followup.send(f"✅ Refreshed <#{self.conf['channel_id']}>.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Directory(bot))
