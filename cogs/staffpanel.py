"""Private staff control panel: a channel with one button per staff action.

Each button opens a pop-up form. Player fields are Discord user pickers (live search as you type a name);
business / item fields accept part of a name and are matched to the closest existing one.
The channel is created automatically (visible to bot owners + staff-rank roles only) and the panel is re-posted
if it goes missing. Every action re-checks the clicker's staff permission — hiding the channel isn't the security.
"""
import difflib, logging

import discord
from discord import app_commands
from discord.ext import commands

import core
from core import (EconError, q, q1, ex, cfg, money, emb, parse_amount, transfer, ensure_user, get_user, log,
                  is_admin, MINT, BURN, GREEN, RED, BLUE, GOLD)
from cogs.orgs import move_user_items, give_user_item, take_stock

logger = logging.getLogger("economy.panel")
CHANNEL_NAME = "staff-panel"


# ───────── fuzzy "prefill" matching ─────────
def best_match(query: str, names: list[str], what: str) -> str:
    qv = (query or "").strip().lower()
    if not qv:
        raise EconError(f"Type a {what} name.")
    low = {n.lower(): n for n in names}
    if qv in low:
        return low[qv]
    starts = [n for n in names if n.lower().startswith(qv)]
    if len(starts) == 1:
        return starts[0]
    contains = [n for n in names if qv in n.lower()]
    if len(contains) == 1:
        return contains[0]
    pool = starts or contains
    if pool:   # several candidates: shortest name is the most likely intended one
        return sorted(pool, key=len)[0]
    close = difflib.get_close_matches(qv, list(low), n=1, cutoff=0.55)
    if close:
        return low[close[0]]
    sugg = difflib.get_close_matches(qv, list(low), n=5, cutoff=0.3)
    raise EconError(f"No {what} matching **{query}**." + (f" Did you mean: {', '.join(low[s] for s in sugg)}?" if sugg else ""))


async def match_org(gid, query, kind=None):
    rows = await q("SELECT * FROM orgs WHERE guild_id=%s" + (" AND kind=%s" if kind else ""), gid, *([kind] if kind else []))
    name = best_match(query, [r["name"] for r in rows], "business/department")
    return next(r for r in rows if r["name"] == name)


def picked(label: discord.ui.Label):
    vals = getattr(label.component, "values", None) or []
    return vals[0] if vals else None


def text(label: discord.ui.Label) -> str:
    return (label.component.value or "").strip()


def user_field(title="Player", desc="Start typing their name", required=True):
    return discord.ui.Label(text=title, description=desc,
                            component=discord.ui.UserSelect(placeholder="Search players…", required=required,
                                                                   min_values=1 if required else 0))


def text_field(title, desc=None, placeholder=None, required=True, long=False, default=None):
    return discord.ui.Label(text=title, description=desc, component=discord.ui.TextInput(
        placeholder=placeholder, required=required, default=default,
        style=discord.TextStyle.paragraph if long else discord.TextStyle.short, max_length=300 if long else 100))


def choice_field(title, options: list[tuple[str, str]], desc=None):
    return discord.ui.Label(text=title, description=desc, component=discord.ui.Select(
        options=[discord.SelectOption(label=l, value=v, default=(i == 0)) for i, (l, v) in enumerate(options)]))


async def need(inter: discord.Interaction, perm: str):
    c = await cfg(inter.guild_id)
    if not is_admin(inter.user, c, perm):
        raise EconError(f"You need the **{core.STAFF_PERMS[perm][0]}** staff permission for this.")
    return c


async def done(inter, e: discord.Embed, public_log=True):
    if public_log:
        await log(inter.client, inter.guild_id, e)
    await core.reply(inter, embed=e, ephemeral=True)


class PanelModal(discord.ui.Modal):
    perm = "economy"

    async def on_submit(self, inter: discord.Interaction):
        try:
            c = await need(inter, self.perm)
            await self.run(inter, c)
        except EconError as err:
            await core.reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)

    async def on_error(self, inter, error):
        logger.exception("panel action failed", exc_info=error)
        await core.reply(inter, embed=emb("❌ Something went wrong — check the bot logs.", None, RED), ephemeral=True)


