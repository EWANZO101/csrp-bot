import discord
from discord import app_commands
from discord.ext import commands

import core
from core import EconError, q, q1, cfg, set_cfg, emb, reply, GREEN, RED
from cogs.tickets import panel_url, brand_color
import apps_lib


class DenyModal(discord.ui.Modal, title="Deny application"):
    reason = discord.ui.TextInput(label="Reason (sent to the applicant)", style=discord.TextStyle.paragraph, max_length=900,
                                  required=False)

    def __init__(self, app_id):
        super().__init__()
        self.app_id = app_id

    async def on_submit(self, inter):
        note = await apps_lib.decide(inter.client, inter.guild, self.app_id, False, inter.user.id, self.reason.value or None)
        await inter.response.send_message(f"❌ Application #{self.app_id} denied. {note}", ephemeral=True)

    async def on_error(self, inter, error):
        await reply(inter, embed=emb("❌ " + (str(error) if isinstance(error, EconError) else "Failed."), None, RED), ephemeral=True)


class Applications(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def post_panel(self, guild: discord.Guild, force_new=False):
        c = await cfg(guild.id)
        ch = guild.get_channel(c.get("apps_channel") or 0)
        if not ch:
            return "No applications channel set."
        forms = [apps_lib.form_dict(r) for r in await q("SELECT * FROM app_forms WHERE guild_id=%s ORDER BY position, name", guild.id)]
        e = discord.Embed(title=f"📝 {c['apps_title']}", description=c["apps_text"], color=brand_color(c))
        view = discord.ui.View(timeout=None)
        for f in forms[:20]:
            reqs = []
            r = f["requirements"]
            if r.get("min_account_days"): reqs.append(f"account {r['min_account_days']}d+")
            if r.get("min_member_days"): reqs.append(f"member {r['min_member_days']}d+")
            if r.get("docs_ack"): reqs.append("read the documents")
            if r.get("roles_required"): reqs.append(" ".join(f"<@&{x}>" for x in r["roles_required"]))
            val = (f["description"] or "")[:300]
            if reqs:
                val += f"\n**Requirements:** {', '.join(reqs)}"
            e.add_field(name=f"{f['icon'] or '📝'} {f['name']} — {'🟢 Open' if f['is_open'] else '🔴 Closed'}",
                        value=val[:1024] or "​", inline=False)
            url = panel_url(self.bot, f"/#apply-{f['id']}")
            if f["is_open"] and url and len(view.children) < 25:
                view.add_item(discord.ui.Button(label=f"Apply: {f['name']}"[:80], emoji=f["icon"] or "📝", url=url))
        if not forms:
            e.add_field(name="​", value="*No applications are open right now.*")
        e.set_footer(text=c["brand_full"] or c["brand_name"])
        msg = None
        if c.get("apps_message") and not force_new:
            try:
                msg = await ch.fetch_message(c["apps_message"])
                await msg.edit(embed=e, view=view)
            except discord.HTTPException:
                msg = None
        if msg is None:
            msg = await ch.send(embed=e, view=view)
            await set_cfg(guild.id, apps_message=msg.id)

    @commands.Cog.listener()
    async def on_ready(self):
        g = self.bot.get_guild(self.bot.state.guild_id or 0)
        if g:
            try:
                await self.post_panel(g)
            except Exception as e:
                print(f"[applications] panel: {e!r}")

    @commands.Cog.listener()
    async def on_interaction(self, inter: discord.Interaction):
        if inter.type != discord.InteractionType.component:
            return
        cid = (inter.data or {}).get("custom_id", "")
        if not cid.startswith("app:"):
            return
        try:
            _, action, aid = cid.split(":")
            aid = int(aid)
            a = await q1("SELECT * FROM applications WHERE id=%s AND guild_id=%s", aid, inter.guild_id)
            if not a:
                raise EconError("Application not found.")
            frow = await q1("SELECT * FROM app_forms WHERE id=%s", a["form_id"]) if a["form_id"] else None
            form = apps_lib.form_dict(frow) if frow else {"reviewer_roles": []}
            if not apps_lib.can_review(inter.user, form, await cfg(inter.guild_id)):
                raise EconError("You can't review this application.")
            if action == "accept":
                await inter.response.defer(ephemeral=True)
                note = await apps_lib.decide(self.bot, inter.guild, aid, True, inter.user.id, None)
                await inter.followup.send(f"✅ Application #{aid} accepted. {note}", ephemeral=True)
            elif action == "deny":
                await inter.response.send_modal(DenyModal(aid))
        except EconError as e:
            await reply(inter, embed=emb("❌ " + str(e), None, RED), ephemeral=True)

    @app_commands.command(name="apply", description="See open applications and apply")
    @app_commands.guild_only()
    async def apply(self, inter: discord.Interaction):
        c = await cfg(inter.guild_id)
        forms = [apps_lib.form_dict(r) for r in await q("SELECT * FROM app_forms WHERE guild_id=%s AND is_open=1 "
                                                         "ORDER BY position, name", inter.guild_id)]
        if not forms:
            raise EconError("No applications are open right now.")
        e = discord.Embed(title=f"📝 {c['apps_title']}", color=brand_color(c))
        view = discord.ui.View()
        for f in forms[:20]:
            checks = await apps_lib.check_requirements(inter.user, f)
            ok = all(x["ok"] is not False for x in checks)
            lines = [("✅" if x["ok"] else "❌" if x["ok"] is False else "▫️") + " " + x["label"]
                     + (f" ({x['detail']})" if x.get("detail") and x["ok"] is False else "") for x in checks]
            e.add_field(name=f"{f['icon'] or '📝'} {f['name']}" + ("" if ok else " — requirements not met"),
                        value="\n".join(lines)[:1024] or "No requirements", inline=False)
            url = panel_url(self.bot, f"/#apply-{f['id']}")
            if ok and url:
                view.add_item(discord.ui.Button(label=f["name"][:80], emoji=f["icon"] or "📝", url=url))
        await reply(inter, embed=e, view=view, ephemeral=True)

    @app_commands.command(name="apply_panel", description="Post or refresh the applications panel (admin)")
    @app_commands.guild_only()
    async def apply_panel(self, inter: discord.Interaction, new_message: bool = False):
        await core.require_admin(inter, "setup")
        err = await self.post_panel(inter.guild, force_new=new_message)
        if err:
            raise EconError(err + " Set it in the web panel → Setup.")
        await reply(inter, embed=emb("✅ Applications panel updated", None, GREEN), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Applications(bot))
