"""/business and /department — share one implementation."""
import discord
from discord import app_commands

from core import (EconError, q, q1, ex, cfg, money, emb, parse_amount, transfer, ensure_user, get_user,
                  is_admin, org_level, require_level, require_admin, find_org, log, dm, reply, confirm,
                  MINT, BURN, GREEN, RED, BLUE, GOLD, MEMBER, HEAD, ADMIN, KIND_LABEL)

LEVELS = [app_commands.Choice(name="Head (manage)", value="head"), app_commands.Choice(name="Member", value="member")]


def org_autocomplete(kind, managed=False):
    async def ac(inter: discord.Interaction, current: str):
        rows = await q("SELECT * FROM orgs WHERE guild_id=%s AND kind=%s AND name LIKE %s ORDER BY name LIMIT 50",
                       inter.guild_id, kind, f"%{current}%")
        out = []
        for r in rows:
            if managed and await org_level(inter.user, r) < HEAD:
                continue
            out.append(app_commands.Choice(name=r["name"], value=r["name"]))
        return out[:25]
    return ac


def item_autocomplete(param):
    async def ac(inter: discord.Interaction, current: str):
        name = getattr(inter.namespace, param, None)
        if not name:
            return []
        rows = await q("SELECT i.name, i.price FROM org_items i JOIN orgs o ON o.id=i.org_id "
                       "WHERE o.guild_id=%s AND o.name=%s AND i.name LIKE %s ORDER BY i.name LIMIT 25",
                       inter.guild_id, name, f"%{current}%")
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]
    return ac


async def get_item(org, name):
    it = await q1("SELECT * FROM org_items WHERE org_id=%s AND name=%s", org["id"], name)
    if not it:
        raise EconError(f"**{org['name']}** has no item called **{name}**.")
    return it


def item_line(c, it, show_price=True):
    stock = "∞" if it["stock"] is None else it["stock"]
    bits = [f"**{it['name']}**"]
    if show_price:
        bits.append(money(c, it["price"]))
    bits.append(f"stock {stock}")
    if not it["for_sale"]:
        bits.append("not for sale")
    if it["role_id"]:
        bits.append(f"grants <@&{it['role_id']}>" + (f" for {it['role_days']}d" if it["role_days"] else ""))
    line = " · ".join(bits)
    if it["description"]:
        line += f"\n  ↳ {it['description'][:90]}"
    return line


async def give_user_item(cur, gid, uid, it, qty, source):
    await cur.execute(
        "INSERT INTO user_items (guild_id,user_id,name,description,consumable,use_message,role_id,role_days,source,qty) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE qty=qty+VALUES(qty), "
        "description=VALUES(description), consumable=VALUES(consumable), use_message=VALUES(use_message), "
        "role_id=VALUES(role_id), role_days=VALUES(role_days)",
        (gid, uid, it["name"], it["description"], it["consumable"], it["use_message"], it["role_id"], it["role_days"],
         source, qty))


async def take_stock(cur, item_id, qty):
    await cur.execute("SELECT stock FROM org_items WHERE id=%s FOR UPDATE", (item_id,))
    row = await cur.fetchone()
    if not row:
        raise EconError("That item no longer exists.")
    if row["stock"] is not None:
        if row["stock"] < qty:
            raise EconError(f"Only {row['stock']} left in stock.")
        await cur.execute("UPDATE org_items SET stock=stock-%s WHERE id=%s", (qty, item_id))


