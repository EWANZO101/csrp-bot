import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands

import core
from core import (EconError, q, q1, ex, cfg, money, emb, ts, parse_amount, transfer, ensure_user, find_org,
                  require_level, org_level, is_admin, log, dm, reply, GREEN, BLUE, GOLD, RED, HEAD, MEMBER, BURN)
from cogs.orgs import org_autocomplete

SRC = [app_commands.Choice(name="Bank", value="bank"), app_commands.Choice(name="Wallet", value="wallet")]


async def cit_ac(inter: discord.Interaction, current: str):
    rows = await q("SELECT c.id, c.amount, c.late_fee, c.reason, c.user_id FROM citations c WHERE c.guild_id=%s "
                   "AND c.status='unpaid' AND (c.user_id=%s OR CAST(c.id AS CHAR) LIKE %s) ORDER BY c.id DESC LIMIT 25",
                   inter.guild_id, inter.user.id, f"{current}%")
    cf = await cfg(inter.guild_id)
    return [app_commands.Choice(name=f"#{r['id']} · {money(cf, r['amount'] + r['late_fee'])} · {r['reason']}"[:100],
                                value=r["id"]) for r in rows]


async def get_cit(gid, cid):
    r = await q1("SELECT c.*, o.name dept FROM citations c LEFT JOIN orgs o ON o.id=c.dept_id "
                 "WHERE c.guild_id=%s AND c.id=%s", gid, cid)
    if not r:
        raise EconError(f"No citation #{cid}.")
    return r


def cit_embed(c, r):
    color = {"unpaid": RED, "paid": GREEN, "void": 0x95A5A6}[r["status"]]
    overdue = r["status"] == "unpaid" and r["due_at"] and r["due_at"] < dt.datetime.utcnow()
    e = emb(f"🚨 Citation #{r['id']}" + (" — OVERDUE" if overdue else ""), r["reason"], color)
    e.add_field(name="Citizen", value=f"<@{r['user_id']}>")
    e.add_field(name="Issued by", value=f"<@{r['issuer_id']}>" + (f"\n{r['dept']}" if r.get("dept") else ""))
    e.add_field(name="Status", value=r["status"].upper())
    e.add_field(name="Fine", value=money(c, r["amount"]))
    if r["late_fee"]:
        e.add_field(name="Late fee", value=money(c, r["late_fee"]))
    e.add_field(name="Total due" if r["status"] == "unpaid" else "Total", value=f"**{money(c, r['amount'] + r['late_fee'])}**")
    e.add_field(name="Issued", value=ts(r["issued_at"], "f"))
    if r["status"] == "unpaid":
        e.add_field(name="Due", value=ts(r["due_at"]))
    elif r["status"] == "paid":
        e.add_field(name="Paid", value=f"{ts(r['paid_at'])} by <@{r['paid_by']}>")
    if r["note"]:
        e.add_field(name="Note", value=r["note"], inline=False)
    return e


class PayButton(discord.ui.View):
    def __init__(self, cid):
        super().__init__(timeout=None)
        self.cid = cid

    @discord.ui.button(label="Pay now", emoji="💳", style=discord.ButtonStyle.success)
    async def pay(self, inter, b):
        e = await pay_citation(inter, self.cid, "bank")
        await inter.response.send_message(embed=e, ephemeral=True)


async def pay_citation(inter, cid, source):
    gid = inter.guild_id
    c = await cfg(gid)
    r = await get_cit(gid, cid)
    if r["status"] != "unpaid":
        raise EconError(f"Citation #{cid} is already {r['status']}.")
    await ensure_user(gid, inter.user.id)
    total = r["amount"] + r["late_fee"]
    dst = ("org", r["dept_id"]) if r["dept_id"] else (("org", c["treasury_dept"]) if c["treasury_dept"] else BURN)

    async def extra(cur):
        await cur.execute("UPDATE citations SET status='paid', paid_at=UTC_TIMESTAMP(), paid_by=%s "
                          "WHERE id=%s AND status='unpaid'", (inter.user.id, cid))
        if cur.rowcount != 1:
            raise EconError("That citation was just paid or voided.")
    await transfer(gid, [((source, inter.user.id), dst, total)], inter.user.id, "citation", f"Citation #{cid}", extra=extra)
    await core.citation_paid(cid)
    e = emb("✅ Citation paid", f"#{cid} · **{money(c, total)}** paid by {inter.user.mention}"
            + (f" for <@{r['user_id']}>" if r["user_id"] != inter.user.id else ""), GREEN)
    await log(inter.client, gid, e)
    return e