# ───────── actions ─────────
class LookupModal(PanelModal, title="👤 Player lookup"):
    user = user_field()

    async def run(self, inter, c):
        u = picked(self.user)
        gid = inter.guild_id
        await ensure_user(gid, u.id)
        acc = await get_user(gid, u.id)
        items = await q("SELECT name, qty FROM user_items WHERE guild_id=%s AND user_id=%s AND qty>0 ORDER BY name", gid, u.id)
        biz = await q("SELECT name, balance FROM orgs WHERE guild_id=%s AND owner_id=%s ORDER BY name", gid, u.id)
        cits = await q1("SELECT COUNT(*) n, COALESCE(SUM(amount+late_fee),0) s FROM citations WHERE guild_id=%s AND user_id=%s AND status='unpaid'", gid, u.id)
        e = emb(f"👤 {u.display_name}", f"{u.mention} · `{u.id}`" + ("\n🧊 **Account frozen**" if acc["frozen"] else ""), BLUE)
        e.add_field(name="💵 Wallet", value=money(c, acc["wallet"]))
        e.add_field(name="🏦 Bank", value=money(c, acc["bank"]))
        e.add_field(name="🧾 Unpaid citations", value=f"{cits['n']} ({money(c, cits['s'])})")
        if biz:
            e.add_field(name="🏢 Owns", value="\n".join(f"**{b['name']}** — {money(c, b['balance'])}" for b in biz)[:1024], inline=False)
        e.add_field(name=f"🎒 Inventory ({len(items)})", value=", ".join(f"{i['name']} ×{i['qty']}" for i in items)[:1024] or "Empty", inline=False)
        await done(inter, e, public_log=False)


class MoneyModal(PanelModal):
    user = user_field(desc="Pick a player — OR leave empty and type a business below", required=False)
    org = text_field("Business / department", "Part of the name is fine", "e.g. black eagle", required=False)
    amount = text_field("Amount", None, "1000, 5k, 2.5m")
    into = choice_field("Account (players only)", [("Bank", "bank"), ("Wallet", "wallet")])
    reason = text_field("Reason", None, "optional", required=False)
    op = "print"

    async def run(self, inter, c):
        gid, u, oq = inter.guild_id, picked(self.user), text(self.org)
        if bool(u) == bool(oq):
            raise EconError("Pick a player **or** type a business — not both, not neither.")
        if u:
            await ensure_user(gid, u.id)
            where = self.into.component.values[0] if self.into.component.values else "bank"
            ref, label = (where, u.id), f"{u.mention} ({where})"
            cur = (await get_user(gid, u.id))[where]
        else:
            o = await match_org(gid, oq)
            ref, label, cur = ("org", o["id"]), f"**{o['name']}**", o["balance"]
        reason = text(self.reason)
        if self.op == "print":
            amt = parse_amount(text(self.amount))
            ids = await transfer(gid, [(MINT, ref, amt)], inter.user.id, "admin_print", reason or "Admin print (panel)")
            e = emb("🖨️ Money printed", f"{inter.user.mention} printed **{money(c, amt)}** → {label}", GOLD)
        else:
            amt = parse_amount(text(self.amount), cur)
            ids = await transfer(gid, [(ref, BURN, amt)], inter.user.id, "admin_remove", reason or "Admin remove (panel)")
            e = emb("🔥 Money removed", f"{inter.user.mention} removed **{money(c, amt)}** from {label}", GOLD)
        if reason:
            e.description += f"\n> {reason}"
        e.set_footer(text=f"tx #{ids[0]}")
        await done(inter, e)


class PrintModal(MoneyModal, title="💰 Print money"):
    op = "print"


class RemoveModal(MoneyModal, title="🔥 Remove money"):
    op = "remove"


class MoveItemsModal(PanelModal, title="📦 Move items between players"):
    src = user_field("From player", "Start typing their name")
    dst = user_field("To player", "Start typing their name")
    item = text_field("Item", "Part of the name is fine — leave EMPTY to move everything", required=False)
    qty = text_field("Quantity", "Leave empty for all of it", "all", required=False)

    async def run(self, inter, c):
        gid, src, dst = inter.guild_id, picked(self.src), picked(self.dst)
        if dst.bot or src.id == dst.id:
            raise EconError("Pick two different (human) players.")
        item = None
        if text(self.item):
            names = [r["name"] for r in await q("SELECT name FROM user_items WHERE guild_id=%s AND user_id=%s AND qty>0", gid, src.id)]
            item = best_match(text(self.item), names, f"item in {src.display_name}'s inventory")
        qs = text(self.qty)
        if qs and not qs.isdigit():
            raise EconError("Quantity must be a whole number.")
        moved = await move_user_items(gid, src.id, dst.id, inter.user.id, item, int(qs) if qs else None)
        await done(inter, emb("📦 Items moved", f"{inter.user.mention} moved {src.mention} → {dst.mention}:\n"
                              + "\n".join(f"• {m}" for m in moved)[:3800], GOLD))