async def org_embed(inter, org):
    c = await cfg(inter.guild_id)
    lvl = await org_level(inter.user, org)
    icon = "🏢" if org["kind"] == "business" else "🏛️"
    e = emb(f"{icon} {org['name']}", org["description"] or None, BLUE)
    if org["owner_id"]:
        e.add_field(name="Owner", value=f"<@{org['owner_id']}>")
    if lvl >= MEMBER:
        e.add_field(name="Balance", value=money(c, org["balance"]))
    roles = await q("SELECT * FROM org_roles WHERE org_id=%s", org["id"])
    heads = " ".join(f"<@&{r['role_id']}>" for r in roles if r["level"] == "head") or "—"
    mems = " ".join(f"<@&{r['role_id']}>" for r in roles if r["level"] == "member") or "—"
    e.add_field(name="Head roles", value=heads, inline=False)
    e.add_field(name="Member roles", value=mems, inline=False)
    pay = await q1("SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM payroll WHERE org_id=%s", org["id"])
    e.add_field(name="Payroll", value=f"{pay['n']} people" + (f" · {money(c, int(pay['s']))}/cycle" if lvl >= MEMBER else ""))
    items = await q("SELECT * FROM org_items WHERE org_id=%s ORDER BY name LIMIT 15", org["id"])
    if items:
        e.add_field(name="Items", value="\n".join(item_line(c, i) for i in items)[:1024], inline=False)
    e.set_footer(text=f"ID #{org['id']}")
    return e


