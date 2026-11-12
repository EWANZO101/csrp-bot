import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands

from core import (EconError, q, q1, cfg, money, emb, ts, transfer, ensure_user, find_org, log, dm, reply,
                  BURN, GREEN, BLUE, GOLD)
from cogs.orgs import org_autocomplete, item_autocomplete, get_item, take_stock, give_user_item, item_line


async def inv_ac(inter: discord.Interaction, current: str):
    rows = await q("SELECT name, qty FROM user_items WHERE guild_id=%s AND user_id=%s AND qty>0 AND name LIKE %s "
                   "ORDER BY name LIMIT 25", inter.guild_id, inter.user.id, f"%{current}%")
    return [app_commands.Choice(name=f"{r['name']} (×{r['qty']})", value=r["name"]) for r in rows]


async def shop_ac(inter: discord.Interaction, current: str):
    rows = await q("SELECT DISTINCT o.name FROM orgs o JOIN org_items i ON i.org_id=o.id WHERE o.guild_id=%s "
                   "AND i.for_sale=1 AND o.name LIKE %s ORDER BY o.name LIMIT 25", inter.guild_id, f"%{current}%")
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


async def shop_item_ac(inter: discord.Interaction, current: str):
    name = getattr(inter.namespace, "store", None)
    if not name:
        return []
    c = await cfg(inter.guild_id)
    rows = await q("SELECT i.name, i.price, i.stock FROM org_items i JOIN orgs o ON o.id=i.org_id WHERE o.guild_id=%s "
                   "AND o.name=%s AND i.for_sale=1 AND i.name LIKE %s ORDER BY i.name LIMIT 25",
                   inter.guild_id, name, f"%{current}%")
    return [app_commands.Choice(name=f"{r['name']} — {money(c, r['price'])}"[:100], value=r["name"]) for r in rows]


async def do_buy(inter, store_name, item_name, qty):
    gid = inter.guild_id
    c = await cfg(gid)
    o = await find_org(gid, store_name)
    it = await get_item(o, item_name)
    if not it["for_sale"]:
        raise EconError("That item isn't for sale.")
    await ensure_user(gid, inter.user.id)
    total = it["price"] * qty
    tax = total * int(c["sales_tax"] or 0) // 100
    tax_dst = ("org", c["treasury_dept"]) if c["treasury_dept"] else BURN
    legs = [(("bank", inter.user.id), ("org", o["id"]), total - tax), (("bank", inter.user.id), tax_dst, tax)]

    async def extra(cur):
        await take_stock(cur, it["id"], qty)
        await give_user_item(cur, gid, inter.user.id, it, qty, o["name"])

    if total == 0:
        await transfer(gid, [], inter.user.id, "purchase", extra=extra)
    else:
        await transfer(gid, legs, inter.user.id, "purchase", f"{qty}× {it['name']} @ {o['name']}", extra=extra)
    e = emb("🛒 Purchase", f"{inter.user.mention} bought **{qty}× {it['name']}** from **{o['name']}** "
            f"for **{money(c, total)}**" + (f" (incl. {money(c, tax)} tax)" if tax else ""), GREEN)
    await log(inter.client, gid, e)
    if o["owner_id"] and o["owner_id"] != inter.user.id:
        await dm(inter.client, o["owner_id"], emb("🛒 Sale", f"{inter.user} bought {qty}× **{it['name']}** at "
                 f"**{o['name']}** for {money(c, total)}", GREEN), gid)
    return e


class BuyView(discord.ui.View):
    def __init__(self, store, items, c):
        super().__init__(timeout=300)
        self.store = store
        opts = [discord.SelectOption(label=i["name"][:100], value=i["name"],
                                     description=f"{money(c, i['price'])} · stock {'∞' if i['stock'] is None else i['stock']}"[:100])
                for i in items[:25]]
        sel = discord.ui.Select(placeholder="Buy an item…", options=opts)
        sel.callback = self.on_pick
        self.add_item(sel)

    async def on_pick(self, inter):
        e = await do_buy(inter, self.store, inter.data["values"][0], 1)
        await inter.response.send_message(embed=e, ephemeral=True)