class GiveItemModal(PanelModal, title="🎁 Give item from business stock"):
    perm = "orgs"
    org = text_field("Business / department", "Part of the name is fine", "e.g. black eagle")
    item = text_field("Item", "Part of the name is fine", "e.g. m4")
    user = user_field("Give to player")
    qty = text_field("Quantity", None, "1", required=False, default="1")

    async def run(self, inter, c):
        gid = inter.guild_id
        o = await match_org(gid, text(self.org))
        rows = await q("SELECT * FROM org_items WHERE org_id=%s", o["id"])
        it = next(r for r in rows if r["name"] == best_match(text(self.item), [r["name"] for r in rows], f"item in {o['name']}"))
        u, qs = picked(self.user), text(self.qty) or "1"
        if not qs.isdigit() or int(qs) < 1:
            raise EconError("Quantity must be a whole number.")
        qty = int(qs)
        await ensure_user(gid, u.id)

        async def extra(cur):
            await take_stock(cur, it["id"], qty)
            await give_user_item(cur, gid, u.id, it, qty, o["name"])
        await transfer(gid, [], inter.user.id, "issue_item", extra=extra)
        await done(inter, emb("🎁 Item issued", f"{inter.user.mention} gave **{qty}× {it['name']}** from **{o['name']}** to {u.mention}", GREEN))


class ShopItemModal(PanelModal, title="🛒 Add / edit shop item"):
    perm = "orgs"
    org = text_field("Business / department", "Part of the name is fine", "e.g. yellowjack")
    item = text_field("Item name", "Existing item (part of name) is edited, otherwise a new one is created")
    price = text_field("Price", None, "e.g. 2500")
    stock = text_field("Stock", "Leave empty for unlimited", "e.g. 50", required=False)
    desc = text_field("Description", None, "optional", required=False, long=True)

    async def run(self, inter, c):
        gid = inter.guild_id
        o = await match_org(gid, text(self.org))
        price = parse_amount(text(self.price)) if text(self.price) not in ("0", "") else 0
        st = text(self.stock)
        if st and not st.isdigit():
            raise EconError("Stock must be a whole number (or empty for unlimited).")
        stock = int(st) if st else None
        names = [r["name"] for r in await q("SELECT name FROM org_items WHERE org_id=%s", o["id"])]
        try:
            name = best_match(text(self.item), names, "item") if names else None
        except EconError:
            name = None
        if name and (name.lower() == text(self.item).lower() or text(self.item).lower() in name.lower()):
            await ex("UPDATE org_items SET price=%s, stock=%s, description=COALESCE(NULLIF(%s,''),description) WHERE org_id=%s AND name=%s",
                     price, stock, text(self.desc), o["id"], name)
            title = f"✏️ Shop item updated: {name}"
        else:
            name = text(self.item)[:64]
            await ex("INSERT INTO org_items (org_id,name,description,price,stock,for_sale,consumable) VALUES (%s,%s,%s,%s,%s,1,0)",
                     o["id"], name, text(self.desc) or None, price, stock)
            title = f"➕ Shop item added: {name}"
        await done(inter, emb(title, f"**{o['name']}** · {money(c, price)} · stock {stock if stock is not None else '∞'}"
                              f"\nby {inter.user.mention}", GREEN))


class OrgInfoModal(PanelModal, title="🏢 Business / department info"):
    perm = "orgs"
    org = text_field("Business / department", "Part of the name is fine", "e.g. used cars")

    async def run(self, inter, c):
        o = await match_org(inter.guild_id, text(self.org))
        items = await q("SELECT * FROM org_items WHERE org_id=%s ORDER BY name", o["id"])
        staff = await q1("SELECT COUNT(*) n FROM payroll WHERE org_id=%s", o["id"])
        e = emb(f"{'🏢' if o['kind'] == 'business' else '🏛️'} {o['name']}", o["description"] or None, BLUE)
        e.add_field(name="Balance", value=money(c, o["balance"]))
        e.add_field(name="Owner", value=f"<@{o['owner_id']}>" if o["owner_id"] else "—")
        e.add_field(name="On payroll", value=str(staff["n"]))
        e.add_field(name=f"Items ({len(items)})", value="\n".join(
            f"• {i['name']} — {money(c, i['price'])} · stock {'∞' if i['stock'] is None else i['stock']}" for i in items)[:1024] or "None",
            inline=False)
        await done(inter, e, public_log=False)


