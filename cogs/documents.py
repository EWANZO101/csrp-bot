"""Documents panel: posts/updates a branded message in the documents channel on startup, with link buttons to
each document on the web panel and an 'I have read the documents' button that grants a role."""
import discord
from discord import app_commands
from discord.ext import commands

import core
from core import EconError, q, cfg, set_cfg, emb, reply, log, is_admin, GREEN, RED, BLUE
from cogs.tickets import panel_url, brand_color


async def doc_link(bot, d):
    if d["kind"] == "link" and d["url"]:
        return d["url"]
    return panel_url(bot, f"/docs/{d['slug']}")


class Documents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def post_panel(self, guild: discord.Guild, force_new=False):
        c = await cfg(guild.id)
        ch = guild.get_channel(c.get("docs_channel") or 0)
        if not ch:
            return "No documents channel set."
        docs = await q("SELECT * FROM documents WHERE guild_id=%s AND in_panel=1 AND visibility<>'staff' "
                       "ORDER BY position, title", guild.id)
        e = discord.Embed(title=f"📚 {c['docs_title']}", description=c["docs_text"], color=brand_color(c))
        cats = {}
        for d in docs:
            cats.setdefault(d["category"] or "General", []).append(d)
        for cat, items in list(cats.items())[:20]:
            val = "\n".join(f"{d['icon'] or '📄'} **{d['title']}**" + (f" — {d['summary']}" if d["summary"] else "") for d in items)
            e.add_field(name=cat, value=val[:1024], inline=False)
        if not docs:
            e.add_field(name="​", value="*No documents yet — add them in the web panel.*")
        index = panel_url(self.bot, "/docs")
        e.set_footer(text=f"{c['brand_full'] or c['brand_name']}" + (" · all documents on the web panel" if index else ""))
        view = discord.ui.View(timeout=None)
        n = 0
        for d in docs:
            url = await doc_link(self.bot, d)
            if not url or n >= 20:
                continue
            view.add_item(discord.ui.Button(label=d["title"][:80], emoji=d["icon"] or "📄", url=url, row=n // 5))
            n += 1
        row = min(4, (n + 4) // 5)
        if c.get("docs_ack_role"):
            view.add_item(discord.ui.Button(label="I have read the documents", emoji="✅", style=discord.ButtonStyle.success,
                                            custom_id="docs:ack", row=row))
        elif index and n < 25:
            view.add_item(discord.ui.Button(label="All documents", emoji="🌐", url=index, row=row))
        msg = None
        if c.get("docs_message") and not force_new:
            try:
                msg = await ch.fetch_message(c["docs_message"])
                await msg.edit(embed=e, view=view)
            except discord.HTTPException:
                msg = None
        if msg is None:
            msg = await ch.send(embed=e, view=view)
            await set_cfg(guild.id, docs_message=msg.id)
        return None

    @commands.Cog.listener()
    async def on_ready(self):
        g = self.bot.get_guild(self.bot.state.guild_id or 0)
        if g:
            try:
                await self.post_panel(g)
            except Exception as e:
                print(f"[documents] panel: {e!r}")

    @commands.Cog.listener()
    async def on_interaction(self, inter: discord.Interaction):
        if inter.type != discord.InteractionType.component or (inter.data or {}).get("custom_id") != "docs:ack":
            return
        c = await cfg(inter.guild_id)
        role = inter.guild.get_role(c.get("docs_ack_role") or 0)
        if not role:
            return await inter.response.send_message("Thanks for reading! ✅", ephemeral=True)
        if role in inter.user.roles:
            return await inter.response.send_message(f"You already have {role.mention}. ✅", ephemeral=True)
        try:
            await inter.user.add_roles(role, reason="Acknowledged server documents")
        except discord.Forbidden:
            return await inter.response.send_message("I can't give that role — staff need to move my role above it.", ephemeral=True)
        await inter.response.send_message(f"✅ Thanks! You've been given {role.mention}.", ephemeral=True)
        await log(self.bot, inter.guild_id, emb("📚 Documents acknowledged", f"{inter.user.mention} → {role.mention}", GREEN))

    @app_commands.command(name="docs", description="Server documents (SOPs, penal codes, links)")
    @app_commands.guild_only()
    async def docs(self, inter: discord.Interaction):
        c = await cfg(inter.guild_id)
        staff = is_admin(inter.user, c, "documents")
        docs = await q("SELECT * FROM documents WHERE guild_id=%s AND (visibility<>'staff' OR %s) ORDER BY position, title",
                       inter.guild_id, staff)
        lines = []
        for d in docs:
            url = await doc_link(self.bot, d)
            lines.append(f"{d['icon'] or '📄'} " + (f"[{d['title']}]({url})" if url else f"**{d['title']}**")
                         + (f" — {d['summary']}" if d["summary"] else ""))
        e = discord.Embed(title=f"📚 {c['docs_title']}", description="\n".join(lines)[:4000] or "No documents yet.",
                          color=brand_color(c))
        await reply(inter, embed=e, ephemeral=True)

    @app_commands.command(name="docs_panel", description="Post or refresh the documents panel (admin)")
    @app_commands.guild_only()
    async def docs_panel(self, inter: discord.Interaction, new_message: bool = False):
        await core.require_admin(inter, "setup")
        err = await self.post_panel(inter.guild, force_new=new_message)
        if err:
            raise EconError(err + " Set it in the web panel → Setup.")
        await reply(inter, embed=emb("✅ Documents panel updated", None, GREEN), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Documents(bot))
