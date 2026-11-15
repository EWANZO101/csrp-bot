"""Utility bills — companies, plans, payment methods, autopay, invoices, late fees, shut-offs.

Players (in #utilities, or /utilities):
  💳 Payment menu   — autopay from bank / wallet / manual, backup method, reminder DMs. Required before signing up.
  ➕ Sign up         — pick a company and a plan (setup fee charged).
  🔌 My services     — status, next bill, change plan, cancel.
  📄 My bills        — pay one or all; 📜 history.
Automatic (every minute): bills each billing cycle (± usage swing), autopay (with optional discount, backup
method fallback, retried until paid), late fee after the due date, service shut off after N overdue days,
reconnect fee to restore.
Admins (#utilities-admin): companies, plans, rules, overview, unpaid bills, waive, shut off / restore, bill now,
starter pack, and lock / open the player channel. Both channels start LOCKED (staff only).
"""
import datetime as dt
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, money, emb, ts, transfer, ensure_user, get_user, log, dm, reply, is_admin, \
    parse_amount, BURN, GREEN, RED, BLUE, GOLD

logger = logging.getLogger("economy.utilities")
KINDS = {"electric": "⚡", "water": "💧", "gas": "🔥", "internet": "🌐", "phone": "📱", "trash": "🗑️", "insurance": "🛡️",
         "cable": "📺", "security": "🚨", "rent": "🏠", "other": "🧾"}
