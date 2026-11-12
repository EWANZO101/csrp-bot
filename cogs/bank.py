import discord
from discord import app_commands
from discord.ext import commands

from core import (EconError, q, q1, cfg, money, emb, ts, parse_amount, transfer, ensure_user, get_user,
                  is_admin, org_level, log, dm, reply, GREEN, BLUE, GOLD, MEMBER)

SRC = [app_commands.Choice(name="Bank", value="bank"), app_commands.Choice(name="Wallet", value="wallet")]

KIND_TXT = {"start": "Starting balance", "pay": "Payment", "deposit": "Deposit", "withdraw": "Withdrawal",
            "org_deposit": "Org deposit", "org_withdraw": "Org withdrawal", "purchase": "Purchase", "tax": "Tax",
            "payroll": "Payroll", "citation": "Citation", "admin_print": "Admin print", "admin_remove": "Admin remove",
            "interest": "Interest", "undo": "Reversal", "business_fee": "Business fee", "org_close": "Org closed",
            "wipe": "Wipe", "org_transfer": "Org transfer"}


async def balance_embed(inter: discord.Interaction, member: discord.Member) -> discord.Embed:
    gid = inter.guild_id
    c = await cfg(gid)
    u = await get_user(gid, member.id)
    e = emb(None, None, GREEN)
    e.set_author(name=f"{member.display_name}'s balance", icon_url=member.display_avatar.url)
    e.add_field(name="👛 Wallet", value=money(c, u["wallet"]))
    e.add_field(name="🏦 Bank", value=money(c, u["bank"]))
    e.add_field(name="💰 Net worth", value=money(c, u["wallet"] + u["bank"]))
    orgs = await q("SELECT * FROM orgs WHERE guild_id=%s ORDER BY kind, name", gid)
    biz, dep = [], []
    for o in orgs:
        if await org_level(member, o, ignore_admin=True) >= MEMBER:
            (biz if o["kind"] == "business" else dep).append(f"**{o['name']}** — {money(c, o['balance'])}")
    if biz:
        e.add_field(name="🏢 Businesses", value="\n".join(biz[:10]), inline=False)
    if dep:
        e.add_field(name="🏛️ Departments", value="\n".join(dep[:10]), inline=False)
    unpaid = await q1("SELECT COUNT(*) n, COALESCE(SUM(amount+late_fee),0) s FROM citations "
                      "WHERE guild_id=%s AND user_id=%s AND status='unpaid'", gid, member.id)
    if unpaid["n"]:
        e.add_field(name="🚨 Unpaid citations", value=f"{unpaid['n']} totalling {money(c, int(unpaid['s']))}", inline=False)
    if u["frozen"]:
        e.description = "🧊 **This account is frozen.**"
    return e


async def do_move(inter, kind, amount_raw, src, dst, memo):
    gid, uid = inter.guild_id, inter.user.id
    c = await cfg(gid)
    u = await get_user(gid, uid)
    amt = parse_amount(amount_raw, u[src[0]] if src[0] in ("wallet", "bank") else None)
    await transfer(gid, [(src, dst, amt)], uid, kind, memo)
    return c, amt


async def do_pay(inter: discord.Interaction, target: discord.abc.User, amount_raw: str, memo: str | None, source="bank"):
    if target.bot or target.id == inter.user.id:
        raise EconError("You can't pay yourself or a bot.")
    gid = inter.guild_id
    await ensure_user(gid, target.id)
    c, amt = await do_move(inter, "pay", amount_raw, (source, inter.user.id), ("bank", target.id),
                           memo or f"Payment to {target}")
    e = emb("💸 Payment sent", f"{inter.user.mention} → {target.mention}\n**{money(c, amt)}**"
            + (f"\n> {memo}" if memo else ""), GREEN)
    await log(inter.client, gid, e)
    await dm(inter.client, target.id, emb("💸 You received money",
             f"**{money(c, amt)}** from {inter.user.mention} in **{inter.guild.name}**" + (f"\n> {memo}" if memo else ""),
             GREEN), gid)
    return e