def build(kind: str) -> app_commands.Group:
    label = "business" if kind == "business" else "department"
    L = KIND_LABEL[kind]
    grp = app_commands.Group(name=label, description=f"{L} management", guild_only=True)
    items = app_commands.Group(name="item", description=f"{L} items", parent=grp)
    roles = app_commands.Group(name="role", description=f"{L} role access", parent=grp)
    ac_any, ac_managed, ac_item = org_autocomplete(kind), org_autocomplete(kind, True), item_autocomplete(label)

    def org_param(f):
        f = app_commands.rename(org=label)(f)
        return f

    # ───────── create / delete / list / info ─────────
    if kind == "business":
        @grp.command(name="create", description="Open a new business")
        @app_commands.describe(name="Business name", description="Short description",
                               owner="Owner (admins only; defaults to you)")
        async def create(inter: discord.Interaction, name: app_commands.Range[str, 2, 64],
                         description: app_commands.Range[str, 0, 500] | None = None,
                         owner: discord.Member | None = None):
            gid = inter.guild_id
            c = await cfg(gid)
            admin = is_admin(inter.user, c, "orgs")
            owner = owner if (owner and admin) else inter.user
            await ensure_user(gid, owner.id)
            if not admin:
                n = await q1("SELECT COUNT(*) n FROM orgs WHERE guild_id=%s AND kind='business' AND owner_id=%s",
                             gid, owner.id)
                if n["n"] >= c["max_businesses"]:
                    raise EconError(f"You already own the max of {c['max_businesses']} businesses.")
            if await q1("SELECT 1 FROM orgs WHERE guild_id=%s AND kind='business' AND name=%s", gid, name):
                raise EconError("A business with that name already exists.")
            fee = 0 if admin else c["business_cost"]
            if fee:
                dst = ("org", c["treasury_dept"]) if c["treasury_dept"] else BURN
                await transfer(gid, [(("bank", owner.id), dst, fee)], inter.user.id, "business_fee", f"Open {name}")
            _, oid = await ex("INSERT INTO orgs (guild_id,kind,name,owner_id,description) VALUES (%s,'business',%s,%s,%s)",
                              gid, name, owner.id, description)
            e = emb("🏢 Business opened", f"**{name}** owned by {owner.mention}" + (f"\nFee: {money(c, fee)}" if fee else ""), GREEN)
            await log(inter.client, gid, e)
            await reply(inter, embed=e)
    else:
        @grp.command(name="create", description="Create a department (admin)")
        @app_commands.describe(name="Department name", head_role="Role that manages it", member_role="Role for members")
        async def create(inter: discord.Interaction, name: app_commands.Range[str, 2, 64],
                         head_role: discord.Role | None = None, member_role: discord.Role | None = None,
                         description: app_commands.Range[str, 0, 500] | None = None):
            await require_admin(inter, "orgs")
            gid = inter.guild_id
            if await q1("SELECT 1 FROM orgs WHERE guild_id=%s AND kind='dept' AND name=%s", gid, name):
                raise EconError("A department with that name already exists.")
            _, oid = await ex("INSERT INTO orgs (guild_id,kind,name,description) VALUES (%s,'dept',%s,%s)",
                              gid, name, description)
            for r, lv in ((head_role, "head"), (member_role, "member")):
                if r:
                    await ex("INSERT IGNORE INTO org_roles VALUES (%s,%s,%s)", oid, r.id, lv)
            e = emb("🏛️ Department created", f"**{name}**", GREEN)
            await log(inter.client, gid, e)
            await reply(inter, embed=e)

    @grp.command(name="delete", description=f"Delete a {label} and all its data")
    @org_param
    @app_commands.autocomplete(org=ac_managed)
    async def delete(inter: discord.Interaction, org: str):
        o = await find_org(inter.guild_id, org, kind)
        if kind == "dept":
            await require_admin(inter, "orgs")
        elif await org_level(inter.user, o) < ADMIN and o["owner_id"] != inter.user.id:
            raise EconError("Only the owner or an admin can delete this business.")
        c = await cfg(inter.guild_id)
        refund = kind == "business" and o["owner_id"] and o["balance"] > 0
        if not await confirm(inter, f"Delete **{o['name']}**? Items, roles and payroll are removed.\n"
                             + (f"Remaining {money(c, o['balance'])} goes to <@{o['owner_id']}>'s bank."
                                if refund else f"Remaining balance ({money(c, o['balance'])}) is destroyed."), "Delete"):
            return
        if o["balance"] > 0:
            dst = ("bank", o["owner_id"]) if refund else BURN
            if refund:
                await ensure_user(inter.guild_id, o["owner_id"])
            await transfer(inter.guild_id, [(("org", o["id"]), dst, o["balance"])], inter.user.id, "org_close",
                           f"Closed {o['name']}")
        await ex("DELETE FROM orgs WHERE id=%s", o["id"])
        e = emb(f"🗑️ {L} deleted", f"**{o['name']}** by {inter.user.mention}", RED)
        await log(inter.client, inter.guild_id, e)
        await inter.followup.send(embed=e, ephemeral=True)

    @grp.command(name="list", description=f"List all {label}s")
    async def list_(inter: discord.Interaction):
        rows = await q("SELECT o.*, (SELECT COUNT(*) FROM org_items i WHERE i.org_id=o.id) items, "
                       "(SELECT COUNT(*) FROM payroll p WHERE p.org_id=o.id) staff "
                       "FROM orgs o WHERE guild_id=%s AND kind=%s ORDER BY name", inter.guild_id, kind)
        lines = [f"**{r['name']}**" + (f" · <@{r['owner_id']}>" if r["owner_id"] else "")
                 + f" · {r['items']} items · {r['staff']} on payroll" for r in rows]
        await reply(inter, embed=emb(f"{'🏢' if kind == 'business' else '🏛️'} {L}s ({len(rows)})",
                                     "\n".join(lines)[:4000] or "None yet.", BLUE))

    @grp.command(name="info", description=f"Details, roles and items of a {label}")
    @org_param
    @app_commands.autocomplete(org=ac_any)
    async def info(inter: discord.Interaction, org: str):
        o = await find_org(inter.guild_id, org, kind)
        await reply(inter, embed=await org_embed(inter, o))

    @grp.command(name="inventory", description=f"Full item list of a {label}")
    @org_param
    @app_commands.autocomplete(org=ac_any)
    async def inventory(inter: discord.Interaction, org: str):
        o = await find_org(inter.guild_id, org, kind)
        c = await cfg(inter.guild_id)
        rows = await q("SELECT * FROM org_items WHERE org_id=%s ORDER BY name", o["id"])
        await reply(inter, embed=emb(f"📦 {o['name']} inventory", "\n".join(item_line(c, r) for r in rows)[:4000]
                                     or "No items.", BLUE))

    # ───────── money ─────────
    @grp.command(name="deposit", description=f"Put money from your bank into a {label}")
    @org_param
    @app_commands.autocomplete(org=ac_any)
    async def deposit(inter: discord.Interaction, org: str, amount: str):
        o = await find_org(inter.guild_id, org, kind)
        u = await get_user(inter.guild_id, inter.user.id)
        c = await cfg(inter.guild_id)
        amt = parse_amount(amount, u["bank"])
        await transfer(inter.guild_id, [(("bank", inter.user.id), ("org", o["id"]), amt)], inter.user.id,
                       "org_deposit", f"Deposit to {o['name']}")
        e = emb("📥 Deposit", f"{inter.user.mention} deposited **{money(c, amt)}** into **{o['name']}**", GREEN)
        await log(inter.client, inter.guild_id, e)
        await reply(inter, embed=e, ephemeral=True)

    @grp.command(name="withdraw", description=f"Take money out of a {label} into your bank (head/owner)")
    @org_param
    @app_commands.autocomplete(org=ac_managed)
    async def withdraw(inter: discord.Interaction, org: str, amount: str, reason: str | None = None):
        o = await find_org(inter.guild_id, org, kind)
        await require_level(inter.user, o, HEAD, "withdraw funds")
        c = await cfg(inter.guild_id)
        await ensure_user(inter.guild_id, inter.user.id)
        amt = parse_amount(amount, o["balance"])
        await transfer(inter.guild_id, [(("org", o["id"]), ("bank", inter.user.id), amt)], inter.user.id,
                       "org_withdraw", reason or f"Withdraw from {o['name']}")
        e = emb("📤 Withdrawal", f"{inter.user.mention} withdrew **{money(c, amt)}** from **{o['name']}**"
                + (f"\n> {reason}" if reason else ""), GOLD)
        await log(inter.client, inter.guild_id, e)
        await reply(inter, embed=e, ephemeral=True)

    @grp.command(name="send", description=f"Send money from a {label} to a user, business or department (head/owner)")
    @org_param
    @app_commands.autocomplete(org=ac_managed)
    @app_commands.describe(to_user="Pay a user", to_org="…or a business/department name", reason="Memo")
    async def send(inter: discord.Interaction, org: str, amount: str, to_user: discord.Member | None = None,
                   to_org: str | None = None, reason: str | None = None):
        o = await find_org(inter.guild_id, org, kind)
        await require_level(inter.user, o, HEAD, "send funds")
        c = await cfg(inter.guild_id)
        if bool(to_user) == bool(to_org):
            raise EconError("Pick exactly one of `to_user` or `to_org`.")
        if to_user:
            await ensure_user(inter.guild_id, to_user.id)
            dst, label_ = ("bank", to_user.id), to_user.mention
        else:
            t = await find_org(inter.guild_id, to_org)
            dst, label_ = ("org", t["id"]), f"**{t['name']}**"
        amt = parse_amount(amount, o["balance"])
        await transfer(inter.guild_id, [(("org", o["id"]), dst, amt)], inter.user.id, "org_transfer",
                       reason or f"From {o['name']}")
        e = emb("💸 Org transfer", f"**{o['name']}** → {label_}: **{money(c, amt)}**" + (f"\n> {reason}" if reason else ""),
                GREEN)
        await log(inter.client, inter.guild_id, e)
        await reply(inter, embed=e)

    if kind == "business":
        @grp.command(name="transfer_ownership", description="Hand your business to someone else")
        @org_param
        @app_commands.autocomplete(org=ac_managed)
        async def transfer_ownership(inter: discord.Interaction, org: str, new_owner: discord.Member):
            o = await find_org(inter.guild_id, org, kind)
            if o["owner_id"] != inter.user.id and await org_level(inter.user, o) < ADMIN:
                raise EconError("Only the owner can transfer this business.")
            if not await confirm(inter, f"Give **{o['name']}** to {new_owner.mention}?", "Transfer"):
                return
            await ensure_user(inter.guild_id, new_owner.id)
            await ex("UPDATE orgs SET owner_id=%s WHERE id=%s", new_owner.id, o["id"])
            e = emb("🤝 Ownership transferred", f"**{o['name']}** → {new_owner.mention}", GREEN)
            await log(inter.client, inter.guild_id, e)
            await inter.followup.send(embed=e)

    @grp.command(name="edit", description=f"Rename or re-describe a {label} (head/owner)")
    @org_param
    @app_commands.autocomplete(org=ac_managed)
    async def edit(inter: discord.Interaction, org: str, new_name: app_commands.Range[str, 2, 64] | None = None,
                   description: app_commands.Range[str, 0, 500] | None = None):
        o = await find_org(inter.guild_id, org, kind)
        await require_level(inter.user, o, HEAD, "edit it")
        if new_name:
            if await q1("SELECT 1 FROM orgs WHERE guild_id=%s AND kind=%s AND name=%s AND id<>%s",
                        inter.guild_id, kind, new_name, o["id"]):
                raise EconError("That name is taken.")
            await ex("UPDATE orgs SET name=%s WHERE id=%s", new_name, o["id"])
        if description is not None:
            await ex("UPDATE orgs SET description=%s WHERE id=%s", description, o["id"])
        await reply(inter, embed=await org_embed(inter, await find_org(inter.guild_id, f"#{o['id']}", kind)), ephemeral=True)

    # ───────── roles ─────────
    @roles.command(name="add", description=f"Give a Discord role head or member access to a {label}")
    @org_param
    @app_commands.autocomplete(org=ac_managed)
    @app_commands.choices(level=LEVELS)
    async def role_add(inter: discord.Interaction, org: str, role: discord.Role, level: str):
        o = await find_org(inter.guild_id, org, kind)
        await require_level(inter.user, o, HEAD, "manage roles")
        await ex("INSERT INTO org_roles VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE level=VALUES(level)",
                 o["id"], role.id, level)
        e = emb("🔑 Role added", f"{role.mention} is now **{level}** of **{o['name']}**", GREEN)
        await log(inter.client, inter.guild_id, e)
        await reply(inter, embed=e, ephemeral=True)

    @roles.command(name="remove", description=f"Remove a role's access to a {label}")
    @org_param
    @app_commands.autocomplete(org=ac_managed)
    async def role_remove(inter: discord.Interaction, org: str, role: discord.Role):
        o = await find_org(inter.guild_id, org, kind)
        await require_level(inter.user, o, HEAD, "manage roles")
        n, _ = await ex("DELETE FROM org_roles WHERE org_id=%s AND role_id=%s", o["id"], role.id)
        if not n:
            raise EconError(f"{role.mention} has no access to **{o['name']}**.")
        e = emb("🔑 Role removed", f"{role.mention} removed from **{o['name']}**", GOLD)
        await log(inter.client, inter.guild_id, e)
        await reply(inter, embed=e, ephemeral=True)

    # ───────── items ─────────
    @items.command(name="add", description=f"Add an item to a {label}")
    @org_param
    @app_commands.autocomplete(org=ac_managed)
    @app_commands.describe(price="Price per unit", stock="Leave empty for unlimited", for_sale="Show in /shop",
                           consumable="Used up when /use'd", use_message="Shown when used",
                           role="Role granted when used", role_days="Role expires after N days (empty = permanent)")
    async def item_add(inter: discord.Interaction, org: str, name: app_commands.Range[str, 1, 64], price: str = "0",
                       description: app_commands.Range[str, 0, 300] | None = None,
                       stock: app_commands.Range[int, 0, 10_000_000] | None = None,
                       for_sale: bool | None = None, consumable: bool = True,
                       use_message: app_commands.Range[str, 0, 300] | None = None,
                       role: discord.Role | None = None, role_days: app_commands.Range[int, 1, 3650] | None = None):
        o = await find_org(inter.guild_id, org, kind)
        await require_level(inter.user, o, HEAD, "manage items")
        p = 0 if price.strip() in ("0", "free") else parse_amount(price)
        if role and role >= inter.guild.me.top_role:
            raise EconError(f"I can't hand out {role.mention} — move my bot role above it first.")
        if for_sale is None:
            for_sale = kind == "business"
        try:
            await ex("INSERT INTO org_items (org_id,name,description,price,stock,for_sale,consumable,use_message,role_id,role_days) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", o["id"], name, description, p, stock, for_sale,
                     consumable, use_message, role.id if role else None, role_days)
        except Exception:
            raise EconError(f"**{o['name']}** already has an item called **{name}**.")
        c = await cfg(inter.guild_id)
        it = await get_item(o, name)
        await reply(inter, embed=emb("➕ Item added", f"**{o['name']}**\n{item_line(c, it)}", GREEN))

    @items.command(name="edit", description=f"Edit an item in a {label}")
    @org_param
    @app_commands.autocomplete(org=ac_managed, item=ac_item)
    @app_commands.describe(stock="-1 = unlimited", role_days="0 = permanent")
    async def item_edit(inter: discord.Interaction, org: str, item: str,
                        new_name: app_commands.Range[str, 1, 64] | None = None, price: str | None = None,
                        description: app_commands.Range[str, 0, 300] | None = None,
                        stock: app_commands.Range[int, -1, 10_000_000] | None = None,
                        for_sale: bool | None = None, consumable: bool | None = None,
                        use_message: app_commands.Range[str, 0, 300] | None = None,
                        role: discord.Role | None = None, clear_role: bool = False,
                        role_days: app_commands.Range[int, 0, 3650] | None = None):
        o = await find_org(inter.guild_id, org, kind)
        await require_level(inter.user, o, HEAD, "manage items")
        it = await get_item(o, item)
        sets = {}
        if new_name: sets["name"] = new_name
        if price is not None: sets["price"] = 0 if price.strip() in ("0", "free") else parse_amount(price)
        if description is not None: sets["description"] = description
        if stock is not None: sets["stock"] = None if stock < 0 else stock
        if for_sale is not None: sets["for_sale"] = for_sale
        if consumable is not None: sets["consumable"] = consumable
        if use_message is not None: sets["use_message"] = use_message
        if role:
            if role >= inter.guild.me.top_role:
                raise EconError(f"I can't hand out {role.mention} — move my bot role above it first.")
            sets["role_id"] = role.id
        if clear_role: sets["role_id"] = None
        if role_days is not None: sets["role_days"] = role_days or None
        if not sets:
            raise EconError("Nothing to change.")
        await ex(f"UPDATE org_items SET {', '.join(f'{k}=%s' for k in sets)} WHERE id=%s", *sets.values(), it["id"])
        c = await cfg(inter.guild_id)
        it = await q1("SELECT * FROM org_items WHERE id=%s", it["id"])
        await reply(inter, embed=emb("✏️ Item updated", f"**{o['name']}**\n{item_line(c, it)}", GREEN))

    @items.command(name="remove", description=f"Remove an item from a {label}")
    @org_param
    @app_commands.autocomplete(org=ac_managed, item=ac_item)
    async def item_remove(inter: discord.Interaction, org: str, item: str):
        o = await find_org(inter.guild_id, org, kind)
        await require_level(inter.user, o, HEAD, "manage items")
        it = await get_item(o, item)
        await ex("DELETE FROM org_items WHERE id=%s", it["id"])
        await reply(inter, embed=emb("➖ Item removed", f"**{it['name']}** from **{o['name']}**", GOLD))

    @items.command(name="give", description=f"Issue an item from a {label}'s stock to a user (free)")
    @org_param
    @app_commands.autocomplete(org=ac_managed, item=ac_item)
    async def item_give(inter: discord.Interaction, org: str, item: str, user: discord.Member,
                        quantity: app_commands.Range[int, 1, 1000] = 1):
        o = await find_org(inter.guild_id, org, kind)
        await require_level(inter.user, o, HEAD, "issue items")
        it = await get_item(o, item)
        await ensure_user(inter.guild_id, user.id)

        async def extra(cur):
            await take_stock(cur, it["id"], quantity)
            await give_user_item(cur, inter.guild_id, user.id, it, quantity, o["name"])
        await transfer(inter.guild_id, [], inter.user.id, "issue", extra=extra)
        e = emb("📦 Item issued", f"{quantity}× **{it['name']}** from **{o['name']}** → {user.mention}", GREEN)
        await log(inter.client, inter.guild_id, e)
        await reply(inter, embed=e)

    return grp


async def setup(bot):
    bot.tree.add_command(build("business"))
    bot.tree.add_command(build("dept"))
