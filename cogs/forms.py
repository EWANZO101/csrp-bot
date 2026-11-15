"""Member forms: #💢│user-setup and #💢│dept-transfers.

Each channel gets a card (kept at the bottom) with a 📝 button that opens a form. Every submission is posted as a card
with ✅ Approve / ❌ Deny for staff (Applications staff permission).
  • User setup  — First & Last Name, Xbox Gamertag, Department(s). Approve gives [✗] CSRP Member + department roles,
                  sets the nickname (editable, prefilled "{CIV} First Last") and removes "Needs User Setup".
  • Transfer    — from / to department, 30+ days, aware of policies, reason. Approve can swap the department roles.
The member gets a DM with the decision. Old free-text posts in both channels were imported (status "legacy"), so
🔎 Look up a member shows everyone's setup info and transfer history.
"""
import datetime as dt
import json
import logging
import re

import discord
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, emb, log, reply, is_admin, GREEN, RED, BLUE, GOLD

logger = logging.getLogger("economy.forms")
PERM = "applications"
CHANNELS = {"setup": 1032587296085643295, "transfer": 1032587296085643298}
NEEDS_SETUP = 1337436044097880105
MEMBER_ROLES = [1032587294881882120, 1032587294881882121]          # [✗] CSRP Member, ~~~ CSRP Roles ~~~
DEPTS = {  # code: (label, emoji, header role, tag role, nickname prefix)
    "civ": ("Civilian", "🧍", 1032587295053840384, 1032587295020302466, "{CIV}"),
    "sast": ("SAST — State Troopers", "🚔", 1032587295192252428, 1155356012484763719, "{SAST}"),
    "bcso": ("BCSO — Sheriff's Office", "⭐", 1032587295150313493, 1155355945027768360, "{BCSO}"),
    "lspd": ("LSPD — Police", "🚓", 1032587295217422345, 1155355856725082162, "{LSPD}"),
    "safd": ("SAFD — Fire & EMS", "🚒", 1032587295074816045, 1155356073046323280, "{SAFD}"),
    "dispatch": ("Dispatch", "📟", 1032587294999326769, 1155356260871438428, "{DISPATCH}"),
}
SETUP_INTRO = ("Thanks for joining CSRP. To be an official member of the community, press **📝 Fill out user setup** and "
               "answer the questions below. Staff will review it and give you your roles.")