METHODS = {"bank": "🏦 Bank autopay", "wallet": "💵 Wallet autopay", "manual": "✋ Pay manually"}
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS util_config (
      guild_id BIGINT UNSIGNED PRIMARY KEY,
      open TINYINT NOT NULL DEFAULT 0,
      late_fee_pct INT NOT NULL DEFAULT 10,
      grace_days INT NOT NULL DEFAULT 3,
      shutoff_days INT NOT NULL DEFAULT 7,
      reconnect_fee BIGINT NOT NULL DEFAULT 250,
      autopay_discount_pct INT NOT NULL DEFAULT 5
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS util_providers (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      name VARCHAR(64) NOT NULL,
      kind VARCHAR(16) NOT NULL DEFAULT 'other',
      org_id INT NULL,
      description VARCHAR(300) NULL,
      enabled TINYINT NOT NULL DEFAULT 1,
      UNIQUE KEY uq_up (guild_id, name)
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS util_plans (
      id INT AUTO_INCREMENT PRIMARY KEY,
      provider_id INT NOT NULL,
      name VARCHAR(64) NOT NULL,
      price BIGINT NOT NULL,
      cycle_days INT NOT NULL DEFAULT 7,
      variance_pct INT NOT NULL DEFAULT 0,
      setup_fee BIGINT NOT NULL DEFAULT 0,
      enabled TINYINT NOT NULL DEFAULT 1,
      UNIQUE KEY uq_pl (provider_id, name),
      FOREIGN KEY (provider_id) REFERENCES util_providers(id) ON DELETE CASCADE
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS util_payment (
      guild_id BIGINT UNSIGNED NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      method VARCHAR(8) NOT NULL DEFAULT 'manual',
      backup VARCHAR(8) NOT NULL DEFAULT 'none',
      reminders TINYINT NOT NULL DEFAULT 1,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (guild_id, user_id)
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS util_accounts (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      provider_id INT NOT NULL,
      plan_id INT NOT NULL,
      status VARCHAR(10) NOT NULL DEFAULT 'active',
      started_at DATETIME NOT NULL,
      next_bill_at DATETIME NOT NULL,
      INDEX ix_ua (guild_id, user_id, status),
      FOREIGN KEY (plan_id) REFERENCES util_plans(id) ON DELETE CASCADE
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS util_bills (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      account_id INT NULL,
      provider_id INT NULL,
      label VARCHAR(160) NOT NULL,
      amount BIGINT NOT NULL,
      late_fee BIGINT NOT NULL DEFAULT 0,
      status VARCHAR(8) NOT NULL DEFAULT 'unpaid',
      issued_at DATETIME NOT NULL,
      due_at DATETIME NOT NULL,
      paid_at DATETIME NULL,
      paid_via VARCHAR(16) NULL,
      reminded TINYINT NOT NULL DEFAULT 0,
      INDEX ix_ub (guild_id, user_id, status)
    ) ENGINE=InnoDB""",
]
STARTER = [  # (name, kind, description, [(plan, price, cycle_days, variance, setup)])
    ("Los Santos Power & Light", "electric", "City electricity", [("Basic", 150, 7, 25, 50), ("Plus", 300, 7, 20, 50)]),
    ("San Andreas Water", "water", "Water & sewer", [("Standard", 80, 7, 30, 25)]),
    ("Blaine County Gas", "gas", "Natural gas for heating & cooking", [("Standard", 90, 7, 35, 25)]),
    ("Fiber Fast Internet", "internet", "Home internet", [("100 Mbps", 120, 7, 0, 100), ("1 Gig", 220, 7, 0, 100)]),
    ("LS Mobile", "phone", "Phone plans", [("Talk & Text", 60, 7, 0, 0), ("Unlimited", 140, 7, 0, 0)]),
    ("City Waste Services", "trash", "Trash & recycling pickup", [("Weekly pickup", 40, 7, 0, 0)]),
    ("Pacific Shield Insurance", "insurance", "Home & auto insurance", [("Basic cover", 200, 7, 0, 0), ("Full cover", 450, 7, 0, 0)]),
]


def now():
    return dt.datetime.utcnow().replace(microsecond=0)


def unix(d):
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


async def ucfg(gid):
    r = await q1("SELECT * FROM util_config WHERE guild_id=%s", gid)
    if not r:
        await ex("INSERT IGNORE INTO util_config (guild_id) VALUES (%s)", gid)
        r = await q1("SELECT * FROM util_config WHERE guild_id=%s", gid)
    return r


async def pay_settings(gid, uid):
    return await q1("SELECT * FROM util_payment WHERE guild_id=%s AND user_id=%s", gid, uid)


async def check_open(inter):
    c, u = await cfg(inter.guild_id), await ucfg(inter.guild_id)
    if not u["open"] and not is_admin(inter.user, c):
        raise EconError("Utilities aren't open yet — check back soon.")


async def collect(bot, gid, bill, source, actor) -> bool:
    """Pay a bill from wallet/bank. Returns True if paid."""
    if bill["status"] != "unpaid":
        return False
    total = bill["amount"] + bill["late_fee"]
    acc = await get_user(gid, bill["user_id"])
    if acc[source] < total:
        return False
    prov = await q1("SELECT org_id FROM util_providers WHERE id=%s", bill["provider_id"]) if bill["provider_id"] else None
    dst = ("org", prov["org_id"]) if prov and prov["org_id"] else BURN

    async def extra(cur):
        await cur.execute("UPDATE util_bills SET status='paid', paid_at=UTC_TIMESTAMP(), paid_via=%s WHERE id=%s AND status='unpaid'",
                          (source, bill["id"]))
        if cur.rowcount != 1:
            raise EconError("That bill was just paid.")
    await transfer(gid, [((source, bill["user_id"]), dst, total)], actor, "utility", f"Utility bill #{bill['id']}: {bill['label']}"[:255],
                   extra=extra)
    bill["status"] = "paid"
    return True


async def try_autopay(bot, gid, bill) -> str | None:
    ps = await pay_settings(gid, bill["user_id"])
    if not ps or ps["method"] == "manual":
        return None
    for src in [ps["method"]] + ([ps["backup"]] if ps["backup"] in ("bank", "wallet") and ps["backup"] != ps["method"] else []):
        try:
            if await collect(bot, gid, bill, src, bot.user.id):
                return src
        except EconError:
            return None
    return None


# ═════════════════════ player UI (ephemeral flows) ═════════════════════
class PaymentMenu(discord.ui.View):
    def __init__(self, cog, ps, discount):
        super().__init__(timeout=300)
        self.cog = cog
        self.method = ps["method"] if ps else "bank"
        self.backup = ps["backup"] if ps else "none"
        self.reminders = bool(ps["reminders"]) if ps else True
        m = discord.ui.Select(placeholder="Main payment method", row=0, options=[
            discord.SelectOption(label="Bank autopay", value="bank", emoji="🏦", default=self.method == "bank",
                                 description=f"Bills paid automatically from your bank" + (f" · {discount}% off" if discount else "")),
            discord.SelectOption(label="Wallet autopay", value="wallet", emoji="💵", default=self.method == "wallet",
                                 description="Bills paid automatically from your wallet" + (f" · {discount}% off" if discount else "")),
            discord.SelectOption(label="Pay manually", value="manual", emoji="✋", default=self.method == "manual",
                                 description="You pay each bill yourself")])
        b = discord.ui.Select(placeholder="Backup if the main method can't cover it", row=1, options=[
            discord.SelectOption(label="No backup", value="none", default=self.backup == "none"),
            discord.SelectOption(label="Bank", value="bank", emoji="🏦", default=self.backup == "bank"),
            discord.SelectOption(label="Wallet", value="wallet", emoji="💵", default=self.backup == "wallet")])
        r = discord.ui.Select(placeholder="Reminder DMs", row=2, options=[
            discord.SelectOption(label="Reminders on — DM me bills, receipts & due dates", value="1", emoji="🔔", default=self.reminders),
            discord.SelectOption(label="Reminders off", value="0", emoji="🔕", default=not self.reminders)])
        m.callback, b.callback, r.callback = self.set_m, self.set_b, self.set_r
        for x in (m, b, r):
            self.add_item(x)

    async def set_m(self, inter):
        self.method = inter.data["values"][0]
        await inter.response.defer()

    async def set_b(self, inter):
        self.backup = inter.data["values"][0]
        await inter.response.defer()

    async def set_r(self, inter):
        self.reminders = inter.data["values"][0] == "1"
        await inter.response.defer()

    @discord.ui.button(label="Save payment settings", emoji="💾", style=discord.ButtonStyle.success, row=3)
    async def save(self, inter, _):
        gid = inter.guild_id
        await ensure_user(gid, inter.user.id)
        await ex("INSERT INTO util_payment (guild_id,user_id,method,backup,reminders,updated_at) VALUES (%s,%s,%s,%s,%s,UTC_TIMESTAMP()) "
                 "ON DUPLICATE KEY UPDATE method=VALUES(method), backup=VALUES(backup), reminders=VALUES(reminders), updated_at=UTC_TIMESTAMP()",
                 gid, inter.user.id, self.method, self.backup, int(self.reminders))
        paid = 0
        if self.method != "manual":   # settle anything outstanding right away
            for b in await q("SELECT * FROM util_bills WHERE guild_id=%s AND user_id=%s AND status='unpaid' ORDER BY due_at", gid, inter.user.id):
                if await try_autopay(self.cog.bot, gid, b):
                    paid += 1
        await inter.response.edit_message(embed=emb("✅ Payment settings saved",
            f"**Method:** {METHODS[self.method]}\n**Backup:** {METHODS.get(self.backup, 'none')}\n"
            f"**Reminders:** {'on' if self.reminders else 'off'}" + (f"\n\nPaid {paid} outstanding bill(s) automatically." if paid else ""),
            GREEN), view=None)


class SignupFlow(discord.ui.View):
    def __init__(self, cog, providers):
        super().__init__(timeout=300)
        self.cog, self.provider, self.plan = cog, None, None
        s = discord.ui.Select(placeholder="1 · Choose a company", options=[
            discord.SelectOption(label=p["name"][:100], value=str(p["id"]), emoji=KINDS.get(p["kind"], "🧾"),
                                 description=(p["description"] or p["kind"].title())[:100]) for p in providers[:25]])
        s.callback = self.pick_provider
        self.add_item(s)

    async def pick_provider(self, inter):
        self.provider = int(inter.data["values"][0])
        c = await cfg(inter.guild_id)
        plans = await q("SELECT * FROM util_plans WHERE provider_id=%s AND enabled=1 ORDER BY price", self.provider)
        if not plans:
            return await inter.response.send_message(embed=emb("❌ That company has no plans yet.", None, RED), ephemeral=True)
        for item in list(self.children):
            if isinstance(item, discord.ui.Select) and item.placeholder.startswith("2"):
                self.remove_item(item)
        s = discord.ui.Select(placeholder="2 · Choose a plan", options=[
            discord.SelectOption(label=f"{p['name']} — {money(c, p['price'])} every {p['cycle_days']}d"[:100], value=str(p["id"]),
                                 description=(f"usage ±{p['variance_pct']}% · " if p["variance_pct"] else "fixed price · ")
                                 + (f"setup {money(c, p['setup_fee'])}" if p["setup_fee"] else "no setup fee")) for p in plans[:25]])
        s.callback = self.pick_plan
        self.add_item(s)
        await inter.response.edit_message(view=self)

    async def pick_plan(self, inter):
        self.plan = int(inter.data["values"][0])
        await inter.response.defer()

    @discord.ui.button(label="Sign up", emoji="✅", style=discord.ButtonStyle.success, row=4)
    async def confirm(self, inter, _):
        try:
            await self.cog.signup(inter, self.provider, self.plan)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class ServicesView(discord.ui.View):
    def __init__(self, cog, accounts):
        super().__init__(timeout=300)
        self.cog = cog
        if accounts:
            s = discord.ui.Select(placeholder="Pick a service to change or cancel…", options=[
                discord.SelectOption(label=f"{a['pname']} — {a['plan']}"[:100], value=str(a["id"]), emoji=KINDS.get(a["kind"], "🧾"),
                                     description=a["status"]) for a in accounts[:25]])
            s.callback = self.pick
            self.add_item(s)

    async def pick(self, inter):
        aid = int(inter.data["values"][0])
        a = await q1("SELECT a.*, p.name plan, pr.name pname FROM util_accounts a JOIN util_plans p ON p.id=a.plan_id "
                     "JOIN util_providers pr ON pr.id=a.provider_id WHERE a.id=%s AND a.user_id=%s", aid, inter.user.id)
        if not a:
            return await inter.response.send_message("Not found.", ephemeral=True)
        await inter.response.send_message(embed=emb(f"{a['pname']} — {a['plan']}", f"Status: **{a['status']}**", BLUE),
                                          view=ServiceActions(self.cog, a), ephemeral=True)


class ServiceActions(discord.ui.View):
    def __init__(self, cog, a):
        super().__init__(timeout=300)
        self.cog, self.a = cog, a

    @discord.ui.button(label="Change plan", emoji="🔁", style=discord.ButtonStyle.primary)
    async def change(self, inter, _):
        c = await cfg(inter.guild_id)
        plans = await q("SELECT * FROM util_plans WHERE provider_id=%s AND enabled=1 AND id<>%s ORDER BY price",
                        self.a["provider_id"], self.a["plan_id"])
        if not plans:
            return await inter.response.send_message(embed=emb("No other plans for this company.", None, GOLD), ephemeral=True)
        v = discord.ui.View(timeout=300)
        s = discord.ui.Select(placeholder="New plan (takes effect from the next bill)", options=[
            discord.SelectOption(label=f"{p['name']} — {money(c, p['price'])} / {p['cycle_days']}d"[:100], value=str(p["id"])) for p in plans[:25]])

        async def done(i2):
            await ex("UPDATE util_accounts SET plan_id=%s WHERE id=%s AND user_id=%s", int(i2.data["values"][0]), self.a["id"], i2.user.id)
            await i2.response.edit_message(embed=emb("🔁 Plan changed — applies from your next bill.", None, GREEN), view=None)
        s.callback = done
        v.add_item(s)
        await inter.response.send_message(view=v, ephemeral=True)

    @discord.ui.button(label="Reconnect", emoji="🔌", style=discord.ButtonStyle.success)
    async def reconnect(self, inter, _):
        try:
            msg = await self.cog.reconnect(inter.guild_id, inter.user.id, self.a["id"])
            await inter.response.send_message(embed=emb("🔌 " + msg, None, GREEN), ephemeral=True)
        except EconError as err:
            await inter.response.send_message(embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)

    @discord.ui.button(label="Cancel service", emoji="🛑", style=discord.ButtonStyle.danger)
    async def cancel(self, inter, _):
        await ex("UPDATE util_accounts SET status='cancelled' WHERE id=%s AND user_id=%s", self.a["id"], inter.user.id)
        await inter.response.edit_message(embed=emb(f"🛑 {self.a['pname']} cancelled",
                                                    "No new bills will be sent. Any unpaid bills still need paying.", GOLD), view=None)


class BillsView(discord.ui.View):
    def __init__(self, cog, bills, c):
        super().__init__(timeout=300)
        self.cog = cog
        if bills:
            s = discord.ui.Select(placeholder="Pay one bill…", options=[
                discord.SelectOption(label=f"#{b['id']} {b['label']}"[:100], value=str(b["id"]),
                                     description=f"{money(c, b['amount'] + b['late_fee'])} · due {b['due_at']:%b %d}"[:100]) for b in bills[:25]])
            s.callback = self.pay_one
            self.add_item(s)

    async def _pay(self, inter, bills):
        gid, paid, total, fails = inter.guild_id, 0, 0, 0
        c = await cfg(gid)
        for b in bills:
            src = None
            for s in ("bank", "wallet"):
                if await collect(self.cog.bot, gid, b, s, inter.user.id):
                    src = s
                    break
            if src:
                paid, total = paid + 1, total + b["amount"] + b["late_fee"]
            else:
                fails += 1
        await self.cog.after_payment(gid, inter.user.id)
        e = emb("💳 Payment", f"Paid **{paid}** bill(s) · {money(c, total)}" + (f"\n⚠️ {fails} couldn't be paid — not enough money." if fails else ""),
                GREEN if not fails else GOLD)
        await inter.response.send_message(embed=e, ephemeral=True)

    async def pay_one(self, inter):
        b = await q1("SELECT * FROM util_bills WHERE id=%s AND user_id=%s AND status='unpaid'", int(inter.data["values"][0]), inter.user.id)
        await self._pay(inter, [b] if b else [])

    @discord.ui.button(label="Pay all", emoji="💵", style=discord.ButtonStyle.success, row=1)
    async def pay_all(self, inter, _):
        bills = await q("SELECT * FROM util_bills WHERE guild_id=%s AND user_id=%s AND status='unpaid' ORDER BY due_at", inter.guild_id, inter.user.id)
        await self._pay(inter, bills)

    @discord.ui.button(label="History", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def history(self, inter, _):
        c = await cfg(inter.guild_id)
        rows = await q("SELECT * FROM util_bills WHERE guild_id=%s AND user_id=%s ORDER BY id DESC LIMIT 20", inter.guild_id, inter.user.id)
        icon = {"paid": "✅", "unpaid": "⏳", "void": "🚫"}
        lines = [f"{icon.get(b['status'], '•')} `#{b['id']}` {b['label']} — {money(c, b['amount'] + b['late_fee'])}"
                 + (f" · paid via {b['paid_via']}" if b["paid_via"] else f" · due <t:{unix(b['due_at'])}:R>") for b in rows]
        await inter.response.send_message(embed=emb("📜 Bill history", "\n".join(lines)[:4000] or "No bills yet.", BLUE), ephemeral=True)


# ═════════════════════ channel cards ═════════════════════
def S(text, button):
    return discord.ui.Section(discord.ui.TextDisplay(text), accessory=button)


class PlayerCard(discord.ui.LayoutView):
    def __init__(self, cog, header=None):
        super().__init__(timeout=None)
        self.cog = cog
        btn = {k: discord.ui.Button(label=l, emoji=e, style=st, custom_id=f"util:{k}") for k, l, e, st in [
            ("pay", "Payment menu", "💳", discord.ButtonStyle.primary), ("signup", "Sign up", "➕", discord.ButtonStyle.success),
            ("services", "My services", "🔌", discord.ButtonStyle.secondary), ("bills", "My bills", "📄", discord.ButtonStyle.danger)]}
        for b in btn.values():
            b.callback = self.on_button
        self.add_item(discord.ui.Container(
            *(header or [discord.ui.TextDisplay("## 💡 Utilities")]), discord.ui.Separator(),
            S("**💳 Payment menu**\nSet up how your bills get paid — bank or wallet autopay (with a discount), a backup method, "
              "or manual. **You need this before signing up.**", btn["pay"]),
            discord.ui.Separator(visible=False),
            S("**➕ Sign up for a service**\nPick a company and a plan. Some plans have a one-off setup fee.", btn["signup"]),
            discord.ui.Separator(visible=False),
            S("**🔌 My services**\nSee what you're signed up for, your next bill date, change plan, reconnect or cancel.", btn["services"]),
            discord.ui.Separator(visible=False),
            S("**📄 My bills**\nWhat you owe, due dates and late fees. Pay one or all, and see your payment history.", btn["bills"]),
            accent_colour=discord.Colour.yellow()))

    async def on_button(self, inter):
        try:
            await check_open(inter)
            await self.cog.player_action(inter, inter.data["custom_id"].split(":")[1])
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


def tf(label, desc=None, default=None, required=True, ph=None):
    return discord.ui.Label(text=label, description=desc, component=discord.ui.TextInput(
        default=None if default in (None, "") else str(default), required=required, placeholder=ph, max_length=300))


def val(label):
    return (label.component.value or "").strip()


def num(label, name, allow_float=False):
    v = val(label).replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(v) if allow_float else int(v)
    except ValueError:
        raise EconError(f"{name} must be a number.")


class AdminModal(discord.ui.Modal):
    perm = "setup"

    def __init__(self, cog, **kw):
        super().__init__(timeout=600)
        self.cog = cog
        self.kw = kw
        self.build(**kw)

    def build(self, **kw):
        pass

    async def on_submit(self, inter):
        try:
            if not is_admin(inter.user, await cfg(inter.guild_id), self.perm):
                raise EconError(f"You need the {core.STAFF_PERMS[self.perm][0]} staff permission.")
            e = await self.run(inter)
            await log(inter.client, inter.guild_id, e)
            await reply(inter, embed=e, ephemeral=True)
            await self.cog.sync()
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class ProviderModal(AdminModal, title="🏢 Add / edit utility company"):
    def build(self):
        self.name = tf("Company name", "Existing name = edit it", ph="e.g. Los Santos Power & Light")
        self.kind = tf("Type", ", ".join(KINDS), "electric")
        self.org = tf("Business / department that gets paid", "Part of the name · empty = money destroyed", required=False)
        self.desc = tf("Description", required=False, ph="optional")
        self.enabled = tf("Enabled? (yes / no)", None, "yes")
        for x in (self.name, self.kind, self.org, self.desc, self.enabled):
            self.add_item(x)

    async def run(self, inter):
        gid = inter.guild_id
        kind = val(self.kind).lower()
        if kind not in KINDS:
            raise EconError(f"Type must be one of: {', '.join(KINDS)}")
        org_id = None
        if val(self.org):
            from cogs.staffpanel import match_org
            org_id = (await match_org(gid, val(self.org)))["id"]
        en = 0 if val(self.enabled).lower() in ("no", "n", "false", "0", "off") else 1
        await ex("INSERT INTO util_providers (guild_id,name,kind,org_id,description,enabled) VALUES (%s,%s,%s,%s,%s,%s) "
                 "ON DUPLICATE KEY UPDATE kind=VALUES(kind), org_id=VALUES(org_id), description=VALUES(description), enabled=VALUES(enabled)",
                 gid, val(self.name)[:64], kind, org_id, val(self.desc) or None, en)
        return emb(f"{KINDS[kind]} Company saved: {val(self.name)}", f"by {inter.user.mention}" + ("" if en else " · disabled"), GREEN)


class PlanModal(AdminModal, title="📦 Add / edit plan"):
    def build(self):
        self.provider = tf("Company", "Part of the name is fine", ph="e.g. power")
        self.name = tf("Plan name", "Existing plan name = edit it", ph="e.g. Basic")
        self.price = tf("Price per bill", ph="e.g. 150")
        self.cycle = tf("Bill every N days", None, "7")
        self.extra = tf("Usage swing % , setup fee", "e.g. 25, 50  → bills vary ±25%, $50 setup fee", "0, 0")
        for x in (self.provider, self.name, self.price, self.cycle, self.extra):
            self.add_item(x)

    async def run(self, inter):
        gid = inter.guild_id
        provs = await q("SELECT * FROM util_providers WHERE guild_id=%s", gid)
        from cogs.staffpanel import best_match
        pname = best_match(val(self.provider), [p["name"] for p in provs], "company")
        p = next(x for x in provs if x["name"] == pname)
        price, cycle = parse_amount(val(self.price)), num(self.cycle, "Cycle days")
        parts = [x.strip() for x in val(self.extra).split(",")] + ["0", "0"]
        try:
            var, setup = int(parts[0] or 0), int((parts[1] or "0").replace("$", ""))
        except ValueError:
            raise EconError("Usage swing and setup fee must be numbers, e.g. `25, 50`.")
        if not 1 <= cycle <= 90 or not 0 <= var <= 100 or setup < 0:
            raise EconError("Cycle 1–90 days, swing 0–100%, setup fee ≥ 0.")
        await ex("INSERT INTO util_plans (provider_id,name,price,cycle_days,variance_pct,setup_fee) VALUES (%s,%s,%s,%s,%s,%s) "
                 "ON DUPLICATE KEY UPDATE price=VALUES(price), cycle_days=VALUES(cycle_days), variance_pct=VALUES(variance_pct), "
                 "setup_fee=VALUES(setup_fee), enabled=1", p["id"], val(self.name)[:64], price, cycle, var, setup)
        c = await cfg(gid)
        return emb(f"📦 Plan saved: {pname} — {val(self.name)}",
                   f"{money(c, price)} every {cycle}d · swing ±{var}% · setup {money(c, setup)} · by {inter.user.mention}", GREEN)


class RulesModal(AdminModal, title="⚙️ Billing rules"):
    def build(self, u):
        self.late = tf("Late fee %", "Added once when a bill passes its due date", u["late_fee_pct"])
        self.grace = tf("Days to pay a bill", None, u["grace_days"])
        self.shut = tf("Shut off after N days overdue", "0 = never", u["shutoff_days"])
        self.reco = tf("Reconnect fee", None, u["reconnect_fee"])
        self.disc = tf("Autopay discount %", "0 = none", u["autopay_discount_pct"])
        for x in (self.late, self.grace, self.shut, self.reco, self.disc):
            self.add_item(x)

    async def run(self, inter):
        late, grace, shut, reco, disc = (num(self.late, "Late fee"), num(self.grace, "Days"), num(self.shut, "Shut-off days"),
                                         num(self.reco, "Reconnect fee"), num(self.disc, "Discount"))
        if not (0 <= late <= 100 and 1 <= grace <= 60 and 0 <= shut <= 365 and reco >= 0 and 0 <= disc <= 50):
            raise EconError("Late fee 0–100%, days 1–60, shut-off 0–365, discount 0–50%.")
        await ex("UPDATE util_config SET late_fee_pct=%s, grace_days=%s, shutoff_days=%s, reconnect_fee=%s, autopay_discount_pct=%s "
                 "WHERE guild_id=%s", late, grace, shut, reco, disc, inter.guild_id)
        return emb("⚙️ Utility billing rules saved", f"Late fee {late}% · {grace} days to pay · shut off after {shut or 'never'} days · "
                   f"reconnect ${reco:,} · autopay discount {disc}% · by {inter.user.mention}", GREEN)


class AccountModal(AdminModal, title="🔌 Shut off / restore / waive"):
    perm = "economy"

    def build(self):
        self.user = discord.ui.Label(text="Player", description="Start typing their name",
                                     component=discord.ui.UserSelect(placeholder="Search players…"))
        self.what = discord.ui.Label(text="Action", component=discord.ui.Select(options=[
            discord.SelectOption(label="Shut off a service", value="shutoff", emoji="⛔", default=True),
            discord.SelectOption(label="Restore a service (no fee)", value="restore", emoji="🔌"),
            discord.SelectOption(label="Waive ALL their unpaid bills", value="waive", emoji="💸"),
            discord.SelectOption(label="Waive one bill (number below)", value="waive1", emoji="🧾")]))
        self.target = tf("Company (part of name) or bill #", "Company for shut off / restore · bill number for waive one",
                         required=False)
        for x in (self.user, self.what, self.target):
            self.add_item(x)

    async def run(self, inter):
        gid, u = inter.guild_id, self.user.component.values[0]
        what = (self.what.component.values or ["shutoff"])[0]
        if what in ("waive", "waive1"):
            if what == "waive1":
                n = val(self.target).lstrip("#")
                if not n.isdigit():
                    raise EconError("Type the bill number.")
                cnt, _ = await ex("UPDATE util_bills SET status='void' WHERE guild_id=%s AND user_id=%s AND id=%s AND status='unpaid'", gid, u.id, int(n))
            else:
                cnt, _ = await ex("UPDATE util_bills SET status='void' WHERE guild_id=%s AND user_id=%s AND status='unpaid'", gid, u.id)
            await self.cog.after_payment(gid, u.id)
            return emb("💸 Utility bills waived", f"{cnt} bill(s) for {u.mention} · by {inter.user.mention}", GOLD)
        accs = await q("SELECT a.*, pr.name pname FROM util_accounts a JOIN util_providers pr ON pr.id=a.provider_id "
                       "WHERE a.guild_id=%s AND a.user_id=%s AND a.status<>'cancelled'", gid, u.id)
        if not accs:
            raise EconError(f"{u.display_name} has no services.")
        from cogs.staffpanel import best_match
        name = best_match(val(self.target) or accs[0]["pname"], [a["pname"] for a in accs], "service")
        a = next(x for x in accs if x["pname"] == name)
        st = "suspended" if what == "shutoff" else "active"
        await ex("UPDATE util_accounts SET status=%s WHERE id=%s", st, a["id"])
        return emb("⛔ Service shut off" if st == "suspended" else "🔌 Service restored", f"{name} for {u.mention} · by {inter.user.mention}", GOLD)


class AdminCard(discord.ui.LayoutView):
    def __init__(self, cog, header=None, is_open=False):
        super().__init__(timeout=None)
        self.cog = cog
        spec = [("provider", "Company", "🏢", discord.ButtonStyle.primary), ("plan", "Plan", "📦", discord.ButtonStyle.primary),
                ("rules", "Billing rules", "⚙️", discord.ButtonStyle.secondary), ("overview", "Overview", "📊", discord.ButtonStyle.secondary),
                ("unpaid", "Unpaid bills", "🧾", discord.ButtonStyle.secondary), ("account", "Shut off / restore / waive", "🔌", discord.ButtonStyle.danger),
                ("billnow", "Bill everyone now", "⚡", discord.ButtonStyle.secondary), ("starter", "Starter pack", "🌱", discord.ButtonStyle.success),
                ("toggle", "Open to players" if not is_open else "Lock player channel", "🔓" if not is_open else "🔒",
                 discord.ButtonStyle.success if not is_open else discord.ButtonStyle.danger)]
        btn = {}
        for k, l, e, st in spec:
            b = discord.ui.Button(label=l, emoji=e, style=st, custom_id=f"utiladm:{k}")
            b.callback = self.on_button
            btn[k] = b
        self.add_item(discord.ui.Container(
            *(header or [discord.ui.TextDisplay("## 💡 Utilities — admin")]), discord.ui.Separator(),
            S("**🏢 Utility companies**\nAdd or edit a company: type, who gets paid, on/off.", btn["provider"]),
            S("**📦 Plans**\nPrice, billing cycle, usage swing (variable bills) and setup fee.", btn["plan"]),
            S("**⚙️ Billing rules**\nLate fee, days to pay, shut-off, reconnect fee, autopay discount.", btn["rules"]),
            discord.ui.Separator(),
            S("**📊 Overview**\nCustomers, revenue, outstanding and per-company totals.", btn["overview"]),
            S("**🧾 Unpaid bills**\nEveryone who owes, oldest first.", btn["unpaid"]),
            S("**🔌 Accounts**\nShut off or restore a player's service, or waive bills.", btn["account"]),
            discord.ui.Separator(),
            S("**⚡ Bill everyone now**\nIssue every active account's next bill immediately.", btn["billnow"]),
            S("**🌱 Starter pack**\nCreate 7 ready-made companies with plans (skips ones that exist).", btn["starter"]),
            S("**🔒 Player channel**\n#utilities is " + ("**open** to everyone." if is_open else "**locked** — staff only."), btn["toggle"]),
            accent_colour=discord.Colour.dark_gold()))

    async def on_button(self, inter):
        k = inter.data["custom_id"].split(":")[1]
        try:
            c = await cfg(inter.guild_id)
            if not is_admin(inter.user, c):
                raise EconError("Staff only.")
            await self.cog.admin_action(inter, k)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


# ═════════════════════ cog ═════════════════════
class Utilities(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(PlayerCard(self))
        bot.add_view(AdminCard(self))
        self.ticker.start()
        self.cards.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)

    def cog_unload(self):
        self.ticker.cancel()
        self.cards.cancel()

    @property
    def conf(self):
        return self.bot.state.settings.setdefault("utilities", {})

    # ───── player actions ─────
    async def player_action(self, inter, k):
        gid, uid = inter.guild_id, inter.user.id
        c, u = await cfg(gid), await ucfg(gid)
        await ensure_user(gid, uid)
        if k == "pay":
            ps = await pay_settings(gid, uid)
            e = emb("💳 Payment menu", "Choose how your utility bills get paid, then press **Save**."
                    + (f"\n🏷️ Autopay saves you **{u['autopay_discount_pct']}%** on every bill." if u["autopay_discount_pct"] else ""), BLUE)
            await inter.response.send_message(embed=e, view=PaymentMenu(self, ps, u["autopay_discount_pct"]), ephemeral=True)
        elif k == "signup":
            if not await pay_settings(gid, uid):
                raise EconError("Set up your **💳 Payment menu** first.")
            provs = await q("SELECT p.* FROM util_providers p WHERE guild_id=%s AND enabled=1 AND EXISTS "
                            "(SELECT 1 FROM util_plans x WHERE x.provider_id=p.id AND x.enabled=1) ORDER BY kind, name", gid)
            if not provs:
                raise EconError("No utility companies are available yet.")
            await inter.response.send_message(embed=emb("➕ Sign up", "Choose a company, then a plan, then press **Sign up**.", BLUE),
                                              view=SignupFlow(self, provs), ephemeral=True)
        elif k == "services":
            accs = await q("SELECT a.*, p.name plan, p.price, p.cycle_days, pr.name pname, pr.kind FROM util_accounts a "
                           "JOIN util_plans p ON p.id=a.plan_id JOIN util_providers pr ON pr.id=a.provider_id "
                           "WHERE a.guild_id=%s AND a.user_id=%s AND a.status<>'cancelled' ORDER BY pr.kind", gid, uid)
            ico = {"active": "🟢", "suspended": "⛔"}
            lines = [f"{KINDS.get(a['kind'], '🧾')} **{a['pname']}** — {a['plan']} · {money(c, a['price'])}/{a['cycle_days']}d\n"
                     f"  {ico.get(a['status'], '•')} {a['status']}" + (f" · next bill <t:{unix(a['next_bill_at'])}:R>" if a["status"] == "active" else
                                                                    " · pay your bills, then press Reconnect") for a in accs]
            ps = await pay_settings(gid, uid)
            e = emb("🔌 My services", "\n".join(lines)[:3800] or "You're not signed up for anything yet.", BLUE)
            e.set_footer(text=f"Paying by: {METHODS.get(ps['method']) if ps else 'not set up'}")
            await inter.response.send_message(embed=e, view=ServicesView(self, accs), ephemeral=True)
        elif k == "bills":
            bills = await q("SELECT * FROM util_bills WHERE guild_id=%s AND user_id=%s AND status='unpaid' ORDER BY due_at", gid, uid)
            tot = sum(b["amount"] + b["late_fee"] for b in bills)
            lines = [f"`#{b['id']}` {b['label']} — **{money(c, b['amount'] + b['late_fee'])}**"
                     + (f" (incl. {money(c, b['late_fee'])} late fee)" if b["late_fee"] else "")
                     + f" · due <t:{unix(b['due_at'])}:R>" + (" ⚠️ **overdue**" if b["due_at"] < now() else "") for b in bills]
            await inter.response.send_message(embed=emb(f"📄 My bills · {money(c, tot)} owed", "\n".join(lines)[:3800] or "All paid up 🎉", BLUE),
                                              view=BillsView(self, bills, c), ephemeral=True)

    async def signup(self, inter, provider_id, plan_id):
        gid, uid = inter.guild_id, inter.user.id
        if not (provider_id and plan_id):
            raise EconError("Pick a company and a plan first.")
        plan = await q1("SELECT p.*, pr.name pname, pr.org_id FROM util_plans p JOIN util_providers pr ON pr.id=p.provider_id "
                        "WHERE p.id=%s AND p.provider_id=%s AND p.enabled=1", plan_id, provider_id)
        if not plan:
            raise EconError("That plan isn't available.")
        if await q1("SELECT 1 FROM util_accounts WHERE guild_id=%s AND user_id=%s AND provider_id=%s AND status<>'cancelled'", gid, uid, provider_id):
            raise EconError(f"You already have **{plan['pname']}** — change plan in 🔌 My services.")
        c = await cfg(gid)
        if plan["setup_fee"]:
            ps = await pay_settings(gid, uid)
            src = ps["method"] if ps["method"] in ("bank", "wallet") else "bank"
            dst = ("org", plan["org_id"]) if plan["org_id"] else BURN
            await transfer(gid, [((src, uid), dst, plan["setup_fee"])], uid, "utility", f"Setup fee — {plan['pname']}")
        t = now()
        await ex("INSERT INTO util_accounts (guild_id,user_id,provider_id,plan_id,status,started_at,next_bill_at) VALUES (%s,%s,%s,%s,'active',%s,%s)",
                 gid, uid, provider_id, plan_id, t, t + dt.timedelta(days=plan["cycle_days"]))
        e = emb(f"✅ Signed up: {plan['pname']} — {plan['name']}",
                f"{money(c, plan['price'])} every {plan['cycle_days']} days" + (f" (±{plan['variance_pct']}% with usage)" if plan["variance_pct"] else "")
                + (f"\nSetup fee paid: {money(c, plan['setup_fee'])}" if plan["setup_fee"] else "")
                + f"\nFirst bill <t:{unix(t + dt.timedelta(days=plan['cycle_days']))}:R>.", GREEN)
        await log(self.bot, gid, emb("💡 New utility customer", f"{inter.user.mention} → {plan['pname']} ({plan['name']})", BLUE))
        await reply(inter, embed=e, ephemeral=True)

    async def reconnect(self, gid, uid, aid):
        a = await q1("SELECT * FROM util_accounts WHERE id=%s AND user_id=%s", aid, uid)
        if not a or a["status"] != "suspended":
            raise EconError("That service isn't shut off.")
        owed = await q1("SELECT COUNT(*) n FROM util_bills WHERE guild_id=%s AND user_id=%s AND account_id=%s AND status='unpaid'", gid, uid, aid)
        if owed["n"]:
            raise EconError("Pay this service's unpaid bills first (📄 My bills).")
        u, c = await ucfg(gid), await cfg(gid)
        if u["reconnect_fee"]:
            prov = await q1("SELECT org_id FROM util_providers WHERE id=%s", a["provider_id"])
            dst = ("org", prov["org_id"]) if prov and prov["org_id"] else BURN
            await transfer(gid, [(("bank", uid), dst, u["reconnect_fee"])], uid, "utility", "Reconnect fee")
        await ex("UPDATE util_accounts SET status='active', next_bill_at=%s WHERE id=%s", now() + dt.timedelta(days=1), aid)
        return f"Reconnected" + (f" — {money(c, u['reconnect_fee'])} reconnect fee paid." if u["reconnect_fee"] else ".")

    async def after_payment(self, gid, uid):
        """Nothing automatic on reconnect (needs the fee); kept as a hook for future rules."""
        return

    # ───── admin actions ─────
    async def admin_action(self, inter, k):
        gid = inter.guild_id
        c, u = await cfg(gid), await ucfg(gid)
        setup_ok = is_admin(inter.user, c, "setup")
        if k in ("provider", "plan", "rules", "starter", "toggle") and not setup_ok:
            raise EconError("You need the Server setup staff permission.")
        if k == "provider":
            await inter.response.send_modal(ProviderModal(self))
        elif k == "plan":
            if not await q1("SELECT 1 FROM util_providers WHERE guild_id=%s", gid):
                raise EconError("Add a company first (or use 🌱 Starter pack).")
            await inter.response.send_modal(PlanModal(self))
        elif k == "rules":
            await inter.response.send_modal(RulesModal(self, u=u))
        elif k == "account":
            await inter.response.send_modal(AccountModal(self))
        elif k == "overview":
            await reply(inter, embed=await self.overview(gid), ephemeral=True)
        elif k == "unpaid":
            rows = await q("SELECT * FROM util_bills WHERE guild_id=%s AND status='unpaid' ORDER BY due_at LIMIT 40", gid)
            lines = [f"`#{b['id']}` <@{b['user_id']}> — {b['label']} · **{money(c, b['amount'] + b['late_fee'])}** · due <t:{unix(b['due_at'])}:R>"
                     + (" ⚠️" if b["due_at"] < now() else "") for b in rows]
            await reply(inter, embed=emb(f"🧾 Unpaid utility bills ({len(rows)})", "\n".join(lines)[:4000] or "None 🎉", BLUE), ephemeral=True)
        elif k == "billnow":
            await inter.response.defer(ephemeral=True)
            n = await self.run_billing(force_gid=gid)
            await inter.followup.send(embed=emb("⚡ Billing run", f"Issued {n} bill(s).", GREEN), ephemeral=True)
        elif k == "starter":
            made = await self.starter(gid)
            await reply(inter, embed=emb("🌱 Starter pack", f"Created {made} compan{'y' if made == 1 else 'ies'} with plans."
                                         + ("\nTip: set who gets paid with 🏢 Company." if made else " Everything already existed."), GREEN), ephemeral=True)
            await self.sync()
        elif k == "toggle":
            await ex("UPDATE util_config SET open=%s WHERE guild_id=%s", 0 if u["open"] else 1, gid)
            await self.sync(perms=True)
            await reply(inter, embed=emb("🔓 #utilities is now OPEN to everyone" if not u["open"] else "🔒 #utilities is now LOCKED (staff only)",
                                         None, GREEN if not u["open"] else GOLD), ephemeral=True)
            await log(self.bot, gid, emb(f"💡 Utilities {'opened' if not u['open'] else 'locked'}", f"by {inter.user.mention}", GOLD))

    async def starter(self, gid):
        made = 0
        for name, kind, desc, plans in STARTER:
            n, _ = await ex("INSERT IGNORE INTO util_providers (guild_id,name,kind,description) VALUES (%s,%s,%s,%s)", gid, name, kind, desc)
            if n:
                made += 1
                pid = (await q1("SELECT id FROM util_providers WHERE guild_id=%s AND name=%s", gid, name))["id"]
                for pl, price, cyc, var, setup in plans:
                    await ex("INSERT IGNORE INTO util_plans (provider_id,name,price,cycle_days,variance_pct,setup_fee) VALUES (%s,%s,%s,%s,%s,%s)",
                             pid, pl, price, cyc, var, setup)
        return made

    async def overview(self, gid):
        c = await cfg(gid)
        acc = await q1("SELECT COUNT(*) n, SUM(status='active') a, SUM(status='suspended') s, COUNT(DISTINCT user_id) users "
                       "FROM util_accounts WHERE guild_id=%s AND status<>'cancelled'", gid)
        bills = await q1("SELECT COALESCE(SUM(CASE WHEN status='paid' THEN amount+late_fee END),0) rev, "
                         "COALESCE(SUM(CASE WHEN status='unpaid' THEN amount+late_fee END),0) owed, SUM(status='unpaid') un, "
                         "SUM(status='unpaid' AND due_at < UTC_TIMESTAMP()) od FROM util_bills WHERE guild_id=%s", gid)
        autopay = await q1("SELECT COUNT(*) n FROM util_payment WHERE guild_id=%s AND method<>'manual'", gid)
        per = await q("SELECT pr.name, pr.kind, COUNT(a.id) n, (SELECT COALESCE(SUM(amount+late_fee),0) FROM util_bills b WHERE "
                      "b.provider_id=pr.id AND b.status='paid') rev FROM util_providers pr LEFT JOIN util_accounts a ON a.provider_id=pr.id "
                      "AND a.status<>'cancelled' WHERE pr.guild_id=%s GROUP BY pr.id ORDER BY n DESC", gid)
        e = emb("📊 Utilities overview", None, BLUE)
        e.add_field(name="Customers", value=f"{acc['users'] or 0} players · {int(acc['a'] or 0)} active · {int(acc['s'] or 0)} shut off")
        e.add_field(name="Autopay users", value=str(autopay["n"]))
        e.add_field(name="Revenue collected", value=money(c, int(bills["rev"])))
        e.add_field(name="Outstanding", value=f"{money(c, int(bills['owed']))} · {int(bills['un'] or 0)} bills ({int(bills['od'] or 0)} overdue)")
        e.add_field(name="By company", value="\n".join(f"{KINDS.get(p['kind'], '🧾')} {p['name']} — {p['n']} customers · {money(c, int(p['rev']))}"
                                                        for p in per)[:1024] or "No companies yet.", inline=False)
        return e

    # ───── automatic billing ─────
    async def run_billing(self, force_gid=None) -> int:
        issued = 0
        cond = "a.status='active' AND " + ("a.guild_id=%s" if force_gid else "a.next_bill_at <= UTC_TIMESTAMP()")
        rows = await q(f"SELECT a.*, p.name plan, p.price, p.cycle_days, p.variance_pct, pr.name pname, pr.kind FROM util_accounts a "
                       f"JOIN util_plans p ON p.id=a.plan_id JOIN util_providers pr ON pr.id=a.provider_id WHERE {cond}",
                       *([force_gid] if force_gid else []))
        for a in rows:
            gid, u, c = a["guild_id"], await ucfg(a["guild_id"]), await cfg(a["guild_id"])
            amount = a["price"]
            if a["variance_pct"]:
                amount = max(0, round(amount * (1 + random.uniform(-a["variance_pct"], a["variance_pct"]) / 100)))
            ps = await pay_settings(gid, a["user_id"])
            if ps and ps["method"] != "manual" and u["autopay_discount_pct"]:
                amount = amount * (100 - u["autopay_discount_pct"]) // 100
            t = now()
            label = f"{KINDS.get(a['kind'], '🧾')} {a['pname']} — {a['plan']}"
            _, bid = await ex("INSERT INTO util_bills (guild_id,user_id,account_id,provider_id,label,amount,issued_at,due_at) "
                              "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", gid, a["user_id"], a["id"], a["provider_id"], label[:160], amount, t,
                              t + dt.timedelta(days=u["grace_days"]))
            nxt = a["next_bill_at"]
            while nxt <= t:
                nxt += dt.timedelta(days=a["cycle_days"])
            await ex("UPDATE util_accounts SET next_bill_at=%s WHERE id=%s", nxt, a["id"])
            issued += 1
            bill = await q1("SELECT * FROM util_bills WHERE id=%s", bid)
            via = await try_autopay(self.bot, gid, bill)
            if ps and ps["reminders"]:
                if via:
                    msg = emb("🧾 Bill paid automatically", f"{label}\n**{money(c, amount)}** paid from your {via}.", GREEN)
                else:
                    msg = emb("🧾 New utility bill", f"{label}\n**{money(c, amount)}** due <t:{unix(bill['due_at'])}:R>."
                              + ("\n⚠️ Autopay couldn't cover it — it'll retry when you have money." if ps["method"] != "manual" else
                                 "\nPay it in #utilities → 📄 My bills."), GOLD)
                await dm(self.bot, a["user_id"], msg, gid)
        return issued

    @tasks.loop(minutes=1)
    async def ticker(self):
        try:
            await self.run_billing()
            # retry autopay on unpaid bills
            for b in await q("SELECT b.* FROM util_bills b JOIN util_payment p ON p.guild_id=b.guild_id AND p.user_id=b.user_id "
                             "WHERE b.status='unpaid' AND p.method<>'manual'"):
                await try_autopay(self.bot, b["guild_id"], b)
            # late fees
            for b in await q("SELECT * FROM util_bills WHERE status='unpaid' AND late_fee=0 AND due_at < UTC_TIMESTAMP()"):
                u, c = await ucfg(b["guild_id"]), await cfg(b["guild_id"])
                fee = b["amount"] * u["late_fee_pct"] // 100
                if fee:
                    await ex("UPDATE util_bills SET late_fee=%s WHERE id=%s", fee, b["id"])
                    ps = await pay_settings(b["guild_id"], b["user_id"])
                    if not ps or ps["reminders"]:
                        await dm(self.bot, b["user_id"], emb("⚠️ Utility bill overdue", f"{b['label']}\nA late fee of **{money(c, fee)}** "
                                                             f"was added. Total now **{money(c, b['amount'] + fee)}**.", RED), b["guild_id"])
            # due-soon reminders (24h)
            for b in await q("SELECT b.* FROM util_bills b LEFT JOIN util_payment p ON p.guild_id=b.guild_id AND p.user_id=b.user_id "
                             "WHERE b.status='unpaid' AND b.reminded=0 AND b.due_at BETWEEN UTC_TIMESTAMP() AND UTC_TIMESTAMP() + INTERVAL 1 DAY "
                             "AND COALESCE(p.reminders,1)=1"):
                c = await cfg(b["guild_id"])
                await ex("UPDATE util_bills SET reminded=1 WHERE id=%s", b["id"])
                await dm(self.bot, b["user_id"], emb("⏰ Utility bill due soon", f"{b['label']} — **{money(c, b['amount'])}** due "
                                                     f"<t:{unix(b['due_at'])}:R>.", GOLD), b["guild_id"])
            # shut-offs
            for a in await q("SELECT DISTINCT a.id, a.guild_id, a.user_id, pr.name pname, u.shutoff_days FROM util_accounts a "
                             "JOIN util_providers pr ON pr.id=a.provider_id JOIN util_config u ON u.guild_id=a.guild_id "
                             "JOIN util_bills b ON b.account_id=a.id AND b.status='unpaid' "
                             "WHERE a.status='active' AND u.shutoff_days>0 AND b.due_at < UTC_TIMESTAMP() - INTERVAL u.shutoff_days DAY"):
                await ex("UPDATE util_accounts SET status='suspended' WHERE id=%s", a["id"])
                await dm(self.bot, a["user_id"], emb("⛔ Service shut off", f"**{a['pname']}** was shut off for unpaid bills. Pay them, "
                                                     "then press 🔌 Reconnect in #utilities → My services.", RED), a["guild_id"])
                await log(self.bot, a["guild_id"], emb("⛔ Utility shut off", f"<@{a['user_id']}> — {a['pname']}", RED))
        except Exception:
            logger.exception("utility billing tick failed")

    @ticker.before_loop
    async def _w(self):
        await self.bot.wait_until_ready()

    # ───── channels ─────
    async def overwrites(self, guild, open_):
        panel = self.bot.get_cog("StaffPanel")
        ow = await panel.overwrites(guild) if panel else {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        for t, p in ow.items():
            if t != guild.me:
                p.send_messages = False
        if open_:
            ow[guild.default_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False)
        return ow

    async def ensure(self, guild, key, name, topic, view, ow, perms):
        ch = guild.get_channel(int(self.conf.get(f"{key}_channel") or 0))
        if not ch:
            c = await cfg(guild.id)
            logch = guild.get_channel(c.get("log_channel") or 0)
            ch = await guild.create_text_channel(name, overwrites=ow, category=getattr(logch, "category", None), topic=topic)
            self.conf.update({f"{key}_channel": ch.id, f"{key}_message": None})
            logger.info("created #%s (%s)", ch.name, ch.id)
        elif perms:
            await ch.edit(overwrites=ow)
        ok = False
        if self.conf.get(f"{key}_message"):
            try:
                await ch.get_partial_message(int(self.conf[f"{key}_message"])).edit(view=view)
                ok = True
            except discord.NotFound:
                pass
        if not ok:
            msg = await ch.send(view=view)
            self.conf[f"{key}_message"] = msg.id
        self.bot.state.save()

    async def sync(self, perms=False):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            c, u = await cfg(gid), await ucfg(gid)
            provs = await q("SELECT pr.*, (SELECT MIN(price) FROM util_plans p WHERE p.provider_id=pr.id AND p.enabled=1) fromp "
                            "FROM util_providers pr WHERE guild_id=%s AND enabled=1 ORDER BY kind, name", gid)
            plist = "\n".join(f"{KINDS.get(p['kind'], '🧾')} **{p['name']}**" + (f" — from {money(c, p['fromp'])}" if p["fromp"] else "")
                              for p in provs) or "*No companies yet.*"
            stamp = f"<t:{int(discord.utils.utcnow().timestamp())}:R>"
            head = [discord.ui.TextDisplay(f"## 💡 Utilities\nKeep the lights on. Set up a payment method, sign up for services and pay your bills."
                                           + (f"\n🏷️ **Autopay saves {u['autopay_discount_pct']}%** on every bill." if u["autopay_discount_pct"] else "")),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(f"### 🏢 Companies\n{plist}\n-# Bills are due {u['grace_days']} days after they're issued · "
                                           f"{u['late_fee_pct']}% late fee" + (f" · shut off after {u['shutoff_days']} days overdue" if u["shutoff_days"] else "")
                                           + f" · updated {stamp}")]
            await self.ensure(g, "player", "utilities", "Utility bills — payment menu, services and bills",
                              PlayerCard(self, head), await self.overwrites(g, bool(u["open"])), perms)
            ov = await self.overview(gid)
            stat = " · ".join(f"**{f.name}:** {f.value}" for f in ov.fields[:4])
            ahead = [discord.ui.TextDisplay(f"## 💡 Utilities — admin\n{'🔓 Player channel **OPEN**' if u['open'] else '🔒 Player channel **LOCKED** (staff only)'}"
                                            f"\n{stat}\n-# Staff only · updated {stamp}")]
            await self.ensure(g, "admin", "utilities-admin", "Utility bills — admin controls (staff only)",
                              AdminCard(self, ahead, bool(u["open"])), await self.overwrites(g, False), perms)

    @tasks.loop(minutes=2)
    async def cards(self):
        try:
            await self.sync()
        except Exception:
            logger.exception("utility cards refresh failed")

    @cards.before_loop
    async def _wc(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="utilities", description="Your utility services, bills and payment menu")
    @app_commands.guild_only()
    async def utilities(self, inter: discord.Interaction):
        await check_open(inter)
        v = discord.ui.View(timeout=300)
        for k, l, e in [("pay", "Payment menu", "💳"), ("signup", "Sign up", "➕"), ("services", "My services", "🔌"), ("bills", "My bills", "📄")]:
            b = discord.ui.Button(label=l, emoji=e)

            async def cb(i2, k=k):
                try:
                    await self.player_action(i2, k)
                except EconError as err:
                    await reply(i2, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)
            b.callback = cb
            v.add_item(b)
        await inter.response.send_message(embed=emb("💡 Utilities", "What do you want to do?", BLUE), view=v, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Utilities(bot))
