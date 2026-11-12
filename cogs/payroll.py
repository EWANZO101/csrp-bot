import discord
from discord import app_commands
from discord.ext import commands

from core import (EconError, q, q1, ex, cfg, money, emb, ts, parse_amount, transfer, ensure_user, find_org,
                  require_level, org_level, log, dm, reply, GREEN, BLUE, GOLD, RED, HEAD, MEMBER)


async def any_org_ac(inter: discord.Interaction, current: str):
    rows = await q("SELECT * FROM orgs WHERE guild_id=%s AND name LIKE %s ORDER BY kind, name LIMIT 50",
                   inter.guild_id, f"%{current}%")
    out = []
    for r in rows:
        if await org_level(inter.user, r) >= MEMBER:
            icon = "🏢" if r["kind"] == "business" else "🏛️"
            out.append(app_commands.Choice(name=f"{icon} {r['name']}", value=f"#{r['id']}"))
    return out[:25]


async def run_payroll(bot, org, actor_id, only_due=False) -> tuple[int, int, list[str]]:
    """Pay everyone on an org's payroll. Returns (paid_count, total, failures)."""
    gid = org["guild_id"]
    c = await cfg(gid)
    cond = ""
    if only_due:
        cond = f" AND (last_paid IS NULL OR last_paid <= UTC_TIMESTAMP() - INTERVAL {int(c['payroll_hours'])} HOUR)"
    rows = await q(f"SELECT * FROM payroll WHERE org_id=%s{cond} ORDER BY id", org["id"])
    paid, total, fails = 0, 0, []
    for r in rows:
        await ensure_user(gid, r["user_id"])
        try:
            await transfer(gid, [(("org", org["id"]), ("bank", r["user_id"]), r["amount"])], actor_id, "payroll",
                           f"Payroll — {org['name']}")
        except EconError:
            fails.append(f"<@{r['user_id']}> ({money(c, r['amount'])})")
            continue
        await ex("UPDATE payroll SET last_paid=UTC_TIMESTAMP() WHERE id=%s", r["id"])
        paid += 1
        total += r["amount"]
        await dm(bot, r["user_id"], emb("💼 Payday", f"You were paid **{money(c, r['amount'])}** by **{org['name']}**.",
                                        GREEN), gid)
    if rows:
        e = emb("💼 Payroll run", f"**{org['name']}** paid {paid}/{len(rows)} people · **{money(c, total)}**", GREEN)
        if fails:
            e.add_field(name="⚠️ Not paid (insufficient funds)", value="\n".join(fails)[:1024], inline=False)
            e.color = GOLD
        await log(bot, gid, e)
    return paid, total, fails


class Payroll(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    payroll = app_commands.Group(name="payroll", description="Business & department payroll", guild_only=True)

    @payroll.command(name="add", description="Add/update someone on a payroll")
    @app_commands.rename(org="employer")
    @app_commands.autocomplete(org=any_org_ac)
    @app_commands.describe(amount="Pay per cycle")
    async def add(self, inter: discord.Interaction, org: str, user: discord.Member, amount: str):
        o = await find_org(inter.guild_id, org)
        await require_level(inter.user, o, HEAD, "manage payroll")
        amt = parse_amount(amount)
        c = await cfg(inter.guild_id)
        await ensure_user(inter.guild_id, user.id)
        await ex("INSERT INTO payroll (org_id,user_id,amount,added_by,last_paid) VALUES (%s,%s,%s,%s,UTC_TIMESTAMP()) "
                 "ON DUPLICATE KEY UPDATE amount=VALUES(amount)", o["id"], user.id, amt, inter.user.id)
        every = f"every {c['payroll_hours']}h automatically" if c["payroll_hours"] else "when someone runs `/payroll run`"
        e = emb("💼 Payroll updated", f"{user.mention} earns **{money(c, amt)}** from **{o['name']}** {every}.", GREEN)
        await log(inter.client, inter.guild_id, e)
        await reply(inter, embed=e)

    @payroll.command(name="remove", description="Remove someone from a payroll")
    @app_commands.rename(org="employer")
    @app_commands.autocomplete(org=any_org_ac)
    async def remove(self, inter: discord.Interaction, org: str, user: discord.Member):
        o = await find_org(inter.guild_id, org)
        await require_level(inter.user, o, HEAD, "manage payroll")
        n, _ = await ex("DELETE FROM payroll WHERE org_id=%s AND user_id=%s", o["id"], user.id)
        if not n:
            raise EconError(f"{user.mention} isn't on **{o['name']}**'s payroll.")
        e = emb("💼 Removed from payroll", f"{user.mention} — **{o['name']}**", GOLD)
        await log(inter.client, inter.guild_id, e)
        await reply(inter, embed=e)

    @payroll.command(name="list", description="Everyone on a payroll")
    @app_commands.rename(org="employer")
    @app_commands.autocomplete(org=any_org_ac)
    async def list_(self, inter: discord.Interaction, org: str):
        o = await find_org(inter.guild_id, org)
        await require_level(inter.user, o, MEMBER, "view payroll")
        c = await cfg(inter.guild_id)
        rows = await q("SELECT * FROM payroll WHERE org_id=%s ORDER BY amount DESC", o["id"])
        total = sum(r["amount"] for r in rows)
        lines = [f"<@{r['user_id']}> — **{money(c, r['amount'])}** · last paid {ts(r['last_paid'])}" for r in rows]
        e = emb(f"💼 {o['name']} payroll", "\n".join(lines)[:3800] or "Nobody.", BLUE)
        e.add_field(name="Per cycle", value=money(c, total))
        e.add_field(name="Balance", value=money(c, o["balance"]))
        if c["payroll_hours"]:
            e.add_field(name="Auto-pay", value=f"every {c['payroll_hours']}h")
        if total > o["balance"]:
            e.color = GOLD
            e.set_footer(text="⚠️ Balance won't cover the next payroll")
        await reply(inter, embed=e)

    @payroll.command(name="run", description="Pay everyone on a payroll right now")
    @app_commands.rename(org="employer")
    @app_commands.autocomplete(org=any_org_ac)
    async def run(self, inter: discord.Interaction, org: str):
        o = await find_org(inter.guild_id, org)
        await require_level(inter.user, o, HEAD, "run payroll")
        await inter.response.defer()
        c = await cfg(inter.guild_id)
        paid, total, fails = await run_payroll(self.bot, o, inter.user.id)
        e = emb("💼 Payroll paid", f"Paid **{paid}** people **{money(c, total)}** from **{o['name']}**.",
                GOLD if fails else GREEN)
        if fails:
            e.add_field(name="Not paid", value="\n".join(fails)[:1024])
        await inter.followup.send(embed=e)


async def setup(bot):
    await bot.add_cog(Payroll(bot))
