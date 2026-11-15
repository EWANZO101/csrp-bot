"""Characters: a player can have several characters, each with its own wallet, bank and inventory.

The ACTIVE character's money and items live in `users` / `user_items` exactly like before, so banking, shops, payroll,
hospital, utilities, the web panel etc. all keep working unchanged. Inactive characters are parked in `characters`
(wallet/bank) and `character_items`. Switching swaps the two inside one DB transaction and writes ledger entries
(kind "character", accounts cwallet/cbank) so every balance still matches its transaction history.

A player's existing account becomes their main character (created automatically the first time it's needed).
Imports from the old bot always go to the main character, even while another one is active.
Businesses, departments, payroll, citations and staff access stay tied to the Discord user.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

import core
from core import (EconError, q, q1, ex, tx, cfg, money, emb, transfer, ensure_user, is_admin, log, reply, confirm,
                  MINT, BURN, GREEN, BLUE, GOLD, RED)

logger = logging.getLogger("economy.characters")
ITEM_COLS = "name,description,consumable,use_message,role_id,role_days,source,qty"


async def ensure_main(gid: int, member: discord.abc.User) -> None:
    """Turn the player's existing account into their main (active) character if they have no characters yet."""
    await ensure_user(gid, member.id)
    if await q1("SELECT id FROM characters WHERE guild_id=%s AND user_id=%s LIMIT 1", gid, member.id):
        return
    name = (getattr(member, "display_name", None) or member.name or "Main").strip()[:64] or "Main"
    await ex("INSERT IGNORE INTO characters (guild_id,user_id,name,is_main,active) VALUES (%s,%s,%s,1,1)",
             gid, member.id, name)


async def characters_of(gid: int, uid: int) -> list[dict]:
    return await q("SELECT * FROM characters WHERE guild_id=%s AND user_id=%s ORDER BY is_main DESC, id", gid, uid)


def find(chars: list[dict], name: str) -> dict:
    n = (name or "").strip().lower()
    for ch in chars:
        if ch["name"].lower() == n:
            return ch
    raise EconError(f"You don't have a character called **{name}**. See `/character list`.")


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not 1 <= len(name) <= 32:
        raise EconError("Character names must be 1–32 characters.")
    if any(ch in name for ch in "@<>`*_~|"):
        raise EconError("Character names can't contain @ < > ` * _ ~ |")
    return name


async def switch_to(gid: int, uid: int, target_name: str, actor_id: int) -> tuple[dict, dict]:
    """Park the active character and load `target_name`. Returns (old, new) character rows."""
    async with core.IMPORT_LOCK:
        async with tx() as cur:
            await cur.execute("SELECT wallet,bank,frozen FROM users WHERE guild_id=%s AND user_id=%s FOR UPDATE", (gid, uid))
            u = await cur.fetchone()
            if not u:
                raise EconError("You don't have an account yet.")
            if u["frozen"]:
                raise EconError("Your account is frozen, so you can't switch characters right now.")
            await cur.execute("SELECT * FROM characters WHERE guild_id=%s AND user_id=%s ORDER BY id FOR UPDATE", (gid, uid))
            chars = await cur.fetchall()
            old = next((c for c in chars if c["active"]), None)
            new = find(chars, target_name)
            if not old:
                raise EconError("Couldn't find your active character — ask staff to check your account.")
            if new["id"] == old["id"]:
                raise EconError(f"**{new['name']}** is already your active character.")

            legs = []
            for col in ("wallet", "bank"):
                live, parked = u[col], new[col]
                if live > 0:
                    legs.append(((col, uid), ("c" + col, old["id"]), live))
                elif live < 0:
                    legs.append((("c" + col, old["id"]), (col, uid), -live))
                if parked > 0:
                    legs.append((("c" + col, new["id"]), (col, uid), parked))
                elif parked < 0:
                    legs.append(((col, uid), ("c" + col, new["id"]), -parked))
            await cur.execute("UPDATE characters SET wallet=%s, bank=%s, active=0 WHERE id=%s", (u["wallet"], u["bank"], old["id"]))
            await cur.execute("UPDATE characters SET wallet=0, bank=0, active=1 WHERE id=%s", (new["id"],))
            await cur.execute("UPDATE users SET wallet=%s, bank=%s WHERE guild_id=%s AND user_id=%s",
                              (new["wallet"], new["bank"], gid, uid))
            memo = f"Switch character: {old['name']} → {new['name']}"[:255]
            for s, d, a in legs:
                await cur.execute(
                    "INSERT INTO ledger (guild_id,actor_id,kind,src_type,src_id,dst_type,dst_id,amount,memo) "
                    "VALUES (%s,%s,'character',%s,%s,%s,%s,%s,%s)", (gid, actor_id, s[0], s[1], d[0], d[1], a, memo))

            # items: park the live inventory on the old character, load the new character's
            await cur.execute("DELETE FROM character_items WHERE char_id=%s", (old["id"],))
            await cur.execute(f"INSERT INTO character_items (char_id,{ITEM_COLS}) SELECT %s,{ITEM_COLS} FROM user_items "
                              "WHERE guild_id=%s AND user_id=%s AND qty>0", (old["id"], gid, uid))
            await cur.execute("DELETE FROM user_items WHERE guild_id=%s AND user_id=%s", (gid, uid))
            await cur.execute(f"INSERT INTO user_items (guild_id,user_id,{ITEM_COLS}) SELECT %s,%s,{ITEM_COLS} "
                              "FROM character_items WHERE char_id=%s", (gid, uid, new["id"]))
            await cur.execute("DELETE FROM character_items WHERE char_id=%s", (new["id"],))
    return old, new