# ───────────────── bank panel UI ─────────────────
class AmountModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Amount", placeholder="500, 2.5k, 1m or all", max_length=20)

    def __init__(self, title, kind, src, dst, panel):
        super().__init__(title=title)
        self.kind, self.src, self.dst, self.panel = kind, src, dst, panel

    async def on_submit(self, inter):
        c, amt = await do_move(inter, self.kind, self.amount.value, self.src, self.dst, self.kind.title())
        await inter.response.edit_message(embed=await balance_embed(inter, inter.user), view=self.panel)
        await inter.followup.send(f"✅ {self.kind.title()} of **{money(c, amt)}** complete.", ephemeral=True)

    async def on_error(self, inter, error):
        await reply(inter, embed=emb("❌", str(error) if isinstance(error, EconError) else "Something went wrong.", 0xE74C3C),
                    ephemeral=True)


class PayModal(discord.ui.Modal, title="Send money"):
    amount = discord.ui.TextInput(label="Amount", placeholder="500, 2.5k, 1m", max_length=20)
    memo = discord.ui.TextInput(label="Memo (optional)", required=False, max_length=150)

    def __init__(self, target):
        super().__init__()
        self.target = target

    async def on_submit(self, inter):
        e = await do_pay(inter, self.target, self.amount.value, self.memo.value or None)
        await inter.response.send_message(embed=e, ephemeral=True)

    async def on_error(self, inter, error):
        await reply(inter, embed=emb("❌", str(error) if isinstance(error, EconError) else "Something went wrong.", 0xE74C3C),
                    ephemeral=True)


class PayPicker(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Who do you want to pay?")
    async def pick(self, inter, select):
        await inter.response.send_modal(PayModal(select.values[0]))


class BankPanel(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=600)
        self.owner_id = owner_id

    async def interaction_check(self, inter):
        if inter.user.id != self.owner_id:
            await inter.response.send_message("Open your own panel with `/bank`.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Deposit", emoji="📥", style=discord.ButtonStyle.success)
    async def deposit(self, inter, b):
        await inter.response.send_modal(AmountModal("Deposit to bank", "deposit", ("wallet", inter.user.id),
                                                     ("bank", inter.user.id), self))

    @discord.ui.button(label="Withdraw", emoji="📤", style=discord.ButtonStyle.primary)
    async def withdraw(self, inter, b):
        await inter.response.send_modal(AmountModal("Withdraw to wallet", "withdraw", ("bank", inter.user.id),
                                                     ("wallet", inter.user.id), self))

    @discord.ui.button(label="Pay someone", emoji="💸", style=discord.ButtonStyle.secondary)
    async def pay(self, inter, b):
        await inter.response.send_message("Pick who to pay:", view=PayPicker(), ephemeral=True)

    @discord.ui.button(label="History", emoji="🧾", style=discord.ButtonStyle.secondary)
    async def hist(self, inter, b):
        await inter.response.send_message(embed=await history_embed(inter, inter.user, 1), ephemeral=True)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, inter, b):
        await inter.response.edit_message(embed=await balance_embed(inter, inter.user), view=self)


async def history_embed(inter, member, page: int) -> discord.Embed:
    gid = inter.guild_id
    c = await cfg(gid)
    per = 12
    rows = await q("SELECT * FROM ledger WHERE guild_id=%s AND ((src_type IN ('wallet','bank') AND src_id=%s) "
                   "OR (dst_type IN ('wallet','bank') AND dst_id=%s)) ORDER BY id DESC LIMIT %s OFFSET %s",
                   gid, member.id, member.id, per, (page - 1) * per)
    lines = []
    for r in rows:
        incoming = r["dst_type"] in ("wallet", "bank") and r["dst_id"] == member.id
        outgoing = r["src_type"] in ("wallet", "bank") and r["src_id"] == member.id
        if incoming and outgoing:
            sign = "↔️"
        else:
            sign = "🟢 +" if incoming else "🔴 −"
        lines.append(f"`#{r['id']}` {sign}**{money(c, r['amount'])}** · {KIND_TXT.get(r['kind'], r['kind'])}"
                     f"{' · ' + r['memo'][:40] if r['memo'] else ''} · {ts(r['ts'])}"
                     f"{' ~~reversed~~' if r['reversed_by'] else ''}")
    e = emb(f"🧾 {member.display_name} — history (page {page})", "\n".join(lines) or "No transactions.", BLUE)
    return e


