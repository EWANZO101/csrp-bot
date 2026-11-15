"""US-style hospital billing.

• /hospital setup   (admin)  — hospital department, cost per second, admission fee, bill cap, days to pay a bill.
• /hospital admit @user      — starts the clock (admission fee charged straight away).
• While admitted the patient is billed automatically: every 30 s the cost accrued since the last charge
  (seconds × rate) is taken from their bank and paid to the hospital department. If they can't cover it, the
  shortfall is carried as debt.
• /hospital discharge @user  — final charge; any debt becomes a medical bill (a citation from the hospital
  department) payable with /citation pay, on the web panel, or via the "Pay now" button in the DM.
• /hospital patients, /hospital bill @user — live running totals.
Admit/discharge: members of the hospital department, or staff with the Citations / Citizens & money permission.
"""
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, money, emb, ts, transfer, ensure_user, get_user, log, dm, reply, is_admin, \
    org_level, MEMBER, BURN, GREEN, RED, BLUE, GOLD
from cogs.orgs import org_autocomplete

logger = logging.getLogger("economy.hospital")
TICK = 30
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS hospital_config (
      guild_id BIGINT UNSIGNED PRIMARY KEY,
      dept_id INT NULL,
      rate DOUBLE NOT NULL DEFAULT 1,
      admit_fee BIGINT NOT NULL DEFAULT 0,
      max_bill BIGINT NOT NULL DEFAULT 0,
      bill_days INT NOT NULL DEFAULT 14
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS hospital_stays (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      admitted_by BIGINT UNSIGNED NOT NULL,
      reason VARCHAR(300) NULL,
      rate DOUBLE NOT NULL,
      admit_fee BIGINT NOT NULL DEFAULT 0,
      max_bill BIGINT NOT NULL DEFAULT 0,
      admitted_at DATETIME NOT NULL,
      accrued BIGINT NOT NULL DEFAULT 0,
      paid BIGINT NOT NULL DEFAULT 0,
      discharged_at DATETIME NULL,
      discharged_by BIGINT UNSIGNED NULL,
      bill_id INT NULL,
      INDEX ix_hs (guild_id, user_id, discharged_at)
    ) ENGINE=InnoDB""",
]


def now():
    return dt.datetime.utcnow().replace(microsecond=0)


def accrued_until(s, when=None) -> int:
    """Total owed for a stay up to `when`: admission fee + seconds × rate, capped by max_bill."""
    secs = max(0, ((when or now()) - s["admitted_at"]).total_seconds())
    total = s["admit_fee"] + int(secs * s["rate"])
    return min(total, s["max_bill"]) if s["max_bill"] else total


def fmt_rate(c, rate):
    r = f"{rate:,.2f}".rstrip("0").rstrip(".")
    return f"{c['currency']}{r}/sec (≈ {money(c, int(rate * 3600))}/hr)"


async def hcfg(gid):
    r = await q1("SELECT * FROM hospital_config WHERE guild_id=%s", gid)
    if not r:
        await ex("INSERT IGNORE INTO hospital_config (guild_id) VALUES (%s)", gid)
        r = await q1("SELECT * FROM hospital_config WHERE guild_id=%s", gid)
    return r


async def active_stay(gid, uid):
    return await q1("SELECT * FROM hospital_stays WHERE guild_id=%s AND user_id=%s AND discharged_at IS NULL", gid, uid)


async def can_treat(member, gid, h):
    c = await cfg(gid)
    if is_admin(member, c, "citations") or is_admin(member, c, "economy"):
        return True
    if h["dept_id"]:
        o = await q1("SELECT * FROM orgs WHERE id=%s", h["dept_id"])
        if o and await org_level(member, o) >= MEMBER:
            return True
    return False


async def charge(bot, s, when=None) -> tuple[int, int]:
    """Bring a stay's billing up to date. Returns (charged_now, debt)."""
    gid, uid = s["guild_id"], s["user_id"]
    h = await hcfg(gid)
    owed = accrued_until(s, when)
    due = owed - s["accrued"]          # newly accrued since last tick
    debt_before = s["accrued"] - s["paid"]
    want = due + debt_before           # try to also catch up any previous shortfall
    took = 0
    if want > 0:
        acc = await get_user(gid, uid)
        took = max(0, min(want, acc["bank"]))
        if took:
            dst = ("org", h["dept_id"]) if h["dept_id"] else BURN
            await transfer(gid, [(("bank", uid), dst, took)], bot.user.id, "medical", f"Hospital stay #{s['id']}")
    await ex("UPDATE hospital_stays SET accrued=%s, paid=paid+%s WHERE id=%s", owed, took, s["id"])
    s["accrued"], s["paid"] = owed, s["paid"] + took
    return took, owed - s["paid"]


class PayBill(discord.ui.View):
    def __init__(self, cid):
        super().__init__(timeout=None)
        b = discord.ui.Button(label="Pay bill now", emoji="💳", style=discord.ButtonStyle.success, custom_id=f"hbill:{cid}")
        b.callback = self.pay
        self.add_item(b)
        self.cid = cid

    async def pay(self, inter):
        from cogs.citations import pay_citation
        try:
            e = await pay_citation(inter, self.cid, "bank")
        except EconError as err:
            e = emb("❌ " + str(err)[:240], None, RED)
        await inter.response.send_message(embed=e, ephemeral=True)


class AdmitModal(discord.ui.Modal, title="🏥 Admit a patient"):
    user = discord.ui.Label(text="Patient", description="Start typing their name",
                            component=discord.ui.UserSelect(placeholder="Search players…"))
    reason = discord.ui.Label(text="Reason / injury", component=discord.ui.TextInput(required=False, max_length=300,
                                                                                     placeholder="optional"))

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, inter):
        try:
            await self.cog.do_admit(inter, self.user.component.values[0], self.reason.component.value or None)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class DischargeModal(discord.ui.Modal, title="🩺 Discharge a patient"):
    user = discord.ui.Label(text="Patient", description="Start typing their name",
                            component=discord.ui.UserSelect(placeholder="Search players…"))
    note = discord.ui.Label(text="Note on the bill", component=discord.ui.TextInput(required=False, max_length=300,
                                                                                    placeholder="optional"))

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, inter):
        try:
            await self.cog.do_discharge(inter, self.user.component.values[0], self.note.component.value or None)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class SettingsModal(discord.ui.Modal, title="⚙️ Hospital billing settings"):
    def __init__(self, cog, h, dept_name, c):
        super().__init__()
        self.cog = cog
        def field(label, desc, default, required=True):
            return discord.ui.Label(text=label, description=desc, component=discord.ui.TextInput(
                default=str(default) if default not in (None, "") else None, required=required, max_length=64))
        rate = f"{h['rate']:.4f}".rstrip("0").rstrip(".")
        self.dept = field("Hospital department", "Gets the money · part of the name is fine · empty = money destroyed",
                          dept_name or "", required=False)
        self.rate = field("Cost per second", "Decimals ok, e.g. 0.5", rate)
        self.fee = field("Admission fee", "Charged on admission (0 = none)", h["admit_fee"])
        self.cap = field("Cap per stay", "0 = no cap", h["max_bill"])
        self.days = field("Days to pay the final bill", None, h["bill_days"])
        for f in (self.dept, self.rate, self.fee, self.cap, self.days):
            self.add_item(f)

    async def on_submit(self, inter):
        gid = inter.guild_id
        try:
            c = await cfg(gid)
            if not is_admin(inter.user, c, "setup"):
                raise EconError("You need the Server setup staff permission.")
            try:
                rate = float(self.rate.component.value.replace(",", "").replace("$", ""))
                fee, cap, days = (int(x.component.value.replace(",", "").replace("$", "")) for x in (self.fee, self.cap, self.days))
            except ValueError:
                raise EconError("Rate, fee, cap and days must be numbers.")
            if rate < 0 or fee < 0 or cap < 0 or not 1 <= days <= 365:
                raise EconError("Values can't be negative; days must be 1–365.")
            dept_id = None
            if (self.dept.component.value or "").strip():
                from cogs.staffpanel import match_org
                dept_id = (await match_org(gid, self.dept.component.value))["id"]
            await hcfg(gid)
            await ex("UPDATE hospital_config SET dept_id=%s, rate=%s, admit_fee=%s, max_bill=%s, bill_days=%s WHERE guild_id=%s",
                     dept_id, rate, fee, cap, days, gid)
            e = await self.cog.settings_embed(gid)
            e.description = f"Saved by {inter.user.mention} · applies to new admissions"
            await log(inter.client, gid, e)
            await reply(inter, embed=e, ephemeral=True)
            await self.cog.sync_board()
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class WaiveModal(discord.ui.Modal, title="💸 Waive a medical bill"):
    bill = discord.ui.Label(text="Bill number", description="The # from the bill (see 🧾 Unpaid bills)",
                            component=discord.ui.TextInput(placeholder="e.g. 12", max_length=10))
    reason = discord.ui.Label(text="Reason", component=discord.ui.TextInput(required=False, max_length=300, placeholder="optional"))

    async def on_submit(self, inter):
        gid = inter.guild_id
        try:
            c = await cfg(gid)
            if not (is_admin(inter.user, c, "citations") or is_admin(inter.user, c, "economy")):
                raise EconError("You need the Citations or Citizens & money staff permission.")
            v = self.bill.component.value.strip().lstrip("#")
            if not v.isdigit():
                raise EconError("Type the bill number, e.g. 12.")
            r = await q1("SELECT * FROM citations WHERE guild_id=%s AND id=%s AND reason LIKE %s", gid, int(v), "🏥%")
            if not r:
                raise EconError(f"No medical bill #{v}.")
            if r["status"] != "unpaid":
                raise EconError(f"Bill #{v} is already {r['status']}.")
            note = self.reason.component.value or "Waived by hospital admin"
            await ex("UPDATE citations SET status='void', note=%s WHERE id=%s AND status='unpaid'", note[:300], r["id"])
            e = emb("💸 Medical bill waived", f"Bill #{r['id']} for <@{r['user_id']}> · {money(c, r['amount'] + r['late_fee'])}"
                    f"\nby {inter.user.mention} — {note}", GOLD)
            await log(inter.client, gid, e)
            await reply(inter, embed=e, ephemeral=True)
            await dm(inter.client, r["user_id"], e, gid)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class HospitalAdminView(discord.ui.LayoutView):
    def __init__(self, cog, body=None):
        super().__init__(timeout=None)
        self.cog = cog
        row1 = [discord.ui.Button(label="Billing settings", emoji="⚙️", style=discord.ButtonStyle.primary, custom_id="hadm:settings"),
                discord.ui.Button(label="Patients", emoji="🛏️", style=discord.ButtonStyle.secondary, custom_id="hadm:patients"),
                discord.ui.Button(label="Force discharge", emoji="🚑", style=discord.ButtonStyle.danger, custom_id="hadm:discharge")]
        row2 = [discord.ui.Button(label="Unpaid bills", emoji="🧾", style=discord.ButtonStyle.secondary, custom_id="hadm:bills"),
                discord.ui.Button(label="Waive a bill", emoji="💸", style=discord.ButtonStyle.secondary, custom_id="hadm:waive"),
                discord.ui.Button(label="Stats", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="hadm:stats")]
        for b in row1 + row2:
            b.callback = self.on_button
        self.add_item(discord.ui.Container(*(body or [discord.ui.TextDisplay("## 🏥 Hospital admin")]), discord.ui.Separator(),
                                           discord.ui.ActionRow(*row1), discord.ui.ActionRow(*row2),
                                           accent_colour=discord.Colour.dark_red()))

    async def on_button(self, inter: discord.Interaction):
        cid, gid = inter.data.get("custom_id"), inter.guild_id
        try:
            c, h = await cfg(gid), await hcfg(gid)
            if not is_admin(inter.user, c):
                raise EconError("Staff only.")
            if cid == "hadm:settings":
                if not is_admin(inter.user, c, "setup"):
                    raise EconError("You need the Server setup staff permission.")
                dept = await q1("SELECT name FROM orgs WHERE id=%s", h["dept_id"]) if h["dept_id"] else None
                await inter.response.send_modal(SettingsModal(self.cog, h, dept["name"] if dept else "", c))
            elif cid == "hadm:patients":
                await self.cog.do_patients(inter, True)
            elif cid == "hadm:discharge":
                if not await can_treat(inter.user, gid, h):
                    raise EconError("Only hospital staff can discharge.")
                await inter.response.send_modal(DischargeModal(self.cog))
            elif cid == "hadm:waive":
                await inter.response.send_modal(WaiveModal())
            elif cid == "hadm:bills":
                rows = await q("SELECT * FROM citations WHERE guild_id=%s AND status='unpaid' AND reason LIKE %s ORDER BY due_at", gid, "🏥%")
                tot = sum(r["amount"] + r["late_fee"] for r in rows)
                lines = [f"`#{r['id']}` <@{r['user_id']}> — **{money(c, r['amount'] + r['late_fee'])}** · due {ts(r['due_at'], 'R')}"
                         + (" ⚠️ overdue" if r["due_at"] and r["due_at"] < dt.datetime.utcnow() else "") for r in rows]
                await reply(inter, embed=emb(f"🧾 Unpaid medical bills ({len(rows)}) · {money(c, tot)}",
                                             "\n".join(lines)[:4000] or "None 🎉", BLUE), ephemeral=True)
            elif cid == "hadm:stats":
                await reply(inter, embed=await self.cog.stats_embed(gid), ephemeral=True)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class HospitalView(discord.ui.LayoutView):
    def __init__(self, cog, body=None):
        super().__init__(timeout=None)
        self.cog = cog
        buttons = [
            discord.ui.Button(label="Admit patient", emoji="🏥", style=discord.ButtonStyle.danger, custom_id="hosp:admit"),
            discord.ui.Button(label="Discharge", emoji="🩺", style=discord.ButtonStyle.success, custom_id="hosp:discharge"),
            discord.ui.Button(label="Patients", emoji="🛏️", style=discord.ButtonStyle.secondary, custom_id="hosp:patients"),
            discord.ui.Button(label="My bill", emoji="💳", style=discord.ButtonStyle.primary, custom_id="hosp:mybill"),
        ]
        for b in buttons:
            b.callback = self.on_button
        admit, discharge, patients, mybill = buttons
        S, T = discord.ui.Section, discord.ui.TextDisplay
        rows = [
            T("### 📋 Commands"),
            S(T("**🏥 `/hospital admit user:@player reason:…`**\nStarts the clock and charges the admission fee. The patient gets a DM."),
              accessory=admit),
            discord.ui.Separator(visible=False),
            T("**⏱️ Automatic, every 30 sec**\nThe cost so far is taken from the patient's bank and paid to the hospital department. "
              "If they run out of money, the rest is recorded as debt."),
            discord.ui.Separator(visible=False),
            S(T("**🩺 `/hospital discharge user:@player`**\nFinal charge. Any debt becomes a 🏥 medical bill with a due date. "
                "The patient gets a DM with a 💳 Pay bill now button."), accessory=discharge),
            discord.ui.Separator(visible=False),
            S(T("**🛏️ `/hospital patients`**\nEveryone admitted right now, with their live bill and how long they've been in."),
              accessory=patients),
            discord.ui.Separator(visible=False),
            S(T("**💳 `/hospital bill user:@player`**\nSomeone's running bill, or their unpaid medical bills."), accessory=mybill),
            T(f"-# Admit / Discharge: hospital staff only · updated <t:{int(discord.utils.utcnow().timestamp())}:R>"),
        ]
        self.add_item(discord.ui.Container(*(body or [discord.ui.TextDisplay("Loading…")]), discord.ui.Separator(), *rows,
                                           accent_colour=discord.Colour.red()))

    async def on_button(self, inter: discord.Interaction):
        cid = inter.data.get("custom_id")
        try:
            if cid in ("hosp:admit", "hosp:discharge"):
                if not await can_treat(inter.user, inter.guild_id, await hcfg(inter.guild_id)):
                    raise EconError("Only hospital department members or staff can do that.")
                await inter.response.send_modal(AdmitModal(self.cog) if cid == "hosp:admit" else DischargeModal(self.cog))
            elif cid == "hosp:patients":
                await self.cog.do_patients(inter, True)
            else:
                await self.cog.do_bill(inter, None)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class Hospital(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(HospitalView(self))
        bot.add_view(HospitalAdminView(self))
        self.ticker.start()
        self.board.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)

    def cog_unload(self):
        self.ticker.cancel()
        self.board.cancel()

    # ───────── #hospital board ─────────
    @property
    def conf(self):
        return self.bot.state.settings.setdefault("hospital_board", {})

    async def render(self, guild):
        gid = guild.id
        c, h = await cfg(gid), await hcfg(gid)
        dept = await q1("SELECT name FROM orgs WHERE id=%s", h["dept_id"]) if h["dept_id"] else None
        rows = await q("SELECT * FROM hospital_stays WHERE guild_id=%s AND discharged_at IS NULL ORDER BY admitted_at", gid)
        beds = "\n".join(f"🛏️ <@{s['user_id']}> — in since <t:{int(s['admitted_at'].replace(tzinfo=dt.timezone.utc).timestamp())}:R>"
                         f" · bill so far **{money(c, accrued_until(s))}**" + (f"\n-# ↳ {s['reason']}" if s["reason"] else "")
                         for s in rows[:25]) or "*No patients right now.*"
        body = [
            discord.ui.TextDisplay(f"## 🏥 {dept['name'] if dept else 'Hospital'}\n"
                                   f"Billing **{fmt_rate(c, h['rate'])}** while admitted"
                                   + (f" · admission fee **{money(c, h['admit_fee'])}**" if h["admit_fee"] else "")
                                   + (f" · capped at **{money(c, h['max_bill'])}** per stay" if h["max_bill"] else "")
                                   + f" · bills due in **{h['bill_days']} days**"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"### 🛏️ Patients ({len(rows)})\n{beds}"),
        ]
        return HospitalView(self, body)

    async def stats_embed(self, gid):
        c = await cfg(gid)
        st = await q1("SELECT COUNT(*) n, SUM(admitted_at > UTC_TIMESTAMP() - INTERVAL 1 DAY) d, "
                      "SUM(admitted_at > UTC_TIMESTAMP() - INTERVAL 7 DAY) w, COALESCE(SUM(accrued),0) billed, "
                      "COALESCE(SUM(paid),0) paid, COALESCE(AVG(TIMESTAMPDIFF(SECOND, admitted_at, COALESCE(discharged_at, UTC_TIMESTAMP()))),0) avg_s "
                      "FROM hospital_stays WHERE guild_id=%s", gid)
        bills = await q1("SELECT COUNT(*) n, COALESCE(SUM(amount+late_fee),0) owed FROM citations WHERE guild_id=%s "
                         "AND status='unpaid' AND reason LIKE %s", gid, "🏥%")
        paid_bills = await q1("SELECT COALESCE(SUM(amount+late_fee),0) s FROM citations WHERE guild_id=%s AND status='paid' "
                              "AND reason LIKE %s", gid, "🏥%")
        e = emb("📊 Hospital stats", None, BLUE)
        e.add_field(name="Admissions", value=f"{st['n']} total · {int(st['d'] or 0)} today · {int(st['w'] or 0)} this week")
        e.add_field(name="Average stay", value=str(dt.timedelta(seconds=int(st["avg_s"]))))
        e.add_field(name="Billed", value=money(c, int(st["billed"])))
        e.add_field(name="Collected", value=money(c, int(st["paid"]) + int(paid_bills["s"])))
        e.add_field(name="Unpaid bills", value=f"{bills['n']} · {money(c, int(bills['owed']))}")
        return e

    async def sync_admin(self):
        panel = self.bot.state.settings.get("staff_panel") or {}
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            ch = g and g.get_channel(int(panel.get("channel_id") or 0))
            if not ch:
                continue
            c, h = await cfg(gid), await hcfg(gid)
            dept = await q1("SELECT name FROM orgs WHERE id=%s", h["dept_id"]) if h["dept_id"] else None
            n = await q1("SELECT COUNT(*) n FROM hospital_stays WHERE guild_id=%s AND discharged_at IS NULL", gid)
            b = await q1("SELECT COUNT(*) n, COALESCE(SUM(amount+late_fee),0) s FROM citations WHERE guild_id=%s AND status='unpaid' "
                         "AND reason LIKE %s", gid, "🏥%")
            body = [discord.ui.TextDisplay(
                f"## 🏥 Hospital admin\n"
                f"**Department:** {dept['name'] if dept else '⚠️ not set — money is destroyed'}\n"
                f"**Cost:** {fmt_rate(c, h['rate'])} · **Admission fee:** {money(c, h['admit_fee'])} · "
                f"**Cap:** {money(c, h['max_bill']) if h['max_bill'] else 'none'} · **Bills due in:** {h['bill_days']} days\n"
                f"🛏️ **{n['n']}** admitted now · 🧾 **{b['n']}** unpaid bills ({money(c, int(b['s']))})\n"
                f"-# Staff only · settings need the Server setup permission · updated <t:{int(discord.utils.utcnow().timestamp())}:R>")]
            view = HospitalAdminView(self, body)
            ok = False
            if self.conf.get("admin_message_id"):
                try:
                    await ch.get_partial_message(int(self.conf["admin_message_id"])).edit(view=view)
                    ok = True
                except discord.NotFound:
                    pass
            if not ok:
                msg = await ch.send(view=view)
                self.conf["admin_message_id"] = msg.id
                self.bot.state.save()

    async def sync_board(self):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            ch = g.get_channel(int(self.conf.get("channel_id") or 0))
            if not ch:
                c = await cfg(gid)
                logch = g.get_channel(c.get("log_channel") or 0)
                ow = {g.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False),
                      g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
                ch = await g.create_text_channel("hospital", overwrites=ow, category=getattr(logch, "category", None),
                                                 topic="Hospital admissions & medical bills — live", reason="Hospital board")
                self.conf.update(channel_id=ch.id, message_id=None)
                logger.info("created #%s (%s)", ch.name, ch.id)
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
                self.conf["message_id"] = msg.id
            self.bot.state.save()

    @tasks.loop(seconds=30)
    async def board(self):
        try:
            await self.sync_board()
            await self.sync_admin()
        except Exception:
            logger.exception("hospital board refresh failed")

    @board.before_loop
    async def _wait_board(self):
        await self.bot.wait_until_ready()

    hospital = app_commands.Group(name="hospital", description="Hospital admissions & medical billing", guild_only=True)

    # ───────── admin ─────────
    @hospital.command(name="setup", description="(Admin) Hospital department, cost per second, fees and bill rules")
    @app_commands.describe(department="Department that receives the money (e.g. EMS / hospital)",
                           per_second="Amount billed per second while admitted (decimals ok, e.g. 0.5)",
                           admission_fee="One-off fee charged on admission",
                           max_bill="Cap per stay (0 = no cap)", bill_days="Days to pay the final bill")
    @app_commands.autocomplete(department=org_autocomplete("dept"))
    async def setup(self, inter: discord.Interaction, department: str | None = None,
                    per_second: app_commands.Range[float, 0, 1_000_000] | None = None,
                    admission_fee: app_commands.Range[int, 0, 1_000_000_000] | None = None,
                    max_bill: app_commands.Range[int, 0, 10_000_000_000] | None = None,
                    bill_days: app_commands.Range[int, 1, 365] | None = None):
        await core.require_admin(inter, "setup")
        gid = inter.guild_id
        h = await hcfg(gid)
        if department:
            o = await core.find_org(gid, department, "dept")
            await ex("UPDATE hospital_config SET dept_id=%s WHERE guild_id=%s", o["id"], gid)
        for col, val in (("rate", per_second), ("admit_fee", admission_fee), ("max_bill", max_bill), ("bill_days", bill_days)):
            if val is not None:
                await ex(f"UPDATE hospital_config SET {col}=%s WHERE guild_id=%s", val, gid)
        await reply(inter, embed=await self.settings_embed(gid), ephemeral=True)

    async def settings_embed(self, gid):
        c, h = await cfg(gid), await hcfg(gid)
        dept = await q1("SELECT name FROM orgs WHERE id=%s", h["dept_id"]) if h["dept_id"] else None
        e = emb("🏥 Hospital settings", None, BLUE)
        e.add_field(name="Department (gets paid)", value=dept["name"] if dept else "— none (money destroyed) —", inline=False)
        e.add_field(name="Cost", value=fmt_rate(c, h["rate"]))
        e.add_field(name="Admission fee", value=money(c, h["admit_fee"]))
        e.add_field(name="Cap per stay", value=money(c, h["max_bill"]) if h["max_bill"] else "none")
        e.add_field(name="Days to pay bill", value=str(h["bill_days"]))
        e.set_footer(text="Change with /hospital setup · new rates apply to new admissions")
        return e

    @hospital.command(name="settings", description="Show the hospital billing settings")
    async def settings(self, inter: discord.Interaction):
        await reply(inter, embed=await self.settings_embed(inter.guild_id), ephemeral=True)

    # ───────── medical staff ─────────
    @hospital.command(name="admit", description="Admit a patient — billing starts now")
    async def admit(self, inter: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 0, 300] | None = None):
        await self.do_admit(inter, user, reason)

    async def do_admit(self, inter, user, reason=None):
        gid = inter.guild_id
        h = await hcfg(gid)
        if not await can_treat(inter.user, gid, h):
            raise EconError("Only hospital department members or staff can admit patients.")
        if user.bot:
            raise EconError("Bots don't get sick.")
        if await active_stay(gid, user.id):
            raise EconError(f"{user.mention} is already admitted.")
        await ensure_user(gid, user.id)
        t = now()
        await ex("INSERT INTO hospital_stays (guild_id,user_id,admitted_by,reason,rate,admit_fee,max_bill,admitted_at) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", gid, user.id, inter.user.id, reason, h["rate"], h["admit_fee"], h["max_bill"], t)
        s = await active_stay(gid, user.id)
        await charge(self.bot, s)                     # admission fee straight away
        c = await cfg(gid)
        e = emb("🏥 Patient admitted", f"{user.mention} admitted by {inter.user.mention}" + (f"\n> {reason}" if reason else ""), BLUE)
        e.add_field(name="Billing", value=fmt_rate(c, h["rate"]))
        if h["admit_fee"]:
            e.add_field(name="Admission fee", value=money(c, h["admit_fee"]))
        e.add_field(name="Since", value=ts(t, "R"))
        await log(self.bot, gid, e)
        await reply(inter, embed=e)
        await dm(self.bot, user.id, emb("🏥 You've been admitted to hospital",
                                        f"You're being billed {fmt_rate(c, h['rate'])} until you're discharged.", BLUE), gid)

    @hospital.command(name="discharge", description="Discharge a patient and send the final bill")
    async def discharge(self, inter: discord.Interaction, user: discord.Member, note: app_commands.Range[str, 0, 300] | None = None):
        await self.do_discharge(inter, user, note)

    async def do_discharge(self, inter, user, note=None):
        gid = inter.guild_id
        h = await hcfg(gid)
        if not await can_treat(inter.user, gid, h):
            raise EconError("Only hospital department members or staff can discharge patients.")
        s = await active_stay(gid, user.id)
        if not s:
            raise EconError(f"{user.mention} isn't admitted.")
        t = now()
        took, debt = await charge(self.bot, s, t)
        bill_id = None
        if debt > 0:
            _, bill_id = await ex("INSERT INTO citations (guild_id,dept_id,user_id,issuer_id,amount,reason,due_at,note) "
                               "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", gid, h["dept_id"], user.id, inter.user.id, debt,
                               f"🏥 Medical bill — hospital stay #{s['id']}" + (f" ({s['reason']})" if s["reason"] else ""),
                               t + dt.timedelta(days=h["bill_days"]), note)
        await ex("UPDATE hospital_stays SET discharged_at=%s, discharged_by=%s, bill_id=%s WHERE id=%s",
                 t, inter.user.id, bill_id, s["id"])
        c = await cfg(gid)
        secs = int((t - s["admitted_at"]).total_seconds())
        e = emb("🩺 Patient discharged", f"{user.mention} discharged by {inter.user.mention}" + (f"\n> {note}" if note else ""),
                GREEN if not debt else GOLD)
        e.add_field(name="Time admitted", value=str(dt.timedelta(seconds=secs)))
        e.add_field(name="Total cost", value=money(c, s["accrued"]))
        e.add_field(name="Paid from bank", value=money(c, s["paid"]))
        e.add_field(name="Outstanding bill", value=f"{money(c, debt)} — bill #{bill_id}, due {ts(t + dt.timedelta(days=h['bill_days']), 'D')}"
                    if debt else "None — fully paid", inline=False)
        await log(self.bot, gid, e)
        await reply(inter, embed=e)
        if bill_id:
            e.set_footer(text=f"Pay with the button below or /citation pay id:{bill_id}")
        try:
            u = self.bot.get_user(user.id) or await self.bot.fetch_user(user.id)
            await u.send(embed=e, view=PayBill(bill_id) if bill_id else discord.utils.MISSING)
        except discord.HTTPException:
            pass

    @hospital.command(name="patients", description="Everyone currently admitted, with live running bills")
    async def patients(self, inter: discord.Interaction):
        await self.do_patients(inter, False)

    async def do_patients(self, inter, ephemeral):
        gid = inter.guild_id
        c = await cfg(gid)
        rows = await q("SELECT * FROM hospital_stays WHERE guild_id=%s AND discharged_at IS NULL ORDER BY admitted_at", gid)
        lines = []
        for s in rows:
            owed = accrued_until(s)
            lines.append(f"🛏️ <@{s['user_id']}> — since {ts(s['admitted_at'], 'R')} · bill so far **{money(c, owed)}** "
                         f"(paid {money(c, s['paid'])}) · {fmt_rate(c, s['rate'])}" + (f"\n  ↳ {s['reason']}" if s["reason"] else ""))
        await reply(inter, embed=emb(f"🏥 Patients ({len(rows)})", "\n".join(lines)[:4000] or "Nobody is admitted.", BLUE),
                    ephemeral=ephemeral)

    @hospital.command(name="bill", description="A patient's running bill (or your own)")
    async def bill(self, inter: discord.Interaction, user: discord.Member | None = None):
        await self.do_bill(inter, user)

    async def do_bill(self, inter, user=None):
        gid, user = inter.guild_id, user or inter.user
        c = await cfg(gid)
        s = await active_stay(gid, user.id)
        if s:
            owed = accrued_until(s)
            e = emb(f"🏥 {user.display_name} — admitted", f"Since {ts(s['admitted_at'], 'R')} · {fmt_rate(c, s['rate'])}", BLUE)
            e.add_field(name="Bill so far", value=money(c, owed))
            e.add_field(name="Paid", value=money(c, s["paid"]))
            e.add_field(name="Owing", value=money(c, max(0, owed - s["paid"])))
        else:
            bills = await q("SELECT * FROM citations WHERE guild_id=%s AND user_id=%s AND status='unpaid' AND reason LIKE %s",
                            gid, user.id, "🏥%")
            e = emb(f"🏥 {user.display_name}", "Not admitted.", BLUE)
            if bills:
                e.add_field(name="Unpaid medical bills", value="\n".join(
                    f"#{b['id']} — {money(c, b['amount'] + b['late_fee'])}, due {ts(b['due_at'], 'D')}" for b in bills)[:1024])
        await reply(inter, embed=e, ephemeral=True)

    # ───────── auto billing ─────────
    @tasks.loop(seconds=TICK)
    async def ticker(self):
        try:
            for s in await q("SELECT * FROM hospital_stays WHERE discharged_at IS NULL"):
                await charge(self.bot, s)
        except Exception:
            logger.exception("hospital billing tick failed")

    @ticker.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Hospital(bot))