async def char_ac(inter: discord.Interaction, current: str):
    target = getattr(inter.namespace, "user", None)
    uid = target.id if target else inter.user.id
    rows = await q("SELECT name, active, is_main FROM characters WHERE guild_id=%s AND user_id=%s AND name LIKE %s "
                   "ORDER BY is_main DESC, id LIMIT 25", inter.guild_id, uid, f"%{current}%")
    return [app_commands.Choice(name=f"{r['name']}{' (active)' if r['active'] else ''}{' · main' if r['is_main'] else ''}"[:100],
                                value=r["name"]) for r in rows]


class Characters(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    character = app_commands.Group(name="character", description="Your characters — each has its own bank, wallet and inventory",
                                   guild_only=True)

    @character.command(name="list", description="Your characters (staff can view anyone's)")
    async def list_(self, inter: discord.Interaction, user: discord.Member | None = None):
        user = user or inter.user
        c = await cfg(inter.guild_id)
        if user.id != inter.user.id and not is_admin(inter.user, c, "economy"):
            raise EconError("Only staff can view other people's characters.")
        gid = inter.guild_id
        await ensure_main(gid, user)
        live = await q1("SELECT wallet, bank FROM users WHERE guild_id=%s AND user_id=%s", gid, user.id)
        live_items = (await q1("SELECT COUNT(*) n, COALESCE(SUM(qty),0) t FROM user_items WHERE guild_id=%s AND user_id=%s "
                               "AND qty>0", gid, user.id))
        lines = []
        chars = await characters_of(gid, user.id)
        for ch in chars:
            if ch["active"]:
                w, b, n, t = live["wallet"], live["bank"], live_items["n"], live_items["t"]
            else:
                w, b = ch["wallet"], ch["bank"]
                it = await q1("SELECT COUNT(*) n, COALESCE(SUM(qty),0) t FROM character_items WHERE char_id=%s AND qty>0", ch["id"])
                n, t = it["n"], it["t"]
            tags = (" · **active**" if ch["active"] else "") + (" · main (gets old-bot imports)" if ch["is_main"] else "")
            lines.append(f"{'🟢' if ch['active'] else '⚪'} **{ch['name']}**{tags}\n"
                         f"  👛 {money(c, w)} · 🏦 {money(c, b)} · 🎒 {int(t)} item(s) ({n} kinds)")
        e = emb(f"🎭 {user.display_name}'s characters ({len(chars)}/{c.get('max_characters', 3)})", "\n".join(lines), BLUE)
        e.set_footer(text="/character create · /character switch · /character rename · /character delete")
        await reply(inter, embed=e, ephemeral=True)

    @character.command(name="create", description="Make a new character with its own bank and inventory")
    @app_commands.describe(name="Character name (1–32 characters)", switch_now="Switch to the new character straight away")
    async def create(self, inter: discord.Interaction, name: str, switch_now: bool = True):
        gid, uid = inter.guild_id, inter.user.id
        c = await cfg(gid)
        name = clean_name(name)
        await ensure_main(gid, inter.user)
        chars = await characters_of(gid, uid)
        limit = int(c.get("max_characters", 3) or 0)
        if len(chars) >= limit:
            raise EconError(f"You already have the maximum of **{limit}** character(s). Delete one first "
                            "(`/character delete`) or ask staff to raise the limit.")
        if any(ch["name"].lower() == name.lower() for ch in chars):
            raise EconError(f"You already have a character called **{name}**.")
        n, cid = await ex("INSERT IGNORE INTO characters (guild_id,user_id,name,is_main,active) VALUES (%s,%s,%s,0,0)",
                          gid, uid, name)
        if not n:
            raise EconError(f"You already have a character called **{name}**.")
        if c["start_balance"] > 0:
            await transfer(gid, [(MINT, ("cbank", cid), c["start_balance"])], uid, "start", f"Starting balance ({name})")
        txt = f"🏦 Starting bank: **{money(c, c['start_balance'])}** · 🎒 empty inventory"
        if switch_now:
            old, _ = await switch_to(gid, uid, name, uid)
            txt += f"\nYou're now playing as **{name}**. **{old['name']}**'s money and items are kept safe — " \
                   f"`/character switch` to go back."
        else:
            txt += "\nUse `/character switch` to play as them."
        await log(self.bot, gid, emb("🎭 Character created", f"{inter.user.mention} created **{name}**", GREEN))
        await reply(inter, embed=emb(f"🎭 Character **{name}** created", txt, GREEN), ephemeral=True)

    @character.command(name="switch", description="Play as another of your characters")
    @app_commands.autocomplete(name=char_ac)
    async def switch(self, inter: discord.Interaction, name: str):
        gid, uid = inter.guild_id, inter.user.id
        c = await cfg(gid)
        await ensure_main(gid, inter.user)
        old, new = await switch_to(gid, uid, name, uid)
        u = await q1("SELECT wallet, bank FROM users WHERE guild_id=%s AND user_id=%s", gid, uid)
        await log(self.bot, gid, emb("🎭 Character switched", f"{inter.user.mention}: **{old['name']}** → **{new['name']}**", BLUE))
        await reply(inter, embed=emb(f"🎭 Now playing as **{new['name']}**",
                                     f"👛 {money(c, u['wallet'])} · 🏦 {money(c, u['bank'])}\n"
                                     f"**{old['name']}**'s money and items are parked until you switch back.", GREEN),
                    ephemeral=True)

    @character.command(name="rename", description="Rename one of your characters")
    @app_commands.autocomplete(name=char_ac)
    async def rename(self, inter: discord.Interaction, name: str, new_name: str):
        gid, uid = inter.guild_id, inter.user.id
        await ensure_main(gid, inter.user)
        new_name = clean_name(new_name)
        chars = await characters_of(gid, uid)
        ch = find(chars, name)
        if any(o["name"].lower() == new_name.lower() and o["id"] != ch["id"] for o in chars):
            raise EconError(f"You already have a character called **{new_name}**.")
        await ex("UPDATE characters SET name=%s WHERE id=%s", new_name, ch["id"])
        await reply(inter, embed=emb("🎭 Character renamed", f"**{ch['name']}** → **{new_name}**", GREEN), ephemeral=True)

    @character.command(name="delete", description="Delete one of your extra characters (its money and items are lost)")
    @app_commands.autocomplete(name=char_ac)
    async def delete(self, inter: discord.Interaction, name: str):
        gid, uid = inter.guild_id, inter.user.id
        c = await cfg(gid)
        await ensure_main(gid, inter.user)
        ch = find(await characters_of(gid, uid), name)
        if ch["is_main"]:
            raise EconError("You can't delete your main character.")
        if ch["active"]:
            raise EconError("Switch to another character before deleting this one.")
        it = await q1("SELECT COALESCE(SUM(qty),0) t FROM character_items WHERE char_id=%s", ch["id"])
        if not await confirm(inter, f"Delete **{ch['name']}**? Their 👛 {money(c, ch['wallet'])}, 🏦 {money(c, ch['bank'])} "
                             f"and {int(it['t'])} item(s) will be **gone for good**.", "Delete"):
            return
        async with core.IMPORT_LOCK:
            row = await q1("SELECT * FROM characters WHERE id=%s AND active=0", ch["id"])
            if not row:
                raise EconError("That character changed — try again.")
            legs = []
            for col in ("wallet", "bank"):
                v = row[col]
                if v > 0:
                    legs.append((("c" + col, row["id"]), BURN, v))
                elif v < 0:
                    legs.append((MINT, ("c" + col, row["id"]), -v))

            async def extra(cur):
                await cur.execute("DELETE FROM character_items WHERE char_id=%s", (row["id"],))
                await cur.execute("DELETE FROM characters WHERE id=%s AND active=0", (row["id"],))
            await transfer(gid, legs, uid, "character", f"Deleted character {row['name']}", extra=extra)
        await log(self.bot, gid, emb("🎭 Character deleted", f"{inter.user.mention} deleted **{row['name']}**", RED))
        await reply(inter, embed=emb("🗑️ Character deleted", f"**{row['name']}** is gone.", GOLD), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Characters(bot))
