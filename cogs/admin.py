import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands, tasks

import core
from core import (EconError, q, q1, ex, cfg, set_cfg, coerce_cfg, DEFAULTS, money, emb, ts, parse_amount, transfer,
                  ensure_user, get_user, find_org, require_admin, is_creator, log, dm, reply, confirm,
                  MINT, BURN, GREEN, RED, BLUE, GOLD)
from cogs.payroll import any_org_ac, run_payroll

SETTING_HELP = {
    "currency": "Currency symbol (e.g. $, R, £)",
    "start_balance": "Money new accounts start with",
    "payroll_hours": "Auto-payroll every N hours (0 = manual only)",
    "sales_tax": "% tax on shop sales → treasury",
    "treasury_dept": "Department ID that receives taxes/fees (use /department info for #ID)",
    "interest": "% daily interest on bank balances (0 = off)",
    "late_fee": "% added once when a citation becomes overdue",
    "citation_days": "Default days to pay a citation",
    "business_cost": "Cost to open a business",
    "max_businesses": "Max businesses per user",
    "dm_receipts": "DM users receipts (true/false)",
}
TARGET = [app_commands.Choice(name="Bank", value="bank"), app_commands.Choice(name="Wallet", value="wallet")]


async def all_org_ac(inter, current):
    rows = await q("SELECT * FROM orgs WHERE guild_id=%s AND name LIKE %s ORDER BY kind,name LIMIT 25",
                   inter.guild_id, f"%{current}%")
    return [app_commands.Choice(name=f"{'🏢' if r['kind'] == 'business' else '🏛️'} {r['name']}", value=f"#{r['id']}")
            for r in rows]


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ticker.start()

    def cog_unload(self):
        self.ticker.cancel()

    admin = app_commands.Group(name="admin", description="Staff tools (access is set on the web panel → Staff)", guild_only=True)

    async def _target(self, inter, user, org, where):
        if bool(user) == bool(org):
            raise EconError("Pick exactly one of `user` or `org`.")
        if user:
            await ensure_user(inter.guild_id, user.id)
            return (where, user.id), user.mention
        o = await find_org(inter.guild_id, org)
        return ("org", o["id"]), f"**{o['name']}**"

    @admin.command(name="print", description="Create money for a user or business/department")
    @app_commands.autocomplete(org=all_org_ac)
    @app_commands.choices(into=TARGET)
    async def print_(self, inter: discord.Interaction, amount: str, user: discord.Member | None = None,
                     org: str | None = None, into: str = "bank", reason: str | None = None):
        c = await require_admin(inter, "economy")
        dst, label = await self._target(inter, user, org, into)
        amt = parse_amount(amount)
        ids = await transfer(inter.guild_id, [(MINT, dst, amt)], inter.user.id, "admin_print", reason or "Admin print")
        e = emb("🖨️ Money printed", f"{inter.user.mention} printed **{money(c, amt)}** → {label}"
                + (f"\n> {reason}" if reason else ""), GOLD)
        e.set_footer(text=f"tx #{ids[0]}")
        await log(self.bot, inter.guild_id, e)
        await reply(inter, embed=e)

    @admin.command(name="remove", description="Remove money from a user or business/department")
    @app_commands.autocomplete(org=all_org_ac)
    @app_commands.choices(source=TARGET)
    async def remove(self, inter: discord.Interaction, amount: str, user: discord.Member | None = None,
                     org: str | None = None, source: str = "bank", reason: str | None = None):
        c = await require_admin(inter, "economy")
        src, label = await self._target(inter, user, org, source)
        if src[0] == "org":
            cur = (await q1("SELECT balance FROM orgs WHERE id=%s", src[1]))["balance"]
        else:
            cur = (await get_user(inter.guild_id, src[1]))[src[0]]
        amt = parse_amount(amount, cur)
        ids = await transfer(inter.guild_id, [(src, BURN, amt)], inter.user.id, "admin_remove", reason or "Admin remove")
        e = emb("🔥 Money removed", f"{inter.user.mention} removed **{money(c, amt)}** from {label}"
                + (f"\n> {reason}" if reason else ""), GOLD)
        e.set_footer(text=f"tx #{ids[0]}")
        await log(self.bot, inter.guild_id, e)
        await reply(inter, embed=e)

    @admin.command(name="wipe", description="Completely wipe all data for a user")
    async def wipe(self, inter: discord.Interaction, user: discord.Member):
        await require_admin(inter, "economy")
        if not await confirm(inter, f"Wipe **all** economy data for {user.mention}? (money, items, citations, payroll, "
                             "owned businesses). This can't be undone.", "Wipe"):
            return
        gid = inter.guild_id
        biz = await q("SELECT id FROM orgs WHERE guild_id=%s AND kind='business' AND owner_id=%s", gid, user.id)
        for b in biz:
            await ex("DELETE FROM orgs WHERE id=%s", b["id"])
        await ex("DELETE FROM users WHERE guild_id=%s AND user_id=%s", gid, user.id)
        await ex("DELETE FROM user_items WHERE guild_id=%s AND user_id=%s", gid, user.id)
        await ex("DELETE FROM citations WHERE guild_id=%s AND user_id=%s", gid, user.id)
        await ex("DELETE FROM role_grants WHERE guild_id=%s AND user_id=%s", gid, user.id)
        await ex("DELETE p FROM payroll p JOIN orgs o ON o.id=p.org_id WHERE o.guild_id=%s AND p.user_id=%s", gid, user.id)
        e = emb("🧹 Account wiped", f"{user.mention} wiped by {inter.user.mention} ({len(biz)} businesses removed)", RED)
        await log(self.bot, gid, e)
        await inter.followup.send(embed=e, ephemeral=True)

    @admin.command(name="freeze", description="Freeze/unfreeze a user's account (blocks spending)")
    async def freeze(self, inter: discord.Interaction, user: discord.Member, frozen: bool = True):
        await require_admin(inter, "economy")
        await ensure_user(inter.guild_id, user.id)
        await ex("UPDATE users SET frozen=%s WHERE guild_id=%s AND user_id=%s", frozen, inter.guild_id, user.id)
        e = emb("🧊 Account frozen" if frozen else "🔥 Account unfrozen", f"{user.mention} by {inter.user.mention}", GOLD)
        await log(self.bot, inter.guild_id, e)
        await reply(inter, embed=e, ephemeral=True)

    @admin.command(name="undo", description="Reverse a transaction by its # number")
    async def undo(self, inter: discord.Interaction, tx_id: int, force: bool = False):
        c = await require_admin(inter, "economy")
        r = await q1("SELECT * FROM ledger WHERE guild_id=%s AND id=%s", inter.guild_id, tx_id)
        if not r:
            raise EconError(f"No transaction #{tx_id}.")
        if r["reversed_by"]:
            raise EconError(f"#{tx_id} was already reversed by #{r['reversed_by']}.")
        if r["kind"] == "undo":
            raise EconError("Can't undo a reversal.")
        src = (r["dst_type"], r["dst_id"])
        dst = (r["src_type"], r["src_id"])
        if src[0] in ("wallet", "bank"):
            await ensure_user(inter.guild_id, src[1])
        if dst[0] in ("wallet", "bank"):
            await ensure_user(inter.guild_id, dst[1])

        async def extra(cur):
            await cur.execute("UPDATE ledger SET reversed_by=LAST_INSERT_ID() WHERE id=%s", (tx_id,))
        ids = await transfer(inter.guild_id, [(src, dst, r["amount"])], inter.user.id, "undo", f"Reverse #{tx_id}",
                             extra=extra, allow_negative=force)
        e = emb("↩️ Transaction reversed", f"#{tx_id} ({money(c, r['amount'])}, {r['kind']}) reversed as #{ids[0]} "
                f"by {inter.user.mention}", GOLD)
        await log(self.bot, inter.guild_id, e)
        await reply(inter, embed=e)

    @admin.command(name="audit", description="Recent transactions (optionally for one user)")
    async def audit(self, inter: discord.Interaction, user: discord.Member | None = None,
                    limit: app_commands.Range[int, 5, 40] = 20):
        c = await require_admin(inter, "economy")
        if user:
            rows = await q("SELECT * FROM ledger WHERE guild_id=%s AND ((src_type IN ('wallet','bank') AND src_id=%s) OR "
                           "(dst_type IN ('wallet','bank') AND dst_id=%s)) ORDER BY id DESC LIMIT %s",
                           inter.guild_id, user.id, user.id, limit)
        else:
            rows = await q("SELECT * FROM ledger WHERE guild_id=%s ORDER BY id DESC LIMIT %s", inter.guild_id, limit)
        orgs = {o["id"]: o["name"] for o in await q("SELECT id,name FROM orgs WHERE guild_id=%s", inter.guild_id)}

        def lab(t, i):
            if t in ("wallet", "bank"):
                return f"<@{i}>{'👛' if t == 'wallet' else ''}"
            if t == "org":
                return f"**{orgs.get(i, f'org#{i}')}**"
            return f"*{t}*"
        lines = [f"`#{r['id']}` {lab(r['src_type'], r['src_id'])} → {lab(r['dst_type'], r['dst_id'])} "
                 f"**{money(c, r['amount'])}** · {r['kind']}{' ~~rev~~' if r['reversed_by'] else ''} · {ts(r['ts'])}"
                 for r in rows]
        await reply(inter, embed=emb("📜 Audit log", "\n".join(lines)[:4000] or "Nothing.", BLUE), ephemeral=True)

    @admin.command(name="set_log", description="Channel where economy logs are sent")
    async def set_log(self, inter: discord.Interaction, channel: discord.TextChannel | None = None):
        await require_admin(inter, "setup")
        await set_cfg(inter.guild_id, log_channel=channel.id if channel else None)
        await reply(inter, embed=emb("📝 Log channel", channel.mention if channel else "Disabled", GREEN), ephemeral=True)

    @admin.command(name="staff", description="Who has access to the staff area")
    async def staff(self, inter: discord.Interaction):
        c = await cfg(inter.guild_id)
        if not core.is_admin(inter.user, c):
            raise EconError("Staff only.")
        e = emb("🛡️ Staff access", "Managed on the web panel → **Staff**.", BLUE)
        e.add_field(name="👑 Owners (full access)", value=" ".join(f"<@{u}>" for u in sorted(core.BOT_OWNERS)) or "—", inline=False)
        for rk in c.get("staff_ranks") or []:
            who = " ".join([f"<@&{r}>" for r in rk.get("roles", [])] + [f"<@{u}>" for u in rk.get("users", [])]) or "*nobody yet*"
            perms = ", ".join(core.STAFF_PERMS[p][0] for p in rk.get("perms", []) if p in core.STAFF_PERMS) or "no permissions"
            e.add_field(name=rk.get("name", "Rank"), value=f"{who}\n-# {perms}"[:1024], inline=False)
        await reply(inter, embed=e, ephemeral=True)

    @admin.command(name="settings", description="View economy settings")
    async def settings(self, inter: discord.Interaction):
        c = await require_admin(inter, "setup")
        lines = [f"`{k}` = **{c[k]}** — {v}" for k, v in SETTING_HELP.items()]
        log_ch = f"<#{c['log_channel']}>" if c["log_channel"] else "—"
        lines.append(f"`log_channel` = {log_ch}")
        await reply(inter, embed=emb("⚙️ Settings", "\n".join(lines), BLUE), ephemeral=True)

    @admin.command(name="config", description="Change an economy setting")
    @app_commands.choices(setting=[app_commands.Choice(name=f"{k} — {v}"[:100], value=k) for k, v in SETTING_HELP.items()])
    async def config(self, inter: discord.Interaction, setting: str, value: str):
        await require_admin(inter, "setup")
        v = coerce_cfg(setting, value)
        if setting == "treasury_dept" and v:
            await find_org(inter.guild_id, f"#{v}", "dept")
        await set_cfg(inter.guild_id, **{setting: v})
        e = emb("⚙️ Setting changed", f"`{setting}` → **{v}** by {inter.user.mention}", GREEN)
        await log(self.bot, inter.guild_id, e)
        await reply(inter, embed=e, ephemeral=True)

    @admin.command(name="stats", description="Economy overview")
    async def stats(self, inter: discord.Interaction):
        c = await require_admin(inter, "economy")
        gid = inter.guild_id
        u = await q1("SELECT COUNT(*) n, COALESCE(SUM(wallet),0) w, COALESCE(SUM(bank),0) b FROM users WHERE guild_id=%s", gid)
        o = await q1("SELECT COALESCE(SUM(CASE WHEN kind='business' THEN balance END),0) biz, "
                     "COALESCE(SUM(CASE WHEN kind='dept' THEN balance END),0) dep, COUNT(*) n FROM orgs WHERE guild_id=%s", gid)
        cit = await q1("SELECT COUNT(*) n, COALESCE(SUM(amount+late_fee),0) s FROM citations WHERE guild_id=%s AND status='unpaid'", gid)
        day = await q1("SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM ledger WHERE guild_id=%s AND ts > UTC_TIMESTAMP() - INTERVAL 1 DAY", gid)
        total = int(u["w"]) + int(u["b"]) + int(o["biz"]) + int(o["dep"])
        e = emb("📊 Economy stats", None, BLUE)
        e.add_field(name="Accounts", value=str(u["n"]))
        e.add_field(name="Money supply", value=money(c, total))
        e.add_field(name="Wallets / Banks", value=f"{money(c, int(u['w']))} / {money(c, int(u['b']))}")
        e.add_field(name="Businesses", value=money(c, int(o["biz"])))
        e.add_field(name="Departments", value=money(c, int(o["dep"])))
        e.add_field(name="Unpaid citations", value=f"{cit['n']} · {money(c, int(cit['s']))}")
        e.add_field(name="Last 24h", value=f"{day['n']} tx · {money(c, int(day['s']))} moved")
        await reply(inter, embed=e, ephemeral=True)

    # ───────────── background jobs ─────────────
    @tasks.loop(minutes=5)
    async def ticker(self):
        for g in self.bot.guilds:
            try:
                await self.tick_guild(g)
            except Exception as err:  # keep the loop alive
                print(f"[ticker] {g.id}: {err!r}")

    @ticker.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    async def tick_guild(self, g: discord.Guild):
        gid = g.id
        c = await cfg(gid)
        # auto payroll
        if c["payroll_hours"]:
            orgs = await q("SELECT DISTINCT o.* FROM orgs o JOIN payroll p ON p.org_id=o.id WHERE o.guild_id=%s AND "
                           f"(p.last_paid IS NULL OR p.last_paid <= UTC_TIMESTAMP() - INTERVAL {int(c['payroll_hours'])} HOUR)", gid)
            for o in orgs:
                await run_payroll(self.bot, o, self.bot.user.id, only_due=True)
        # citation late fees (once)
        if c["late_fee"]:
            due = await q("SELECT * FROM citations WHERE guild_id=%s AND status='unpaid' AND late_fee=0 AND due_at < UTC_TIMESTAMP()", gid)
            for r in due:
                fee = max(1, r["amount"] * int(c["late_fee"]) // 100)
                await ex("UPDATE citations SET late_fee=%s WHERE id=%s", fee, r["id"])
                await dm(self.bot, r["user_id"], emb("⏰ Citation overdue", f"Citation #{r['id']} is overdue — a late fee of "
                         f"**{money(c, fee)}** was added. Pay with `/citation pay {r['id']}`.", RED), gid)
                await log(self.bot, gid, emb("⏰ Citation overdue", f"#{r['id']} <@{r['user_id']}> +{money(c, fee)} late fee", GOLD))
        # expire timed roles
        for r in await q("SELECT * FROM role_grants WHERE guild_id=%s AND expires_at <= UTC_TIMESTAMP()", gid):
            m = g.get_member(r["user_id"])
            if m is None:
                try:
                    m = await g.fetch_member(r["user_id"])
                except discord.HTTPException:
                    m = None
            role = g.get_role(r["role_id"])
            if m and role:
                try:
                    await m.remove_roles(role, reason=f"{r['item'] or 'Item'} expired")
                except discord.HTTPException:
                    pass
                await dm(self.bot, m.id, emb("⌛ Item expired", f"Your **{r['item'] or role.name}** in {g.name} has expired.", GOLD), gid)
            await ex("DELETE FROM role_grants WHERE id=%s", r["id"])
        # daily interest
        if c["interest"]:
            last = c.get("last_interest", 0)
            now = int(dt.datetime.utcnow().timestamp())
            if now - last >= 86400:
                await set_cfg(gid, last_interest=now)
                rows = await q("SELECT user_id, bank FROM users WHERE guild_id=%s AND bank>0 AND frozen=0", gid)
                legs = [(MINT, ("bank", r["user_id"]), int(r["bank"] * float(c["interest"]) / 100)) for r in rows]
                legs = [l for l in legs if l[2] > 0]
                for i in range(0, len(legs), 200):
                    await transfer(gid, legs[i:i + 200], self.bot.user.id, "interest", f"Daily interest {c['interest']}%")
                if legs:
                    await log(self.bot, gid, emb("📈 Interest paid", f"{len(legs)} accounts · "
                              f"{money(c, sum(l[2] for l in legs))} total", GREEN))


HELP = {
    "💰 Money": "`/bank` panel with buttons · `/balance` · `/pay` · `/deposit` · `/withdraw` · `/history` · `/leaderboard`",
    "🛍️ Shops & items": "`/shop browse` · `/shop buy` · `/shop all` · `/inventory` · `/give` · `/use`",
    "🏢 Businesses": "`/business create|delete|list|info|inventory|edit|deposit|withdraw|send|transfer_ownership`\n"
                    "`/business item add|edit|remove|give` · `/business role add|remove`",
    "🏛️ Departments": "`/department create|delete|list|info|inventory|edit|deposit|withdraw|send`\n"
                      "`/department item add|edit|remove|give` · `/department role add|remove`",
    "💼 Payroll": "`/payroll add|remove|list|run` — auto-paid on a schedule",
    "🚨 Citations": "`/citation issue|pay|mine|list|info|edit|void` — character name + reason required; late fees applied automatically",
    "🎫 Tickets": "Open one from the ticket panel · `/ticket close|add|remove|rename|panel`",
    "📚 Documents": "`/docs` — SOPs, penal codes and links (also on the web panel)",
    "📝 Applications": "`/apply` — see requirements and apply on the web panel",
    "⚙️ Admin": "`/admin print|remove|wipe|freeze|undo|audit|stats|settings|config|set_log|staff`",
}


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="How to use the economy bot")
    async def help(self, inter: discord.Interaction):
        e = emb("📖 Economy bot", "Amounts accept `500`, `2.5k`, `1m` or `all`. Your account is created automatically.", BLUE)
        for k, v in HELP.items():
            e.add_field(name=k, value=v, inline=False)
        url = self.bot.state.settings.get("public_url") or self.bot.state.settings.get("seen_url")
        if url:
            e.add_field(name="🌐 Web panel", value=url, inline=False)
        await reply(inter, embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
    await bot.add_cog(Help(bot))