class Bank(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="balance", description="Check wallet, bank, business and department balances")
    @app_commands.guild_only()
    async def balance(self, inter: discord.Interaction, user: discord.Member | None = None):
        user = user or inter.user
        c = await cfg(inter.guild_id)
        if user.id != inter.user.id and not is_admin(inter.user, c, "economy"):
            raise EconError("Only admins can view other people's balances.")
        await reply(inter, embed=await balance_embed(inter, user), ephemeral=user.id != inter.user.id)

    @app_commands.command(name="bank", description="Open your interactive bank panel")
    @app_commands.guild_only()
    async def bank(self, inter: discord.Interaction):
        await reply(inter, embed=await balance_embed(inter, inter.user), view=BankPanel(inter.user.id), ephemeral=True)

    @app_commands.command(name="deposit", description="Move money from your wallet into your bank")
    @app_commands.describe(amount="e.g. 500, 2.5k, all")
    @app_commands.guild_only()
    async def deposit(self, inter: discord.Interaction, amount: str):
        c, amt = await do_move(inter, "deposit", amount, ("wallet", inter.user.id), ("bank", inter.user.id), "Deposit")
        await reply(inter, embed=emb("📥 Deposited", f"**{money(c, amt)}** moved to your bank.", GREEN), ephemeral=True)

    @app_commands.command(name="withdraw", description="Move money from your bank into your wallet")
    @app_commands.describe(amount="e.g. 500, 2.5k, all")
    @app_commands.guild_only()
    async def withdraw(self, inter: discord.Interaction, amount: str):
        c, amt = await do_move(inter, "withdraw", amount, ("bank", inter.user.id), ("wallet", inter.user.id), "Withdrawal")
        await reply(inter, embed=emb("📤 Withdrawn", f"**{money(c, amt)}** moved to your wallet.", GREEN), ephemeral=True)

    @app_commands.command(name="pay", description="Send money to another user")
    @app_commands.describe(user="Who to pay", amount="e.g. 500, 2.5k", memo="Optional note", source="Pay from bank (default) or wallet")
    @app_commands.choices(source=SRC)
    @app_commands.guild_only()
    async def pay(self, inter: discord.Interaction, user: discord.Member, amount: str, memo: str | None = None,
                  source: str = "bank"):
        await reply(inter, embed=await do_pay(inter, user, amount, memo, source))

    @app_commands.command(name="history", description="Your recent transactions")
    @app_commands.guild_only()
    async def history(self, inter: discord.Interaction, page: app_commands.Range[int, 1, 500] = 1,
                      user: discord.Member | None = None):
        user = user or inter.user
        if user.id != inter.user.id and not is_admin(inter.user, await cfg(inter.guild_id), "economy"):
            raise EconError("Only admins can view other people's history.")
        await reply(inter, embed=await history_embed(inter, user, page), ephemeral=True)

    @app_commands.command(name="leaderboard", description="Richest citizens")
    @app_commands.guild_only()
    async def leaderboard(self, inter: discord.Interaction):
        c = await cfg(inter.guild_id)
        rows = await q("SELECT user_id, wallet+bank total FROM users WHERE guild_id=%s ORDER BY total DESC LIMIT 10",
                       inter.guild_id)
        medals = ["🥇", "🥈", "🥉"] + [f"`{i}.`" for i in range(4, 11)]
        lines = [f"{medals[i]} <@{r['user_id']}> — **{money(c, int(r['total']))}**" for i, r in enumerate(rows)]
        await reply(inter, embed=emb("🏆 Leaderboard", "\n".join(lines) or "Nobody yet.", GOLD))


async def setup(bot):
    await bot.add_cog(Bank(bot))