class Items(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    shop = app_commands.Group(name="shop", description="Buy from businesses", guild_only=True)

    @shop.command(name="browse", description="See what a business sells (with buy menu)")
    @app_commands.autocomplete(store=shop_ac)
    async def browse(self, inter: discord.Interaction, store: str):
        c = await cfg(inter.guild_id)
        o = await find_org(inter.guild_id, store)
        rows = await q("SELECT * FROM org_items WHERE org_id=%s AND for_sale=1 ORDER BY price, name", o["id"])
        if not rows:
            raise EconError(f"**{o['name']}** isn't selling anything right now.")
        e = emb(f"🛍️ {o['name']}", (o["description"] or "") + "\n\n" + "\n".join(item_line(c, r) for r in rows)[:3800], BLUE)
        if c["sales_tax"]:
            e.set_footer(text=f"Sales tax {c['sales_tax']}%")
        await reply(inter, embed=e, view=BuyView(o["name"], rows, c), ephemeral=True)

    @shop.command(name="buy", description="Buy an item from a business")
    @app_commands.autocomplete(store=shop_ac, item=shop_item_ac)
    async def buy(self, inter: discord.Interaction, store: str, item: str,
                  quantity: app_commands.Range[int, 1, 1000] = 1):
        await reply(inter, embed=await do_buy(inter, store, item, quantity))

    @shop.command(name="all", description="Every item for sale across all businesses")
    async def all_(self, inter: discord.Interaction):
        c = await cfg(inter.guild_id)
        rows = await q("SELECT i.*, o.name org FROM org_items i JOIN orgs o ON o.id=i.org_id WHERE o.guild_id=%s "
                       "AND i.for_sale=1 ORDER BY o.name, i.price LIMIT 80", inter.guild_id)
        lines, cur = [], None
        for r in rows:
            if r["org"] != cur:
                cur = r["org"]
                lines.append(f"\n__**{cur}**__")
            lines.append(f"• {r['name']} — {money(c, r['price'])}")
        await reply(inter, embed=emb("🛍️ Marketplace", "\n".join(lines)[:4000] or "Nothing for sale.", BLUE), ephemeral=True)

    @app_commands.command(name="inventory", description="Show your inventory or someone else's")
    @app_commands.guild_only()
    async def inventory(self, inter: discord.Interaction, user: discord.Member | None = None):
        user = user or inter.user
        rows = await q("SELECT * FROM user_items WHERE guild_id=%s AND user_id=%s AND qty>0 ORDER BY name",
                       inter.guild_id, user.id)
        grants = await q("SELECT * FROM role_grants WHERE guild_id=%s AND user_id=%s ORDER BY expires_at",
                         inter.guild_id, user.id)
        lines = [f"**{r['name']}** ×{r['qty']}" + (f" · from {r['source']}" if r["source"] else "")
                 + (f"\n  ↳ {r['description'][:80]}" if r["description"] else "") for r in rows]
        e = emb(f"🎒 {user.display_name}'s inventory", "\n".join(lines)[:3500] or "Empty.", BLUE)
        if grants:
            e.add_field(name="⏳ Active timed items", value="\n".join(
                f"<@&{g['role_id']}> ({g['item'] or 'item'}) expires {ts(g['expires_at'])}" for g in grants)[:1024], inline=False)
        await reply(inter, embed=e)

    @app_commands.command(name="give", description="Give an inventory item to another user")
    @app_commands.autocomplete(item=inv_ac)
    @app_commands.guild_only()
    async def give(self, inter: discord.Interaction, user: discord.Member, item: str,
                   quantity: app_commands.Range[int, 1, 1000] = 1):
        if user.bot or user.id == inter.user.id:
            raise EconError("Pick another (human) user.")
        gid = inter.guild_id
        await ensure_user(gid, user.id)

        async def extra(cur):
            await cur.execute("SELECT * FROM user_items WHERE guild_id=%s AND user_id=%s AND name=%s FOR UPDATE",
                              (gid, inter.user.id, item))
            it = await cur.fetchone()
            if not it or it["qty"] < quantity:
                raise EconError(f"You don't have {quantity}× **{item}**.")
            await cur.execute("UPDATE user_items SET qty=qty-%s WHERE id=%s", (quantity, it["id"]))
            await give_user_item(cur, gid, user.id, it, quantity, it["source"])
        await transfer(gid, [], inter.user.id, "give_item", extra=extra)
        e = emb("🎁 Item given", f"{inter.user.mention} gave **{quantity}× {item}** to {user.mention}", GREEN)
        await log(inter.client, gid, e)
        await reply(inter, embed=e)

    @app_commands.command(name="use", description="Use an item from your inventory")
    @app_commands.autocomplete(item=inv_ac)
    @app_commands.guild_only()
    async def use(self, inter: discord.Interaction, item: str):
        gid = inter.guild_id
        it = await q1("SELECT * FROM user_items WHERE guild_id=%s AND user_id=%s AND name=%s AND qty>0",
                      gid, inter.user.id, item)
        if not it:
            raise EconError(f"You don't have **{item}**.")
        role_note = ""
        if it["role_id"]:
            role = inter.guild.get_role(it["role_id"])
            if not role:
                raise EconError("The role this item grants no longer exists.")
            try:
                await inter.user.add_roles(role, reason=f"Used item {item}")
            except discord.Forbidden:
                raise EconError("I don't have permission to give that role — ask staff to move my role higher.")
            if it["role_days"]:
                exp = dt.datetime.utcnow() + dt.timedelta(days=it["role_days"])
                existing = await q1("SELECT * FROM role_grants WHERE guild_id=%s AND user_id=%s AND role_id=%s",
                                    gid, inter.user.id, role.id)
                if existing:  # extend
                    exp = max(existing["expires_at"], dt.datetime.utcnow()) + dt.timedelta(days=it["role_days"])
                    await q("UPDATE role_grants SET expires_at=%s WHERE id=%s", exp, existing["id"])
                else:
                    await q("INSERT INTO role_grants (guild_id,user_id,role_id,item,expires_at) VALUES (%s,%s,%s,%s,%s)",
                            gid, inter.user.id, role.id, item, exp)
                role_note = f"\nYou now have {role.mention} until {ts(exp, 'f')}."
            else:
                role_note = f"\nYou now have {role.mention}."
        if it["consumable"]:
            await q("UPDATE user_items SET qty=qty-1 WHERE id=%s", it["id"])
        msg = (it["use_message"] or f"used **{item}**.").replace("{user}", inter.user.mention)
        e = emb(None, f"{inter.user.mention} {msg}" if "{user}" not in (it["use_message"] or "") else msg, GREEN)
        e.description += role_note
        await reply(inter, embed=e)


async def setup(bot):
    await bot.add_cog(Items(bot))