class Citations(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    citation = app_commands.Group(name="citation", description="Fines & citations", guild_only=True)

    @citation.command(name="issue", description="Issue a citation/fine (department members)")
    @app_commands.rename(org="department")
    @app_commands.autocomplete(org=org_autocomplete("dept"))
    @app_commands.describe(due_days="Days to pay before a late fee (default from settings)")
    async def issue(self, inter: discord.Interaction, org: str, user: discord.Member, amount: str,
                    reason: app_commands.Range[str, 3, 500], due_days: app_commands.Range[int, 1, 365] | None = None):
        gid = inter.guild_id
        o = await find_org(gid, org, "dept")
        await require_level(inter.user, o, MEMBER, "issue citations")
        if user.bot:
            raise EconError("Can't fine a bot.")
        c = await cfg(gid)
        amt = parse_amount(amount)
        await ensure_user(gid, user.id)
        days = due_days or c["citation_days"]
        _, cid = await ex("INSERT INTO citations (guild_id,dept_id,user_id,issuer_id,amount,reason,due_at) "
                          "VALUES (%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP() + INTERVAL %s DAY)",
                          gid, o["id"], user.id, inter.user.id, amt, reason, days)
        r = await get_cit(gid, cid)
        e = cit_embed(c, r)
        await reply(inter, content=user.mention, embed=e)
        await log(inter.client, gid, e)
        await dm(self.bot, user.id, emb(f"🚨 You received a citation in {inter.guild.name}",
                 f"**{money(c, amt)}** — {reason}\nDue {ts(r['due_at'])}. Pay with `/citation pay {cid}`.", RED), gid)

    @citation.command(name="pay", description="Pay a citation (yours or someone else's)")
    @app_commands.autocomplete(id=cit_ac)
    @app_commands.choices(source=SRC)
    async def pay(self, inter: discord.Interaction, id: int, source: str = "bank"):
        await reply(inter, embed=await pay_citation(inter, id, source))

    @citation.command(name="mine", description="All citations issued to you")
    async def mine(self, inter: discord.Interaction):
        await self._list(inter, inter.user, "all", ephemeral=True)

    @citation.command(name="list", description="Search a user's citations")
    @app_commands.choices(status=[app_commands.Choice(name=s.title(), value=s) for s in ("unpaid", "paid", "void", "all")])
    async def list_(self, inter: discord.Interaction, user: discord.Member, status: str = "unpaid"):
        await self._list(inter, user, status)

    async def _list(self, inter, user, status, ephemeral=False):
        c = await cfg(inter.guild_id)
        cond = "" if status == "all" else " AND status=%s"
        args = [inter.guild_id, user.id] + ([] if status == "all" else [status])
        rows = await q(f"SELECT * FROM citations WHERE guild_id=%s AND user_id=%s{cond} ORDER BY id DESC LIMIT 25", *args)
        icon = {"unpaid": "🔴", "paid": "🟢", "void": "⚪"}
        now = dt.datetime.utcnow()
        lines = [f"{icon[r['status']]} `#{r['id']}` **{money(c, r['amount'] + r['late_fee'])}** — {r['reason'][:60]}"
                 + (" · **OVERDUE**" if r["status"] == "unpaid" and r["due_at"] < now else
                    (f" · due {ts(r['due_at'])}" if r["status"] == "unpaid" else "")) for r in rows]
        owed = sum(r["amount"] + r["late_fee"] for r in rows if r["status"] == "unpaid")
        e = emb(f"🚨 {user.display_name}'s citations ({status})", "\n".join(lines)[:4000] or "None.", RED if owed else GREEN)
        if owed:
            e.set_footer(text=f"Outstanding: {money(c, owed)}")
        await reply(inter, embed=e, ephemeral=ephemeral)

    @citation.command(name="info", description="Full details of a citation")
    @app_commands.autocomplete(id=cit_ac)
    async def info(self, inter: discord.Interaction, id: int):
        c = await cfg(inter.guild_id)
        r = await get_cit(inter.guild_id, id)
        await reply(inter, embed=cit_embed(c, r), view=PayButton(id) if r["status"] == "unpaid" else None)

    @citation.command(name="edit", description="Edit a citation (department head)")
    @app_commands.autocomplete(id=cit_ac)
    async def edit(self, inter: discord.Interaction, id: int, amount: str | None = None,
                   reason: app_commands.Range[str, 3, 500] | None = None, extend_days: app_commands.Range[int, 1, 365] | None = None,
                   note: app_commands.Range[str, 0, 300] | None = None):
        gid = inter.guild_id
        r = await get_cit(gid, id)
        await self._head_check(inter, r)
        if r["status"] != "unpaid":
            raise EconError("Only unpaid citations can be edited.")
        if amount: await ex("UPDATE citations SET amount=%s WHERE id=%s", parse_amount(amount), id)
        if reason: await ex("UPDATE citations SET reason=%s WHERE id=%s", reason, id)
        if extend_days: await ex("UPDATE citations SET due_at=GREATEST(due_at, UTC_TIMESTAMP()) + INTERVAL %s DAY WHERE id=%s", extend_days, id)
        if note is not None: await ex("UPDATE citations SET note=%s WHERE id=%s", note, id)
        c = await cfg(gid)
        e = cit_embed(c, await get_cit(gid, id))
        e.title = "✏️ " + e.title + " (edited)"
        await log(inter.client, gid, e)
        await reply(inter, embed=e)

    @citation.command(name="void", description="Cancel a citation (department head)")
    @app_commands.autocomplete(id=cit_ac)
    async def void(self, inter: discord.Interaction, id: int, reason: app_commands.Range[str, 0, 300] | None = None):
        gid = inter.guild_id
        r = await get_cit(gid, id)
        await self._head_check(inter, r)
        if r["status"] != "unpaid":
            raise EconError(f"Citation is already {r['status']}.")
        await ex("UPDATE citations SET status='void', note=%s WHERE id=%s", reason or f"Voided by {inter.user}", id)
        e = emb("⚪ Citation voided", f"#{id} for <@{r['user_id']}> by {inter.user.mention}" + (f"\n> {reason}" if reason else ""), GOLD)
        await log(inter.client, gid, e)
        await reply(inter, embed=e)

    async def _head_check(self, inter, r):
        if r["dept_id"]:
            o = await find_org(inter.guild_id, f"#{r['dept_id']}", "dept")
            if not is_admin(inter.user, await cfg(inter.guild_id), "citations"):
                await require_level(inter.user, o, HEAD, "change citations")
        elif not is_admin(inter.user, await cfg(inter.guild_id), "citations"):
            raise EconError("Only admins can change this citation.")


async def setup(bot):
    await bot.add_cog(Citations(bot))