class OwnerModal(PanelModal, title="👑 Set business owner"):
    perm = "orgs"
    org = text_field("Business", "Part of the name is fine", "e.g. ls tires")
    user = user_field("New owner")

    async def run(self, inter, c):
        o = await match_org(inter.guild_id, text(self.org), "business")
        u = picked(self.user)
        await ex("UPDATE orgs SET owner_id=%s WHERE id=%s", u.id, o["id"])
        await done(inter, emb("👑 Owner changed", f"**{o['name']}** is now owned by {u.mention} (set by {inter.user.mention})", GOLD))


class FreezeModal(PanelModal, title="🧊 Freeze / unfreeze account"):
    user = user_field()
    mode = choice_field("Action", [("Freeze (block spending)", "1"), ("Unfreeze", "0")])

    async def run(self, inter, c):
        u = picked(self.user)
        frozen = (self.mode.component.values or ["1"])[0] == "1"
        await ensure_user(inter.guild_id, u.id)
        await ex("UPDATE users SET frozen=%s WHERE guild_id=%s AND user_id=%s", frozen, inter.guild_id, u.id)
        await done(inter, emb("🧊 Account frozen" if frozen else "♨️ Account unfrozen", f"{u.mention} by {inter.user.mention}", GOLD))


class AuditModal(PanelModal, title="🧾 Recent transactions"):
    user = user_field(desc="Leave empty for the whole server", required=False)

    async def run(self, inter, c):
        gid, u = inter.guild_id, picked(self.user)
        if u:
            rows = await q("SELECT * FROM ledger WHERE guild_id=%s AND ((src_type IN ('wallet','bank') AND src_id=%s) OR "
                           "(dst_type IN ('wallet','bank') AND dst_id=%s)) ORDER BY id DESC LIMIT 20", gid, u.id, u.id)
        else:
            rows = await q("SELECT * FROM ledger WHERE guild_id=%s ORDER BY id DESC LIMIT 20", gid)
        lines = [f"`#{r['id']}` {core.ts(r['ts'], 'R')} **{money(c, r['amount'])}** {r['kind']}"
                 f" · {r['src_type']}→{r['dst_type']}" + (f" — {r['memo'][:40]}" if r["memo"] else "") for r in rows]
        await done(inter, emb(f"🧾 Transactions{' — ' + u.display_name if u else ''}", "\n".join(lines)[:4000] or "None.", BLUE),
                   public_log=False)


class WipeModal(PanelModal, title="🧹 Wipe a player's account"):
    user = user_field()
    confirm = text_field("Type WIPE to confirm", "Deletes money, items, citations and owned businesses. Can't be undone.", "WIPE")

    async def run(self, inter, c):
        if text(self.confirm) != "WIPE":
            raise EconError("Not wiped — you must type WIPE exactly.")
        gid, u = inter.guild_id, picked(self.user)
        biz = await q("SELECT id FROM orgs WHERE guild_id=%s AND kind='business' AND owner_id=%s", gid, u.id)
        for b in biz:
            await ex("DELETE FROM orgs WHERE id=%s", b["id"])
        for t in ("users", "user_items", "citations", "role_grants"):
            await ex(f"DELETE FROM {t} WHERE guild_id=%s AND user_id=%s", gid, u.id)
        await ex("DELETE p FROM payroll p JOIN orgs o ON o.id=p.org_id WHERE o.guild_id=%s AND p.user_id=%s", gid, u.id)
        await done(inter, emb("🧹 Account wiped", f"{u.mention} wiped by {inter.user.mention} ({len(biz)} businesses removed)", RED))