TRANSFER_INTRO = "Want to move departments? Press **📝 Request a transfer** and answer the questions. Staff will review it."
CARD_TEXT = {  # defaults; staff can override each part with ✏️ Edit (saved in settings → forms.<kind>.text)
    "setup": {"title": "📋 California State Roleplay — User Setup", "intro": SETUP_INTRO,
              "asks": "**The form asks for:**\n• 🪪 Discord name *(filled in for you)*\n• 👤 First & Last Name\n"
                      "• 🎮 Xbox Gamertag\n• 🏛️ Department — Civilian, SAST, BCSO, LSPD, SAFD or Dispatch"},
    "transfer": {"title": "🔁 Department Transfer", "intro": TRANSFER_INTRO,
                 "asks": "**The form asks:**\n• Which department you're transferring **from** and **to**\n"
                         "• Have you been in your current department for more than 30 days?\n"
                         "• Are you aware of the policies of the department you're going to?\n• Why you'd like to transfer (1–2 sentences)"},
}
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS form_entries (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      kind VARCHAR(16) NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      data TEXT NOT NULL,
      status VARCHAR(12) NOT NULL DEFAULT 'pending',
      staff_id BIGINT UNSIGNED NULL,
      note VARCHAR(500) NULL,
      msg_id BIGINT UNSIGNED NULL,
      legacy_msg_id BIGINT UNSIGNED NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      decided_at DATETIME NULL,
      UNIQUE KEY uq_form_legacy (legacy_msg_id),
      INDEX ix_form (guild_id, kind, status),
      INDEX ix_form_user (guild_id, user_id)
    ) ENGINE=InnoDB""",
]
STATUS = {"pending": ("🟡", "Pending review", GOLD), "approved": ("✅", "Approved", GREEN), "denied": ("❌", "Denied", RED),
          "legacy": ("🗂️", "Imported from old post", BLUE)}


def dept_label(codes) -> str:
    return ", ".join(f"{DEPTS[c][1]} {DEPTS[c][0]}" for c in codes if c in DEPTS) or "—"


def dept_codes(text) -> list[str]:
    """Free text → department codes (for old posts)."""
    t = (text or "").lower()
    out = []
    for code, words in (("civ", ("civ", "civilian", "mechanic", "mechnic")), ("sast", ("sast", "trooper", "state")),
                        ("bcso", ("bcso", "sheriff", "blaine")), ("lspd", ("lspd", "police")),
                        ("safd", ("safd", "fire", "ems", "medic")), ("dispatch", ("dispatch",))):
        if any(re.search(rf"\b{w}", t) for w in words):
            out.append(code)
    return out


def dept_options(default=()):
    return [discord.SelectOption(label=v[0], value=k, emoji=v[1], default=k in default) for k, v in DEPTS.items()]


def yn_options():
    return [discord.SelectOption(label="Yes", value="yes", emoji="✅"), discord.SelectOption(label="No", value="no", emoji="❌")]


def text_label(label, ph=None, default=None, required=True, paragraph=False, max_length=100, desc=None):
    return discord.ui.Label(text=label, description=desc, component=discord.ui.TextInput(
        placeholder=ph, default=default or None, required=required, max_length=max_length,
        style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short))


def select_label(label, options, min_v=1, max_v=1, desc=None, required=True):
    return discord.ui.Label(text=label, description=desc, component=discord.ui.Select(
        options=options, min_values=min_v if required else 0, max_values=max_v, required=required))


def entry_embed(e, member=None) -> discord.Embed:
    d = json.loads(e["data"])
    ico, st, colour = STATUS.get(e["status"], ("•", e["status"], BLUE))
    if e["kind"] == "setup":
        em = emb("📋 California State Roleplay — User Setup", None, colour)
        em.add_field(name="Discord", value=f"<@{e['user_id']}>" + (f" (`{d.get('discord')}`)" if d.get("discord") else ""), inline=False)
        em.add_field(name="First & Last Name", value=d.get("name") or "—")
        em.add_field(name="Xbox Gamertag", value=d.get("gamertag") or "—")
        em.add_field(name="Department", value=dept_label(d.get("depts", [])) if d.get("depts") else (d.get("dept_text") or "—"), inline=False)
        if d.get("notes"):
            em.add_field(name="Notes", value=d["notes"][:1024], inline=False)
    else:
        em = emb("🔁 Department Transfer", None, colour)
        em.add_field(name="Member", value=f"<@{e['user_id']}>" + (f" (`{d.get('discord')}`)" if d.get("discord") else ""), inline=False)
        em.add_field(name="From", value=dept_label([d["from"]]) if d.get("from") in DEPTS else (d.get("from_text") or "—"))
        em.add_field(name="To", value=dept_label([d["to"]]) if d.get("to") in DEPTS else (d.get("to_text") or "—"))
        em.add_field(name="30+ days in current department?", value={"yes": "✅ Yes", "no": "❌ No"}.get(d.get("days30"), d.get("days30") or "—"))
        em.add_field(name="Aware of the new department's policies?", value={"yes": "✅ Yes", "no": "❌ No"}.get(d.get("policies"), d.get("policies") or "—"))
        em.add_field(name="Why transfer?", value=(d.get("reason") or "—")[:1024], inline=False)
    status = f"{ico} **{st}**"
    if e["staff_id"] and e["status"] in ("approved", "denied"):
        status += f" by <@{e['staff_id']}>"
    if e["note"]:
        status += f"\n> {e['note'][:400]}"
    if d.get("done"):
        status += "\n" + "\n".join(f"• {x}" for x in d["done"])[:600]
    em.add_field(name="Status", value=status, inline=False)
    if member is not None:
        em.set_thumbnail(url=member.display_avatar.url)
    em.set_footer(text=f"#{e['id']}")
    return em


# ═════════════════════ player forms ═════════════════════
class SetupModal(discord.ui.Modal, title="📋 CSRP User Setup"):
    def __init__(self, cog, prev=None):
        super().__init__(timeout=900)
        self.cog = cog
        p = prev or {}
        self.name = text_label("First & Last Name (your character)", "e.g. Julian Vance", p.get("name"), max_length=64)
        self.gt = text_label("Xbox Gamertag", "e.g. Holyendkiller", p.get("gamertag"), max_length=64)
        self.dept = select_label("Department", dept_options(p.get("depts", ())), 1, 3,
                                 desc="Pick your main department — add a second if you're part-time somewhere else.")
        self.notes = text_label("Anything else? (optional)", "e.g. primary Civ, part-time SAST", None, required=False,
                                paragraph=True, max_length=300)
        for i in (self.name, self.gt, self.dept, self.notes):
            self.add_item(i)

    async def on_submit(self, inter):
        try:
            await self.cog.submit(inter, "setup", {"discord": inter.user.name, "name": self.name.component.value.strip(),
                                                   "gamertag": self.gt.component.value.strip(), "depts": list(self.dept.component.values),
                                                   "notes": (self.notes.component.value or "").strip() or None})
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class TransferModal(discord.ui.Modal, title="🔁 Department Transfer"):
    def __init__(self, cog):
        super().__init__(timeout=900)
        self.cog = cog
        self.frm = select_label("Which department are you transferring FROM?", dept_options())
        self.to = select_label("Which department do you want to transfer TO?", dept_options())
        self.days = select_label("Been in your current department 30+ days?", yn_options())
        self.pol = select_label("Aware of the new department's policies?", yn_options())
        self.reason = text_label("Why would you like to transfer?", "1–2 sentences", None, paragraph=True, max_length=600)
        for i in (self.frm, self.to, self.days, self.pol, self.reason):
            self.add_item(i)

    async def on_submit(self, inter):
        try:
            frm, to = self.frm.component.values[0], self.to.component.values[0]
            if frm == to:
                raise EconError("The FROM and TO departments are the same.")
            await self.cog.submit(inter, "transfer", {"discord": inter.user.name, "from": frm, "to": to,
                                                      "days30": self.days.component.values[0], "policies": self.pol.component.values[0],
                                                      "reason": self.reason.component.value.strip()})
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class LookupModal(discord.ui.Modal, title="🔎 Look up a member"):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.pick = discord.ui.Label(text="Pick a member", description="Start typing to search everyone in the server.",
                                     component=discord.ui.UserSelect(placeholder="Search members…", min_values=0, max_values=1,
                                                                     required=False))
        self.who = text_label("…or type a name", "Character name, gamertag, username or ID — part of it is fine",
                              required=False, max_length=100,
                              desc="Use this for people who left the server or to search by character / gamertag.")
        self.add_item(self.pick)
        self.add_item(self.who)

    async def on_submit(self, inter):
        try:
            picked = self.pick.component.values
            raw = str(picked[0].id) if picked else (self.who.component.value or "").strip()
            if not raw:
                raise EconError("Pick a member or type a name.")
            await self.cog.lookup(inter, raw)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class EditCardModal(discord.ui.Modal, title="✏️ Edit form card"):
    def __init__(self, cog, kind):
        super().__init__(timeout=900)
        self.cog, self.kind = cog, kind
        t = cog.card_text(kind)
        self.t_title = text_label("Title", None, t["title"], required=False, max_length=150, desc="Blank = back to the default.")
        self.t_intro = text_label("Intro", None, t["intro"], required=False, paragraph=True, max_length=1500,
                                  desc="Markdown works. Blank = back to the default.")
        self.t_asks = text_label("What the form asks", None, t["asks"], required=False, paragraph=True, max_length=1500,
                                 desc="Markdown works. Blank = back to the default.")
        for i in (self.t_title, self.t_intro, self.t_asks):
            self.add_item(i)

    async def on_submit(self, inter):
        if not is_admin(inter.user, await cfg(inter.guild_id), PERM):
            return await reply(inter, embed=emb("❌ Staff only.", None, RED), ephemeral=True)
        new = {k: (getattr(self, "t_" + k).component.value or "").strip() for k in ("title", "intro", "asks")}
        conf = self.cog.conf(self.kind)
        conf["text"] = {k: v for k, v in new.items() if v and v != CARD_TEXT[self.kind][k]}
        self.cog.bot.state.save()
        await inter.response.defer(ephemeral=True)
        await self.cog.sync()
        await inter.followup.send(embed=emb("✅ Card updated", None, GREEN), ephemeral=True)
        await log(self.cog.bot, inter.guild_id, emb(f"✏️ {'User setup' if self.kind == 'setup' else 'Transfer'} card edited",
                                                    f"by {inter.user.mention}", BLUE))


class Card(discord.ui.LayoutView):
    def __init__(self, cog, kind, body=None):
        super().__init__(timeout=None)
        self.cog, self.kind = cog, kind
        if kind == "setup":
            main = discord.ui.Button(label="Fill out user setup", emoji="📝", style=discord.ButtonStyle.success, custom_id="form:setup:new")
        else:
            main = discord.ui.Button(label="Request a transfer", emoji="📝", style=discord.ButtonStyle.success, custom_id="form:transfer:new")
        pend = discord.ui.Button(label="Pending (staff)", emoji="📥", style=discord.ButtonStyle.secondary, custom_id=f"form:{kind}:pending")
        look = discord.ui.Button(label="Look up (staff)", emoji="🔎", style=discord.ButtonStyle.secondary, custom_id=f"form:{kind}:lookup")
        edit = discord.ui.Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id=f"form:{kind}:edit")
        for b in (main, pend, look, edit):
            b.callback = self.on_button
        self.add_item(discord.ui.Container(
            *(body or [discord.ui.TextDisplay("## 📋 Form")]),
            discord.ui.Separator(),
            discord.ui.ActionRow(main, pend, look, edit),
            accent_colour=discord.Colour.blurple() if kind == "setup" else discord.Colour.orange()))

    async def on_button(self, inter):
        act = inter.data["custom_id"].split(":")[2]
        try:
            if act == "new":
                if self.kind == "setup":
                    prev = await self.cog.latest(inter.guild_id, "setup", inter.user.id)
                    if prev and prev["status"] == "pending":
                        raise EconError("Your user setup is already waiting for staff — hang tight!")
                    await inter.response.send_modal(SetupModal(self.cog, json.loads(prev["data"]) if prev else None))
                else:
                    if await q1("SELECT 1 FROM form_entries WHERE guild_id=%s AND kind='transfer' AND user_id=%s AND status='pending'",
                                inter.guild_id, inter.user.id):
                        raise EconError("You already have a transfer request waiting for staff.")
                    await inter.response.send_modal(TransferModal(self.cog))
                return
            if not is_admin(inter.user, await cfg(inter.guild_id), PERM):
                raise EconError("Staff only.")
            if act == "lookup":
                await inter.response.send_modal(LookupModal(self.cog))
            elif act == "pending":
                await self.cog.pending(inter, self.kind)
            elif act == "edit":
                await inter.response.send_modal(EditCardModal(self.cog, self.kind))
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


# ═════════════════════ staff decisions ═════════════════════
class Decide(discord.ui.DynamicItem[discord.ui.Button], template=r"formd:(?P<act>ok|no):(?P<eid>\d+)"):
    def __init__(self, act: str, eid: int):
        super().__init__(discord.ui.Button(label="Approve" if act == "ok" else "Deny", emoji="✅" if act == "ok" else "❌",
                                           style=discord.ButtonStyle.success if act == "ok" else discord.ButtonStyle.danger,
                                           custom_id=f"formd:{act}:{eid}"))
        self.act, self.eid = act, eid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["act"], int(match["eid"]))

    async def callback(self, inter):
        cog = inter.client.get_cog("Forms")
        try:
            if not is_admin(inter.user, await cfg(inter.guild_id), PERM):
                raise EconError("Only staff can review this.")
            e = await q1("SELECT * FROM form_entries WHERE id=%s AND guild_id=%s", self.eid, inter.guild_id)
            if not e or e["status"] != "pending":
                raise EconError("This was already reviewed.")
            if self.act == "no":
                await inter.response.send_modal(DenyModal(cog, e))
            elif e["kind"] == "setup":
                await inter.response.send_modal(ApproveSetupModal(cog, e, inter.guild.get_member(e["user_id"])))
            else:
                await inter.response.send_modal(ApproveTransferModal(cog, e))
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


def decide_view(eid):
    v = discord.ui.View(timeout=None)
    v.add_item(Decide("ok", eid))
    v.add_item(Decide("no", eid))
    return v


class ApproveSetupModal(discord.ui.Modal, title="✅ Approve user setup"):
    def __init__(self, cog, e, member):
        super().__init__(timeout=600)
        self.cog, self.e = cog, e
        d = json.loads(e["data"])
        depts = d.get("depts") or []
        prefix = DEPTS[depts[0]][4] if depts else "{CIV}"
        self.nick = text_label("Nickname (blank = don't change)", None, f"{prefix} {d.get('name', '')}".strip()[:32],
                               required=False, max_length=32)
        self.depts = select_label("Department roles to give", dept_options(depts), 0, len(DEPTS), required=False,
                                  desc="Gives the department tag + header roles. Ranks are still up to the department.")
        self.note = text_label("Note to the member (optional)", None, None, required=False, paragraph=True, max_length=300)
        for i in (self.nick, self.depts, self.note):
            self.add_item(i)

    async def on_submit(self, inter):
        try:
            await self.cog.approve_setup(inter, self.e["id"], (self.nick.component.value or "").strip(),
                                         list(self.depts.component.values), (self.note.component.value or "").strip() or None)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class ApproveTransferModal(discord.ui.Modal, title="✅ Approve transfer"):
    def __init__(self, cog, e):
        super().__init__(timeout=600)
        self.cog, self.e = cog, e
        self.swap = select_label("Swap their department roles now?", [
            discord.SelectOption(label="Yes — remove FROM roles, give TO roles", value="yes", emoji="🔁", default=True),
            discord.SelectOption(label="No — I'll do roles myself", value="no", emoji="✋")])
        self.note = text_label("Note to the member (optional)", None, None, required=False, paragraph=True, max_length=300)
        self.add_item(self.swap)
        self.add_item(self.note)

    async def on_submit(self, inter):
        try:
            await self.cog.approve_transfer(inter, self.e["id"], self.swap.component.values[0] == "yes",
                                            (self.note.component.value or "").strip() or None)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class DenyModal(discord.ui.Modal, title="❌ Deny"):
    def __init__(self, cog, e):
        super().__init__(timeout=600)
        self.cog, self.e = cog, e
        self.note = text_label("Reason (sent to the member)", "e.g. please use your full character name", None,
                               paragraph=True, max_length=300)
        self.add_item(self.note)

    async def on_submit(self, inter):
        try:
            await self.cog.deny(inter, self.e["id"], self.note.component.value.strip())
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


# ═════════════════════ cog ═════════════════════
class Forms(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        for kind in CHANNELS:
            bot.add_view(Card(self, kind))
        bot.add_dynamic_items(Decide)
        self.cards.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)

    def cog_unload(self):
        self.cards.cancel()
        self.bot.remove_dynamic_items(Decide)

    def conf(self, kind) -> dict:
        return self.bot.state.settings.setdefault("forms", {}).setdefault(kind, {})

    async def latest(self, gid, kind, uid):
        return await q1("SELECT * FROM form_entries WHERE guild_id=%s AND kind=%s AND user_id=%s ORDER BY id DESC LIMIT 1", gid, kind, uid)

    # ───── submit ─────
    async def submit(self, inter, kind, data):
        gid, uid = inter.guild_id, inter.user.id
        if kind == "setup" and (not data["name"] or len(data["name"].split()) < 2):
            raise EconError("Please enter your character's **first and last** name.")
        _, eid = await ex("INSERT INTO form_entries (guild_id,kind,user_id,data) VALUES (%s,%s,%s,%s)", gid, kind, uid, json.dumps(data))
        e = await q1("SELECT * FROM form_entries WHERE id=%s", eid)
        ch = inter.guild.get_channel(CHANNELS[kind])
        staff = await self.staff_role(inter.guild)
        if ch:
            msg = await ch.send(content=f"{staff.mention} — new {'user setup' if kind == 'setup' else 'transfer request'} from {inter.user.mention}"
                                if staff else None, embed=entry_embed(e, inter.user), view=decide_view(eid),
                                allowed_mentions=discord.AllowedMentions(roles=True, users=False))
            await ex("UPDATE form_entries SET msg_id=%s WHERE id=%s", msg.id, eid)
        await reply(inter, embed=emb("📨 Sent!", "Staff will review it and you'll get a DM when it's done.", GREEN), ephemeral=True)
        await log(self.bot, gid, emb(f"📋 New {'user setup' if kind == 'setup' else 'department transfer'}", f"#{eid} from {inter.user.mention}", BLUE))
        await self.repost_card(inter.guild, kind)

    async def staff_role(self, guild):
        return guild.get_role(1283787066349715480)   # 『✦』 CSRP Staff

    # ───── decisions ─────
    async def _finish(self, inter, eid, status, note, extra=None):
        e = await q1("SELECT * FROM form_entries WHERE id=%s", eid)
        data = json.loads(e["data"])
        if extra:
            data["done"] = extra
        n, _ = await ex("UPDATE form_entries SET status=%s, staff_id=%s, note=%s, data=%s, decided_at=UTC_TIMESTAMP() "
                        "WHERE id=%s AND status='pending'", status, inter.user.id, note, json.dumps(data), eid)
        if not n:
            raise EconError("This was already reviewed.")
        e = await q1("SELECT * FROM form_entries WHERE id=%s", eid)
        member = inter.guild.get_member(e["user_id"])
        ch = inter.guild.get_channel(CHANNELS[e["kind"]])
        if ch and e["msg_id"]:
            try:
                await ch.get_partial_message(e["msg_id"]).edit(content=None, embed=entry_embed(e, member), view=None)
            except discord.HTTPException:
                pass
        what = "user setup" if e["kind"] == "setup" else "department transfer request"
        txt = (f"✅ Your {what} was **approved**." if status == "approved" else f"❌ Your {what} was **denied**.") + \
            (f"\n> {note}" if note else "") + ("\nYou can fill it out again in <#%d>." % CHANNELS[e["kind"]] if status == "denied" else "")
        await core.dm(self.bot, e["user_id"], emb("📋 CSRP " + ("User Setup" if e["kind"] == "setup" else "Department Transfer"), txt,
                                                  GREEN if status == "approved" else RED))
        await log(self.bot, inter.guild_id, emb(f"📋 {what.capitalize()} {status}", f"#{eid} <@{e['user_id']}> by {inter.user.mention}"
                                                + ("\n" + "\n".join(extra) if extra else ""), GREEN if status == "approved" else RED))
        return e

    async def approve_setup(self, inter, eid, nick, depts, note):
        e = await q1("SELECT * FROM form_entries WHERE id=%s AND guild_id=%s", eid, inter.guild_id)
        if not e or e["status"] != "pending":
            raise EconError("This was already reviewed.")
        member = inter.guild.get_member(e["user_id"])
        if not member:
            raise EconError("That member isn't in the server any more — deny it instead.")
        await inter.response.defer(ephemeral=True)
        done = []
        add = [r for rid in MEMBER_ROLES + [x for c in depts for x in DEPTS[c][2:4]] if (r := inter.guild.get_role(rid)) and r not in member.roles]
        try:
            if add:
                await member.add_roles(*add, reason=f"User setup #{eid} approved by {inter.user}")
                done.append("Roles given: " + ", ".join(r.name for r in add))
            need = inter.guild.get_role(NEEDS_SETUP)
            if need and need in member.roles:
                await member.remove_roles(need, reason=f"User setup #{eid} approved")
                done.append("Removed: Needs User Setup")
        except discord.Forbidden:
            done.append("⚠️ Couldn't change roles (bot lacks permission)")
        if nick and nick != member.nick:
            try:
                await member.edit(nick=nick[:32], reason=f"User setup #{eid} approved")
                done.append(f"Nickname → {nick[:32]}")
            except discord.Forbidden:
                done.append("⚠️ Couldn't set nickname (bot lacks permission, or they're above the bot)")
        await self._finish(inter, eid, "approved", note, done)
        await inter.followup.send(embed=emb(f"✅ Approved — {member.display_name}", "\n".join(f"• {x}" for x in done) or "No changes needed.", GREEN),
                                  ephemeral=True)

    async def approve_transfer(self, inter, eid, swap, note):
        e = await q1("SELECT * FROM form_entries WHERE id=%s AND guild_id=%s", eid, inter.guild_id)
        if not e or e["status"] != "pending":
            raise EconError("This was already reviewed.")
        d = json.loads(e["data"])
        member = inter.guild.get_member(e["user_id"])
        await inter.response.defer(ephemeral=True)
        done = []
        if swap and member and d.get("from") in DEPTS and d.get("to") in DEPTS:
            rm = [r for rid in DEPTS[d["from"]][2:4] if (r := inter.guild.get_role(rid)) and r in member.roles]
            add = [r for rid in DEPTS[d["to"]][2:4] if (r := inter.guild.get_role(rid)) and r not in member.roles]
            try:
                if rm:
                    await member.remove_roles(*rm, reason=f"Transfer #{eid} approved by {inter.user}")
                    done.append("Removed: " + ", ".join(r.name for r in rm))
                if add:
                    await member.add_roles(*add, reason=f"Transfer #{eid} approved by {inter.user}")
                    done.append("Given: " + ", ".join(r.name for r in add))
                done.append("Rank roles in the new department are still up to the department.")
            except discord.Forbidden:
                done.append("⚠️ Couldn't change roles (bot lacks permission)")
        elif swap and not member:
            done.append("⚠️ Member left the server — no roles changed")
        await self._finish(inter, eid, "approved", note, done)
        await inter.followup.send(embed=emb("✅ Transfer approved", "\n".join(f"• {x}" for x in done) or "Roles left for you to update.", GREEN),
                                  ephemeral=True)

    async def deny(self, inter, eid, note):
        await inter.response.defer(ephemeral=True)
        await self._finish(inter, eid, "denied", note)
        await inter.followup.send(embed=emb("❌ Denied", "The member has been told why.", GOLD), ephemeral=True)

    # ───── staff tools ─────
    async def pending(self, inter, kind):
        rows = await q("SELECT * FROM form_entries WHERE guild_id=%s AND kind=%s AND status='pending' ORDER BY id LIMIT 30", inter.guild_id, kind)
        lines = [f"`#{r['id']}` <@{r['user_id']}> — <t:{int(r['created_at'].replace(tzinfo=dt.timezone.utc).timestamp())}:R>"
                 + (f" · [open](https://discord.com/channels/{inter.guild_id}/{CHANNELS[kind]}/{r['msg_id']})" if r["msg_id"] else "")
                 for r in rows]
        need = inter.guild.get_role(NEEDS_SETUP)
        extra = ""
        if kind == "setup" and need:
            waiting = [m for m in need.members if not await q1("SELECT 1 FROM form_entries WHERE guild_id=%s AND kind='setup' AND user_id=%s "
                                                               "AND status IN ('pending','approved')", inter.guild_id, m.id)]
            if waiting:
                extra = "\n\n**Have the Needs User Setup role but haven't filled it out:**\n" + " ".join(m.mention for m in waiting[:40])
        await reply(inter, embed=emb(f"📥 Pending {'user setups' if kind == 'setup' else 'transfers'} ({len(rows)})",
                                     ("\n".join(lines) or "Nothing waiting.") + extra, BLUE), ephemeral=True)

    async def lookup(self, inter, raw):
        g = inter.guild
        m = re.search(r"\d{15,21}", raw)
        uid = int(m.group()) if m else None
        if not uid:
            n = raw.lstrip("@").lower()
            exact, part = {}, {}   # user_id → label

            def check(uid_, label, *names):
                names = [x.lower() for x in names if x]
                if n in names:
                    exact.setdefault(uid_, label)
                elif any(n in x for x in names):
                    part.setdefault(uid_, label)

            for x in g.members:
                check(x.id, x.display_name, x.name, x.global_name, x.display_name)
            for r in await q("SELECT user_id, data FROM form_entries WHERE guild_id=%s AND kind='setup' ORDER BY id DESC", inter.guild_id):
                d = json.loads(r["data"])
                check(r["user_id"], f"{d.get('name') or '?'} ({d.get('gamertag') or 'no gamertag'})",
                      d.get("name"), d.get("gamertag"), d.get("discord"))
            found = exact or part
            if len(found) == 1:
                uid = next(iter(found))
            elif found:
                lines = [f"<@{u}> — {lbl}" for u, lbl in list(found.items())[:25]]
                more = f"\n…and {len(found) - 25} more — type more of the name." if len(found) > 25 else ""
                return await reply(inter, embed=emb(f"🔎 {len(found)} matches for “{raw}”",
                                                    "\n".join(lines) + more + "\n\nLook up again and pick one.", BLUE), ephemeral=True)
        if not uid:
            raise EconError(f"Couldn't find **{raw}** (tried members, character names and gamertags).")
        member = g.get_member(uid)
        rows = await q("SELECT * FROM form_entries WHERE guild_id=%s AND user_id=%s ORDER BY id DESC LIMIT 10", inter.guild_id, uid)
        if not rows:
            raise EconError(f"<@{uid}> hasn't filled out a setup or transfer.")
        setup = next((r for r in rows if r["kind"] == "setup"), None)
        transfer = next((r for r in rows if r["kind"] == "transfer"), None)
        embeds = [entry_embed(r, member) for r in (setup, transfer) if r]
        hist = [f"{STATUS.get(r['status'], ('•',))[0]} `#{r['id']}` {'Setup' if r['kind'] == 'setup' else 'Transfer'} · "
                f"<t:{int(r['created_at'].replace(tzinfo=dt.timezone.utc).timestamp())}:d>" for r in rows]
        embeds.append(emb(f"🗂️ History for {member.display_name if member else uid}", "\n".join(hist), BLUE))
        await inter.response.send_message(embeds=embeds, ephemeral=True)

    # ───── cards ─────
    def card_text(self, kind) -> dict:
        return {**CARD_TEXT[kind], **(self.conf(kind).get("text") or {})}

    def card_body(self, kind, stats):
        t = self.card_text(kind)
        return [discord.ui.TextDisplay(f"## {t['title']}\n{t['intro']}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(t["asks"]),
                discord.ui.TextDisplay(f"-# {stats.get('pending', 0)} waiting for review · {stats.get('approved', 0)} approved")]

    async def stats(self, gid, kind):
        return {r["status"]: r["n"] for r in await q("SELECT status, COUNT(*) n FROM form_entries WHERE guild_id=%s AND kind=%s GROUP BY status", gid, kind)}

    async def repost_card(self, guild, kind):
        """Keep the card as the last message in the channel."""
        ch = guild.get_channel(CHANNELS[kind])
        if not ch:
            return
        conf = self.conf(kind)
        if conf.get("card_id"):
            try:
                await ch.get_partial_message(int(conf["card_id"])).delete()
            except discord.HTTPException:
                pass
        msg = await ch.send(view=Card(self, kind, self.card_body(kind, await self.stats(guild.id, kind))))
        conf["card_id"] = msg.id
        self.bot.state.save()

    async def sync(self):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            for kind in CHANNELS:
                ch = g.get_channel(CHANNELS[kind])
                if not ch:
                    continue
                conf = self.conf(kind)
                view = Card(self, kind, self.card_body(kind, await self.stats(gid, kind)))
                last = [m async for m in ch.history(limit=1)]
                if conf.get("card_id") and last and last[0].id == int(conf["card_id"]):
                    try:
                        await ch.get_partial_message(int(conf["card_id"])).edit(view=view)
                        continue
                    except discord.NotFound:
                        pass
                await self.repost_card(g, kind)   # someone posted below the card (or it's missing) → move it to the bottom

    @tasks.loop(minutes=5)
    async def cards(self):
        try:
            await self.sync()
        except Exception:
            logger.exception("form cards refresh failed")

    @cards.before_loop
    async def _w(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Forms(bot))
