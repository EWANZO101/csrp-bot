"""Business & property requests.

For each kind (business, property) there are two channels:
  #business-requests / #property-requests              — players press 📝 to send a request, 📋 to see theirs.
  #business-requests-admin / #property-requests-admin  — every request is posted here with ✅ Approve / ❌ Deny,
                                                         plus a card to see what's pending and open / lock the player channel.
Player channels start PRIVATE (staff only); staff with the Server setup permission can make them public.

Approving a business request creates the business owned by the requester. Approving a property request adds the
property to the requester's inventory (active character). Staff can charge a price (taken from the requester's bank,
paid to the treasury department if one is set) and add a note; the requester gets a DM either way.
"""
import datetime as dt
import logging

import discord
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, money, emb, transfer, ensure_user, log, dm, reply, is_admin, \
    BURN, GREEN, RED, BLUE, GOLD

logger = logging.getLogger("economy.requests")
MAX_PENDING = 3
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS req_requests (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      kind VARCHAR(16) NOT NULL,
      user_id BIGINT UNSIGNED NOT NULL,
      title VARCHAR(64) NOT NULL,
      ptype VARCHAR(100) NULL,
      location VARCHAR(100) NULL,
      details TEXT NULL,
      status VARCHAR(12) NOT NULL DEFAULT 'pending',
      admin_id BIGINT UNSIGNED NULL,
      note VARCHAR(500) NULL,
      price BIGINT NOT NULL DEFAULT 0,
      admin_msg_id BIGINT UNSIGNED NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      decided_at DATETIME NULL,
      INDEX ix_req (guild_id, kind, status),
      INDEX ix_req_user (guild_id, user_id)
    ) ENGINE=InnoDB""",
    "ALTER TABLE req_requests MODIFY title VARCHAR(120) NOT NULL",
    "ALTER TABLE req_requests ADD COLUMN IF NOT EXISTS property_id INT NULL",
    "ALTER TABLE req_requests ADD COLUMN IF NOT EXISTS civ_name VARCHAR(100) NULL",
]
KINDS = {
    "business": dict(emoji="🏢", label="Business", channel="business-requests", perm="orgs", colour=discord.Colour.blue(),
                     intro="Want to open a business? Send a request and staff will review it.",
                     f_title=("Business name", "e.g. Burger Shot Vinewood", 2), f_type=("Type of business", "e.g. Restaurant, mechanic, security"),
                     f_loc=("Location (optional)", "e.g. Vinewood Blvd"), f_det=("What will the business do?", "Plans, staff, hours, why you want it…")),
    "property": dict(emoji="🏠", label="Property", channel="property-requests", perm="economy", colour=discord.Colour.green(),
                     intro="Want a house, apartment, warehouse or other property? Send a request and staff will review it.",
                     f_title=("Property address / name", "e.g. 1229 Strawberry Ave", 2), f_type=("Type of property", "e.g. House, apartment, warehouse"),
                     f_loc=("Area (optional)", "e.g. Mirror Park"), f_det=("What's it for? Budget?", "Who lives there, what you'll use it for, budget…")),
}
STATUS = {"pending": ("🟡", "Pending"), "approved": ("✅", "Approved"), "denied": ("❌", "Denied")}


def S(text, button):
    return discord.ui.Section(discord.ui.TextDisplay(text), accessory=button)


def request_embed(c, r, user_mention=None) -> discord.Embed:
    k = KINDS[r["kind"]]
    ico, st = STATUS.get(r["status"], ("•", r["status"]))
    colour = {"pending": GOLD, "approved": GREEN, "denied": RED}.get(r["status"], BLUE)
    e = emb(f"{k['emoji']} {k['label']} request #{r['id']} — {r['title']}", None, colour)
    e.add_field(name="From", value=user_mention or f"<@{r['user_id']}>")
    if r.get("property_id"):
        e.add_field(name="Category", value=r["ptype"] or "—")
        e.add_field(name="Character name", value=r.get("civ_name") or "—")
        e.add_field(name="Listed price", value=money(c, r["listed_price"]) if r.get("listed_price") else "Not set")
    else:
        e.add_field(name="Type", value=r["ptype"] or "—")
        e.add_field(name="Location", value=r["location"] or "—")
    e.add_field(name="Details", value=(r["details"] or "—")[:1024], inline=False)
    status = f"{ico} **{st}**"
    if r["status"] != "pending":
        status += f" by <@{r['admin_id']}>" if r["admin_id"] else ""
        if r["status"] == "approved" and r["price"]:
            status += f" · charged {money(c, r['price'])}"
        if r["note"]:
            status += f"\n> {r['note'][:400]}"
    e.add_field(name="Status", value=status, inline=False)
    e.set_footer(text=f"Request #{r['id']}")
    e.timestamp = r["created_at"].replace(tzinfo=dt.timezone.utc) if r.get("created_at") else None
    return e


# ═════════════════════ player side ═════════════════════
class RequestModal(discord.ui.Modal):
    def __init__(self, cog, kind):
        k = KINDS[kind]
        super().__init__(title=f"{k['emoji']} Request a {k['label'].lower()}", timeout=900)
        self.cog, self.kind = cog, kind
        self.t = discord.ui.TextInput(label=k["f_title"][0], placeholder=k["f_title"][1], min_length=k["f_title"][2], max_length=64)
        self.ty = discord.ui.TextInput(label=k["f_type"][0], placeholder=k["f_type"][1], max_length=100)
        self.lo = discord.ui.TextInput(label=k["f_loc"][0], placeholder=k["f_loc"][1], max_length=100, required=False)
        self.de = discord.ui.TextInput(label=k["f_det"][0], placeholder=k["f_det"][1], style=discord.TextStyle.paragraph,
                                       max_length=1000)
        for i in (self.t, self.ty, self.lo, self.de):
            self.add_item(i)

    async def on_submit(self, inter):
        try:
            await self.cog.submit(inter, self.kind, self.t.value.strip(), self.ty.value.strip(),
                                  self.lo.value.strip() or None, self.de.value.strip())
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class PropertyModal(discord.ui.Modal):
    def __init__(self, cog, prop, char_name):
        super().__init__(title=f"🏠 {prop['name']}"[:45], timeout=900)
        self.cog, self.prop = cog, prop
        self.civ = discord.ui.TextInput(label="Your character's name (shown on the list)", default=(char_name or "")[:100] or None,
                                        max_length=100, placeholder="e.g. Jax Jacobs")
        self.de = discord.ui.TextInput(label="What's it for? (optional)", style=discord.TextStyle.paragraph, max_length=1000,
                                       required=False, placeholder="Who lives there, what you'll use it for…")
        self.add_item(self.civ)
        self.add_item(self.de)

    async def on_submit(self, inter):
        try:
            await self.cog.submit_property(inter, self.prop["id"], self.civ.value.strip(), self.de.value.strip() or None)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class PropertyPicker(discord.ui.View):
    """Step 1: pick a group (Residential, Garages, …). Step 2: a category. Step 3: an available property (opens PropertyModal)."""
    def __init__(self, cog, groups, c, char_name):
        super().__init__(timeout=600)
        self.cog, self.c, self.char_name = cog, c, char_name
        sel = discord.ui.Select(placeholder="1️⃣ What kind of property?", options=[
            discord.SelectOption(label=g["grp"][:100], value=g["grp"][:100], description=f"{g['free']} available")
            for g in groups[:25]])
        sel.callback = self.pick_group
        self.add_item(sel)

    async def pick_group(self, inter):
        grp = inter.data["values"][0]
        cats = await inter.client.get_cog("Properties").categories(inter.guild_id, available_only=True, group=grp)
        if not cats:
            return await inter.response.edit_message(embed=emb("❌ Nothing available there any more.", None, RED), view=self)
        v = discord.ui.View(timeout=600)
        s = discord.ui.Select(placeholder="2️⃣ Pick a category", options=[
            discord.SelectOption(label=r["category"][:100], value=r["category"][:100], description=f"{r['free']} available")
            for r in cats[:25]])
        s.callback = self.pick_cat
        v.add_item(s)
        await inter.response.edit_message(embed=emb(f"🏘️ {grp}", "Pick a category.", BLUE), view=v)

    async def pick_cat(self, inter):
        cat = inter.data["values"][0]
        props = await inter.client.get_cog("Properties").available(inter.guild_id, cat)
        if not props:
            return await inter.response.edit_message(embed=emb("❌ Nothing available in that category any more.", None, RED), view=None)
        v = discord.ui.View(timeout=600)
        for i in range(0, min(len(props), 100), 25):
            chunk = props[i:i + 25]
            s = discord.ui.Select(placeholder=f"3️⃣ Pick a property ({i + 1}–{i + len(chunk)})", options=[
                discord.SelectOption(label=p["name"][:100], value=str(p["id"]),
                                     description=money(self.c, p["price"]) if p["price"] else "Price set by staff")
                for p in chunk])

            async def picked(i2, s=s):
                p = await i2.client.get_cog("Properties").get(int(s.values[0]))
                await i2.response.send_modal(PropertyModal(self.cog, p, self.char_name))
            s.callback = picked
            v.add_item(s)
        await inter.response.edit_message(embed=emb(f"🏘️ {cat}", f"{len(props)} available — pick one below.", BLUE), view=v)


class PlayerCard(discord.ui.LayoutView):
    def __init__(self, cog, kind, header=None):
        super().__init__(timeout=None)
        self.cog, self.kind = cog, kind
        k = KINDS[kind]
        new = discord.ui.Button(label=f"Request a {k['label'].lower()}", emoji="📝", style=discord.ButtonStyle.success,
                                custom_id=f"req:{kind}:new")
        mine = discord.ui.Button(label="My requests", emoji="📋", style=discord.ButtonStyle.secondary, custom_id=f"req:{kind}:mine")
        new.callback = mine.callback = self.on_button
        self.add_item(discord.ui.Container(
            *(header or [discord.ui.TextDisplay(f"## {k['emoji']} {k['label']} Requests")]), discord.ui.Separator(),
            S(f"**📝 Request a {k['label'].lower()}**\nFill in a short form. Staff will approve or deny it and you'll get a DM.", new),
            discord.ui.Separator(visible=False),
            S("**📋 My requests**\nSee your requests and whether they've been approved.", mine),
            accent_colour=k["colour"]))

    async def on_button(self, inter):
        try:
            await self.cog.player_action(inter, self.kind, inter.data["custom_id"].split(":")[2])
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


# ═════════════════════ admin side ═════════════════════
class Decide(discord.ui.DynamicItem[discord.ui.Button], template=r"reqd:(?P<act>approve|deny):(?P<rid>\d+)"):
    def __init__(self, act: str, rid: int):
        super().__init__(discord.ui.Button(
            label="Approve" if act == "approve" else "Deny", emoji="✅" if act == "approve" else "❌",
            style=discord.ButtonStyle.success if act == "approve" else discord.ButtonStyle.danger, custom_id=f"reqd:{act}:{rid}"))
        self.act, self.rid = act, rid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["act"], int(match["rid"]))

    async def callback(self, inter):
        cog = inter.client.get_cog("Requests")
        try:
            await cog.decide_start(inter, self.act, self.rid)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


def decide_view(rid):
    v = discord.ui.View(timeout=None)
    v.add_item(Decide("approve", rid))
    v.add_item(Decide("deny", rid))
    return v


class DecideModal(discord.ui.Modal):
    def __init__(self, cog, r, act, default_price):
        k = KINDS[r["kind"]]
        super().__init__(title=f"{'✅ Approve' if act == 'approve' else '❌ Deny'} {k['label'].lower()} #{r['id']}"[:45], timeout=600)
        self.cog, self.r, self.act = cog, r, act
        self.price = None
        if act == "approve":
            self.price = discord.ui.TextInput(label="Price to charge (0 = free)", default=str(default_price), max_length=20)
            self.add_item(self.price)
        self.note = discord.ui.TextInput(label="Note to the player" + (" (optional)" if act == "approve" else " — reason"),
                                         style=discord.TextStyle.paragraph, max_length=500, required=act != "approve")
        self.add_item(self.note)

    async def on_submit(self, inter):
        try:
            await self.cog.decide(inter, self.r["id"], self.act, self.price.value if self.price else "0", self.note.value.strip() or None)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class AdminCard(discord.ui.LayoutView):
    def __init__(self, cog, kind, header=None, is_open=False):
        super().__init__(timeout=None)
        self.cog, self.kind = cog, kind
        k = KINDS[kind]
        pend = discord.ui.Button(label="Pending requests", emoji="📥", style=discord.ButtonStyle.primary, custom_id=f"reqadm:{kind}:pending")
        hist = discord.ui.Button(label="Recent decisions", emoji="📜", style=discord.ButtonStyle.secondary, custom_id=f"reqadm:{kind}:history")
        tog = discord.ui.Button(label="Make public" if not is_open else "Make private", emoji="🔓" if not is_open else "🔒",
                                style=discord.ButtonStyle.success if not is_open else discord.ButtonStyle.danger,
                                custom_id=f"reqadm:{kind}:toggle")
        for b in (pend, hist, tog):
            b.callback = self.on_button
        extra = []
        if kind == "property":
            tools = {a: discord.ui.Button(label=l, emoji=e, style=st, custom_id=f"reqadm:{kind}:{a}") for a, l, e, st in [
                ("edit", "Edit a property", "🏘️", discord.ButtonStyle.primary),
                ("import", "Import from sheet", "📥", discord.ButtonStyle.secondary),
                ("sheet", "Sheet sync", "🔗", discord.ButtonStyle.secondary),
                ("audit", "Audit owners", "🔍", discord.ButtonStyle.success)]}
            for b in tools.values():
                b.callback = self.on_button
            extra = [discord.ui.Separator(),
                     S("**🏘️ Edit a property**\nSet or clear an owner, change the price, or mark it available — updates #property-list "
                       "and the Google Sheet.", tools["edit"]),
                     S("**📥 Import from sheet**\nReplace the bot's property list with what's in the Google Sheet right now.", tools["import"]),
                     S("**🔗 Sheet sync**\nConnect the Google Sheet so approved sales are written back automatically.", tools["sheet"]),
                     S("**🔍 Audit owners**\nGo through every owned property one by one — ✅ still owns / ❌ doesn't. Progress is saved.",
                       tools["audit"])]
        self.add_item(discord.ui.Container(
            *(header or [discord.ui.TextDisplay(f"## {k['emoji']} {k['label']} Requests — admin")]), discord.ui.Separator(),
            S("**📥 Pending requests**\nEverything waiting for a decision, oldest first, with links.", pend),
            S("**📜 Recent decisions**\nThe last approved / denied requests.", hist),
            S(f"**🔒 Player channel**\n#{k['channel']} is " + ("**public** — everyone can see it and send requests."
                                                              if is_open else "**private** — staff only."), tog),
            *extra,
            accent_colour=discord.Colour.dark_gold()))

    async def on_button(self, inter):
        try:
            if not is_admin(inter.user, await cfg(inter.guild_id), KINDS[self.kind]["perm"]):
                raise EconError("Staff only.")
            await self.cog.admin_action(inter, self.kind, inter.data["custom_id"].split(":")[2])
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


# ═════════════════════ cog ═════════════════════
class Requests(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        for kind in KINDS:
            bot.add_view(PlayerCard(self, kind))
            bot.add_view(AdminCard(self, kind))
        bot.add_dynamic_items(Decide)
        self.cards.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)

    def cog_unload(self):
        self.cards.cancel()
        self.bot.remove_dynamic_items(Decide)

    def conf(self, kind) -> dict:
        return self.bot.state.settings.setdefault("requests", {}).setdefault(kind, {"open": False})

    # ───── player ─────
    async def player_action(self, inter, kind, act):
        gid, uid = inter.guild_id, inter.user.id
        c = await cfg(gid)
        if not self.conf(kind).get("open") and not is_admin(inter.user, c, KINDS[kind]["perm"]):
            raise EconError(f"{KINDS[kind]['label']} requests aren't open yet — check back soon.")
        if act == "new":
            n = await q1("SELECT COUNT(*) n FROM req_requests WHERE guild_id=%s AND kind=%s AND user_id=%s AND status='pending'",
                         gid, kind, uid)
            if n["n"] >= MAX_PENDING:
                raise EconError(f"You already have {MAX_PENDING} pending {kind} requests — wait for staff to review them.")
            props = self.bot.get_cog("Properties")
            if kind == "property" and props and await props.count(gid):
                groups = await props.groups(gid, available_only=True)
                if not groups:
                    raise EconError("Every property is owned right now — check back later.")
                char = await core.active_character_name(gid, uid)
                await inter.response.send_message(
                    embed=emb("🏠 Request a property", "Pick the kind of property, a category, then an available one. "
                              "See everything in #property-list.", BLUE),
                    view=PropertyPicker(self, groups, c, char), ephemeral=True)
                return
            await inter.response.send_modal(RequestModal(self, kind))
        elif act == "mine":
            rows = await q("SELECT * FROM req_requests WHERE guild_id=%s AND kind=%s AND user_id=%s ORDER BY id DESC LIMIT 15",
                           gid, kind, uid)
            lines = []
            for r in rows:
                ico, st = STATUS.get(r["status"], ("•", r["status"]))
                line = f"{ico} `#{r['id']}` **{r['title']}** — {st}"
                if r["status"] == "approved" and r["price"]:
                    line += f" · paid {money(c, r['price'])}"
                if r["note"] and r["status"] != "pending":
                    line += f"\n  > {r['note'][:150]}"
                lines.append(line)
            k = KINDS[kind]
            await reply(inter, embed=emb(f"📋 My {k['label'].lower()} requests", "\n".join(lines)[:4000] or "You haven't sent any yet.", BLUE),
                        ephemeral=True)

    async def submit(self, inter, kind, title, ptype, location, details):
        gid, uid = inter.guild_id, inter.user.id
        await ensure_user(gid, uid)
        if kind == "business" and await q1("SELECT 1 FROM orgs WHERE guild_id=%s AND kind='business' AND name=%s", gid, title):
            raise EconError(f"A business called **{title}** already exists — pick another name.")
        if await q1("SELECT 1 FROM req_requests WHERE guild_id=%s AND kind=%s AND user_id=%s AND title=%s AND status='pending'",
                    gid, kind, uid, title):
            raise EconError(f"You already have a pending request for **{title}**.")
        _, rid = await ex("INSERT INTO req_requests (guild_id,kind,user_id,title,ptype,location,details) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                          gid, kind, uid, title, ptype, location, details)
        r = await self.load(rid)
        c = await cfg(gid)
        ch = inter.guild.get_channel(int(self.conf(kind).get("admin_channel") or 0))
        if ch:
            try:
                msg = await ch.send(embed=request_embed(c, r, inter.user.mention), view=decide_view(rid))
                await ex("UPDATE req_requests SET admin_msg_id=%s WHERE id=%s", msg.id, rid)
            except discord.HTTPException:
                logger.exception("couldn't post request #%s", rid)
        k = KINDS[kind]
        await reply(inter, embed=emb(f"📨 {k['label']} request #{rid} sent", f"**{title}** — staff will review it and you'll get a DM "
                                     "with the decision. Check its status with 📋 My requests.", GREEN), ephemeral=True)
        await log(self.bot, gid, emb(f"{k['emoji']} New {kind} request", f"#{rid} **{title}** from {inter.user.mention}", BLUE))
        await self.sync()

    async def load(self, rid):
        return await q1("SELECT r.*, p.price listed_price FROM req_requests r LEFT JOIN properties p ON p.id=r.property_id "
                        "WHERE r.id=%s", rid)

    async def submit_property(self, inter, pid, civ_name, details):
        gid, uid = inter.guild_id, inter.user.id
        await ensure_user(gid, uid)
        props = self.bot.get_cog("Properties")
        p = await props.get(pid)
        if not p or p["guild_id"] != gid:
            raise EconError("That property no longer exists.")
        if p["owner_id"] or (p["owner_name"] or "").strip() or (p["owner_discord"] or "").strip():
            raise EconError(f"**{p['name']}** was just taken — pick another one.")
        if not civ_name:
            raise EconError("Enter your character's name.")
        if await q1("SELECT 1 FROM req_requests WHERE guild_id=%s AND user_id=%s AND property_id=%s AND status='pending'", gid, uid, pid):
            raise EconError(f"You already have a pending request for **{p['name']}**.")
        others = (await q1("SELECT COUNT(*) n FROM req_requests WHERE guild_id=%s AND property_id=%s AND status='pending'", gid, pid))["n"]
        _, rid = await ex("INSERT INTO req_requests (guild_id,kind,user_id,title,ptype,details,property_id,civ_name) "
                          "VALUES (%s,'property',%s,%s,%s,%s,%s,%s)", gid, uid, p["name"], p["category"], details, pid, civ_name)
        r = await self.load(rid)
        c = await cfg(gid)
        ch = inter.guild.get_channel(int(self.conf("property").get("admin_channel") or 0))
        if ch:
            try:
                msg = await ch.send(content=f"⚠️ {others} other pending request(s) for this property." if others else None,
                                    embed=request_embed(c, r, inter.user.mention), view=decide_view(rid))
                await ex("UPDATE req_requests SET admin_msg_id=%s WHERE id=%s", msg.id, rid)
            except discord.HTTPException:
                logger.exception("couldn't post request #%s", rid)
        await reply(inter, embed=emb(f"📨 Property request #{rid} sent",
                                     f"**{p['name']}** ({p['category']})" + (f" · listed at {money(c, p['price'])}" if p["price"] else "")
                                     + "\nStaff will review it and you'll get a DM with the decision.", GREEN), ephemeral=True)
        await log(self.bot, gid, emb("🏠 New property request", f"#{rid} **{p['name']}** from {inter.user.mention}", BLUE))
        await self.sync()

    # ───── admin ─────
    async def admin_action(self, inter, kind, act):
        gid = inter.guild_id
        c = await cfg(gid)
        k = KINDS[kind]
        if act == "toggle":
            if not is_admin(inter.user, c, "setup"):
                raise EconError("You need the Server setup staff permission to change who can see the channel.")
            conf = self.conf(kind)
            conf["open"] = not conf.get("open")
            self.bot.state.save()
            await self.sync(perms=True)
            txt = f"🔓 #{k['channel']} is now PUBLIC" if conf["open"] else f"🔒 #{k['channel']} is now PRIVATE (staff only)"
            await reply(inter, embed=emb(txt, None, GREEN if conf["open"] else GOLD), ephemeral=True)
            await log(self.bot, gid, emb(f"{k['emoji']} {k['label']} requests {'made public' if conf['open'] else 'made private'}",
                                         f"by {inter.user.mention}", GOLD))
        elif act in ("pending", "history"):
            if act == "pending":
                rows = await q("SELECT * FROM req_requests WHERE guild_id=%s AND kind=%s AND status='pending' ORDER BY id LIMIT 30", gid, kind)
            else:
                rows = await q("SELECT * FROM req_requests WHERE guild_id=%s AND kind=%s AND status<>'pending' ORDER BY decided_at DESC LIMIT 20",
                               gid, kind)
            ch_id = self.conf(kind).get("admin_channel")
            lines = []
            for r in rows:
                ico, st = STATUS.get(r["status"], ("•", r["status"]))
                link = f" · [open](https://discord.com/channels/{gid}/{ch_id}/{r['admin_msg_id']})" if r["admin_msg_id"] and ch_id else ""
                extra = "" if act == "pending" else f" · {st} by <@{r['admin_id']}>" + (f" · {money(c, r['price'])}" if r["price"] else "")
                lines.append(f"{ico} `#{r['id']}` **{r['title']}** — <@{r['user_id']}>{extra}{link}")
            title = f"📥 Pending {kind} requests ({len(rows)})" if act == "pending" else f"📜 Recent {kind} decisions"
            await reply(inter, embed=emb(title, "\n".join(lines)[:4000] or "Nothing here.", BLUE), ephemeral=True)
        elif act in ("edit", "import", "sheet", "audit"):
            await self.bot.get_cog("Properties").admin(inter, act)

    async def decide_start(self, inter, act, rid):
        r = await q1("SELECT * FROM req_requests WHERE id=%s AND guild_id=%s", rid, inter.guild_id)
        if not r:
            raise EconError("That request no longer exists.")
        c = await cfg(inter.guild_id)
        if not is_admin(inter.user, c, KINDS[r["kind"]]["perm"]):
            raise EconError(f"You need the {core.STAFF_PERMS[KINDS[r['kind']]['perm']][0]} staff permission.")
        if r["status"] != "pending":
            raise EconError(f"Request #{rid} was already {r['status']}.")
        if r["kind"] == "business":
            default_price = c.get("business_cost", 0)
        else:
            p = await self.bot.get_cog("Properties").get(r["property_id"]) if r.get("property_id") else None
            default_price = (p["price"] or 0) if p else 0
        await inter.response.send_modal(DecideModal(self, r, act, default_price))

    async def decide(self, inter, rid, act, price_raw, note):
        gid = inter.guild_id
        c = await cfg(gid)
        r = await q1("SELECT * FROM req_requests WHERE id=%s AND guild_id=%s", rid, gid)
        if not r or r["status"] != "pending":
            raise EconError(f"Request #{rid} was already decided.")
        k = KINDS[r["kind"]]
        if not is_admin(inter.user, c, k["perm"]):
            raise EconError("Staff only.")
        uid = r["user_id"]
        status = "approved" if act == "approve" else "denied"
        price = 0
        warn = ""
        if act == "approve":
            raw = (price_raw or "0").strip().replace(",", "").replace(c["currency"], "").replace("$", "")
            try:
                price = int(float(raw or 0))
            except ValueError:
                raise EconError("Price must be a number.")
            if price < 0:
                raise EconError("Price can't be negative.")
            await ensure_user(gid, uid)
            if r["kind"] == "business":
                if await q1("SELECT 1 FROM orgs WHERE guild_id=%s AND kind='business' AND name=%s", gid, r["title"]):
                    raise EconError(f"A business called **{r['title']}** already exists — deny this request or rename that business first.")
                owned = await q1("SELECT COUNT(*) n FROM orgs WHERE guild_id=%s AND kind='business' AND owner_id=%s", gid, uid)
                if owned["n"] >= c["max_businesses"]:
                    warn = f"\n⚠️ They now own {owned['n'] + 1} businesses (limit for self-service is {c['max_businesses']})."
        legs = []
        prop = None
        if act == "approve" and r.get("property_id"):
            prop = await self.bot.get_cog("Properties").get(r["property_id"])
            if not prop:
                raise EconError("That property no longer exists — deny this request.")
            if prop["owner_id"] or (prop["owner_name"] or "").strip() or (prop["owner_discord"] or "").strip():
                raise EconError(f"**{prop['name']}** is already owned — deny this request (or clear the owner with 🏘️ Edit a property).")
        if price:
            re_dept = await self.bot.get_cog("Properties").real_estate_dept(gid) if r["kind"] == "property" else None
            dst = ("org", re_dept) if re_dept else (("org", c["treasury_dept"]) if c.get("treasury_dept") else BURN)
            legs = [(("bank", uid), dst, price)]
        member = inter.guild.get_member(uid)

        async def extra(cur):
            await cur.execute("UPDATE req_requests SET status=%s, admin_id=%s, note=%s, price=%s, decided_at=UTC_TIMESTAMP() "
                              "WHERE id=%s AND status='pending'", (status, inter.user.id, note, price, rid))
            if cur.rowcount != 1:
                raise EconError(f"Request #{rid} was already decided.")
            if act != "approve":
                return
            desc = " · ".join(x for x in (r["ptype"], r["location"]) if x)
            if r["kind"] == "business":
                await cur.execute("INSERT INTO orgs (guild_id,kind,name,owner_id,description) VALUES (%s,'business',%s,%s,%s)",
                                  (gid, r["title"], uid, (desc + (" — " if desc and r["details"] else "") + (r["details"] or ""))[:500] or None))
            else:
                await cur.execute(
                    "INSERT INTO user_items (guild_id,user_id,name,description,consumable,source,qty) VALUES (%s,%s,%s,%s,0,'property request',1) "
                    "ON DUPLICATE KEY UPDATE qty=qty+1", (gid, uid, r["title"], desc[:300] or None))
                if prop:
                    await cur.execute("UPDATE properties SET owner_id=%s, owner_name=%s, owner_discord=%s, price=%s, sheet_synced=0, "
                                      "updated_at=UTC_TIMESTAMP() WHERE id=%s AND owner_id IS NULL AND COALESCE(TRIM(owner_name),'')='' "
                                      "AND COALESCE(TRIM(owner_discord),'')=''",
                                      (uid, r.get("civ_name") or (member.display_name if member else None),
                                       member.name if member else None, price or prop["price"], prop["id"]))
                    if cur.rowcount != 1:
                        raise EconError(f"**{prop['name']}** was just taken by someone else.")
        try:
            await transfer(gid, legs, inter.user.id, "business_fee" if r["kind"] == "business" else "property",
                           f"{k['label']} request #{rid}: {r['title']}", extra=extra)
        except EconError as err:
            if "Insufficient" in str(err):
                raise EconError(f"<@{uid}> can't afford {money(c, price)} — {err}")
            raise
        r = await self.load(rid)
        sheet_note = ""
        if prop:
            ok, msg = await self.bot.get_cog("Properties").push(prop["id"])
            sheet_note = "\n📄 Google Sheet updated." if ok else f"\n📄 Sheet not updated yet: {msg} (will retry)."
        # update the admin post (remove buttons)
        ch = inter.guild.get_channel(int(self.conf(r["kind"]).get("admin_channel") or 0))
        if ch and r["admin_msg_id"]:
            try:
                await ch.get_partial_message(r["admin_msg_id"]).edit(embed=request_embed(c, r), view=None)
            except discord.HTTPException:
                pass
        # tell the player
        what = (f"Your business **{r['title']}** is open — manage it with `/business`." if r["kind"] == "business"
                else f"**{r['title']}** has been added to your inventory (`/inventory`).")
        txt = (f"✅ Your {r['kind']} request **#{rid} {r['title']}** was **approved**.\n{what}"
               + (f"\n💸 {money(c, price)} was taken from your bank." if price else "")) if act == "approve" else \
            f"❌ Your {r['kind']} request **#{rid} {r['title']}** was **denied**."
        if note:
            txt += f"\n> {note}"
        await dm(self.bot, uid, emb(f"{k['emoji']} {k['label']} request update", txt, GREEN if act == "approve" else RED))
        await reply(inter, embed=emb(f"{'✅ Approved' if act == 'approve' else '❌ Denied'} #{rid} — {r['title']}",
                                     f"For <@{uid}>" + (f" · charged {money(c, price)}" if price else "") + warn + sheet_note,
                                     GREEN if act == "approve" else GOLD), ephemeral=True)
        await log(self.bot, gid, emb(f"{k['emoji']} {k['label']} request {status}",
                                     f"#{rid} **{r['title']}** for <@{uid}> by {inter.user.mention}"
                                     + (f" · {money(c, price)}" if price else ""), GREEN if act == "approve" else RED))
        await self.sync()

    # ───── channels ─────
    async def overwrites(self, guild, open_):
        panel = self.bot.get_cog("StaffPanel")
        ow = await panel.overwrites(guild) if panel else {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                                                          guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        for t, p in ow.items():
            if t != guild.me:
                p.send_messages = False
        if open_:
            ow[guild.default_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False)
        return ow

    async def ensure(self, guild, kind, side, name, topic, view, ow, perms):
        conf = self.conf(kind)
        ch = guild.get_channel(int(conf.get(f"{side}_channel") or 0))
        if not ch:
            c = await cfg(guild.id)
            logch = guild.get_channel(c.get("log_channel") or 0)
            ch = await guild.create_text_channel(name, overwrites=ow, category=getattr(logch, "category", None), topic=topic)
            conf.update({f"{side}_channel": ch.id, f"{side}_message": None})
            logger.info("created #%s (%s)", ch.name, ch.id)
        elif perms:
            await ch.edit(overwrites=ow)
        ok = False
        if conf.get(f"{side}_message"):
            try:
                await ch.get_partial_message(int(conf[f"{side}_message"])).edit(view=view)
                ok = True
            except discord.NotFound:
                pass
        if not ok:
            msg = await ch.send(view=view)
            conf[f"{side}_message"] = msg.id
        self.bot.state.save()

    async def sync(self, perms=False):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            for kind, k in KINDS.items():
                is_open = bool(self.conf(kind).get("open"))
                stats = {r["status"]: r["n"] for r in await q(
                    "SELECT status, COUNT(*) n FROM req_requests WHERE guild_id=%s AND kind=%s GROUP BY status", gid, kind)}
                stamp = f"<t:{int(discord.utils.utcnow().timestamp())}:R>"
                head = [discord.ui.TextDisplay(f"## {k['emoji']} {k['label']} Requests\n{k['intro']}\n"
                                               f"-# {stats.get('approved', 0)} approved so far · updated {stamp}")]
                await self.ensure(g, kind, "player", k["channel"], f"{k['label']} requests — send a request to staff",
                                  PlayerCard(self, kind, head), await self.overwrites(g, is_open), perms)
                ahead = [discord.ui.TextDisplay(
                    f"## {k['emoji']} {k['label']} Requests — admin\n"
                    f"{'🔓 Player channel **PUBLIC**' if is_open else '🔒 Player channel **PRIVATE** (staff only)'}\n"
                    f"🟡 **{stats.get('pending', 0)}** pending · ✅ {stats.get('approved', 0)} approved · ❌ {stats.get('denied', 0)} denied\n"
                    f"-# New requests appear below with Approve / Deny buttons · updated {stamp}")]
                await self.ensure(g, kind, "admin", f"{k['channel']}-admin", f"{k['label']} requests — review (staff only)",
                                  AdminCard(self, kind, ahead, is_open), await self.overwrites(g, False), perms)
                props = self.bot.get_cog("Properties")
                if kind == "property" and props:
                    try:
                        await props.refresh_list(g, await self.overwrites(g, is_open), perms)
                    except Exception:
                        logger.exception("property list refresh failed")

    @tasks.loop(minutes=5)
    async def cards(self):
        try:
            await self.sync(perms=self.cards.current_loop == 0)
        except Exception:
            logger.exception("request cards refresh failed")

    @cards.before_loop
    async def _wc(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Requests(bot))