ACTIONS = [  # (custom_id, label, emoji, style, modal, row)
    ("lookup", "Player lookup", "👤", discord.ButtonStyle.primary, LookupModal, 0),
    ("print", "Print money", "💰", discord.ButtonStyle.success, PrintModal, 0),
    ("remove", "Remove money", "🔥", discord.ButtonStyle.danger, RemoveModal, 0),
    ("freeze", "Freeze / unfreeze", "🧊", discord.ButtonStyle.secondary, FreezeModal, 0),
    ("move", "Move items", "📦", discord.ButtonStyle.primary, MoveItemsModal, 1),
    ("give", "Give item from stock", "🎁", discord.ButtonStyle.success, GiveItemModal, 1),
    ("shop", "Add / edit shop item", "🛒", discord.ButtonStyle.secondary, ShopItemModal, 1),
    ("org", "Business info", "🏢", discord.ButtonStyle.primary, OrgInfoModal, 2),
    ("owner", "Set business owner", "👑", discord.ButtonStyle.secondary, OwnerModal, 2),
    ("audit", "Transactions", "🧾", discord.ButtonStyle.secondary, AuditModal, 2),
    ("wipe", "Wipe player", "🧹", discord.ButtonStyle.danger, WipeModal, 3),
]


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for cid, label, emoji, style, modal, row in ACTIONS:
            b = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"staffpanel:{cid}", row=row)
            b.callback = self._opener(modal)
            self.add_item(b)

    @staticmethod
    def _opener(modal_cls):
        async def cb(inter: discord.Interaction):
            c = await cfg(inter.guild_id)
            if not is_admin(inter.user, c, modal_cls.perm):
                return await inter.response.send_message(
                    embed=emb(f"❌ You need the **{core.STAFF_PERMS[modal_cls.perm][0]}** staff permission.", None, RED), ephemeral=True)
            await inter.response.send_modal(modal_cls())
        return cb


def panel_embed():
    e = emb("🛠️ Staff panel", "Click a button — a form pops up.\n"
            "• **Player** boxes: start typing a name and pick from the list.\n"
            "• **Business / item** boxes: type part of the name — the closest match is used "
            "(and you'll see which one in the reply).\n"
            "Replies are only visible to you; money/item changes are also posted to the economy log.", GOLD)
    e.set_footer(text="Only staff can see this channel · every action re-checks your staff permission")
    return e


class StaffPanel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.view = PanelView()
        bot.add_view(self.view)
        self.ready_once = False

    @property
    def conf(self):
        return self.bot.state.settings.setdefault("staff_panel", {})

    async def overwrites(self, guild: discord.Guild):
        c = await cfg(guild.id)
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
              guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                    read_message_history=True, manage_messages=True)}
        for rk in c.get("staff_ranks") or []:
            if not rk.get("perms"):
                continue
            for rid in core._ids(rk.get("roles")):
                role = guild.get_role(rid)
                if role:
                    ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
            for uid in core._ids(rk.get("users")):
                m = guild.get_member(uid)
                if m:
                    ow[m] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
        for uid in core.BOT_OWNERS | core._ids(self.bot.state.settings.get("owner_ids")):
            m = guild.get_member(uid)
            if m:
                ow[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        return ow

    async def ensure_panel(self, repost=False):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            ch = g.get_channel(int(self.conf.get("channel_id") or 0))
            ow = await self.overwrites(g)
            if not ch:
                c = await cfg(gid)
                logch = g.get_channel(c.get("log_channel") or 0)
                ch = await g.create_text_channel(CHANNEL_NAME, overwrites=ow, category=getattr(logch, "category", None),
                                                 topic="Staff-only control panel for the economy bot", reason="Staff panel")
                self.conf.update(channel_id=ch.id, message_id=None)
                logger.info("created staff panel channel #%s (%s)", ch.name, ch.id)
            else:
                await ch.edit(overwrites=ow, reason="Staff panel permissions sync")
            msg = None
            if self.conf.get("message_id") and not repost:
                try:
                    msg = await ch.fetch_message(int(self.conf["message_id"]))
                    await msg.edit(embed=panel_embed(), view=self.view)
                except discord.NotFound:
                    msg = None
            if not msg:
                msg = await ch.send(embed=panel_embed(), view=self.view)
                self.conf["message_id"] = msg.id
            self.bot.state.save()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.ready_once:
            return
        self.ready_once = True
        try:
            await self.ensure_panel()
        except discord.Forbidden:
            logger.warning("staff panel: missing Manage Channels permission")
        except Exception:
            logger.exception("staff panel setup failed")

    @app_commands.command(name="staff_panel", description="(Re)post the staff button panel and re-sync who can see it")
    @app_commands.guild_only()
    async def staff_panel(self, inter: discord.Interaction):
        await need(inter, "setup")
        await inter.response.defer(ephemeral=True)
        await self.ensure_panel(repost=True)
        await inter.followup.send(f"✅ Staff panel is in <#{self.conf['channel_id']}>.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(StaffPanel(bot))
