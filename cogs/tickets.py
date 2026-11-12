"""Support tickets: panel with categories → private channel → claim/close → transcript (Discord + web panel)."""
import io, re, html, asyncio, datetime as dt
import discord
from discord import app_commands
from discord.ext import commands

import core
from core import EconError, q, q1, ex, cfg, set_cfg, emb, ts, reply, log, dm, is_admin, GREEN, RED, BLUE, GOLD


def panel_url(bot, path=""):
    base = bot.state.settings.get("public_url") or bot.state.settings.get("seen_url")
    return f"{base}{path}" if base else None


def brand_color(c):
    try:
        return int(str(c.get("brand_color", "#38b6ff")).lstrip("#"), 16)
    except ValueError:
        return BLUE


def is_staff(member: discord.Member, c: dict) -> bool:
    if is_admin(member, c, "tickets"):
        return True
    staff = {int(r) for r in c.get("ticket_staff_roles") or [] if str(r).isdigit()}
    return any(r.id in staff for r in member.roles)


class OpenModal(discord.ui.Modal):
    subject = discord.ui.TextInput(label="What do you need help with?", style=discord.TextStyle.paragraph,
                                   max_length=900, placeholder="Give staff as much detail as you can…")

    def __init__(self, cog, tcat):
        super().__init__(title=tcat["name"][:45])
        self.cog, self.tcat = cog, tcat

    async def on_submit(self, inter):
        await self.cog.open_ticket(inter, self.tcat, self.subject.value)

    async def on_error(self, inter, error):
        msg = str(error) if isinstance(error, EconError) else "Couldn't open the ticket."
        if not isinstance(error, EconError):
            core_log = __import__("logging").getLogger("economy.tickets")
            core_log.exception("ticket open failed", exc_info=error)
        await reply(inter, embed=emb("❌ " + msg, None, RED), ephemeral=True)


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ───────── panel ─────────
    async def post_panel(self, guild: discord.Guild, force_new=False):
        c = await cfg(guild.id)
        ch = guild.get_channel(c.get("ticket_panel_channel") or 0)
        if not ch:
            return "No ticket panel channel set."
        types = c.get("ticket_types") or []
        e = discord.Embed(title=f"🎫 {c['ticket_panel_title']}", description=c["ticket_panel_text"], color=brand_color(c))
        for t in types[:10]:
            e.add_field(name=f"{t.get('emoji') or '•'} {t['name']}", value=t.get("description") or "​", inline=False)
        e.set_footer(text=c["brand_full"] or c["brand_name"])
        view = discord.ui.View(timeout=None)
        if len(types) <= 5:
            for i, t in enumerate(types):
                view.add_item(discord.ui.Button(label=t["name"][:80], emoji=t.get("emoji") or None,
                                                style=discord.ButtonStyle.primary if i == 0 else discord.ButtonStyle.secondary,
                                                custom_id=f"ticket:open:{i}"))
        else:
            view.add_item(discord.ui.Select(custom_id="ticket:select", placeholder="Choose a ticket type…", options=[
                discord.SelectOption(label=t["name"][:100], value=str(i), emoji=t.get("emoji") or None,
                                     description=(t.get("description") or "")[:100]) for i, t in enumerate(types[:25])]))
        msg = None
        if c.get("ticket_panel_message") and not force_new:
            try:
                msg = await ch.fetch_message(c["ticket_panel_message"])
                await msg.edit(embed=e, view=view)
            except discord.HTTPException:
                msg = None
        if msg is None:
            msg = await ch.send(embed=e, view=view)
            await set_cfg(guild.id, ticket_panel_message=msg.id)
        return None

    @commands.Cog.listener()
    async def on_ready(self):
        g = self.bot.get_guild(self.bot.state.guild_id or 0)
        if g:
            try:
                await self.post_panel(g)
            except Exception as e:
                print(f"[tickets] panel: {e!r}")

    @commands.Cog.listener()
    async def on_interaction(self, inter: discord.Interaction):
        if inter.type != discord.InteractionType.component:
            return
        cid = (inter.data or {}).get("custom_id", "")
        if not cid.startswith("ticket:"):
            return
        try:
            c = await cfg(inter.guild_id)
            types = c.get("ticket_types") or []
            if cid.startswith("ticket:open:") or cid == "ticket:select":
                idx = int(cid.rsplit(":", 1)[1]) if cid.startswith("ticket:open:") else int(inter.data["values"][0])
                if idx >= len(types):
                    raise EconError("That ticket type no longer exists.")
                await inter.response.send_modal(OpenModal(self, types[idx]))
            elif cid == "ticket:claim":
                await self.claim(inter)
            elif cid == "ticket:close":
                v = discord.ui.View(timeout=60)
                v.add_item(discord.ui.Button(label="Close ticket", style=discord.ButtonStyle.danger, custom_id="ticket:close_yes"))
                await inter.response.send_message("Close this ticket? A transcript will be saved.", view=v, ephemeral=True)
            elif cid == "ticket:close_yes":
                await inter.response.edit_message(content="Closing…", view=None)
                await self.close(inter, None)
        except EconError as e:
            await reply(inter, embed=emb("❌ " + str(e), None, RED), ephemeral=True)

    # ───────── open / claim / close ─────────
    async def open_ticket(self, inter: discord.Interaction, tcat: dict, subject: str):
        g, user = inter.guild, inter.user
        c = await cfg(g.id)
        n = await q1("SELECT COUNT(*) n FROM tickets WHERE guild_id=%s AND user_id=%s AND status='open'", g.id, user.id)
        if n["n"] >= int(c.get("ticket_max_open") or 2):
            raise EconError(f"You already have {n['n']} open ticket(s). Please use those first.")
        await inter.response.defer(ephemeral=True, thinking=True)
        _, tid = await ex("INSERT INTO tickets (guild_id,user_id,category,subject) VALUES (%s,%s,%s,%s)",
                          g.id, user.id, tcat["name"], subject[:300])
        category = g.get_channel(c.get("ticket_category") or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = None
        ow = {
            g.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True,
                                              embed_links=True),
            g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                              read_message_history=True, embed_links=True, attach_files=True),
        }
        staff_mentions = []
        for rid in c.get("ticket_staff_roles") or []:
            role = g.get_role(int(rid))
            if role:
                ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                                       read_message_history=True, manage_messages=True)
                staff_mentions.append(role.mention)
        name = re.sub(r"[^a-z0-9-]", "", f"ticket-{tid:04d}-{user.name.lower()}")[:90]
        try:
            ch = await g.create_text_channel(name, category=category, overwrites=ow,
                                             topic=f"{tcat['name']} · {user} · #{tid}", reason=f"Ticket #{tid}")
        except discord.Forbidden:
            await ex("DELETE FROM tickets WHERE id=%s", tid)
            raise EconError("I need the **Manage Channels** permission to open tickets.")
        await ex("UPDATE tickets SET channel_id=%s WHERE id=%s", ch.id, tid)
        e = discord.Embed(title=f"{tcat.get('emoji') or '🎫'} {tcat['name']} — Ticket #{tid}", description=subject,
                          color=brand_color(c))
        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.add_field(name="Opened by", value=user.mention)
        e.add_field(name="Status", value="🟢 Open — waiting for staff")
        e.set_footer(text="Staff: press Claim to take this ticket. Anyone: Close when resolved.")
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="Claim", emoji="🙋", style=discord.ButtonStyle.success, custom_id="ticket:claim"))
        v.add_item(discord.ui.Button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close"))
        await ch.send(content=f"{user.mention} {' '.join(staff_mentions)}", embed=e, view=v,
                      allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await inter.followup.send(embed=emb("🎫 Ticket opened", f"Head over to {ch.mention}.", GREEN), ephemeral=True)
        await self.log(g, emb("🎫 Ticket opened", f"#{tid} · {tcat['name']} · {user.mention} · {ch.mention}", GREEN))

    async def ticket_here(self, inter) -> dict:
        t = await q1("SELECT * FROM tickets WHERE channel_id=%s AND status='open'", inter.channel_id)
        if not t:
            raise EconError("This isn't an open ticket channel.")
        return t

    async def claim(self, inter):
        t = await self.ticket_here(inter)
        c = await cfg(inter.guild_id)
        if not is_staff(inter.user, c):
            raise EconError("Only staff can claim tickets.")
        if t["claimed_by"]:
            raise EconError(f"Already claimed by <@{t['claimed_by']}>.")
        await ex("UPDATE tickets SET claimed_by=%s WHERE id=%s", inter.user.id, t["id"])
        await inter.response.send_message(embed=emb(None, f"🙋 {inter.user.mention} has claimed this ticket and will help you.", GREEN))

    async def transcript(self, ch: discord.TextChannel) -> str:
        lines = []
        async for m in ch.history(limit=2000, oldest_first=True):
            content = m.content or ""
            for e in m.embeds:
                content += f"\n[embed] {e.title or ''} {e.description or ''}".rstrip()
            for a in m.attachments:
                content += f"\n[attachment] {a.url}"
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author} ({m.author.id}){' [bot]' if m.author.bot else ''}: {content}")
        return "\n".join(lines)

    async def close(self, inter, reason: str | None):
        t = await self.ticket_here(inter)
        c = await cfg(inter.guild_id)
        if inter.user.id != t["user_id"] and not is_staff(inter.user, c):
            raise EconError("Only the ticket owner or staff can close it.")
        ch = inter.channel
        text = await self.transcript(ch)
        await ex("UPDATE tickets SET status='closed', closed_at=UTC_TIMESTAMP(), closed_by=%s, close_reason=%s, transcript=%s "
                 "WHERE id=%s", inter.user.id, (reason or "")[:300] or None, text, t["id"])
        await ch.send(embed=emb("🔒 Ticket closed", f"Closed by {inter.user.mention}" + (f"\n> {reason}" if reason else "")
                                + "\nThis channel will be deleted in 5 seconds.", GOLD))
        url = panel_url(self.bot, f"/#ticket-{t['id']}")
        e = emb(f"🔒 Ticket #{t['id']} closed", f"**{t['category']}** · opened by <@{t['user_id']}> · closed by {inter.user.mention}"
                + (f"\nClaimed by <@{t['claimed_by']}>" if t["claimed_by"] else "") + (f"\n> {reason}" if reason else ""), GOLD)
        if url:
            e.add_field(name="Transcript", value=f"[View in panel]({url})")
        await self.log(inter.guild, e, file=discord.File(io.BytesIO(text.encode()), filename=f"ticket-{t['id']}.txt"))
        await dm(self.bot, t["user_id"], emb(f"🔒 Your ticket #{t['id']} in {inter.guild.name} was closed",
                                            (f"> {reason}\n" if reason else "") + "Thanks for reaching out!", GOLD))
        await asyncio.sleep(5)
        try:
            await ch.delete(reason=f"Ticket #{t['id']} closed")
        except discord.HTTPException:
            pass

    async def log(self, guild, embed, file=None):
        c = await cfg(guild.id)
        ch = guild.get_channel(c.get("ticket_log_channel") or 0) or guild.get_channel(c.get("log_channel") or 0)
        if ch:
            try:
                await ch.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    # ───────── slash commands ─────────
    ticket = app_commands.Group(name="ticket", description="Support tickets", guild_only=True)

    @ticket.command(name="close", description="Close this ticket")
    async def close_cmd(self, inter: discord.Interaction, reason: str | None = None):
        await inter.response.send_message("Closing…", ephemeral=True)
        await self.close(inter, reason)

    @ticket.command(name="add", description="Add someone to this ticket")
    async def add(self, inter: discord.Interaction, user: discord.Member):
        await self.ticket_here(inter)
        if not is_staff(inter.user, await cfg(inter.guild_id)):
            raise EconError("Staff only.")
        await inter.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
        await reply(inter, embed=emb(None, f"➕ {user.mention} added to the ticket.", GREEN))

    @ticket.command(name="remove", description="Remove someone from this ticket")
    async def remove(self, inter: discord.Interaction, user: discord.Member):
        await self.ticket_here(inter)
        if not is_staff(inter.user, await cfg(inter.guild_id)):
            raise EconError("Staff only.")
        await inter.channel.set_permissions(user, overwrite=None)
        await reply(inter, embed=emb(None, f"➖ {user.mention} removed from the ticket.", GOLD))

    @ticket.command(name="rename", description="Rename this ticket channel")
    async def rename(self, inter: discord.Interaction, name: app_commands.Range[str, 2, 80]):
        await self.ticket_here(inter)
        if not is_staff(inter.user, await cfg(inter.guild_id)):
            raise EconError("Staff only.")
        await inter.channel.edit(name=name)
        await reply(inter, embed=emb(None, f"✏️ Renamed to **{name}**", GREEN), ephemeral=True)

    @ticket.command(name="panel", description="Post or refresh the ticket panel (admin)")
    async def panel(self, inter: discord.Interaction, new_message: bool = False):
        await core.require_admin(inter, "setup")
        err = await self.post_panel(inter.guild, force_new=new_message)
        if err:
            raise EconError(err + " Set it in the web panel → Setup.")
        await reply(inter, embed=emb("✅ Ticket panel updated", None, GREEN), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
