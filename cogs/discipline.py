"""Discipline: warnings, 3-strike system and timed suspensions (staff permission "discipline").

/discipline warn | strike | suspend | unsuspend | unstrike | unban | record
• Warnings      — logged + [W] Warning role. No automatic action.
• Strikes       — never expire. The member always holds exactly the role for their count:
                  1 → [✗] Strike I, 2 → [✗] Strike II, 3 → [✗] Final Strike + DM + ban.
• Suspensions   — timed (e.g. 3d, 1w). Gives [✗] Suspended, takes their other roles away (saved, given back at the end)
                  and a Discord timeout (max 28 days). Ends automatically, or early with /discipline unsuspend.
Leaving and rejoining doesn't clear anything: strike/warning/suspended roles are put back on join.
Every action DMs the member and goes to the bot log. Full history shows in /discipline record and in ticket player info.
"""
import datetime as dt
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, set_cfg, emb, log, reply, is_admin, GREEN, RED, BLUE, GOLD

logger = logging.getLogger("economy.discipline")
PERM = "discipline"
PANEL_CHANNEL = 1571310411129561200   # 🛠️・staff-panel
WARNING_ROLE = 1032587295238398019                                               # [W] Warning
STRIKE_ROLES = [1032587294932209675, 1032587294911254547, 1032587294911254546]   # Strike I, Strike II, Final Strike
MAX_STRIKES = 3
SUSPENDED_NAME = "[✗] Suspended"
TIMEOUT_MAX = dt.timedelta(days=28) - dt.timedelta(minutes=5)
KINDS = {"warning": ("⚠️", "Warning", GOLD), "strike": ("❗", "Strike", RED), "suspension": ("⏸️", "Suspension", RED),
         "ban": ("🔨", "Ban", RED)}
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS discipline (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      kind VARCHAR(12) NOT NULL,
      reason VARCHAR(500) NOT NULL,
      staff_id BIGINT UNSIGNED NOT NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      ends_at DATETIME NULL,
      active TINYINT NOT NULL DEFAULT 1,
      saved_roles TEXT NULL,
      removed_by BIGINT UNSIGNED NULL,
      removed_at DATETIME NULL,
      removed_reason VARCHAR(300) NULL,
      INDEX ix_disc_user (guild_id, user_id),
      INDEX ix_disc_active (guild_id, kind, active)
    ) ENGINE=InnoDB""",
]


def now():
    return dt.datetime.utcnow().replace(microsecond=0)


def unix(d):
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def parse_duration(raw: str) -> dt.timedelta:
    total = dt.timedelta()
    parts = re.findall(r"(\d+)\s*(w|d|h|m)", (raw or "").lower())
    if not parts or re.sub(r"[\d\swdhm]", "", raw.lower()):
        raise EconError("Duration looks like `3d`, `1w`, `12h` or `1w 2d`.")
    for n, u in parts:
        total += {"w": dt.timedelta(weeks=int(n)), "d": dt.timedelta(days=int(n)), "h": dt.timedelta(hours=int(n)),
                  "m": dt.timedelta(minutes=int(n))}[u]
    if total < dt.timedelta(minutes=5) or total > dt.timedelta(days=365):
        raise EconError("Suspensions can be between 5 minutes and 365 days.")
    return total


def human(td: dt.timedelta) -> str:
    s = int(td.total_seconds())
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return " ".join(x for x in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m and not d else "") if x) or "0m"


async def summary(gid: int, uid: int) -> dict:
    """Used by the ticket player card too."""
    r = await q1("SELECT SUM(kind='strike' AND active=1) strikes, SUM(kind='warning' AND active=1) warnings, "
                 "SUM(kind='suspension') suspensions, COUNT(*) total FROM discipline WHERE guild_id=%s AND user_id=%s", gid, uid)
    sus = await q1("SELECT ends_at, reason FROM discipline WHERE guild_id=%s AND user_id=%s AND kind='suspension' AND active=1 "
                   "ORDER BY id DESC LIMIT 1", gid, uid)
    last = await q("SELECT kind, reason, created_at, active FROM discipline WHERE guild_id=%s AND user_id=%s ORDER BY id DESC LIMIT 5", gid, uid)
    return {"strikes": int(r["strikes"] or 0), "warnings": int(r["warnings"] or 0), "suspensions": int(r["suspensions"] or 0),
            "total": r["total"], "suspended": sus, "last": last}


def _user_label(title="Member", desc="Start typing to search"):
    return discord.ui.Label(text=title, description=desc,
                            component=discord.ui.UserSelect(placeholder="Search members…", min_values=1, max_values=1))


def _text_label(title, ph=None, desc=None, required=True, long=False, max_length=500):
    return discord.ui.Label(text=title, description=desc, component=discord.ui.TextInput(
        placeholder=ph, required=required, max_length=max_length,
        style=discord.TextStyle.paragraph if long else discord.TextStyle.short))


class ActionModal(discord.ui.Modal):
    """One modal for every panel action — fields depend on `act`."""
    TITLES = {"warn": "⚠️ Warn a member", "strike": "❗ Strike a member (3 = ban)", "suspend": "⏸️ Suspend a member",
              "unsuspend": "▶️ End a suspension", "unstrike": "↩️ Remove a strike / warning", "unban": "🔓 Unban",
              "record": "🔎 Discipline record"}

    def __init__(self, cog, act):
        super().__init__(title=self.TITLES[act], timeout=600)
        self.cog, self.act = cog, act
        self.user = self.reason = self.length = self.uid = self.what = None
        if act in ("unban", "record"):
            if act == "record":
                self.user = discord.ui.Label(text="Member", description="Start typing to search",
                                             component=discord.ui.UserSelect(min_values=0, max_values=1, required=False))
                self.add_item(self.user)
            self.uid = _text_label("Discord ID", "e.g. 344560012997165057", required=act == "unban", max_length=21,
                                   desc="Banned / left members can't be picked — paste their ID." if act == "unban" else "…or their ID if they left")
            self.add_item(self.uid)
        else:
            self.user = _user_label()
            self.add_item(self.user)
        if act == "suspend":
            self.length = _text_label("How long", "3d · 1w · 12h · 1w 2d", max_length=20)
            self.add_item(self.length)
        if act == "unstrike":
            self.what = discord.ui.Label(text="Remove", component=discord.ui.Select(options=[
                discord.SelectOption(label="Most recent strike", value="strike", emoji="❗", default=True),
                discord.SelectOption(label="Most recent warning", value="warning", emoji="⚠️")]))
            self.add_item(self.what)
        if act != "record":
            self.reason = _text_label("Reason", "Sent to the member" if act in ("warn", "strike", "suspend") else None,
                                      long=True, required=act not in ("unsuspend",))
            self.add_item(self.reason)

    async def on_submit(self, inter):
        cog, act = self.cog, self.act
        reason = ((self.reason.component.value if self.reason else "") or "").strip()
        picked = self.user.component.values if self.user else []
        member = picked[0] if picked else None
        if member is not None and not isinstance(member, discord.Member):
            member = inter.guild.get_member(member.id)

        async def go():
            if act in ("warn", "strike", "suspend", "unsuspend", "unstrike") and member is None:
                raise EconError("That member isn't in the server.")
            if act == "warn":
                return await cog.do_warn(inter, member, reason)
            if act == "strike":
                if member.top_role >= inter.guild.me.top_role:
                    raise EconError(f"{member.mention}'s highest role is above mine, so I can't manage their strikes.")
                return await cog.do_strike(inter, member, reason)
            if act == "suspend":
                return await cog.do_suspend(inter, member, parse_duration(self.length.component.value), reason)
            if act == "unsuspend":
                return await cog.do_unsuspend(inter, member, reason or "Ended early")
            if act == "unstrike":
                return await cog.do_unstrike(inter, member, self.what.component.values[0], reason)
            if act == "unban":
                return await cog.do_unban(inter, (self.uid.component.value or "").strip(), reason, True)
            uid = member.id if member else int(v) if (v := (self.uid.component.value or "").strip()).isdigit() else None
            if not uid:
                raise EconError("Pick a member or give a user ID.")
            await inter.response.send_message(embed=await cog.record_embed(inter.guild, uid), ephemeral=True)
        await cog.run(inter, go)


class Panel(discord.ui.LayoutView):
    BUTTONS = [("warn", "Warn", "⚠️", discord.ButtonStyle.secondary), ("strike", "Strike", "❗", discord.ButtonStyle.danger),
               ("suspend", "Suspend", "⏸️", discord.ButtonStyle.danger), ("unsuspend", "End suspension", "▶️", discord.ButtonStyle.success),
               ("unstrike", "Remove strike", "↩️", discord.ButtonStyle.secondary), ("unban", "Unban", "🔓", discord.ButtonStyle.secondary),
               ("record", "Record", "🔎", discord.ButtonStyle.primary)]

    def __init__(self, cog, body=None):
        super().__init__(timeout=None)
        self.cog = cog
        btns = []
        for act, label, emoji, style in self.BUTTONS:
            b = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"disc:{act}")
            b.callback = self.on_button
            btns.append(b)
        self.add_item(discord.ui.Container(
            *(body or [discord.ui.TextDisplay("## ⚖️ Strikes & suspensions")]),
            discord.ui.Separator(),
            discord.ui.ActionRow(*btns[:4]), discord.ui.ActionRow(*btns[4:]),
            accent_colour=discord.Colour.red()))

    async def on_button(self, inter):
        try:
            await self.cog.need(inter)
            await inter.response.send_modal(ActionModal(self.cog, inter.data["custom_id"].split(":")[1]))
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class Discipline(commands.Cog):
    group = app_commands.Group(name="discipline", description="Warnings, strikes & suspensions (staff)", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        bot.add_view(Panel(self))
        self.expire.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)

    def cog_unload(self):
        self.expire.cancel()

    # ───── helpers ─────
    async def need(self, inter):
        c = await cfg(inter.guild_id)
        if not is_admin(inter.user, c, PERM):
            raise EconError("You need the **Strikes & suspensions** staff permission. Staff ranks are managed on the web panel.")
        return c

    async def suspended_role(self, guild: discord.Guild) -> discord.Role | None:
        conf = self.bot.state.settings.setdefault("discipline", {})
        role = guild.get_role(int(conf.get("suspended_role") or 0)) or discord.utils.get(guild.roles, name=SUSPENDED_NAME)
        if not role:
            try:
                role = await guild.create_role(name=SUSPENDED_NAME, colour=discord.Colour.dark_grey(), permissions=discord.Permissions.none(),
                                               reason="Discipline: suspended members")
            except discord.Forbidden:
                return None
        if conf.get("suspended_role") != role.id:
            conf["suspended_role"] = role.id
            self.bot.state.save()
        return role

    def keep(self, role: discord.Role, guild: discord.Guild) -> bool:
        """Roles a suspension leaves alone."""
        return (role.is_default() or role.managed or role.is_premium_subscriber() or role >= guild.me.top_role
                or role.id in STRIKE_ROLES or role.id == WARNING_ROLE)

    async def sync_roles(self, member: discord.Member, reason: str):
        """Strike/warning/suspended roles must match the database."""
        s = await summary(member.guild.id, member.id)
        g = member.guild
        want = set()
        if s["strikes"]:
            want.add(STRIKE_ROLES[min(s["strikes"], MAX_STRIKES) - 1])
        if s["warnings"]:
            want.add(WARNING_ROLE)
        managed = set(STRIKE_ROLES) | {WARNING_ROLE}
        sus = await self.suspended_role(g)
        if sus:
            managed.add(sus.id)
            if s["suspended"]:
                want.add(sus.id)
        have = {r.id for r in member.roles}
        add = [r for rid in want - have if (r := g.get_role(rid))]
        rm = [r for rid in (managed & have) - want if (r := g.get_role(rid))]
        try:
            if add:
                await member.add_roles(*add, reason=reason)
            if rm:
                await member.remove_roles(*rm, reason=reason)
        except discord.Forbidden:
            logger.warning("can't change discipline roles for %s", member)

    async def notify(self, guild, uid, title, text, colour):
        await core.dm(self.bot, uid, emb(f"{title} — {guild.name}", text, colour))

    async def add(self, guild, uid, kind, reason, staff, ends_at=None, saved=None):
        _, rid = await ex("INSERT INTO discipline (guild_id,user_id,kind,reason,staff_id,ends_at,saved_roles,created_at) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())", guild.id, uid, kind, reason[:500], staff.id, ends_at,
                          json.dumps(saved) if saved is not None else None)
        return rid

    # ───── actions ─────
    async def do_warn(self, inter, member, reason):
        rid = await self.add(inter.guild, member.id, "warning", reason, inter.user)
        await self.sync_roles(member, f"Warning #{rid} by {inter.user}")
        await self.notify(inter.guild, member.id, "⚠️ Warning", f"**Reason:** {reason}\n\nPlease review the server rules.", GOLD)
        return emb(f"⚠️ Warned {member.display_name}", f"{member.mention} · #{rid}\n**Reason:** {reason}", GOLD)

    async def do_strike(self, inter, member, reason):
        g = inter.guild
        rid = await self.add(g, member.id, "strike", reason, inter.user)
        s = await summary(g.id, member.id)
        n = s["strikes"]
        await self.sync_roles(member, f"Strike #{rid} by {inter.user}")
        if n >= MAX_STRIKES:
            await self.notify(g, member.id, "🔨 Final strike — banned",
                              f"**Strike {n}/{MAX_STRIKES}:** {reason}\n\nYou have reached {MAX_STRIKES} strikes and have been banned. "
                              "Contact staff if you would like to appeal.", RED)
            try:
                await g.ban(member, reason=f"3 strikes — strike #{rid} by {inter.user}: {reason}"[:500], delete_message_seconds=0)
                await self.add(g, member.id, "ban", f"Reached {MAX_STRIKES} strikes", inter.user)
                extra = f"\n\n🔨 **Banned** — reached {MAX_STRIKES} strikes."
            except discord.Forbidden:
                extra = "\n\n⚠️ **Reached 3 strikes but I couldn't ban them** — I need the Ban Members permission and a role above theirs."
        else:
            await self.notify(g, member.id, f"❗ Strike {n}/{MAX_STRIKES}",
                              f"**Reason:** {reason}\n\nAt {MAX_STRIKES} strikes you will be banned.", RED)
            extra = ""
        return emb(f"❗ Strike {n}/{MAX_STRIKES} — {member.display_name}", f"{member.mention} · #{rid}\n**Reason:** {reason}{extra}", RED)

    async def do_suspend(self, inter, member, length: dt.timedelta, reason):
        g = inter.guild
        if await q1("SELECT 1 FROM discipline WHERE guild_id=%s AND user_id=%s AND kind='suspension' AND active=1", g.id, member.id):
            raise EconError(f"{member.mention} is already suspended — end it first with `/discipline unsuspend`.")
        if member.top_role >= g.me.top_role:
            raise EconError(f"{member.mention}'s highest role is above mine, so I can't suspend them.")
        await inter.response.defer(ephemeral=True)
        ends = now() + length
        taken = [r for r in member.roles if not self.keep(r, g)]
        rid = await self.add(g, member.id, "suspension", reason, inter.user, ends, [r.id for r in taken])
        notes = []
        try:
            if taken:
                await member.remove_roles(*taken, reason=f"Suspension #{rid} by {inter.user}")
                notes.append(f"Took away {len(taken)} roles (given back when it ends)")
        except discord.Forbidden:
            notes.append("⚠️ Couldn't remove their roles")
        await self.sync_roles(member, f"Suspension #{rid}")
        try:
            await member.timeout(min(length, TIMEOUT_MAX), reason=f"Suspension #{rid}: {reason}"[:500])
            notes.append("Timed out" + (" for 28 days (Discord's max — the role stays until the end)" if length > TIMEOUT_MAX else ""))
        except discord.Forbidden:
            notes.append("⚠️ Couldn't time them out (I need Moderate Members)")
        await self.notify(g, member.id, "⏸️ Suspended", f"**For:** {human(length)} — ends <t:{unix(ends)}:F>\n**Reason:** {reason}\n\n"
                          "Your roles will be given back automatically when it ends.", RED)
        return emb(f"⏸️ Suspended {member.display_name} — {human(length)}",
                   f"{member.mention} · #{rid}\nEnds <t:{unix(ends)}:F> (<t:{unix(ends)}:R>)\n**Reason:** {reason}\n"
                   + "\n".join(f"• {x}" for x in notes), RED)

    async def do_unsuspend(self, inter, member, reason):
        row = await q1("SELECT * FROM discipline WHERE guild_id=%s AND user_id=%s AND kind='suspension' AND active=1", inter.guild_id, member.id)
        if not row:
            raise EconError(f"{member.mention} isn't suspended.")
        await inter.response.defer(ephemeral=True)
        notes = await self.end_suspension(inter.guild, row, inter.user.id, reason)
        return emb(f"▶️ Ended {member.display_name}'s suspension", f"{member.mention} · #{row['id']}\n**Reason:** {reason}\n"
                   + "\n".join(f"• {x}" for x in notes), GREEN)

    async def do_unstrike(self, inter, member, what, reason):
        row = await q1("SELECT id FROM discipline WHERE guild_id=%s AND user_id=%s AND kind=%s AND active=1 ORDER BY id DESC LIMIT 1",
                       inter.guild_id, member.id, what)
        if not row:
            raise EconError(f"{member.mention} has no active {what}s.")
        await ex("UPDATE discipline SET active=0, removed_by=%s, removed_at=UTC_TIMESTAMP(), removed_reason=%s WHERE id=%s",
                 inter.user.id, reason[:300], row["id"])
        await self.sync_roles(member, f"{what} #{row['id']} removed by {inter.user}")
        s = await summary(inter.guild_id, member.id)
        await self.notify(inter.guild, member.id, f"✅ {what.capitalize()} removed", f"**Reason:** {reason}\n"
                          f"You now have {s['strikes']}/{MAX_STRIKES} strikes.", GREEN)
        return emb(f"✅ Removed {what} #{row['id']} — {member.display_name}",
                   f"{member.mention} now has **{s['strikes']}/{MAX_STRIKES}** strikes, {s['warnings']} warnings.\n**Reason:** {reason}", GREEN)

    async def do_unban(self, inter, user_id, reason, clear_strike):
        if not user_id.isdigit():
            raise EconError("Give their numeric Discord ID.")
        uid = int(user_id)
        try:
            await inter.guild.unban(discord.Object(uid), reason=f"by {inter.user}: {reason}"[:500])
        except discord.NotFound:
            raise EconError("That user isn't banned.")
        except discord.Forbidden:
            raise EconError("I need the Ban Members permission.")
        await ex("UPDATE discipline SET active=0, removed_by=%s, removed_at=UTC_TIMESTAMP(), removed_reason=%s "
                 "WHERE guild_id=%s AND user_id=%s AND kind='ban' AND active=1", inter.user.id, reason[:300], inter.guild_id, uid)
        if clear_strike:
            await ex("UPDATE discipline SET active=0, removed_by=%s, removed_at=UTC_TIMESTAMP(), removed_reason=%s "
                     "WHERE guild_id=%s AND user_id=%s AND kind='strike' AND active=1 ORDER BY id DESC LIMIT 1",
                     inter.user.id, f"Unbanned: {reason}"[:300], inter.guild_id, uid)
        s = await summary(inter.guild_id, uid)
        return emb("🔓 Unbanned", f"<@{uid}> · `{uid}` — now on **{s['strikes']}/{MAX_STRIKES}** strikes "
                   "(roles are put back when they rejoin).\n**Reason:** " + reason, GREEN)

    # ───── panel card ─────
    async def panel_body(self, guild):
        sus = await q("SELECT user_id, ends_at, reason FROM discipline WHERE guild_id=%s AND kind='suspension' AND active=1 ORDER BY ends_at", guild.id)
        st = await q("SELECT user_id, COUNT(*) n FROM discipline WHERE guild_id=%s AND kind='strike' AND active=1 GROUP BY user_id "
                     "ORDER BY n DESC, MAX(id) DESC", guild.id)
        recent = await q("SELECT * FROM discipline WHERE guild_id=%s ORDER BY id DESC LIMIT 6", guild.id)
        tot = await q1("SELECT SUM(kind='warning' AND active=1) w, SUM(kind='strike' AND active=1) s, SUM(kind='ban' AND active=1) b "
                       "FROM discipline WHERE guild_id=%s", guild.id)
        sus_lines = [f"<@{r['user_id']}> — ends <t:{unix(r['ends_at'])}:R> · {r['reason'][:50]}" for r in sus[:15]]
        st_lines = [f"{'🔴' if r['n'] >= 2 else '🟠'} <@{r['user_id']}> — **{r['n']}/{MAX_STRIKES}**" for r in st if r["n"] < MAX_STRIKES][:20]
        rec = [f"{KINDS.get(r['kind'], ('•',))[0]} <@{r['user_id']}> · {KINDS.get(r['kind'], ('', r['kind']))[1]}"
               f" by <@{r['staff_id']}> <t:{unix(r['created_at'])}:R>" + ("" if r["active"] else " *(removed)*") for r in recent]
        return [discord.ui.TextDisplay("## ⚖️ Strikes & suspensions\n"
                                       f"**3 strikes = ban.** Strikes never expire. Members always hold the role for their count "
                                       f"(<@&{STRIKE_ROLES[0]}> → <@&{STRIKE_ROLES[1]}> → <@&{STRIKE_ROLES[2]}> + ban). "
                                       "Suspensions take their roles away + time them out, and give everything back automatically when they end. "
                                       "Every action DMs the member and is logged."),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"### ⏸️ Suspended now ({len(sus)})\n" + ("\n".join(sus_lines) or "Nobody")),
                discord.ui.TextDisplay(f"### ❗ On strikes ({len(st_lines)})\n" + ("\n".join(st_lines) or "Nobody")),
                discord.ui.TextDisplay("### 🕑 Latest\n" + ("\n".join(rec) or "Nothing yet")),
                discord.ui.TextDisplay(f"-# Active: {int(tot['w'] or 0)} warnings · {int(tot['s'] or 0)} strikes · {int(tot['b'] or 0)} strike bans"
                                       " · also `/discipline` commands")]

    async def refresh_panel(self, guild=None):
        guild = guild or self.bot.get_guild(self.bot.state.guild_id or 0)
        ch = guild and guild.get_channel(PANEL_CHANNEL)
        if not ch:
            return
        conf = self.bot.state.settings.setdefault("discipline", {})
        view = Panel(self, await self.panel_body(guild))
        if conf.get("panel_id"):
            try:
                await ch.get_partial_message(int(conf["panel_id"])).edit(view=view)
                return
            except discord.NotFound:
                pass
        msg = await ch.send(view=view)
        conf["panel_id"] = msg.id
        self.bot.state.save()

    async def end_suspension(self, guild, row, by_id=None, why="Suspension ended"):
        n, _ = await ex("UPDATE discipline SET active=0, removed_by=%s, removed_at=UTC_TIMESTAMP(), removed_reason=%s "
                        "WHERE id=%s AND active=1", by_id, why[:300], row["id"])
        if not n:
            return []
        member = guild.get_member(row["user_id"])
        notes = []
        if member:
            back = [r for rid in json.loads(row["saved_roles"] or "[]") if (r := guild.get_role(rid)) and r not in member.roles
                    and r < guild.me.top_role and not r.managed]
            try:
                if back:
                    await member.add_roles(*back, reason=f"Suspension #{row['id']} over")
                    notes.append(f"Gave back {len(back)} roles")
                if member.is_timed_out():
                    await member.timeout(None, reason=f"Suspension #{row['id']} over")
                    notes.append("Timeout removed")
            except discord.Forbidden:
                notes.append("⚠️ Couldn't restore roles / timeout")
            await self.sync_roles(member, f"Suspension #{row['id']} over")
        await self.notify(guild, row["user_id"], "▶️ Suspension over", "Your roles have been given back. Welcome back!", GREEN)
        await log(self.bot, guild.id, emb("▶️ Suspension ended", f"<@{row['user_id']}> · #{row['id']} · {why}"
                                          + (f" by <@{by_id}>" if by_id else "") + ("\n" + "\n".join(notes) if notes else ""), GREEN))
        return notes

    async def record_embed(self, guild, uid):
        s = await summary(guild.id, uid)
        member = guild.get_member(uid)
        rows = await q("SELECT * FROM discipline WHERE guild_id=%s AND user_id=%s ORDER BY id DESC LIMIT 20", guild.id, uid)
        e = emb(f"⚖️ Record — {member.display_name if member else uid}", f"<@{uid}> · `{uid}`", RED if s["strikes"] else BLUE)
        e.add_field(name="❗ Strikes", value=f"**{s['strikes']}/{MAX_STRIKES}**")
        e.add_field(name="⚠️ Warnings", value=str(s["warnings"]))
        e.add_field(name="⏸️ Suspended", value=f"until <t:{unix(s['suspended']['ends_at'])}:R>" if s["suspended"] else "No")
        lines = []
        for r in rows:
            ico, label, _ = KINDS.get(r["kind"], ("•", r["kind"], BLUE))
            line = f"{ico} `#{r['id']}` **{label}** <t:{unix(r['created_at'])}:d> by <@{r['staff_id']}> — {r['reason'][:80]}"
            if r["kind"] == "suspension" and r["ends_at"]:
                line += f" *(until <t:{unix(r['ends_at'])}:d>)*"
            if not r["active"]:
                line = f"~~{line}~~" + (f" *removed: {r['removed_reason'][:40]}*" if r["removed_reason"] else "")
            lines.append(line)
        e.add_field(name="History", value="\n".join(lines)[:1024] or "Clean record", inline=False)
        return e

    # ───── commands ─────
    async def run(self, inter, coro_fn):
        try:
            await self.need(inter)
            e = await coro_fn()
            if e is not None:
                await reply(inter, embed=e, ephemeral=True)
                await log(self.bot, inter.guild_id, e.copy().set_footer(text=f"by {inter.user}"))
                try:
                    await self.refresh_panel(inter.guild)
                except Exception:
                    logger.exception("discipline panel refresh failed")
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)

    @group.command(name="warn", description="Give a member a warning")
    @app_commands.describe(member="Who", reason="Why (sent to them)")
    async def warn(self, inter: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 3, 500]):
        await self.run(inter, lambda: self.do_warn(inter, member, reason))

    @group.command(name="strike", description="Give a member a strike — 3 strikes = ban")
    @app_commands.describe(member="Who", reason="Why (sent to them)")
    async def strike(self, inter: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 3, 500]):
        async def go():
            if member.top_role >= inter.guild.me.top_role:
                raise EconError(f"{member.mention}'s highest role is above mine, so I can't manage their strikes.")
            return await self.do_strike(inter, member, reason)
        await self.run(inter, go)

    @group.command(name="suspend", description="Suspend a member for a while (roles taken away + timeout)")
    @app_commands.describe(member="Who", length="e.g. 3d, 1w, 12h, 1w 2d", reason="Why (sent to them)")
    async def suspend(self, inter: discord.Interaction, member: discord.Member, length: str, reason: app_commands.Range[str, 3, 500]):
        await self.run(inter, lambda: self.do_suspend(inter, member, parse_duration(length), reason))

    @group.command(name="unsuspend", description="End a member's suspension early")
    @app_commands.describe(member="Who", reason="Why")
    async def unsuspend(self, inter: discord.Interaction, member: discord.Member, reason: str = "Ended early"):
        await self.run(inter, lambda: self.do_unsuspend(inter, member, reason))

    @group.command(name="unstrike", description="Remove a member's most recent strike (or warning)")
    @app_commands.describe(member="Who", reason="Why", what="Strike or warning")
    @app_commands.choices(what=[app_commands.Choice(name="Strike", value="strike"), app_commands.Choice(name="Warning", value="warning")])
    async def unstrike(self, inter: discord.Interaction, member: discord.Member, reason: str, what: str = "strike"):
        await self.run(inter, lambda: self.do_unstrike(inter, member, what, reason))

    @group.command(name="unban", description="Unban someone (e.g. after an appeal) — optionally clears a strike")
    @app_commands.describe(user_id="Their Discord ID", reason="Why", clear_strike="Also remove their most recent strike (so they're back on 2)")
    async def unban(self, inter: discord.Interaction, user_id: str, reason: str, clear_strike: bool = True):
        await self.run(inter, lambda: self.do_unban(inter, user_id.strip(), reason, clear_strike))

    @group.command(name="record", description="Show a member's warnings, strikes and suspensions")
    @app_commands.describe(member="Who (or use user_id for people who left)", user_id="Discord ID if they're not in the server")
    async def record(self, inter: discord.Interaction, member: discord.Member | None = None, user_id: str | None = None):
        try:
            await self.need(inter)
            uid = member.id if member else int(user_id) if (user_id or "").isdigit() else None
            if not uid:
                raise EconError("Pick a member or give a user ID.")
            await inter.response.send_message(embed=await self.record_embed(inter.guild, uid), ephemeral=True)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)

    # ───── background ─────
    @tasks.loop(minutes=1)
    async def expire(self):
        self._ticks = getattr(self, "_ticks", -1) + 1
        try:
            if self._ticks % 5 == 0:
                await self.refresh_panel()
            for row in await q("SELECT * FROM discipline WHERE kind='suspension' AND active=1 AND ends_at <= UTC_TIMESTAMP()"):
                g = self.bot.get_guild(row["guild_id"])
                if g:
                    await self.end_suspension(g, row, None, "Time served")
                    await self.refresh_panel(g)
            # suspensions longer than Discord's 28-day timeout: renew the timeout while it's still running
            for row in await q("SELECT * FROM discipline WHERE kind='suspension' AND active=1 AND ends_at > UTC_TIMESTAMP()"):
                g = self.bot.get_guild(row["guild_id"])
                m = g and g.get_member(row["user_id"])
                if m and not m.is_timed_out():
                    try:
                        await m.timeout(min(row["ends_at"] - now(), TIMEOUT_MAX), reason=f"Suspension #{row['id']} (renewed)")
                    except discord.HTTPException:
                        pass
        except Exception:
            logger.exception("discipline expiry failed")

    @expire.before_loop
    async def _w(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            if not await q1("SELECT 1 FROM discipline WHERE guild_id=%s AND user_id=%s AND active=1", member.guild.id, member.id):
                return
            await self.sync_roles(member, "Rejoined — discipline roles put back")
            row = await q1("SELECT * FROM discipline WHERE guild_id=%s AND user_id=%s AND kind='suspension' AND active=1", member.guild.id, member.id)
            if row:
                try:
                    await member.timeout(min(row["ends_at"] - now(), TIMEOUT_MAX), reason=f"Suspension #{row['id']} (rejoined)")
                except discord.HTTPException:
                    pass
                await log(self.bot, member.guild.id, emb("⏸️ Suspended member rejoined", f"{member.mention} · still suspended until "
                                                         f"<t:{unix(row['ends_at'])}:F> — roles and timeout put back", GOLD))
        except Exception:
            logger.exception("discipline rejoin check failed")


async def setup(bot):
    await bot.add_cog(Discipline(bot))
